"""Snapshot write: commit the working tree → `git bundle` it → base64 it over the
`/exec` endpoint → `put` to Blob.

`git bundle create <file> HEAD` CARRIES COMPLETE HISTORY, not just the current tree — it names
HEAD and git walks its ancestry — and the health verdict identifies an app by its ROOT COMMIT,
which only survives a restore because that history does. WRITTEN by the session API; READ also
by `submit`, which copies the snapshot to an immutable per-submission key, so a swallowed
`write_snapshot` failure costs the citizen their latest build, not just their resume, silently.

CONCURRENCY: two snapshots of one app can overlap (Save is not gated on an in-flight session), so
this module owns both halves — a per-call bundle path and a per-app lock (`_BUNDLE_PREFIX`,
`_serialized_per_app`)."""

from __future__ import annotations

import asyncio
import base64
import enum
import secrets
import time
import uuid
from collections import Counter
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Final

import structlog

from src.services.build_sessions.integrity import container_state, is_the_untouched_starter
from src.services.sandbox import SandboxClient, SandboxError, SandboxHandle
from src.services.storage import (
    SNAPSHOT_HEAD_METADATA_KEY,
    get_storage,
    quarantine_key,
    snapshot_key,
)
from src.services.storage.base import ObjectStorage
from src.services.storage.bundle import BUNDLE_CONTENT_TYPE, parse_bundle_head_sha

_log = structlog.get_logger()

# THE AGENT DOES NOT COMMIT AS IT WORKS — this script is the platform's single commit, taken at
# the turn boundary as step one of every bundle. So "HEAD unchanged + a dirty tree" is the normal
# shape of a building turn, and any reader that decides from a sha taken before this script runs
# is reading the previous turn's.

_NO_REPOSITORY_EXIT: Final = 64
"""The exit code `_COMMIT_SCRIPT`'s leading probe reserves for "there is no repository here".

A DISCRIMINATOR, not a guess: a full disk and a locked `.git/index` also fail the commit step, and
`WorkspaceHasNoRepositoryError` is raised on this code alone. Every other non-zero exit stays the
generic snapshot failure. Sized out of the way of git's own 1/128."""

# Drops from the index every tracked file the IMAGE's exclude file ignores (`core.excludesFile`,
# baked from `sandbox/platform-owned.gitignore`). The files stay on disk. Tracked, the dev server's
# output makes a save of unchanged code a new commit on every boot, and a new commit cancels the
# approval pinned to the old one.
#
# Only that file decides: `--exclude-standard` would also obey a `.gitignore` the agent wrote, and
# untrack the code it lists. `update-index --force-remove`, not `git rm --cached`, which refuses a
# file whose index matches neither HEAD nor the disk and would then fail every Save. The listing
# goes through a file because `sh` has no `pipefail`: a listing that fails, as it does when the
# exclude file is missing, untracks nothing. An image that sets no exclude file skips the step.
_UNTRACK_WHAT_THE_IMAGE_IGNORES: Final = (
    "excludes=$(git config --path --get core.excludesFile) && listing=$(mktemp) && { "
    'git ls-files -ci -z --exclude-from="$excludes" > "$listing" '
    '&& git update-index --force-remove -z --stdin < "$listing"; rm -f "$listing"; }; '
)

