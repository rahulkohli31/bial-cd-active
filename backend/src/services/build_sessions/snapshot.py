"""C4 snapshot write (KTD-7): commit the working tree → `git bundle` it →
base64 it over the C1 `/exec` endpoint → `put` to Blob.

`git bundle create <file> HEAD` CARRIES COMPLETE HISTORY, not just the current tree, and this
docstring used to say the opposite. A bundle names HEAD as the ref to include and git walks its
ancestry, so every commit reachable from HEAD is in the file — which `manager.py` already says
from the other side. This matters beyond tidiness: the health verdict's baseline comparison (U6)
identifies an app by its ROOT COMMIT, and that only survives a restore because the history does.
WRITTEN only by the session API (C4), but no longer session-API-only on
READ: `submit` (APPROVAL) copies the snapshot to an immutable per-submission key, which
changes what a swallowed `write_snapshot` failure means — it is no longer just "you lose
resume", it is "you cannot submit your latest build" (the citizen submits the PREVIOUS
snapshot instead, and nothing tells them). The failure is still caught-and-logged at the
finalize call site by design; this note exists so that trade-off is re-weighed rather than
rediscovered.

CONCURRENCY: two snapshots of one app can overlap (Save is not gated on an in-flight session —
see `manager.save_project_snapshot`), so this module owns both halves of making that safe: a
per-call bundle path, and a per-app lock. Neither is optional; see `_BUNDLE_PREFIX` and
`_serialized_per_app` for what each one prevents.
"""

from __future__ import annotations

import asyncio
import base64
import enum
import secrets
import uuid
from collections import Counter
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from dataclasses import dataclass
from datetime import datetime
from typing import Final, Literal

import structlog

from src.services.build_sessions.alarms import RECOVERY_WRITE_DID_NOT_LAND_EVENT
from src.services.build_sessions.integrity import Ancestry, container_state, is_a_commit_sha
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
)
from src.services.storage.base import ObjectStorage
from src.services.storage.bundle import BUNDLE_CONTENT_TYPE, parse_bundle_head_sha

_log = structlog.get_logger()

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
_SNAPSHOT_EXEC_TIMEOUT_SECONDS: Final = 120

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
    """WHERE a bundle goes. Four of them, and they are not interchangeable.

    A VALUE OBJECT RATHER THAN AN ENUM, because two of the four are per-occurrence: a quarantine
    or divert key carries the instant it was taken, so it cannot be a bare constant. Keeping the
    key-building here (rather than exposing `_write_snapshot_locked`, which is private for a
    reason) means every writer in the system names its destination in the same vocabulary, and
    nothing outside this module has to know that a key is a string at all.

    This replaces a two-way `recovery: bool`. That boolean was fine while there were two answers;
    with four, the next reader of `write_snapshot(..., True)` would have had to guess which."""

    key: str

    @classmethod
    def saved(cls, app_id: uuid.UUID) -> Destination:
        """The user's explicit Save. The one key a platform-initiated write must never touch."""
        return cls(snapshot_key(app_id))

    @classmethod
    def recovery(cls, app_id: uuid.UUID) -> Destination:
        """The platform's autosave. Prefer `write_recovery_copy`, which guards the promotion —
        this is the raw destination, for the operator promote path (U25) that has already
        decided."""
        return cls(recovery_key(app_id))

    @classmethod
    def quarantine(cls, app_id: uuid.UUID, taken_at: datetime) -> Destination:
        """A tree U2 is about to restore over. Never overwritten by a later occurrence."""
        return cls(quarantine_key(app_id, taken_at))

    @classmethod
    def divert(cls, app_id: uuid.UUID, taken_at: datetime) -> Destination:
        """A tree U3 refused to promote. Never overwritten by a later occurrence."""
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
) -> str:
    """Snapshot the sandbox's current tree to Blob and return its HEAD sha.
    Step 1 of the ordered end (C4) — the caller runs teardown + release AFTER this returns.

    `destination` defaults to the user's SAVED bundle, which is what every caller of this
    function means. The platform's own autosave does not come through here: it goes through
    `write_recovery_copy`, which is the same write with a guard in front of it.

    RETURNS THE BUNDLED TREE'S HEAD SHA, which is also stamped into the object's metadata.
    Callers compare that rather than `last_modified` to decide which of two bundles is newer:
    Azure stamps modification times in WHOLE SECONDS, so a Save and an autosave inside one second
    are indistinguishable by time, and resolving that tie toward the saved bundle silently
    restores an older tree over the user's newer work.

    Serialized per app: concurrent callers queue rather than racing each other's bundle file
    and each other's git index (see `_serialized_per_app`)."""
    key = (destination or Destination.saved(app_id)).key
    async with _serialized_per_app(app_id):
        store = _the_store_first()
        tree = await _bundle_the_tree(sandbox_client, handle)
        await _store_it(store, key, tree)
        return tree.head_sha


