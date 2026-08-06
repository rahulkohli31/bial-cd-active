"""Build-sessions HTTP router — the C3 control surface (Wave 1).

`start` / `stop` / `status` + the five lock ops + the superadmin `internal/reap`, all
owner-scoped by `user.id` (ADR-0004): every not-found-or-other-user case is a non-leaking
404 EXCEPT the one owner-asserted 403 on `force-end` (C3). The mutating POSTs carry the
reusable `RequireCsrf` dependency (KTD-4); the `status` GET and the GET-SSE progress feed
(`sse.py`, `Last-Event-ID`-resumable) are exempt.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Request, status
from fastapi.responses import JSONResponse, StreamingResponse

from src.api.deps import CurrentUser, DbSession
from src.api.deps_rbac import CurrentSuperadmin
from src.api.v1.build_sessions.deps import (
    OptionalSandbox,
    RequireCsrf,
    RunBuildDep,
    SandboxDep,
    SessionManagerDep,
)
from src.api.v1.build_sessions.schemas import (
    HEARTBEAT_CADENCE_SECONDS,
    LOCK_TTL_SECONDS,
    BuildSessionStatus,
    BuildSessionStatusResponse,
    ForceEndResponse,
    HeartbeatResponse,
    LockReleaseResponse,
    LockStateResponse,
    RelaunchPreviewRequest,
    RelaunchPreviewResponse,
    StartBuildRequest,
    StartBuildResponse,
    StopBuildRequest,
    StopBuildResponse,
)
from src.api.v1.build_sessions.sse import build_sse_response
from src.core.errors import AppApiError
from src.schemas import AUTH_401, CamelModel, ErrorEnvelope, error_responses
from src.services.audit.log import append_audit
from src.services.build_sessions import (
    BuildAttachmentError,
    BuildSession,
    BuildSessionConflictError,
    ConversationNotFoundError,
    NoLiveSandboxError,
    NoSnapshotToRelaunchError,
    SessionManager,
    SnapshotUnavailableError,
    lock_expires_at,
    release_lock_as_holder,
    renew_lock,
    sweep_all,
    write_heartbeat,
)
from src.services.projects.resolve import owned_project_or_404
from src.services.redis import (
    BUILD_COORDINATION_UNAVAILABLE_MSG,
    build_coordination_or_503,
    get_redis,
)
from src.services.sandbox import SandboxError

router = APIRouter(prefix="/build-sessions", tags=["build_sessions"])

# R6 — the user-approved wording, VERBATIM. Note there is deliberately no trailing period,
# unlike its neighbours: this exact string is the approved copy and reaches the portal
# unmodified. Do not reword, re-punctuate or "improve" it; a test pins it character-for-
# character so a well-meaning edit fails CI instead of shipping.
_SANDBOX_UNAVAILABLE_MSG = "Sandbox unavailable. Please try again later or contact the admin"


class ReapResponse(CamelModel):
    """`POST /internal/reap` → 200 — what the sweep reaped, and what it could not.

    `failed` is not decoration: without it a sweep in which every user threw is reported as
    `{"reaped": 0}`, which is indistinguishable from a sweep that found nothing to do."""

    reaped: int
    failed: int = 0


class _ConflictError(CamelModel):
    """The inner error object of a build-session 409 (`start` already-active, or `lock/acquire`
    while another session holds the lock): the plain `{message, code}` envelope PLUS the
    existing session's id, which `_conflict_response` carries but `ErrorEnvelope` omits."""

    message: str
    code: str
    session_id: str | None = None  # → `sessionId`; present when the live session is known.


class ConflictEnvelope(CamelModel):
    """`{"error": {message, code, sessionId?}}` — a build-session 409 body
    (`_conflict_response`), documenting the `sessionId` the plain `ErrorEnvelope` omits.
    `sessionId` is optional, so this also describes the `lock_lost` 409 (which carries none)."""

    error: _ConflictError


def _owned_or_404(
    manager: SessionManager, session_id: uuid.UUID, user_id: uuid.UUID
) -> BuildSession:
    """Load a session scoped to its owner, or fail closed with a non-leaking 404 (a
    cross-user id is indistinguishable from a missing one, ADR-0004)."""
    session = manager.get(session_id)
    if session is None or session.user_id != user_id:
        raise AppApiError(status.HTTP_404_NOT_FOUND, "Build session not found.")
    return session


