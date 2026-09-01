"""Build-sessions HTTP router — the C3 control surface (Wave 1).

`start` / `stop` / `status` + `force-end` (the one surviving lock op — U28 retired
`acquire`/`renew`/`release`/`heartbeat`, which nothing called) + the superadmin
`internal/reap`, all owner-scoped by `user.id` (ADR-0004): every not-found-or-other-user
case is a non-leaking 404 EXCEPT the one owner-asserted 403 on `force-end` (C3). The
mutating POSTs carry the reusable `RequireCsrf` dependency (KTD-4); the `status` GET and
the GET-SSE progress feed (`sse.py`, `Last-Event-ID`-resumable) are exempt.

U13 adds one inbound route that is not a control op at all — `projects/{project_id}/client-error`,
where the app's own in-browser error reporter's findings arrive by way of the portal. It follows
the same pattern as everything else here (CSRF, `CurrentUser`, owned-or-404), and the reason it
lives in THIS router rather than under `apps/` is that its only consumer is the build harness's
health verdict.
"""

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
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
    BuildSessionStatus,
    BuildSessionStatusResponse,
    ClientErrorReportRequest,
    ClientErrorReportResponse,
    CompileStateResponse,
    ForceEndResponse,
    ParkedTree,
    ParkedTreesResponse,
    PreviewLifeState,
    PromoteParkedRequest,
    PromoteParkedResponse,
    RelaunchPreviewRequest,
    RelaunchPreviewResponse,
    StartBuildRequest,
    StartBuildResponse,
    StopBuildRequest,
    StopBuildResponse,
    WorkspaceCheckResponse,
)
from src.api.v1.build_sessions.sse import build_sse_response
from src.api.v1.live_build import ReclaimBlockedError, reclaim_blocked_response
from src.core.errors import AppApiError
from src.core.integrity_types import WorkspaceState
from src.db.models.app_registry import AppRegistry
from src.schemas import AUTH_401, CamelModel, ErrorEnvelope, error_responses
from src.services.audit.log import append_audit
from src.services.build_sessions import (
    BuildAttachmentError,
    BuildSession,
    BuildSessionConflictError,
    ConversationNotFoundError,
    NoLiveSandboxError,
    NoSnapshotToRelaunchError,
    SandboxReclaimBlockedError,
    SandboxUnreachableError,
    SessionManager,
    SnapshotUnavailableError,
    app_name_for,
    sweep_all,
)
from src.services.build_sessions.snapshot import (
    ParkedTreeNotOursError,
    list_parked_trees,
    promote_parked,
)
from src.services.orchestrator.client_errors import (
    park_client_error,
)
from src.services.projects.resolve import owned_project_or_404
from src.services.redis import (
    build_coordination_or_503,
    coordination_is_gone,
    get_redis,
)
from src.services.sandbox import SandboxError
from src.services.sandbox.base import CompileState

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
    """The inner error object of a build-session 409 (`start` or `relaunch` already-active):
    the plain `{message, code}` envelope PLUS the existing session's id, which
    `_conflict_response` carries but `ErrorEnvelope` omits."""

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


