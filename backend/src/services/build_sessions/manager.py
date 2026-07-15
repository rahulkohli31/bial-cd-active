"""The in-process build-session lifecycle: `SessionManager` + `BuildSession`.

KTD-1 — the non-serializable core of a session (the `SandboxHandle` holding the raw
bearer, the progress `asyncio.Queue` subscribers, the in-process envelope buffer, the
background `run_build` task) lives in memory, NOT Postgres. On a single replica the
whole session is in-process; the frozen Redis keys (lock/heartbeat/registry) are the
durable cross-restart coordination.

KTD-2 — teardown + lock-release is SESSION-API-owned; BRAIN signals end via the `ended`
envelope + `BuildResult`, never touching Redis. `_finalize` runs the authoritative end
sequence exactly once (guarded by `terminal_committed`): snapshot → teardown → holder
release → clear registry → synthesize a terminal `ended` if BRAIN exited without one.

KTD-9 — the brain + sandbox client are threaded IN from the router's `Depends`, never
resolved inline, so `app.dependency_overrides` reach them in tests.
"""

from __future__ import annotations

import asyncio
import uuid
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.v1.build_sessions.schemas import (
    BuildSessionStatus,
    EndedEvent,
    PreviewReadyEvent,
    ProgressEnvelope,
    RunBuild,
)
from src.db.models.user import User
from src.services.build_sessions.appdata import build_app_env, resolve_app_for_project
from src.services.build_sessions.appstorage import provision_app_storage
from src.services.build_sessions.locks import (
    acquire_lock,
    delete_registry,
    mark_registry_ending,
    read_registry,
    release_lock_as_holder,
    renew_lock,
    write_heartbeat,
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
    get_storage,
    snapshot_key,
)

_log = structlog.get_logger()

# `build_failed` is the only reason that maps to the terminal FAILED status; every other
# end reason (stopped_by_user / idle_teardown / quota_exceeded / completed) is graceful.
_BUILD_FAILED: str = "build_failed"

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


