"""One-click deploy, its owner-facing production levers, and the admin kill-switch.

WHY THIS EXISTS

`POST /v1/projects/{id}/deploy` answers with one of TWO statuses, and the difference is the
whole point of the gate: **202** when a deploy actually started (the work is detached and
the client polls `GET /v1/projects/{id}/deployment`) and **200** when the request was ROUTED
into the admin queue instead, where nothing was started and there is nothing to poll. Naming
only the 202 would read as a promise the route does not make on every path. The 202 is not a
style choice: a deploy runs for minutes and the edge gateway times out at twenty seconds.

THE PUBLISH GATE IS A PRECEDENCE LADDER, AND THIS IS WHERE THE TWO LINEAGES JOIN.
`deploy_project` resolves the shipping commit, reads the platform's own stored review of it,
merges that with the citizen's declaration (stricter-of per question), and lands on exactly
one of four outcomes in precedence order: refuse, PUBLISH, DEFER to the pipeline's own
re-check, or ROUTE into the admin approve queue. The ladder is PROSE plus `# --- rule N ---`
markers in `deploy_project`'s body; there is no `_LADDER` constant. THE INVARIANT ON THAT
LAST OUTCOME: a routed deploy leaves the app in the queue at exactly the version examined,
and publishes nothing — on BOTH sides of the 202. `mark-deployed` stays on the runbook
lineage; approval of a `self_publish` submission is consumed HERE, by the citizen.

NO AUTHENTICATION ON THE PUBLISHED APP, deliberately out of scope: until that lands, anyone
with the URL can open any deployed app. `ingress` is `external` (`deploy/config.py`); whether
the managed environment's VNet integration restricts that to the corporate network is
UNCONFIRMED, so treat a deployed app as reachable on the public internet."""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
import structlog
from fastapi import APIRouter, Response, status
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import CurrentUser, DbSession, OptionalStorage
from src.api.deps_rbac import CurrentSuperadmin
from src.api.v1.build_sessions.deps import OptionalSandbox, RequireCsrf, SessionManagerDep
from src.api.v1.classification.deps import ReviewService
from src.api.v1.deploy.deps import OptionalDeployService, OptionalPublishedAppRemover
from src.api.v1.deploy.schemas import (
    ApprovalState,
    DeploymentResponse,
    DeployRequest,
    DeployRoutedResponse,
    DeployStartedResponse,
    PublishState,
    RestartStartedResponse,
    SavedState,
    TakedownResponse,
    UnpublishResponse,
    compute_publish_state,
)
from src.api.v1.live_build import refuse_while_build_session_live
from src.core.errors import AppApiError
from src.core.redaction import redact_secrets
from src.db.models.app_registry import AppRegistry, ApprovalRoute, AppStatus
from src.db.models.deployment import Deployment, DeploymentStatus
from src.db.models.user import User
from src.schemas import ADMIN_AUTH, AUTH_401, ErrorEnvelope, error_responses
from src.services.approvals.submit import submit_app_for_review
from src.services.audit.log import append_audit
from src.services.build_sessions.manager import NoLiveSandboxError, SessionManager
from src.services.classification.merge import merge_questions
from src.services.deploy import store
from src.services.deploy.classification import total_weight

# The gate's shared reading — the stored review situated against H, the merge inputs, the
# declaration document and the one audit action. Extracted because the detached pipeline is
# a second writer of all four (the drift re-check produces the same document for the same
# queue) and a service cannot import the route that calls it.
from src.services.deploy.gate import (
    ReviewAtHead,
    append_gate_audit,
    declaration_document,
    merge_inputs,
    review_at_head,
)
from src.services.deploy.names import published_app_name
from src.services.deploy.service import (
    FAIL_NO_SNAPSHOT,
    DeployNotPossibleError,
    LiveRevision,
    VersionRecheck,
    deployment_for_app,
)
from src.services.deploy.teardown import sweep_published_apps
from src.services.projects.resolve import owned_project_or_404
from src.services.sandbox import SandboxClient
from src.services.storage import (
    ObjectStorage,
    StorageError,
    head_sha_from_metadata,
    snapshot_key,
)

_log = structlog.get_logger()

router = APIRouter(prefix="/projects", tags=["deploy"])

# Separate router for the admin app-lever, keyed on app_id like every other
# superadmin action (admin/router.py's `/{app_id}/disable`, `/{app_id}/enable`, …) rather
# than this file's own citizen-facing `/projects/{project_id}/...` convention — an admin
# operates on an app, not a project they own. Lives here rather than in admin/router.py
# because that file is being edited by two other in-flight branches; mirrors
# admin/router.py's own two-router-per-file shape (`router` + `users_router`).
#
# THE PREFIX IS `/admin/apps`, NOT `/apps`, AND THE FILE IT LIVES IN DOES NOT GET A VOTE. Keeping
# the code out of admin/router.py avoids a merge conflict; that is never a reason to change the
# URL. Every superadmin-gated app lever in this codebase answers on `/v1/admin/apps/{app_id}/...`
# (`admin/router.py`'s `APIRouter(prefix="/admin/apps", ...)`), while `/v1/apps/*` is the citizen
# surface (`apps/router.py`'s `APIRouter(prefix="/apps", ...)`), where every route is
# `user_id`-scoped and a cross-user id is a non-leaking 404. Mounting an admin lever there would
# give that prefix two different authorization contracts, hide it from any gateway/WAF/log filter
# keyed on `/v1/admin`, and split it off in OpenAPI — and URLs are public contract, so moving it
# afterwards is a breaking change. The portal's admin client is built entirely on
# `/api/admin/apps/*` (portal/src/utils/appRegistryApi.ts) and the edge rewrites `/api/X` ->
# `/v1/X` blindly, so this prefix is what a follow-up admin button already expects.
# This file therefore defines TWO routers — `router` above is the citizen-facing pair, this one
# carries the superadmin-only `unpublish` lever — and `api/v1/router.py` registers them
# separately. Which file the code lives in and which URL it answers on are independent here.
admin_router = APIRouter(prefix="/admin/apps", tags=["admin"])

_UNAVAILABLE = "Deploying is not switched on for this environment. Please tell an administrator."
_BUILD_IN_FLIGHT = "Your app is being built right now. Wait for that to finish, then deploy."
# With storage down, the queue AND the pipeline are equally out of reach (both read
# the same bundle) — so this is honest for every branch, and retrying settles it.
_STORAGE_DOWN = "Publishing isn't possible right now. Please try again in a moment."
_NOTHING_TO_DEPLOY = "There is nothing to deploy yet — build something and save it first."
_DISABLED_MSG = "This app has been disabled by an administrator and cannot be published."
_WAITING_MSG = (
    "This version is already waiting for an administrator's review — "
    "withdraw it if you need to submit a different one."
)
_EXPLANATION_REQUIRED = (
    "This app handles higher-sensitivity data — please explain what it does "
    "with it before sending it for review."
)
_ROUTED_MSG = (
    "Your app was sent to an administrator for review. You'll be able to publish "
    "this exact version once it's approved."
)
_SNAPSHOT_MOVED_MSG = (
    "Your app was saved again while this request was being decided, so nothing was "
    "submitted. Try again to publish the version that's saved now."
)
_RESTART_NEVER_DEPLOYED = "This app has never been deployed, so there is nothing to restart."
_RESTART_NOT_LIVE = (
    "This app isn't running in production right now, so there is nothing to restart. "
    "Deploy it to publish the version you have saved."
)
_RESTART_TAKEN_OFFLINE = (
    "This app has been taken offline. Publish again to put it back — a restart only "
    "recycles something that is already running."
)
_RESTART_DISABLED = "This app has been disabled by an administrator and cannot be restarted."
_BUSY_MSG = "Something is already running for this app. Wait for it to finish, then try again."
_TAKEDOWN_NEVER_DEPLOYED = "This app has never been deployed, so there is nothing to take down."
_TAKEDOWN_WHILE_DEPLOYING = (
    "A deploy is running for this app right now. Wait for it to finish before taking the app "
    "down — otherwise it may publish the app again moments after this removes it."
)
_TAKEDOWN_DONE = (
    "Your app is no longer running in production. Everything it holds — its chats, its data "
    "and its files — is kept, and Publish again puts it back."
)
# Appended, never substituted: a take-down withdraws nothing, and an owner who has a version
# waiting should not have to discover that by going to look.
_TAKEDOWN_REVIEW_UNTOUCHED = " The version waiting for an administrator's review is untouched."