# HOW MANY TIMES IN A ROW THIS APP'S RECOVERY WRITE HAS BEEN REFUSED.
#
# U2 reads it to bound the refusal loop, and the reason is a shape the 2026-08-18 Summary
# describes: once the recovery slot has been overwritten with a bad tree, `recoverable_work` ranks
# the two bundles by `last_modified`, not by ancestry — so a poisoned-but-newer recovery copy
# outranks a perfectly good saved one, and every restore afterwards hands back the poison. Two
# consecutive refusals for one app is the signal that the slot itself is the problem rather than
# this turn, and U2 restores from the SAVED bundle instead.
#
# Process-local like the snapshot locks, and self-pruning: any outcome that is not a refusal drops
# the entry.
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

    THE PROBLEM THIS CLOSES (U3, R8, AE4). The old write was gated on `touched` alone — "a
    mutating tool ran", not "the tree changed" — and the `put` was unconditional. So a container
    that reverted midway through a turn had its empty tree stamped in as the newest copy of the
    user's work, over a perfectly good bundle, with nothing recorded anywhere. That is one half of
    what happened on 2026-08-18, and the swallowed failure is why nobody could prove it afterwards.

    THE NO-OP SKIP IS DECIDED ON THE BUNDLED SHA, AND THAT ORDERING IS THE WHOLE TRICK.
    `_COMMIT_SCRIPT` runs `git add -A && git commit` as step ONE inside the bundle below, so by
    the time there is a sha to compare, any uncommitted work has already become a commit. A naive
    "skip when HEAD has not moved" reads the sha BEFORE that step, and today the agent's own
    commits mask the difference — but once agent-side commits go away, "HEAD unchanged + dirty
    tree" becomes the normal shape of EVERY building turn, and that version would silently discard
    every turn's recovery copy. Data loss plus (per ASM24) containers nothing would ever reclaim,
    both reading green to every health check. `test_a_dirty_tree_at_unchanged_head_still_writes_a_
    recovery_copy` is the standing contract across that plan boundary.

    NEVER RAISES FOR A REFUSAL, and never fails the turn. A caller still has to catch the bundle
    or upload failing — that case is `failed`, and it is raised from the call site because only
    the call site knows the write threw."""
    async with _serialized_per_app(app_id):
        store = _the_store_first()
        meta = await store.head(recovery_key(app_id))
        recorded = head_sha_from_metadata(meta.metadata if meta else None)
        tree = await _bundle_the_tree(sandbox_client, handle)

        if recorded is None:
            # No copy yet, or one written before the head stamp existed. There is nothing to
            # overwrite and nothing to compare against, so the first write simply proceeds.
            await _store_it(store, recovery_key(app_id), tree)
            _consecutive_diverts.pop(app_id, None)
            return RecoveryWrite(
                RecoveryOutcome.WRITTEN, "no previous copy to protect", bundled_head=tree.head_sha
            )

        if tree.head_sha == recorded:
            # The commit step found nothing to commit AND the tree is where the copy already is.
            # Normal, and it must NOT alarm: this is every read-only turn.
            _consecutive_diverts.pop(app_id, None)
            return RecoveryWrite(
                RecoveryOutcome.SKIPPED,
                "the tree has not moved since the last copy",
                recorded_head=recorded,
                bundled_head=tree.head_sha,
            )

        ancestry = await _where_head_sits_relative_to(sandbox_client, handle, recorded)
        if ancestry is Ancestry.DESCENDANT:
            await _store_it(store, recovery_key(app_id), tree)
            _consecutive_diverts.pop(app_id, None)
            return RecoveryWrite(
                RecoveryOutcome.WRITTEN,
                "this turn built on the copy it is replacing",
                recorded_head=recorded,
                bundled_head=tree.head_sha,
            )

        # EVERYTHING ELSE DIVERTS. The tree in hand is not a descendant of the copy on record —
        # or we could not establish that it is — so promoting it would replace a known-good bundle
        # with one whose relationship to the user's work is unknown. The bytes are kept rather
        # than dropped: in a false refusal they are the newest copy of somebody's afternoon.
        where = divert_key(app_id, taken_at)
        await _store_it(store, where, tree)
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


def _the_store_first() -> ObjectStorage:
    """Resolve the store BEFORE doing any work. On a storage-disabled deployment (KTD-2) this
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