# THIS SCRIPT NEVER CREATES A REPOSITORY. The repo is seeded at provision
# (`sandbox/client._INIT_REPO_SCRIPT`), so a workspace that reaches here without one has LOST it —
# and a root commit written here would hold the finished app, which makes "is this still the
# starter page?" compare the app against itself forever. Refuse instead; the next turn's integrity
# verdict routes a repo-less container into quarantine-and-restore.
#
# The probe is a shell builtin, not a git command: a git that fails for any other reason, killed
# short of memory say, must not read as a lost repository, because a caller destroys on that.
#
# Commit only when something is staged (`git commit` exits non-zero on a clean tree); a no-change
# re-snapshot still bundles the existing HEAD below.
_COMMIT_SCRIPT = (
    f"[ -e .git ] || exit {_NO_REPOSITORY_EXIT}; "
    + _UNTRACK_WHAT_THE_IMAGE_IGNORES
    + "git add -A && { git diff --cached --quiet || git commit -q -m bial-snapshot; }"
)
# The on-disk bundle path is PER-CALL, never the fixed `app.bundle` it used to be. Two snapshots
# can run against one container at the same time — the commonest pair being a user clicking Save
# while another writer bundles the same tree — and a shared name made them corrupt each other two
# ways: one call's `rm -f` (the cleanup below) deleted the file the other had not finished
# base64-ing, and `base64` could read a path `git bundle create` was still writing. Either way the
# short read was uploaded, and `put` is an unconditional overwrite into a store with neither
# versioning nor soft delete — so a truncated bundle landed on top of the only copy of the user's
# work. `secrets.token_hex` (not a counter) so the name cannot collide across replicas either.
#
# WRITTEN OUTSIDE THE WORKTREE, under /tmp, so no ignore rule has to be right: a bundle left in
# `/workspace/app` and missed by one would be committed by the next save's `git add -A`, multi-MB
# of binary compounding into every later bundle.
_BUNDLE_PREFIX = "/tmp/bial-snapshot"

# Every exec here is bounded. The client default is 900 s per call (`sandbox/client.py`), and
# these four now run on the PER-TURN path inside `asyncio.shield` — so an unbounded wait would
# hold the user's one-per-user build slot and their conversation guard for the better part of an
# hour against a container that merely stopped answering. Sized like the liveness collector's
# 60 s: enough for a large tree over `/exec`, nowhere near enough to strand a session.
SNAPSHOT_EXEC_TIMEOUT_SECONDS: Final = 120

SNAPSHOT_EXECS: Final = 4
"""How many bounded execs one `write_snapshot` runs: commit, bundle, base64, and the cleanup in
the `finally`. Named beside the per-exec bound so the product of the two is a number a caller can
derive rather than count by reading this file."""

#: Fields: `app_id`, `lock_wait_ms`, `commit_ms`, `bundle_ms`, `base64_ms`, `cleanup_ms`,
#: `store_ms`. Save is synchronous in-request with no client-side timeout, so this is the only
#: record of which of the three candidates — the four execs, the per-app queue, or the blob
#: write — a slow save actually lost its time to. One event per save, success or failure; a
#: step never reached (a failed exec, or a write-back that skipped the starter template)
#: stays `None`.
SNAPSHOT_STEP_TIMINGS_EVENT: Final = "snapshot_step_timings"


@dataclass
class _SaveStepTimings:
    """Filled in as one save's steps complete, across two scopes: the per-app lock wait and the
    store write happen in the writer, the four execs happen inside `_bundle_the_tree`. MUTABLE
    and passed in rather than returned, so a step that raises still leaves every step before it
    on the record — a return value cannot do that once the raise has already unwound past it."""

    lock_wait_ms: int | None = None
    commit_ms: int | None = None
    bundle_ms: int | None = None
    base64_ms: int | None = None
    cleanup_ms: int | None = None
    store_ms: int | None = None


# One serialization lock per app, plus a holder+waiter count so the entry can be dropped when it
# is provably idle. Unique bundle names above already make a concurrent pair non-destructive; this
# additionally stops them CONTENDING — two `git add -A && git commit` runs against one worktree
# race on `.git/index.lock`, and the loser exits non-zero, which surfaces to the user as a failed
# Save. It also keeps a slower writer from overwriting a newer bundle at the same key with an
# older tree. Process-local, which matches the single-replica deploy contract the reaper already
# depends on (`reaper.py`).
_app_locks: dict[uuid.UUID, asyncio.Lock] = {}
_app_lock_users: Counter[uuid.UUID] = Counter()


@asynccontextmanager
async def _serialized_per_app(app_id: uuid.UUID) -> AsyncIterator[None]:
    """Hold the per-app snapshot lock, evicting it once nobody holds OR wants it.

    The count is incremented BEFORE the first await, so it covers waiters as well as the holder.
    Pruning on `Lock.locked()` alone would not be safe: `release()` clears the flag and only
    schedules the next waiter, so a lock with a queued waiter reads as unlocked — dropping it
    there would let the next caller mint a fresh `Lock` and shatter mutual exclusion.
    """
    lock = _app_locks.setdefault(app_id, asyncio.Lock())
    _app_lock_users[app_id] += 1
    try:
        async with lock:
            yield
    finally:
        _app_lock_users[app_id] -= 1
        if _app_lock_users[app_id] <= 0:
            del _app_lock_users[app_id]
            _app_locks.pop(app_id, None)