# NOT "could not be removed" — see the route. `sweep_published_apps` names the ids that
# SURVIVED, and a survivor collapses "ARM refused" together with "the delete is still running
# past our ceiling", whose outcome `await_lro` documents as genuinely unknown. Claiming removal
# failed would assert something nobody observed; this says only what is true, and points at the
# retry that settles it either way (`delete_app` is idempotent, so retrying is safe in both).
_TEARDOWN_UNCONFIRMED = "The takedown could not be confirmed. Retrying is safe and will settle it."


@router.post(
    "/{project_id}/deploy",
    response_model=DeployStartedResponse | DeployRoutedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[RequireCsrf],
    responses={
        200: {
            "model": DeployRoutedResponse,
            "description": "Routed to an administrator for review — an outcome, not a failure",
        },
        **error_responses(
            (403, ErrorEnvelope, "CSRF check failed"),
            AUTH_401,
            (404, ErrorEnvelope, "Project not found"),
            (
                409,
                ErrorEnvelope,
                "Disabled (`app_disabled`), already waiting for review "
                "(`waiting_for_review`, the pending state in `error.detail`), nothing "
                "saved to deploy, unsaved changes (`unsaved_changes`), a build running "
                "for this app (`build_in_flight` — wait and retry), already deploying "
                "(`deploy_in_flight`), or a save landed mid-request (`snapshot_moved`)",
            ),
            (
                422,
                ErrorEnvelope,
                "A weighted Yes on the merged answers with no explanation "
                "(`explanation_required`); an incomplete body is FastAPI's own "
                'validation 422 with the `{"detail": [...]}` shape instead',
            ),
            (
                503,
                ErrorEnvelope,
                "Object storage is unavailable (`storage_unavailable` — and so is "
                "publishing), or deploying is unconfigured — checked only once a "
                "branch actually needs the pipeline, so routing works without it",
            ),
        ),
    },
)
async def deploy_project(
    project_id: uuid.UUID,
    user: CurrentUser,
    db: DbSession,
    manager: SessionManagerDep,
    sandbox: OptionalSandbox,
    service: OptionalDeployService,
    storage: OptionalStorage,
    reviews: ReviewService,
    body: DeployRequest,
    response: Response,
) -> DeployStartedResponse | DeployRoutedResponse:
    """Publish, or route to a person — THE PRECEDENCE LADDER. Returns 202 with the id to poll
    when the pipeline started, 200 with the routed outcome when the app went to the admin queue.

    A 202 IS NOT A PROMISE TO PUBLISH — read it as "the id to watch". UNSAVED WORK IS REFUSED BY
    DEFAULT: a deploy ships the last SAVED version, and `saveFirst` is the explicit "save and
    deploy" the citizen opts into. `dirty` is TRI-STATE and unknown is not dirty: with no live
    workspace there is nothing to compare against, and the saved version is the only version."""
    # Every cell of the state table resolves to exactly one branch, evaluated in order against
    # `H`, the commit about to ship (resolved from the snapshot blob's metadata stamp AFTER the
    # optional save below — the decision must be about the version that will actually leave):
    #
    #   1.  disabled                                     -> refuse
    #   2.  pending                                      -> refuse: waiting
    #   3.  approved AND approved pin == H
    #         AND lineage == self_publish                -> PUBLISH  (pre-feature approvals
    #                                                       are inert here)
    #   3a. THIS request saved first AND the stored
    #         review is stamped a commit other than H    -> DEFER to the pipeline's re-check
    #   4.  the stored review for H anything other than
    #         genuinely COMPLETE (absent, stale, still
    #         running, aged out, failed, or complete-
    #         but-flagged-partial)                       -> ROUTE
    #   5.  rejected                                     -> ROUTE    (sticky, whatever a fresh
    #                                                       review says)
    #   6.  any weighted category merges to Yes          -> ROUTE
    #   7.  otherwise                                    -> PUBLISH
    #
    # Rule 3 sits ABOVE rule 6 deliberately: the review keeps returning the same Yes for the same
    # code, so without the override a flagged app would route forever and the flow would never
    # terminate. Rule 3a is narrow on purpose — only a save THIS request performed defers, and
    # rules 1, 2 and 5 are status checks evaluated before that save, so a disabled, pending or
    # rejected app never reaches the pipeline by that door. Rule 4 says COMPLETE (status, the
    # runner's own completeness signal, AND the age ceiling) because a review still running is
    # neither absent nor failed — falling through to rule 6 there would publish on the citizen's
    # word alone, the exact bypass this ladder exists to close, reachable by answering six
    # questions faster than the review lands.
    #
    # THE GATE READS THE STORED REVIEW, NEVER THE BROWSER'S COPY: the request schema has no
    # review field, unknown body keys are dropped at the boundary, and both answer sets plus the
    # merge outcome are computed right here, server-side.
    #
    # THE SAVE RUNS BEFORE THE GATE, ON PURPOSE. The ladder's version-dependent rules must run
    # against the post-save H, and saving is what the citizen explicitly asked for on that path —
    # so "a refused deploy changes nothing" is NOT the invariant here. The one that holds: a
    # ROUTED deploy leaves the app in the queue at exactly the version examined, and publishes
    # nothing; the plain REFUSALS (rules 1 and 2) are decided before the save and change nothing.
    #
    # On rule 3a the decision is deliberately unfinished when this route answers: the pipeline
    # reviews the version it extracted and may route it into the queue instead of shipping it,
    # long after the response left. The invariant above covers that case unchanged.
    await owned_project_or_404(db, user.id, project_id)

    # The full registry row, not `deploy_target`'s two-column projection: the ladder
    # reads status, the approval pin, the lineage and the rejection note.
    app_row = (
        await db.execute(
            sa.select(AppRegistry).where(
                AppRegistry.project_id == project_id,
                AppRegistry.user_id == user.id,
            )
        )
    ).scalar_one_or_none()
    if app_row is None:
        # The SAME code `_shipping_head` raises below for the other "nothing saved"
        # site, and the same string the pipeline itself settles a `Deployment` row with
        # when it extracts a snapshot that turns out not to exist (`FAIL_SNAPSHOT_MOVED`'s
        # own precedent for sharing one string across an immediate refusal and a later
        # settlement of the same fact) — so a client asserts on `error.code` once,
        # rather than parsing this sentence at two call sites that mean the same thing.
        raise AppApiError(status.HTTP_409_CONFLICT, _NOTHING_TO_DEPLOY, code=FAIL_NO_SNAPSHOT)

    flags = body.answers.classification_flags()
    # The citizen's explanation passes through the shared redactor before it is
    # stored anywhere — it lands in the same records the review's own text is kept clean of.
    notes = (body.answers.notes or "").strip()
    explanation = redact_secrets(notes) if notes else None

    # --- rules 1 and 2: plain refusals, decided BEFORE the save -----------------------
    if app_row.status is AppStatus.DISABLED:
        await _audit_gate(
            db,
            user=user,
            app_id=app_row.id,
            project_id=project_id,
            decision="refused",
            rule="disabled",
            extra={"citizenAnswers": flags, "explanation": explanation},
        )
        await db.commit()
        raise AppApiError(status.HTTP_409_CONFLICT, _DISABLED_MSG, code="app_disabled")

    if app_row.status is AppStatus.PENDING:
        # The structured 409: the state, the submitted version, and the rejection
        # note when one exists — everything both citizen surfaces need to render the
        # waiting state without a second call.
        pending = {
            "status": AppStatus.PENDING.value,
            "submittedSha": app_row.source_commit_sha,
            "submittedAt": (
                app_row.submitted_at.isoformat() if app_row.submitted_at is not None else None
            ),
            "rejectionNote": app_row.rejection_note,
        }
        await _audit_gate(
            db,
            user=user,
            app_id=app_row.id,
            project_id=project_id,
            decision="refused",
            rule="pending",
            extra={"citizenAnswers": flags, "explanation": explanation},
        )
        await db.commit()
        raise AppApiError(
            status.HTTP_409_CONFLICT, _WAITING_MSG, code="waiting_for_review", detail=pending
        )

    # Rule 5's FACT, read before the save like rules 1 and 2:
    # a rejected app never defers through rule 3a and never publishes — but its ROUTE
    # still happens below, after the merge, so the queue item carries the record.
    #
    # Read off `rejection_standing`, NOT off `status`. A rejected app that publishes
    # routes (REJECTED -> PENDING) and may then be withdrawn (PENDING -> DRAFT), so by
    # the next request `status` has forgotten the refusal entirely — two citizen calls
    # that laundered a rejection into an unattended publish. The flag is cleared by
    # `approve` alone, which is what "an administrator lifts it" means.
    rejected = app_row.rejection_standing

    # A build session writing files while the snapshot is taken would ship a tree that
    # never coherently existed: valid bytes, wrong app, undetectable afterwards.
    await refuse_while_build_session_live(
        user.id,
        conflict_message=_BUILD_IN_FLIGHT,
        app_id=app_row.id,
        # Every refusal on this route carries a code and the 409 list enumerates them;
        # without one an agent cannot tell "wait, a build is running" from a permanent
        # conflict except by reading prose.
        conflict_code="build_in_flight",
    )

    # The gate runs AFTER this, deliberately: the version-dependent rules must run
    # against the post-save H. `resolved.saved` is rule 3a's "this request saved first"
    # fact; `resolved.head_sha` is the commit the resolution landed on.
    resolved = await _resolve_unsaved_work(
        db, user=user, project_id=project_id, manager=manager, sandbox=sandbox, request=body
    )
    saved = resolved.saved

    # Storage is the one dependency EVERY remaining branch needs — the queue copy and
    # the pipeline read the same bundle, so with it down publishing and routing are
    # equally unavailable and nobody is stranded behind a gate that works while the
    # pipeline doesn't. The deploy service, by contrast, is checked only where
    # a branch actually starts the pipeline — routing must work without it.
    if storage is None:
        raise AppApiError(
            status.HTTP_503_SERVICE_UNAVAILABLE, _STORAGE_DOWN, code="storage_unavailable"
        )
    head_sha = await _shipping_head(storage, app_row.id)

    # TWO READINGS OF THE SAME TREE, AND THEY MUST AGREE. The stamp above and the
    # resolution's own commit are written by the same save — `snapshot.write_snapshot`
    # stamps the blob with exactly the head it parsed out of the bundle it uploaded — so a
    # disagreement is not ambiguity, it is a THIRD save landing between the two reads. The
    # ladder would then decide about one version while the citizen asked to publish
    # another. Nothing has happened yet on this path (no claim, no copy, no row), so this
    # is the cheapest possible place to refuse; the pipeline's own expected-commit
    # assertion closes the rest of the window, after the claim.
    if resolved.head_sha is not None and head_sha is not None and resolved.head_sha != head_sha:
        _log.warning(
            "publish_gate_snapshot_moved_before_gate",
            app_id=str(app_row.id),
            resolved=resolved.head_sha,
            stamped=head_sha,
        )
        raise AppApiError(status.HTTP_409_CONFLICT, _SNAPSHOT_MOVED_MSG, code="snapshot_moved")

    # THE STORED REVIEW, read through the same service the review routes resolve — by
    # app, situated against H by `gate.review_at_head`. Never a browser-supplied copy.
    readout = await reviews.read(db, app_id=app_row.id)
    review: ReviewAtHead = review_at_head(readout, head_sha)

    # Both answer sets and the merge outcome, computed server-side inside this request —
    # the portal's local copy drives affordances and never decides. The merge runs on
    # every branch below (not just rule 6) because the record of EVERY
    # decision must carry the effective answers and the differences.
    merged = merge_questions(merge_inputs(flags, review))
    declaration = declaration_document(
        head_sha=head_sha, citizen=flags, explanation=explanation, review=review, merged=merged
    )
    score = total_weight(flags)

    # --- rule 3: the approval override -------------------------------------------
    # The pinned commit must equal H and the lineage must be self_publish — which is what
    # makes approvals predating this feature inert here: the 0030 backfill marked
    # them runbook, and a runbook approval authorises the manual go-live runbook only.
    if (
        app_row.status is AppStatus.APPROVED
        and app_row.approval_route is ApprovalRoute.SELF_PUBLISH
        and head_sha is not None
        and app_row.approved_commit_sha == head_sha
    ):
        return await _start_pipeline(
            db,
            service=service,
            user=user,
            app_row=app_row,
            project_id=project_id,
            body=body,
            score=score,
            declaration=declaration,
            expected_commit_sha=head_sha,
            decision="published",
            rule="approved_override",
        )

    # The explanation is obliged exactly when the MERGED answers would route
    # (a Public-Data-only Yes carries no weight and needs none; an approved app already
    # answered it — rule 3 sits above). A 422, not a scoring refusal: an unexplained
    # weighted Yes is an INCOMPLETE submission, not a rejected one — and it is not a gate
    # outcome either, so it deliberately writes no `publish_gate` row.
    #
    # THIS SITS ABOVE RULE 3a, and the order is the point. `merged` here is the citizen's
    # OWN declaration (on the save-and-publish path the stored review is stamped the
    # pre-save commit, so it contributes nothing), and a citizen's weighted Yes is known
    # at request time on every branch. Below 3a it was skipped exactly there: the defer
    # returned first, the pipeline re-merged the same answers, found the same weighted
    # Yes — review verdicts can only ADD Yes — and filed the queue item with
    # `citizen.explanation: null`, handing an administrator a flagged app with nothing
    # written about it. Rule 3 stays exempt above; the drift case is untouched, because a
    # category only the RE-CHECK raises is not in `merged` at request time.
    # `explanation_owed`, NOT `any_weighted_yes`: the two came apart when a dispute the
    # citizen has no surface for became a routing reason. A Tier A hit the review
    # overruled, or a Yes the review discarded, both route — but the form showed the review
    # answering No on that category, so demanding an explanation would refuse the citizen
    # over a fact nothing has told them, and no answer they could type would satisfy it.
    # Routing still keys off the full weighted Yes below; only the OBLIGATION narrows.
    if merged.explanation_owed and explanation is None:
        # Name the categories that oblige the explanation, the way the waiting-for-review
        # 409 above carries the pending state: the form has to mark the fields it is
        # asking about, and these are questionnaire keys the citizen already sees.
        raise AppApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            _EXPLANATION_REQUIRED,
            code="explanation_required",
            detail={
                "weightedYesKeys": [
                    question.key
                    for question in merged.questions
                    if question.weighted_yes and not question.disputed_only
                ]
            },
        )

    # --- rule 3a: the save-and-publish defer ---------------------------------------
    # Narrow on purpose: only a save THIS request performed, only when a stored review
    # exists stamped some other commit, and never for a rejected app (rule 5's status was
    # read above). Without this branch rule 4 would route every single save-and-publish,
    # because a fresh save always moves H off the stored stamp. This branch neither
    # routes nor refuses — it starts the pipeline and lets the pipeline's own re-check
    # decide.
    #
    # THE SEAM THIS OPENS IS CLOSED DOWNSTREAM. `service.start` carries the expected commit
    # AND, on this branch alone, a `VersionRecheck`: the pipeline asserts the tree it
    # extracts is `head_sha`, then reviews THAT version as its first step, before packing,
    # and re-runs rules 4-7 against the answer that review gives. DEFERRING IS NOT A PASS
    # — a weighted merged Yes still routes, exactly as rule 6 below would have routed it;
    # the only thing this branch skips is deciding on a review of the wrong version. The
    # declaration below travels along as the record of what was submitted.
    if (
        saved
        and not rejected
        and head_sha is not None
        and readout is not None
        and readout.review.head_sha != head_sha
    ):
        return await _start_pipeline(
            db,
            service=service,
            user=user,
            app_row=app_row,
            project_id=project_id,
            body=body,
            score=score,
            declaration=declaration,
            expected_commit_sha=head_sha,
            recheck=VersionRecheck(
                answered_about=readout.review.head_sha, declaration=declaration
            ),
            decision="deferred_to_pipeline",
            rule="saved_over_stale_review",
            extra={"staleReviewSha": readout.review.head_sha},
        )

    # --- rule 4: no genuinely-COMPLETE review for H -> ROUTE, whatever was answered
    if not review.complete:
        return await _route_to_review(
            db,
            storage,
            user=user,
            app_row=app_row,
            project_id=project_id,
            head_sha=head_sha,
            declaration=declaration,
            rule="review_not_current",
            response=response,
        )

    # --- rule 5: a rejection is sticky — an administrator lifts it, a re-roll never
    if rejected:
        return await _route_to_review(
            db,
            storage,
            user=user,
            app_row=app_row,
            project_id=project_id,
            head_sha=head_sha,
            declaration=declaration,
            rule="rejection_standing",
            response=response,
        )

    # --- rule 6: any weighted category merged to Yes -> ROUTE -----------------------
    if merged.any_weighted_yes:
        return await _route_to_review(
            db,
            storage,
            user=user,
            app_row=app_row,
            project_id=project_id,
            head_sha=head_sha,
            declaration=declaration,
            rule="weighted_yes",
            response=response,
        )

    # --- rule 7: nothing weighted from either side -> PUBLISH, unattended ----------
    return await _start_pipeline(
        db,
        service=service,
        user=user,
        app_row=app_row,
        project_id=project_id,
        body=body,
        score=score,
        declaration=declaration,
        expected_commit_sha=head_sha,
        decision="published",
        rule="all_clear",
    )