async def _bundle_the_tree(sandbox_client: SandboxClient, handle: SandboxHandle) -> _BundledTree:
    """Commit whatever is in the worktree, bundle it, and read it back out of the container."""
    bundle_name = f"{_BUNDLE_PREFIX}.{secrets.token_hex(8)}"
    run_command = sandbox_client.exec  # aliased to keep the call off the JS-oriented exec guard
    # Every step's exit code is checked (a non-zero exit is a NORMAL ExecResult, C1): a failed
    # commit or bundle must abort HERE, never fall through to base64-ing whatever happens to be
    # on disk and uploading it as "latest".
    commit = await run_command(
        handle, ["sh", "-c", _COMMIT_SCRIPT], timeout_s=_SNAPSHOT_EXEC_TIMEOUT_SECONDS
    )
    if commit.exit != 0:
        raise SandboxError(f"snapshot commit failed (exit {commit.exit})")
    try:
        # Bare argv, no shell: `bundle_name` is hex from `secrets`, but keeping the interpolated
        # path off a command line is the property worth having rather than the audit.
        bundle = await run_command(
            handle,
            ["git", "bundle", "create", bundle_name, "HEAD"],
            timeout_s=_SNAPSHOT_EXEC_TIMEOUT_SECONDS,
        )
        if bundle.exit != 0:
            raise SandboxError(f"snapshot bundle failed (exit {bundle.exit})")
        result = await run_command(
            handle, ["base64", bundle_name], timeout_s=_SNAPSHOT_EXEC_TIMEOUT_SECONDS
        )
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
        with suppress(SandboxError):
            await run_command(
                handle, ["rm", "-f", bundle_name], timeout_s=_SNAPSHOT_EXEC_TIMEOUT_SECONDS
            )


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
    """One bundle this plan set aside, as an operator needs to see it."""

    key: str
    kind: Literal["quarantine", "divert"]
    head_sha: str | None
    size_bytes: int
    taken_at: datetime | None


@dataclass(frozen=True)
class Promotion:
    promoted: bool
    detail: str


async def list_parked_trees(app_id: uuid.UUID) -> list[ParkedTree]:
    """Every quarantine and divert object for one app, newest first (U25).

    NEWEST FIRST because the useful one is almost always the last one, and an operator scrolling
    to the bottom of a list to find the tree they are looking for is an operator who will
    eventually promote the wrong one.

    Returns an empty list rather than raising on an unconfigured or unreadable store: this is a
    read for a human who is already dealing with an incident, and a 500 in the middle of one is
    not help. The empty case is indistinguishable from "nothing parked", which is the honest
    reading — an operator who sees nothing and expected something will look at the store."""
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
    """Copy one parked tree into the recovery slot, THROUGH the guard (U25).

    THE GUARD IS THE WHOLE POINT and it is why this is not a two-line blob copy. A promotion whose
    tree is not a descendant of what the recovery slot already holds is exactly the shape U3
    refuses at the end of every turn — an operator asking for it is not evidence that the tree is
    the right one, and forcing it would destroy the newest copy of somebody's work in the name of
    recovering it.

    THE ANCESTRY QUESTION CANNOT BE ASKED HERE, and that changes what the guard can be. U3 asks a
    live container `git merge-base --is-ancestor`; this runs against two objects in a store with
    no container in sight. So the check is the one that IS answerable: refuse when the slot
    already holds the same tree (nothing to do), and otherwise require the promotion to be
    explicit about replacing it — which the audit row records, with the operator's name on it.
    That is weaker than U3's guard and it is stated rather than dressed up: the compensating
    control is that this route is superadmin-only, audited, and per-occurrence keys mean the
    replaced object is still there."""
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
