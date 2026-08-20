"""One-click deploy — the citizen-facing control surface, plus the admin kill-switch (#113).

`POST /v1/projects/{id}/deploy` starts a deploy and returns **202 immediately**. That is not
a style choice: a deploy runs for minutes and the edge gateway times out at twenty seconds,
so anything that waits for the result is a guaranteed 504 on a deploy that is in fact going
fine. The work is detached; the client polls `GET /v1/projects/{id}/deployment`.

THE PUBLISH GATE IS A PRECEDENCE LADDER, AND THIS IS WHERE THE TWO LINEAGES JOIN (U9).
An earlier revision of this docstring said "no admin approval to start a deploy" and that
the `submit`/`approve`/`reject` surface "is simply not what `deploy_project` calls" — both
became false here, on purpose. `deploy_project` now resolves the shipping commit, reads
the platform's own stored review of it, merges that with the citizen's declaration
(stricter-of per question), and lands on exactly one of four outcomes, in precedence
order (see `_LADDER` on the route): refuse (disabled / already waiting), PUBLISH (an
administrator approved exactly this version for self-publishing — R17 — or nothing
weighted merged Yes — R14), DEFER to the pipeline's own re-check (this request saved
first, R13), or ROUTE the app into the admin approve queue through the approvals submit
service (R15a's one route in). The old invariant "a refused deploy changes nothing" is
superseded by that last outcome and rewritten where it stood (see the route): a routed
deploy leaves the app in the queue at exactly the version examined, and publishes
nothing. Since U10 that holds on BOTH sides of the 202 — a deferred deploy whose
in-pipeline re-check finds something new routes the same way, minutes after the request
returned, and settles the deployment row FAILED with `routed_for_review` rather than
publishing. `mark-deployed` stays guarded on the runbook lineage; approval of a
`self_publish` submission is consumed HERE, by the citizen publishing it themselves.

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
    UnpublishResponse,
)
from src.api.v1.live_build import refuse_while_build_session_live
from src.core.errors import AppApiError
from src.core.redaction import redact_secrets
from src.db.models.app_registry import AppRegistry, ApprovalRoute, AppStatus
from src.db.models.deployment import Deployment
from src.db.models.user import User
from src.schemas import ADMIN_AUTH, AUTH_401, ErrorEnvelope, error_responses
from src.services.approvals.submit import submit_app_for_review
from src.services.audit.log import append_audit
from src.services.build_sessions.manager import NoLiveSandboxError, SessionManager
from src.services.classification.merge import merge_questions
from src.services.deploy import store
from src.services.deploy.classification import total_weight

# The gate's shared reading — the stored review situated against H, the merge inputs, the
# declaration document and the one audit action. Extracted in U10 because the detached
# pipeline is now a second writer of all four (the drift re-check produces the same
# document for the same queue) and a service cannot import the route that calls it.
from src.services.deploy.gate import (
    ReviewAtHead,
    append_gate_audit,
    declaration_document,
    merge_inputs,
    review_at_head,
)
from src.services.deploy.names import published_app_name
from src.services.deploy.service import DeployNotPossibleError, VersionRecheck, deployment_for_app
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
# ASM21: with storage down, the queue AND the pipeline are equally out of reach (both read
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
# NOT "could not be removed" — see the route. `sweep_published_apps` returns a count, and a
# zero collapses "ARM refused" together with "the delete is still running past our ceiling",
# whose outcome `await_lro` documents as genuinely unknown. Claiming removal failed would
# assert something nobody observed; this says only what is true, and points at the retry that
# settles it either way (`delete_app` is idempotent, so retrying is safe in both cases).
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
                "saved to deploy, unsaved changes (`unsaved_changes`), already deploying "
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
                "publishing, ASM21), or deploying is unconfigured — checked only once a "
                "branch actually needs the pipeline, so routing works without it (ASM10)",
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
    """Publish, or route to a person — THE PRECEDENCE LADDER (U9). Returns 202 with the
    id to poll when the pipeline started, 200 with the routed outcome when the app went
    to the admin queue instead.

    Every cell of the state table resolves to exactly one branch, evaluated in order
    against `H`, the commit about to ship (resolved from the snapshot blob's metadata
    stamp AFTER the optional save below — the decision must be about the version that
    will actually leave):

      1.  disabled                                     -> refuse
      2.  pending                                      -> refuse: waiting (R15b)
      3.  approved AND approved pin == H
            AND lineage == self_publish                -> PUBLISH  (R17; P5 makes
                                                          pre-feature approvals inert)
      3a. THIS request saved first AND the stored
            review is stamped a commit other than H    -> DEFER to the pipeline's
                                                          re-check (R13, U10)
      4.  the stored review for H anything other than
            genuinely COMPLETE (absent, stale, still
            running, aged out, failed, or complete-
            but-flagged-partial)                       -> ROUTE    (R20)
      5.  rejected                                     -> ROUTE    (P4 — sticky,
                                                          whatever a fresh review says)
      6.  any weighted category merges to Yes          -> ROUTE    (R9)
      7.  otherwise                                    -> PUBLISH  (R14)

    Rule 3 sits ABOVE rule 6 deliberately: the review keeps returning the same Yes for
    the same code, so without the override a flagged app would route forever and the
    flow would never terminate. Rule 3a is narrow on purpose — only a save THIS request
    performed defers, and rules 1, 2 and 5 are status checks evaluated before that save,
    so a disabled, pending or rejected app never reaches the pipeline by that door. Rule
    4 says COMPLETE (status, the runner's own completeness signal, AND the age ceiling)
    because a review still running is neither absent nor failed — falling through to
    rule 6 there would publish on the citizen's word alone, the exact bypass this ladder
    exists to close, reachable by answering six questions faster than the review lands.

    THE GATE READS THE STORED REVIEW, NEVER THE BROWSER'S COPY (R12): the request schema
    has no review field, unknown body keys are dropped at the boundary, and both answer
    sets plus the merge outcome are computed right here, server-side.

    THE OLD INVARIANT IS SUPERSEDED, ON PURPOSE (R13). This route used to promise that
    "a refused deploy changes nothing" and ran its gate before the save. The ladder's
    version-dependent rules must run against the post-save H, so the save now happens
    first when asked for — saving is what the citizen explicitly asked for on that path.
    The replacement invariant: a ROUTED deploy leaves the app in the queue at exactly
    the version examined, and publishes nothing; the plain REFUSALS (rules 1 and 2) are
    still decided before the save and still change nothing.

    AND A 202 IS NOT A PROMISE TO PUBLISH (U10). On rule 3a the decision is deliberately
    unfinished when this route answers: the pipeline reviews the version it extracted and
    may route it into the queue instead of shipping it, long after the response left. The
    replacement invariant above is written to cover that case unchanged — the app ends up
    in the queue at exactly the version examined, and nothing is published — but a caller
    must not read 202 as "this will go live", only as "the id to watch".

    UNSAVED WORK IS REFUSED BY DEFAULT. A deploy ships the last SAVED version, so quietly
    deploying while the workspace is ahead of it would publish something the citizen never
    asked for and give them no way to tell. `saveFirst` is the explicit "save and deploy"
    they opted into. `dirty` is TRI-STATE and unknown is not dirty: with no live workspace
    there is nothing to compare against, and the saved version is the only version.
    """
    await owned_project_or_404(db, user.id, project_id)

    # The full registry row, not `deploy_target`'s two-column projection: the ladder
    # reads status, the approval pin, the lineage and the rejection note. Ownership
    # predicate in the WHERE clause (ADR-0004) — a dropped `user_id` is a cross-user leak.
    app_row = (
        await db.execute(
            sa.select(AppRegistry).where(
                AppRegistry.project_id == project_id,
                AppRegistry.user_id == user.id,
            )
        )
    ).scalar_one_or_none()
    if app_row is None:
        raise AppApiError(status.HTTP_409_CONFLICT, _NOTHING_TO_DEPLOY)

    flags = body.answers.classification_flags()
    # ASM15: the citizen's explanation passes through the shared redactor before it is
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
        # R15b's structured 409: the state, the submitted version, and the rejection
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

    # Rule 5's status FACT, read before the save like rules 1 and 2 (the plan's ordering
    # note): a rejected app never defers through rule 3a and never publishes — but its
    # ROUTE still happens below, after the merge, so the queue item carries the record.
    rejected = app_row.status is AppStatus.REJECTED

    # A build session writing files while the snapshot is taken would ship a tree that
    # never coherently existed: valid bytes, wrong app, undetectable afterwards.
    await refuse_while_build_session_live(
        user.id, conflict_message=_BUILD_IN_FLIGHT, app_id=app_row.id
    )

    # R13 deliberately moved the gate AFTER this: the version-dependent rules must run
    # against the post-save H. `resolved.saved` is rule 3a's "this request saved first"
    # fact; `resolved.head_sha` is the commit the resolution landed on (U10).
    resolved = await _resolve_unsaved_work(
        db, user=user, project_id=project_id, manager=manager, sandbox=sandbox, request=body
    )
    saved = resolved.saved

    # Storage is the one dependency EVERY remaining branch needs — the queue copy and
    # the pipeline read the same bundle, so with it down publishing and routing are
    # equally unavailable and nobody is stranded behind a gate that works while the
    # pipeline doesn't (ASM21). The deploy service, by contrast, is checked only where
    # a branch actually starts the pipeline (ASM10 — routing must work without it).
    if storage is None:
        raise AppApiError(
            status.HTTP_503_SERVICE_UNAVAILABLE, _STORAGE_DOWN, code="storage_unavailable"
        )
    head_sha = await _shipping_head(storage, app_row.id)

    # TWO READINGS OF THE SAME TREE, AND THEY MUST AGREE (U10). The stamp above and the
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
    # app, situated against H by `gate.review_at_head`. Never a browser-supplied copy (R12).
    readout = await reviews.read(db, app_id=app_row.id)
    review: ReviewAtHead = review_at_head(readout, head_sha)

    # Both answer sets and the merge outcome, computed server-side inside this request —
    # the portal's local copy drives affordances and never decides. The merge runs on
    # every branch below (not just rule 6) because R22 requires the record of EVERY
    # decision to carry the effective answers and the differences.
    merged = merge_questions(merge_inputs(flags, review))
    declaration = declaration_document(
        head_sha=head_sha, citizen=flags, explanation=explanation, review=review, merged=merged
    )
    score = total_weight(flags)

    # --- rule 3: the approval override (R17) -------------------------------------------
    # The pinned commit must equal H and the lineage must be self_publish — which is what
    # makes approvals predating this feature inert here (P5): the 0030 backfill marked
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

    # --- rule 3a: the save-and-publish defer (R13) ---------------------------------------
    # Narrow on purpose: only a save THIS request performed, only when a stored review
    # exists stamped some other commit, and never for a rejected app (rule 5's status was
    # read above). Without this branch rule 4 would route every single save-and-publish,
    # because a fresh save always moves H off the stored stamp. This branch neither
    # routes nor refuses — it starts the pipeline and lets the pipeline's own re-check
    # decide (U10).
    #
    # THE SEAM U9 LEFT IS CLOSED (U10). `service.start` now carries the expected commit
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

    # ASM22/R10: the explanation is obliged exactly when the MERGED answers would route
    # (a Public-Data-only Yes carries no weight and needs none; an approved app already
    # answered it — rule 3 sits above). A 422, not a scoring refusal: an unexplained
    # weighted Yes is an INCOMPLETE submission, not a rejected one — and it is not a gate
    # outcome either, so it deliberately writes no `publish_gate` row.
    if merged.any_weighted_yes and explanation is None:
        raise AppApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            _EXPLANATION_REQUIRED,
            code="explanation_required",
        )

    # --- rule 4: no genuinely-COMPLETE review for H -> ROUTE, whatever was answered (R20)
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

    # --- rule 5: a rejection is sticky (P4) — an administrator lifts it, a re-roll never
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

    # --- rule 6: any weighted category merged to Yes -> ROUTE (R9) -----------------------
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

    # --- rule 7: nothing weighted from either side -> PUBLISH, unattended (R14) ----------
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

    `saved` is ladder rule 3a's "this request saved first" fact, which must mean a real
    write — `saveFirst` on an already-clean workspace saves nothing and defers nothing.

    `head_sha` is THE COMMIT THE RESOLUTION RESOLVED TO (U10): the one the save landed
    at, or the one the workspace was already level with when it performed none. It is a
    second, independent reading of the same tree the ladder's H comes from — the save's
    own return value and the container/bundle comparison, against the snapshot blob's
    metadata stamp. `None` means the resolution has no opinion (no sandbox runtime at
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

    U10 widened the answer from a bare "did we save" to the COMMIT it resolved to, so the
    version this request is about is named by the step that settled it rather than
    inferred afterwards from a blob header. The caller cross-checks it against the
    shipping stamp and threads it into the pipeline as the expected commit."""
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
    """H — the commit this request is about to ship, from the snapshot blob's metadata
    stamp. One `head()`, never an extraction: the classification routes settled that
    reading (the extract helper downloads the whole bundle before consulting its cache),
    and the pipeline re-derives the real head from the tree anyway.

    None means the saved bundle predates the stamp. That is not fatal here and must not
    be: it simply means no review can be matched to it, so rule 3 cannot fire and rule 4
    routes — the fail-safe direction. A store that will NOT answer is the documented 503,
    never "no stamp": unknown must not read as a state (ASM21)."""
    try:
        meta = await storage.head(snapshot_key(app_id))
    except StorageError as exc:
        raise AppApiError(
            status.HTTP_503_SERVICE_UNAVAILABLE, _STORAGE_DOWN, code="storage_unavailable"
        ) from exc
    if meta is None:
        # Nothing saved at all — the pipeline would fail on the missing bundle and the
        # queue copy has nothing to fork, so this is the same "build something first"
        # refusal the resolver used to give.
        raise AppApiError(status.HTTP_409_CONFLICT, _NOTHING_TO_DEPLOY)
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

    Audit-then-commit is the shipped gate's shape and it is kept: the submit service is
    commit-less, so its guarded UPDATE, its own `submit` row and this gate's decision
    record all land in ONE transaction — the app cannot end up pending with no record of
    why, nor recorded as routed without actually being in the queue.

    200, not an error status: the platform did exactly what it promised. U12 renders this
    as an informational state and must never paint the red failure badge over it."""
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

    THE UNCONFIGURED-DEPLOY 503 LIVES HERE, not at the top of the route (ASM10). It used
    to be `deploy_project`'s first body statement, which shut the door before the ladder
    ran — stranding exactly the citizens ASM10 says are not stranded, since routing needs
    object storage and the queue, never the deploy service. Moved down to immediately
    before the pipeline starts, so every ROUTE branch completes without it.

    `expected_commit_sha` IS H, ON EVERY BRANCH (U10), not just the deferring one: the
    pipeline extracts the mutable snapshot, and between this claim and that extraction
    another save can land. Handing it the commit the gate decided about — and failing the
    deploy closed when the tree turns out to be a different one — is what makes "what was
    approved is what is running" provable rather than assumed. `None` (a saved bundle
    predating the stamp) asserts nothing, which is the honest reading of an unknown.

    `recheck` is rule 3a's alone: it carries the commit the citizen's answers were written
    about and the declaration that stands as the baseline for "did this version raise
    anything new"."""
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
            # What was declared, on the gated action itself (ADR-0005). The deployment row
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
    for both, live in `deploy/gate.append_gate_audit` (U10 moved them there when the
    detached pipeline became the second writer). This adapter exists only so the route's
    six call sites can keep passing the `User` they already hold."""
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
    envelope, exactly as `save-state` answers for a project with no workspace.

    IT ALSO CARRIES THE APP'S APPROVAL STATE (U12), and that is not scope creep. The two
    citizen publish surfaces poll this one response through one hook; the toolbar one has
    no app id to make a second, app-scoped call with, and a status card that reads its own
    lifecycle once on mount goes stale the moment the publish it is watching routes into
    the queue. One response, one poll lifetime, two surfaces that cannot disagree.

    The FULL registry row, not `deploy_target`'s two-column projection, with the ownership
    predicate in the WHERE clause (ADR-0004) — a dropped `user_id` is a cross-user leak."""
    if service is None:
        raise AppApiError(status.HTTP_503_SERVICE_UNAVAILABLE, _UNAVAILABLE)
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
        return DeploymentResponse()

    approval = ApprovalState.of(app_row)
    row = await deployment_for_app(db, app_id=app_row.id)
    if row is None:
        return DeploymentResponse(app_id=str(app_row.id), approval=approval)
    return DeploymentResponse.of(row, approval=approval)


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
