"""One-click deploy — the citizen-facing control surface.

`POST /v1/projects/{id}/deploy` starts a deploy and returns **202 immediately**. That is not
a style choice: a deploy runs for minutes and the edge gateway times out at twenty seconds,
so anything that waits for the result is a guaranteed 504 on a deploy that is in fact going
fine. The work is detached; the client polls `GET /v1/projects/{id}/deployment`.

NO ADMIN APPROVAL ON THIS PATH. The existing `submit` / `approve` / `reject` / `disable`
admin surface is untouched and still works; it is simply not what this route calls. The two
lineages stay separate on purpose — `mark-deployed` is guarded on `status == APPROVED`, and
a self-deployed app is still `draft`, so relaxing that guard to fit would dissolve the
approval invariant rather than reuse it.

NO AUTHENTICATION ON THE PUBLISHED APP. Deliberately out of scope for this feature, and
worth stating plainly: until that lands, any member of staff who has the URL can open any
deployed app. The container is only reachable inside the corporate network — that is the
whole of the current protection.

Both routes take the OPTIONAL dependencies. Every `Depends` is resolved BEFORE the route
body's first statement, so a raising provider escapes the body's `try` and produces an
undocumented 500 with the wrong envelope — which is exactly how the 503 paths on the storage
and sandbox routes were once broken.
"""

from __future__ import annotations

import uuid

import structlog
from fastapi import APIRouter, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import CurrentUser, DbSession
from src.api.v1.build_sessions.deps import OptionalSandbox, RequireCsrf, SessionManagerDep
from src.api.v1.deploy.deps import OptionalDeployService
from src.api.v1.deploy.schemas import DeploymentResponse, DeployRequest, DeployStartedResponse
from src.api.v1.live_build import refuse_while_build_session_live
from src.core.errors import AppApiError
from src.db.models.user import User
from src.schemas import AUTH_401, ErrorEnvelope, error_responses
from src.services.audit.log import append_audit
from src.services.build_sessions.manager import NoLiveSandboxError, SessionManager
from src.services.deploy.resolve import deploy_target
from src.services.deploy.service import DeployNotPossibleError, deployment_for_app
from src.services.projects.resolve import owned_project_or_404
from src.services.sandbox import SandboxClient

_log = structlog.get_logger()

router = APIRouter(prefix="/projects", tags=["deploy"])

_UNAVAILABLE = "Deploying is not switched on for this environment. Please tell an administrator."
_BUILD_IN_FLIGHT = "Your app is being built right now. Wait for that to finish, then deploy."


@router.post(
    "/{project_id}/deploy",
    response_model=DeployStartedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[RequireCsrf],
    responses=error_responses(
        (403, ErrorEnvelope, "CSRF check failed"),
        AUTH_401,
        (404, ErrorEnvelope, "Project not found"),
        (409, ErrorEnvelope, "Nothing saved to deploy, unsaved changes, or already deploying"),
        (503, ErrorEnvelope, "Deploying is not configured on this deployment"),
    ),
)
async def deploy_project(
    project_id: uuid.UUID,
    user: CurrentUser,
    db: DbSession,
    manager: SessionManagerDep,
    sandbox: OptionalSandbox,
    service: OptionalDeployService,
    body: DeployRequest | None = None,
) -> DeployStartedResponse:
    """Start a deploy. Returns 202 with the id to poll.

    UNSAVED WORK IS REFUSED BY DEFAULT. A deploy ships the last SAVED version, so quietly
    deploying while the workspace is ahead of it would publish something the citizen never
    asked for and give them no way to tell. `saveFirst` is the explicit "save and deploy"
    they opted into.

    `dirty` is TRI-STATE and unknown is not dirty: with no live workspace there is nothing to
    compare against, and the saved version is the only version — refusing there would make a
    perfectly ordinary "come back tomorrow and deploy" impossible.
    """
    if service is None:
        raise AppApiError(status.HTTP_503_SERVICE_UNAVAILABLE, _UNAVAILABLE)
    await owned_project_or_404(db, user.id, project_id)
    request = body or DeployRequest()

    target = await deploy_target(db, user_id=user.id, project_id=project_id)
    if target is None:
        raise AppApiError(
            status.HTTP_409_CONFLICT,
            "There is nothing to deploy yet — build something and save it first.",
        )
    app_id = target.app_id

    # A build session writing files while the snapshot is taken would ship a tree that never
    # coherently existed: valid bytes, wrong app, undetectable afterwards.
    await refuse_while_build_session_live(
        user.id, conflict_message=_BUILD_IN_FLIGHT, app_id=app_id
    )

    await _resolve_unsaved_work(
        db, user=user, project_id=project_id, manager=manager, sandbox=sandbox, request=request
    )

    try:
        started = await service.start(
            db,
            user_id=user.id,
            app_id=app_id,
            project_id=project_id,
            conversation_id=target.conversation_id,
        )
    except DeployNotPossibleError as exc:
        raise AppApiError(status.HTTP_409_CONFLICT, str(exc), code=exc.code) from None

    await append_audit(
        db,
        actor_id=user.id,
        action="deploy",
        resource_type="app",
        resource_id=str(app_id),
        detail={"deploymentId": str(started.deployment_id), "projectId": str(project_id)},
    )
    await db.commit()

    _log.info("deploy_started", app_id=str(app_id), deployment_id=str(started.deployment_id))
    return DeployStartedResponse(
        deployment_id=str(started.deployment_id),
        app_id=str(started.app_id),
        status="running",
    )


