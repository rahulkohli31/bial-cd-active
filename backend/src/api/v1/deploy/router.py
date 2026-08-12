"""One-click deploy — the citizen-facing control surface, plus the admin kill-switch (#113).

`POST /v1/projects/{id}/deploy` starts a deploy and returns **202 immediately**. That is not
a style choice: a deploy runs for minutes and the edge gateway times out at twenty seconds,
so anything that waits for the result is a guaranteed 504 on a deploy that is in fact going
fine. The work is detached; the client polls `GET /v1/projects/{id}/deployment`.

NO ADMIN APPROVAL TO **START** A DEPLOY. The existing `submit` / `approve` / `reject` /
`disable` admin surface is untouched and still works; it is simply not what `deploy_project`
calls. The two lineages stay separate on purpose — `mark-deployed` is guarded on
`status == APPROVED`, and a self-deployed app is still `draft`, so relaxing that guard to fit
would dissolve the approval invariant rather than reuse it. `unpublish` below is the
exception: it IS an admin lever, deliberately routed here rather than through that lineage —
see its own docstring.

NO AUTHENTICATION ON THE PUBLISHED APP. Deliberately out of scope for this feature, and
worth stating plainly: until that lands, anyone who has the URL can open any deployed app.
The app's `ingress` is `external` (`deploy/config.py`), reachable outside the Container
Apps environment — whether the managed environment's own VNet integration further
restricts that to the corporate network is UNCONFIRMED (see the comment on `config.py`'s
`ingress` field for how to check). Until confirmed, treat a deployed app as reachable on
the public internet, not just from inside the corporate network. `unpublish` is the first
real answer to "take it down now" short of destroying the citizen's project or app.

TWO ROUTERS IN ONE FILE. `router` (prefix `/projects`) is the citizen-facing pair above;
`admin_router` (prefix `/apps`) is the superadmin-only `unpublish` lever, kept in this file
rather than `admin/router.py` because that file is being edited by two other in-flight
branches — mirrors `admin/router.py`'s own two-router shape (`router` + `users_router`).
Both are registered separately in `api/v1/router.py`.

Every route here takes its Azure/service dependency as OPTIONAL. Every `Depends` is
resolved BEFORE the route body's first statement, so a raising provider escapes the body's
own `try` and produces an undocumented 500 with the wrong envelope — which is exactly how
the 503 paths on the storage and sandbox routes were once broken.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import structlog
from fastapi import APIRouter, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import CurrentUser, DbSession
from src.api.deps_rbac import CurrentSuperadmin
from src.api.v1.build_sessions.deps import OptionalSandbox, RequireCsrf, SessionManagerDep
from src.api.v1.deploy.deps import OptionalDeployService, OptionalPublishedAppRemover
from src.api.v1.deploy.schemas import (
    DeploymentResponse,
    DeployRequest,
    DeployStartedResponse,
    UnpublishResponse,
)
from src.api.v1.live_build import refuse_while_build_session_live
from src.core.errors import AppApiError
from src.db.models.app_registry import AppRegistry
from src.db.models.user import User
from src.schemas import AUTH_401, DetailBody, ErrorEnvelope, error_responses
from src.services.audit.log import append_audit
from src.services.build_sessions.manager import NoLiveSandboxError, SessionManager
from src.services.deploy import store
from src.services.deploy.classification import (
    qualifies_for_deploy,
    refusal_message,
    total_weight,
)
from src.services.deploy.resolve import deploy_target
from src.services.deploy.service import DeployNotPossibleError, deployment_for_app
from src.services.deploy.teardown import sweep_published_apps
from src.services.projects.resolve import owned_project_or_404
from src.services.sandbox import SandboxClient

_log = structlog.get_logger()

router = APIRouter(prefix="/projects", tags=["deploy"])

# Separate router for the admin app-lever (#113), keyed on app_id like every other
# superadmin action (admin/router.py's `/{app_id}/disable`, `/{app_id}/enable`, …) rather
# than this file's own citizen-facing `/projects/{project_id}/...` convention — an admin
# operates on an app, not a project they own. Lives here rather than in admin/router.py
# because that file is being edited by two other in-flight branches; mirrors
# admin/router.py's own two-router-per-file shape (`router` + `users_router`).
admin_router = APIRouter(prefix="/apps", tags=["deploy-admin"])

_UNAVAILABLE = "Deploying is not switched on for this environment. Please tell an administrator."
_BUILD_IN_FLIGHT = "Your app is being built right now. Wait for that to finish, then deploy."
_ADMIN_AUTH = (AUTH_401, (403, DetailBody, "Super-admin privileges required"))
_TEARDOWN_FAILED = "The published container could not be removed. Please try again."


@router.post(
    "/{project_id}/deploy",
    response_model=DeployStartedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[RequireCsrf],
    responses=error_responses(
        (403, ErrorEnvelope, "CSRF check failed"),
        AUTH_401,
        (404, ErrorEnvelope, "Project not found"),
        (
            409,
            ErrorEnvelope,
            "Data-classification score below the threshold, nothing saved to deploy, "
            "unsaved changes, or already deploying",
        ),
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
    body: DeployRequest,
) -> DeployStartedResponse:
    """Start a deploy. Returns 202 with the id to poll.

    THE DATA-CLASSIFICATION QUESTIONNAIRE IS THE GATE, and it is enforced here rather than
    anywhere a client could skip. There is no separate "score my answers" endpoint on
    purpose: one that merely reported a number would be advisory, and a caller that never
    asked for it would reach this route unscored. Scoring inside the deploying request
    makes clearing the gate and being deployed the same event.

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

    # THE CLASSIFICATION GATE, AND IT GOES FIRST FOR A REASON. Scoring is a pure function
    # of the body, so it costs nothing — while everything below it either reads state or,
    # in `_resolve_unsaved_work`, WRITES it by saving the workspace. Refusing after that
    # save would leave a side effect behind on a request the platform declined, which is
    # exactly what "a refused deploy changes nothing" is supposed to rule out.
    flags = body.answers.classification_flags()
    score = total_weight(flags)
    if not qualifies_for_deploy(flags):
        _log.info("deploy_refused_classification", project_id=str(project_id), score=score)
        # The score + full declaration (notes included) so the explanation the citizen
        # was compelled to write is not simply thrown away with the 409 — mirrors the
        # successful-deploy audit below. This is a backend trail, not a review queue:
        # nothing here notifies an admin or surfaces the refusal in a UI, which is why
        # `refusal_message` says "ask an administrator" rather than promising a review
        # will happen on its own.
        await append_audit(
            db,
            actor_id=user.id,
            action="deploy_refused_classification",
            resource_type="project",
            resource_id=str(project_id),
            detail={"classificationScore": score, "classification": body.answers.model_dump()},
        )
        await db.commit()
        raise AppApiError(
            status.HTTP_409_CONFLICT,
            refusal_message(flags),
            code="classification_below_threshold",
        )

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
        db, user=user, project_id=project_id, manager=manager, sandbox=sandbox, request=body
    )

    try:
        started = await service.start(
            db,
            user_id=user.id,
            app_id=app_id,
            project_id=project_id,
            conversation_id=target.conversation_id,
            classification=body.answers.model_dump(),
            classification_score=score,
        )
    except DeployNotPossibleError as exc:
        raise AppApiError(status.HTTP_409_CONFLICT, str(exc), code=exc.code) from None

    await append_audit(
        db,
        actor_id=user.id,
        action="deploy",
        resource_type="app",
        resource_id=str(app_id),
        detail={
            "deploymentId": str(started.deployment_id),
            "projectId": str(project_id),
            # What was declared, recorded on the gated action itself (ADR-0005). The
            # deployment row holds the same facts, but audit outlives it: an app deleted
            # after a bad deploy takes its `deployments` rows with it via CASCADE, and the
            # declaration that authorised the publish is exactly what a later review needs.
            "classificationScore": score,
            # The full declaration, explanation included — the explanation IS the
            # justification a reviewer would ask for, so recording the flags without it
            # would preserve the decision and lose the reasoning behind it.
            "classification": body.answers.model_dump(),
        },
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


@admin_router.post(
    "/{app_id}/unpublish",
    responses=error_responses(
        (404, ErrorEnvelope, "App not found"),
        (409, ErrorEnvelope, "A deploy is in flight, or this app has never been published"),
        (503, ErrorEnvelope, "The published container could not be removed"),
        *_ADMIN_AUTH,
    ),
)
async def unpublish(
    app_id: uuid.UUID,
    admin: CurrentSuperadmin,
    db: DbSession,
    remover: OptionalPublishedAppRemover,
) -> UnpublishResponse:
    """THE admin kill-switch (#113). Takes the published container down; leaves the app row,
    its per-project database and its Blob container completely untouched — a later Deploy
    brings it back at the same URL, because the container name is a pure function of the
    immutable app id and nothing about unpublishing constrains a future deployment row.

    NOT the citizen-facing case, and no submit-for-review lineage is touched — this is a
    separate, admin-only lever, same posture as `admin/router.py`'s `disable`.

    ORDER MATTERS, same discipline as `disable`: the in-flight check comes first because
    letting an unpublish through while a deploy is running would race that deploy's own
    `create_or_update` — a moment later, the "removed" container could simply reappear,
    silently undoing the admin's action. A 409 there is a refusal to leave in a state that
    lies about what just happened, not caution for its own sake.

    IDEMPOTENT: if the app is already unpublished, this returns 200 with the existing state
    and never touches Azure again — a repeat click cannot fail.

    FAILS LOUD, NOT BEST-EFFORT: `sweep_published_apps` is reused exactly as it exists
    (best-effort, never-raising) rather than duplicating a second delete path, but its
    return count is read back here — 0 swept means the delete did not actually happen, and
    `unpublished_at` is deliberately NOT written in that case. Retrying is safe, because
    `AcaPublishedApps.delete_app` is independently idempotent (an already-absent container
    is a no-op) — a partial failure never leaves the row and reality permanently
    disagreeing.
    """
    app = await db.get(AppRegistry, app_id)
    if app is None:
        raise AppApiError(status.HTTP_404_NOT_FOUND, "App not found.")

    if await store.in_flight(db, app_id=app_id) is not None:
        raise AppApiError(
            status.HTTP_409_CONFLICT,
            "A deploy is currently in progress for this app. Wait for it to finish, or stop "
            "it, before unpublishing — otherwise it may re-publish the app right after this "
            "removes it.",
            code="deploy_in_flight",
        )

    row = await store.last_successful(db, app_id=app_id)
    if row is None:
        raise AppApiError(
            status.HTTP_409_CONFLICT,
            "This app has never been published — there is nothing to unpublish.",
            code="never_published",
        )

    if row.unpublished_at is not None:
        # Idempotent: already down. No Azure call, so this branch cannot fail.
        return UnpublishResponse(
            app_id=str(app_id), deployment_id=str(row.id), unpublished_at=row.unpublished_at
        )

    swept = await sweep_published_apps([app_id], client=remover)
    if swept == 0:
        raise AppApiError(status.HTTP_503_SERVICE_UNAVAILABLE, _TEARDOWN_FAILED)

    now = datetime.now(UTC)
    settled = await store.unpublish(db, row.id, at=now)
    if not settled:
        # Lost a race with a concurrent unpublish of the same row — Azure is already torn
        # down (delete_app is idempotent, so the redundant call above was harmless), and
        # the other caller's write is what's on record. Report that, not this call's own
        # (unwritten) timestamp.
        await db.refresh(row)
        # `row.unpublished_at` is refreshed from the DB and the losing branch of the race
        # guarantees some caller set it — but that's not something a type checker can see
        # through a refresh, so `or now` is a belt-and-braces fallback, not the expected path.
        return UnpublishResponse(
            app_id=str(app_id), deployment_id=str(row.id), unpublished_at=row.unpublished_at or now
        )

    await append_audit(
        db,
        actor_id=admin.id,
        action="unpublish",
        resource_type="app",
        resource_id=str(app_id),
        detail={
            "deploymentId": str(row.id),
            "projectId": str(app.project_id),
            "containerAppName": row.container_app_name,
        },
    )
    await db.commit()

    _log.info("app_unpublished", app_id=str(app_id), deployment_id=str(row.id))
    return UnpublishResponse(app_id=str(app_id), deployment_id=str(row.id), unpublished_at=now)
