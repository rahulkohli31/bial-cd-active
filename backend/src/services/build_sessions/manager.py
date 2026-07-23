"""The in-process build-session lifecycle: `SessionManager` + `BuildSession`.

KTD-1 — the non-serializable core of a session (the `SandboxHandle` holding the raw
bearer, the progress `asyncio.Queue` subscribers, the in-process envelope buffer, the
background `run_build` task) lives in memory, NOT Postgres. On a single replica the
whole session is in-process; the frozen Redis keys (lock/heartbeat/registry) are the
durable cross-restart coordination.

KTD-2 — teardown + lock-release is SESSION-API-owned; BRAIN signals end by RETURNING a
`BuildResult`, never touching Redis and never emitting a terminal frame. `_finalize` runs
the authoritative end sequence exactly once (guarded by `terminal_committed`): snapshot →
teardown-or-pardon → holder release → emit THE terminal `ended`. A COMPLETED build's
container is PARDONED, not executed (#13/R2): it stays up under the bounded stay-of-
execution lease (registry kept, lock released) so the user can use what they just built;
every other end path — quota / escalated / stop / force_end / idle-reap / a raised
run_build — still tears down and clears the registry.

That order is the whole point of R7: the `ended` is emitted AFTER the step-1 snapshot, so
its `snapshot_committed` is the real post-commit value. Every end path converges on this
one emission, so the feed carries exactly one terminal, always truthful.

KTD-9 — the brain + sandbox client are threaded IN from the router's `Depends`, never
resolved inline, so `app.dependency_overrides` reach them in tests.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Literal

import redis.asyncio as aioredis
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
from src.services.build_sessions.appdb_env import provision_app_database
from src.services.build_sessions.appstorage import provision_app_storage
from src.services.build_sessions.attachments import resolve_build_attachments
from src.services.build_sessions.liveness import flag_liveness_overpromise
from src.services.build_sessions.locks import (
    acquire_lock,
    delete_registry,
    grant_stay_of_execution,
    mark_registry_ending,
    read_registry,
    release_lock_as_holder,
    renew_lock,
    write_heartbeat,
)
from src.services.build_sessions.outcome import (
    FORCE_ENDED,
    STOPPED_BY_USER,
    newest_build_outcome_status,
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

# The one end reason that PARDONS the container instead of tearing it down (#13/R2): a
# successful build's preview stays live under the idle lease so the user sees what they
# just built. Matches BRAIN's success verdict and `_do_finalize`'s legacy fallback.
_COMPLETED: str = "completed"

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


class NoSnapshotToRelaunchError(Exception):
    """Relaunch (#43) found no saved snapshot to restore: the project was never built, or its
    bundle is CONFIRMED absent. Distinct from `SnapshotUnavailableError` (transient/unknown →
    503): this is a definite "nothing to relaunch", which the router maps to a 404. Unlike a
    build start, relaunch has NO fresh-provision fallback — a blank template is not a preview
    of the user's app — so a confirmed-absent bundle is a dead end, not a blank start."""

    def __init__(self, app_id: uuid.UUID) -> None:
        super().__init__("no saved build to relaunch")
        self.app_id = app_id


def app_name_for(app_id: uuid.UUID) -> str:
    """An ACA-compliant container name (2–32 chars, lowercase alphanumeric/hyphen,
    letter-first, ends alphanumeric), stable per app: `sbx-` + 28 hex chars of the
    app_id (`str(app_id)` is an invalid ACA name — dots/length; the hex slug is safe)."""
    return f"sbx-{app_id.hex[:28]}"


@dataclass(frozen=True)
class RelaunchedPreview:
    """What `relaunch_preview` (#43) hands the router: the durable app id, the live (READY)
    preview URL, and whether the newest recorded build outcome for the project was FAILED —
    in which case the restored snapshot is the last SAVED state, not that build's intent, and
    the portal labels it "last saved version" (U6)."""

    app_id: uuid.UUID
    preview_url: str
    restored_from_failed_build: bool


@dataclass
class _LockScope:
    """The mutable state a `_holding_user_lock` body shares with its compensation: the held
    token, any container the body created (torn down if the body fails), and whether the body
    ADOPTED the lock+container (a start's session takes ownership — `_do_finalize` releases
    and tears down — so a clean exit must not release, and compensation must not touch them)."""

    token: str
    handle: SandboxHandle | None = None
    adopted: bool = False

    def adopt(self) -> None:
        self.adopted = True


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

    # --- the shared acquire-with-conflict-check + compensated-release shape ---

    async def _compensate_lock_and_container(
        self,
        redis: aioredis.Redis,
        user_id: uuid.UUID,
        scope: _LockScope,
        sandbox_client: SandboxClient,
    ) -> None:
        """Undo a failed `_holding_user_lock` body: tear down any container it created, then
        holder-release the lock (LAST — mirroring `reap_user`'s ordering, so a concurrent
        start can never acquire while the doomed container is still up). Each step is guarded
        separately: a Redis blip on release must never mask the teardown, and vice versa. An
        adopted scope is a no-op — the session owns both from `_do_finalize` onward."""
        if scope.adopted:
            return
        if scope.handle is not None:
            with suppress(SandboxError):
                await sandbox_client.teardown(scope.handle)
        try:
            await release_lock_as_holder(redis, user_id, scope.token)
        except Exception:
            _log.exception("lock release failed in compensation", user_id=str(user_id))

    @asynccontextmanager
    async def _holding_user_lock(
        self,
        redis: aioredis.Redis,
        user_id: uuid.UUID,
        sandbox_client: SandboxClient,
    ) -> AsyncIterator[_LockScope]:
        """Reconcile stale state → acquire the one-per-user Redis lock → run the body
        compensated. The ONE skeleton behind `_start_locked` and `relaunch_preview` (their
        pre-checks deliberately differ — see each call site).

        THE TWO WAYS THE LOCK CAN DENY, and why they leave here as different exceptions
        (U3). `acquire_lock` returning `None` now means one thing only — the lock is
        genuinely HELD — so `BuildSessionConflictError` (router 409, carrying the live
        session id) is always a true statement about a real session. A Redis failure
        instead raises `LockUnavailableError` (a `RedisError`), which passes straight
        through to the router's `build_coordination_or_503` and becomes a 503. Before that
        split, an outage was swallowed into the same `None` and every affected user was
        told a build session was already active when none existed.

        `reconcile_user` above runs BEFORE the acquire and calls the deliberately-unguarded
        primitives (see the REDIS-ERROR POLICY in `locks.py`), so a HARD outage usually
        raises there first — a raw `RedisError`, which the same router seam maps to the same
        503. Both shapes land on one status; neither is a 409 and neither is a 500.

        The reconcile passes `certified_dead=True` (#10/R3): every caller of this context
        manager holds the per-user `_start_lock_for` AND has already verified
        `user_id not in _active_by_user`, and the deploy contract is single-replica — so a
        lock/heartbeat still present in Redis here is a dead session's residue, not
        liveness, and reconcile reaps THROUGH it instead of letting the acquire below 409
        on a ghost. The sweep's `reconcile_user` keeps the shield (it holds neither fact).

        Failure-safe by construction:
        - Compensation runs on ANY body failure INCLUDING CancelledError — relaunch blocks for
          minutes, so a dropped request (uvicorn cancels the handler) must still tear down the
          container and release the lock. It runs in its own task awaited under `shield`
          (the `_finalize` pattern), so even a second cancel delivered mid-compensation lets
          it complete.
        - A clean exit releases the lock UNLESS the body adopted it (start's session owns the
          token; `_do_finalize` releases). The release sits inside the protected region: if it
          fails, compensation still tears the container down rather than leaving a live
          preview behind a lock nobody can release.
        """
        await reconcile_user(
            redis, user_id, sandbox_client, has_live_session=False, certified_dead=True
        )
        token = await acquire_lock(redis, user_id)
        if token is None:
            raise BuildSessionConflictError(self._active_by_user.get(user_id))
        scope = _LockScope(token=token)
        try:
            yield scope
            if not scope.adopted:
                await release_lock_as_holder(redis, user_id, token)
        except BaseException:
            comp = asyncio.ensure_future(
                self._compensate_lock_and_container(redis, user_id, scope, sandbox_client)
            )
            self._tasks.add(comp)
            comp.add_done_callback(self._tasks.discard)
            # `suppress` here only ever eats a re-delivered CancelledError from the shield
            # await (the compensation task itself never raises); the original failure is
            # re-raised either way, with the compensation guaranteed to run to completion.
            with suppress(BaseException):
                await asyncio.shield(comp)
            raise

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

    async def relaunch_preview(
        self,
        db: AsyncSession,
        user: User,
        project_id: uuid.UUID,
        sandbox_client: SandboxClient,
    ) -> RelaunchedPreview:
        """Restore a project's saved app into a fresh, READY sandbox — the #43 "Relaunch
        preview" path for an app whose live build session has already been torn down.

        Deliberately NOT a build (Decision 6): it runs under `_holding_user_lock` (the same
        skeleton as `_start_locked`) but never adopts the lock, never enters `_active_by_user`
        and never spawns a `run_and_finalize` task, so it does NOT occupy the one-per-user
        build slot — the user's next real build never 409s on a relaunched preview. It
        registers a READY handle in Redis (a side effect of `restore_from_snapshot` via
        `_write_registry`), seeds a heartbeat, then the scope RELEASES the per-user lock on
        exit. Nothing here re-snapshots: the workspace is served read-only (an edit is a new
        build, which finalizes normally), so `_do_finalize` — the only writer of a snapshot —
        is never on this path.

        Because it holds no lock and renews no heartbeat, its container's lifetime is owned
        by an explicit STAY OF EXECUTION granted below: a bounded lease on the registry hash
        that the background sweep honors and then reaps through. Reconcile-on-start reaps it
        immediately regardless of the lease — that build needs the one-per-user slot, and
        sparing the preview there would orphan its container under the new registry entry.

        Diverges from `_start_locked` in two deliberate ways:
        - No finalize-grace wait on a terminal-committed session: the snapshot relaunch would
          restore is written only by that session's finalize, so 409ing until it settles is
          correct — never unify this with start's `_FINALIZE_GRACE_SECONDS` arm.
        - It must NOT reuse `_restore_or_provision`, whose confirmed-absent arm provisions a
          BLANK template — the wrong answer for relaunch, where an empty app is not a preview
          of the user's work. Instead it checks the snapshot itself and restores directly:
          confirmed-absent (or vanished) snapshot → `NoSnapshotToRelaunchError` (router 404);
          transient/unknown snapshot state, or a restore that fails every attempt →
          `SnapshotUnavailableError` (router 503); a live build already active for this user →
          `BuildSessionConflictError` (router 409).
        """
        async with self._start_lock_for(user.id):
            redis = get_redis()
            user_id = user.id
            if user_id in self._active_by_user:
                raise BuildSessionConflictError(self._active_by_user.get(user_id))
            async with self._holding_user_lock(redis, user_id, sandbox_client) as scope:
                app_id = await resolve_app_for_project(db, user_id, project_id)
                # The snapshot gate runs BEFORE the commit and the storage provision: the 404
                # path must not persist the speculative DRAFT app row (`get_db` rolls the
                # uncommitted insert back) nor provision blob storage for an app that was
                # never built. No fresh-provision fallback: a confirmed-absent bundle is a
                # dead end (404), never a blank template.
                if not await self._snapshot_exists_or_bust(app_id):
                    raise NoSnapshotToRelaunchError(app_id)
                # U6's "last saved version" signal: when the newest recorded outcome FAILED,
                # the snapshot being restored is the last SAVED state, not that build's intent.
                restored_from_failed_build = (
                    await newest_build_outcome_status(db, user_id=user_id, project_id=project_id)
                    is BuildSessionStatus.FAILED
                )
                await db.commit()
                # The FIVE injected vars (the two always-present BIAL_* + the two blob
                # coordinates with a freshly rotated SAS + the per-project DSN), exactly as a
                # start's birth arm builds them. Deliberately written twice — this must NOT be
                # unified with `_restore_or_provision` (see the docstring above), so a var added
                # to only one of the two sites is a silent half-fix.
                env = {
                    **build_app_env(app_id),
                    **await provision_app_storage(app_id),
                    **await provision_app_database(db, project_id),
                }
                # `_restore_or_bust` re-raises `StorageNotFoundError` (a bundle that vanished
                # between head-check and pull) — the same 404 bucket.
                try:
                    scope.handle = await self._restore_or_bust(
                        sandbox_client, user_id, app_name_for(app_id), app_id, env
                    )
                except StorageNotFoundError as exc:
                    raise NoSnapshotToRelaunchError(app_id) from exc
                # The lease starts HERE, not at the end. `_restore_or_bust` has just created
                # the container AND written its registry hash, so from this instant the
                # sweep can see a user whose state reads: registry PRESENT, lock held,
                # heartbeat ABSENT — and `reconcile_user`'s guard is an AND, so lock-held-
                # without-a-heartbeat is REAPABLE. `live_users` does not cover it either: a
                # relaunch never enters `_active_by_user` (Decision 6). Without a stay at
                # this point a concurrent sweep tears down the container we are still
                # bringing up, and this call still returns 200 with a dead preview URL.
                # Seeding the heartbeat early is NOT a substitute: HEARTBEAT_TTL_SECONDS is
                # 90 s while `wait_ready` waits up to 120 s, so the beat can lapse mid-wait.
                # Re-granted after `write_heartbeat` below, so the user-visible 30 minutes
                # starts from READY rather than from the start of a multi-minute provision.
                await grant_stay_of_execution(redis, user_id)
                # `restore_from_snapshot` returns a ready=False handle; without dev_start +
                # wait_ready the fresh preview URL 404s. This is the step restore omits.
                await sandbox_client.dev_start(scope.handle)
                scope.handle = await sandbox_client.wait_ready(scope.handle)
                preview_url = scope.handle.preview_url
                # Seed the heartbeat INSIDE the protected region (never enter `_active_by_user`,
                # never spawn a finalize task — Decision 6): if it fails, the compensation still
                # tears the container down + releases the lock instead of 500ing with a live
                # container behind a held lock. The scope releases the lock on clean exit.
                await write_heartbeat(redis, user_id)
                # …and RE-grant the bounded lease that actually owns this container's
                # lifetime: nothing renews that heartbeat, so without a stay the background
                # sweep would reap a preview the user is still reading (and without the
                # sweep the container would outlive everyone). Re-granted rather than
                # granted because the provision window above already needed one — this
                # second stamp simply re-bases the 30 minutes on the instant the preview
                # actually became viewable. Inside the protected region for the same reason
                # as the heartbeat: a failure here tears the container down rather than
                # leaving it running with no owner at all.
                await grant_stay_of_execution(redis, user_id)
            return RelaunchedPreview(
                app_id=app_id,
                preview_url=preview_url,
                restored_from_failed_build=restored_from_failed_build,
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
        # SINGLE-REPLICA CONSTRAINT (binding — see the deploy checklist): this guard is the
        # `self._active_by_user` in-process dict, so on two replicas there are two guards
        # that cannot see each other and the same user could run two concurrent builds — the
        # Redis lock is the ONLY cross-process backstop, and it is deliberately not trusted
        # as the sole guard here. One replica is a deploy-time invariant, not a runtime check.
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
        # Not live: reconcile the user's OWN stale state before acquiring (KTD-3 — closes the
        # crashed-tab lockout at the exact moment it matters), then run the provision steps
        # compensated: any failure — a cancelled request included — tears down any container
        # that was created and holder-releases the lock (`_holding_user_lock`).
        async with self._holding_user_lock(redis, user_id, sandbox_client) as scope:
            app_id = await resolve_app_for_project(db, user_id, project_id)
            await db.commit()
            # The DSN merge lives HERE and not beside the storage merge in
            # `_restore_or_provision`, which has neither `db` nor `project_id` in scope —
            # the database is PROJECT-keyed while everything on that seam is app-keyed.
            # It must also follow the commit above: `ensure_project_database` commits its
            # own claim and its own terminal marker, so calling it earlier would commit a
            # half-built request transaction (the speculative DRAFT app row included).
            # This is also the LAZY ensure: a project created before the feature existed,
            # or while the substrate was unconfigured, is provisioned on its next build.
            env = {
                **build_app_env(app_id),
                **await provision_app_database(db, project_id),
            }
            handle = await self._resolve_sandbox(sandbox_client, user_id, app_id, env)
            scope.handle = handle  # compensation tears it down until the session adopts it
            # Seed the heartbeat INSIDE the protected region, BEFORE adopt (mirroring
            # relaunch_preview's seed) so an immediate reconcile can't reap the fresh session.
            # The placement is load-bearing: under the new retry policy a `write_heartbeat`
            # RedisError is a multi-second window, and out here — after adopt, after the block
            # exited — it propagated uncaught, orphaning `_active_by_user[user_id]` forever and
            # leaking the container. Inside the region (scope not yet adopted) its raise is caught
            # by `_holding_user_lock`'s `except BaseException`, which tears the container down and
            # releases the lock, exactly as a failing lock acquire does.
            await write_heartbeat(redis, user_id)
            # The session ADOPTS the lock + container: from here `_do_finalize` owns their
            # release/teardown, so the scope must not release on exit.
            scope.adopt()

        session = BuildSession(
            session_id=uuid.uuid7(),
            user_id=user_id,
            project_id=project_id,
            app_id=app_id,
            prompt=prompt,
            lock_token=scope.token,
            handle=handle,
            attachments=attachments,
            conversation_id=conversation_id,
            started_seq=started_seq,
        )
        self._sessions[session.session_id] = session
        self._active_by_user[user_id] = session.session_id

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
        the user's work onto a blank template.

        The ATTACH arm passes no `env`, and that is correct: a container keeps its BIRTH
        env forever (ACA env vars are set on the revision, not on a running process). Same
        reason the Blob SAS is not rotated on attach (KTD-3) — and the same consequence for
        `BIAL_DATABASE_URL`: re-pointing an app at a different database means a REBIRTH
        (teardown + restore), never an attach."""
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

    async def _pardon_the_container(self, redis: aioredis.Redis, session: BuildSession) -> None:
        """#13/R2 — the success-path alternative to teardown: the container outlives its
        build so the user can actually use what they just built.

        Mirrors `relaunch_preview`'s lifetime model exactly. The registry entry STAYS (it is
        the sweep's only map to the container — deleting it would orphan a live sandbox);
        the bounded stay of execution owns the lifetime (`sweep_all(honor_stay=True)` spares
        the preview until the lease lapses, then reaps through it); and the per-user lock is
        released so a pardoned preview never occupies the one-build slot. Reconcile-on-start
        still reaps THROUGH an unexpired stay (the incoming build needs the slot), which is
        the "cleanly replaced, never orphaned" half of the contract — the freshly written
        step-1 snapshot is what the next start restores.

        ORDER IS LOAD-BEARING: the stay is granted while the lock is STILL HELD. Releasing
        first would open a window where a concurrent sweep sees lock-gone (and, ≤90 s later,
        heartbeat-lapsed) with no lease yet, and executes the container we just pardoned.

        Best-effort per the end-sequence policy (a raise here would hang every SSE feed).
        Degraded modes are all safe: a failed stay grant means the sweep reaps at heartbeat
        lapse (~90 s — the pre-#13 lifetime, never an orphan, because the registry is still
        there to find); a failed lock release means the lock lingers to its TTL and the next
        start's `reap_lock` clears it."""
        try:
            await grant_stay_of_execution(redis, session.user_id)
        except Exception:
            _log.exception(
                "stay grant failed in pardon; the sweep will reap at heartbeat lapse",
                session_id=str(session.session_id),
            )
        if session.lock_token:
            try:
                await release_lock_as_holder(redis, session.user_id, session.lock_token)
            except Exception:
                _log.exception("lock release failed in pardon", session_id=str(session.session_id))

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
        continues to the terminal synthesis (C4/C5 ordering: snapshot → teardown-or-pardon
        → release → synthesize)."""
        redis = get_redis()
        reason = reason or session.end_reason or _COMPLETED

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

        # 1b. The #46 generation-time detector (plan U1): while the container is still up, flag
        #     an app whose copy promises live/shared data with no refetch anywhere in the
        #     workspace. A structlog signal only — never a gate — and it swallows its own
        #     failures, so it can never delay or break the end sequence beyond one exec.
        if session.handle is not None and not session.force_ended:
            await flag_liveness_overpromise(
                sandbox_client,
                session.handle,
                app_id=session.app_id,
                session_id=session.session_id,
            )

        # The terminal status/URL are computed BEFORE step 2 because the teardown-or-pardon
        # decision needs them (see WHY at the emit below for the status derivation rules).
        status = result.status if result is not None else _terminal_status(reason)
        preview_url = (result.preview_url if result is not None else None) or session.preview_url

        # #13/R2 — the pardon decision: ONLY a genuinely successful build keeps its
        # container. `status` (not just the reason string) is part of the test so a
        # hypothetical FAILED verdict carrying a "completed" reason could never leave a
        # broken container running as if it were a success.
        pardoned = (
            reason == _COMPLETED
            and status is BuildSessionStatus.ENDED
            and not session.force_ended
            and session.handle is not None
        )

        # 2. Teardown → 3. holder release (LAST) → clear registry — or, on the completed
        #    path, PARDON: keep the container + registry, lease its lifetime, release the
        #    lock (see `_pardon_the_container`). Release + registry-delete run ONLY on a
        #    CLEAN teardown: a teardown SandboxError means the container may still be live,
        #    so KEEP the Redis lock + registry (mirroring reaper.reap_user's
        #    keep-state-on-failure) for the next reaper sweep to retry — clearing them now
        #    would orphan a container the reaper's registry-only scan can never see again.
        #    `_active_by_user` is popped regardless (guaranteed-run finally) so the SSE feed
        #    always closes even on a kept-state teardown failure.
        try:
            if pardoned:
                await self._pardon_the_container(redis, session)
            else:
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
                            await release_lock_as_holder(
                                redis, session.user_id, session.lock_token
                            )
                        except Exception:
                            _log.exception(
                                "lock release failed in finalize",
                                session_id=str(session.session_id),
                            )
                    try:
                        await delete_registry(redis, session.user_id)
                    except Exception:
                        _log.exception(
                            "registry delete failed in finalize",
                            session_id=str(session.session_id),
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
        #    (`status`/`preview_url` are computed above step 2 — the pardon decision needs them.)

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