async def _resolve_unsaved_work(
    db: AsyncSession,
    *,
    user: User,
    project_id: uuid.UUID,
    manager: SessionManager,
    sandbox: SandboxClient | None,
    request: DeployRequest,
) -> None:
    """Save first if asked, refuse if not — never deploy over unsaved work silently."""
    if sandbox is None:
        # No sandbox runtime configured at all, so there is no live workspace that could be
        # ahead of the saved version. Nothing to compare, nothing to refuse — the saved
        # version IS the version. Same reading as `dirty=None` below.
        return
    state = await manager.project_save_state(db, user, project_id, sandbox_client=sandbox)
    if not state.dirty:
        return
    if not request.save_first:
        raise AppApiError(
            status.HTTP_409_CONFLICT,
            "You have changes that are not saved yet. Save them first, or choose "
            "'Save and deploy'.",
            code="unsaved_changes",
        )
    try:
        await manager.save_project_snapshot(db, user, project_id, sandbox_client=sandbox)
    except NoLiveSandboxError:
        # The workspace went away between the dirty check and the save. The saved version is
        # intact, so this is not fatal — but it IS a different deploy from the one asked for,
        # so say so rather than shipping the older tree silently.
        raise AppApiError(
            status.HTTP_409_CONFLICT,
            "Your workspace stopped running before the changes could be saved, so there was "
            "nothing new to deploy. Your last saved version is intact.",
        ) from None


@router.get(
    "/{project_id}/deployment",
    response_model=DeploymentResponse,
    responses=error_responses(
        AUTH_401,
        (404, ErrorEnvelope, "Project not found"),
        (503, ErrorEnvelope, "Deploying is not configured on this deployment"),
    ),
)
async def latest_deployment(
    project_id: uuid.UUID,
    user: CurrentUser,
    db: DbSession,
    service: OptionalDeployService,
) -> DeploymentResponse:
    """The latest deploy attempt for this project — what the client polls.

    An app that has never been deployed is a NORMAL state, not a 404: the answer is an empty
    envelope, exactly as `save-state` answers for a project with no workspace."""
    if service is None:
        raise AppApiError(status.HTTP_503_SERVICE_UNAVAILABLE, _UNAVAILABLE)
    await owned_project_or_404(db, user.id, project_id)

    target = await deploy_target(db, user_id=user.id, project_id=project_id)
    if target is None:
        return DeploymentResponse()

    row = await deployment_for_app(db, app_id=target.app_id)
    if row is None:
        return DeploymentResponse(app_id=str(target.app_id))
    return DeploymentResponse.of(row)
