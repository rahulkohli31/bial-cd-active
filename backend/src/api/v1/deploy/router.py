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

TWO ROUTERS IN ONE FILE, AND TWO NAMESPACES. `router` (prefix `/projects`) is the
citizen-facing pair above; `admin_router` (prefix `/admin/apps`) is the superadmin-only
`unpublish` lever, kept in this file rather than `admin/router.py` because that file is
being edited by two other in-flight branches — mirrors `admin/router.py`'s own two-router
shape (`router` + `users_router`). Which FILE the code lives in and which URL it answers on
are independent decisions here: the lever sits under `/v1/admin/*` with every other
superadmin route regardless of the module it was convenient to write it in. Both are
registered separately in `api/v1/router.py`.

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
from src.db.models.deployment import Deployment
from src.db.models.user import User
from src.schemas import ADMIN_AUTH, AUTH_401, ErrorEnvelope, error_responses
from src.services.audit.log import append_audit
from src.services.build_sessions.manager import NoLiveSandboxError, SessionManager
from src.services.deploy import store
from src.services.deploy.classification import (
    qualifies_for_deploy,
    refusal_message,
    total_weight,
)
from src.services.deploy.names import published_app_name
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
#
# THE PREFIX IS `/admin/apps`, NOT `/apps`, AND THE FILE IT LIVES IN DOES NOT GET A VOTE.
# Keeping the code out of admin/router.py avoids a merge conflict; that was never a reason
# to change the URL, and an earlier revision of this router mistakenly carried both. Every
# superadmin-gated app lever in this codebase answers on `/v1/admin/apps/{app_id}/...`
# (admin/router.py:148), while `/v1/apps/*` is the citizen surface (apps/router.py:51),
# where every route is `user_id`-scoped and a cross-user id is a non-leaking 404. Mounting
# an admin lever there would give that prefix two different authorization contracts, hide
# it from any gateway/WAF/log filter keyed on `/v1/admin`, and split it off in OpenAPI —
# and URLs are public contract, so moving it afterwards is a breaking change. The portal's
# admin client is built entirely on `/api/admin/apps/*` (portal/src/utils/appRegistryApi.ts)
# and the edge rewrites `/api/X` -> `/v1/X` blindly, so this prefix is what a follow-up
# admin button already expects.
admin_router = APIRouter(prefix="/admin/apps", tags=["admin"])

