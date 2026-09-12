"""Build-sessions HTTP router — the build-session control surface.

WHY THIS EXISTS

`stop` / `status` + the SSE feed + `relaunch` + the project-scoped save/preview/stop ops + the
superadmin `internal/reap`, all owner-scoped by `user.id`: every not-found-or-other-user case
is a non-leaking 404. The mutating POSTs carry the reusable `RequireCsrf` dependency; the
`status` GET and the GET-SSE progress feed (`sse.py`, `Last-Event-ID`-resumable) are exempt.

THERE IS NO `start` ANY MORE, and the three `{session_id}` routes below serve HISTORICAL sessions
only. The bare `POST` on this collection — the start route — lost its browser client and was
deleted here with the whole harness behind it; the last lock op, `lock/force-end`, went with it
(it had had no UI since the block banner's Force-end button was removed, which both
`buildSessionApi.ts` and `useBuildSession.ts` recorded in their own comments). The only remaining
producer of a session id the portal can reach is a `build_started` transcript row written before
that deletion — those rows are permanent, so `status`/`stop`/`events` stay as their reader. A
build now runs as an ordinary Write chat turn, which registers its workspace through
`SessionManager.ensure_sandbox` and never serialises a session id at all.

One inbound route here is not a control op at all — `projects/{project_id}/client-error`,
where the app's own in-browser error reporter's findings arrive by way of the portal. It
follows the same pattern as everything else here (CSRF, `CurrentUser`, owned-or-404), and
it lives in THIS router rather than under `apps/` because its only consumer is the build
harness's health verdict."""

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
    SandboxDep,
    SessionManagerDep,
)
from src.api.v1.build_sessions.schemas import (
    BuildSessionStatus,
    BuildSessionStatusResponse,
    ClientErrorReportRequest,
    ClientErrorReportResponse,
    CompileStateResponse,
    ParkedTree,
    ParkedTreesResponse,
    PreviewLifeState,
    PromoteParkedRequest,
    PromoteParkedResponse,
    RelaunchPreviewRequest,
    RelaunchPreviewResponse,
    SharedPreviewResponse,
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
    BuildSession,
    BuildSessionConflictError,
    NoLiveSandboxError,
    NoSnapshotToRelaunchError,
    SandboxReclaimBlockedError,
    SandboxUnreachableError,
    SessionManager,
    SharedProjectHasNoAppError,
    SnapshotUnavailableError,
    StopOutcome,
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
from src.services.projects.resolve import (
    ProjectAccess,
    owned_project_or_404,
    resolve_project_access,
)
from src.services.redis import (
    build_coordination_or_503,
    coordination_is_gone,
    get_redis,
)
from src.services.sandbox import SandboxError
from src.services.sandbox.base import CompileState

router = APIRouter(prefix="/build-sessions", tags=["build_sessions"])

# The user-approved wording, VERBATIM. Note there is deliberately no trailing period,
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
    """The inner error object of a build-session 409 (`relaunch` already-active): the plain
    `{message, code}` envelope PLUS the existing session's id, which `_conflict_response`
    carries but `ErrorEnvelope` omits.

    NOTHING READS `sessionId`. It was the start route's contribution to this shape and it
    survives only because `relaunch_preview` raises the same error; the portal's
    `existingSessionIdOf` has no consumer and its one catch site branches on the error CODE.
    Retiring the field is a contract change and is deliberately left for one."""

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
    """Load a session scoped to its owner, or fail closed with a 404."""
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
        error["sessionId"] = str(exc.session_id)
    return JSONResponse(status_code=status.HTTP_409_CONFLICT, content={"error": error})


def _coordination_is_gone() -> AppApiError:
    """The 503 for the ONE arm `build_coordination_or_503` deliberately does not raise on:
    `RedisNotConfiguredError`, whose contract is "proceed".

    Delegates to the shared `coordination_is_gone` so this module's seven call sites and
    `admin.reconcile_sandboxes` cannot drift into two subtly different apologies — the same
    reason `BUILD_COORDINATION_UNAVAILABLE_MSG` is a constant."""
    # "Proceed" is right for a GATE — submit asks "is a build live?", and with no Redis the
    # answer is a certain no, so submit proceeds. It is wrong for every route below, where Redis
    # is not consulted about the operation, it IS the operation: with no coordination subsystem
    # there is nowhere to take a lock, seed a heartbeat, or register a session. So each `with`
    # block returns from inside itself, and reaching the line AFTER it means the helper skipped
    # the body — which for these routes is a refusal, not a pass.
    #
    # Same user-facing copy: from the caller's side "not configured yet" and "not answering" are
    # the same unavailable service, and the difference is an internal detail the caller is not
    # owed. Unreachable in production, where the settings gate requires Redis.
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
    """The trees set aside for one app — quarantined or diverted.

    WITHOUT THIS THEY ARE WRITE-ONLY: no reader, no retention, no runbook. In a false-`REVERTED`
    case those objects hold the only copy of a citizen's newest work.

    `CurrentSuperadmin`, and mounted beside `internal/reap` deliberately: this is an operator
    action in the same category as the reaper, not a user-facing viewing surface. Audited,
    like every gated action."""
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
    """Put one parked tree back into the recovery slot.

    THROUGH THE DIVERSION'S OWN GUARD, never around it. A promotion whose tree is not a
    descendant of what the slot already holds is REFUSED and alarmed rather than forced. The
    key is named explicitly rather than "the newest": a request that cannot say what it means
    is one that can be misread."""
    # An operator recovering the wrong tree over somebody's newest work is the precise failure
    # the guard exists to stop, and "an operator asked for it" is not evidence that the tree is
    # the right one.
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
    """Operator-triggered full reconciliation sweep — `CurrentSuperadmin`-guarded, CSRF'd,
    audited, idempotent, concurrency-safe. The by-hand door onto the same sweep the scheduled
    pass runs; this route itself is cookie-only, so nothing machine-authed can drive it."""
    # Retention sweep of ended in-process sessions rides the same operator path (the other
    # opportunistic seam is start()) — nothing evicts them on a timer, and nothing scheduled
    # could: this map is per-process state another process cannot reach.
    manager.evict_ended_sessions()
    # The sweep walks the registry namespace with bare primitives, so an outage here is a 503
    # to the operator rather than an opaque 500. The audit row is deliberately inside: a sweep
    # that never ran is not an action worth recording. Redis is resolved LAZILY inside the seam,
    # so `get_redis()` raises here and the trailing `_coordination_is_gone()` answers.
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


# --- control ops: relaunch / stop / status ------------------------------------


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
    """Restore a torn-down app from its snapshot into a fresh, READY sandbox.

    Not a build (Decision 6): it runs no agent at all, and the manager path never occupies the
    one-per-user build slot — it registers a ready handle in Redis, releases the lock, and
    returns the live preview synchronously (`wait_ready` blocks until the dev server is up).
    """
    if sandbox is None:
        raise AppApiError(status.HTTP_503_SERVICE_UNAVAILABLE, _SANDBOX_UNAVAILABLE_MSG)
    # The coordination seam the deleted `start_build` also ran inside, and relaunch needs it
    # at least as badly: it takes the same per-user lock through the same `_holding_user_lock`,
    # so before the split a Redis blip here told the user a build was already running.
    with build_coordination_or_503():
        try:
            relaunched = await manager.relaunch_preview(
                db, user, body.project_id, sandbox, prefer_saved=body.prefer_saved
            )
        except BuildSessionConflictError as exc:
            # A build is currently running for this user — relaunch never pre-empts it (409).
            return _conflict_response(exc)
        except SandboxReclaimBlockedError as exc:
            # Another project holds the one slot and has unsaved work.
            return reclaim_blocked_response(exc)
        except NoSnapshotToRelaunchError as exc:
            # Confirmed-absent (or vanished) snapshot: nothing to relaunch, and there is no
            # blank-template fallback (an empty app is not a preview of the user's work). 404.
            #
            # CODED, because this route answers 404 for TWO unrelated reasons and a client has to
            # tell them apart. `owned_project_or_404` fails a deleted or someone else's project
            # with the same status; the rail treats "nothing saved to bring back" as a normal
            # first message and opens the chat anyway, which for the other 404 would
            # open a chat that dies a beat later instead of reporting the failure. Only this one
            # carries `no_saved_build`, so the rail's arm can be exact — the same reason
            # `sandbox_reclaim_blocked` names itself rather than letting a client match prose.
            raise AppApiError(
                status.HTTP_404_NOT_FOUND,
                "No saved build to relaunch. Build the app first.",
                code="no_saved_build",
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
    """The build-session SSE progress feed (cookie-authed, `Last-Event-ID`-resumable, no CSRF). The
    only synchronous pre-stream failure is the 404 ownership check; a brain failure is
    delivered IN-BAND as a synthesized terminal FAILED `ended` + `[DONE]`."""
    session = _owned_or_404(manager, session_id, user.id)
    return build_sse_response(session, _parse_last_event_id(request.headers.get("last-event-id")))


# --- THERE ARE NO LOCK OPS LEFT ----------------------------------------------
#
# `acquire` / `renew` / `release` / `heartbeat` were retired along with their shared
# `_renew_and_state` helper: the portal's keep-alive loop that was their only caller was
# itself deleted, and a route with no caller is not neutral — it reads as a supported way to
# hold the lock, and the next person needing one would have wired the loop straight back.
# What holds a turn open now is the wall-clock lease the SERVER renews, legible to a sweep
# in another process, which a browser timer never was.
#
# `force-end` was the last one standing and it has now gone the same way, its client exports
# with it. The kill switch a citizen actually reaches is
# `projects/{project_id}/stop-active-build` (the take-back dialog), which is project-scoped
# and needs no session id.


# --- the save model ---------------------------------------------------------


class SaveResponse(CamelModel):
    """What a Save returns: the commit the work was saved AT, so the client settles its dirty
    indicator from the write itself rather than going back to ask."""

    app_id: str
    head_sha: str | None = None


class PreviewStateResponse(CamelModel):
    """FIVE STATES AND AN UNKNOWN, not one boolean.

    A boolean has no unknown arm, so a FAILED registry read says "your preview is gone" for a
    question nobody managed to ask. It also flattens three genuinely different ordinary states
    — never built, asleep, another project took the slot — into the same shrug, leaving the
    pane only one sentence to offer back. `previewUrl` is echoed so a tab that reconnects can
    re-frame without a second call."""

    state: PreviewLifeState
    # RETAINED, strictly `state == alive`. A browser tab loaded before this change is still
    # polling `alive`, and a tab reading a missing field as false would paint "gone" over a
    # live preview for the whole rollout window. It cannot express UNKNOWN — new clients
    # branch on `state`, and this field exists only so old ones keep working.
    alive: bool
    preview_url: str | None = None
    # DIAGNOSTIC ONLY — NO CLIENT LOGIC MAY BRANCH ON THIS FIELD. Non-null strictly when
    # `state == alive`: the ISO-8601 instant at which something first watched this container
    # answer a request (the `serving_since` stamp on the registry hash, the same fact
    # `app_first_served` carries into the log).
    #
    # WHY IT IS HERE AT ALL: "alive" is now a claim the platform earned at a knowable moment,
    # and an operator handed a screenshot of a wait card — or of a preview that should not have
    # been on screen — could previously only take that claim on faith. With this field they can
    # join the screenshot to the log line and read WHEN, which is precisely the question nobody
    # could answer about the eight seconds on 2026-09-10.
    #
    # WHY BRANCHING ON IT IS FORBIDDEN: it is the same fact as `state` spelled a second time
    # from one read, with nothing forcing the two spellings to agree. A client computing
    # liveness as `servingSince !== null` is one backend refactor away from disagreeing with
    # `state`, and a resolver fed both would believe both. `portal/src/utils/previewAddress.ts`
    # already refuses exactly this shape — a `containerAlive` boolean beside its URL arms — for
    # exactly this reason: one expression, one source. Keep the field; forbid the branch, in
    # this docstring and in review.
    #
    # Additive and defaulted, so emitters and readers written before it stay wire-valid — the
    # same rule `StepEvent.hidden` carries in `schemas.py`.
    serving_since: datetime | None = None
    # SLOT_TAKEN only. Null when the live container matches no app this user owns (a ghost) —
    # naming the wrong project in a sentence about someone's work is worse than naming none.
    occupying_project_id: uuid.UUID | None = None
    occupying_project_name: str | None = None
    # TRI-STATE like `SaveStateResponse.dirty`, and for the identical reason: `null` is NO
    # CLAIM, never "no". Two ways to reach it, one instruction to the client — the object store
    # was unreachable, or `state` is `alive` and the poll declined to spend a Blob round trip on
    # a question no surface asks about a running app (this route's fixed budget allows none on
    # the hot path).
    # Answered WITHOUT a container, which is the whole point — it is the one restore signal
    # that survives the container being reclaimed.
    restorable: bool | None = None


class StopActiveBuildResponse(CamelModel):
    """THREE NAMED STATES, never a boolean — the shared shape of the ask and the status read.

    A `stopped: bool` whose `false` means "nothing was running" is a value the caller proceeds
    on, so a stop that had NOT finished arrives wearing the same face as one that had — and the
    next thing the client does is take the container. See `StopOutcome` for what each state
    licenses: `stopped` and `nothing_was_running` are both permission to continue,
    `still_running` is not."""

    # The ask reports the state at the instant the stop began; the status read reports it now. A
    # client that had to decode two shapes would be one refactor away from reading one of them
    # with the other's rules.
    state: StopOutcome


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
    # When the platform last wrote this app's tree to the recovery slot, or None if it never has
    # (also None when the store would not answer — an offer nobody can honour is one this does
    # not make). Lets the UI offer unsaved work back after a reclaim instead of quietly
    # forgetting it.
    #
    # AND IT IS THE ANSWER TO "CAN THE PLATFORM PUT THIS BACK?", which is a stronger fact than
    # this comment used to claim. It said `savedHead` was the only thing a relaunch restores;
    # that stopped being true when `SessionManager.newest_restore_source` landed. Every
    # automatic restore now goes through it, and it hands back the RECOVERY bundle in preference
    # to the saved one — deliberately, to close the data-loss bug its own docstring describes,
    # where a reclaimed container was rebuilt from the last SAVED tree and everything done after
    # that Save then existed nowhere. Where it does hand back the saved one, that bundle is the
    # newer of the two or holds the same tree, so a non-null instant here means the same thing
    # either way: what comes back is no older than this. A client may say so, and may stop
    # treating a bare `dirty: true` as work about to be lost — the state a citizen is in the
    # moment a build finishes, having saved nothing because there was nothing yet to save.
    #
    # STILL NEVER A SUBSTITUTE FOR THE USER'S OWN SAVE, and that distinction is the whole point:
    # RESUMPTION, NOT PROMOTION. `snapshot_key` is untouched, `dirty` stays true, and nothing on
    # this path creates a VERSION — only the citizen's own Save does, and Save stays manual. A
    # recovery copy is what the platform can resume from; `savedHead` is what its owner chose to
    # keep, and only that one is a thing they can ask to come back to.
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

    409, not 200, when there is no live workspace: a Save that reports success having stored
    nothing is the single worst outcome available here — the user walks away believing their
    work is kept. 409 as well while the agent is still writing, which is the SECOND worst: that
    save succeeded, and stored a tree caught mid-edit as the version a Relaunch would restore."""
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
    """ASK for the work in this project to stop. Returns as soon as the stop is under way.

    THE FIRST OF THREE: stop → save → release. `still_running` means the stop is in flight,
    which is what the caller polls on; `nothing_was_running` means there was nothing to stop, a
    success the caller proceeds on, not a miss. Deliberately no 409: asking a settled project
    to stop is already the state you wanted."""
    # It exists because the other two both refuse while a session is live, which made the reclaim
    # dialog a dead end — a user switching away from a building project was offered Save and
    # Switch, and the server declined both.
    #
    # Its own route rather than a flag on `release`, deliberately. Stopping is destructive to work
    # in progress and the user is choosing it explicitly; hiding it inside a release would make
    # "give up the workspace" sometimes also mean "kill the agent", the kind of silent extra
    # consequence this whole flow exists to remove. Each step stays one verb and keeps its own
    # refusal, so the ORDER is enforced by the guards rather than by a client remembering to call
    # them in sequence.
    #
    # NOTHING IS HELD OPEN FOR THE LENGTH OF A STOP. Awaiting the whole thing here would put the
    # stop's budget under whatever request timeout the gateway in front of the service enforces —
    # a number owned by the client's network and written down nowhere here. The wait lives in a
    # detached task and `GET .../stop-state` reports how it went, so a stop may honestly take as
    # long as it takes.
    #
    # NO `build_coordination_or_503` SEAM, unlike `release` and `save` beside it, and the
    # asymmetry is deliberate rather than an omission. Those two ask REDIS what is live — the
    # registry hash is their source of truth — so with no coordination subsystem they can decide
    # nothing and must refuse. This route asks a question Redis cannot answer and does not need
    # to: "is THIS PROCESS running work for this user?" lives in `_active_by_user`, and the stop
    # itself is a `task.cancel()` plus an await. Wrapping it produced a trailing
    # `_coordination_is_gone()` that could never execute (verified: the body completes and returns
    # 200 with the Redis singleton unset). Worse, it would have been wrong on the path that
    # matters — a live in-process build during a Redis outage is exactly when a user still needs
    # to stop it.
    if sandbox is None:
        raise AppApiError(status.HTTP_503_SERVICE_UNAVAILABLE, _SANDBOX_UNAVAILABLE_MSG)
    await owned_project_or_404(db, user.id, project_id)
    state = await manager.request_stop_of_active_work(db, user, project_id, sandbox_client=sandbox)
    return StopActiveBuildResponse(state=state)


@router.get(
    "/projects/{project_id}/stop-state",
    response_model=StopActiveBuildResponse,
    responses=error_responses(AUTH_401, (404, ErrorEnvelope, "Project not found")),
)
async def stop_state(
    project_id: uuid.UUID,
    user: CurrentUser,
    db: DbSession,
    manager: SessionManagerDep,
) -> StopActiveBuildResponse:
    """HAS IT STOPPED YET? The other half of the ask above, and the only authority on the answer.

    Read from the source of truth — whether a session still holds this project's app — and
    never inferred from how long the caller has been waiting. The browser polls this while it
    narrates the hand-over and proceeds only on `stopped` or `nothing_was_running`. A dropped
    connection costs nothing: the stop is a detached task, so asking again picks the answer up
    where it was left."""
    # A container declared dead when it was merely slow has already destroyed a citizen's unsaved
    # work in this repo, and the shape of that bug was a timeout read as a verdict.
    #
    # NO CSRF, and a GET, because it changes nothing. NO `build_coordination_or_503` either, for
    # the same reason the ask has none — the question lives in this process's session map, not in
    # Redis, and a live build during a Redis outage is exactly when the answer is still needed.
    #
    # Cheap enough to poll: two user-scoped DB reads and a dict lookup. No attach, no container
    # call, no registry round trip — a status poll that touched the container would manufacture
    # the very activity signal this route must never produce, on the workspace we are asking
    # permission to take.
    await owned_project_or_404(db, user.id, project_id)
    state = await manager.stop_state_of_active_work(db, user, project_id)
    return StopActiveBuildResponse(state=state)


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
    """Give up this project's workspace so another project can have the slot.

    The only route that destroys a container on purpose, and the counterpart to the reclaim
    refusal. `released: false` is a success, not a miss: the workspace was already gone, which
    is the state the caller asked for. Refuses with 409 while a build is actually running."""
    # The start path used to destroy the container silently, inside the request for a DIFFERENT
    # project, taking any unsaved work with it; here it is the user's own action, taken after
    # being told what it costs and offered a Save first.
    #
    # The 409 is there because an in-process session owns its container, and pulling it out from
    # under one is the strand this module exists to prevent.
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


# --- a colleague's shared-runtime view (#198) --------------------------------


async def _shared_preview_or_refuse(
    project_id: uuid.UUID,
    user: CurrentUser,
    db: DbSession,
    sandbox: OptionalSandbox,
    manager: SessionManagerDep,
    *,
    force_refresh: bool,
) -> SharedPreviewResponse | JSONResponse:
    """The whole of Launch and Refresh (#198 R19-R22) — the two endpoints below differ only in
    which arm of `SessionManager.launch_shared_preview` they ask for, so the access gate, the
    exception mapping and the response shape live here once.

    SHARED-ONLY, NEVER OWNER: `resolve_project_access` also admits the project's own owner, but
    an owner has `relaunch_preview` for this — Launch/Refresh exist for a COLLEAGUE'S restricted
    view, and an owner reaching this route reads identically to a stranger who was never shared
    with (the same non-leaking 404 `owned_project_or_404` gives everywhere else)."""
    if sandbox is None:
        raise AppApiError(status.HTTP_503_SERVICE_UNAVAILABLE, _SANDBOX_UNAVAILABLE_MSG)
    resolved = await resolve_project_access(db, user.id, project_id)
    if resolved.access is not ProjectAccess.SHARED:
        raise AppApiError(status.HTTP_404_NOT_FOUND, "Project not found.")
    with build_coordination_or_503():
        try:
            preview = await manager.launch_shared_preview(
                db, user, resolved.project, sandbox, force_refresh=force_refresh
            )
        except BuildSessionConflictError as exc:
            # The recipient is mid-build on a project of their OWN — a shared view never
            # pre-empts that (409, same shape `relaunch_preview` answers with).
            return _conflict_response(exc)
        except SandboxReclaimBlockedError as exc:
            return reclaim_blocked_response(exc)
        except SharedProjectHasNoAppError as exc:
            # Unreachable in practice (see the error's own docstring) — mapped to the same
            # "nothing to launch" 404 a confirmed-absent snapshot gets, since from the
            # recipient's side the two facts read identically.
            raise AppApiError(
                status.HTTP_404_NOT_FOUND,
                "Nothing to launch for this shared project.",
                code="no_saved_build",
            ) from exc
        except NoSnapshotToRelaunchError as exc:
            raise AppApiError(
                status.HTTP_404_NOT_FOUND,
                "The owner hasn't saved a version of this app yet.",
                code="no_saved_build",
            ) from exc
        except (SnapshotUnavailableError, SandboxUnreachableError, SandboxError) as exc:
            raise AppApiError(
                status.HTTP_503_SERVICE_UNAVAILABLE, _SANDBOX_UNAVAILABLE_MSG
            ) from exc
        return SharedPreviewResponse(
            app_id=preview.app_id,
            preview_url=preview.preview_url,
            ready=preview.ready,
            snapshot_taken_at=preview.snapshot_taken_at,
        )
    raise _coordination_is_gone()


@router.post(
    "/projects/{project_id}/shared-launch",
    response_model=SharedPreviewResponse,
    dependencies=[RequireCsrf],
    responses=error_responses(
        (403, ErrorEnvelope, "CSRF check failed"),
        AUTH_401,
        (404, ErrorEnvelope, "Project not found, not shared with you, or nothing saved yet"),
        (
            409,
            BuildConflictEnvelope,
            "You have a build running, or another project holds your workspace with unsaved work",
        ),
        (503, ErrorEnvelope, "The sandbox or build coordination is temporarily unavailable"),
    ),
)
async def launch_shared_project(
    project_id: uuid.UUID,
    user: CurrentUser,
    db: DbSession,
    sandbox: OptionalSandbox,
    manager: SessionManagerDep,
) -> SharedPreviewResponse | JSONResponse:
    """Open a project a colleague shared with you (#198 R19). Attaches to an already-live view
    if one is up (a reopened tab, a second click); otherwise restores one from the owner's
    latest SAVED snapshot — never their crash-recovery bundle (requirement 21)."""
    return await _shared_preview_or_refuse(
        project_id, user, db, sandbox, manager, force_refresh=False
    )


@router.post(
    "/projects/{project_id}/shared-refresh",
    response_model=SharedPreviewResponse,
    dependencies=[RequireCsrf],
    responses=error_responses(
        (403, ErrorEnvelope, "CSRF check failed"),
        AUTH_401,
        (404, ErrorEnvelope, "Project not found, not shared with you, or nothing saved yet"),
        (
            409,
            BuildConflictEnvelope,
            "You have a build running, or another project holds your workspace with unsaved work",
        ),
        (503, ErrorEnvelope, "The sandbox or build coordination is temporarily unavailable"),
    ),
)
async def refresh_shared_project(
    project_id: uuid.UUID,
    user: CurrentUser,
    db: DbSession,
    sandbox: OptionalSandbox,
    manager: SessionManagerDep,
) -> SharedPreviewResponse | JSONResponse:
    """Re-restore a shared project from whatever is CURRENTLY saved (#198 R22) — unlike Launch,
    never attaches to an already-live view even when one is up, since the owner may have saved
    something newer since it was brought up. `snapshotTakenAt` on the response is how the
    caller learns whether anything actually moved."""
    return await _shared_preview_or_refuse(
        project_id, user, db, sandbox, manager, force_refresh=True
    )


@router.post(
    "/shared-view/release",
    response_model=ReleaseResponse,
    dependencies=[RequireCsrf],
    responses=error_responses(
        (403, ErrorEnvelope, "CSRF check failed"),
        AUTH_401,
        (409, ConflictEnvelope, "A build is running in this workspace"),
        (503, ErrorEnvelope, "The sandbox or build coordination is temporarily unavailable"),
    ),
)
async def release_shared_view(
    user: CurrentUser,
    manager: SessionManagerDep,
    sandbox: OptionalSandbox,
) -> ReleaseResponse | JSONResponse:
    """Give up whatever colleague's shared view currently holds the caller's OWN slot (#198,
    requirement 24's self-service exit) — no `project_id`, because the caller may not own one
    that names it. The occupant `SandboxReclaimBlockedError` reports for a shared view is its
    OWNER's project, which a recipient never owns, so `stopActiveBuild`/`release` (both gated on
    `owned_project_or_404`) can never be the hand-over dialog's remedy for this case; this route
    asks nothing but "is a shared view sitting in my slot right now" and needs no id to ask it.

    `released: false` is a success — nothing was there to give up, or what was there was the
    caller's OWN build sandbox (`release_project_sandbox`'s job, not this one's)."""
    if sandbox is None:
        raise AppApiError(status.HTTP_503_SERVICE_UNAVAILABLE, _SANDBOX_UNAVAILABLE_MSG)
    with build_coordination_or_503():
        try:
            released = await manager.give_up_shared_view(user.id, sandbox_client=sandbox)
        except BuildSessionConflictError as exc:
            return _conflict_response(exc)
        except SandboxError as exc:
            raise AppApiError(
                status.HTTP_503_SERVICE_UNAVAILABLE,
                "Could not close that shared app just now. Please try again.",
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
    """Is the preview this tab is framing still real — and if not, WHY?

    Answers about THIS project only, and its budget is deliberately fixed: one round trip to
    the coordination store (two commands, pipelined), at most two user-scoped rows, at most two
    object-store HEADs, and NO container call of any kind."""
    # A framed preview that has been reclaimed looks EXACTLY like a working app — the last render
    # stays on screen, the iframe reports nothing, and a cross-origin pane cannot read a status
    # code. Once a build ends the tab holds no SSE and no timer, and the teardown happens inside a
    # DIFFERENT project's request, so there is nothing to push down. The tab has to ask.
    #
    # Deliberately NOT `save-state`, which the client could otherwise have polled: that runs two
    # `git` execs inside the container per call, and its `dirty=null` conflates three unrelated
    # causes.
    #
    # A container serving a different app is `slot_taken` here, named where we can name it: the
    # one-per-user registry means somebody else's container is exactly when yours is asleep, and
    # the builder deserves to be told which of their own projects is standing in the way rather
    # than that their app disappeared. A start already in flight for this project — this tab's own
    # press, another tab's, or a chat message that just started one — answers `starting` rather
    # than the stale `asleep` a second press used to invite. So does a container that exists but
    # has never answered a request: `alive` here means SERVED, not scheduled, and the whole
    # window between "a container was created" and "the app answered" is now a wait rather than
    # a preview URL a tab would frame over nginx's 404 page. The budget above is unchanged by
    # that — the serving proof is one more field on the registry hash this route already reads
    # whole, so it costs no extra round trip and still no container call.
    await owned_project_or_404(db, user.id, project_id)
    state = await manager.project_preview_state(db, user, project_id)
    return PreviewStateResponse(
        state=state.state,
        alive=state.alive,
        preview_url=state.preview_url,
        serving_since=state.serving_since,
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
    """Is the app this tab is framing still the citizen's app?

    A POST, WITH CSRF, because it is not a free read: it costs a container exec and it can
    raise an operational alarm. IT ONLY REPORTS — nothing is restored and nothing is destroyed
    here. The restore belongs to the next turn, where the citizen is present, has been told,
    and can confirm."""
    # THE TURN MAY NEVER COME. Every other integrity check in this system runs at the start of a
    # turn, which catches every reversion between one message and the next — and catches nothing
    # at all for someone who is reading, or in another tab, or at lunch. A standing completion
    # claim above their preview goes on being displayed for as long as the page stays open, which
    # is what this route exists to end.
    #
    # It follows this file's pattern exactly — `RequireCsrf`, `CurrentUser`, owned-or-404 — and
    # its path is in `_MUTATING_POSTS` so the CSRF matrix actually exercises it.
    #
    # DELIBERATELY NOT FOLDED INTO `preview-state`, whose budget is fixed at NO container call of
    # any kind because a browser tab drives it on a 45-second timer. The client calls this one only
    # when preview-state already reports alive AND a completion claim is standing, so the two never
    # both fire on a dark pane — and the manager rate-limits per app on top of that, so a tab left
    # open overnight cannot spin the container.
    #
    # Recovering somebody's app behind their back while they are looking at another tab is not a
    # kindness.
    await owned_project_or_404(db, user.id, project_id)
    if sandbox is None:
        # No sandbox service configured. Nothing can be checked and nothing is claimed —
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
    """Is the app compiling, compiled, or broken — asked by a tab with NO LIVE TURN.

    `unknown` for everything unanswerable, and the caller HOLDS its cover on it. Absent must
    never read as clean — that is the whole contract of this signal."""
    # THE RELOAD HOLE THIS CLOSES. The compile signal reaches the portal as a frame on the turn
    # stream, so its producer stops the moment the turn does. Reload the page after a turn that
    # ended red and the pane comes up with no signal at all: it initialises uncovered, and the
    # citizen is shown the framework's full-screen error screen underneath a live-preview label.
    # That is the exact failure the cover exists to prevent, reachable with one page reload.
    #
    # DELIBERATELY NOT FOLDED INTO `preview-state`, whose cost budget is fixed at NO container call
    # of any kind — it is a browser tab on a 45-second timer and that ceiling is the contract. This
    # is its own route, and the client only calls it when it is already framing a preview and no
    # turn is running, so the two never both fire on a dark pane.
    #
    # CHEAP BY CONSTRUCTION on the answer side: `/dev/compile` reads an in-memory value in the
    # container and never touches the dev server. The expensive part is the attach, which is why
    # the no-live-container case short-circuits before it.
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


# --- the app's own client-error report ----------------


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
    """The app crashed in the browser; the platform is being told.

    THE MISSING WITNESS. Every health signal the harness has runs against the SERVER — the
    type-check, the dev-server log tail, `/dev/status`, the root-route warm — and a Next app can
    satisfy all four and still throw before it paints anything. The only observer of that failure
    is the browser, and the generated app relays its own `window.onerror` /
    `unhandledrejection` / `console.*` captures to the framing portal
    (`sandbox/template/components/bial/error-capture.tsx` is the emitting side). This route is
    the receiving end: the portal validates the frame's origin and forwards what it caught, and
    the report is parked for the next `selfheal.verify` to collect, where it makes the verdict
    not-green. That is the whole user-visible consequence — the completion claim does not
    appear — because the report's own text goes to the AGENT and to nobody else.

    202, not 200: nothing has happened yet when this returns. The report is parked, and whether it
    changes anything depends on a verify that has not run.

    `recorded: false` is still a 202. The one thing that can be refused here is a report the store
    had no room for, and that is not a client error to raise — it is a fact about volume the
    caller deserves to be told (see `ClientErrorReportResponse`).

    OWNED-OR-404 on the app, like every other route in this file: a cross-user app id and a
    missing one are the same non-leaking answer. Not 403 — telling a caller "that app exists but
    is not yours" is exactly the probe the 404 exists to refuse. Every route in this file now
    answers that way; the one owner-asserted 403 this file ever had belonged to `force-end`,
    which is deleted.

    NO SESSION, NO REDIS, NO SANDBOX. Deliberately PROJECT-scoped rather than session-scoped: the
    crash arrives from a framed preview, and a preview outlives its build session by design
    (relaunch registers none at all). A route that needed a live session would be unable to
    receive a report about an app the user is simply looking at, which is most of them. Project
    rather than app because a project has exactly one app and the project is what the caller
    holds — the framing pane is addressed by project everywhere else in the portal, and an ingest
    that needed an app id would have to buy one with an extra round trip per crash.

    READ-ONLY on the way to the app. `resolve_app_for_project` is the usual accessor and it is
    the wrong one here: it UPSERTS, so a stray report against a project nobody has ever built
    would MINT an app row. An app that does not exist is a 404 — there is no build for a browser
    crash to be about."""
    await owned_project_or_404(db, user.id, project_id)
    # Owner AND project in the predicate, not just project: `owned_project_or_404` above has
    # already refused another user's project, and the second predicate is what keeps that true
    # if this query is ever moved somewhere that has not.
    app_id = (
        await db.execute(
            sa.select(AppRegistry.id).where(
                AppRegistry.project_id == project_id, AppRegistry.user_id == user.id
            )
        )
    ).scalar_one_or_none()
    if app_id is None:
        raise AppApiError(status.HTTP_404_NOT_FOUND, "Project not found.")
    # `app_name_for` is the same forward mapping the sandbox is NAMED by, so the key written here
    # is exactly the `SandboxHandle.app_name` the verify reads back. It is deliberately not
    # reversed anywhere — the mapping is lossy (28 of 32 hex chars) and only ever computed
    # forwards, which is the property `inventory.py` relies on for the same reason.
    recorded = park_client_error(
        app_name_for(app_id), source=body.source, title=body.title, stack=body.stack
    )
    return ClientErrorReportResponse(recorded=recorded)