def _conflict_response(exc: BuildSessionConflictError) -> JSONResponse:
    error: dict[str, str] = {
        "message": "A build session is already active.",
        "code": "build_session_already_active",
    }
    if exc.session_id is not None:
        error["sessionId"] = str(exc.session_id)  # carry the existing session (C3)
    return JSONResponse(status_code=status.HTTP_409_CONFLICT, content={"error": error})


def _coordination_is_gone() -> AppApiError:
    """The 503 for the ONE arm `build_coordination_or_503` deliberately does not raise on:
    `RedisNotConfiguredError`, whose contract is "proceed" (KD-1).

    That contract is right for a GATE — submit asks "is a build live?", and with no Redis
    the answer is a certain no, so submit proceeds. It is wrong for every route below,
    where Redis is not consulted about the operation, it IS the operation: with no
    coordination subsystem there is nowhere to take a lock, seed a heartbeat, or register a
    session. So each `with` block returns from inside itself, and reaching the line AFTER
    it means the helper skipped the body — which for these routes is a refusal, not a pass.
    Same user-facing copy: from the caller's side "not configured yet" and "not answering"
    are the same unavailable service, and the difference is an internal detail
    (`.claude/rules/security.md`). Unreachable in production, where the settings gate
    requires Redis."""
    return AppApiError(status.HTTP_503_SERVICE_UNAVAILABLE, BUILD_COORDINATION_UNAVAILABLE_MSG)


# --- internal/reap (registered FIRST so `internal` is never parsed as a session id) ---


@router.post(
    "/internal/reap",
    dependencies=[RequireCsrf],
    responses=error_responses(
        AUTH_401,
        (403, ErrorEnvelope, "CSRF check failed"),
        (503, ErrorEnvelope, "Build coordination is temporarily unavailable"),
    ),
)
async def internal_reap(
    admin: CurrentSuperadmin,
    db: DbSession,
    sandbox: SandboxDep,
    manager: SessionManagerDep,
) -> ReapResponse:
    """Operator-triggered full reconciliation sweep (KTD-3) — `CurrentSuperadmin`-guarded,
    CSRF'd, audited, idempotent, concurrency-safe. Automated headless scheduling is deferred
    hardening (a machine-auth path; `CurrentSuperadmin` is cookie-only)."""
    # Retention sweep of ended in-process sessions rides the same operator path (the other
    # opportunistic seam is start()) — no background task.
    manager.evict_ended_sessions()
    # U3 — the sweep walks the registry namespace with bare primitives, so an outage here
    # is a 503 to the operator rather than an opaque 500. The audit row is deliberately
    # inside: a sweep that never ran is not an action worth recording. Redis is resolved
    # LAZILY inside the seam (never an eager Redis dependency — the `RedisDep` alias that
    # caused this has been deleted, KTD-9): on a Redis-off deployment
    # `get_redis()` raises here and the seam's trailing `_coordination_is_gone()` answers 503
    # — an eager dependency would raise at solve-time and become an undocumented 500.
    with build_coordination_or_503():
        redis = get_redis()
        result = await sweep_all(redis, sandbox, live_users=manager.live_user_ids())
        await append_audit(
            db,
            actor_id=admin.id,
            action="build_session.reap",
            resource_type="build_session",
            # `failed` belongs in the trail too: an audit row saying only "reaped 0" for a
            # sweep in which every user threw is a false record of a clean run.
            detail={"reaped": result.reaped, "failed": result.failed},
        )
        await db.commit()
        return ReapResponse(reaped=result.reaped, failed=result.failed)
    raise _coordination_is_gone()


# --- control ops: start / stop / status --------------------------------------


