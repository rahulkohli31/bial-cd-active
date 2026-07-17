"""The in-process build-session lifecycle: `SessionManager` + `BuildSession`.

KTD-1 — the non-serializable core of a session (the `SandboxHandle` holding the raw
bearer, the progress `asyncio.Queue` subscribers, the in-process envelope buffer, the
background `run_build` task) lives in memory, NOT Postgres. On a single replica the
whole session is in-process; the frozen Redis keys (lock/heartbeat/registry) are the
durable cross-restart coordination.

KTD-2 — teardown + lock-release is SESSION-API-owned; BRAIN signals end by RETURNING a
`BuildResult`, never touching Redis and never emitting a terminal frame. `_finalize` runs
the authoritative end sequence exactly once (guarded by `terminal_committed`): snapshot →
teardown → holder release → clear registry → emit THE terminal `ended`.

That order is the whole point of R7: the `ended` is emitted at step 4, AFTER the step-1
snapshot, so its `snapshot_committed` is the real post-commit value. Every end path —
completed / quota / escalated / stop / force_end / idle-reap / a raised run_build —
converges on this one emission, so the feed carries exactly one terminal, always truthful.

KTD-9 — the brain + sandbox client are threaded IN from the router's `Depends`, never
resolved inline, so `app.dependency_overrides` reach them in tests.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

import structlog
from pydantic_ai import BinaryContent
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.v1.build_sessions.schemas import (
    BuildResult,
    BuildSessionStatus,
    EndedEvent,
    PreviewReadyEvent,
    ProgressEnvelope,
    RunBuild,
)
from src.db.base import async_session_factory
from src.db.models.user import User
from src.services.build_sessions.appdata import build_app_env, resolve_app_for_project
from src.services.build_sessions.appstorage import provision_app_storage
from src.services.build_sessions.attachments import resolve_build_attachments
from src.services.build_sessions.locks import (
    acquire_lock,
    delete_registry,
    mark_registry_ending,
    read_registry,
    release_lock_as_holder,
    renew_lock,
    write_heartbeat,
)
from src.services.build_sessions.outcome import (
    FORCE_ENDED,
    STOPPED_BY_USER,
    transcript_head_seq,
    write_build_outcome,
)
from src.services.build_sessions.reaper import reconcile_user
from src.services.build_sessions.snapshot import write_snapshot
from src.services.redis import get_redis
from src.services.sandbox import (
    SandboxClient,
    SandboxError,
    SandboxGoneError,
    SandboxHandle,
)
from src.services.storage import (
    StorageError,
    StorageNotFoundError,
    StorageUnconfiguredError,
    get_storage,
    snapshot_key,
)

_log = structlog.get_logger()

# `build_failed` is the only reason that maps to the terminal FAILED status; every other
# end reason (stopped_by_user / idle_teardown / quota_exceeded / completed) is graceful.
_BUILD_FAILED: str = "build_failed"

# R6 — bounded retry for the restore path's two fallible steps. Budgets differ because the
# steps cost wildly different amounts: `head` is a single cheap metadata call, so retrying it
# is nearly free (worst case ~0.75s of added start latency); `restore_from_snapshot` re-runs a
# whole container provision + `npm install`, so it gets ONE retry — enough to ride out a
# registry blip, bounded enough that a doomed start still fails in ONE-digit minutes rather
# than looping. Be honest about that bound: each restore attempt can block up to the sandbox
# layer's `_RESTORE_TIMEOUT_SECONDS` (600s, `services/sandbox/client.py`) when the npm reconcile
# hangs to its own timeout, so the true worst case here is ~2 × 600s + backoff ≈ 20 minutes, not
# "tens of seconds". That is a deliberately generous ceiling on the RARE hung-install case (the
# common failure — a `set -e` npm error — raises in seconds); an outer start deadline would be a
# design change, not a comment fix.
# Both exhaust into `SnapshotUnavailableError`; neither may fall back to fresh.
_HEAD_ATTEMPTS: int = 3
_HEAD_BACKOFF_SECONDS: float = 0.25
_RESTORE_ATTEMPTS: int = 2
_RESTORE_BACKOFF_SECONDS: float = 1.0


async def _asleep(seconds: float) -> None:
    """Backoff sleep behind one indirection so tests can record the schedule without real
    waits (mirrors `sandbox/client.py::_asleep`)."""
    await asyncio.sleep(seconds)


def _terminal_status(reason: str) -> Literal[BuildSessionStatus.ENDED, BuildSessionStatus.FAILED]:
    """The terminal status for a SESSION-API-originated end reason (stop / force_end /
    idle-reap / a raised run_build). Only sound because those reasons are a closed, graceful
    set plus `build_failed` — BRAIN's reasons are NOT derivable this way (`escalated` is FAILED
    yet != `_BUILD_FAILED`), which is why its verdict carries an explicit `status`."""
    return BuildSessionStatus.FAILED if reason == _BUILD_FAILED else BuildSessionStatus.ENDED


# How long an ended session (with its envelope replay buffer) stays resident after its
# terminal commit: long enough that a late SSE reconnect still replays + [DONE], short
# enough that `_sessions` never grows unbounded. Evicted opportunistically at the top of
# start() and on the internal reap sweep — no background task.
_ENDED_RETENTION_SECONDS: float = 300.0

# How long a start will wait for an ended-but-still-finalizing session's shielded end
# sequence before keeping the 409 — a refine sent right after natural completion must not
# bounce off its own finished build (the finalize is usually sub-second; the bound only
# guards a wedged teardown).
_FINALIZE_GRACE_SECONDS: float = 30.0

# How long the end sequence will wait for the outcome record before giving up and emitting the
# terminal anyway (003-U5). The write is a handful of indexed queries against a live connection —
# seconds is already generous, and the terminal frame is worth more than the record: without it
# every SSE feed hangs and the session is never evicted.
_OUTCOME_WRITE_TIMEOUT_SECONDS: float = 10.0


# The end sequence's own DB session factory (it outlives the starting request). Typed as what this
# module actually DOES with it — call it, `async with` the result — rather than as
# `async_sessionmaker`, so a test can bind it to the rolled-back session with a plain
# context-manager factory (the real `async_sessionmaker` satisfies this by construction).
SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]


class BuildSessionConflictError(Exception):
    """The user already holds a live build session (the one-per-user lock is held).
    Carries the existing `session_id` so the router can surface it in the 409."""

    def __init__(self, session_id: uuid.UUID | None) -> None:
        super().__init__("a build session is already active")
        self.session_id = session_id


class SnapshotUnavailableError(Exception):
    """R6 — the restore path could not be completed and the snapshot is NOT confirmed
    absent: either the head-check never got an answer (transient `StorageError` on every
    attempt) or the bundle is known-present but its restore kept failing.

    This is the fail-closed half of the three-state head-check (`.claude/rules/fail-first.md`:
    ambiguity denies). The tempting "recovery" — provision a fresh template and let the user
    work — is the exact outcome R6 forbids, because it is not a degraded start but a
    DESTRUCTIVE one: `_do_finalize`'s step-1 snapshot would write the blank workspace OVER
    the user's good bundle, permanently. Aborting leaves the bundle byte-for-byte intact for
    the next start. Raised from `_resolve_sandbox`, so it lands inside `_start_locked`'s
    compensation block (lock released, any container torn down); the router maps it to a 503.
    """

    def __init__(self, message: str, *, app_id: uuid.UUID) -> None:
        super().__init__(message)
        self.app_id = app_id


def app_name_for(app_id: uuid.UUID) -> str:
    """An ACA-compliant container name (2–32 chars, lowercase alphanumeric/hyphen,
    letter-first, ends alphanumeric), stable per app: `sbx-` + 28 hex chars of the
    app_id (`str(app_id)` and the default `AppRegistry.name` are BOTH invalid ACA names)."""
    return f"sbx-{app_id.hex[:28]}"


@dataclass
class BuildSession:
    """One in-flight build (KTD-1). Held only in memory — never persisted."""

    session_id: uuid.UUID
    user_id: uuid.UUID
    project_id: uuid.UUID
    app_id: uuid.UUID
    prompt: str
    lock_token: str
    handle: SandboxHandle
    # The thread this build belongs to, carried past `start` so `_do_finalize` can record the
    # outcome in it (003-U5). None when the start named no conversation — an API-only caller,
    # which has no transcript to write to.
    conversation_id: uuid.UUID | None = None
    # The thread's high-water seq the moment this build STARTED — recorded on the outcome part as
    # `startedSeq` so the NEXT build can tell the turns that arrived while this one ran from the
    # ones it already consumed (R3). None when the start named no conversation. Captured at start
    # because it is unrecoverable later: at the terminal, a turn sent mid-build looks exactly like
    # one sent before it.
    started_seq: int | None = None
    # R3 — the conversation's attachments, already materialized (blob bytes rehydrated, office
    # text fenced) at start. Empty when the start carried no `conversationId` or the thread has
    # no attachments since its last build outcome. Resolved BEFORE the lock (see `start`), so by
    # the time a session exists these are pure in-memory content; `_live_session_spec` appends
    # them to the prompt when BRAIN resolves its run context.
    attachments: list[str | BinaryContent] = field(default_factory=list)
    status: BuildSessionStatus = BuildSessionStatus.PROVISIONING
    last_seq: int = 0
    preview_url: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    task: asyncio.Task[None] | None = None
    # The replay buffer (every emitted envelope) + one queue per live SSE connection.
    envelopes: list[ProgressEnvelope] = field(default_factory=list)
    subscribers: set[asyncio.Queue[ProgressEnvelope]] = field(default_factory=set)
    end_reason: str | None = None
    force_ended: bool = False
    terminal_committed: bool = False
    terminal_emitted: bool = False
    snapshot_committed: bool = False
    # The single shielded end-sequence task (created by the first _finalize caller); every
    # caller awaits it, so a caller's own cancellation can't tear the sequence in half.
    finalize_task: asyncio.Task[None] | None = None
    # Stamped when the end sequence completes — starts the retention window after which the
    # session (and its envelope buffer) is evicted from the manager.
    ended_at: datetime | None = None


class SessionManager:
    """The in-process session registry + lifecycle. Held as a module singleton with an
    accessor (mirrors `get_redis`), overridable in tests."""

    def __init__(self, session_factory: SessionFactory | None = None) -> None:
        # The end sequence outlives the request that started the build, so it cannot borrow the
        # request's session — it opens its OWN, exactly as the chat relay's disconnect-safe
        # billing drain does. Injectable so tests bind it to their rolled-back session instead of
        # committing to the real database.
        self._session_factory: SessionFactory = session_factory or async_session_factory
        self._sessions: dict[uuid.UUID, BuildSession] = {}
        self._active_by_user: dict[uuid.UUID, uuid.UUID] = {}
        # Strong refs to background run_build tasks so a client disconnect (which cancels
        # only the SSE generator) can't let the loop GC a still-running build.
        self._tasks: set[asyncio.Task[None]] = set()
        # One serialization lock per user, held across the WHOLE of start() — closes the
        # window where a concurrent same-user start would reconcile-away the first start's
        # in-flight lock (held but registry not yet written) and double-allocate a sandbox.
        self._start_locks: dict[uuid.UUID, asyncio.Lock] = {}

    def _start_lock_for(self, user_id: uuid.UUID) -> asyncio.Lock:
        lock = self._start_locks.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            self._start_locks[user_id] = lock
        return lock

    def _maybe_prune_start_lock(self, user_id: uuid.UUID) -> None:
        """Evict the per-user start lock once no live session remains — bounding the
        otherwise-unbounded `_start_locks` growth. Skipped when a concurrent start currently
        HOLDS the lock: that start owns the exact `Lock` object, so dropping it would let the
        next start build a fresh one and shatter mutual exclusion. (The safe slice of #2 —
        the `_sessions`/envelope retention window is a separate design decision.)"""
        if user_id in self._active_by_user:
            return
        lock = self._start_locks.get(user_id)
        if lock is not None and not lock.locked():
            self._start_locks.pop(user_id, None)

    def evict_ended_sessions(self, *, now: datetime | None = None) -> int:
        """Drop every session whose `ended_at` is past the retention window, with its
        envelope buffer and any consistent `_active_by_user`/`_start_locks` entries.
        Called opportunistically (start + the internal reap sweep) — a session inside the
        window is KEPT so a late SSE reconnect can still replay + `[DONE]`."""
        now = now or datetime.now(UTC)
        evicted = 0
        for session_id, session in list(self._sessions.items()):
            if session.ended_at is None:
                continue
            if (now - session.ended_at).total_seconds() < _ENDED_RETENTION_SECONDS:
                continue
            self._sessions.pop(session_id, None)
            if self._active_by_user.get(session.user_id) == session_id:
                self._active_by_user.pop(session.user_id, None)
            self._maybe_prune_start_lock(session.user_id)
            evicted += 1
        return evicted

    # --- lookups (router owns the user-scoping 404) --------------------------

    def get(self, session_id: uuid.UUID) -> BuildSession | None:
        return self._sessions.get(session_id)

    def active_session_for(self, user_id: uuid.UUID) -> BuildSession | None:
        session_id = self._active_by_user.get(user_id)
        return self._sessions.get(session_id) if session_id is not None else None

    def live_user_ids(self) -> set[uuid.UUID]:
        """Users with a live in-proc session — never reaped by a sweep (KTD-3)."""
        return set(self._active_by_user)

    # --- start ---------------------------------------------------------------

    async def start(
        self,
        db: AsyncSession,
        user: User,
        project_id: uuid.UUID,
        prompt: str,
        *,
        conversation_id: uuid.UUID | None = None,
        run_build: RunBuild,
        sandbox_client: SandboxClient,
    ) -> BuildSession:
        # Opportunistic retention sweep — the only guaranteed-recurring seam (no background
        # task), so ended sessions never accumulate unboundedly.
        self.evict_ended_sessions()
        # R3 — materialize the conversation's attachments FIRST: before the per-user start lock,
        # before the Redis lock, before any container. A `ConversationNotFoundError` (404) or a
        # `BuildAttachmentError` (422) therefore aborts a start that has allocated NOTHING — no
        # lock to release, no sandbox to tear down, no quota burnt. That ordering is the whole
        # point of the fail-first rule here: a build that silently ignores the user's spreadsheet
        # is the bug R3 deletes, and the only honest alternative is refusing to start at all.
        # (Outside the start lock deliberately: blob rehydration is I/O, and it needs no
        # mutual exclusion — it reads the caller's own committed rows.)
        attachments: list[str | BinaryContent] = []
        started_seq: int | None = None
        if conversation_id is not None:
            attachments = await resolve_build_attachments(db, user.id, project_id, conversation_id)
            # The START marker, read in the same breath as the attachments this build consumes —
            # so the two can never disagree about which turns this build saw. `resolve_build_
            # attachments` has just proven the thread is the caller's, which is what earns this
            # unscoped read of its seq space.
            started_seq = await transcript_head_seq(db, conversation_id)
        # Serialize concurrent same-user starts: the whole start (reconcile → acquire →
        # provision → register) runs under one per-user lock, so a second start can't
        # reconcile-away the first start's in-flight lock (held but registry-not-yet-written)
        # and double-allocate a sandbox (the critical reap_lock/fresh-acquire race).
        async with self._start_lock_for(user.id):
            return await self._start_locked(
                db,
                user,
                project_id,
                prompt,
                attachments=attachments,
                conversation_id=conversation_id,
                started_seq=started_seq,
                run_build=run_build,
                sandbox_client=sandbox_client,
            )

    async def _start_locked(
        self,
        db: AsyncSession,
        user: User,
        project_id: uuid.UUID,
        prompt: str,
        *,
        attachments: list[str | BinaryContent],
        conversation_id: uuid.UUID | None,
        started_seq: int | None,
        run_build: RunBuild,
        sandbox_client: SandboxClient,
    ) -> BuildSession:
        redis = get_redis()
        user_id = user.id
        # A live in-process session is the AUTHORITATIVE double-session guard: a second
        # run_build loop must never launch even if the Redis lock lapsed under the first
        # (a lapsed lock must not be the ONLY guard). Fail closed BEFORE reconcile/acquire.
        if user_id in self._active_by_user:
            blocking_id = self._active_by_user.get(user_id)
            blocking = self._sessions.get(blocking_id) if blocking_id is not None else None
            finalize = blocking.finalize_task if blocking is not None else None
            if blocking is None or not blocking.terminal_committed or finalize is None:
                raise BuildSessionConflictError(blocking_id)
            # The blocking session has already COMMITTED its terminal — it is ended but
            # still finalizing (a refine sent right on the heels of natural completion).
            # Wait (bounded) for the shielded end sequence instead of 409ing the user's own
            # finished build, then fall through to a fresh start; on a timeout or a finalize
            # error, keep the 409.
            try:
                await asyncio.wait_for(asyncio.shield(finalize), timeout=_FINALIZE_GRACE_SECONDS)
            except Exception:
                raise BuildSessionConflictError(blocking_id) from None
        # Not live: reconcile the user's OWN stale state before acquiring (KTD-3) — closes
        # the crashed-tab lockout at the exact moment it matters.
        await reconcile_user(redis, user_id, sandbox_client, has_live_session=False)

        token = await acquire_lock(redis, user_id)
        if token is None:
            existing = self._active_by_user.get(user_id)
            raise BuildSessionConflictError(existing)

        # Post-acquire steps are compensated: any failure holder-releases the lock (we
        # still own the token) and tears down any container that was created.
        handle: SandboxHandle | None = None
        try:
            app_id, app_key = await resolve_app_for_project(db, user_id, project_id)
            await db.commit()
            env = build_app_env(app_id, app_key)
            handle = await self._resolve_sandbox(sandbox_client, user_id, app_id, env)
        except Exception:
            await release_lock_as_holder(redis, user_id, token)
            if handle is not None:
                with suppress(SandboxError):
                    await sandbox_client.teardown(handle)
            raise

        session = BuildSession(
            session_id=uuid.uuid7(),
            user_id=user_id,
            project_id=project_id,
            app_id=app_id,
            prompt=prompt,
            lock_token=token,
            handle=handle,
            attachments=attachments,
            conversation_id=conversation_id,
            started_seq=started_seq,
        )
        self._sessions[session.session_id] = session
        self._active_by_user[user_id] = session.session_id
        # Seed the heartbeat so an immediate reconcile doesn't reap the fresh session.
        await write_heartbeat(redis, user_id)

        task = asyncio.create_task(self._run_and_finalize(session, run_build, sandbox_client))
        session.task = task
        self._tasks.add(task)

        def _on_done(finished: asyncio.Task[None]) -> None:
            # Drop the strong ref, then surface any exception that escaped
            # `_run_and_finalize` — a clean run or a stop/force-end cancellation is silent,
            # but a real bug must never die invisibly in a detached background task.
            self._tasks.discard(finished)
            if finished.cancelled():
                return
            exc = finished.exception()
            if exc is not None and not isinstance(exc, asyncio.CancelledError):
                _log.error(
                    "build task exited with an unhandled exception",
                    exc_info=exc,
                    session_id=str(session.session_id),
                    user_id=str(session.user_id),
                )

        task.add_done_callback(_on_done)
        return session

    async def _resolve_sandbox(
        self,
        sandbox_client: SandboxClient,
        user_id: uuid.UUID,
        app_id: uuid.UUID,
        env: dict[str, str],
    ) -> SandboxHandle:
        """The one-per-user rehydrate resolution: live registry → attach; otherwise (no
        registry — which a CLEAN end always leaves behind, since finalize deletes it — or
        registry-but-gone) restore the C4 snapshot when one exists, else provision fresh.
        Without the no-registry restore arm every graceful stop→start loop would discard
        the user's work onto a blank template."""
        redis = get_redis()
        app_name = app_name_for(app_id)
        if await read_registry(redis, user_id) is None:
            return await self._restore_or_provision(sandbox_client, user_id, app_name, app_id, env)
        try:
            return await sandbox_client.attach_existing(str(user_id))
        except SandboxGoneError:
            return await self._restore_or_provision(sandbox_client, user_id, app_name, app_id, env)

    async def _restore_or_provision(
        self,
        sandbox_client: SandboxClient,
        user_id: uuid.UUID,
        app_name: str,
        app_id: uuid.UUID,
        env: dict[str, str],
    ) -> SandboxHandle:
        """Restore the C4 snapshot when one exists; provision a fresh template ONLY when the
        bundle is CONFIRMED absent.

        R6 — fresh-provision has exactly ONE reachable arm: `StorageNotFoundError`, i.e. the
        store positively answered "no bundle" (a genuinely new app, or one that vanished
        between the head-check and the pull). No error path reaches it. An unknown head state
        or a restore that keeps failing raises `SnapshotUnavailableError` and aborts the
        start, because a fresh template here would be silently overwritten onto the user's
        saved work by finalize's step-1 snapshot."""
        # Ensure the app's Blob container + mint a fresh session SAS ONLY on this birth
        # (provision/restore) arm — never on attach, which reuses the live container's SAS (KTD-3).
        # A configured-store failure propagates: it fails the start before any sandbox handle
        # exists (start's compensation releases the lock; nothing to tear down), and the idempotent
        # container is simply reused on the next start. Disabled storage (dev/test) yields {} — a
        # no-op merge (KTD-2). C9 §6.
        env = {**env, **await provision_app_storage(app_id)}
        if await self._snapshot_exists_or_bust(app_id):
            try:
                return await self._restore_or_bust(sandbox_client, user_id, app_name, app_id, env)
            except StorageNotFoundError:
                # The ONLY error that may reach provision_new: the store positively answered
                # "no bundle" on the pull, so there is no work to overwrite.
                _log.warning(
                    "snapshot disappeared between head-check and restore; provisioning fresh",
                    app_id=str(app_id),
                )
        return await sandbox_client.provision_new(str(user_id), app_name, app_env=env)

    async def _restore_or_bust(
        self,
        sandbox_client: SandboxClient,
        user_id: uuid.UUID,
        app_name: str,
        app_id: uuid.UUID,
        env: dict[str, str],
    ) -> SandboxHandle:
        """Pull the known-present snapshot into a fresh container, with bounded retry.

        `npm install` lives INSIDE the `set -e` restore script, so a transient npm/registry
        blip surfaces here as a `SandboxError`. This used to fall back to `provision_new` to
        keep the start "recoverable" — the worry being that propagating would strand the
        session, since every later start re-runs the same failing restore. That worry was
        real but the cure was worse than the disease: the bundle EXISTS on this arm, so the
        fallback handed the user a blank template and finalize then snapshotted it over their
        good bundle — silent, permanent data loss (R6).

        The retry is what answers the stranding worry for the case that actually motivated it
        (a TRANSIENT blip): attempt two usually succeeds and the start proceeds normally. A
        PERSISTENT failure — a genuinely corrupt bundle, a lasting registry outage — does
        strand the session, deliberately: a 503 telling the user to retry or contact the
        admin, with their work intact and recoverable, beats a start that silently destroys
        it. "Contact the admin" IS the unstick path; automatic snapshot-quarantine remains
        the noted follow-up.

        `restore_from_snapshot` self-cleans on every exception (teardown + registry delete +
        token evict, C4), so each attempt starts from no container and an exhausted retry
        leaves nothing running — start's compensation only has the lock to release.

        The retry covers BOTH fallible halves of the attempt, because the pull is one too:
        `restore_from_snapshot` opens with `get_storage().get(...)`, so a `StorageAuthError` or
        a transient blob blip is exactly as retryable as an npm blip — and if it escaped
        uncaught it would be a bare 500 with zero retries, which is neither the docstring's
        promise nor a fair answer to the user. `StorageNotFoundError` is the ONE storage
        outcome that must not retry: it is the caller's legitimate fresh-provision arm.
        """
        attempt = 0
        while True:
            attempt += 1
            try:
                return await sandbox_client.restore_from_snapshot(
                    str(user_id), app_name, app_env=env
                )
            except StorageNotFoundError:
                # Discriminated by TYPE, and this clause MUST stay first: `StorageNotFoundError`
                # IS a `StorageError`, so the retry arm below would otherwise swallow a
                # confirmed-absent bundle and 503 a start that should simply provision fresh.
                raise
            except (SandboxError, StorageError) as exc:
                if attempt >= _RESTORE_ATTEMPTS:
                    _log.exception(
                        "snapshot restore failed on every attempt; failing the start closed "
                        "(saved version left intact — never provisioning over it)",
                        app_id=str(app_id),
                        attempts=attempt,
                    )
                    raise SnapshotUnavailableError(
                        "snapshot restore failed after retries", app_id=app_id
                    ) from exc
                _log.warning(
                    "snapshot restore failed; retrying",
                    app_id=str(app_id),
                    attempt=attempt,
                    exc_info=True,
                )
                await _asleep(_RESTORE_BACKOFF_SECONDS)

    async def _snapshot_exists_or_bust(self, app_id: uuid.UUID) -> bool:
        """The three-state head-check, expressed the fail-closed way: `True` = bundle
        present, `False` = CONFIRMED absent, raise = state unknown.

        `head()` has always given all three signals (meta / `None` / raise) — only this
        caller was lossy, collapsing a transient `StorageError` into `False`. That single
        wrong answer is the most expensive one available: "absent" provisions a blank
        template, which finalize then snapshots over the user's real work. So a blip is
        retried, and an unanswered head-check aborts the start instead of guessing (R6,
        plan `docs/plans/2026-07-16-002-feat-pilot-closure-plan.md` §U6). Mirrors submit's
        own fail-closed read (`api/v1/apps/router.py`, D9): absent and transient are
        different answers and must never be folded together.

        The store is resolved ONCE, outside the loop: no-store-configured is a permanent
        config fact, so retrying it three times only delays the same answer.
        """
        try:
            store = get_storage()
        except StorageUnconfiguredError:
            # NOT a transient failure — the supported storage-off deployment (`src.config`
            # gates the requirement on `is_production`; `provision_app_storage` returns {}
            # here for the same reason). With no store there can be no bundle, so a fresh
            # provision is provably non-destructive: this is a CONFIRMED absent, the exact
            # distinction R6 cares about. Folding it into the fail-closed arm instead would
            # 503 EVERY build start on such a deployment.
            return False
        attempt = 0
        while True:
            attempt += 1
            try:
                return await store.head(snapshot_key(app_id)) is not None
            except StorageError as exc:
                if attempt >= _HEAD_ATTEMPTS:
                    _log.exception(
                        "snapshot head-check failed on every attempt; failing the start closed "
                        "rather than provisioning over restorable work",
                        app_id=str(app_id),
                        attempts=attempt,
                    )
                    raise SnapshotUnavailableError(
                        "snapshot state unknown after retries", app_id=app_id
                    ) from exc
                _log.warning(
                    "snapshot head-check failed; retrying",
                    app_id=str(app_id),
                    attempt=attempt,
                    exc_info=True,
                )
                await _asleep(_HEAD_BACKOFF_SECONDS * 2 ** (attempt - 1))

    # --- progress channel ----------------------------------------------------

    async def on_progress(self, session: BuildSession, env: ProgressEnvelope) -> None:
        """The C7 `ProgressSink`: buffer the envelope, derive status, refresh liveness,
        and fan out to every live SSE subscriber (no Redis — this IS the transport)."""
        session.envelopes.append(env)
        session.last_seq = env.seq
        session.updated_at = datetime.now(UTC)
        if isinstance(env, PreviewReadyEvent):
            session.status = BuildSessionStatus.READY
            session.preview_url = env.preview_url
        elif isinstance(env, EndedEvent):
            session.status = env.status
            if env.preview_url is not None:
                session.preview_url = env.preview_url
            session.terminal_emitted = True
            # In production this fold is an identity — `_do_finalize` builds the frame FROM
            # `session.snapshot_committed`. It stays because `on_progress` is the generic C7
            # sink: it must derive correct state from any envelope handed to it, including the
            # ones tests push directly, without reaching back into who emitted them.
            session.snapshot_committed = session.snapshot_committed or env.snapshot_committed
        elif session.status == BuildSessionStatus.PROVISIONING:
            session.status = BuildSessionStatus.BUILDING  # first sign of the loop running

        # Build activity = liveness: renew the lock + heartbeat SERVER-side so an active
        # build whose tab is closed keeps its lock and is never reaped as idle. Skipped for
        # a terminal frame (the lock is about to be released) and best-effort (a redis blip
        # must not break the relay).
        if not isinstance(env, EndedEvent) and session.lock_token:
            try:
                redis = get_redis()
                if not await renew_lock(redis, session.user_id, session.lock_token):
                    # The lock lapsed under an active build (reaped / expired) — the reaper
                    # may now double-allocate. Best-effort still, but no longer invisible.
                    _log.warning(
                        "build session lock lost during an active build",
                        session_id=str(session.session_id),
                        user_id=str(session.user_id),
                    )
                await write_heartbeat(redis, session.user_id)
            except Exception:
                # A Redis blip must not break the progress relay, but — like the other
                # best-effort Redis paths in this file — it is logged, never swallowed.
                _log.exception(
                    "liveness renew/heartbeat failed during build",
                    session_id=str(session.session_id),
                )

        # Fan out with per-subscriber failure isolation — a slow/dead subscriber is dropped,
        # never allowed to raise QueueFull back into run_build.
        for queue in list(session.subscribers):
            try:
                queue.put_nowait(env)
            except asyncio.QueueFull:
                session.subscribers.discard(queue)

    # --- completion + the single-owner end sequence --------------------------

    async def _run_and_finalize(
        self, session: BuildSession, run_build: RunBuild, sandbox_client: SandboxClient
    ) -> None:
        """Await the opaque BRAIN task and finalize on a NORMAL or FAILED completion. A
        CANCELLATION is re-raised WITHOUT finalizing here — `stop`/`force_end` own the end
        sequence in that case, running it OUTSIDE the cancelled task so `_finalize`'s awaits
        actually complete (a cancelled task's `finally` awaits would themselves be cancelled,
        leaving the session half-finalized). `_finalize`'s `terminal_committed` guard keeps
        it single-owner across the two paths (KTD-2).

        The `BuildResult` is BRAIN's whole verdict and is threaded into the end sequence: it
        is the ONLY carrier of the outcome (BRAIN emits no terminal frame), so dropping it
        would cost the real `reason`/`status`/`preview_url` of every completed build (R7)."""

        async def sink(env: ProgressEnvelope) -> None:
            await self.on_progress(session, env)

        try:
            result = await run_build(session.session_id, session.user_id, sandbox_client, sink)
        except asyncio.CancelledError:
            raise  # stop/force_end finalizes; just unwind
        except Exception:
            # BRAIN broke its own never-raise invariant (KD-12) — no verdict exists, so the end
            # sequence derives a `build_failed` terminal itself rather than stranding the feed.
            _log.exception("run_build raised", session_id=str(session.session_id))
            await self._finalize(session, _BUILD_FAILED, sandbox_client)
            return
        await self._finalize(session, result.reason, sandbox_client, result=result)

    async def _finalize(
        self,
        session: BuildSession,
        reason: str | None,
        sandbox_client: SandboxClient,
        *,
        result: BuildResult | None = None,
    ) -> None:
        """Single-owner dispatcher: the FIRST caller (synchronously, no await between the
        check and the create) spawns ONE shielded end-sequence task; every caller then
        awaits it under `asyncio.shield`, so a caller's own cancellation (a racing stop
        cancelling the run_build task mid-finalize) can NEVER tear the sequence in half —
        `_do_finalize` runs to completion in its own task regardless."""
        if session.finalize_task is None:
            session.terminal_committed = True
            session.finalize_task = asyncio.ensure_future(
                self._do_finalize(session, reason, sandbox_client, result=result)
            )
        await asyncio.shield(session.finalize_task)

    async def _record_outcome(
        self,
        session: BuildSession,
        *,
        status: BuildSessionStatus,
        preview_url: str | None,
        reason: str | None,
    ) -> None:
        """Write the build-outcome message to the session's thread (003-U5).

        The SERVER records this, not the portal, because the portal is not reliably there: builds
        take minutes and users close tabs, and an in-memory session is evicted 5 minutes after its
        terminal — so a portal-only record would miss exactly the users the record serves. See
        `outcome.py` for the full rationale.

        Best-effort and never raising: this runs inside the end sequence, where a raise would skip
        the terminal frame and hang every SSE feed. A build with no thread (an API-only start with
        no `conversationId`) has nowhere to record and is a no-op, not an error.

        TIME-BOUNDED for the same reason a raise is caught. This is the only step in the end
        sequence that opens a DB session, and a wedged connection (no statement_timeout, a network
        partition) would block here forever — the terminal would never be emitted, every SSE feed
        would hang without `[DONE]`, and `ended_at` would never be stamped, so the session would
        never be evicted. The record is worth waiting seconds for, never the terminal.
        """
        if session.conversation_id is None:
            return
        try:
            async with asyncio.timeout(_OUTCOME_WRITE_TIMEOUT_SECONDS):
                async with self._session_factory() as db:
                    await write_build_outcome(
                        db,
                        user_id=session.user_id,
                        conversation_id=session.conversation_id,
                        session_id=session.session_id,
                        status=status,
                        preview_url=preview_url,
                        snapshot_committed=session.snapshot_committed,
                        reason=reason,
                        started_seq=session.started_seq,
                    )
        except (Exception, TimeoutError):  # fmt: skip  # ruff py314 strips parens
            _log.exception("build outcome write failed", session_id=str(session.session_id))

    async def _do_finalize(
        self,
        session: BuildSession,
        reason: str | None,
        sandbox_client: SandboxClient,
        *,
        result: BuildResult | None = None,
    ) -> None:
        """The authoritative end sequence, run exactly once. Every step is best-effort:
        a Redis blip on release/delete must NOT abort the sequence (which would leave the
        session half-finalized with the SSE feed hung) — it is logged and the sequence
        continues to the terminal synthesis (C4/C5 ordering: snapshot → teardown → release
        → clear registry → synthesize)."""
        redis = get_redis()
        reason = reason or session.end_reason or "completed"

        # 1. Snapshot — only with live progress to persist; skipped for force_end / already done.
        if (
            session.handle is not None
            and not session.force_ended
            and not session.snapshot_committed
        ):
            try:
                await write_snapshot(sandbox_client, session.handle, session.app_id)
                session.snapshot_committed = True
            except Exception:
                _log.exception("snapshot failed in finalize", session_id=str(session.session_id))

        # 2. Teardown → 3. holder release (LAST) → clear registry. Release + registry-delete
        #    run ONLY on a CLEAN teardown: a teardown SandboxError means the container may
        #    still be live, so KEEP the Redis lock + registry (mirroring reaper.reap_user's
        #    keep-state-on-failure) for the next reaper sweep to retry — clearing them now
        #    would orphan a container the reaper's registry-only scan can never see again.
        #    `_active_by_user` is popped regardless (guaranteed-run finally) so the SSE feed
        #    always closes even on a kept-state teardown failure.
        try:
            torn_down = True
            if session.handle is not None:
                try:
                    await sandbox_client.teardown(session.handle)
                except SandboxError:
                    torn_down = False
                    _log.exception(
                        "teardown failed in finalize; keeping lock+registry for the reaper",
                        session_id=str(session.session_id),
                    )
            if torn_down:
                if session.lock_token:
                    try:
                        await release_lock_as_holder(redis, session.user_id, session.lock_token)
                    except Exception:
                        _log.exception(
                            "lock release failed in finalize", session_id=str(session.session_id)
                        )
                try:
                    await delete_registry(redis, session.user_id)
                except Exception:
                    _log.exception(
                        "registry delete failed in finalize", session_id=str(session.session_id)
                    )
        finally:
            self._active_by_user.pop(session.user_id, None)
            self._maybe_prune_start_lock(session.user_id)

        # 4. Emit THE terminal `ended` — the session's one and only terminal frame (R7). It
        #    drives the derived status AND lets every SSE generator emit `[DONE]` (a bare close
        #    would leave status stuck at BUILDING/READY and hang the feed). Must run even if a
        #    prior step raised, so status is always terminal.
        #
        #    WHY HERE, and nowhere else: this point is downstream of the step-1 snapshot, so
        #    `session.snapshot_committed` is settled — true when the C4 bundle actually pushed,
        #    false when it failed/was skipped. BRAIN cannot emit this frame (no `ended` helper
        #    exists on its emitter): anything it emitted would necessarily predate the snapshot
        #    and could only ever report `snapshot_committed=false` — the exact lie R7 fixes.
        #
        #    `status` comes from BRAIN's verdict when there is one — the reason string alone
        #    cannot decide it (an `escalated` end is FAILED, a `quota_exceeded` end is ENDED, and
        #    neither equals `_BUILD_FAILED`). Only the verdict-less paths (stop / force_end /
        #    idle-reap / a raised run_build) fall back to deriving it from the reason.
        status = result.status if result is not None else _terminal_status(reason)
        preview_url = (result.preview_url if result is not None else None) or session.preview_url

        # 3b. Record the outcome in the thread (003-U5) — BEFORE the terminal frame, so the row is
        #     already there when any client learns the build is over (the reverse order races every
        #     reader). Best-effort like every other step here: a failed write must not abort the
        #     sequence and hang the SSE feed. Same values as the frame below, by construction.
        await self._record_outcome(session, status=status, preview_url=preview_url, reason=reason)

        if not session.terminal_emitted:
            ended = EndedEvent(
                status=status,
                # BRAIN's final URL wins; fall back to the last `preview_ready` we saw, so an
                # escalation that carries no URL still reports a preview that genuinely came up.
                preview_url=preview_url,
                snapshot_committed=session.snapshot_committed,
                reason=reason,
                seq=session.last_seq + 1,  # continues BRAIN's stream — gap-free across the handoff
            )
            try:
                await self.on_progress(session, ended)
            except Exception:
                _log.exception("terminal emit failed", session_id=str(session.session_id))
                session.status = status  # guarantee a terminal status regardless
        else:
            # Unreachable: `_do_finalize` runs exactly once per session (the `finalize_task`
            # single-owner guard) and is now the ONLY emitter of `ended`, so nothing can have
            # set this flag before us. Kept as the last structural line of defense for "never
            # two terminals" — but loud, because reaching it means the single-owner guard broke.
            _log.warning(
                "terminal ended already present at finalize; skipping a second emit",
                session_id=str(session.session_id),
            )

        # 5. Start the retention window — the session (and its replay buffer) stays resident
        #    for a late SSE reconnect, then `evict_ended_sessions` drops it.
        session.ended_at = datetime.now(UTC)

    # --- stop / force-end (graceful vs kill switch) --------------------------

    async def stop(
        self,
        session: BuildSession,
        sandbox_client: SandboxClient,
        *,
        reason: str = STOPPED_BY_USER,
    ) -> BuildSession:
        return await self._end(session, sandbox_client, reason=reason, force=False)

    async def force_end(
        self, session: BuildSession, sandbox_client: SandboxClient, *, reason: str = FORCE_ENDED
    ) -> BuildSession:
        return await self._end(session, sandbox_client, reason=reason, force=True)

    async def _await_end_sequence(self, session: BuildSession) -> BuildSession:
        """A terminal-committed session's end sequence, awaited to completion. The caller lost
        the race to the end sequence's owner, so it touches NO session state — it only waits
        for the shielded task and hands back the terminal session (a `stop`/`force_end` is
        idempotent, so returning mid-teardown would report a state that isn't final yet)."""
        if session.finalize_task is not None:
            with suppress(Exception):
                await asyncio.shield(session.finalize_task)
        return session

    async def _end(
        self,
        session: BuildSession,
        sandbox_client: SandboxClient,
        *,
        reason: str,
        force: bool,
    ) -> BuildSession:
        # Already ending/ended (a completion or a prior stop won the race): don't cancel —
        # just await the in-flight shielded end sequence and return the terminal state.
        if session.terminal_committed:
            return await self._await_end_sequence(session)
        # Mark-ending is best-effort and runs BEFORE the session flags are mutated: a Redis
        # blip must neither 500 the kill switch (the build would keep burning tokens) nor
        # leave a poisoned `force_ended` on a still-running session (a later natural
        # completion would then silently skip its snapshot). Matches the file's other
        # best-effort Redis paths (on_progress / _do_finalize) — logged, never swallowed.
        try:
            await mark_registry_ending(get_redis(), session.user_id)
        except Exception:
            _log.exception(
                "mark-registry-ending failed in _end; proceeding to cancel + finalize",
                session_id=str(session.session_id),
            )
        # Re-check AFTER that await — the entry check above is stale the moment we suspend.
        # INVARIANT: `force_ended` may only be written while `terminal_committed` is False,
        # checked with NO await in between. A completion landing inside the mark-ending await
        # commits the terminal and starts finalize; writing the flags now would be a write
        # BEHIND the commit — `force_ended=True` after finalize already passed its snapshot
        # step tears the container down with no bundle while the terminal frame it already
        # emitted still reports "completed". A silently lost snapshot is the worst outcome
        # this file has, so a lost race means: mutate nothing, just await the sequence.
        if session.terminal_committed:
            return await self._await_end_sequence(session)
        session.end_reason = reason
        session.force_ended = force
        task = session.task
        if task is not None and not task.done():
            task.cancel()
            # Await the FULL unwind BEFORE finalize, so no late real on_progress envelope
            # races the synthetic terminal seq (C7 gap-free invariant). Cancelling the task
            # cannot tear the end sequence: `_finalize` runs it in a SHIELDED task, so even a
            # cancel delivered while the task is already mid-finalize lets `_do_finalize`
            # complete; every caller awaits that same shielded task.
            with suppress(asyncio.CancelledError):
                await task
        await self._finalize(session, reason, sandbox_client)
        return session


# --- accessor singleton (mirrors get_redis / get_sandbox) --------------------

_manager_singleton: SessionManager | None = None


def get_session_manager() -> SessionManager:
    global _manager_singleton
    if _manager_singleton is None:
        _manager_singleton = SessionManager()
    return _manager_singleton


def set_session_manager_for_tests(manager: SessionManager | None) -> None:
    global _manager_singleton
    _manager_singleton = manager


def reset_session_manager_for_tests() -> None:
    global _manager_singleton
    _manager_singleton = None