class BuildConflictEnvelope(CamelModel):
    """The 409 for a route that can conflict two ways: a build already running for this user
    (`sessionId`), or another project holding the one workspace with unsaved work
    (`projectId`/`projectName`/`dirty`). `code` discriminates — `build_session_already_active`
    vs `sandbox_reclaim_blocked` — and a client must branch on it, since only the second has a
    remedy the user can act on."""

    error: _ConflictError | ReclaimBlockedError


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
    requires Redis.

    Delegates to the shared `coordination_is_gone` so this module's seven call sites and
    `admin.reconcile_sandboxes` cannot drift into two subtly different apologies — the same
    reason `BUILD_COORDINATION_UNAVAILABLE_MSG` is a constant."""
    return coordination_is_gone()


# --- internal/reap (registered FIRST so `internal` is never parsed as a session id) ---


@router.post(
    "/internal/apps/{app_id}/parked",
    dependencies=[RequireCsrf],
    responses=error_responses(AUTH_401, (403, ErrorEnvelope, "CSRF check failed")),
)
async def parked_trees(
    app_id: uuid.UUID, admin: CurrentSuperadmin, db: DbSession
) -> ParkedTreesResponse:
    """The trees this plan set aside for one app — U2 quarantines and U3 diverts (U25).

    WITHOUT THIS THEY ARE WRITE-ONLY: no reader, no retention, no runbook. In a false-`REVERTED`
    case those objects hold the only copy of a citizen's newest work, and this plan names exactly
    that shape as a defect elsewhere, so it must not reproduce it.

    `CurrentSuperadmin`, and mounted beside `internal/reap` deliberately: this is an operator
    action in the same category as the reaper, not the user-facing viewing surface the plan's
    Scope Boundaries exclude. Audited, like every gated action (ADR-0005)."""
    trees = await list_parked_trees(app_id)
    await append_audit(
        db,
        actor_id=admin.id,
        action="harness:parked:list",
        resource_type="app",
        resource_id=str(app_id),
        detail={"found": len(trees)},
    )
    await db.commit()
    return ParkedTreesResponse(
        trees=[
            ParkedTree(
                key=tree.key,
                kind=tree.kind,
                head_sha=tree.head_sha,
                size_bytes=tree.size_bytes,
                taken_at=tree.taken_at,
            )
            for tree in trees
        ]
    )


@router.post(
    "/internal/apps/{app_id}/promote",
    dependencies=[RequireCsrf],
    responses=error_responses(AUTH_401, (403, ErrorEnvelope, "CSRF check failed")),
)
async def promote_parked_tree(
    app_id: uuid.UUID,
    body: PromoteParkedRequest,
    admin: CurrentSuperadmin,
    db: DbSession,
) -> PromoteParkedResponse:
    """Put one parked tree back into the recovery slot (U25).

    THROUGH U3'S GUARD, never around it. A promotion whose tree is not a descendant of what the
    slot already holds is REFUSED and alarmed rather than forced — an operator recovering the
    wrong tree over somebody's newest work is the precise failure this plan exists to stop, and
    "an operator asked for it" is not evidence that the tree is the right one.

    The key is named explicitly rather than "the newest": a request that cannot say what it means
    is one that can be misread."""
    try:
        outcome = await promote_parked(app_id, key=body.key)
    except ParkedTreeNotOursError as exc:
        # A 400, not a 500: pasting the wrong key is an ordinary operator mistake, and rendering
        # it as an internal fault sends them looking for a broken store instead of at the key.
        raise AppApiError(
            status.HTTP_400_BAD_REQUEST, "That parked tree belongs to a different app."
        ) from exc
    await append_audit(
        db,
        actor_id=admin.id,
        action="harness:parked:promote",
        resource_type="app",
        resource_id=str(app_id),
        detail={"key": body.key, "promoted": outcome.promoted},
    )
    await db.commit()
    return PromoteParkedResponse(promoted=outcome.promoted, detail=outcome.detail)


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
    # opportunistic seam is start()) — nothing evicts them on a timer. Narrowed 2026-08-11:
    # this said "no background task", which now reads as a claim about the repo. The repo HAS
    # scheduled work (ADR-0011); what it has no scheduled evictor for is THIS in-process map,
    # which is per-process state a shared scheduler could not reach anyway.
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
        (
            409,
            BuildConflictEnvelope,
            "A build is already running, or another project holds the workspace with unsaved work",
        ),
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
        except SandboxReclaimBlockedError as exc:
            # #83 — another project holds the one slot and has unsaved work. Relaunch used to
            # take it anyway and destroy that work silently; now the user decides.
            return reclaim_blocked_response(exc)
        except NoSnapshotToRelaunchError as exc:
            # Confirmed-absent (or vanished) snapshot: nothing to relaunch, and there is no
            # blank-template fallback (an empty app is not a preview of the user's work). 404.
            raise AppApiError(
                status.HTTP_404_NOT_FOUND, "No saved build to relaunch. Build the app first."
            ) from exc
        except (SnapshotUnavailableError, SandboxUnreachableError, SandboxError) as exc:
            # Transient/unknown snapshot state, a restore that failed every attempt, or the dev
            # server not coming ready — the saved version is intact; a retry is the way forward.
            #
            # `SandboxUnreachableError` IS THIS ANSWER, and it is named here rather than left to
            # fall through as a 500. The attach fork now refuses on it instead of restoring: the
            # registry says a container is live, the attach could not confirm anything, and the
            # honest reply is "we could not tell" — which is precisely what this arm already
            # says. It is NOT a `SandboxError` (it is a `NoLiveSandboxError` subclass), so
            # listing it is the only way it reaches this message rather than an unhandled 500.
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


# --- lock ops: force-end (the operator/owner kill switch) ---------------------
#
# U28 retired `acquire` / `renew` / `release` / `heartbeat`, along with their shared
# `_renew_and_state` helper: the portal's keep-alive loop that was their only caller was
# itself deleted back in U13 (`buildSessionApi.ts` says so), and a route with no caller is
# not neutral — it reads as a supported way to hold the lock, and the next person needing
# one would have wired the loop straight back. What holds a turn open now is the R10
# wall-clock lease the SERVER renews (U12), legible to a sweep in another process, which a
# browser timer never was. `force-end` is the one lock op still reachable from the UI (fed
# by relaunch's 409) and it CARRIES NO REQUEST BODY, same as its four retired neighbours —
# the surviving proof that this section's routes take none.


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


# --- the save model (U5b / KTD-5e) ---------------------------------------------------------


class SaveResponse(CamelModel):
    """What a Save returns: the commit the work was saved AT, so the client settles its dirty
    indicator from the write itself rather than going back to ask."""

    app_id: str
    head_sha: str | None = None


class PreviewStateResponse(CamelModel):
    """FOUR STATES AND AN UNKNOWN, not one boolean (C3 §8.3).

    The old shape was `{alive, previewUrl}`, and its docstring argued that `alive` needed no
    unknown arm because the registry answers without a container round trip. That was wrong in
    the way that costs users: the registry read can FAIL, and `alive=false` said "your preview
    is gone" for a question nobody had managed to ask. It also flattened three genuinely
    different ordinary states — never built, asleep, another project took the slot — into the
    same shrug, so the pane could only ever offer the same one sentence back.

    `previewUrl` is echoed so a tab that reconnects can re-frame without a second call."""

    state: PreviewLifeState
    # RETAINED, strictly `state == alive`. A browser tab loaded before this change is still
    # polling `alive`, and a tab reading a missing field as false would paint "gone" over a
    # live preview for the whole rollout window. It cannot express UNKNOWN — new clients
    # branch on `state`, and this field exists only so old ones keep working.
    alive: bool
    preview_url: str | None = None
    # SLOT_TAKEN only. Null when the live container matches no app this user owns (a ghost) —
    # naming the wrong project in a sentence about someone's work is worse than naming none.
    occupying_project_id: uuid.UUID | None = None
    occupying_project_name: str | None = None
    # TRI-STATE like `SaveStateResponse.dirty`, and for the identical reason: `null` is NO
    # CLAIM, never "no". Two ways to reach it, one instruction to the client — the object store
    # was unreachable, or `state` is `alive` and the poll declined to spend a Blob round trip on
    # a question no surface asks about a running app (C3 §8.3's budget: none on the hot path).
    # Answered WITHOUT a container, which is the whole point — it is the one restore signal
    # that survives the container being reclaimed.
    restorable: bool | None = None


class StopActiveBuildResponse(CamelModel):
    """`stopped` says whether there was actually something running to stop. False is a success:
    the project is settled, which is the state the caller needed before saving or releasing.

    Says nothing about whether the stop SUCCEEDED in freeing the slot, because it cannot — the
    wait is bounded, and a wedged container can outlast it. The next step's refusal is the
    authority on that, which is why the save and release guards stay in place rather than
    trusting this call to have done its job."""

    stopped: bool


class ReleaseResponse(CamelModel):
    """`released` says whether there was actually a container to give up. False is a success —
    the workspace was already gone, which is the state the caller wanted.

    That reading is only safe because the service reaps with `strict=True`: a teardown that
    FAILED leaves as a `SandboxError` and becomes a 503, so it never arrives here wearing the
    same `false` as "nothing to release". Any future caller that drops strict re-collapses the
    two, and the client believes a slot was freed while the container is still standing."""

    released: bool


class SaveStateResponse(CamelModel):
    """`dirty` is TRI-STATE and the null matters: there is no live workspace to compare, or the
    store could not be read. A client that renders null as clean tells the user their work is
    safe when nothing checked."""

    app_id: str | None = None
    dirty: bool | None = None
    container_head: str | None = None
    # When the platform last autosaved (#83 follow-up). Lets the UI offer unsaved work back
    # after a reclaim instead of quietly forgetting it. Never a substitute for the user's own
    # save — `savedHead` is still the only thing a relaunch restores.
    recovery_at: datetime | None = None
    saved_head: str | None = None


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
    work is kept.

    409 as well while the agent is still writing, which is the SECOND worst: that save
    succeeded, and stored a tree caught mid-edit as the version a Relaunch would restore."""
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
    except BuildSessionConflictError:
        raise AppApiError(
            status.HTTP_409_CONFLICT,
            "Your app is still being built. Saving now would store a half-finished version — "
            "wait for it to finish, or stop it first.",
        ) from None
    return SaveResponse(app_id=str(outcome.app_id), head_sha=outcome.head_sha)


