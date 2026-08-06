"""C4 snapshot write (KTD-7): commit the working tree → bundle the CURRENT tree only →
base64 it over the C1 `/exec` endpoint → `put` to Blob at `snapshot_key(app_id)`.

The bundle is the current tree (HEAD), NOT `git bundle --all` full history: the POC only
needs current code to survive teardown so the user can resume, and dropping history keeps
the base64-over-`/exec` payload small (workspaces are source-only — node_modules is baked
into the image). WRITTEN only by the session API (C4), but no longer session-API-only on
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
import secrets
import uuid
from collections import Counter
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, suppress
from typing import Final

import structlog

from src.services.sandbox import SandboxClient, SandboxError, SandboxHandle
from src.services.storage import get_storage, recovery_key, snapshot_key
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


async def write_snapshot(
    sandbox_client: SandboxClient,
    handle: SandboxHandle,
    app_id: uuid.UUID,
    *,
    recovery: bool = False,
) -> str:
    """Snapshot the sandbox's current tree to Blob (overwrite-latest) and return its HEAD sha.
    Step 1 of the ordered end (C4) — the caller runs teardown + release AFTER this returns.

    `recovery=True` writes to `recovery_key` instead: the platform's autosave, which must
    NEVER land on the saved bundle. See `recovery_key`'s docstring — that separation is what
    lets the platform stop losing work without taking the decision of what counts as a saved
    version away from the user (KTD-5e).

    RETURNS THE BUNDLED TREE'S HEAD SHA, which is also stamped into the object's metadata.
    Callers compare that rather than `last_modified` to decide which of the two bundles is
    newer: Azure stamps modification times in WHOLE SECONDS, so a Save and an autosave inside
    one second are indistinguishable by time, and resolving that tie toward the saved bundle
    silently restores an older tree over the user's newer work.

    Serialized per app: concurrent callers queue rather than racing each other's bundle file
    and each other's git index (see `_serialized_per_app`)."""
    key = recovery_key(app_id) if recovery else snapshot_key(app_id)
    async with _serialized_per_app(app_id):
        return await _write_snapshot_locked(sandbox_client, handle, key)


async def _write_snapshot_locked(
    sandbox_client: SandboxClient, handle: SandboxHandle, key: str
) -> str:
    # Resolve the store BEFORE doing any work. On a storage-disabled deployment (KTD-2) this
    # raises here, so the turn does not commit, bundle and base64 a whole tree over `/exec`
    # only to discover at the upload that there is nowhere to put it.
    store = get_storage()
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
        head_sha = parse_bundle_head_sha(data)
        # STAMP THE TREE, not just the bytes. `last_modified` is whole seconds on Azure, so a
        # Save and a turn-boundary write inside one second are indistinguishable by time — and
        # the restore path picks the newer of the two. Recording which tree each bundle holds
        # is what lets a reader answer "same content?" and "which is newer?" without a
        # download, and without a tie silently resolving to the older tree.
        await store.put(
            key, data, content_type=BUNDLE_CONTENT_TYPE, metadata={"head_sha": head_sha}
        )
        return head_sha
    finally:
        # Cleanup runs on the FAILURE path too, which the success-only version did not: a bundle
        # left behind is multi-MB of binary sitting in the worktree that the next snapshot's
        # `git add -A` would commit into the user's tree. `/app.bundle*` in the template's
        # .gitignore is the backstop for a call killed before it reaches here; this is the fix.
        with suppress(SandboxError):
            await run_command(
                handle, ["rm", "-f", bundle_name], timeout_s=_SNAPSHOT_EXEC_TIMEOUT_SECONDS
            )