@router.post(
    "",
    status_code=status.HTTP_201_CREATED,
    response_model=StartBuildResponse,
    dependencies=[RequireCsrf],
    responses=error_responses(
        (403, ErrorEnvelope, "CSRF check failed"),
        AUTH_401,
        (404, ErrorEnvelope, "Project or conversation not found"),
        (409, ConflictEnvelope, "A build session is already active"),
        (422, ErrorEnvelope, "An attached file could not be used in the build"),
        (
            503,
            ErrorEnvelope,
            "Build engine not configured, or the sandbox or build coordination "
            "is temporarily unavailable",
        ),
    ),
)
async def start_build(
    body: StartBuildRequest,
    user: CurrentUser,
    db: DbSession,
    sandbox: OptionalSandbox,
    run_build: RunBuildDep,
    manager: SessionManagerDep,
) -> StartBuildResponse | JSONResponse:
    if run_build is None:
        raise AppApiError(status.HTTP_503_SERVICE_UNAVAILABLE, "Build engine not configured.")
    # This route's `responses=` names the sandbox in its 503 ("Build engine not configured, or
    # the sandbox or build coordination is temporarily unavailable"), so an unconfigured sandbox
    # owes the caller THAT answer. It arrives as `None` (the None-tolerant `OptionalSandbox`)
    # rather than raising at dependency-solve time, where no `except` here could have reached it.
    if sandbox is None:
        raise AppApiError(status.HTTP_503_SERVICE_UNAVAILABLE, _SANDBOX_UNAVAILABLE_MSG)
    # U3 — the whole start is inside the coordination seam, because Redis is touched at three
    # points the caller cannot tell apart: `reconcile_user` (raw `RedisError`), the lock
    # acquire (`LockUnavailableError`), and the heartbeat seed. Every one of them now answers
    # with the same retryable 503 instead of a 500, or a 409 inventing a session that never
    # existed.
    with build_coordination_or_503():
        try:
            session = await manager.start(
                db,
                user,
                body.project_id,
                body.prompt,
                conversation_id=body.conversation_id,
                run_build=run_build,
                sandbox_client=sandbox,
            )
        except ConversationNotFoundError as exc:
            # R3 — the referenced thread is not the caller's, or belongs to another project. Both
            # are the SAME non-leaking 404 as a missing one (ADR-0004): grounding a build in
            # another project's files must not even be probeable.
            raise AppApiError(status.HTTP_404_NOT_FOUND, "Conversation not found.") from exc
        except BuildAttachmentError as exc:
            # R3 — an attached file could not be materialized (missing bytes, a magic-byte
            # mismatch, a deck, over the per-file text ceiling). FAIL the start naming the file
            # rather than building as if the file weren't there — the silent-ignore is the exact
            # bug R3 deletes. Nothing was allocated (the resolution runs before the lock and the
            # sandbox), so there is no compensation to run. `str(exc)` is the service's own
            # user-facing copy, never an internal detail (`.claude/rules/security.md`).
            raise AppApiError(
                status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc), code="build_attachment_unusable"
            ) from exc
        except BuildSessionConflictError as exc:
            # A REAL conflict only: `acquire_lock` no longer folds a Redis failure into the
            # same signal, so this 409 always describes a session that genuinely holds the
            # one-per-user lock.
            return _conflict_response(exc)
        except SnapshotUnavailableError as exc:
            # R6 — the restore could not be completed and the snapshot is not confirmed absent,
            # so the manager refused to provision a blank template over the user's work. Their
            # saved version is intact; a retry (or the admin) is the way forward. 503 = try again.
            raise AppApiError(
                status.HTTP_503_SERVICE_UNAVAILABLE, _SANDBOX_UNAVAILABLE_MSG
            ) from exc
        return StartBuildResponse(
            session_id=session.session_id,
            project_id=session.project_id,
            app_id=session.app_id,
            status=session.status,
            preview_url=None,
            created_at=session.created_at,
        )
    raise _coordination_is_gone()


