"""Build-sessions HTTP router — the build-session control surface.

WHY THIS EXISTS

`relaunch` + the project-scoped save/preview/stop ops + the superadmin `internal/reap`, all
owner-scoped by `user.id`: every not-found-or-other-user case is a non-leaking 404. The mutating
POSTs carry the reusable `RequireCsrf` dependency; the GETs are exempt.

No route here is addressed by a session id. A build runs as an ordinary Write chat turn, which
registers its workspace through `SessionManager.ensure_sandbox` and never serialises one.

One inbound route here is not a control op at all — `projects/{project_id}/client-error`,
where the app's own in-browser error reporter's findings arrive by way of the portal. It
follows the same pattern as everything else here (CSRF, `CurrentUser`, owned-or-404), and
it lives in THIS router rather than under `apps/` because its only consumer is the build
harness's health verdict."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import sqlalchemy as sa
import structlog
from fastapi import APIRouter, status
from fastapi.responses import JSONResponse
from redis.exceptions import RedisError

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
    ClientErrorReportRequest,
    ClientErrorReportResponse,
    CompileStateResponse,
    DiscardRequest,
    PreviewLifeState,
    RelaunchPreviewRequest,
    RelaunchPreviewResponse,
    RenewalOutcome,
    RenewPresenceRequest,
    RenewPresenceResponse,
    SharedPreviewResponse,
    WorkspaceCheckResponse,
)
from src.api.v1.live_build import ReclaimBlockedError, reclaim_blocked_response
from src.core.errors import AppApiError
from src.core.integrity_types import WorkspaceState
from src.db.models.app_registry import AppRegistry
from src.db.models.conversation import Conversation
from src.schemas import AUTH_401, CamelModel, ErrorEnvelope, error_responses
from src.services.audit.log import append_audit
from src.services.build_sessions import (
    BuildSessionConflictError,
    NoLiveSandboxError,
    NoSnapshotToRelaunchError,
    NothingSavedToGoBackToError,
    SandboxReclaimBlockedError,
    SandboxUnreachableError,
    SaveState,
    SharedProjectHasNoAppError,
    SnapshotUnavailableError,
    StopOutcome,
    app_name_for,
    sweep_all,
)
from src.services.build_sessions.inventory import owning_app_ids
from src.services.build_sessions.locks import (
    renew_presence_stay,
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
from src.services.storage import StorageError

router = APIRouter(prefix="/build-sessions", tags=["build_sessions"])
_log = structlog.get_logger()

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
    """The inner error object of a build-session 409: `{message, code}`. A client branches on
    the CODE — the status alone cannot tell these conflicts apart."""

    message: str
    code: str


class ConflictEnvelope(CamelModel):
    """`{"error": {message, code}}` — a build-session 409 body (`_conflict_response`), and the
    `lock_lost` 409 too."""

    error: _ConflictError


class BuildConflictEnvelope(CamelModel):
    """The 409 for a route that can conflict two ways: this very project's own work already
    running, or a COLLEAGUE'S SHARED VIEW holding the one workspace
    (`projectId`/`projectName`/`isSharedView`). `code` discriminates —
    `build_session_already_active` vs `sandbox_reclaim_blocked` — and a client must branch on
    it, since only the second names a project.

    A CITIZEN'S OWN OTHER PROJECT IS NOT IN THIS LIST ANY MORE. Opening one starts it and hands
    the outgoing container to the shutdown routine, so neither code is raised for it."""

    error: _ConflictError | ReclaimBlockedError


_BUILD_SESSION_ACTIVE = "A build session is already active."

_PROJECT_IS_HOLDING_IT = (
    "“{project}” has a chat or a build running. Finish or stop it, then close the project."
)


def _conflict_response(*, project_name: str | None = None) -> JSONResponse:
    """The one 409 both conflicting routes answer with — same code, same shape, two sentences.

    NAMING THE PROJECT IS THE RELEASE ROUTE'S ALONE. Its gate compares the app the live session
    holds against the one this project owns, so the project it refuses IS the project holding
    the workspace, and saying which one is the whole of what the citizen can act on. Relaunch
    conflicts on the per-user slot and knows no project, so it keeps the bare sentence rather
    than naming one it has not compared."""
    error: dict[str, str] = {
        "message": (
            _BUILD_SESSION_ACTIVE
            if project_name is None
            else _PROJECT_IS_HOLDING_IT.format(project=project_name)
        ),
        "code": "build_session_already_active",
    }
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


# --- internal/reap ---


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
    # The sweep walks the registry namespace with bare primitives, so an outage here is a 503
    # to the operator rather than an opaque 500. The audit row is deliberately inside: a sweep
    # that never ran is not an action worth recording. Redis is resolved LAZILY inside the seam,
    # so `get_redis()` raises here and the trailing `_coordination_is_gone()` answers.
    with build_coordination_or_503():
        redis = get_redis()
        # THE SAME MAP THE SCHEDULED PASS SUPPLIES. Without it every container resolves to no
        # app, and a sweep with no app to name has nowhere to write a tree back to — this door
        # would destroy every idle citizen's unsaved work and report a clean pass.
        result = await sweep_all(
            redis,
            sandbox,
            live_users=manager.live_user_ids(),
            app_ids_by_name=await owning_app_ids(db),
        )
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


# --- control ops: relaunch ----------------------------------------------------


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
            "This project's own work is already running, or a shared view holds the workspace",
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

    Not a build: it runs no agent at all, and the manager path never occupies the
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
            relaunched = await manager.relaunch_preview(db, user, body.project_id, sandbox)
        except BuildSessionConflictError:
            # This project's own work is running — relaunch never pre-empts it (409). A
            # DIFFERENT project of theirs never reaches here: that is a switch, and it starts.
            return _conflict_response()
        except SandboxReclaimBlockedError as exc:
            # A colleague's shared view holds the one slot, and it has no hand-over.
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


# A turn is held open by the wall-clock lease the SERVER renews, legible to a sweep in another
# process. A CONTAINER between turns is held by a browser timer — `projects/{project_id}/renew`
# below — and what makes that safe is the absolute age ceiling: the renewal cannot push past the
# ceiling `reaper.py` evaluates in both sparing arms, so a tab that will not stop renewing can
# never make a container unreclaimable. It renews the preview's stay and nothing else — never the
# lock, never the heartbeat.


# --- the save model ---------------------------------------------------------


class SaveResponse(CamelModel):
    """What a Save returns: the commit the work was saved AT, so the client settles its dirty
    indicator from the write itself rather than going back to ask."""

    app_id: str
    head_sha: str | None = None


class PreviewStateResponse(CamelModel):
    """THREE STATES, and a read that could not decide is not one of them: it is a 503.

    A boolean would let a FAILED registry read say "your preview is gone" for a question nobody
    managed to ask, and it cannot tell a wait from a rest. `previewUrl` is echoed so a tab that
    reconnects can re-frame without a second call."""

    state: PreviewLifeState
    # RETAINED, strictly `state == alive`. A browser tab loaded before this change is still
    # polling `alive`, and a tab reading a missing field as false would paint "gone" over a
    # live preview for the whole rollout window. New clients branch on `state` and read this
    # only as the fallback for a state they do not recognise.
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
    # same rule `RelaunchPreviewResponse.ready` carries in `schemas.py`.
    serving_since: datetime | None = None
    # STARTING only: the ISO-8601 instant this project's wait began, so the pane's elapsed
    # figure is the real wait rather than the life of the current page. Null is NO CLAIM — the
    # client falls back to counting from its own mount.
    #
    # THE CLIENT MAY BRANCH ON THIS ONE, unlike `serving_since` above, and the difference is
    # that it is not `state` spelled twice: `state == starting` says a wait is under way and
    # this says how long it has been under way, which is a fact no other field carries.
    #
    # Additive and defaulted, so emitters and readers written before it stay wire-valid.
    starting_since: datetime | None = None
    # TRI-STATE like `SaveStateResponse.dirty`, and for the identical reason: `null` is NO
    # CLAIM, never "no". Two ways to reach it, one instruction to the client — the object store
    # was unreachable, or `state` is `alive` or `starting` and the poll declined to spend a Blob
    # round trip on a question no surface asks about a running app (this route's fixed budget
    # allows none on the hot path).
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
    saved_head: str | None = None


class DiscardNotice(CamelModel):
    """The line a discard wrote into the conversation it came from, so the page can show it where
    a reload would."""

    seq: int
    saved_at: datetime | None = None


class DiscardResponse(SaveStateResponse):
    """The save state after a discard, plus `notice` for the conversation the request came from —
    `None` when it came from outside one."""

    notice: DiscardNotice | None = None


def _save_state_fields(state: SaveState) -> dict[str, Any]:
    return {
        "app_id": str(state.app_id) if state.app_id else None,
        "dirty": state.dirty,
        "container_head": state.container_head,
        "saved_head": state.saved_head,
    }


@router.post(
    "/projects/{project_id}/save",
    response_model=SaveResponse,
    dependencies=[RequireCsrf],
    responses=error_responses(
        (403, ErrorEnvelope, "CSRF check failed"),
        AUTH_401,
        (404, ErrorEnvelope, "Project not found"),
        (409, ErrorEnvelope, "There is no live workspace to save"),
        (503, ErrorEnvelope, "The sandbox or the file store is unavailable"),
    ),
)
async def save_project(
    project_id: uuid.UUID,
    user: CurrentUser,
    db: DbSession,
    manager: SessionManagerDep,
    sandbox: OptionalSandbox,
) -> SaveResponse:
    """THE SAVE. The agent commits inside the container as it works; this is the only thing that
    pushes the result to durable storage BECAUSE THE USER ASKED — the platform writes the same
    slot itself when it destroys a container, so this is the citizen's write, not the only one.

    409, not 200, when there is no live workspace: a Save that reports success having stored
    nothing is the single worst outcome available here — the user walks away believing their
    work is kept. 409 as well while the agent is still writing, which is the SECOND worst: that
    save succeeded, and stored a tree caught mid-edit as the version a Relaunch would restore.

    A container blip or a store that will not answer is a 503 in the same words Discard uses:
    nothing was written, and trying again is the whole of what the citizen can do."""
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
    except (StorageError, SandboxError) as exc:
        _log.warning("workspace_save_failed", project_id=str(project_id), exc_info=exc)
        raise AppApiError(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Your changes could not be saved just now. Try again in a moment.",
        ) from None
    return SaveResponse(app_id=str(outcome.app_id), head_sha=outcome.head_sha)


@router.post(
    "/projects/{project_id}/discard",
    response_model=DiscardResponse,
    dependencies=[RequireCsrf],
    responses=error_responses(
        (403, ErrorEnvelope, "CSRF check failed"),
        AUTH_401,
        (404, ErrorEnvelope, "Project or conversation not found"),
        (409, ErrorEnvelope, "There is nothing to discard right now"),
        (503, ErrorEnvelope, "The sandbox or the file store is unavailable"),
    ),
)
async def discard_unsaved_changes(
    project_id: uuid.UUID,
    body: DiscardRequest,
    user: CurrentUser,
    db: DbSession,
    manager: SessionManagerDep,
    sandbox: OptionalSandbox,
) -> DiscardResponse:
    """Put the app back to the version its owner last saved — the Discard button.

    What it replaces is parked, never deleted. Every conversation of the project that spoke since
    that save gets a note its next reply reads; `notice` is the one written into the conversation
    the request came from, so the page shows it without a reload."""
    if sandbox is None:
        raise AppApiError(status.HTTP_503_SERVICE_UNAVAILABLE, _SANDBOX_UNAVAILABLE_MSG)
    await owned_project_or_404(db, user.id, project_id)
    if body.conversation_id is not None:
        found = await db.scalar(
            sa.select(Conversation.id).where(
                Conversation.id == body.conversation_id,
                Conversation.user_id == user.id,
                Conversation.project_id == project_id,
            )
        )
        if found is None:
            raise AppApiError(status.HTTP_404_NOT_FOUND, "Conversation not found.")
    try:
        outcome = await manager.discard_unsaved_changes(
            db, user, project_id, sandbox_client=sandbox, conversation_id=body.conversation_id
        )
    except NoLiveSandboxError:
        raise AppApiError(
            status.HTTP_409_CONFLICT,
            "Your workspace is not running, so there is nothing to discard. Your saved version "
            "is intact.",
        ) from None
    except BuildSessionConflictError:
        raise AppApiError(
            status.HTTP_409_CONFLICT, "Wait for the reply to finish, then discard."
        ) from None
    except NothingSavedToGoBackToError:
        raise AppApiError(
            status.HTTP_409_CONFLICT, "There is no saved version to go back to yet."
        ) from None
    except (StorageError, SandboxError) as exc:
        _log.warning("workspace_discard_failed", project_id=str(project_id), exc_info=exc)
        raise AppApiError(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "Your changes could not be discarded just now. Try again in a moment.",
        ) from None
    seq = outcome.notes.get(body.conversation_id) if body.conversation_id is not None else None
    return DiscardResponse(
        **_save_state_fields(outcome.state),
        notice=DiscardNotice(seq=seq, saved_at=outcome.saved_at) if seq is not None else None,
    )


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
    project_name = (await owned_project_or_404(db, user.id, project_id)).name
    with build_coordination_or_503():
        try:
            released = await manager.release_project_sandbox(
                db, user, project_id, sandbox_client=sandbox
            )
        except BuildSessionConflictError:
            return _conflict_response(project_name=project_name)
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
        except BuildSessionConflictError:
            # The recipient is mid-build on a project of their OWN — a shared view never
            # pre-empts that (409, same shape `relaunch_preview` answers with).
            return _conflict_response()
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
        except BuildSessionConflictError:
            return _conflict_response()
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
    responses=error_responses(
        AUTH_401,
        (404, ErrorEnvelope, "Project not found"),
        (503, ErrorEnvelope, "Build coordination is temporarily unavailable"),
    ),
)
async def preview_state(
    project_id: uuid.UUID,
    user: CurrentUser,
    db: DbSession,
    manager: SessionManagerDep,
) -> PreviewStateResponse:
    """Is the preview this tab is framing still real — and if not, WHY?

    Answers about THIS project only, and its budget is deliberately fixed: one round trip to
    the coordination store (two commands, pipelined), two user-scoped rows, at most two
    object-store HEADs, and NO container call of any kind. A store that will not answer is a 503,
    which both polls already read as a check that decided nothing."""
    # A framed preview that has been reclaimed looks EXACTLY like a working app — the last render
    # stays on screen, the iframe reports nothing, and a cross-origin pane cannot read a status
    # code. Once a build ends the tab holds no SSE and no timer, and the teardown happens inside a
    # DIFFERENT project's request, so there is nothing to push down. The tab has to ask.
    #
    # Deliberately NOT `save-state`, which the client could otherwise have polled: that runs two
    # `git` execs inside the container per call, and its `dirty=null` conflates three unrelated
    # causes.
    #
    # A container serving a different app is `asleep` here: pressing start takes the one workspace
    # back, so the pane offers the same thing whoever is holding it. A start already in flight for
    # this project — this tab's own press, another tab's, or a chat message that just started one
    # — answers `starting` rather than the stale `asleep` a second press used to invite. So does a
    # container that exists but has never answered a request: `alive` here means SERVED, not
    # scheduled, and the whole window between "a container was created" and "the app answered" is
    # now a wait rather than a preview URL a tab would frame over nginx's 404 page. The budget
    # above is unchanged by that — the serving proof is one more field on the registry hash this
    # route already reads whole, so it costs no extra round trip and still no container call.
    await owned_project_or_404(db, user.id, project_id)
    try:
        state = await manager.project_preview_state(db, user, project_id)
    except RedisError as exc:
        raise coordination_is_gone() from exc
    return PreviewStateResponse(
        state=state.state,
        alive=state.alive,
        preview_url=state.preview_url,
        serving_since=state.serving_since,
        starting_since=state.starting_since,
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
    raise an operational alarm. IT RESTORES NOTHING — the restore belongs to the next turn, where
    the citizen is present, has been told, and can confirm. The one thing it may restart is the
    dev server of an INTACT app that has stopped, in the same container, because nothing else ever
    ends that wait; see `SessionManager.project_workspace_check` for the guards."""
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
    # on a reading worth a container call — `alive` under a standing completion claim, or a wait
    # that looks stuck (`mayHaveStopped` in the portal) — and the manager rate-limits per app on
    # top of that, so a tab left open overnight cannot spin the container.
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