@router.post(
    "/projects/{project_id}/stop-active-build",
    response_model=StopActiveBuildResponse,
    dependencies=[RequireCsrf],
    responses=error_responses(
        (403, ErrorEnvelope, "CSRF check failed"),
        AUTH_401,
        (404, ErrorEnvelope, "Project not found"),
        # Only the sandbox-unconfigured arm — this route does not consult Redis, so there is no
        # coordination-unavailable case to document. See the docstring.
        (503, ErrorEnvelope, "The sandbox service is unavailable"),
    ),
)
async def stop_active_build(
    project_id: uuid.UUID,
    user: CurrentUser,
    db: DbSession,
    manager: SessionManagerDep,
    sandbox: OptionalSandbox,
) -> StopActiveBuildResponse:
    """Stop what the agent is doing in this project, and wait for it to settle.

    THE FIRST OF THREE, and the only new one: stop → save → release. It exists because the
    other two both refuse while a session is live, which used to make the reclaim dialog a dead
    end — a user switching away from a building project was offered Save and Switch, and the
    server declined both.

    Its own route rather than a flag on `release`, deliberately. Stopping is destructive to
    work in progress and the user is choosing it explicitly; hiding it inside a release would
    make "give up the workspace" sometimes also mean "kill the agent", which is exactly the
    kind of silent extra consequence this whole flow exists to remove. Each step stays one verb,
    each keeps its own refusal, so the ORDER is enforced by the guards rather than by a client
    remembering to call them in sequence.

    `stopped: false` means nothing was running — a success the caller proceeds on, not a miss.
    Deliberately no 409: asking a settled project to stop is already the state you wanted.

    NO `build_coordination_or_503` SEAM, unlike `release` and `save` beside it, and the
    asymmetry is deliberate rather than an omission. Those two ask REDIS what is live — the
    registry hash is their source of truth — so with no coordination subsystem they can decide
    nothing and must refuse. This route asks a question Redis cannot answer and does not need
    to: "is THIS PROCESS running work for this user?" lives in `_active_by_user`, and the stop
    itself is a `task.cancel()` plus an await. Wrapping it produced a trailing
    `_coordination_is_gone()` that could never execute (verified: the body completes and
    returns 200 with the Redis singleton unset), which is the dead-arm shape this PR's review
    caught elsewhere. Worse, it would have been wrong on the path that matters — a live
    in-process build during a Redis outage is exactly when a user still needs to stop it."""
    if sandbox is None:
        raise AppApiError(status.HTTP_503_SERVICE_UNAVAILABLE, _SANDBOX_UNAVAILABLE_MSG)
    await owned_project_or_404(db, user.id, project_id)
    stopped = await manager.stop_active_work(db, user, project_id, sandbox_client=sandbox)
    return StopActiveBuildResponse(stopped=stopped)