@dataclass(frozen=True)
class _ResolvedWork:
    """What the unsaved-work resolution settled on.

    `saved` is ladder rule 3a's "this request saved first" fact, which must mean a real write —
    `saveFirst` on an already-clean workspace saves nothing and defers nothing. `head_sha` is
    THE COMMIT THE RESOLUTION RESOLVED TO: the one the save landed at, or the one the workspace
    was already level with. `None` means the resolution has no opinion (no sandbox runtime at
    all, or a save whose head could not be read), which is not a disagreement."""

    saved: bool
    head_sha: str | None


async def _resolve_unsaved_work(
    db: AsyncSession,
    *,
    user: User,
    project_id: uuid.UUID,
    manager: SessionManager,
    sandbox: SandboxClient | None,
    request: DeployRequest,
) -> _ResolvedWork:
    """Save first if asked, refuse if not — never deploy over unsaved work silently, and
    report the commit that leaves.

    The answer is the COMMIT it resolved to, not a bare "did we save", so the version this
    request is about is named by the step that settled it rather than inferred afterwards
    from a blob header. The caller cross-checks it against the shipping stamp and threads
    it into the pipeline as the expected commit."""
    if sandbox is None:
        # No sandbox runtime configured at all, so there is no live workspace that could be
        # ahead of the saved version. Nothing to compare, nothing to refuse — the saved
        # version IS the version. Same reading as `dirty=None` below.
        return _ResolvedWork(saved=False, head_sha=None)
    state = await manager.project_save_state(db, user, project_id, sandbox_client=sandbox)
    if not state.dirty:
        # Clean (or UNKNOWN — `dirty=None` is not dirty here, see the route's docstring):
        # the version already saved is the version that ships, and the save-state read
        # already knows which commit that is.
        return _ResolvedWork(saved=False, head_sha=state.saved_head)
    if not request.save_first:
        raise AppApiError(
            status.HTTP_409_CONFLICT,
            "You have changes that are not saved yet. Save them first, or choose "
            "'Save and deploy'.",
            code="unsaved_changes",
        )
    try:
        outcome = await manager.save_project_snapshot(db, user, project_id, sandbox_client=sandbox)
    except NoLiveSandboxError:
        # The workspace went away between the dirty check and the save. The saved version is
        # intact, so this is not fatal — but it IS a different deploy from the one asked for,
        # so say so rather than shipping the older tree silently.
        raise AppApiError(
            status.HTTP_409_CONFLICT,
            "Your workspace stopped running before the changes could be saved, so there was "
            "nothing new to deploy. Your last saved version is intact.",
        ) from None
    return _ResolvedWork(saved=True, head_sha=outcome.head_sha)