_UNAVAILABLE = "Deploying is not switched on for this environment. Please tell an administrator."
_BUILD_IN_FLIGHT = "Your app is being built right now. Wait for that to finish, then deploy."
# NOT "could not be removed" — see the route. `sweep_published_apps` returns a count, and a
# zero collapses "ARM refused" together with "the delete is still running past our ceiling",
# whose outcome `await_lro` documents as genuinely unknown. Claiming removal failed would
# assert something nobody observed; this says only what is true, and points at the retry that
# settles it either way (`delete_app` is idempotent, so retrying is safe in both cases).
_TEARDOWN_UNCONFIRMED = "The takedown could not be confirmed. Retrying is safe and will settle it."


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
        (409, ErrorEnvelope, "A deploy is in flight, or this app has never been deployed"),
        # One entry, two meanings — `error_responses` rejects a duplicate status, so the
        # two are told apart by `error.code`, never by the prose: `publishing_unavailable`
        # (retrying can never help) vs `teardown_unconfirmed` (retrying is the right move).
        (
            503,
            ErrorEnvelope,
            "Publishing is not configured on this deployment "
            "(`publishing_unavailable`), or the takedown could not be confirmed "
            "(`teardown_unconfirmed`)",
        ),
        *ADMIN_AUTH,
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

    AN OPERATOR CONVENIENCE, NOT AN ENFORCEMENT LEVER, and the distinction matters against a
    hostile app. Nothing in `deploy_project` consults `unpublished_at` or `AppRegistry.status`,
    so the owner can republish at the same URL one click later. That is the right default for
    the case this exists for — an app misbehaving by accident, taken down while it is fixed —
    but it means this is NOT the answer to a compromised or data-leaking app. `disable` is:
    it fails closed by severing the database. Enforcement is deliberately left to #113's
    follow-up rather than smuggled in here.

    THE ACCOUNTABILITY ROW IS COMMITTED BEFORE AZURE IS CALLED, the opposite of `disable`'s
    ordering, and the inversion is deliberate rather than inherited. `disable` audits first so
    a failing side effect ROLLS THE AUDIT BACK — its side effect is a local `ALTER ROLE` that
    either lands in milliseconds or raises. This lever's side effect is an ARM long-running
    delete bounded at `provision_timeout_s` (300s) behind an edge gateway that gives up at
    twenty (see this module's docstring). The failure mode is therefore not "the side effect
    raised" but "this request never returns" — and a request that never returns cannot audit
    anything on its way out. So the trail is made durable FIRST: after that commit, the fact
    that a named superadmin pulled this lever on this app survives a 504, a worker recycle,
    and an ARM call that lands ten minutes later. What it deliberately does NOT claim is that
    the container is gone — `await_lro` raises on expiry precisely because the outcome is
    unknown, and an audit row asserting an outcome nobody observed would be worse than none.

    Committing there also RELEASES THE DB CONNECTION for the duration of the ARM call, rather
    than holding one idle-in-transaction for up to five minutes per concurrent admin.

    TWO AUDIT ACTIONS, and every request that is about to touch Azure writes the first before
    it does:
      `unpublish`             — an admin exercised the lever. One row per request that reached
                                the sweep, so two admins racing the same incident leave two
                                rows, correctly attributed, which is the point.
      `unpublish:unconfirmed` — the sweep came back empty, so this request never observed the
                                container go away. Written after the attempt row, mirroring
                                `deploy_refused_classification` above: audit the outcome,
                                commit, then raise. NOT `:failed` — see the sweep branch.
    A successful unpublish therefore writes ONE row, not two: the pre-ARM row already carries
    the whole ADR-0005 payload (who, what, which, when), and "it worked" is already durable in
    `unpublished_at` and the `app_unpublished` log line. One `unpublish` row with no
    `:unconfirmed` sibling and `unpublished_at` still NULL reads as "attempted, outcome
    unknown" — which is exactly what a 504 leaves behind, and exactly what `await_lro` can
    honestly prove. Paths that mutate nothing write nothing (the 404, both 409s, the
    already-down 200), matching this codebase's own rule that a no-op admin request is not an
    audited action.

    ORDER MATTERS, same discipline as `disable`: the unconfigured-publishing check goes first
    because it costs no query and an environment with `DEPLOY__*` unset has nothing to tear
    down; the in-flight check next, because letting an unpublish through while a deploy is
    running would race that deploy's own `create_or_update` — a moment later the "removed"
    container could simply reappear, silently undoing the admin's action. That check is
    check-then-act: a deploy can still start between it and the sweep, so the 409 NARROWS the
    window rather than closing it. It is a refusal to act on a state already known to be
    changing, not a guarantee about the state at the moment the sweep lands.

    THE ROW TO STAMP IS THE NEWEST ONE, NOT THE NEWEST SUCCEEDED ONE. The pipeline creates the
    container app at step 5 and only then awaits the revision, so an attempt that settles
    FAILED at step 6 leaves `pub-<app_id>` running, externally addressable, holding the app's
    database URL and Blob SAS, and billing. Resolving through `last_successful` would answer
    "never published" while exactly that container served traffic — and on a
    succeeded-then-unpublished-then-failed history it would take the already-down early return
    and leave the re-created container up. `latest_for_app` closes both. A missing row is still
    a safe 409: the container is only ever created by a pipeline that owns a deployment row,
    and rows leave only by CASCADE with the app itself (a 404 here), so no row provably means
    no container.

    IDEMPOTENT: if the newest attempt is already stamped, this returns 200 with the existing
    state and never touches Azure again — a repeat click cannot fail.

    FAILS LOUD, NOT BEST-EFFORT: `sweep_published_apps` is reused exactly as it exists
    (best-effort, never-raising) rather than duplicating a second delete path, but its
    return count is read back here — 0 swept means this request never observed the delete
    succeed, and `unpublished_at` is deliberately NOT written in that case. The count is a
    weak signal in BOTH directions, and the route is written to over-claim in neither: a
    non-zero count means "no error" rather than "something was deleted", because `delete_app`
    no-ops on an absent container and still counts; a zero means "not observed" rather than
    "failed", because the sweep collapses a terminal `AcaError` and an `AcaTransientError`
    from ceiling expiry into the same number. Both readings are the right ones for a lever
    whose job is to guarantee absence rather than to prove authorship of it. Retrying is safe
    either way, because `AcaPublishedApps.delete_app` is independently idempotent — a partial
    failure never leaves the row and reality permanently disagreeing.
    """
    # First, and before any query: an environment with `DEPLOY__*` unset has no publish plane
    # at all. Without this the `None` flows into `sweep_published_apps`, which re-resolves the
    # singleton, catches `DeployNotConfiguredError` and returns 0 — landing in the
    # unconfirmed-teardown branch below, which invites a retry that can never work here. This
    # is the one 503 on this route that is TERMINAL, hence the distinct `code`: the other says
    # "try again", and a client cannot tell them apart from the prose. Both sibling routes in
    # this module open with the same check against the same constant, whose "tell an
    # administrator" is the right advice for exactly the reason "please try again" is not.
    if remover is None:
        raise AppApiError(
            status.HTTP_503_SERVICE_UNAVAILABLE, _UNAVAILABLE, code="publishing_unavailable"
        )

    app = await db.get(AppRegistry, app_id)
    if app is None:
        raise AppApiError(status.HTTP_404_NOT_FOUND, "App not found.")

    if await store.in_flight(db, app_id=app_id) is not None:
        raise AppApiError(
            status.HTTP_409_CONFLICT,
            "A deploy is currently in progress for this app. Wait for it to finish before "
            "unpublishing — otherwise it may re-publish the app right after this removes it. "
            "If the deploy is wedged, an administrator can clear it with reconcile-deploys.",
            code="deploy_in_flight",
        )

    # The NEWEST attempt, whatever its status — see the docstring. `None` here is the one
    # state in which no container can exist, so it stays a refusal rather than a blind sweep.
    row = await store.latest_for_app(db, app_id=app_id)
    if row is None:
        raise AppApiError(
            status.HTTP_409_CONFLICT,
            "This app has never been deployed — there is nothing to unpublish.",
            code="never_deployed",
        )

    if row.unpublished_at is not None:
        # Idempotent: already down. No Azure call and no state change, so this branch cannot
        # fail and does not audit.
        return UnpublishResponse(
            app_id=str(app_id), deployment_id=str(row.id), unpublished_at=row.unpublished_at
        )

    _log.info("app_unpublish_requested", app_id=str(app_id), deployment_id=str(row.id))
    await append_audit(
        db,
        actor_id=admin.id,
        action="unpublish",
        resource_type="app",
        resource_id=str(app_id),
        detail={
            "deploymentId": str(row.id),
            "projectId": str(app.project_id),
            # DERIVED, not read off the row. `container_app_name` is written by the `_advance`
            # that runs AFTER `create_or_update` returns, so a deploy that died inside that
            # call leaves the column NULL over a container that exists — and this name is the
            # one `delete_app` actually targets, so the audit records what was really acted on.
            "containerAppName": published_app_name(app_id),
            # Ids and enum labels only (never user data in the blob). The status is here
            # because tearing down behind a FAILED row is the interesting case, and an
            # operator should not have to join back to `deployments` to notice it.
            "deploymentStatus": row.status.value,
        },
    )
    # THE DURABILITY BOUNDARY. Everything above is re-derivable; nothing below it is. `app`
    # and `row` survive this commit intact and IO-free (`expire_on_commit=False`, db/base.py),
    # so no re-read is needed — but they are now snapshots, which is why `store.unpublish`'s
    # guarded UPDATE, not `row.unpublished_at`, remains the authority on who won the race.
    await db.commit()

    if await sweep_published_apps([app_id], client=remover) == 0:
        # UNCONFIRMED, NOT FAILED, and the distinction is the same one this route's audit
        # discipline is built on. `sweep_published_apps` collapses every exception into a
        # count, so a zero means "we did not observe a success" — which covers a terminal
        # `AcaError` (ARM refused; it really is still up) AND an `AcaTransientError` from
        # `await_lro`'s ceiling expiry, whose docstring says the outcome is genuinely unknown
        # because "the operation may still land". Recording that as a confirmed failure would
        # be the same sin as recording an unobserved success, and the far likelier one here:
        # the ceiling is 300s and the gateway gives up at 20, so a slow-but-fine delete is
        # exactly what lands in this branch. `unpublished_at` stays NULL either way, which is
        # the conservative choice — a retry re-attempts the delete (idempotent) and settles
        # the row, whereas stamping it now could mark an app down that is still serving.
        _log.warning(
            "app_unpublish_teardown_unconfirmed", app_id=str(app_id), deployment_id=str(row.id)
        )
        await append_audit(
            db,
            actor_id=admin.id,
            action="unpublish:unconfirmed",
            resource_type="app",
            resource_id=str(app_id),
            detail={"deploymentId": str(row.id), "reason": "teardown_unconfirmed"},
        )
        await db.commit()
        raise AppApiError(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            _TEARDOWN_UNCONFIRMED,
            code="teardown_unconfirmed",
        )

    now = datetime.now(UTC)
    if not await store.unpublish(db, row.id, at=now):
        # ZERO ROWS TOUCHED HAS TWO CAUSES, and only one of them is the race. Either another
        # caller stamped this row while we were in ARM, or the row is GONE — a concurrent
        # `DELETE /v1/admin/apps/{id}` cascades `deployments` away, and the whole window
        # between the pre-sweep commit and here is minutes wide, which is exactly when an
        # admin dealing with a bad app is most likely to reach for delete next.
        #
        # `db.get(..., populate_existing=True)`, never `db.refresh(row)`: refresh raises
        # `ObjectDeletedError` on a vanished row, which escapes as an undocumented 500 —
        # and it would do so on a request whose teardown actually SUCCEEDED, which is the
        # worst possible moment to look like a server fault.
        current = await db.get(Deployment, row.id, populate_existing=True)
        if current is None:
            # The app was deleted mid-flight. Its own teardown sweeps the same container, so
            # the admin's intent holds either way — but there is no longer a deployment to
            # report, and inventing one would be a lie. 404 is already this route's documented
            # answer for "no such app", and it is now true.
            _log.info("app_unpublish_app_deleted_mid_flight", app_id=str(app_id))
            raise AppApiError(status.HTTP_404_NOT_FOUND, "App not found.")
        # Lost a race with a concurrent unpublish of the same row — Azure is already torn
        # down (`delete_app` is idempotent, so the redundant call above was harmless), and
        # the other caller's write is what's on record. Report THAT, not this call's own
        # unwritten timestamp. Still a 200: the world is exactly as the admin asked for it to
        # be, and answering 409 for a state the repeat-click branch above answers 200 for
        # would make the status depend on timing rather than on state. This request is already
        # audited — its `unpublish` row was committed before the sweep — which is precisely
        # the "two admins, one audit row" gap that ordering closes.
        #
        # The losing branch of the race guarantees some caller set the timestamp, but that is
        # not something a type checker can see through a re-read, so `or now` is
        # belt-and-braces rather than the expected path.
        settled_at = current.unpublished_at or now
        await db.commit()
        return UnpublishResponse(
            app_id=str(app_id), deployment_id=str(row.id), unpublished_at=settled_at
        )

    await db.commit()
    _log.info("app_unpublished", app_id=str(app_id), deployment_id=str(row.id))
    return UnpublishResponse(app_id=str(app_id), deployment_id=str(row.id), unpublished_at=now)