@dataclass(frozen=True)
class Destination:
    """WHERE a bundle goes. Two of them, and they are not interchangeable.

    A VALUE OBJECT RATHER THAN AN ENUM, because a quarantine key carries the instant it was
    taken, so it cannot be a bare constant. Keeping the key-building here means every writer in
    the system names its destination in the same vocabulary, and nothing outside this module has
    to know that a key is a string at all."""

    key: str

    @classmethod
    def saved(cls, app_id: uuid.UUID) -> Destination:
        """The citizen's app, as a relaunch hands it back.

        Two writers: the citizen's own Save, and `write_the_tree_back` when the platform is
        about to destroy the container holding this tree."""
        return cls(snapshot_key(app_id))

    @classmethod
    def quarantine(cls, app_id: uuid.UUID, taken_at: datetime) -> Destination:
        """A tree a restore is about to write over. Never overwritten by a later occurrence."""
        return cls(quarantine_key(app_id, taken_at))


@dataclass(frozen=True)
class _BundledTree:
    """A tree already committed, bundled and read back out of the container."""

    head_sha: str
    data: bytes


async def write_snapshot(
    sandbox_client: SandboxClient,
    handle: SandboxHandle,
    app_id: uuid.UUID,
    *,
    destination: Destination | None = None,
) -> str:
    """Snapshot the sandbox's current tree to Blob and return its HEAD sha.

    Step 1 of the ordered end — the caller runs teardown + release AFTER this returns.
    `destination` defaults to the user's SAVED bundle. The head sha is stamped into the object's
    metadata and callers compare THAT, not `last_modified`: Azure stamps mtimes in whole seconds,
    so two writes in the same second cannot be told apart by time. Serialized per app: callers
    queue, never race.

    Emits `SNAPSHOT_STEP_TIMINGS_EVENT` once, in the `finally`, whether this returns or raises —
    a manual Save can queue behind a write-back holding the same app's lock, and `lock_wait_ms`
    is the only place that queue is visible at all."""
    key = (destination or Destination.saved(app_id)).key
    timings = _SaveStepTimings()
    lock_wait_started = time.monotonic()
    try:
        async with _serialized_per_app(app_id):
            timings.lock_wait_ms = _elapsed_ms(lock_wait_started)
            store = _the_store_first()
            tree = await _bundle_the_tree(sandbox_client, handle, timings)
            await _timed_store(store, key, tree, timings)
            return tree.head_sha
    finally:
        # SUPPRESSED, because this runs in a `finally` on the save path: a save that failed is
        # propagating an exception through here, and an instrument that raised would replace the
        # citizen's real failure with its own. A measurement is never worth a diagnosis.
        with suppress(Exception):
            _log.info(SNAPSHOT_STEP_TIMINGS_EVENT, app_id=str(app_id), **asdict(timings))


class SavedCopyOutcome(enum.StrEnum):
    """What happened to one teardown's attempt to leave a container's work in the saved copy."""

    #: The saved copy now holds this container's tree.
    WRITTEN = "written"
    #: Nothing was written: this container is still serving the untouched starter template, so
    #: there is no app here yet to save.
    SKIPPED = "skipped"


@dataclass(frozen=True)
class SavedCopyWrite:
    outcome: SavedCopyOutcome
    #: The sha this write-back bundled. `None` on `SKIPPED`, which bundles nothing.
    bundled_head: str | None = None