@router.post(
    "/projects/{project_id}/release",
    response_model=ReleaseResponse,
    dependencies=[RequireCsrf],
    responses=error_responses(
        (403, ErrorEnvelope, "CSRF check failed"),
        AUTH_401,
        (404, ErrorEnvelope, "Project not found"),
        (409, ConflictEnvelope, "A build is running in this workspace"),
        (503, ErrorEnvelope, "The sandbox or build coordination is temporarily unavailable"),
    ),
)
async def release_project(
    project_id: uuid.UUID,
    user: CurrentUser,
    db: DbSession,
    manager: SessionManagerDep,
    sandbox: OptionalSandbox,
) -> ReleaseResponse | JSONResponse:
    """Give up this project's workspace so another project can have the slot (#83).

    The counterpart to the reclaim refusal, and the only route that destroys a container on
    purpose. The start path used to do this silently, inside the request for a DIFFERENT
    project, taking any unsaved work with it; here it is the user's own action, taken after
    being told what it costs and offered a Save first.

    `released: false` is a success, not a miss: the workspace was already gone, which is the
    state the caller asked for. Refuses with 409 while a build is actually running — an
    in-process session owns its container, and pulling it out from under one is the strand this
    module exists to prevent."""
    if sandbox is None:
        raise AppApiError(status.HTTP_503_SERVICE_UNAVAILABLE, _SANDBOX_UNAVAILABLE_MSG)
    await owned_project_or_404(db, user.id, project_id)
    with build_coordination_or_503():
        try:
            released = await manager.release_project_sandbox(
                db, user, project_id, sandbox_client=sandbox
            )
        except BuildSessionConflictError as exc:
            return _conflict_response(exc)
        except SandboxError as exc:
            # The container would not go away. Say so rather than reporting a release that did
            # not happen — the caller is about to start something that needs the slot.
            raise AppApiError(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "Could not close that workspace just now. Please try again.",
            ) from exc
        return ReleaseResponse(released=released)
    raise _coordination_is_gone()