# --- the ladder's machinery ----------------------------------------------------------


async def _shipping_head(storage: ObjectStorage, app_id: uuid.UUID) -> str | None:
    """H — the commit this request is about to ship, from the snapshot blob's metadata stamp.

    One `head()`, never an extraction: the extract helper downloads the whole bundle before
    consulting its cache, and the pipeline re-derives the real head from the tree anyway. None
    means the saved bundle predates the stamp — not fatal, and it must not be: no review can be
    matched to it, so rule 3 cannot fire and rule 4 routes, the fail-safe direction. A store
    that will NOT answer is the documented 503, never "no stamp": unknown must not read as a
    state."""
    try:
        meta = await storage.head(snapshot_key(app_id))
    except StorageError as exc:
        raise AppApiError(
            status.HTTP_503_SERVICE_UNAVAILABLE, _STORAGE_DOWN, code="storage_unavailable"
        ) from exc
    if meta is None:
        # Nothing saved at all — the pipeline would fail on the missing bundle and the
        # queue copy has nothing to fork, so this is the "build something first" refusal.
        # Coded, same string as the other "nothing saved" site above — see the comment
        # there.
        raise AppApiError(status.HTTP_409_CONFLICT, _NOTHING_TO_DEPLOY, code=FAIL_NO_SNAPSHOT)
    return head_sha_from_metadata(meta.metadata)


async def _route_to_review(
    db: AsyncSession,
    storage: ObjectStorage,
    *,
    user: User,
    app_row: AppRegistry,
    project_id: uuid.UUID,
    head_sha: str | None,
    declaration: dict[str, Any],
    rule: str,
    response: Response,
) -> DeployRoutedResponse:
    """ROUTE: submit this exact version into the admin queue and tell the citizen so.

    200, not an error status: the platform did exactly what it promised. The citizen's publish
    surfaces render this as an informational state and must never paint the red failure badge
    over it."""
    # Audit-then-commit is the shipped gate's shape and it is kept: the submit service is
    # commit-less, so its guarded UPDATE, its own `submit` row and this gate's decision record all
    # land in ONE transaction — the app cannot end up pending with no record of why, nor recorded
    # as routed without actually being in the queue.
    receipt = await submit_app_for_review(
        db,
        storage,
        user_id=user.id,
        app=app_row,
        declaration=declaration,
        route=ApprovalRoute.SELF_PUBLISH,
    )
    if head_sha is not None and receipt.commit_sha != head_sha:
        # The bundle moved between the metadata read and the copy: the queue item would
        # be pinned to a version this ladder never examined. Nothing is committed, so the
        # submit unwinds with the request — refuse and let the citizen re-publish against
        # the version that actually exists now.
        _log.warning(
            "publish_gate_snapshot_moved_mid_route",
            app_id=str(app_row.id),
            examined=head_sha,
            copied=receipt.commit_sha,
        )
        raise AppApiError(status.HTTP_409_CONFLICT, _SNAPSHOT_MOVED_MSG, code="snapshot_moved")
    await _audit_gate(
        db,
        user=user,
        app_id=app_row.id,
        project_id=project_id,
        decision="routed",
        rule=rule,
        declaration=declaration,
        extra={"submissionId": str(receipt.submission_id), "commitSha": receipt.commit_sha},
    )
    await db.commit()
    _log.info(
        "publish_gate_routed",
        app_id=str(app_row.id),
        rule=rule,
        commit_sha=receipt.commit_sha,
    )
    response.status_code = status.HTTP_200_OK
    return DeployRoutedResponse(
        app_id=str(app_row.id),
        submission_id=str(receipt.submission_id),
        commit_sha=receipt.commit_sha,
        submitted_at=receipt.submitted_at,
        message=_ROUTED_MSG,
    )