@router.post(
    "/relaunch",
    response_model=RelaunchPreviewResponse,
    dependencies=[RequireCsrf],
    responses=error_responses(
        (403, ErrorEnvelope, "CSRF check failed"),
        AUTH_401,
        (404, ErrorEnvelope, "No saved build to relaunch"),
        (409, ConflictEnvelope, "A build is already running"),
        (422, ErrorEnvelope, "Invalid request body"),
        (503, ErrorEnvelope, "The sandbox or build coordination is temporarily unavailable"),
    ),
)
async def relaunch_preview(
    body: RelaunchPreviewRequest,
    user: CurrentUser,
    db: DbSession,
    sandbox: OptionalSandbox,
    manager: SessionManagerDep,
) -> RelaunchPreviewResponse | JSONResponse:
    """Restore a torn-down app from its snapshot into a fresh, READY sandbox (#43).

    Not a build (Decision 6): no `RunBuildDep`, and the manager path never occupies the
    one-per-user build slot — it registers a ready handle in Redis, releases the lock, and
    returns the live preview synchronously (`wait_ready` blocks until the dev server is up).
    """
    # This route documents "The sandbox or build coordination is temporarily unavailable" AND maps
    # `SandboxError -> 503` below. `SandboxNotConfiguredError` IS a `SandboxError`, so that except
    # would have caught it — one frame too late, because an eager `SandboxDep` raised during
    # dependency solving. `OptionalSandbox` hands it over as `None` instead, so the documented
    # answer is actually reachable.
    if sandbox is None:
        raise AppApiError(status.HTTP_503_SERVICE_UNAVAILABLE, _SANDBOX_UNAVAILABLE_MSG)
    # U3 — same coordination seam as `start_build`, and relaunch needs it at least as badly:
    # it takes the same per-user lock through the same `_holding_user_lock`, so before the
    # split a Redis blip here told the user a build was already running.
    with build_coordination_or_503():
        try:
            relaunched = await manager.relaunch_preview(
                db, user, body.project_id, sandbox, prefer_saved=body.prefer_saved
            )
        except BuildSessionConflictError as exc:
            # A build is currently running for this user — relaunch never pre-empts it (409).
            return _conflict_response(exc)
        except NoSnapshotToRelaunchError as exc:
            # Confirmed-absent (or vanished) snapshot: nothing to relaunch, and there is no
            # blank-template fallback (an empty app is not a preview of the user's work). 404.
            raise AppApiError(
                status.HTTP_404_NOT_FOUND, "No saved build to relaunch. Build the app first."
            ) from exc
        except (SnapshotUnavailableError, SandboxError) as exc:
            # Transient/unknown snapshot state, a restore that failed every attempt, or the dev
            # server not coming ready — the saved version is intact; a retry is the way forward.
            raise AppApiError(
                status.HTTP_503_SERVICE_UNAVAILABLE, _SANDBOX_UNAVAILABLE_MSG
            ) from exc
        return RelaunchPreviewResponse(
            app_id=relaunched.app_id,
            preview_url=relaunched.preview_url,
            # PROVISIONING, not READY, when the app is not serving yet: `status` is the field an
            # older client reads, and telling it READY over a page that has not answered is the
            # dishonesty this whole branch has been unwinding. The URL still ships — see the
            # fail-open note on `relaunch_preview`.
            status=(
                BuildSessionStatus.READY if relaunched.ready else BuildSessionStatus.PROVISIONING
            ),
            restored_from_failed_build=relaunched.restored_from_failed_build,
            ready=relaunched.ready,
        )
    raise _coordination_is_gone()


@router.post(
    "/{session_id}/stop",
    dependencies=[RequireCsrf],
    responses=error_responses(
        (403, ErrorEnvelope, "CSRF check failed"),
        AUTH_401,
        (404, ErrorEnvelope, "Build session not found"),
    ),
)
async def stop_build(
    session_id: uuid.UUID,
    body: StopBuildRequest,
    user: CurrentUser,
    sandbox: SandboxDep,
    manager: SessionManagerDep,
) -> StopBuildResponse:
    session = _owned_or_404(manager, session_id, user.id)
    ended = await manager.stop(session, sandbox, reason=body.reason or "stopped_by_user")
    return StopBuildResponse(session_id=ended.session_id, status=ended.status)


@router.get(
    "/{session_id}",
    responses=error_responses(AUTH_401, (404, ErrorEnvelope, "Build session not found")),
)
async def build_status(
    session_id: uuid.UUID, user: CurrentUser, manager: SessionManagerDep
) -> BuildSessionStatusResponse:
    session = _owned_or_404(manager, session_id, user.id)
    return BuildSessionStatusResponse(
        session_id=session.session_id,
        project_id=session.project_id,
        app_id=session.app_id,
        status=session.status,
        preview_url=session.preview_url,
        last_seq=session.last_seq if session.last_seq > 0 else None,
        created_at=session.created_at,
        updated_at=session.updated_at,
    )