@router.get(
    "/projects/{project_id}/preview-state",
    response_model=PreviewStateResponse,
    responses=error_responses(AUTH_401, (404, ErrorEnvelope, "Project not found")),
)
async def preview_state(
    project_id: uuid.UUID,
    user: CurrentUser,
    db: DbSession,
    manager: SessionManagerDep,
) -> PreviewStateResponse:
    """Is the preview this tab is framing still real — and if not, WHY? (#83, C3 §8.3.)

    A framed preview that has been reclaimed looks EXACTLY like a working app — the last render
    stays on screen, the iframe reports nothing, and a cross-origin pane cannot read a status
    code. Once a build ends the tab holds no SSE and no timer, and the teardown happens inside a
    DIFFERENT project's request, so there is nothing to push down. The tab has to ask.

    Deliberately NOT `save-state`, which the client could otherwise have polled: that runs two
    `git` execs inside the container per call, and its `dirty=null` conflates three unrelated
    causes. This route's budget is frozen in C3 §8.3 — one round trip to the coordination store
    (two commands, pipelined: the registry hash and the U13 starting marker), at most two
    user-scoped rows, at most two object-store HEADs, and NO container call of any kind.

    Answers about THIS project only. A container serving a different app is `slot_taken` here,
    named where we can name it: the one-per-user registry means somebody else's container is
    exactly when yours is asleep, and the builder deserves to be told which of their own
    projects is standing in the way rather than that their app disappeared. As of U13, a start
    already in flight for this project — this tab's own press, another tab's, or a chat
    message that just started one — answers `starting` rather than the stale `asleep` a second
    press used to invite."""
    await owned_project_or_404(db, user.id, project_id)
    state = await manager.project_preview_state(db, user, project_id)
    return PreviewStateResponse(
        state=state.state,
        alive=state.alive,
        preview_url=state.preview_url,
        occupying_project_id=state.occupying_project_id,
        occupying_project_name=state.occupying_project_name,
        restorable=state.restorable,
    )