async def _start_pipeline(
    db: AsyncSession,
    *,
    service: OptionalDeployService,
    user: User,
    app_row: AppRegistry,
    project_id: uuid.UUID,
    body: DeployRequest,
    score: int,
    declaration: dict[str, Any],
    expected_commit_sha: str | None,
    decision: str,
    rule: str,
    recheck: VersionRecheck | None = None,
    extra: dict[str, Any] | None = None,
) -> DeployStartedResponse:
    """PUBLISH (or DEFER): start the pipeline and hand back the id to poll.

    `expected_commit_sha` IS H, ON EVERY BRANCH, not just the deferring one: the pipeline
    extracts the mutable snapshot, and between this claim and that extraction another save can
    land. `None` (a saved bundle predating the stamp) asserts nothing, which is the honest
    reading of an unknown."""
    # THE UNCONFIGURED-DEPLOY 503 LIVES HERE, not at the top of the route. At the top it would
    # shut the door before the ladder ran — stranding exactly the citizens routing must never
    # strand, since routing needs object storage and the queue, never the deploy service. Here,
    # immediately before the pipeline starts, every ROUTE branch completes without it.
    #
    # Pinning H and failing the deploy closed when the tree turns out to be a different one is
    # what makes "what was approved is what is running" provable rather than assumed.
    if service is None:
        raise AppApiError(status.HTTP_503_SERVICE_UNAVAILABLE, _UNAVAILABLE)
    try:
        started = await service.start(
            db,
            user_id=user.id,
            app_id=app_row.id,
            project_id=project_id,
            conversation_id=app_row.conversation_id,
            classification=body.answers.model_dump(),
            classification_score=score,
            expected_commit_sha=expected_commit_sha,
            recheck=recheck,
        )
    except DeployNotPossibleError as exc:
        raise AppApiError(status.HTTP_409_CONFLICT, str(exc), code=exc.code) from None

    detail: dict[str, Any] = {"deploymentId": str(started.deployment_id)}
    if extra:
        detail.update(extra)
    await _audit_gate(
        db,
        user=user,
        app_id=app_row.id,
        project_id=project_id,
        decision=decision,
        rule=rule,
        declaration=declaration,
        extra={
            **detail,
            # What was declared, on the gated action itself. The deployment row
            # holds the same facts, but audit outlives it: an app deleted after a bad
            # deploy takes its `deployments` rows with it via CASCADE, and the declaration
            # that authorised the publish is exactly what a later review needs.
            "classificationScore": score,
            "classification": body.answers.model_dump(),
        },
    )
    await db.commit()

    _log.info(
        "deploy_started",
        app_id=str(app_row.id),
        deployment_id=str(started.deployment_id),
        rule=rule,
    )
    return DeployStartedResponse(
        deployment_id=str(started.deployment_id),
        app_id=str(started.app_id),
        status="running",
    )