def _parse_last_event_id(raw: str | None) -> int | None:
    """The SSE resume cursor. Absent → None (live-from-now); a non-integer is ignored
    (treated as absent) rather than 4xx'ing a reconnect."""
    if raw is None:
        return None
    try:
        return int(raw)
    except ValueError:
        return None


@router.get(
    "/{session_id}/events",
    responses=error_responses(AUTH_401, (404, ErrorEnvelope, "Build session not found")),
)
async def build_events(
    session_id: uuid.UUID, request: Request, user: CurrentUser, manager: SessionManagerDep
) -> StreamingResponse:
    """The C3 SSE progress feed (cookie-authed, `Last-Event-ID`-resumable, no CSRF). The
    only synchronous pre-stream failure is the 404 ownership check; a brain failure is
    delivered IN-BAND as the terminal FAILED `ended` + `[DONE]` (U5 synthesis)."""
    session = _owned_or_404(manager, session_id, user.id)
    return build_sse_response(session, _parse_last_event_id(request.headers.get("last-event-id")))


# --- lock ops: acquire / renew / release / force-end / heartbeat --------------


async def _renew_and_state(session: BuildSession, user_id: uuid.UUID) -> LockStateResponse:
    """Re-assert the session's lock (extend the TTL if the caller still owns it); a lost
    lock → 409 `build_session_lock_lost`.

    U3 — `renew_lock` is bare by policy (`locks.py`), and its `False` means one specific
    thing: the token no longer matches, i.e. the lock really was lost. A Redis error must
    therefore NOT reach the 409 below (it would end a healthy build on a phantom "lock
    lost") and must not fall through to a 500 either — the seam maps it to the retryable
    503, exactly as on `start`. Redis is resolved LAZILY inside the seam (never an eager Redis
    dependency — the `RedisDep` alias that caused this has been deleted, KTD-9): a Redis-off
    deployment raises `RedisNotConfiguredError` here and the
    trailing `_coordination_is_gone()` returns 503, not the solve-time 500 the eager dep gave."""
    with build_coordination_or_503():
        redis = get_redis()
        if not await renew_lock(redis, user_id, session.lock_token):
            raise AppApiError(
                status.HTTP_409_CONFLICT,
                "The build session lock was lost.",
                code="build_session_lock_lost",
            )
        return LockStateResponse(
            session_id=session.session_id,
            held=True,
            owner_user_id=user_id,
            ttl_seconds=LOCK_TTL_SECONDS,
            expires_at=lock_expires_at(datetime.now(UTC)),
        )
    raise _coordination_is_gone()


@router.post(
    "/{session_id}/lock/acquire",
    response_model=LockStateResponse,  # the union return needs an explicit success model
    dependencies=[RequireCsrf],
    responses=error_responses(
        (403, ErrorEnvelope, "CSRF check failed"),
        AUTH_401,
        (404, ErrorEnvelope, "Build session not found"),
        (409, ConflictEnvelope, "Another session is already active, or the lock was lost"),
        (503, ErrorEnvelope, "Build coordination is temporarily unavailable"),
    ),
)
async def lock_acquire(
    session_id: uuid.UUID, user: CurrentUser, manager: SessionManagerDep
) -> LockStateResponse | JSONResponse:
    session = _owned_or_404(manager, session_id, user.id)
    # C3 §3.1: acquiring while ANOTHER of the caller's sessions holds the one-per-user lock
    # → 409 `build_session_already_active` (carrying that session), distinct from the
    # `lock_lost` 409 `_renew_and_state` raises when the caller's OWN lock has lapsed. Both
    # the 404 above and this in-process conflict check run BEFORE Redis is ever touched
    # (`_renew_and_state` resolves it lazily inside its seam).
    active = manager.active_session_for(user.id)
    if active is not None and active.session_id != session.session_id:
        return _conflict_response(BuildSessionConflictError(active.session_id))
    return await _renew_and_state(session, user.id)