async def write_the_tree_back(
    sandbox_client: SandboxClient,
    handle: SandboxHandle,
    app_id: uuid.UUID,
) -> SavedCopyWrite:
    """Leave a dying container's tree in the citizen's saved copy.

    RUN ONLY WHERE THE CONTAINER IS ABOUT TO BE DESTROYED. Nothing else about the tree survives
    the delete, so it goes into the one slot a relaunch reads. The single exception is a
    container still holding the untouched starter template: writing that over a saved app is the
    loss this skip exists to prevent, and it is the reason the probe reads the CONTAINER rather
    than the store — a reverted container and a first write are indistinguishable from the store.

    Raises `SandboxError` when the container will not answer: an unestablished fact on a path
    that ends in an ARM delete is not an outcome to return, and both callers spare the container
    on it. Raises `WorkspaceHasNoRepositoryError` when the commit finds no repository AND the state
    probe read no HEAD, and both callers reclaim the container on that: no later attempt could
    save it. Two reads that disagree are a plain `SandboxError`, which spares."""
    timings = _SaveStepTimings()
    lock_wait_started = time.monotonic()
    try:
        async with _serialized_per_app(app_id):
            timings.lock_wait_ms = _elapsed_ms(lock_wait_started)
            store = _the_store_first()
            state = await container_state(sandbox_client, handle)
            if state is None:
                raise SandboxError("the container would not answer its state probe")
            if is_the_untouched_starter(state):
                return SavedCopyWrite(SavedCopyOutcome.SKIPPED)
            try:
                tree = await _bundle_the_tree(sandbox_client, handle, timings)
            except WorkspaceHasNoRepositoryError as exc:
                if state.head is None:
                    raise
                raise SandboxError(
                    "the commit found no repository where the state probe had read a HEAD"
                ) from exc
            await _timed_store(store, snapshot_key(app_id), tree, timings)
            # The one line that says a citizen's unsaved afternoon was kept. Nobody presses
            # anything on this path, so without it a preserved app and a lost one leave the
            # same trace.
            _log.info(
                "wrote a dying container's tree to the saved copy",
                app_id=str(app_id),
                bundled_head=tree.head_sha,
            )
            return SavedCopyWrite(SavedCopyOutcome.WRITTEN, bundled_head=tree.head_sha)
    finally:
        # SUPPRESSED, because this runs in a `finally` on a path that may be propagating the
        # citizen's real failure, and an instrument that raised would replace it with its own.
        with suppress(Exception):
            _log.info(SNAPSHOT_STEP_TIMINGS_EVENT, app_id=str(app_id), **asdict(timings))


def _the_store_first() -> ObjectStorage:
    """Resolve the store BEFORE doing any work. On a storage-disabled deployment this
    raises here, so the turn does not commit, bundle and base64 a whole tree over `/exec` only to
    discover at the upload that there is nowhere to put it."""
    return get_storage()


async def _store_it(store: ObjectStorage, key: str, tree: _BundledTree) -> None:
    """STAMP THE TREE, not just the bytes. `last_modified` is whole seconds on Azure, so two
    writes inside one second are indistinguishable by time. Recording which tree each bundle
    holds is what lets a reader answer "same content?" without a download — the comparison the
    save indicator and the integrity verdict both rest on."""
    await store.put(
        key,
        tree.data,
        content_type=BUNDLE_CONTENT_TYPE,
        metadata={SNAPSHOT_HEAD_METADATA_KEY: tree.head_sha},
    )


def _elapsed_ms(started: float) -> int:
    return int((time.monotonic() - started) * 1000)


async def _timed_store(
    store: ObjectStorage, key: str, tree: _BundledTree, timings: _SaveStepTimings
) -> None:
    """`_store_it`, timed onto the shared accumulator. A separate wrapper rather than inlining at
    each call site, so every writer times the same way."""
    started = time.monotonic()
    await _store_it(store, key, tree)
    timings.store_ms = _elapsed_ms(started)


class WorkspaceHasNoRepositoryError(SandboxError):
    """The container's workspace carries no git repository, so there is nothing to snapshot.

    A `SandboxError` so every caller that already narrow-catches one keeps working; its own type
    so "this container lost its repository" is told apart from "the commit step failed", which a
    full disk or a locked index also produces. Raised only on `_NO_REPOSITORY_EXIT`."""

    def __init__(self) -> None:
        super().__init__("the workspace has no git repository to snapshot")