@router.post(
    "/projects/{project_id}/workspace-check",
    response_model=WorkspaceCheckResponse,
    dependencies=[RequireCsrf],
    responses=error_responses(AUTH_401, (404, ErrorEnvelope, "Project not found")),
)
async def workspace_check(
    project_id: uuid.UUID,
    user: CurrentUser,
    db: DbSession,
    manager: SessionManagerDep,
    sandbox: OptionalSandbox,
) -> WorkspaceCheckResponse:
    """Is the app this tab is framing still the citizen's app? (U4, R4/R7.)

    THE TURN MAY NEVER COME. Every other integrity check in this system runs at the start of a
    turn, which catches every reversion between one message and the next — and catches nothing at
    all for someone who is reading, or in another tab, or at lunch. The "Build complete — your app
    is live below" claim above their preview goes on being displayed for as long as the page stays
    open. That is 2026-08-18 with the clock running, and it is what this route exists to end.

    A POST, WITH CSRF, because it is not a free read: it costs a container exec and it can raise
    an operational alarm. It follows this file's pattern exactly — `RequireCsrf`, `CurrentUser`,
    owned-or-404 — and its path is in `_MUTATING_POSTS` so the CSRF matrix actually exercises it.

    DELIBERATELY NOT FOLDED INTO `preview-state`, whose budget is frozen in C3 §8.3 at NO
    container call of any kind because a browser tab drives it on a 45-second timer. The client
    calls this one only when preview-state already reports alive AND a completion claim is
    standing, so the two never both fire on a dark pane — and the manager rate-limits per app on
    top of that, so a tab left open overnight cannot spin the container.

    IT ONLY REPORTS. Nothing is restored and nothing is destroyed here: the restore belongs to the
    next turn, where the citizen is present, has been told, and can confirm. Recovering somebody's
    app behind their back while they are looking at another tab is not a kindness."""
    await owned_project_or_404(db, user.id, project_id)
    if sandbox is None:
        # No sandbox service configured (KTD-2). Nothing can be checked and nothing is claimed —
        # `UNREADABLE` is the honest answer, and the client holds its claim on it.
        return WorkspaceCheckResponse(state=WorkspaceState.UNREADABLE, reverted=False)
    state = await manager.project_workspace_check(db, user, project_id, sandbox_client=sandbox)
    return WorkspaceCheckResponse(state=state, reverted=state is WorkspaceState.REVERTED)


@router.get(
    "/projects/{project_id}/compile-state",
    response_model=CompileStateResponse,
    responses=error_responses(AUTH_401, (404, ErrorEnvelope, "Project not found")),
)
async def compile_state(
    project_id: uuid.UUID,
    user: CurrentUser,
    db: DbSession,
    manager: SessionManagerDep,
    sandbox: OptionalSandbox,
) -> CompileStateResponse:
    """Is the app compiling, compiled, or broken — asked by a tab with NO LIVE TURN (R17/R18).

    THE RELOAD HOLE THIS CLOSES. The compile signal reaches the portal as a frame on the turn
    stream, so its producer stops the moment the turn does. Reload the page after a turn that
    ended red and the pane comes up with no signal at all: it initialises uncovered, and the
    citizen is shown the framework's full-screen error screen underneath a live-preview label.
    That is the exact failure the cover exists to prevent, reachable by pressing F5.

    DELIBERATELY NOT FOLDED INTO `preview-state`, whose cost budget is frozen in C3 §8.3 at NO
    container call of any kind — it is a browser tab on a 45-second timer and that ceiling is
    the contract. This is its own route, and the client only calls it when it is already framing
    a preview and no turn is running, so the two never both fire on a dark pane.

    CHEAP BY CONSTRUCTION on the answer side: `/dev/compile` reads an in-memory value in the
    container and never touches the dev server. The expensive part is the attach, which is why
    the no-live-container case short-circuits before it.

    `unknown` for everything unanswerable, and the caller HOLDS its cover on it. Absent must
    never read as clean — that is the whole contract of this signal."""
    await owned_project_or_404(db, user.id, project_id)
    if sandbox is None:
        return CompileStateResponse(state=CompileState.UNKNOWN)
    state = await manager.project_compile_state(db, user, project_id, sandbox_client=sandbox)
    return CompileStateResponse(state=state)


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
    return SaveStateResponse(
        app_id=str(state.app_id) if state.app_id else None,
        dirty=state.dirty,
        container_head=state.container_head,
        saved_head=state.saved_head,
        recovery_at=state.recovery_at,
    )