@router.post(
    "/{session_id}/lock/renew",
    dependencies=[RequireCsrf],
    responses=error_responses(
        (403, ErrorEnvelope, "CSRF check failed"),
        AUTH_401,
        (404, ErrorEnvelope, "Build session not found"),
        (409, ErrorEnvelope, "The build session lock was lost"),
        (503, ErrorEnvelope, "Build coordination is temporarily unavailable"),
    ),
)
async def lock_renew(
    session_id: uuid.UUID, user: CurrentUser, manager: SessionManagerDep
) -> LockStateResponse:
    session = _owned_or_404(manager, session_id, user.id)
    return await _renew_and_state(session, user.id)


@router.post(
    "/{session_id}/lock/release",
    dependencies=[RequireCsrf],
    responses=error_responses(
        (403, ErrorEnvelope, "CSRF check failed"),
        AUTH_401,
        (404, ErrorEnvelope, "Build session not found"),
        (503, ErrorEnvelope, "Build coordination is temporarily unavailable"),
    ),
)
async def lock_release(
    session_id: uuid.UUID, user: CurrentUser, manager: SessionManagerDep
) -> LockReleaseResponse:
    session = _owned_or_404(manager, session_id, user.id)  # 404 runs BEFORE Redis is touched
    # U3 — `{"released": true}` is a claim about Redis, so it may only be made when Redis
    # answered. Without the seam a `RedisError` here is a 500; with a guard inside the
    # primitive it would be a 200 asserting a release that never happened (the lie the
    # `locks.py` REDIS-ERROR POLICY calls out by name). 503 is the only honest answer. Redis
    # is resolved LAZILY inside the seam (never an eager Redis dependency — the `RedisDep` alias
    # that caused this has been deleted, KTD-9) so a Redis-off deployment lands on the trailing
    # 503, not a solve-time 500.
    with build_coordination_or_503():
        redis = get_redis()
        await release_lock_as_holder(redis, user.id, session.lock_token)  # idempotent
        return LockReleaseResponse(session_id=session_id, released=True)
    raise _coordination_is_gone()


@router.post(
    "/{session_id}/lock/force-end",
    dependencies=[RequireCsrf],
    responses=error_responses(
        (403, ErrorEnvelope, "CSRF check failed / not the session owner"),
        AUTH_401,
        (404, ErrorEnvelope, "Build session not found"),
    ),
)
async def lock_force_end(
    session_id: uuid.UUID, user: CurrentUser, sandbox: SandboxDep, manager: SessionManagerDep
) -> ForceEndResponse:
    # The ONE route with an owner-asserted 403 (C3): a found-but-foreign session is a 403,
    # not a 404 — force-end is a privileged kill switch, so the caller is told it exists.
    session = manager.get(session_id)
    if session is None:
        raise AppApiError(status.HTTP_404_NOT_FOUND, "Build session not found.")
    if session.user_id != user.id:
        raise AppApiError(
            status.HTTP_403_FORBIDDEN,
            "You do not own this build session.",
            code="build_session_forbidden",
        )
    ended = await manager.force_end(session, sandbox)
    return ForceEndResponse(session_id=ended.session_id, status=ended.status)


@router.post(
    "/{session_id}/heartbeat",
    dependencies=[RequireCsrf],
    responses=error_responses(
        (403, ErrorEnvelope, "CSRF check failed"),
        AUTH_401,
        (404, ErrorEnvelope, "Build session not found"),
        (503, ErrorEnvelope, "Build coordination is temporarily unavailable"),
    ),
)
async def heartbeat(
    session_id: uuid.UUID, user: CurrentUser, manager: SessionManagerDep
) -> HeartbeatResponse:
    session = _owned_or_404(manager, session_id, user.id)  # 404 runs BEFORE Redis is touched
    # U3 — same reasoning as `lock_release`: `{"alive": true}` plus an expiry instant is a
    # claim that the beat landed, and the portal schedules its next beat off it. Redis is
    # resolved LAZILY inside the seam (never an eager Redis dependency — the `RedisDep` alias
    # that caused this has been deleted, KTD-9) so a Redis-off deployment lands on the trailing
    # 503, not a solve-time 500.
    with build_coordination_or_503():
        redis = get_redis()
        expires_at = await write_heartbeat(redis, user.id)
        return HeartbeatResponse(
            session_id=session.session_id,
            alive=True,
            cadence_seconds=HEARTBEAT_CADENCE_SECONDS,
            heartbeat_expires_at=expires_at,
        )
    raise _coordination_is_gone()


