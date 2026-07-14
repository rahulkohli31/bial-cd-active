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
from src.services.storage import StorageError, get_storage, snapshot_key

_log = structlog.get_logger()

# `build_failed` is the only reason that maps to the terminal FAILED status; every other
# end reason (stopped_by_user / idle_teardown / quota_exceeded / completed) is graceful.
_BUILD_FAILED: str = "build_failed"


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


class SessionManager:
    """The in-process session registry + lifecycle. Held as a module singleton with an
    accessor (mirrors `get_redis`), overridable in tests."""

    def __init__(self) -> None:
        self._sessions: dict[uuid.UUID, BuildSession] = {}
        self._active_by_user: dict[uuid.UUID, uuid.UUID] = {}
        # Strong refs to background run_build tasks so a client disconnect (which cancels
        # only the SSE generator) can't let the loop GC a still-running build.
        self._tasks: set[asyncio.Task[None]] = set()

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
        redis = get_redis()
        user_id = user.id
        # Reconcile the user's OWN stale state before acquiring (KTD-3) — closes the
        # crashed-tab lockout at the exact moment it matters.
        has_live = user_id in self._active_by_user
        await reconcile_user(redis, user_id, sandbox_client, has_live_session=has_live)

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
        task.add_done_callback(self._tasks.discard)
        return session

    async def _resolve_sandbox(
        self,
        sandbox_client: SandboxClient,
        user_id: uuid.UUID,
        app_id: uuid.UUID,
        env: dict[str, str],
    ) -> SandboxHandle:
        """The one-per-user rehydrate resolution: no entry → provision / live → attach /
        gone + snapshot → restore / gone + no snapshot → provision fresh (never restore
        into a StorageNotFoundError)."""
        redis = get_redis()
        app_name = app_name_for(app_id)
        if await read_registry(redis, user_id) is None:
            return await sandbox_client.provision_new(str(user_id), app_name, app_env=env)
        try:
            return await sandbox_client.attach_existing(str(user_id))
        except SandboxGoneError:
            if await self._snapshot_exists(app_id):
                return await sandbox_client.restore_from_snapshot(
                    str(user_id), app_name, app_env=env
                )
            return await sandbox_client.provision_new(str(user_id), app_name, app_env=env)

    async def _snapshot_exists(self, app_id: uuid.UUID) -> bool:
        with suppress(StorageError):
            return await get_storage().head(snapshot_key(app_id)) is not None
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
            with suppress(Exception):
                redis = get_redis()
                await renew_lock(redis, session.user_id, session.lock_token)
                await write_heartbeat(redis, session.user_id)

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
        # Single-owner critical section: the flag flip is synchronous (no await between the
        # check and the set), so the first caller commits and every racing caller returns.
        if session.terminal_committed:
            return
        session.terminal_committed = True

        redis = get_redis()
        reason = reason or session.end_reason or "completed"

        # 1. Snapshot — only when there is live progress to persist; skipped for a force_end
        #    or when a snapshot already committed.
        needs_snapshot = (
            session.handle is not None
            and not session.force_ended
            and not session.snapshot_committed
        )
        if needs_snapshot and session.handle is not None:
            try:
                await write_snapshot(sandbox_client, session.handle, session.app_id)
                session.snapshot_committed = True
            except Exception:
                _log.exception("snapshot failed in finalize", session_id=str(session.session_id))

        # 2. Teardown (idempotent) → 3. holder release (LAST) → clear registry.
        if session.handle is not None:
            with suppress(SandboxError):
                await sandbox_client.teardown(session.handle)
        if session.lock_token:
            await release_lock_as_holder(redis, session.user_id, session.lock_token)
        await delete_registry(redis, session.user_id)
        self._active_by_user.pop(session.user_id, None)

        # 4. Synthesize a terminal `ended` if BRAIN exited without emitting one — drives the
        #    derived status AND lets every SSE generator emit `[DONE]` (a bare close would
        #    leave status stuck at BUILDING/READY and hang the feed).
        if not session.terminal_emitted:
            status = (
                BuildSessionStatus.FAILED if reason == _BUILD_FAILED else BuildSessionStatus.ENDED
            )
            ended = EndedEvent(
                status=status,
                preview_url=session.preview_url,
                snapshot_committed=session.snapshot_committed,
                reason=reason,
                seq=session.last_seq + 1,
            )
            await self.on_progress(session, ended)

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
        if session.terminal_committed:
            return session  # idempotent — a second stop returns the terminal state
        session.end_reason = reason
        session.force_ended = force
        await mark_registry_ending(get_redis(), session.user_id)
        task = session.task
        if task is not None and not task.done():
            task.cancel()
            # Await the FULL unwind BEFORE finalize, so no late real on_progress envelope
            # races the synthetic terminal seq (C7 gap-free invariant). The cancelled task
            # re-raises without finalizing (its finally awaits would themselves be cancelled).
            with suppress(asyncio.CancelledError):
                await task
        # Run the authoritative end sequence OUTSIDE the cancelled task, so its awaits
        # complete. Idempotent via `terminal_committed`: if the task already finalized
        # (a normal/failed completion racing the stop), this is a no-op.
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