@router.post(
    "/projects/{project_id}/renew",
    response_model=RenewPresenceResponse,
    dependencies=[RequireCsrf],
    responses=error_responses(
        AUTH_401,
        (403, ErrorEnvelope, "CSRF check failed"),
        (404, ErrorEnvelope, "Project not found"),
        (503, ErrorEnvelope, "Build coordination is temporarily unavailable"),
    ),
)
async def renew_presence(
    project_id: uuid.UUID,
    body: RenewPresenceRequest,
    user: CurrentUser,
    db: DbSession,
) -> RenewPresenceResponse:
    """A screen that can frame this project is still open; hold its container.

    PRESENCE IS THE SIGNAL, AND SILENCE IS DEPARTURE. Nothing is sent when a citizen leaves —
    navigating away, closing the tab, sleeping the machine and losing the network all simply stop
    the renewals, so all four are one event with no code of their own and nothing that can fail to
    arrive. What remains is a short stay that lapses, which is what a departure was always meant
    to produce.

    A POST, WITH CSRF, for the same reason `workspace-check` is one: it is not a free read. It
    writes a deadline onto coordination state, and a deadline a third-party page could push
    forward from a citizen's browser is a deadline an attacker can use to run up a bill.

    THE CONTAINER IS NEVER NAMED ON THE WIRE. The server resolves which container this project
    owns from its own app row — `app_name_for` is the same forward mapping the sandbox is named
    by — and the write is refused inside Redis when the record names anything else. A caller that
    could supply a name could hold somebody else's container open.

    200 ON ALL THREE OUTCOMES. `not_this_container` is the ordinary reading a moment after
    somebody opens a second project, and `nothing_running` is what a screen polling through a
    teardown sees; neither is an error, and neither is something to show anybody. A lease that
    genuinely lapsed reaches the citizen through `preview-state`, which is the one route allowed
    to say a preview is gone."""
    await owned_project_or_404(db, user.id, project_id)
    # Owner AND project in the predicate, for the reason `report_client_error` states: the
    # second clause is what keeps this scoped if the query is ever moved somewhere that has not
    # already refused another user's project.
    app_id = (
        await db.execute(
            sa.select(AppRegistry.id).where(
                AppRegistry.project_id == project_id, AppRegistry.user_id == user.id
            )
        )
    ).scalar_one_or_none()
    if app_id is None:
        # No app row means nothing was ever built for this project, so there is no container a
        # surface could be holding open. A 404 would be wrong — the PROJECT exists and is theirs.
        return RenewPresenceResponse(outcome=RenewalOutcome.NOTHING_RUNNING)
    with build_coordination_or_503():
        redis = get_redis()
        outcome, stay_until = await renew_presence_stay(
            redis,
            user.id,
            app_name=app_name_for(app_id),
            presence=body.presence,
        )
        return RenewPresenceResponse(outcome=outcome, stay_until=stay_until)
    raise _coordination_is_gone()


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
    return SaveStateResponse(**_save_state_fields(state))


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