class BuildSessionConflictError(Exception):
    """The user already holds a live build session (the one-per-user lock is held).
    Carries the existing `session_id` so the router can surface it in the 409."""

    def __init__(self, session_id: uuid.UUID | None) -> None:
        super().__init__("a build session is already active")
        self.session_id = session_id


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

    def __init__(self) -> None:
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
        run_build: RunBuild,
        sandbox_client: SandboxClient,
    ) -> BuildSession:
        # Opportunistic retention sweep — the only guaranteed-recurring seam (no background
        # task), so ended sessions never accumulate unboundedly.
        self.evict_ended_sessions()
        # Serialize concurrent same-user starts: the whole start (reconcile → acquire →
        # provision → register) runs under one per-user lock, so a second start can't
        # reconcile-away the first start's in-flight lock (held but registry-not-yet-written)
        # and double-allocate a sandbox (the critical reap_lock/fresh-acquire race).
        async with self._start_lock_for(user.id):
            return await self._start_locked(
                db, user, project_id, prompt, run_build=run_build, sandbox_client=sandbox_client
            )

    async def _start_locked(
        self,
        db: AsyncSession,
        user: User,
        project_id: uuid.UUID,
        prompt: str,
        *,
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
        """Restore the C4 snapshot when one exists; provision a fresh template otherwise
        (never restore into a StorageNotFoundError). A snapshot that vanishes between the
        head-check and the pull falls back to fresh rather than failing the start."""
        # Ensure the app's Blob container + mint a fresh session SAS ONLY on this birth
        # (provision/restore) arm — never on attach, which reuses the live container's SAS (KTD-3).
        # A configured-store failure propagates: it fails the start before any sandbox handle
        # exists (start's compensation releases the lock; nothing to tear down), and the idempotent
        # container is simply reused on the next start. Disabled storage (dev/test) yields {} — a
        # no-op merge (KTD-2). C9 §6.
        env = {**env, **await provision_app_storage(app_id)}
        if await self._snapshot_exists(app_id):
            try:
                return await sandbox_client.restore_from_snapshot(
                    str(user_id), app_name, app_env=env
                )
            except StorageNotFoundError:
                _log.warning(
                    "snapshot disappeared between head-check and restore; provisioning fresh",
                    app_id=str(app_id),
                )
        return await sandbox_client.provision_new(str(user_id), app_name, app_env=env)

    async def _snapshot_exists(self, app_id: uuid.UUID) -> bool:
        try:
            return await get_storage().head(snapshot_key(app_id)) is not None
        except StorageError:
            # A transient storage error is treated the same as "confirmed absent", which
            # biases toward provisioning FRESH — discarding the user's restorable work. Make
            # it visible at least, rather than swallowing it silently.
            # TODO(#9): a 3-state result (present / absent / unknown) so a transient error
            #   retries instead of silently discarding a snapshot — a separate design call.
            _log.exception(
                "snapshot head-check failed; treating as absent (may discard restorable work)",
                app_id=str(app_id),
            )
            return False

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
        it single-owner across the two paths (KTD-2)."""

        async def sink(env: ProgressEnvelope) -> None:
            await self.on_progress(session, env)

        try:
            await run_build(session.session_id, session.user_id, sandbox_client, sink)
        except asyncio.CancelledError:
            raise  # stop/force_end finalizes; just unwind
        except Exception:
            _log.exception("run_build raised", session_id=str(session.session_id))
            await self._finalize(session, _BUILD_FAILED, sandbox_client)
            return
        await self._finalize(session, None, sandbox_client)

    async def _finalize(
        self, session: BuildSession, reason: str | None, sandbox_client: SandboxClient
    ) -> None:
        """Single-owner dispatcher: the FIRST caller (synchronously, no await between the
        check and the create) spawns ONE shielded end-sequence task; every caller then
        awaits it under `asyncio.shield`, so a caller's own cancellation (a racing stop
        cancelling the run_build task mid-finalize) can NEVER tear the sequence in half —
        `_do_finalize` runs to completion in its own task regardless."""
        if session.finalize_task is None:
            session.terminal_committed = True
            session.finalize_task = asyncio.ensure_future(
                self._do_finalize(session, reason, sandbox_client)
            )
        await asyncio.shield(session.finalize_task)

    async def _do_finalize(
        self, session: BuildSession, reason: str | None, sandbox_client: SandboxClient
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

        # 4. Synthesize a terminal `ended` if BRAIN exited without emitting one — drives the
        #    derived status AND lets every SSE generator emit `[DONE]` (a bare close would
        #    leave status stuck at BUILDING/READY and hang the feed). Must run even if a
        #    prior step raised, so status is always terminal.
        status = BuildSessionStatus.FAILED if reason == _BUILD_FAILED else BuildSessionStatus.ENDED
        if not session.terminal_emitted:
            ended = EndedEvent(
                status=status,
                preview_url=session.preview_url,
                snapshot_committed=session.snapshot_committed,
                reason=reason,
                seq=session.last_seq + 1,
            )
            try:
                await self.on_progress(session, ended)
            except Exception:
                _log.exception("terminal synthesis failed", session_id=str(session.session_id))
                session.status = status  # guarantee a terminal status regardless

        # 5. Start the retention window — the session (and its replay buffer) stays resident
        #    for a late SSE reconnect, then `evict_ended_sessions` drops it.
        session.ended_at = datetime.now(UTC)

    # --- stop / force-end (graceful vs kill switch) --------------------------

    async def stop(
        self,
        session: BuildSession,
        sandbox_client: SandboxClient,
        *,
        reason: str = "stopped_by_user",
    ) -> BuildSession:
        return await self._end(session, sandbox_client, reason=reason, force=False)

    async def force_end(
        self, session: BuildSession, sandbox_client: SandboxClient, *, reason: str = "force_ended"
    ) -> BuildSession:
        return await self._end(session, sandbox_client, reason=reason, force=True)

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
            if session.finalize_task is not None:
                with suppress(Exception):
                    await asyncio.shield(session.finalize_task)
            return session
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
