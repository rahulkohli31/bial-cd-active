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
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager, suppress
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Final, Literal

import structlog

from src.services.build_sessions.alarms import RECOVERY_WRITE_DID_NOT_LAND_EVENT
from src.services.build_sessions.integrity import (
    Ancestry,
    container_state,
    holds_unsaved_work,
    is_a_commit_sha,
)
from src.services.sandbox import SandboxClient, SandboxError, SandboxHandle
from src.services.storage import (
    SNAPSHOT_HEAD_METADATA_KEY,
    StorageError,
    StorageUnconfiguredError,
    all_keys_under,
    divert_key,
    divert_prefix,
    get_storage,
    head_sha_from_metadata,
    quarantine_key,
    quarantine_prefix,
    recovery_key,
    snapshot_key,
    version_key,
)
from src.services.storage.base import ObjectStorage
from src.services.storage.bundle import BUNDLE_CONTENT_TYPE, parse_bundle_head_sha

_log = structlog.get_logger()

# THE AGENT DOES NOT COMMIT AS IT WORKS — this script is the platform's single commit, taken at
# the turn boundary as step one of every bundle. So "HEAD unchanged + a dirty tree" is the normal
# shape of a building turn, and any reader that decides from a sha taken before this script runs
# is reading the previous turn's.
#
# The baked image ships /workspace/app WITHOUT a `.git` (git identity, `init.defaultBranch`,
# and `safe.directory` are baked system-wide in Dockerfile.sandbox), so the FIRST snapshot must
# `git init` — idempotent on every later snapshot (mirrors sandbox/scripts/snapshot.sh). Commit
# only when something is staged (`git commit` exits non-zero on a clean tree); a no-change
# re-snapshot still bundles the existing HEAD below.
_COMMIT_SCRIPT = (
    "git init -q && git add -A && { git diff --cached --quiet || git commit -q -m bial-snapshot; }"
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
# WRITTEN OUTSIDE THE WORKTREE, under /tmp, and that is the load-bearing half. A bundle inside
# `/workspace/app` is only kept out of the user's repo by the template's `.gitignore` — and a
# RESTORED container carries the `.gitignore` committed in its own bundle, which for every app
# created before this change lists the literal `/app.bundle`, not the randomized names above. So
# the ignore would silently stop matching exactly where it was needed, and the next snapshot's
# `git add -A` would commit multi-MB of binary into the user's tree, permanently, compounding
# into every later bundle. /tmp is outside the repo, so no ignore rule has to be right.
# Mirrors what `sandbox/scripts/snapshot.sh` already does with `mktemp`.
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
#: step never reached (a failed exec, or the recovery guard's no-op skip) stays `None`.
SNAPSHOT_STEP_TIMINGS_EVENT: Final = "snapshot_step_timings"


@dataclass
class _SaveStepTimings:
    """Filled in as one save's steps complete, across two scopes: the per-app lock wait and the
    store write happen in `write_snapshot`/`write_recovery_copy`, the four execs happen inside
    `_bundle_the_tree`. MUTABLE and passed in rather than returned, so a step that raises still
    leaves every step before it on the record — a return value cannot do that once the raise has
    already unwound past it."""

    lock_wait_ms: int | None = None
    commit_ms: int | None = None
    bundle_ms: int | None = None
    base64_ms: int | None = None
    cleanup_ms: int | None = None
    store_ms: int | None = None
    #: The second write, when a save also records a version. Separate from `store_ms` so the
    #: cost of keeping a history is a measurement rather than a claim.
    version_store_ms: int | None = None


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
    """WHERE a bundle goes. Five of them, and they are not interchangeable.

    A VALUE OBJECT RATHER THAN AN ENUM, because three of the five are per-occurrence: a
    quarantine, divert or version key carries the instant it was taken, so it cannot be a bare
    constant. Keeping the
    key-building here (rather than exposing `_write_snapshot_locked`, which is private for a
    reason) means every writer in the system names its destination in the same vocabulary, and
    nothing outside this module has to know that a key is a string at all."""

    key: str

    @classmethod
    def saved(cls, app_id: uuid.UUID) -> Destination:
        """The user's explicit Save, and the key nothing may write unasked.

        ONE PLATFORM WRITER REACHES IT, and only through `write_saved_copy_under_guard`: a
        shutdown the citizen never asked for leaves their work here, but only a tree proved to
        descend from what is already here. Anything else — including this destination handed to
        `write_snapshot`, which writes unconditionally — is the user's own click."""
        return cls(snapshot_key(app_id))

    @classmethod
    def recovery(cls, app_id: uuid.UUID) -> Destination:
        """The platform's autosave. Prefer `write_recovery_copy`, which guards the promotion —
        this is the raw destination, for the operator promote path that has already
        decided."""
        return cls(recovery_key(app_id))

    @classmethod
    def quarantine(cls, app_id: uuid.UUID, taken_at: datetime) -> Destination:
        """A tree a restore is about to write over. Never overwritten by a later occurrence."""
        return cls(quarantine_key(app_id, taken_at))

    @classmethod
    def version(cls, app_id: uuid.UUID, saved_at: datetime) -> Destination:
        """One entry in the app's saved history. Never overwritten by a later save, and never
        deleted by the list that offers it — a version that falls off the list stops being
        offered, not stored."""
        return cls(version_key(app_id, saved_at))

    @classmethod
    def divert(cls, app_id: uuid.UUID, taken_at: datetime) -> Destination:
        """A tree the recovery guard refused to promote. Never overwritten by a later
        occurrence."""
        return cls(divert_key(app_id, taken_at))


@dataclass(frozen=True)
class _BundledTree:
    """A tree already committed, bundled and read back out of the container."""

    head_sha: str
    data: bytes


class RecoveryOutcome(enum.StrEnum):
    """What happened to one turn's attempt to make its work durable."""

    #: The recovery slot now holds this turn's tree.
    WRITTEN = "written"
    #: Nothing to write — the tree was clean and HEAD is where the copy already is. Normal, and
    #: the only outcome that does not alarm.
    SKIPPED = "skipped"
    #: The guard would not promote this tree over the existing copy, and the bundle was preserved
    #: under `divert_key` rather than thrown away.
    DIVERTED = "diverted"


@dataclass(frozen=True)
class RecoveryWrite:
    outcome: RecoveryOutcome
    reason: str
    #: The sha the recovery slot held before this turn, when it held one.
    recorded_head: str | None = None
    #: The sha this turn actually bundled.
    bundled_head: str | None = None
    #: Set on `DIVERTED` — where the refused tree went, so an operator can find it.
    diverted_to: str | None = None


async def write_snapshot(
    sandbox_client: SandboxClient,
    handle: SandboxHandle,
    app_id: uuid.UUID,
    *,
    destination: Destination | None = None,
    also: Destination | None = None,
) -> str:
    """Snapshot the sandbox's current tree to Blob and return its HEAD sha.

    Step 1 of the ordered end — the caller runs teardown + release AFTER this returns.
    `destination` defaults to the user's SAVED bundle; the autosave goes through
    `write_recovery_copy`, the same write with a guard in front. The head sha is stamped into the
    object's metadata and callers compare THAT, not `last_modified`: Azure stamps mtimes in whole
    seconds, so a Save and an autosave in the same second cannot be told apart by time, and the tie
    would restore an older tree over newer work. Serialized per app: callers queue, never race.

    Emits `SNAPSHOT_STEP_TIMINGS_EVENT` once, in the `finally`, whether this returns or raises —
    a manual Save can queue behind an autosave holding the same app's lock, and `lock_wait_ms` is
    the only place that queue is visible at all.

    ★ `also` STORES THE SAME BUNDLE TWICE, AND THAT IS WHY A HISTORY IS CHEAP. The forty
    seconds a production save was measured at is `_bundle_the_tree` — commit, `git bundle`, and
    base64 back out of the container. Recording a version adds one `put` of bytes already in
    memory, inside a lock already held: no second commit, no second container round trip. The
    store has no server-side copy, so bundling once and storing twice is also the only shape that
    avoids reading the whole tree back through this process.

    THE SAVED KEY IS WRITTEN FIRST, deliberately. A crash between the two writes leaves a save
    that happened with one version missing, which is a history with a gap; the other order leaves
    a version offered whose content was never what the citizen saved."""
    key = (destination or Destination.saved(app_id)).key
    timings = _SaveStepTimings()
    lock_wait_started = time.monotonic()
    try:
        async with _serialized_per_app(app_id):
            timings.lock_wait_ms = _elapsed_ms(lock_wait_started)
            store = _the_store_first()
            tree = await _bundle_the_tree(sandbox_client, handle, timings)
            await _timed_store(store, key, tree, timings)
            if also is not None:
                started = time.monotonic()
                await _store_it(store, also.key, tree)
                timings.version_store_ms = _elapsed_ms(started)
            return tree.head_sha
    finally:
        # SUPPRESSED, because this runs in a `finally` on the save path: a save that failed is
        # propagating an exception through here, and an instrument that raised would replace the
        # citizen's real failure with its own. A measurement is never worth a diagnosis.
        with suppress(Exception):
            _log.info(SNAPSHOT_STEP_TIMINGS_EVENT, app_id=str(app_id), **asdict(timings))


# HOW MANY TIMES IN A ROW THIS APP'S RECOVERY WRITE HAS BEEN REFUSED. Process-local like the
# snapshot locks, and self-pruning: any outcome that is not a refusal drops the entry, so a streak
# only ever means consecutive refusals seen by this process.
_consecutive_diverts: dict[uuid.UUID, int] = {}


def consecutive_diverts(app_id: uuid.UUID) -> int:
    """How many turns in a row have failed to promote a tree into this app's recovery slot."""
    return _consecutive_diverts.get(app_id, 0)


def reset_divert_streaks_for_tests() -> None:
    """Drop the per-app refusal counters. Process-local state, so a streak must not leak."""
    _consecutive_diverts.clear()


async def write_recovery_copy(
    sandbox_client: SandboxClient,
    handle: SandboxHandle,
    app_id: uuid.UUID,
    *,
    taken_at: datetime,
) -> RecoveryWrite:
    """The turn-end autosave, with a guard that will not overwrite a good copy with a bad tree.

    THE NO-OP SKIP IS DECIDED ON THE BUNDLED SHA, AND THAT ORDERING IS THE WHOLE TRICK.
    `_COMMIT_SCRIPT` commits as step ONE inside the bundle, so a naive "skip when HEAD has not
    moved" reading the sha BEFORE it would discard every turn's recovery copy: the agent does not
    commit as it works, so "HEAD unchanged + dirty tree" is the normal shape of a building turn
    (`test_a_dirty_tree_at_unchanged_head_still_writes_a_recovery_copy`). NEVER RAISES FOR A
    REFUSAL — only bundle/upload failure is raised, at the call site that saw it throw.

    Emits `SNAPSHOT_STEP_TIMINGS_EVENT` once, in the `finally`, whether this returns or raises —
    `store_ms` stays `None` on the `SKIPPED` outcome below, the one arm that writes nothing."""
    timings = _SaveStepTimings()
    lock_wait_started = time.monotonic()
    try:
        async with _serialized_per_app(app_id):
            timings.lock_wait_ms = _elapsed_ms(lock_wait_started)
            store = _the_store_first()
            meta = await store.head(recovery_key(app_id))
            recorded = head_sha_from_metadata(meta.metadata if meta else None)
            tree = await _bundle_the_tree(sandbox_client, handle, timings)

            if meta is None:
                # NO OBJECT AT ALL. There is nothing to overwrite and nothing to compare
                # against, so the first write simply proceeds.
                await _timed_store(store, recovery_key(app_id), tree, timings)
                _consecutive_diverts.pop(app_id, None)
                return RecoveryWrite(
                    RecoveryOutcome.WRITTEN,
                    "no previous copy to protect",
                    bundled_head=tree.head_sha,
                )

            if recorded is None:
                # AN OBJECT IS THERE AND WE CANNOT COMPARE AGAINST IT — a bundle predating the
                # head stamp. That is not a licence to overwrite it: an app whose container has
                # reverted has exactly this shape, so writing would stamp the reverted tree over
                # the user's only durable copy, into a store with neither versioning nor soft
                # delete — and the reaper reads a WRITTEN as proof the work is safe and deletes
                # the container in the same call. Diverted instead, so the bytes are kept for an
                # operator to promote. Same reasoning `_where_head_sits_relative_to` applies to a
                # `recorded` that is not sha-shaped.
                where = divert_key(app_id, taken_at)
                await _timed_store(store, where, tree, timings)
                _consecutive_diverts[app_id] = _consecutive_diverts.get(app_id, 0) + 1
                _log.error(
                    RECOVERY_WRITE_DID_NOT_LAND_EVENT,
                    app_id=str(app_id),
                    reason=RecoveryOutcome.DIVERTED.value,
                    recorded_head=None,
                    bundled_head=tree.head_sha,
                    ancestry="uncomparable",
                    diverted_to=where,
                )
                return RecoveryWrite(
                    RecoveryOutcome.DIVERTED,
                    "the copy on record carries no head to compare against",
                    bundled_head=tree.head_sha,
                    diverted_to=where,
                )

            if tree.head_sha == recorded:
                # The commit step found nothing to commit AND the tree is where the copy already
                # is. Normal, and it must NOT alarm: this is every read-only turn.
                _consecutive_diverts.pop(app_id, None)
                return RecoveryWrite(
                    RecoveryOutcome.SKIPPED,
                    "the tree has not moved since the last copy",
                    recorded_head=recorded,
                    bundled_head=tree.head_sha,
                )

            ancestry = await _where_head_sits_relative_to(sandbox_client, handle, recorded)
            if ancestry is Ancestry.DESCENDANT:
                await _timed_store(store, recovery_key(app_id), tree, timings)
                _consecutive_diverts.pop(app_id, None)
                return RecoveryWrite(
                    RecoveryOutcome.WRITTEN,
                    "this turn built on the copy it is replacing",
                    recorded_head=recorded,
                    bundled_head=tree.head_sha,
                )

            # EVERYTHING ELSE DIVERTS. The tree in hand is not a descendant of the copy on
            # record — or we could not establish that it is — so promoting it would replace a
            # known-good bundle with one whose relationship to the user's work is unknown. The
            # bytes are kept rather than dropped: in a false refusal they are the newest copy of
            # somebody's afternoon.
            where = divert_key(app_id, taken_at)
            await _timed_store(store, where, tree, timings)
            _consecutive_diverts[app_id] = _consecutive_diverts.get(app_id, 0) + 1
            _log.error(
                RECOVERY_WRITE_DID_NOT_LAND_EVENT,
                app_id=str(app_id),
                reason=RecoveryOutcome.DIVERTED.value,
                recorded_head=recorded,
                bundled_head=tree.head_sha,
                ancestry=ancestry.value,
                diverted_to=where,
            )
            return RecoveryWrite(
                RecoveryOutcome.DIVERTED,
                f"the tree is {ancestry.value} of the copy on record",
                recorded_head=recorded,
                bundled_head=tree.head_sha,
                diverted_to=where,
            )
    finally:
        # SUPPRESSED, because this runs in a `finally` on the save path: a save that failed is
        # propagating an exception through here, and an instrument that raised would replace the
        # citizen's real failure with its own. A measurement is never worth a diagnosis.
        with suppress(Exception):
            _log.info(SNAPSHOT_STEP_TIMINGS_EVENT, app_id=str(app_id), **asdict(timings))


async def _where_head_sits_relative_to(
    sandbox_client: SandboxClient, handle: SandboxHandle, recorded: str
) -> Ancestry:
    """One exec: was this tree built on top of the one the recovery slot holds?

    A `recorded` sha that is not sha-shaped never reaches the shell, and comes back
    `REFERENCE_ABSENT` — which diverts, exactly as it should: metadata naming a tree we cannot
    ask about is not a licence to overwrite the object that metadata belongs to."""
    if not is_a_commit_sha(recorded):
        return Ancestry.REFERENCE_ABSENT
    state = await container_state(sandbox_client, handle, reference_sha=recorded)
    return state.ancestry if state is not None else Ancestry.UNREADABLE


SAVED_COPY_WRITE_DID_NOT_LAND_EVENT: Final = "saved_copy_write_did_not_land"
"""A shutdown's write-back could not be promoted into the saved copy, and the tree was parked.

Fields: `app_id`, `recorded_head`, `bundled_head`, `ancestry`, `diverted_to`. The two shas
together say why the promotion was refused; `diverted_to` is the key the tree is sitting under,
which the operator promote procedure takes as its input."""


class SavedCopyOutcome(enum.StrEnum):
    """What happened to one shutdown's attempt to leave a container's work in the saved copy."""

    #: The saved copy now holds this container's tree.
    WRITTEN = "written"
    #: Nothing to write — the tree holds nothing the saved copy does not already have.
    SKIPPED = "skipped"
    #: The guard would not promote this tree over the saved copy, and the bundle was preserved
    #: under `divert_key` rather than thrown away.
    DIVERTED = "diverted"


@dataclass(frozen=True)
class SavedCopyWrite:
    outcome: SavedCopyOutcome
    #: The sha the saved copy held before this write, when it held one.
    recorded_head: str | None = None
    #: The sha this shutdown actually bundled. `None` on `SKIPPED`, which bundles nothing.
    bundled_head: str | None = None
    #: Set on `DIVERTED` — where the refused tree went, so it can be offered back.
    diverted_to: str | None = None


async def write_saved_copy_under_guard(
    sandbox_client: SandboxClient,
    handle: SandboxHandle,
    app_id: uuid.UUID,
    *,
    taken_at: datetime,
) -> SavedCopyWrite:
    """Leave a shutting-down container's tree in the citizen's saved copy — but only if it can be
    shown to descend from what is there.

    `Destination.saved` is the one key a platform-initiated write must never touch, and a teardown
    nobody asked for is the most platform-initiated write there is. So the promotion demands
    positive proof of descent, and every other tree is PARKED under `divert_key` BEFORE this
    returns: in a false refusal those bytes are the newest copy of somebody's afternoon.

    NO PROOF MEANS NO PROMOTION, AND AN EMPTY SLOT IS NOT PROOF. A container that has reverted to
    its baked image presents exactly as a first write, and a template tree written into the saved
    copy becomes the newest thing an automatic restore can hand back — the loss this guard exists
    to prevent, performed by the guard.

    ONE PROBE ANSWERS BOTH QUESTIONS, and it runs BEFORE the bundle's commit step: the dirty read
    has to see the tree as the citizen left it, and ancestry taken before a commit still holds
    after one, since committing only ever adds a child on top of HEAD.

    Raises `SandboxError` when the container will not answer: an unestablished fact on a path that
    ends in an ARM delete is not an outcome to return."""
    timings = _SaveStepTimings()
    lock_wait_started = time.monotonic()
    try:
        async with _serialized_per_app(app_id):
            timings.lock_wait_ms = _elapsed_ms(lock_wait_started)
            store = _the_store_first()
            meta = await store.head(snapshot_key(app_id))
            recorded = head_sha_from_metadata(meta.metadata if meta else None)
            # A stamp that is not sha-shaped never reaches the shell, and refuses by the same
            # door an absent copy does: metadata naming a tree we cannot ask about is not a
            # licence to overwrite the object that metadata belongs to. The raw value is still
            # reported, so the alarm carries the stamp somebody has to go and look at.
            comparable = recorded if is_a_commit_sha(recorded) else None
            state = await container_state(sandbox_client, handle, reference_sha=comparable)
            if state is None:
                raise SandboxError("the container would not answer its state probe")

            # No comparable head on record is a refusal exactly as a non-descendant tree is: in
            # both, this tree cannot be SHOWN to contain the saved work, and the guard promotes
            # only on proof. The alarm below carries which of the two it was.
            refused = comparable is None or state.ancestry is not Ancestry.DESCENDANT
            if (
                comparable is not None
                and state.head == comparable
                and not holds_unsaved_work(state)
            ):
                return SavedCopyWrite(SavedCopyOutcome.SKIPPED, recorded_head=recorded)

            tree = await _bundle_the_tree(sandbox_client, handle, timings)
            if not refused:
                # THE PROOF IS IN REACHING HERE: a comparable head on record, and the probe
                # answering that this tree descends from it. This is the only line in the system
                # that writes the saved copy without a person having asked for it.
                await _timed_store(store, snapshot_key(app_id), tree, timings)
                return SavedCopyWrite(
                    SavedCopyOutcome.WRITTEN,
                    recorded_head=recorded,
                    bundled_head=tree.head_sha,
                )
            where = divert_key(app_id, taken_at)
            await _timed_store(store, where, tree, timings)
            _log.error(
                SAVED_COPY_WRITE_DID_NOT_LAND_EVENT,
                app_id=str(app_id),
                recorded_head=recorded,
                bundled_head=tree.head_sha,
                ancestry=state.ancestry.value,
                diverted_to=where,
            )
            return SavedCopyWrite(
                SavedCopyOutcome.DIVERTED,
                recorded_head=recorded,
                bundled_head=tree.head_sha,
                diverted_to=where,
            )
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
    """STAMP THE TREE, not just the bytes. `last_modified` is whole seconds on Azure, so a Save
    and a turn-boundary write inside one second are indistinguishable by time — and the restore
    path picks the newer of the two. Recording which tree each bundle holds is what lets a reader
    answer "same content?" and "which is newer?" without a download, and without a tie silently
    resolving to the older tree."""
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
    """`_store_it`, timed onto the shared accumulator. A separate wrapper rather than inlining
    at each call site: `write_recovery_copy` picks one of four keys to write to, and every arm
    must time the same way."""
    started = time.monotonic()
    await _store_it(store, key, tree)
    timings.store_ms = _elapsed_ms(started)


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
        # Cleanup runs on the FAILURE path too, which the success-only version did not: a bundle
        # left behind is multi-MB of binary sitting in the worktree that the next snapshot's
        # `git add -A` would commit into the user's tree. `/app.bundle*` in the template's
        # .gitignore is the backstop for a call killed before it reaches here; this is the fix.
        cleanup_started = time.monotonic()
        with suppress(SandboxError):
            await run_command(
                handle, ["rm", "-f", bundle_name], timeout_s=SNAPSHOT_EXEC_TIMEOUT_SECONDS
            )
        timings.cleanup_ms = _elapsed_ms(cleanup_started)


class ParkedTreeNotOursError(Exception):
    """The key named for promotion does not live under this app's quarantine or divert prefix.

    ITS OWN TYPE so the route can answer 400 rather than 500. An operator who pasted the wrong key
    has made an ordinary mistake and needs to be told so; a `StorageError` here would render as an
    internal fault and send them looking for a broken store."""

    def __init__(self, key: str) -> None:
        super().__init__(f"{key} does not belong to this app")
        self.key = key


@dataclass(frozen=True)
class ParkedTree:
    """One bundle the recovery guard set aside, as an operator needs to see it."""

    key: str
    kind: Literal["quarantine", "divert"]
    head_sha: str | None
    size_bytes: int
    taken_at: datetime | None


@dataclass(frozen=True)
class Promotion:
    promoted: bool
    detail: str


async def newest_diverted_at(app_id: uuid.UUID) -> datetime | None:
    """When a platform write-back for this app was last REFUSED, or `None` if none ever was.

    The citizen is owed this sentence. Shutdown now writes their work back with nobody watching,
    and when the ancestry guard refuses, the tree is parked and their app comes back from the last
    SAVED version instead — which looks, from the screen, exactly like a normal reopen. Saying so
    is what makes removing the exit prompts honest rather than merely quieter.

    ONE LIST AND ONE HEAD, unlike `list_parked_trees`, which heads every object it finds: this is
    on a poll, and it needs only the newest. The keys are stamped sortably for exactly this.

    `None` on an unconfigured or unreadable store. A notice we cannot substantiate is one we do
    not make — the opposite failure, claiming a refusal that did not happen, would send somebody
    looking for work that was never set aside."""
    try:
        store = get_storage()
    except StorageUnconfiguredError:
        return None
    try:
        keys = await all_keys_under(store, divert_prefix(app_id))
    except StorageError:
        return None
    if not keys:
        return None
    meta = await store.head(max(keys))
    return meta.last_modified if meta else None


async def list_parked_trees(app_id: uuid.UUID) -> list[ParkedTree]:
    """Every quarantine and divert object for one app, newest first — the useful one is almost
    always the last, and scrolling to the bottom is how an operator promotes the wrong one.

    Returns an empty list rather than raising on an unconfigured or unreadable store: this is a
    read for a human already dealing with an incident, and a 500 mid-incident is not help. The
    empty case reads honestly as "nothing parked" either way."""
    try:
        store = get_storage()
    except StorageUnconfiguredError:
        return []
    found: list[ParkedTree] = []
    for kind, prefix in (
        ("quarantine", quarantine_prefix(app_id)),
        ("divert", divert_prefix(app_id)),
    ):
        try:
            keys = await all_keys_under(store, prefix)
        except StorageError:
            _log.warning("parked_trees_unreadable", app_id=str(app_id), prefix=prefix)
            continue
        for key in keys:
            meta = await store.head(key)
            if meta is None:
                continue
            found.append(
                ParkedTree(
                    key=key,
                    kind=kind,  # type: ignore[arg-type]
                    head_sha=head_sha_from_metadata(meta.metadata),
                    size_bytes=meta.size,
                    taken_at=meta.last_modified,
                )
            )
    # Sorted on the STAMP INSIDE the key, not on the whole key and not on `last_modified`.
    #
    # Not the whole key, because the two prefixes differ before the stamp does: `divert/...` and
    # `quarantine/...` sort by their first letter, so a whole-key sort silently groups by KIND and
    # only orders within each group — which reads as chronological right up until an app has both,
    # which is exactly the incident an operator is looking at when they open this.
    #
    # Not `last_modified`, because Azure stamps it in whole seconds and would tie two objects
    # taken in the same one; the filename stamp is microseconds and is written by us.
    return sorted(found, key=lambda tree: tree.key.rsplit("/", 1)[-1], reverse=True)


async def promote_parked(app_id: uuid.UUID, *, key: str) -> Promotion:
    """Copy one parked tree into the recovery slot, THROUGH the guard — not a two-line blob copy.

    THE ANCESTRY QUESTION CANNOT BE ASKED HERE: the turn-end guard asks a live container `git
    merge-base --is-ancestor`, but this runs against two store objects with no container in sight.
    So the check is the one that IS answerable — refuse when the slot already holds the same tree,
    otherwise require an explicit promotion — which is weaker than the turn-end guard, stated
    rather than dressed up. The compensating control is that this route is superadmin-only,
    audited, and per-occurrence keys mean the replaced object is still there."""
    store = get_storage()
    if not key.startswith((quarantine_prefix(app_id), divert_prefix(app_id))):
        # THE KEY COMES FROM A REQUEST BODY. It names an object to READ and an app to write it
        # into, so without this an operator — or anything that reached this route — could promote
        # one app's tree into another app's recovery slot. The prefix check is the whole of the
        # scoping, and it is a `startswith` against two app-derived prefixes rather than a
        # substring test for exactly that reason.
        raise ParkedTreeNotOursError(key)
    data = await store.get(key)
    head_sha = parse_bundle_head_sha(data)
    async with _serialized_per_app(app_id):
        current = await store.head(recovery_key(app_id))
        if head_sha_from_metadata(current.metadata if current else None) == head_sha:
            return Promotion(False, "the recovery slot already holds this tree")
        await _store_it(store, recovery_key(app_id), _BundledTree(head_sha=head_sha, data=data))
    _log.warning("parked_tree_promoted", app_id=str(app_id), key=key, head_sha=head_sha)
    return Promotion(True, f"the recovery slot now holds {head_sha}")


class VersionBundleMissingError(Exception):
    """A rollback was asked for, and the version's stored bundle is not there.

    Distinct from having no saved version at all: the row says the platform kept this tree, so
    its absence is a fault worth naming rather than an empty history."""

    def __init__(self, app_id: uuid.UUID, key: str) -> None:
        super().__init__(f"app {app_id} has no stored bundle at {key}")
        self.app_id = app_id
        self.key = key


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

    Under the per-app lock, so a Save or an autosave waits: park the live tree in quarantine,
    write the saved bundle into the recovery slot, then reset the container. The store moves
    first: if the reset fails, the container still holds work descending from the saved head, and
    the next turn finds it intact. The slot is overwritten, never emptied — an empty slot reads as
    an app that was never built."""
    return await _restore_over_the_live_tree(
        sandbox_client,
        handle,
        app_id,
        source_key=snapshot_key(app_id),
        taken_at=taken_at,
        missing=lambda: NothingSavedToGoBackToError(app_id),
    )


async def restore_version(
    sandbox_client: SandboxClient,
    handle: SandboxHandle,
    app_id: uuid.UUID,
    *,
    blob_key: str,
    taken_at: datetime,
) -> SavedVersion:
    """Put a stored version back into the live container, keeping the tree it replaces.

    The same ordering a discard uses, over a different source key — which is the whole
    difference between the two operations at this level. Also written into the saved slot by the
    caller, because the restored content BECOMES what the app is saved at: leaving `snapshot_key`
    behind would read as unsaved work against a tree the citizen never edited.
    """
    return await _restore_over_the_live_tree(
        sandbox_client,
        handle,
        app_id,
        source_key=blob_key,
        taken_at=taken_at,
        missing=lambda: VersionBundleMissingError(app_id, blob_key),
    )


async def _restore_over_the_live_tree(
    sandbox_client: SandboxClient,
    handle: SandboxHandle,
    app_id: uuid.UUID,
    *,
    source_key: str,
    taken_at: datetime,
    missing: Callable[[], Exception],
) -> SavedVersion:
    """Park the live tree, arm the recovery slot, then reset the container. In that order.

    ★ THE RECOVERY SLOT IS WRITTEN BEFORE THE RESET, AND THAT IS NOT TIDINESS.
    `write_recovery_copy` diverts any tree whose HEAD is not a descendant of the sha stamped on
    `recovery_key`. Putting an older tree into the container without moving that stamp leaves
    every later turn writing to `divert_key` and firing `recovery_write_did_not_land` — the slot
    reads as poisoned from then on, and crash recovery is broken permanently rather than for one
    turn.

    THE STORE MOVES FIRST for the same reason a discard does it: if the reset fails, the
    container still holds work descending from a head the store knows, and the next turn finds
    it intact. The slot is overwritten, never emptied — an empty slot reads as an app that was
    never built.
    """
    store = _the_store_first()
    async with _serialized_per_app(app_id):
        meta = await store.head(source_key)
        if meta is None:
            raise missing()
        data = await store.get(source_key)
        saved = _BundledTree(head_sha=parse_bundle_head_sha(data), data=data)
        parked = Destination.quarantine(app_id, taken_at).key
        live = await _bundle_the_tree(sandbox_client, handle, _SaveStepTimings())
        await _store_it(store, parked, live)
        await _store_it(store, recovery_key(app_id), saved)
        await sandbox_client.reset_to_bundle(handle, saved.data)
    return SavedVersion(head_sha=saved.head_sha, saved_at=meta.last_modified, parked_at=parked)