# --- the save model (U5b / KTD-5e) ---------------------------------------------------------


class SaveResponse(CamelModel):
    """What a Save returns: the commit the work was saved AT, so the client settles its dirty
    indicator from the write itself rather than going back to ask."""

    app_id: str
    head_sha: str | None = None


class SaveStateResponse(CamelModel):
    """`dirty` is TRI-STATE and the null matters: there is no live workspace to compare, or the
    store could not be read. A client that renders null as clean tells the user their work is
    safe when nothing checked."""

    app_id: str | None = None
    dirty: bool | None = None
    container_head: str | None = None
    saved_head: str | None = None
    # When the platform holds a crash-recovery copy NEWER than the saved version, this is when
    # it was written. Null means there is nothing extra to offer — including every case where
    # we could not prove there is, because promising work we cannot produce is worse than
    # staying quiet. The client turns this into plain language ("your work from 14:47"); it must
    # never surface a bundle, a sha, or the word "recovery" to a citizen developer.
    recoverable_work_at: datetime | None = None


@router.post(
    "/projects/{project_id}/save",
    response_model=SaveResponse,
    dependencies=[RequireCsrf],
    responses=error_responses(
        (403, ErrorEnvelope, "CSRF check failed"),
        AUTH_401,
        (404, ErrorEnvelope, "Project not found"),
        (409, ErrorEnvelope, "There is no live workspace to save"),
        (503, ErrorEnvelope, "The sandbox service is unavailable"),
    ),
)
async def save_project(
    project_id: uuid.UUID,
    user: CurrentUser,
    db: DbSession,
    manager: SessionManagerDep,
    sandbox: OptionalSandbox,
) -> SaveResponse:
    """THE SAVE. The agent commits inside the container as it works; this is the only thing
    that pushes the result to durable storage, and it happens because the user asked.

    409, not 200, when there is no live workspace. A Save that reports success having stored
    nothing is the single worst outcome available here — the user walks away believing their
    work is kept."""
    if sandbox is None:
        raise AppApiError(status.HTTP_503_SERVICE_UNAVAILABLE, _SANDBOX_UNAVAILABLE_MSG)
    await owned_project_or_404(db, user.id, project_id)
    try:
        outcome = await manager.save_project_snapshot(db, user, project_id, sandbox_client=sandbox)
    except NoLiveSandboxError:
        raise AppApiError(
            status.HTTP_409_CONFLICT,
            "Your workspace is no longer running, so there is nothing to save. Send a message "
            "to bring it back — your last saved version is intact.",
        ) from None
    return SaveResponse(app_id=str(outcome.app_id), head_sha=outcome.head_sha)


@router.get(
    "/projects/{project_id}/save-state",
    response_model=SaveStateResponse,
    responses=error_responses(AUTH_401, (404, ErrorEnvelope, "Project not found")),
)
async def save_state(
    project_id: uuid.UUID,
    user: CurrentUser,
    db: DbSession,
    manager: SessionManagerDep,
    sandbox: OptionalSandbox,
) -> SaveStateResponse:
    """Is there unsaved work? Compared by COMMIT — the container's HEAD against the saved
    bundle's — because that is the only comparison that survives a reload, a second tab and a
    process restart, all of which lose in-memory state while the commits stay put."""
    await owned_project_or_404(db, user.id, project_id)
    if sandbox is None:
        return SaveStateResponse()
    state = await manager.project_save_state(db, user, project_id, sandbox_client=sandbox)
    # Asked separately and only when there is an app: `project_save_state` compares the LIVE
    # container against the saved bundle, while this compares two stored bundles — the question
    # that still has an answer once the container is gone, which is exactly when it matters.
    recoverable = (
        await manager.recoverable_work(state.app_id) if state.app_id is not None else None
    )
    return SaveStateResponse(
        app_id=str(state.app_id) if state.app_id else None,
        dirty=state.dirty,
        container_head=state.container_head,
        saved_head=state.saved_head,
        recoverable_work_at=recoverable.written_at if recoverable else None,
    )