async def _bundle_the_tree(
    sandbox_client: SandboxClient,
    handle: SandboxHandle,
    timings: _SaveStepTimings,
) -> _BundledTree:
    """Commit whatever is in the worktree, bundle it, and read it back out of the container.

    Times each of the four execs onto `timings`; the caller owns logging the event once its own
    lock-wait and store steps are known too."""
    bundle_name = f"{_BUNDLE_PREFIX}.{secrets.token_hex(8)}"
    run_command = sandbox_client.exec  # aliased to keep the call off the JS-oriented exec guard
    # Every step's exit code is checked (a non-zero exit is a NORMAL ExecResult): a failed
    # commit or bundle must abort HERE, never fall through to base64-ing whatever happens to be
    # on disk and uploading it as "latest".
    commit_started = time.monotonic()
    commit = await run_command(
        handle, ["sh", "-c", _COMMIT_SCRIPT], timeout_s=SNAPSHOT_EXEC_TIMEOUT_SECONDS
    )
    timings.commit_ms = _elapsed_ms(commit_started)
    if commit.exit == _NO_REPOSITORY_EXIT:
        raise WorkspaceHasNoRepositoryError
    if commit.exit != 0:
        raise SandboxError(f"snapshot commit failed (exit {commit.exit})")
    try:
        # Bare argv, no shell: `bundle_name` is hex from `secrets`, but keeping the interpolated
        # path off a command line is the property worth having rather than the audit.
        bundle_started = time.monotonic()
        bundle = await run_command(
            handle,
            ["git", "bundle", "create", bundle_name, "HEAD"],
            timeout_s=SNAPSHOT_EXEC_TIMEOUT_SECONDS,
        )
        timings.bundle_ms = _elapsed_ms(bundle_started)
        if bundle.exit != 0:
            raise SandboxError(f"snapshot bundle failed (exit {bundle.exit})")
        base64_started = time.monotonic()
        result = await run_command(
            handle, ["base64", bundle_name], timeout_s=SNAPSHOT_EXEC_TIMEOUT_SECONDS
        )
        timings.base64_ms = _elapsed_ms(base64_started)
        if result.exit != 0:
            raise SandboxError(f"snapshot bundle read failed (exit {result.exit})")
        data = base64.b64decode(result.stdout)
        # Parse before the upload, not after: this both validates what we are about to store
        # and gives the caller the HEAD sha, which is what lets a reader compare two bundles
        # for "which of these is the newer tree" without downloading both.
        return _BundledTree(head_sha=parse_bundle_head_sha(data), data=data)
    finally:
        # On the failure path too: each bundle is multi-MB, and the container outlives the save.
        cleanup_started = time.monotonic()
        with suppress(SandboxError):
            await run_command(
                handle, ["rm", "-f", bundle_name], timeout_s=SNAPSHOT_EXEC_TIMEOUT_SECONDS
            )
        timings.cleanup_ms = _elapsed_ms(cleanup_started)


class NothingSavedToGoBackToError(Exception):
    """A discard was asked for, and this app has no saved version to go back to."""

    def __init__(self, app_id: uuid.UUID) -> None:
        super().__init__(f"app {app_id} has no saved version")
        self.app_id = app_id


@dataclass(frozen=True)
class SavedVersion:
    """The version a discard put back, and where the tree it replaced was parked."""

    head_sha: str
    saved_at: datetime | None
    parked_at: str


async def discard_back_to_saved(
    sandbox_client: SandboxClient,
    handle: SandboxHandle,
    app_id: uuid.UUID,
    *,
    taken_at: datetime,
) -> SavedVersion:
    """Put the saved version back into the live container, keeping the tree it replaces.

    Under the per-app lock, so a Save or a write-back waits: park the live tree in quarantine,
    then reset the container. The store moves first: if the reset fails, the container still
    holds work descending from the saved head, and the next turn finds it intact."""
    store = _the_store_first()
    async with _serialized_per_app(app_id):
        meta = await store.head(snapshot_key(app_id))
        if meta is None:
            raise NothingSavedToGoBackToError(app_id)
        data = await store.get(snapshot_key(app_id))
        saved = _BundledTree(head_sha=parse_bundle_head_sha(data), data=data)
        parked = Destination.quarantine(app_id, taken_at).key
        live = await _bundle_the_tree(sandbox_client, handle, _SaveStepTimings())
        await _store_it(store, parked, live)
        await sandbox_client.reset_to_bundle(handle, saved.data)
    return SavedVersion(head_sha=saved.head_sha, saved_at=meta.last_modified, parked_at=parked)