async def _audit_gate(
    db: AsyncSession,
    *,
    user: User,
    app_id: uuid.UUID,
    project_id: uuid.UUID,
    decision: str,
    rule: str,
    declaration: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """The ladder's half of the gate audit — the row itself, its shape and the reasoning
    for both, live in `deploy/gate.append_gate_audit`, because the detached pipeline is a
    second writer of them. This adapter exists only so the route's six call sites can keep
    passing the `User` they already hold."""
    await append_gate_audit(
        db,
        actor_id=user.id,
        email=user.email,
        app_id=app_id,
        project_id=project_id,
        decision=decision,
        rule=rule,
        declaration=declaration,
        extra=extra,
    )


@dataclass(frozen=True)
class _SavedVersion:
    """THE CITIZEN'S OWN LAST SAVE, as the saved bundle describes itself: WHICH commit
    (`head`, from the metadata stamp) and WHEN it was written (`saved_at`, the store's own
    last-modified on that same object). One `head()` answers both, which is why they are
    one value rather than two reads.

    The two halves are INDEPENDENTLY nullable and that is deliberate: a bundle written
    before the stamp existed still has a last-modified, so it can say when without saying
    which. Neither is ever invented — `None` is "no claim" on both axes.

    A THIRD FIELD SAYS WHY THEY ARE ABSENT. The pair alone cannot: it reads the same for a
    citizen who has never saved and for a store that would not answer, and those are
    opposite facts to the person reading the rail."""

    head: str | None
    saved_at: datetime | None
    state: SavedState


# THE SENTINEL WAS ONE VALUE FOR THREE FACTS, and this is the split.
#
# All three still answer the DRIFT question identically — `head=None`, which
# `compute_publish_state` reads as `live_drift_unknown` and never as "up to date" — so
# nothing about the publish state moves. What was missing is the OTHER question the same
# read answers: has this citizen ever saved? "No store bound" and "the store would not
# answer" are claims about the platform's reach; "there is no bundle" is a claim about
# their work, and collapsing the three made the rail tell a citizen who had never saved
# that their save could not be found.
_NEVER_SAVED = _SavedVersion(head=None, saved_at=None, state=SavedState.NEVER_SAVED)
_NO_STORE = _SavedVersion(head=None, saved_at=None, state=SavedState.STORE_UNCONFIGURED)
_STORE_REFUSED = _SavedVersion(head=None, saved_at=None, state=SavedState.STORAGE_ERROR)


async def _saved_version_for_publish_state(
    storage: ObjectStorage | None, app_id: uuid.UUID
) -> _SavedVersion:
    """The one object-store read for the publish-state chip: the same metadata `head()`
    `_shipping_head` and `classification/router.py`'s `_saved_version` already take.

    Deliberately NOT the whole-bundle read `build_sessions/manager._saved_head` uses to answer
    the same question. A small header versus the app's entire git bundle pulled through the API
    process, on a route a client polls on mount, on focus and after every publish, is this
    unit's whole cost argument."""
    # A NAMED DEPARTURE FROM THE GENERAL STORAGE-DOWN RULE, HERE ONLY: both `_shipping_head` and
    # `classification`'s reader turn a `StorageError` into a 503, and they are right to — each is
    # about to ACT on the bundle it names. This read never acts on anything, and this endpoint is
    # the ONLY publishing surface in the product, so a storage blip answering "is there newer
    # work" must not blank the rest of the response (the status, the approval block, the rejection
    # note, the address) over a question that was only ever a hint.
    #
    # So a raise here is caught and folds into `None`, same as an unconfigured store
    # (`storage is None`, the supported dev/test posture this route already accommodates) and same
    # as a bundle saved before the metadata stamp existed: all three are "cannot tell", which
    # `compute_publish_state` reads as `live_drift_unknown`, never as "up to date". If a later
    # reader "fixes" this back to match its two neighbours, that is the regression — the
    # difference is deliberate, and the reason for it lives here, beside the code.
    if storage is None:
        return _NO_STORE
    try:
        # THE KEY CHOICE, AND IT IS THE CITIZEN'S SAVE — `snapshot_key`, which
        # `snapshot.Destination.saved` names "the user's explicit Save; the one key a
        # platform-initiated write must never touch". NOT `recovery_key`, the platform's
        # own turn-boundary autosave, and NOT any helper that picks between the two:
        # `manager.restore_presence` PREFERS the recovery key, and
        # `manager.newest_restore_source` returns whichever bundle is newer. Both are
        # right for what they do (resuming a workspace), and both would be wrong here —
        # the rail's row says "YOUR LATEST", so it must name the version the citizen
        # chose to keep, never one the platform wrote on their behalf. Reading the newer
        # of the two would report work as SAVED that they never saved, which is the same
        # promotion-by-the-back-door this rule exists to prevent.
        meta = await storage.head(snapshot_key(app_id))
    except StorageError:
        _log.warning("publish_state_saved_head_unavailable", app_id=str(app_id))
        return _STORE_REFUSED
    if meta is None:
        return _NEVER_SAVED
    # `head_sha_from_metadata` answers None for an unstamped bundle; `last_modified` is
    # whatever the store knows (also nullable). Neither absence is filled in from the
    # other — an unstamped bundle reports its date and withholds its id.
    #
    # THE OBJECT EXISTS, so this is `SAVED` whatever the two halves say. A bundle whose
    # metadata answers neither question is a save the platform cannot describe, not an
    # absent one — that is the case "We could not tell" was written for, and the one case
    # this deliberately leaves saying it.
    return _SavedVersion(
        head=head_sha_from_metadata(meta.metadata),
        saved_at=meta.last_modified,
        state=SavedState.SAVED,
    )


@router.get(
    "/{project_id}/deployment",
    response_model=DeploymentResponse,
    responses=error_responses(
        AUTH_401,
        (404, ErrorEnvelope, "Project not found"),
    ),
)
async def latest_deployment(
    project_id: uuid.UUID,
    user: CurrentUser,
    db: DbSession,
    storage: OptionalStorage,
) -> DeploymentResponse:
    """The latest deploy attempt for this project — what the client polls.

    An app that has never been deployed is a NORMAL state, not a 404: the answer is an empty
    envelope, exactly as `save-state` answers for a project with no workspace. The response also
    carries the app's APPROVAL STATE and the citizen's saved commit/time, so one poll lifetime
    feeds both publish surfaces and the workspace rail. It needs nothing but the database plus a
    single object-store metadata HEAD — never a download, never a second query, and no sandbox."""
    # IT ALSO CARRIES THE APP'S APPROVAL STATE, and that is not scope creep. The two citizen
    # publish surfaces poll this one response through one hook; the toolbar one has no app id to
    # make a second, app-scoped call with, and a status card that reads its own lifecycle once on
    # mount goes stale the moment the publish it is watching routes into the queue. One response,
    # one poll lifetime, two surfaces that cannot disagree. It reads the FULL registry row, not
    # `deploy_target`'s two-column projection, which carries neither the approval pin nor the
    # rejection note — with the ownership predicate in the WHERE clause, same as every other
    # query here: a dropped `user_id` is a cross-user leak.
    #
    # IT DOES NOT NEED THE DEPLOY PIPELINE, and must not start requiring one. Every field it
    # returns is a committed row, so refusing without `DEPLOY__*` would 503 a request whose
    # complete answer is sitting in the database. That cost lands on the person least able to
    # diagnose it: the publish ladder deliberately ROUTES without a pipeline (a routed app needs a
    # human, not a container), so an app can be sent to an administrator, be rejected with a note
    # written specifically for its developer, and that developer's approval card would render
    # empty. The gate works; the answer never arrives. Publishing is where the pipeline is
    # genuinely required, and `deploy_project` still refuses there.
    #
    # THAT SAME HEAD IS SPENT TWICE, NOT ONCE. The metadata read already happening for
    # `publish_state` carries the citizen's saved commit and the store's last-modified on that
    # bundle, and rather than being discarded both reach the wire as `saved_head`/`saved_at` —
    # which is what lets the workspace rail draw its "YOUR LATEST" row on a project whose
    # CONTAINER IS STOPPED. No sandbox dependency is declared on this route, so there is nothing
    # here that could wake one, and that is the property the row depends on. `save-state` cannot
    # answer it: that read attaches to a container first, so it is silent in exactly the reclaimed
    # case the row is for. An unconfigured store reads the same as one that raised.
    await owned_project_or_404(db, user.id, project_id)

    app_row = (
        await db.execute(
            sa.select(AppRegistry).where(
                AppRegistry.project_id == project_id,
                AppRegistry.user_id == user.id,
            )
        )
    ).scalar_one_or_none()
    if app_row is None:
        # The one `PublishState` member with no app row behind it at all — computed
        # here rather than in `compute_publish_state`, whose signature takes a
        # registry row as a required input precisely because every OTHER member needs
        # one.
        # No app row means no bundle to have saved, so both halves of the saved row are
        # null on the one path that never reaches the store at all — and `NEVER_SAVED`
        # rather than an "unknown", because this path knows: there is nothing that could
        # have been saved, so the rail draws no saved row rather than one that cannot
        # tell.
        return DeploymentResponse(
            publish_state=PublishState.NOTHING_BUILT,
            saved_head=None,
            saved_at=None,
            saved_state=SavedState.NEVER_SAVED,
        )

    approval = ApprovalState.of(app_row)
    # A Postgres round-trip and an object-store metadata HEAD, neither of which needs the
    # other's answer. Both surfaces POLL this route, so serialising them would make every tick
    # cost the SUM of a database query and a network call to the store instead of the larger of
    # the two. Only the first coroutine touches `db`, so the session is never used concurrently.
    #
    # `return_exceptions=True` IS LOAD-BEARING, not defensive noise. A bare `gather` propagates
    # the first exception the moment it is raised and leaves its sibling RUNNING — so a storage
    # error that escapes `_saved_version_for_publish_state` (it catches `StorageError`, and the
    # Azure client can raise from outside that) would return control to `get_db`, which rolls the
    # session back while the SELECT above is still on it. Collecting both answers means each
    # coroutine always finishes before anything unwinds; the failure is then re-raised unchanged,
    # so the route's error surface is exactly what it was when these two ran in sequence.
    row_or_error, saved_or_error = await asyncio.gather(
        deployment_for_app(db, app_id=app_row.id),
        _saved_version_for_publish_state(storage, app_row.id),
        return_exceptions=True,
    )
    if isinstance(row_or_error, BaseException):
        raise row_or_error
    if isinstance(saved_or_error, BaseException):
        raise saved_or_error
    row, saved = row_or_error, saved_or_error
    publish_state = compute_publish_state(app_row, row, saved.head)
    if row is None:
        return DeploymentResponse(
            app_id=str(app_row.id),
            approval=approval,
            publish_state=publish_state,
            saved_head=saved.head,
            saved_at=saved.saved_at,
            saved_state=saved.state,
        )
    return DeploymentResponse.of(
        row,
        approval=approval,
        publish_state=publish_state,
        saved_head=saved.head,
        saved_at=saved.saved_at,
        saved_state=saved.state,
    )


@router.post(
    "/{project_id}/restart",
    response_model=RestartStartedResponse,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[RequireCsrf],
    responses=error_responses(
        (403, ErrorEnvelope, "CSRF check failed"),
        AUTH_401,
        (404, ErrorEnvelope, "Project not found"),
        (
            409,
            ErrorEnvelope,
            "Never deployed (`never_deployed`), not currently serving (`not_live`), taken "
            "offline by its owner (`taken_offline` — publish it again instead), disabled by "
            "an administrator (`app_disabled`), or already deploying or restarting "
            "(`deploy_in_flight`)",
        ),
        (503, ErrorEnvelope, "Publishing is not configured (`publishing_unavailable`)"),
    ),
)
async def restart_project(
    project_id: uuid.UUID,
    user: CurrentUser,
    db: DbSession,
    service: OptionalDeployService,
) -> RestartStartedResponse:
    """Recycle the revision this app is ALREADY RUNNING — same version, same address, same
    data. 202 with the id to poll, because an ARM long-running operation outlives the edge
    gateway's twenty seconds.

    IT CANNOT RUN A NEWER COMMIT, and that is the point rather than a detail: re-running the
    deploy path against whatever is saved now would put work no reviewer has seen into
    production, turning a convenience button into a way around the publish gate. It is also
    not the control for an app that is OFF — a taken-down app is published again, which goes
    through that gate."""
    # THE ORDER IS THE POLICY, and it is the kill-switch's, one lever over: ownership first
    # (a stranger gets the same non-leaking 404 a missing project does), then the environment
    # (nothing to recycle where publishing was never configured), then the administrator's
    # standing decision, then the in-flight guard, then the row that says what is live.
    await owned_project_or_404(db, user.id, project_id)

    if service is None:
        raise AppApiError(
            status.HTTP_503_SERVICE_UNAVAILABLE, _UNAVAILABLE, code="publishing_unavailable"
        )

    app_row = (
        await db.execute(
            sa.select(AppRegistry).where(
                AppRegistry.project_id == project_id,
                AppRegistry.user_id == user.id,
            )
        )
    ).scalar_one_or_none()
    if app_row is None:
        raise AppApiError(status.HTTP_409_CONFLICT, _RESTART_NEVER_DEPLOYED, code="never_deployed")

    # The administrator's other lever over the same container. `disable` also severs the
    # app's database credential, so quietly restarting past it would hand back a container an
    # administrator decided should not be serving.
    if app_row.status is AppStatus.DISABLED:
        raise AppApiError(status.HTTP_409_CONFLICT, _RESTART_DISABLED, code="app_disabled")

    # Check-then-act, narrowing the window rather than closing it — `store.claim` below is the
    # atomic guard, and this exists so the commonest case gets a sentence that names what is
    # actually happening instead of the claim's generic one.
    if await store.in_flight(db, app_id=app_row.id) is not None:
        raise AppApiError(status.HTTP_409_CONFLICT, _BUSY_MSG, code="deploy_in_flight")

    # The NEWEST attempt, whatever its status, for the reason the kill-switch resolves the
    # same way: a deploy that died at the readiness check still left a container running.
    row = await store.latest_for_app(db, app_id=app_row.id)
    if row is None:
        raise AppApiError(status.HTTP_409_CONFLICT, _RESTART_NEVER_DEPLOYED, code="never_deployed")
    if row.unpublished_at is not None:
        raise AppApiError(status.HTTP_409_CONFLICT, _RESTART_TAKEN_OFFLINE, code="taken_offline")
    # A missing digest belongs with the refusals rather than the guesses: without it the
    # platform cannot name the image that is live, and composing one from a mutable tag is
    # exactly the "newer bits" this route is written to prevent.
    if row.status is not DeploymentStatus.SUCCEEDED or row.image_digest is None:
        raise AppApiError(status.HTTP_409_CONFLICT, _RESTART_NOT_LIVE, code="not_live")

    try:
        started = await service.restart(
            db,
            user_id=user.id,
            app_id=app_row.id,
            project_id=project_id,
            live=LiveRevision(image_digest=row.image_digest, head_sha=row.head_sha),
        )
    except DeployNotPossibleError as exc:
        # Lost the race the in-flight check above only narrowed. Nothing was claimed, so
        # nothing is audited — a refused press is not an action.
        raise AppApiError(status.HTTP_409_CONFLICT, str(exc), code=exc.code) from None

    await append_audit(
        db,
        actor_id=user.id,
        action="restart",
        resource_type="app",
        resource_id=str(app_row.id),
        detail={
            "deploymentId": str(started.deployment_id),
            "recycledFrom": str(row.id),
            "projectId": str(app_row.project_id),
            # WHICH VERSION CAME BACK. The rule this route exists to hold is "the commit
            # already live, and no other", and a trail that does not name the commit cannot
            # be used to check it after the fact.
            "headSha": row.head_sha,
            # Derived, like the kill-switch's: `container_app_name` is written after the
            # provision returns, so a deploy that died inside that call leaves it NULL over a
            # container that exists.
            "containerAppName": published_app_name(app_row.id),
        },
    )
    await db.commit()
    _log.info(
        "app_restart_started",
        app_id=str(app_row.id),
        deployment_id=str(started.deployment_id),
        recycled_from=str(row.id),
    )
    return RestartStartedResponse(
        deployment_id=str(started.deployment_id), app_id=str(app_row.id), status="running"
    )


@router.post(
    "/{project_id}/takedown",
    response_model=TakedownResponse,
    dependencies=[RequireCsrf],
    responses=error_responses(
        (403, ErrorEnvelope, "CSRF check failed"),
        AUTH_401,
        (404, ErrorEnvelope, "Project not found"),
        (
            409,
            ErrorEnvelope,
            "Never deployed (`never_deployed`), or a deploy is running for this app "
            "(`deploy_in_flight` — wait and retry)",
        ),
        (
            503,
            ErrorEnvelope,
            "Publishing is not configured (`publishing_unavailable`, terminal), or the "
            "takedown could not be confirmed (`teardown_unconfirmed` — retrying is safe)",
        ),
    ),
)
async def take_project_down(
    project_id: uuid.UUID,
    user: CurrentUser,
    db: DbSession,
    remover: OptionalPublishedAppRemover,
) -> TakedownResponse:
    """Take an owner's own app out of production. The container goes; the app row, its
    per-project database, its Blob container, its chats and any version waiting for review
    all stay, and publishing it again brings it back at the same URL.

    NEITHER DELETE NOR THE ADMIN KILL-SWITCH, though it borrows the kill-switch's mechanism:
    `disable` severs the app's database credential and is for an app that must be stopped,
    while this keeps the data intact and is for an owner who is done serving it. It moves the
    deployment's taken-offline axis and NEVER writes `AppStatus` — whether an app is in
    production is a separate question from Draft / In review / Approved, so nothing here
    returns to Approved and a version in the queue is not withdrawn.

    IDEMPOTENT: an already-stamped attempt answers 200 and never touches Azure again."""
    # THE ACCOUNTABILITY ROW IS COMMITTED BEFORE AZURE IS CALLED, for the reason the
    # kill-switch documents at length: the ARM delete is bounded at five minutes behind an
    # edge gateway that gives up at twenty seconds, so the failure mode is "this request never
    # returns" — and a request that never returns cannot audit on its way out. What that row
    # does NOT claim is that the container is gone; `unpublished_at` claims that, and only
    # after the sweep comes back clean.
    await owned_project_or_404(db, user.id, project_id)

    if remover is None:
        raise AppApiError(
            status.HTTP_503_SERVICE_UNAVAILABLE, _UNAVAILABLE, code="publishing_unavailable"
        )

    # No status check anywhere below: a draft, an approved and a pending app all leave
    # production the same way, because that axis and the app's lifecycle are independent.
    app_row = (
        await db.execute(
            sa.select(AppRegistry).where(
                AppRegistry.project_id == project_id,
                AppRegistry.user_id == user.id,
            )
        )
    ).scalar_one_or_none()
    if app_row is None:
        raise AppApiError(
            status.HTTP_409_CONFLICT, _TAKEDOWN_NEVER_DEPLOYED, code="never_deployed"
        )
    message = _TAKEDOWN_DONE
    if app_row.status is AppStatus.PENDING:
        message += _TAKEDOWN_REVIEW_UNTOUCHED

    if await store.in_flight(db, app_id=app_row.id) is not None:
        raise AppApiError(
            status.HTTP_409_CONFLICT, _TAKEDOWN_WHILE_DEPLOYING, code="deploy_in_flight"
        )

    row = await store.latest_for_app(db, app_id=app_row.id)
    if row is None:
        raise AppApiError(
            status.HTTP_409_CONFLICT, _TAKEDOWN_NEVER_DEPLOYED, code="never_deployed"
        )

    if row.unpublished_at is not None:
        # Already down. No Azure call and no state change, so this branch cannot fail and
        # does not audit.
        return TakedownResponse(
            app_id=str(app_row.id),
            deployment_id=str(row.id),
            unpublished_at=row.unpublished_at,
            message=message,
        )

    _log.info("app_takedown_requested", app_id=str(app_row.id), deployment_id=str(row.id))
    await append_audit(
        db,
        actor_id=user.id,
        action="takedown",
        resource_type="app",
        resource_id=str(app_row.id),
        detail={
            "deploymentId": str(row.id),
            "projectId": str(app_row.project_id),
            "containerAppName": published_app_name(app_row.id),
            "deploymentStatus": row.status.value,
        },
    )
    await db.commit()

    if await sweep_published_apps([app_row.id], client=remover):
        # UNCONFIRMED, NOT FAILED: the sweep collapses a terminal ARM refusal and a delete
        # still running past its ceiling into the same survivor entry, so this request only
        # knows it did not OBSERVE a success. `unpublished_at` stays NULL — marking an app
        # down that is still serving is the expensive direction of that ambiguity — and a
        # retry re-attempts an idempotent delete and settles it either way.
        _log.warning("app_takedown_unconfirmed", app_id=str(app_row.id), deployment_id=str(row.id))
        await append_audit(
            db,
            actor_id=user.id,
            action="takedown:unconfirmed",
            resource_type="app",
            resource_id=str(app_row.id),
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
        # Either another caller stamped this row while we were in ARM, or the row is gone —
        # a concurrent app delete cascades `deployments` away, and the window since the
        # pre-sweep commit is minutes wide. `db.get(..., populate_existing=True)`, never
        # `db.refresh(row)`: refresh raises on a vanished row and would surface as a 500 on a
        # request whose teardown actually succeeded.
        current = await db.get(Deployment, row.id, populate_existing=True)
        if current is None:
            _log.info("app_takedown_app_deleted_mid_flight", app_id=str(app_row.id))
            raise AppApiError(status.HTTP_404_NOT_FOUND, "Project not found.")
        # Lost the race; the other caller's timestamp is what is on record, so report that
        # rather than this call's own unwritten one. Still a 200 — the world is as the owner
        # asked for it to be, and the repeat-click branch above answers 200 for exactly this
        # state.
        settled_at = current.unpublished_at or now
        await db.commit()
        return TakedownResponse(
            app_id=str(app_row.id),
            deployment_id=str(row.id),
            unpublished_at=settled_at,
            message=message,
        )

    await db.commit()
    _log.info("app_taken_down", app_id=str(app_row.id), deployment_id=str(row.id))
    return TakedownResponse(
        app_id=str(app_row.id),
        deployment_id=str(row.id),
        unpublished_at=now,
        message=message,
    )


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
    """THE admin kill-switch. Takes the published container down; leaves the app row, its
    per-project database and its Blob container completely untouched — a later Deploy brings it
    back at the same URL, because the container name is a pure function of the immutable app id.

    IDEMPOTENT: an already-stamped attempt returns 200 and never touches Azure again. AN OPERATOR
    CONVENIENCE, NOT AN ENFORCEMENT LEVER — nothing in `deploy_project` consults `unpublished_at`
    or `AppRegistry.status`, so the owner republishes one click later. Against a compromised or
    data-leaking app, `disable` is the answer instead: it fails CLOSED by severing the database."""
    # NOT the citizen-facing case, and no submit-for-review lineage is touched — a separate,
    # admin-only lever, same posture as `admin/router.py`'s `disable`. The convenience/enforcement
    # distinction matters against a hostile app: the right default here is an app misbehaving by
    # accident, taken down while it is fixed. Enforcement is deliberately left to a follow-up
    # rather than smuggled in here.
    #
    # THE ACCOUNTABILITY ROW IS COMMITTED BEFORE AZURE IS CALLED, the opposite of `disable`'s
    # ordering, and the inversion is deliberate rather than inherited. `disable` audits first so a
    # failing side effect ROLLS THE AUDIT BACK — its side effect is a local `ALTER ROLE` that
    # either lands in milliseconds or raises. This lever's side effect is an ARM long-running
    # delete bounded at `provision_timeout_s` (300s) behind an edge gateway that gives up at
    # twenty. The failure mode is therefore not "the side effect raised" but "this request never
    # returns" — and a request that never returns cannot audit anything on its way out. So the
    # trail is made durable FIRST: after that commit, the fact that a named superadmin pulled this
    # lever on this app survives a 504, a worker recycle, and an ARM call that lands ten minutes
    # later. What it deliberately does NOT claim is that the container is gone — `await_lro`
    # raises on expiry precisely because the outcome is unknown, and an audit row asserting an
    # outcome nobody observed would be worse than none. Committing there also RELEASES THE DB
    # CONNECTION for the duration of the ARM call, rather than holding one idle-in-transaction for
    # up to five minutes per concurrent admin.
    #
    # TWO AUDIT ACTIONS, and every request about to touch Azure writes the first before it does:
    #   `unpublish`             — an admin exercised the lever. One row per request that reached
    #                             the sweep, so two admins racing the same incident leave two rows,
    #                             correctly attributed, which is the point.
    #   `unpublish:unconfirmed` — the sweep came back empty, so this request never observed the
    #                             container go away. Written after the attempt row, mirroring the
    #                             two `publish_gate` refusals at the top of `deploy_project`
    #                             (`rule="disabled"` and `rule="pending"`): audit the outcome,
    #                             commit, then raise. NOT `:failed` — see the sweep branch.
    # A successful unpublish therefore writes ONE row, not two: the pre-ARM row already carries the
    # whole gated-action audit payload (who, what, which, when), and "it worked" is already durable
    # in `unpublished_at` and the `app_unpublished` log line. One `unpublish` row with no
    # `:unconfirmed` sibling and `unpublished_at` still NULL reads as "attempted, outcome unknown"
    # — exactly what a 504 leaves behind, and exactly what `await_lro` can honestly prove. Paths
    # that mutate nothing write nothing (the 404, both 409s, the already-down 200), matching this
    # codebase's rule that a no-op admin request is not an audited action.
    #
    # ORDER MATTERS, same discipline as `disable`: the unconfigured-publishing check goes first
    # because it costs no query and an environment with `DEPLOY__*` unset has nothing to tear down;
    # the in-flight check next, because letting an unpublish through while a deploy is running
    # would race that deploy's own `create_or_update` — a moment later the "removed" container
    # could simply reappear, silently undoing the admin's action. That check is check-then-act: a
    # deploy can still start between it and the sweep, so the 409 NARROWS the window rather than
    # closing it. It is a refusal to act on a state already known to be changing, not a guarantee
    # about the state at the moment the sweep lands.
    #
    # THE ROW TO STAMP IS THE NEWEST ONE, NOT THE NEWEST SUCCEEDED ONE. The pipeline creates the
    # container app at step 5 and only then awaits the revision, so an attempt that settles FAILED
    # at step 6 leaves `pub-<app_id>` running, externally addressable, holding the app's database
    # URL and Blob SAS, and billing. Resolving through the newest SUCCEEDED row would answer "never
    # published" while exactly that container served traffic — and on a
    # succeeded-then-unpublished-then-failed history it would take the already-down early return
    # and leave the re-created container up. `latest_for_app` closes both. A missing row is still a
    # safe 409: the container is only ever created by a pipeline that owns a deployment row, and
    # rows leave only by CASCADE with the app itself (a 404 here), so no row provably means no
    # container.
    #
    # FAILS LOUD, NOT BEST-EFFORT: `sweep_published_apps` is reused exactly as it exists
    # (best-effort, never-raising) rather than duplicating a second delete path, but the
    # SURVIVORS it names are read back here — this app coming back as a survivor means this
    # request never observed the delete succeed, and `unpublished_at` is deliberately NOT
    # written in that case. The signal is weak in BOTH directions, and the route over-claims in
    # neither: an empty survivor list means "no error" rather than "something was deleted",
    # because `delete_app` no-ops on an absent container and still returns clean; a survivor
    # means "not observed" rather than "failed", because the sweep collapses a terminal
    # `AcaError` and an `AcaTransientError` from ceiling expiry into the same entry. Both
    # readings are right for a lever whose job is to guarantee absence rather than prove
    # authorship of it. Retrying is safe either way, because `AcaPublishedApps.delete_app` is
    # independently idempotent — a partial failure never leaves the row and reality permanently
    # disagreeing.
    # First, and before any query: an environment with `DEPLOY__*` unset has no publish plane
    # at all. Without this the `None` flows into `sweep_published_apps`, which re-resolves the
    # singleton, catches `DeployNotConfiguredError` and returns NO survivors — landing in the
    # confirmed-teardown path below and stamping `unpublished_at` for a container that this
    # deployment could never have published. That is the wrong lie in the wrong direction. This
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

    if await sweep_published_apps([app_id], client=remover):
        # UNCONFIRMED, NOT FAILED, and the distinction is the same one this route's audit
        # discipline is built on. `sweep_published_apps` collapses every exception into a
        # survivor entry, so this app coming back means "we did not observe a success" — which
        # covers a terminal `AcaError` (ARM refused; it really is still up) AND an
        # `AcaTransientError` from `await_lro`'s ceiling expiry, whose docstring says the
        # outcome is genuinely unknown because "the operation may still land". Recording that
        # as a confirmed failure would be the same sin as recording an unobserved success, and
        # it is the far likelier one here: the ceiling is 300s and the gateway gives up at 20,
        # so a slow-but-fine delete is exactly what lands in this branch. `unpublished_at`
        # stays NULL either way, which is the conservative choice — a retry re-attempts the
        # delete (idempotent) and settles the row, whereas stamping it now could mark an app
        # down that is still serving.
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