# --- the app's own client-error report (U13, R17 runtime half) ----------------


@router.post(
    "/projects/{project_id}/client-error",
    status_code=status.HTTP_202_ACCEPTED,
    response_model=ClientErrorReportResponse,
    dependencies=[RequireCsrf],
    responses=error_responses(
        (403, ErrorEnvelope, "CSRF check failed"),
        AUTH_401,
        (404, ErrorEnvelope, "Project not found"),
    ),
)
async def report_client_error(
    project_id: uuid.UUID,
    body: ClientErrorReportRequest,
    user: CurrentUser,
    db: DbSession,
) -> ClientErrorReportResponse:
    """The app crashed in the browser; the platform is being told (R17 runtime half, AE11).

    THE MISSING WITNESS. Every health signal the harness has runs against the SERVER — the
    type-check, the dev-server log tail, `/dev/status`, the root-route warm — and a Next app can
    satisfy all four and still throw before it paints anything. The only observer of that failure
    is the browser, and the generated app has been relaying its own `window.onerror` /
    `unhandledrejection` / `console.*` captures to the framing portal since Stage 0 with nothing
    on the receiving end (`sandbox/template/components/bial/error-capture.tsx` says so in its own
    header). This route is the receiving end: the portal validates the frame's origin and forwards
    what it caught, and the report is parked for the next `selfheal.verify` to collect, where it
    makes the verdict not-green. That is the whole user-visible consequence — the completion claim
    does not appear — because the report's own text goes to the AGENT and to nobody else.

    202, not 200: nothing has happened yet when this returns. The report is parked, and whether it
    changes anything depends on a verify that has not run.

    `recorded: false` is still a 202. The one thing that can be refused here is a report the store
    had no room for, and that is not a client error to raise — it is a fact about volume the
    caller deserves to be told (see `ClientErrorReportResponse`).

    OWNED-OR-404 on the app, like every other route in this file: a cross-user app id and a
    missing one are the same non-leaking answer (ADR-0004). Not 403 — telling a caller "that app
    exists but is not yours" is exactly the probe the 404 exists to refuse, and there is no
    force-end-style owner assertion here to make an exception for.

    NO SESSION, NO REDIS, NO SANDBOX. Deliberately PROJECT-scoped rather than session-scoped: the
    crash arrives from a framed preview, and a preview outlives its build session by design
    (relaunch registers none at all). A route that needed a live session would be unable to
    receive a report about an app the user is simply looking at, which is most of them. Project
    rather than app because a project has exactly one app (KTD-6) and the project is what the
    caller holds — the framing pane is addressed by project everywhere else in the portal, and an
    ingest that needed an app id would have to buy one with an extra round trip per crash.

    READ-ONLY on the way to the app. `resolve_app_for_project` is the usual accessor and it is
    the wrong one here: it UPSERTS, so a stray report against a project nobody has ever built
    would MINT an app row. An app that does not exist is a 404 — there is no build for a browser
    crash to be about."""
    await owned_project_or_404(db, user.id, project_id)
    # Owner AND project in the predicate, not just project: the scope is the isolation boundary,
    # not a nicety (ADR-0004). `owned_project_or_404` above already refused another user's
    # project, and the second predicate is what keeps that true if this query is ever moved.
    app_id = (
        await db.execute(
            sa.select(AppRegistry.id).where(
                AppRegistry.project_id == project_id, AppRegistry.user_id == user.id
            )
        )
    ).scalar_one_or_none()
    if app_id is None:
        # Same non-leaking answer as an unowned project: "nothing here to report against".
        raise AppApiError(status.HTTP_404_NOT_FOUND, "Project not found.")
    # `app_name_for` is the same forward mapping the sandbox is NAMED by, so the key written here
    # is exactly the `SandboxHandle.app_name` the verify reads back. It is deliberately not
    # reversed anywhere — the mapping is lossy (28 of 32 hex chars) and only ever computed
    # forwards, which is the property `inventory.py` relies on for the same reason.
    recorded = park_client_error(
        app_name_for(app_id), source=body.source, title=body.title, stack=body.stack
    )
    return ClientErrorReportResponse(recorded=recorded)
