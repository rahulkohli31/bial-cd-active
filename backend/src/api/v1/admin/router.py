"""Super-admin app-registry governance (R27, R29, R9) — the lifecycle state machine,
danger ops, and the durable clear-data confirm token, all `requires_superadmin`-gated
and audited. Ported from Express `admin/apps-routes.js`, but gated by Plan A's env
allowlist (`requires_superadmin`), NOT Express's `role==='admin'` claim. Approval
pins an immutable git-bundle SUBMISSION (APPROVAL D5): approve carries the reviewed
submission id, verifies the artifact exists (R11), and the guarded UPDATE refuses a
re-submitted-since-review app — there is no compiled artifact and no server compile.

The state machine is enforced atomically (`UPDATE ... WHERE status = ANY(allowed)`);
an illegal transition updates zero rows → 409. `enable` carries an explicit
`status==disabled` guard because the `→approved` transition also permits `pending`
(without it, enable would promote an unvetted pending app past the approve gate);
`approve` carries the mirror-image `status==pending` guard for the same reason
(without it, an admin could approve a kill-switched DISABLED app directly).

Approvals carry a LINEAGE (U4: R17a/P5): `runbook` items get no new approvals (the
citizen must re-submit through the publish flow), and `self_publish` apps get neither
the deploy-needed prompt nor the mark-deployed marker — their owner publishes the
approved version themselves, so a runbook record here would describe a deployment
nobody performed.
"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any, cast

import sqlalchemy as sa
import structlog
from fastapi import APIRouter, Query, status
from pydantic.alias_generators import to_camel
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import make_url
from sqlalchemy.exc import SQLAlchemyError

from src.api.deps import ContainerStore, DbSession, OptionalStorage, Storage
from src.api.deps_rbac import CurrentSuperadmin
from src.api.v1.admin.schemas import (
    MAX_DAILY_TOKEN_LIMIT,
    AdminAppOut,
    AdminAppStatusResponse,
    AppListResponse,
    ApproveRequest,
    AttachmentReclaimSummary,
    AuditEventOut,
    AuditListResponse,
    BulkLimitsRequest,
    BulkLimitsResponse,
    BundleUrlResponse,
    DatabaseCredentialResponse,
    DatabaseReconcileCounts,
    DatabaseReconcileResponse,
    DeployCredentialResponse,
    DeployReconcileResponse,
    FeedbackItem,
    FeedbackResponse,
    LimitFields,
    LimitsPatchResponse,
    MarkDeployedRequest,
    MarkDeployedResponse,
    PatchAppRequest,
    PrefixReconcileCounts,
    ReclamationCandidate,
    ReclamationReportResponse,
    RejectRequest,
    RoleReconcileCounts,
    SandboxReconcileResponse,
    SandboxTagBackfillResponse,
    StorageReconcileResponse,
    SuspensionResponse,
    UsageResetResponse,
    UserLimitsOut,
    UsersResponse,
)
from src.api.v1.build_sessions.deps import OptionalSandbox
from src.api.v1.pagination import (
    DEFAULT_PAGE_SIZE,
    CursorQuery,
    LimitQuery,
    SearchQuery,
    clean_limit,
    clean_search,
    parse_cursor,
    split_keyset,
)
from src.config import settings
from src.core.errors import AppApiError
from src.db.base import async_session_factory
from src.db.models.app_registry import (
    STATUS_TRANSITIONS,
    AppRegistry,
    ApprovalRoute,
    AppStatus,
)
from src.db.models.attachment import Attachment
from src.db.models.audit import AuditLog
from src.db.models.feedback import Feedback
from src.db.models.project import Project
from src.db.models.project_database import ProjectDatabase
from src.db.models.token_usage import TokenUsage, TokenUsageKind
from src.db.models.user import User
from src.db.models.user_limit import UserLimit
from src.schemas import ADMIN_AUTH, AUTH_401, ErrorEnvelope, OkResponse, error_responses
from src.services.appdb.engine import get_maintenance_engine
from src.services.appdb.errors import AppDatabaseUnconfiguredError
from src.services.appdb.provision import sandbox_dsn
from src.services.appdb.reconcile import (
    AppDatabaseReconcileReport,
    advisory_database_sizes,
    reconcile_orphaned_app_databases,
)
from src.services.appdb.teardown import (
    TeardownHandles,
    restore_login,
    salt_the_earth,
    sever,
    teardown_handles,
)
from src.services.appserving.governance import nuke_app
from src.services.attachments import AttachmentReclaimResult, reclaim_orphaned_attachments
from src.services.audit.log import append_audit
from src.services.auth.refresh import revoke_all_sessions
from src.services.build_sessions.inventory import (
    FleetLister,
    FleetTagger,
    backfill_sandbox_tags,
    take_sandbox_inventory,
)
from src.services.build_sessions.pass_history import reclamation_pass_freshness
from src.services.build_sessions.reclamation_pass import run_reclamation_pass
from src.services.deploy.aca_publish import DeployNotConfiguredError, get_published_apps
from src.services.deploy.reconcile import reconcile_stalled_deployments
from src.services.rbac.roles import is_super_duper_admin, role_for
from src.services.redis import build_coordination_or_503, coordination_is_gone, get_redis
from src.services.sandbox import SandboxError
from src.services.storage import (
    ObjectStorage,
    StorageError,
    StorageSignError,
    submission_key,
)
from src.services.storage.reconcile import (
    PrefixCounts,
    StorageReconcileReport,
    reconcile_orphaned_storage,
)
from src.services.usage.gate import billable_spend, ist_today, resolve_daily_limit
from src.services.usage.limits import (
    DEFAULT_CONTEXT_HARD,
    DEFAULT_CONTEXT_SOFT,
    MODEL_CONTEXT_WINDOW,
    effective_context,
)

_log = structlog.get_logger()

router = APIRouter(prefix="/admin/apps", tags=["admin"])

# Every admin route is gated by `requires_superadmin`, which layers after
# `current_user`: an unauthenticated caller gets 401 and a non-super-admin 403, both
# bare `HTTPException` -> `{"detail"}` (documented as `DetailBody`). The routes' own
# raises are `AppApiError` -> `ErrorEnvelope`. This shared pair is spread into each
# route's `responses=` alongside that route's own explicit 4xx.
#
# The tuple itself now lives in `src/schemas/responses.py` beside `AUTH_401`, because
# `deploy/router.py`'s `unpublish` (#113) is gated by the same dependency and a second
# copy would be free to drift. Aliased under the module-private name the routes below
# already spread, so the shared definition costs no churn at 24 call sites.
_ADMIN_AUTH = ADMIN_AUTH


# --- helpers -------------------------------------------------------------------


def _project(
    app: AppRegistry,
    project_name: str,
    owner_username: str | None = None,
    *,
    database_bytes: int | None = None,
) -> AdminAppOut:
    # `database_bytes` is keyword-only WITH a default because this projection is shared with
    # `patch_app`, which re-reads one strict `(AppRegistry, name, email)` tuple and has no
    # size to hand over. A required parameter here would have forced a cluster probe into a
    # flag-flip endpoint that has no business talking to the maintenance engine at all.
    return AdminAppOut(
        app_id=app.id,
        name=project_name,
        owner_id=app.user_id,
        owner_username=owner_username,
        status=app.status,
        login_required=app.login_required,
        has_approved_snapshot=app.approved_submission_id is not None,
        submission_id=app.source_submission_id,
        commit_sha=app.source_commit_sha,
        submitted_at=app.submitted_at,
        approved_submission_id=app.approved_submission_id,
        approved_commit_sha=app.approved_commit_sha,
        approved_by=app.approved_by,
        approved_at=app.approved_at,
        # Historical runbook fields stay projected UNCONDITIONALLY: an app that later
        # moved to the self-publish lineage keeps its recorded runbook address visible
        # to the administrator (the older of its two addresses, labelled by the SPA) —
        # lineage suppresses the PROMPT below, never the history.
        deployed_at=app.deployed_at,
        deployed_url=app.deployed_url,
        # Exact and clock-skew-free (D7): ids, not timestamps. False for a
        # never-approved app (None == None); True for approved-but-undeployed —
        # UNLESS the lineage is self-publish (R17a/ASM8): the flag is a runbook
        # prompt, a self-published app never sets `deployed_submission_id`, and the
        # bare derivation would therefore read "Deploy needed" forever, prompting an
        # administrator to run a runbook that must not be run.
        redeploy_needed=(
            app.approval_route is not ApprovalRoute.SELF_PUBLISH
            and app.approved_submission_id != app.deployed_submission_id
        ),
        # The lineage itself (R17a/P5) — what the SPA keys the runbook affordances off
        # — and the submitted declaration (R15), so the review screen can lead with
        # the disagreement without a second call.
        approval_route=app.approval_route,
        declaration=app.declaration,
        database_bytes=database_bytes,
        rejection_note=app.rejection_note,
        created_at=app.created_at,
        updated_at=app.updated_at,
    )


async def _get_app_or_404(db: DbSession, app_id: uuid.UUID) -> AppRegistry:
    app = await db.get(AppRegistry, app_id)
    if app is None:
        raise AppApiError(404, "App not found.")
    return app


async def _transition(
    db: DbSession,
    app_id: uuid.UUID,
    target: AppStatus,
    *guards: sa.ColumnElement[bool],
    **extra: Any,
) -> bool:
    """Atomic guarded status transition (Express `setStatus`): update only when the
    current status is a legal source for `target` AND every extra `guard` predicate
    holds (the D5 seam — approve adds `source_submission_id == reviewed_id`, so a
    re-submit since review updates zero rows); zero rows updated → illegal (409).
    No `user_id` predicate here: an admin acts across owners."""
    result = await db.execute(
        sa.update(AppRegistry)
        .where(
            AppRegistry.id == app_id,
            AppRegistry.status.in_(tuple(STATUS_TRANSITIONS[target])),
            *guards,
        )
        .values(status=target, **extra)
        .returning(AppRegistry.id)
    )
    return result.first() is not None


# The `db:*` levers all act on a PROJECT-scoped resource (the database is keyed by project,
# not by app), which is why every one of them carries `appId` in its `detail`: `read_audit`
# finds a row by `resource_id == app_id` OR `detail["appId"]`, so without it the whole
# database half of the trail would be invisible in the app's audit drawer.
def _db_detail(app_id: uuid.UUID, handles: TeardownHandles) -> dict[str, Any]:
    """Audit `detail` for a database lever: NAMES only.

    Never the DSN, never the password, never the host's credentials (`security.md`, D11) —
    the same discipline `deploy-credential:mint` applies when it audits nothing but an
    expiry. A name is an identifier an operator can act on; the DSN is a credential.
    """
    return {"appId": str(app_id), "dbName": handles.db_name, "roleName": handles.role_name}


# Deliberately vague to the caller and specific in the logs: an operator learns the lever
# did not take (so they must retry rather than believe the app is sealed), and no internal
# error text crosses the API boundary.
_SANDBOX_UNAVAILABLE = "The sandbox service is unavailable. Please try again."
_DB_LEVER_FAILED = "The app's database could not be reached. Please try again."

# The reconcile/observe half's copy. Same posture, different subject: the whole CLUSTER, not
# one app's database — and a sweep must never answer with a partial report dressed as a
# clean one, so an unreachable cluster is a retryable failure, not an empty tally.
_DB_CLUSTER_UNREACHABLE = "The app-database cluster could not be reached. Please try again."

# The lineage refusals (U4: R17a, P5). Both NAME the dead end instead of looping in it —
# the administrator reading these is non-technical (P3), so the copy says what to DO, not
# which column disagreed. The first is the cutover's cost made visible: a queue item that
# predates the publish flow was backfilled `runbook`, and approving it would burn the
# admin's approval on an app its owner still could not publish (they would need a SECOND
# approval once they re-submitted properly). The second is mark-deployed's: recording a
# runbook deployment nobody performed, on an app whose owner publishes it themselves,
# would be a lie in the registry.
_RUNBOOK_ITEM_MUST_RESUBMIT = (
    "This submission predates the publish flow, and approving it would not let the "
    "developer publish. Ask them to re-submit from the app's Publish button — it "
    "returns to this queue with their declaration attached."
)
_SELF_PUBLISHED_HAS_NO_RUNBOOK = (
    "This app is on the self-publish route — the developer publishes it themselves, "
    "and there is no runbook deployment to record."
)

# Same posture again for the publish plane. An unconfigured `DEPLOY__*` block is a supported
# deployment (dev, test, anywhere not yet granted the registry role), so the operator learns the
# lever did not take rather than that a reconcile found nothing to do — the two are opposite
# facts and only one of them is true.
_PUBLISHING_UNAVAILABLE = "Publishing is not available right now. Please try again."


async def _advisory_sizes(db: DbSession, project_ids: Sequence[uuid.UUID]) -> dict[uuid.UUID, int]:
    """On-disk size per project database for the admin listing. ADVISORY, and best-effort.

    Two queries total for the whole page — one registry read, one cluster read — never a
    per-row probe. Every failure mode returns an empty map instead of raising: no substrate
    configured, no provisioned database on the page, or a cluster we could not reach. That
    asymmetry with `reconcile_databases` (which 503s on the same condition) is deliberate —
    a size is decoration, and an admin queue that 500s because a decoration failed is a far
    worse outcome than a queue that renders with the size column blank.

    Only `db_ready` rows are probed: a claim whose external sequence never finished may name
    a database that does not exist yet, and the size of a database is not the thing that
    tells anyone about it.
    """
    if not project_ids:
        return {}
    # Resolved lazily, in the body — this is the same accessor a route must never take as an
    # eager `Depends` (commit 6be7a9c), and here it is genuinely allowed to be absent.
    engine = get_maintenance_engine()
    if engine is None:
        return {}
    claims = (
        await db.execute(
            sa.select(ProjectDatabase.project_id, ProjectDatabase.db_name).where(
                ProjectDatabase.project_id.in_(project_ids),
                ProjectDatabase.db_ready.is_(True),
            )
        )
    ).all()
    if not claims:
        return {}
    try:
        by_name = await advisory_database_sizes(engine, [str(row[1]) for row in claims])
    except (SQLAlchemyError, OSError) as exc:
        # Never the exception text: a connection error can carry the maintenance host.
        _log.warning("admin_app_database_size_probe_failed", error_type=type(exc).__name__)
        return {}
    return {row[0]: by_name[row[1]] for row in claims if row[1] in by_name}


# --- endpoints -----------------------------------------------------------------


@router.get(
    "",
    responses=error_responses((400, ErrorEnvelope, "Invalid status filter"), *_ADMIN_AUTH),
)
async def list_apps(
    admin: CurrentSuperadmin,
    db: DbSession,
    status_filter: Annotated[str | None, Query(alias="status")] = None,
) -> AppListResponse:
    where = []
    valid = {member.value for member in AppStatus}
    if status_filter is not None and status_filter not in valid:
        # Reject an unknown ?status= rather than silently ignoring it and returning
        # every app (fail-open) — Express filtered and yielded an empty set.
        raise AppApiError(400, "Invalid status filter.")
    if status_filter in valid:
        where.append(AppRegistry.status == AppStatus(status_filter))
    # The pending list is a REVIEW QUEUE (R16): oldest submission first, so the
    # next app to review is on top — `created_at` is the wrong axis (it dates the
    # provision, not the submission). Every other view stays newest-created-first.
    order_by = (
        AppRegistry.submitted_at.asc()
        if status_filter == AppStatus.PENDING.value
        else AppRegistry.created_at.desc()
    )
    rows = (
        await db.execute(
            sa.select(AppRegistry, Project.name, User.email)
            .join(User, AppRegistry.user_id == User.id)
            .join(Project, AppRegistry.project_id == Project.id)
            .where(*where)
            .order_by(order_by)
            .limit(200)
        )
    ).all()
    # One probe for the whole page (never per row): the size column is advisory, so a
    # cluster that will not answer leaves it blank rather than failing the queue.
    sizes = await _advisory_sizes(db, [app.project_id for app, _name, _email in rows])
    return AppListResponse(
        apps=[
            _project(app, project_name, owner_email, database_bytes=sizes.get(app.project_id))
            for app, project_name, owner_email in rows
        ]
    )


@router.post(
    "/{app_id}/approve",
    responses=error_responses(
        (404, ErrorEnvelope, "App not found"),
        (
            409,
            ErrorEnvelope,
            "Not pending, re-submitted since review, artifact missing, "
            "or a runbook-lineage item that must be re-submitted",
        ),
        (503, ErrorEnvelope, "Storage temporarily unavailable"),
        *_ADMIN_AUTH,
    ),
)
async def approve(
    app_id: uuid.UUID,
    body: ApproveRequest,
    admin: CurrentSuperadmin,
    db: DbSession,
    storage: OptionalStorage,
) -> AdminAppStatusResponse:
    """Pin EXACTLY the submission the admin reviewed (D5/R7): the request carries the
    reviewed submission id, and the guarded UPDATE adds it as a predicate — a
    re-submit between review and this click updates zero rows → 409."""
    app = await _get_app_or_404(db, app_id)
    # Load-bearing PENDING-only pre-check — the mirror image of `enable`'s
    # DISABLED-only guard: →approved also permits DISABLED, and a kill-switched
    # app's source_submission_id is frozen (submit refuses disabled), so the
    # reviewed-id predicate alone would let an admin approve it directly,
    # bypassing the enable path. Approve reaches APPROVED only from PENDING.
    if app.status is not AppStatus.PENDING:
        raise AppApiError(409, "Only a pending app can be approved.")
    # The runbook lineage gets no new approvals (U4: P5, cutover). This item was in the
    # queue before the publish flow became the only route in — approving it would grant
    # nothing the citizen can use (the gate's self-publish rule needs the self_publish
    # lineage), wasting the admin's decision and looping the citizen back here for a
    # second one. A pre-check with NO atomic-guard twin, deliberately: unlike the
    # re-submit race below, `runbook` has no runtime writer (the 0030 backfill wrote it
    # once, in the migration; the publish flow only ever writes `self_publish`), so the
    # value read here cannot move under us. NULL passes — an interim row submitted
    # before the publish-flow writer lands keeps today's behaviour.
    if app.approval_route is ApprovalRoute.RUNBOOK:
        raise AppApiError(409, _RUNBOOK_ITEM_MUST_RESUBMIT)
    # Captured BEFORE any commit (never read ORM attributes across one). If a
    # re-submit lands after this read, the guarded UPDATE below refuses — and
    # submission ids are never reused, so on success this SHA belongs to the
    # reviewed submission. The owner id travels the same way, for the ASM19
    # self-approval check at the audit call below.
    commit_sha = app.source_commit_sha
    app_user_id = app.user_id

    # R11 — verify the reviewed artifact still exists before pinning it, so an app
    # can never reach APPROVED with a bundle that 404s at runbook time. Fail
    # closed: a storage ERROR is ambiguity, not absence (503, not 409).
    # An UNCONFIGURED store is the same ambiguity, and arrives as `None` (the None-tolerant
    # `OptionalStorage`): with no store there is nothing to verify against, so approving would
    # pin an artifact nobody checked. Answers the DOCUMENTED 503 the eager `Storage` dependency
    # could never reach — it raised at dependency-solve time, before this body ran at all.
    if storage is None:
        raise AppApiError(503, "Storage is temporarily unavailable. Please try again.")
    try:
        artifact = await storage.head(submission_key(app_id, body.submission_id))
    except StorageError as exc:
        raise AppApiError(503, "Storage is temporarily unavailable. Please try again.") from exc
    if artifact is None:
        raise AppApiError(409, "The reviewed submission's artifact is missing — re-review.")

    now = datetime.now(UTC)
    moved = await _transition(
        db,
        app_id,
        AppStatus.APPROVED,
        # PENDING-only, enforced ATOMICALLY too (not just the pre-check above):
        # STATUS_TRANSITIONS[APPROVED] also permits DISABLED, so without this guard the
        # UPDATE would let a kill-switched app be approved directly under a race. A pure
        # tightening — the mirror image of enable's DISABLED-only source.
        AppRegistry.status == AppStatus.PENDING,
        # The D5 guard: pin only what was actually reviewed.
        AppRegistry.source_submission_id == body.submission_id,
        approved_submission_id=body.submission_id,
        approved_commit_sha=commit_sha,
        approved_by=admin.id,
        approved_at=now,
    )
    if not moved:
        raise AppApiError(
            409, "This app was re-submitted since you reviewed it — please re-review."
        )
    # ASM19 — A SUPERADMIN APPROVING THEIR OWN APP IS RECORDED DISTINGUISHABLY, not
    # forbidden. RBAC has two computed roles and no concept of a second approver, and
    # ADR-0005 already books the missing separation of duties as an accepted risk — so
    # the answer is a trail that can be QUERIED, not a refusal that would leave a
    # superadmin unable to publish their own work at all. `approve:self` follows this
    # codebase's existing variant-action convention (`unpublish:unconfirmed`,
    # `config:loginRequired`) and the vocabulary is deliberately open (ASM6), so no
    # migration is involved. Both rows carry identical detail: only the action word
    # differs, which is exactly what makes "list every self-approval" one predicate.
    self_approved = app_user_id == admin.id
    await append_audit(
        db,
        actor_id=admin.id,
        action="approve:self" if self_approved else "approve",
        resource_type="app",
        resource_id=str(app_id),
        detail={"submissionId": str(body.submission_id), "commitSha": commit_sha},
    )
    await db.commit()
    return AdminAppStatusResponse(app_id=app_id, status=AppStatus.APPROVED)


@router.post(
    "/{app_id}/reject",
    responses=error_responses(
        (404, ErrorEnvelope, "App not found"),
        (409, ErrorEnvelope, "Only a pending app can be rejected"),
        *_ADMIN_AUTH,
    ),
)
async def reject(
    app_id: uuid.UUID, body: RejectRequest, admin: CurrentSuperadmin, db: DbSession
) -> AdminAppStatusResponse:
    app = await _get_app_or_404(db, app_id)
    if app.status is not AppStatus.PENDING:
        raise AppApiError(409, "Only a pending app can be rejected.")
    # Length is capped by `RejectRequest.note` (422 at the boundary) — no silent slice here.
    moved = await _transition(db, app_id, AppStatus.REJECTED, rejection_note=body.note or "")
    if not moved:
        raise AppApiError(409, "Could not reject in the current state.")
    await append_audit(
        db, actor_id=admin.id, action="reject", resource_type="app", resource_id=str(app_id)
    )
    await db.commit()
    return AdminAppStatusResponse(app_id=app_id, status=AppStatus.REJECTED)


@router.patch(
    "/{app_id}",
    responses=error_responses((404, ErrorEnvelope, "App not found"), *_ADMIN_AUTH),
)
async def patch_app(
    app_id: uuid.UUID, body: PatchAppRequest, admin: CurrentSuperadmin, db: DbSession
) -> AdminAppOut:
    app = await _get_app_or_404(db, app_id)
    login_flipped = False
    if body.login_required is not None:
        login_flipped = bool(app.login_required) != body.login_required
        app.login_required = body.login_required
    await db.flush()
    # Audit the gated change (ADR-0005). A no-op patch still writes nothing.
    if login_flipped:
        await append_audit(
            db,
            actor_id=admin.id,
            action="config:loginRequired",
            resource_type="app",
            resource_id=str(app_id),
            detail={"count": 1 if body.login_required else 0},
        )
    await db.commit()
    # Re-read the row joined to its project + owner so the response name is project-sourced
    # (#48) and every column reflects the committed state; the joined scalars are non-null by
    # the FK invariants, and `.one()` fails closed if the row vanished under us.
    app, project_name, owner_email = (
        await db.execute(
            sa.select(AppRegistry, Project.name, User.email)
            .join(User, AppRegistry.user_id == User.id)
            .join(Project, AppRegistry.project_id == Project.id)
            .where(AppRegistry.id == app_id)
        )
    ).one()
    return _project(app, project_name, owner_email)


@router.post(
    "/{app_id}/disable",
    responses=error_responses(
        (404, ErrorEnvelope, "App not found"),
        (409, ErrorEnvelope, "Only an approved app can be disabled"),
        (503, ErrorEnvelope, "The app database could not be severed"),
        *_ADMIN_AUTH,
    ),
)
async def disable(
    app_id: uuid.UUID, admin: CurrentSuperadmin, db: DbSession
) -> AdminAppStatusResponse:
    """THE kill switch. Flipping the status stops the platform serving the app; SEVERING
    its database is what stops the app's own running container reaching data, and that is
    now the only data kill there is — the shared-table plane and its per-request app-key
    403 are gone, so a deployed container holds a real credential and answers to nobody but
    PostgreSQL. Hence the sever, not merely the status.

    Order: the status transition FIRST, the sever after it. `→approved` legally accepts
    `pending`, so the guarded UPDATE is the only thing separating "disable an approved app"
    from "touch some other app's state machine" — an early-return 409 must leave the
    database exactly as it found it. A sever failure then 503s and rolls the transition back
    (`get_db`), so status and reality never disagree in the dangerous direction: whatever
    the sever did manage is `NOLOGIN` first, i.e. it fails CLOSED, and the retry is safe
    because `sever` is idempotent.

    NOT severed here, deliberately and per the runbook: the app's deploy Blob SAS (see
    `mint_deploy_credential`). Revoking that means deleting the container's stored access
    policy, which is an operator step — do not read this response as "the files are locked".
    """
    app = await _get_app_or_404(db, app_id)
    project_id = app.project_id
    if not await _transition(db, app_id, AppStatus.DISABLED):
        raise AppApiError(409, "Only an approved app can be disabled.")
    await append_audit(
        db, actor_id=admin.id, action="disable", resource_type="app", resource_id=str(app_id)
    )
    # Scalars read pre-commit; an app from the era before per-project databases (or a
    # deployment with no substrate at all) simply has no row — a clean no-op, not an error.
    handles = await teardown_handles(db, project_id)
    if handles is not None:
        try:
            severed = await sever(db_name=handles.db_name, role_name=handles.role_name)
        # OSError as well as SQLAlchemyError: an unreachable cluster surfaces as a raw
        # connection error out of `engine.connect()` before the driver ever wraps it. The
        # size probe and `reconcile-databases` in this file already catch both — this lever
        # must too, because `disable` is the ONLY data kill for a deployed app now that the
        # X-App-Key plane is gone, and an undocumented 500 is the response an operator is
        # least likely to retry.
        except (SQLAlchemyError, OSError) as exc:
            raise AppApiError(503, _DB_LEVER_FAILED) from exc
        # A registry row exists but `sever` returned False → the substrate is unconfigured,
        # so the lever cannot actually reach the cluster. Fail loud rather than record a
        # `db:revoke` for a sever that never ran (the audit must not lie about the only data
        # kill there is).
        if not severed:
            raise AppApiError(503, _DB_LEVER_FAILED)
        await append_audit(
            db,
            actor_id=admin.id,
            action="db:revoke",
            resource_type="project",
            resource_id=str(project_id),
            detail=_db_detail(app_id, handles),
        )
    await db.commit()
    return AdminAppStatusResponse(app_id=app_id, status=AppStatus.DISABLED)


@router.post(
    "/{app_id}/enable",
    responses=error_responses(
        (404, ErrorEnvelope, "App not found"),
        (409, ErrorEnvelope, "Only a disabled app can be re-enabled"),
        (503, ErrorEnvelope, "The app database could not be restored"),
        *_ADMIN_AUTH,
    ),
)
async def enable(
    app_id: uuid.UUID, admin: CurrentSuperadmin, db: DbSession
) -> AdminAppStatusResponse:
    """`disable`'s inverse, and it must undo BOTH halves — restoring the status while
    leaving the role `NOLOGIN` would hand back an app that serves pages and cannot read a
    row. The restore sits INSIDE the guarded path for the same reason the sever does: the
    two 409s below are the approve gate, and a refused enable must not re-open a database
    the kill switch closed."""
    app = await _get_app_or_404(db, app_id)
    project_id = app.project_id
    # Load-bearing guard: →approved also permits `pending`, so without this an enable
    # could promote a pending app past the approve gate (which pins the reviewed
    # artifact). Approve reaches APPROVED only from PENDING; enable only from DISABLED.
    if app.status is not AppStatus.DISABLED:
        raise AppApiError(409, "Only a disabled app can be re-enabled.")
    # Artifact-pin guard: re-enabling restores APPROVED, so it must never resurrect a
    # legacy DISABLED row the migration spared with a NULL approved pin (the D13 state
    # the schema otherwise prevents) — approved-with-no-artifact. A DISABLED row with no
    # pin updates zero rows → the same 409.
    if not await _transition(
        db, app_id, AppStatus.APPROVED, AppRegistry.approved_submission_id.is_not(None)
    ):
        raise AppApiError(409, "Only a disabled app can be re-enabled.")
    await append_audit(
        db, actor_id=admin.id, action="enable", resource_type="app", resource_id=str(app_id)
    )
    handles = await teardown_handles(db, project_id)
    if handles is not None:
        try:
            restored = await restore_login(db_name=handles.db_name, role_name=handles.role_name)
        # Both error families, for the same reason as `disable` above.
        except (SQLAlchemyError, OSError) as exc:
            raise AppApiError(503, _DB_LEVER_FAILED) from exc
        # False → unconfigured substrate: the restore never reached the cluster, so it must
        # not record a `db:restore` that lies (the mirror of `disable`'s guard above).
        if not restored:
            raise AppApiError(503, _DB_LEVER_FAILED)
        await append_audit(
            db,
            actor_id=admin.id,
            action="db:restore",
            resource_type="project",
            resource_id=str(project_id),
            detail=_db_detail(app_id, handles),
        )
    await db.commit()
    return AdminAppStatusResponse(app_id=app_id, status=AppStatus.APPROVED)


# Minutes-scale (deliberately far under the ABC's 7-day ceiling): long enough for an
# out-of-band review download, short enough that a leaked URL dies fast (R15).
_BUNDLE_URL_TTL = timedelta(minutes=15)


@router.get(
    "/{app_id}/bundle-url",
    responses=error_responses(
        (404, ErrorEnvelope, "App not found"),
        (409, ErrorEnvelope, "App has no submission to download"),
        (503, ErrorEnvelope, "Storage temporarily unavailable"),
        *_ADMIN_AUTH,
    ),
)
async def bundle_download_url(
    app_id: uuid.UUID, admin: CurrentSuperadmin, db: DbSession, storage: OptionalStorage
) -> BundleUrlResponse:
    """Mint a short-TTL signed download URL for the app's submission under review,
    and AUDIT the pull (R15): an admin reading a citizen's full source is precisely
    the gated action ADR-0005 says to record. Deliberately NOT part of the list
    projection — the URL is a bearer credential, and listing would mass-issue one
    per row. The URL is blob-scoped (bound to this one submission's key), and the
    audit `detail` carries only the submission id + SHA — NEVER the URL itself."""
    app = await _get_app_or_404(db, app_id)
    submission_id, commit_sha = app.source_submission_id, app.source_commit_sha
    if submission_id is None:
        # No submission is a non-event: no URL, and no audit row for nothing.
        raise AppApiError(409, "This app has no submission to download.")
    # An unconfigured store arrives as `None` (the None-tolerant `OptionalStorage`) → the SAME
    # documented 503 as a signing failure below, and no audit row for a URL that never existed.
    # An eager `Storage` dependency raised at solve time, ahead of even the 404/409 above.
    if storage is None:
        raise AppApiError(503, "Storage is temporarily unavailable. Please try again.")
    try:
        url = await storage.signed_read_url(
            submission_key(app_id, submission_id), expires_in=_BUNDLE_URL_TTL
        )
    except StorageError as exc:
        raise AppApiError(503, "Storage is temporarily unavailable. Please try again.") from exc
    await append_audit(
        db,
        actor_id=admin.id,
        action="bundle:download",
        resource_type="app",
        resource_id=str(app_id),
        detail={"submissionId": str(submission_id), "commitSha": commit_sha},
    )
    await db.commit()
    return BundleUrlResponse(
        url=url,
        submission_id=submission_id,
        commit_sha=commit_sha,
        expires_in_seconds=int(_BUNDLE_URL_TTL.total_seconds()),
    )


@router.post(
    "/{app_id}/deploy-credential",
    responses=error_responses(
        (404, ErrorEnvelope, "App not found"),
        (409, ErrorEnvelope, "Storage cannot mint a long-lived credential in this configuration"),
        (503, ErrorEnvelope, "Storage temporarily unavailable"),
        *_ADMIN_AUTH,
    ),
)
async def mint_deploy_credential(
    app_id: uuid.UUID,
    admin: CurrentSuperadmin,
    db: DbSession,
    container_store: ContainerStore,
) -> DeployCredentialResponse:
    """Mint the deployed app's long-lived, container-scoped Blob credential (R2) — the runbook's
    step-5 `BIAL_BLOB_CONTAINER_URL` + `BIAL_BLOB_SAS` pair, and the answer to its former KNOWN
    GAP (a session SAS expires in ≤7 days and stranded every live app's storage).

    Deliberately independent: the credential reaches the app's own container DIRECTLY, so a
    deployed app never proxies file traffic through the control-plane. That independence cuts
    both ways and the runbook says so — `disable` kill-switches the DATA plane but does NOT
    revoke this SAS; revoking it means deleting the app's stored access policy.

    Like `bundle-url`, the minted token is a bearer credential: audited as an EVENT (who, which
    app, when it dies) with the SAS value itself never logged and never in the audit `detail`
    (security.md). Not part of any list projection — a mint is always an explicit, recorded act.
    """
    await _get_app_or_404(db, app_id)
    if container_store is None:
        # Fail closed and say what to fix: object storage is simply not configured here, so
        # there is no container and nothing to sign with (the dependency yields None, D2).
        raise AppApiError(409, "Object storage is not configured on this deployment.")
    try:
        credential = await container_store.mint_deploy_container_sas(app_id)
    except StorageSignError as exc:
        # The managed-identity config: only a user-delegation key is available and Azure caps
        # those at 7 days. Actionable, and admin-only — but still no internal error text.
        raise AppApiError(
            409,
            "This deployment's storage uses managed identity, which cannot issue a credential "
            "beyond 7 days. Configure a storage account key to mint a deploy credential.",
        ) from exc
    except StorageError as exc:
        raise AppApiError(503, "Storage is temporarily unavailable. Please try again.") from exc
    await append_audit(
        db,
        actor_id=admin.id,
        action="deploy-credential:mint",
        resource_type="app",
        resource_id=str(app_id),
        # Expiry ONLY — never the SAS, never the container URL's query string.
        detail={"expiresAt": credential.expires_at.isoformat()},
    )
    await db.commit()
    return DeployCredentialResponse(
        container_url=container_store.container_url(app_id),
        sas=credential.sas,
        expires_at=credential.expires_at,
    )


@router.post(
    "/{app_id}/database-credential",
    responses=error_responses(
        (404, ErrorEnvelope, "App not found"),
        (409, ErrorEnvelope, "This project has no database to reveal"),
        *_ADMIN_AUTH,
    ),
)
async def reveal_database_credential(
    app_id: uuid.UUID, admin: CurrentSuperadmin, db: DbSession
) -> DatabaseCredentialResponse:
    """Reveal the project database's connection string — the go-live runbook's
    `BIAL_DATABASE_URL`, byte-for-byte the value the sandbox is injected with (ADR-0028).

    The database is keyed by PROJECT while this router is keyed by app, and that is fine
    rather than merely tolerable: the runbook only ever reveals for an APPROVED app, so an
    app row always exists and `app.project_id` is the resolution. The audit row is
    project-scoped and carries `appId` so it still shows up in the app's trail.

    Modelled on `mint_deploy_credential`, including the parts that are security decisions
    rather than style: the secret is returned in the RESPONSE BODY ONLY, it is never part of
    any list projection (a listing would mass-reveal one credential per row), and the audit
    `detail` records the role name and the host — WHO can connect and WHERE — but never the
    DSN and never the password. A reveal is an event, and the event is what gets recorded.

    There is no rotation lever here on purpose: one role serves both the sandbox and the
    deployed container, so a reset would cut a live deployment off. Leak response is a
    deliberate, separate operator story.
    """
    app = await _get_app_or_404(db, app_id)
    record = (
        await db.execute(
            sa.select(ProjectDatabase).where(
                ProjectDatabase.project_id == app.project_id,
                ProjectDatabase.db_ready.is_(True),
            )
        )
    ).scalar_one_or_none()
    if record is None:
        # Absent OR not-terminal: a claim row whose external sequence never finished means
        # the cross-app wall may be down, so its DSN is not a thing to hand an operator.
        # Fail-closed and non-500 — "no database" is a state, not a bug.
        raise AppApiError(409, "This project has no database yet.")
    try:
        # Resolved lazily, INSIDE the body: an eager `Depends` would raise ahead of the 404
        # and 409 above and turn both into 500s (commit 6be7a9c's whole lesson).
        dsn = sandbox_dsn(record)
    except AppDatabaseUnconfiguredError as exc:
        raise AppApiError(
            409, "Per-project databases are not configured on this deployment."
        ) from exc
    # Plain scalars before the commit — after it, every one of these is lazy I/O.
    db_name, role, host = record.db_name, record.role_name, _dsn_host(dsn)
    await append_audit(
        db,
        actor_id=admin.id,
        action="db:reveal",
        resource_type="project",
        resource_id=str(app.project_id),
        # Role + host + database NAME. The DSN differs from these by exactly one field —
        # the password — and that field is the entire reason this row is not the DSN.
        detail={"appId": str(app_id), "roleName": role, "dbName": db_name, "host": host},
    )
    await db.commit()
    return DatabaseCredentialResponse(dsn=dsn, db_name=db_name, role_name=role, host=host)


def _dsn_host(dsn: str) -> str:
    """`host:port` out of a DSN, parsed rather than sliced so no credential can ride along
    into an audit row (`make_url` keeps the password in a separate field)."""
    url = make_url(dsn)
    host = url.host or ""
    return f"{host}:{url.port}" if url.port else host


@router.post(
    "/{app_id}/mark-deployed",
    responses=error_responses(
        (404, ErrorEnvelope, "App not found"),
        (
            409,
            ErrorEnvelope,
            "Not approved, or a self-published app with no runbook deployment to record",
        ),
        *_ADMIN_AUTH,
    ),
)
async def mark_deployed(
    app_id: uuid.UUID,
    admin: CurrentSuperadmin,
    db: DbSession,
    body: MarkDeployedRequest | None = None,
) -> MarkDeployedResponse:
    """Record that a human ran the go-live runbook for the approved pin (R17, D7),
    and — optionally — WHERE the app now lives (R5).
    A MARKER, not a status: `STATUS_TRANSITIONS` is untouched — but still a guarded
    UPDATE, so a marker can never attach to an unapproved app, and it pins the
    approved submission ATOMICALLY (`deployed := approved` inside the UPDATE, so a
    racing re-approval cannot tear the pair). `redeploy_needed` derives as
    `approved_submission_id != deployed_submission_id` in the projection.

    The URL is DATA, not automation: whatever the runbook operator pastes is what the
    owner's Live link points at — the platform never derives, probes, or verifies it.
    The body (and the field) stay optional, so the pre-R5 call — the admin SPA's bare
    `{}` — still marks a deploy exactly as it did before. `.returning()` gives the
    stamped values as detached scalars: nothing ORM-shaped crosses the `commit()`
    below (`prefer-returning-over-refresh-across-commit`)."""
    app = await _get_app_or_404(db, app_id)
    # A self-published app has NO runbook step (U4: R17a): its owner publishes the
    # approved version themselves, so a marker here would record a deployment nobody
    # performed — and `deployed := approved` would then read as redeploy-not-needed on
    # a runbook nobody is meant to run. Refuse with copy naming the lineage; the
    # guarded UPDATE below carries the atomic twin.
    if app.approval_route is ApprovalRoute.SELF_PUBLISH:
        raise AppApiError(409, _SELF_PUBLISHED_HAS_NO_RUNBOOK)
    recorded_url = None if body is None else body.deployed_url
    stamped_values: dict[str, Any] = {
        "deployed_submission_id": AppRegistry.approved_submission_id,
        "deployed_at": sa.func.now(),
    }
    # Absent URL => leave the column alone (see `MarkDeployedRequest`), which is why
    # this is a conditional key and not `deployed_url=recorded_url`: the latter would
    # blank the live link on every URL-less re-mark.
    if recorded_url is not None:
        stamped_values["deployed_url"] = str(recorded_url)
    stamped = (
        await db.execute(
            sa.update(AppRegistry)
            .where(
                AppRegistry.id == app_id,
                AppRegistry.status == AppStatus.APPROVED,
                # Belt over braces: approve is the only path to APPROVED and always
                # pins, but a marker referencing NO submission would be a lie.
                AppRegistry.approved_submission_id.is_not(None),
                # The lineage pre-check's atomic twin (U4). Reachable only through a
                # double race (a re-submit through the publish flow AND a re-approval,
                # both between our read and this UPDATE), but the cost of a miss is a
                # recorded deployment nobody performed — belt over braces again.
                # IS DISTINCT FROM, not !=: a NULL lineage must pass.
                AppRegistry.approval_route.is_distinct_from(ApprovalRoute.SELF_PUBLISH),
            )
            .values(**stamped_values)
            .returning(
                AppRegistry.deployed_submission_id,
                AppRegistry.deployed_at,
                AppRegistry.deployed_url,
                AppRegistry.approved_commit_sha,
            )
        )
    ).first()
    if stamped is None:
        raise AppApiError(409, "Only an approved app can be marked deployed.")
    await append_audit(
        db,
        actor_id=admin.id,
        action="mark-deployed",
        resource_type="app",
        resource_id=str(app_id),
        detail={
            "submissionId": str(stamped.deployed_submission_id),
            "commitSha": stamped.approved_commit_sha,
            # The app's public address — not a credential (unlike the SAS its
            # `deploy-credential` sibling deliberately keeps out of the trail).
            "deployedUrl": stamped.deployed_url,
        },
    )
    await db.commit()
    return MarkDeployedResponse(
        app_id=app_id,
        deployed_submission_id=stamped.deployed_submission_id,
        deployed_at=stamped.deployed_at,
        deployed_url=stamped.deployed_url,
    )


@router.delete(
    "/{app_id}",
    status_code=status.HTTP_200_OK,
    responses=error_responses((404, ErrorEnvelope, "App not found"), *_ADMIN_AUTH),
)
async def hard_delete(
    app_id: uuid.UUID,
    admin: CurrentSuperadmin,
    db: DbSession,
    storage: Storage,
    container_store: ContainerStore,
) -> OkResponse:
    """Hard-delete an app and sweep its artifacts. The ONLY storage route on this router that
    keeps the raising `Storage`, deliberately: it neither documents a 503 nor maps `StorageError`
    — `nuke_app` requires a real store (its submissions ENUMERATION raises rather than strand
    blobs nobody can find), and it has no storage-unavailable copy to answer with. Storage
    missing here is a deploy bug, and a 500 is the honest answer to a deploy bug; inventing a
    503 would be inventing a contract. Contrast `approve` / `bundle-url` / `reconcile-storage`
    above, which all promise a 503 and so must take `OptionalStorage`.

    Restructured to `delete_project`'s shape for the database half (D10): gather handles →
    audit → `nuke_app` → COMMIT → `salt_the_earth`. `DROP DATABASE` cannot run inside a
    transaction block, so the drop physically cannot sit next to `nuke_app`; and the
    ordering is the safety argument anyway — nothing irreversible outside PostgreSQL's own
    rows happens until the commit that authorizes it. `nuke_app` stays blob-only.

    The PROJECT survives an app hard-delete, so its `project_databases` row is deleted
    explicitly (the project-delete path gets that for free via `ON DELETE CASCADE`). No row
    = never provisioned, which is precisely what makes the next build re-provision a clean
    database instead of injecting a DSN to one that is about to stop existing."""
    app = await _get_app_or_404(db, app_id)
    project_id = app.project_id
    # Plain scalars, read BEFORE the commit that removes the row they come from.
    handles = await teardown_handles(db, project_id)
    # Audit BEFORE destruction — the accountability row (no FK to the app) survives.
    await append_audit(
        db,
        actor_id=admin.id,
        action="app:delete",
        resource_type="app",
        resource_id=str(app_id),
    )
    if handles is not None:
        await append_audit(
            db,
            actor_id=admin.id,
            action="db:drop",
            resource_type="project",
            resource_id=str(project_id),
            detail=_db_detail(app_id, handles),
        )
        await db.execute(
            sa.delete(ProjectDatabase).where(ProjectDatabase.project_id == project_id)
        )
    await nuke_app(db, storage, app_id, container_store)
    await db.commit()
    if handles is not None:
        # Post-commit, never-raising, and its first step IS the sever. A failed drop leaves
        # a logged orphan for the reconciler; it must never un-delete a committed registry.
        await salt_the_earth(db_name=handles.db_name, role_name=handles.role_name)
    return OkResponse(ok=True)


def _counts(counts: PrefixCounts) -> PrefixReconcileCounts:
    return PrefixReconcileCounts(
        scanned=counts.scanned,
        owned=counts.owned,
        within_grace=counts.within_grace,
        eligible=counts.eligible,
        deleted=counts.deleted,
    )


def _reconcile_response(
    report: StorageReconcileReport, reclaim: AttachmentReclaimResult
) -> StorageReconcileResponse:
    return StorageReconcileResponse(
        attachments=_counts(report.attachments),
        snapshots=_counts(report.snapshots),
        recovery=_counts(report.recovery),
        submissions=_counts(report.submissions),
        apps=_counts(report.apps),
        ownerless_submissions=report.ownerless_submissions,
        attachment_reclaim=AttachmentReclaimSummary(
            reclaimed=reclaim.reclaimed,
            freed_bytes=reclaim.freed_bytes,
            swept_keys=reclaim.swept_keys,
        ),
    )


async def _reclaim_orphans_for_all_users(
    db: DbSession, storage: ObjectStorage
) -> AttachmentReclaimResult:
    """Fold the U9 never-sent-upload reclaim into the operator sweep, summed across every owner.

    Per-user by contract (ADR-0004): `reclaim_orphaned_attachments` is user-scoped — a colliding
    client token in another user's transcript must never shield an orphan — so the sweep enumerates
    the distinct owners that have any attachment and drives one pass each. This reclaims exactly
    the orphans the blob-vs-row `reconcile_orphaned_storage` pass CANNOT: that pass treats any
    still-rowed upload as owned, so a never-sent attachment (row intact, referenced by no message)
    survives it forever — the quota leak U9 fixes. Each pass commits + best-effort-sweeps its own
    user's blobs; nothing here is left pending, so the endpoint's trailing audit `commit` still
    lands."""
    owner_ids = (await db.execute(sa.select(sa.distinct(Attachment.user_id)))).scalars().all()
    reclaimed = freed_bytes = swept_keys = 0
    for owner_id in owner_ids:
        result = await reclaim_orphaned_attachments(db, storage, user_id=owner_id)
        reclaimed += result.reclaimed
        freed_bytes += result.freed_bytes
        swept_keys += result.swept_keys
    return AttachmentReclaimResult(
        reclaimed=reclaimed, freed_bytes=freed_bytes, swept_keys=swept_keys
    )


@router.post(
    "/reconcile-storage",
    responses=error_responses(
        (503, ErrorEnvelope, "Storage temporarily unavailable"), *_ADMIN_AUTH
    ),
)
async def reconcile_storage(
    admin: CurrentSuperadmin, db: DbSession, storage: OptionalStorage
) -> StorageReconcileResponse:
    """Sweep the whole object store against the database and reclaim ownerless, past-grace blobs
    (R11/R12/R13) — the recovery lever for a cleanup that a `_log.warning` was the only trail of.

    OPERATOR-INVOKED (KD-7): THIS sweep is not on a schedule — nothing but a superadmin calls it.
    A superadmin drives this endpoint by hand (headlessly too — the admin router declares no CSRF,
    so `curl -b "session=<jwt>"` works); a grace-period sweep nothing calls reclaims nothing.

    Corrected 2026-08-11 (ADR-0029): this said "there is NO scheduler in this repo, and an
    in-process one was deliberately rejected". That was FALSE when written — a 300 s sandbox
    sweeper had run in the lifespan since v1.6.5 — and is doubly false now that ADR-0011 is
    Accepted. Sixteen copies of that claim are why two data-loss incidents were triaged as
    scheduling failures. Only the narrow statement above survives; putting this sweep on the
    Taskiq scheduler is available and unclaimed.

    Two passes, one operator action. First the blob-vs-row diff sweep (`att/` + `snapshots/`
    delete; `submissions/` + `apps/` report-only). Then the U9 never-sent-upload reclaim, per
    owning user (`_reclaim_orphans_for_all_users`) — the pass that finally runs
    `reclaim_orphaned_attachments` in prod rather than only in its unit test, closing the
    quota leak the diff sweep cannot (it treats a still-rowed orphan as owned). `attachmentReclaim`
    in the response and the `reclaimed*` audit fields carry its tallies (counts only, security.md).

    Safe at any time: only a key with NO owning row AND older than the 24h grace is deleted, so a
    blob a concurrent submit/upload is about to record a row for is protected (R12). `submissions/`
    and `apps/` are REPORT-ONLY (the report's whole point) — `submissions` because deleting the
    immutable approval record is the open D7 governance call, `apps` because it has no known writer
    since migration 0017. Idempotent: a second run is a no-op. A `StorageError` surfaces as a
    retryable 503 rather than being lost to a log line — INCLUDING the unconfigured-store case,
    which arrives here as `storage is None` (the None-tolerant `OptionalStorage` dependency)
    rather than the solve-time 500 an eager `Storage` (`get_storage()`) annotation raised."""
    if storage is None:
        # An unconfigured store is the documented 503, the same class of answer as the transient
        # failure below — never the solve-time 500 an eager `Storage` dependency would raise.
        raise AppApiError(503, "Storage is temporarily unavailable. Please try again.")
    try:
        report = await reconcile_orphaned_storage(db, storage)
        reclaim = await _reclaim_orphans_for_all_users(db, storage)
    except StorageError as exc:
        raise AppApiError(503, "Storage is temporarily unavailable. Please try again.") from exc
    await append_audit(
        db,
        actor_id=admin.id,
        action="storage:reconcile",
        resource_type="storage",
        # Counts only in the trail (never keys, security.md): what each sweep reclaimed + the
        # ownerless-submission tally the D7 call needs.
        detail={
            "attDeleted": report.attachments.deleted,
            "snapshotsDeleted": report.snapshots.deleted,
            "recoveryDeleted": report.recovery.deleted,
            "ownerlessSubmissions": report.ownerless_submissions,
            "reclaimedAttachments": reclaim.reclaimed,
            "reclaimedBytes": reclaim.freed_bytes,
            "reclaimedKeys": reclaim.swept_keys,
        },
    )
    await db.commit()
    return _reconcile_response(report, reclaim)


def _database_reconcile_response(
    report: AppDatabaseReconcileReport,
) -> DatabaseReconcileResponse:
    return DatabaseReconcileResponse(
        databases=DatabaseReconcileCounts(
            scanned=report.databases.scanned,
            not_ours=report.databases.not_ours,
            owned=report.databases.owned,
            orphaned=report.databases.orphaned,
            unknown_age=report.databases.unknown_age,
            oldest_orphan_age_hours=report.databases.oldest_orphan_age_hours,
        ),
        roles=RoleReconcileCounts(
            scanned=report.roles.scanned,
            not_ours=report.roles.not_ours,
            owned=report.roles.owned,
            stranded=report.roles.stranded,
            paired=report.roles.paired,
        ),
    )


@router.post(
    "/reconcile-sandboxes",
    responses=error_responses(
        (503, ErrorEnvelope, "The sandbox control plane is temporarily unavailable"),
        *_ADMIN_AUTH,
    ),
)
async def reconcile_sandboxes(
    admin: CurrentSuperadmin, db: DbSession, sandbox: OptionalSandbox
) -> SandboxReconcileResponse:
    """Diff the sandbox containers Azure is billing for against the ones the registry tracks,
    and REPORT the ones nothing is tracking (#83 follow-up).

    THE GAP THIS CLOSES. `sweep_all` walks `bial:sandbox:registry:*`, so it reaches exactly the
    containers it already has a record of. A sandbox whose registry entry is gone — a flushed or
    replaced Redis, a container older than the registry, a teardown that failed after
    `delete_registry` — is invisible to it forever and bills until a human goes looking. One did,
    for twelve days.

    REPORTS, NEVER DELETES, and that is the design rather than a first iteration: a container
    provisioned seconds ago by a start that has not yet written its registry hash is
    indistinguishable from an orphan here (`_start_locked` takes the lock BEFORE it provisions),
    and that ambiguity is not something to hand an irreversible ARM delete. The operator deletes,
    with `release` or the CLI.

    A sibling of `reconcile-storage` and `reconcile-databases` in every operational respect:
    superadmin-gated, operator-invoked, idempotent, and AUDITED WITH COUNTS ONLY — a sandbox name
    embeds its app's uuid, so a name list is an inventory of who is running what.

    The names come back in the RESPONSE (the operator has to know what to delete) but never in
    the audit row or a log line — the same split `reconcile-storage` makes for blob keys."""
    if sandbox is None:
        raise AppApiError(503, _SANDBOX_UNAVAILABLE)
    if not isinstance(sandbox, FleetLister):
        # A deployment whose sandbox client cannot enumerate (a fake, a future substrate that
        # has not implemented it). Retryable-shaped rather than a 500: nothing is wrong with
        # the request, this deployment simply cannot answer it.
        raise AppApiError(503, _SANDBOX_UNAVAILABLE)
    with build_coordination_or_503():
        try:
            inventory = await take_sandbox_inventory(get_redis(), sandbox)
        except SandboxError as exc:
            # Never a partial inventory: a half-enumerated fleet is indistinguishable from a
            # clean one, and "clean" is the answer that gets an orphan forgotten.
            raise AppApiError(503, _SANDBOX_UNAVAILABLE) from exc
        await append_audit(
            db,
            actor_id=admin.id,
            action="sandbox:reconcile",
            resource_type="sandbox",
            resource_id=None,
            detail={
                "live": len(inventory.live),
                "registered": len(inventory.registered),
                "unregistered": len(inventory.unregistered),
                "registeredMissing": len(inventory.registered_missing),
            },
        )
        await db.commit()
        last_pass, stale = await reclamation_pass_freshness(db)
        return SandboxReconcileResponse(
            live=len(inventory.live),
            registered=len(inventory.registered),
            unregistered=list(inventory.unregistered),
            registered_missing=list(inventory.registered_missing),
            last_reclamation_pass_at=last_pass,
            reclamation_stale=stale,
        )
    # Reached only when `build_coordination_or_503` skipped the body on an unconfigured
    # Redis. The registry IS half of this reconcile — without it there is no "registered"
    # set to diff the live fleet against, so an answer here would be a fleet inventory
    # dressed up as a reconciliation, with every live container reported as unregistered.
    raise coordination_is_gone()


@router.post(
    "/reclamation-report",
    responses=error_responses(
        (503, ErrorEnvelope, "The sandbox control plane is temporarily unavailable"),
        *_ADMIN_AUTH,
    ),
)
async def reclamation_report(
    admin: CurrentSuperadmin, db: DbSession, sandbox: OptionalSandbox
) -> ReclamationReportResponse:
    """WHAT WOULD THE RECLAMATION PASS DELETE RIGHT NOW? (R20.)

    THE QUESTION THERE WAS NO WAY TO ASK. Before flipping `SANDBOX__RECLAIM_DESTROY` an operator
    could learn what a pass would do in exactly two ways: read the worker's logs after a pass had
    already run, or turn destruction on and find out. Both answer after the decision is made. The
    whole two-flag design rests on there being a state in which somebody reads a candidate list
    and agrees with it — and until now that list only existed in a log.

    IT DESTROYS NOTHING, AND CANNOT. `run_reclamation_pass` is the pure half: it enumerates ARM,
    reads the coordination store as a spare-list, reads the app table, and returns verdicts. The
    staging stamp and the destroy arm live in the worker task, not here, and neither is reachable
    from this function. That is a property of the seam, not a flag this endpoint remembers to
    check.

    ANSWERS WITH THE FLAGS OFF, deliberately. Refusing to preview because reclamation is disabled
    would withhold the report exactly when it is most wanted — the deployment deciding whether to
    enable it. So the flags come back in the response instead, because they change what the same
    `destroy` list MEANS: a preview on a report-only deployment, a description of what is about to
    happen on an armed one.

    Audited with COUNTS ONLY, like every sibling report here. A sandbox name embeds 28 hex
    characters of its app's uuid, so a name list in the audit log is a durable record of who was
    running what. The names go in the response, where the operator needs them."""
    if sandbox is None or not isinstance(sandbox, FleetLister):
        # Retryable-shaped, not a 500: nothing is wrong with the request — this deployment has no
        # ARM access, or a substrate that cannot enumerate, so it cannot answer.
        raise AppApiError(503, _SANDBOX_UNAVAILABLE)
    with build_coordination_or_503():
        try:
            report = await run_reclamation_pass(control_plane=sandbox)
        except SandboxError as exc:
            # A half-enumerated fleet must never be reported as a whole one: "nothing to collect"
            # read off a truncated list is the answer that gets a ghost forgotten, and it is
            # indistinguishable from success.
            raise AppApiError(503, _SANDBOX_UNAVAILABLE) from exc
        await append_audit(
            db,
            actor_id=admin.id,
            action="sandbox:reclamation_report",
            resource_type="sandbox",
            resource_id=None,
            detail={
                "scanned": report.scanned,
                "spared": report.spared,
                "staged": report.staged,
                "destroy": report.destroy,
                "escalate": report.escalate,
                "notOurs": report.not_ours,
                "storeFault": report.store_fault,
            },
        )
        await db.commit()
        last_pass, stale = await reclamation_pass_freshness(db)
        flags = settings.sandbox
        return ReclamationReportResponse(
            scanned=report.scanned,
            spared=report.spared,
            staged=report.staged,
            destroy=report.destroy,
            escalate=report.escalate,
            not_ours=report.not_ours,
            store_fault=report.store_fault,
            candidates=[
                ReclamationCandidate(
                    name=c.name, tier=str(c.tier), verdict=str(c.verdict), reason=c.reason
                )
                for c in report.candidates
            ],
            reclaim_enabled=flags is not None and flags.reclaim_enabled,
            reclaim_destroy=flags is not None and flags.reclaim_destroy,
            last_reclamation_pass_at=last_pass,
            reclamation_stale=stale,
        )
    # Reached only when `build_coordination_or_503` skipped the body on an unconfigured Redis.
    # The spare-list IS the classifier's second source — without it every claimed container reads
    # as unclaimed, which is a preview of a fleet-wide deletion rather than a report.
    raise coordination_is_gone()


@router.post(
    "/backfill-sandbox-tags",
    responses=error_responses(
        (503, ErrorEnvelope, "The sandbox control plane is temporarily unavailable"),
        *_ADMIN_AUTH,
    ),
)
async def backfill_sandbox_tags_endpoint(
    admin: CurrentSuperadmin, db: DbSession, sandbox: OptionalSandbox
) -> SandboxTagBackfillResponse:
    """Stamp C10 identity tags onto every sandbox container that predates identity stamping (U8).

    WHY THIS IS A RELEASE PREREQUISITE AND NOT A FOLLOW-UP. Everything provisioned from U8 onward
    carries owner, app, control plane and a self-stamped creation time on the ARM resource, so it
    can be judged with Redis down. Every container created BEFORE that carries nothing — and those
    are exactly the ghosts this whole plan exists to collect. Until this has run and the fleet
    reports zero untagged sandboxes, `SANDBOX_RECLAIM_DESTROY` must stay off (C10 §3.5).

    THIS DESTROYS NOTHING. It only writes tags, via ARM merge-`PATCH`, which creates no revision
    and cannot touch container env — a live sandbox being stamped keeps its replica, its restart
    count and its supervisor bearer.

    OWNERSHIP IS RECOVERED, NEVER GUESSED. `app_name_for` keeps 28 of an app_id's 32 hex
    characters, so a sandbox name is NOT invertible; names are matched FORWARD against the app
    table. A container matching no row is stamped `kind` + `backfilled_at` and nothing else — no
    owner, no app — which leaves it escalate-forever: reported on every pass, destroyed by none of
    them. Inventing a plausible owner for it is the one move the escalate-never-destroy
    architecture exists to prevent.

    A sibling of the three reconcilers above in every operational respect: superadmin-gated,
    operator-invoked, idempotent (an already-tagged container is skipped, so the age clock is never
    reset by a second press), and AUDITED WITH COUNTS ONLY. A sandbox name embeds 28 hex characters
    of its app's uuid, so a name list is an inventory of who is running what; the names of
    containers that could not be stamped go to the logs, never to the audit row.

    Unlike `reconcile-sandboxes` this needs NO Redis: identity comes from ARM and the app table,
    and that independence is the property being installed."""
    if sandbox is None:
        raise AppApiError(503, _SANDBOX_UNAVAILABLE)
    if not isinstance(sandbox, FleetTagger):
        # A deployment whose sandbox client cannot enumerate or stamp (a fake, a future substrate
        # that has not implemented it). Retryable-shaped rather than a 500: nothing is wrong with
        # the request, this deployment simply cannot answer it.
        raise AppApiError(503, _SANDBOX_UNAVAILABLE)
    try:
        report = await backfill_sandbox_tags(db, sandbox)
    except SandboxError as exc:
        # Never a partial pass reported as a whole one: "nothing left to stamp" is the false green
        # the destroy flag is gated on, so a half-enumerated fleet must read as retryable.
        raise AppApiError(503, _SANDBOX_UNAVAILABLE) from exc
    await append_audit(
        db,
        actor_id=admin.id,
        action="sandbox:backfill-tags",
        # Fleet-wide, so it belongs to no single sandbox — the `storage:reconcile` shape (a
        # resource TYPE with no id).
        resource_type="sandbox",
        resource_id=None,
        detail={
            "scanned": report.scanned,
            "alreadyTagged": report.already_tagged,
            "stamped": report.stamped,
            "skippedNoRow": report.skipped_no_row,
            "failed": report.failed,
            "unowned": report.unowned,
        },
    )
    await db.commit()
    return SandboxTagBackfillResponse(
        scanned=report.scanned,
        already_tagged=report.already_tagged,
        stamped=report.stamped,
        skipped_no_row=report.skipped_no_row,
        failed=report.failed,
        unowned=report.unowned,
    )


@router.post(
    "/reconcile-databases",
    responses=error_responses(
        # ONE 503 entry: `error_responses` raises on a duplicate status and `_ADMIN_AUTH`
        # already holds 401 + 403.
        (503, ErrorEnvelope, "The app-database cluster is temporarily unavailable"),
        *_ADMIN_AUTH,
    ),
)
async def reconcile_databases(
    admin: CurrentSuperadmin, db: DbSession
) -> DatabaseReconcileResponse:
    """Diff the app-database cluster against the `project_databases` registry and REPORT the
    orphans — databases a failed teardown stranded, and roles whose database is already gone.

    THIS DELETES NOTHING, and that is the design, not a first iteration. The blob sweep can
    reclaim because a blob has a provable `last_modified` and a 24h grace protects an
    in-flight write; `pg_database` has no creation timestamp at all, so the provision-time
    COMMENT is the only age evidence that survives orphaning — mutable, absent on anything
    we did not create, and not something to hang an irreversible `DROP DATABASE` on. Three
    guards decide what is even reportable (denylist, full-uuid name anchor, fail-closed
    parse), and the sweep still stops at telling an operator the number.

    A sibling of `reconcile-storage` in every operational respect: superadmin-gated,
    OPERATOR-INVOKED (this reconciler is not on a schedule; nothing but a superadmin calls it —
    corrected 2026-08-11 from "there is no scheduler in this repo, by decision", which was false
    when written and is doubly false now, see ADR-0029), headless-friendly
    because the admin router declares no CSRF, idempotent, and audited with counts only —
    a database name embeds its project's uuid, so a name list is an inventory of who has
    what, exactly the leak the storage report's key list is pinned against.

    The maintenance engine is resolved HERE, in the body, never as an eager `Depends`: a
    route that documents a 503 must be able to answer with it, and a dependency that raises
    at solve time turns the documented 503 into an undocumented 500 (commit 6be7a9c). An
    unconfigured substrate and an unreachable cluster are the same answer to the caller —
    retryable — because a sweep that reported "no orphans" for either would be actively
    dangerous.
    """
    engine = get_maintenance_engine()
    if engine is None:
        raise AppApiError(503, _DB_CLUSTER_UNREACHABLE)
    try:
        report = await reconcile_orphaned_app_databases(db, engine)
    except (SQLAlchemyError, OSError) as exc:
        # Never a partial report: a half-enumerated cluster is indistinguishable from a
        # clean one, and "clean" is the answer that gets an orphan forgotten.
        raise AppApiError(503, _DB_CLUSTER_UNREACHABLE) from exc
    await append_audit(
        db,
        actor_id=admin.id,
        action="db:reconcile",
        # Cluster-wide, so it belongs to no single app or project — the `storage:reconcile`
        # shape (a resource TYPE with no id), with `database` as the subject.
        resource_type="database",
        # Counts only (security.md): never a database name, never a role name, never a host.
        detail={
            "orphanedDatabases": report.databases.orphaned,
            "unknownAgeDatabases": report.databases.unknown_age,
            "strandedRoles": report.roles.stranded,
            "scannedDatabases": report.databases.scanned,
            "scannedRoles": report.roles.scanned,
        },
    )
    await db.commit()
    return _database_reconcile_response(report)


@router.post(
    "/reconcile-deploys",
    responses=error_responses(
        (503, ErrorEnvelope, "Publishing is not available on this deployment"),
        *_ADMIN_AUTH,
    ),
)
async def reconcile_deploys(admin: CurrentSuperadmin, db: DbSession) -> DeployReconcileResponse:
    """Settle deployment rows whose pipeline died with the process — on demand (U6).

    THE GAP THIS CLOSES. Of the four reconciling sweeps this platform runs, three already had a
    superadmin lever (`reconcile-storage`, `reconcile-sandboxes`, `reconcile-databases`) and this
    one had none: deploy reconciliation was reachable only from the boot path and a 300-second
    in-process timer. So an operator staring at an app whose Deploy button 409s had exactly two
    options — wait out `store.DEPLOY_STALE_AFTER_S` (thirty minutes) or restart the control
    plane. Now there is a third.

    A SIBLING of the other three in every operational respect: superadmin-gated, operator-invoked,
    headless-friendly (the admin router declares no CSRF, so `curl -b "session=<jwt>"` works),
    idempotent, and audited with COUNTS ONLY — a deployment id or an app name would make the trail
    an inventory of who deployed what (`.claude/rules/security.md`).

    Deliberately NOT gated on `DEPLOY__RECONCILE_ENABLED`. That flag switches off the CLOCK (the
    scheduled pass in `src/workers/deploy_reconcile.py`); an operator who has silenced the timer
    must still be able to settle a wedged deploy by hand, and a lever that the kill switch also
    kills is not a recovery lever.

    SAFE TO PRESS AT ANY TIME, including while both the scheduled pass and the in-process loop
    are running. Staleness is measured from `heartbeat_at`, so a live pipeline is never in the
    work list at all, and every terminal write is guarded on `status = 'running'` — of two racing
    reconcilers exactly one settles a given row and the other learns it lost.

    The publish client is resolved HERE, in the body, never as an eager `Depends`: a dependency
    that raises at solve time turns this route's documented 503 into an undocumented 500 (commit
    6be7a9c). An unconfigured `DEPLOY__*` block therefore 503s, which is the honest answer — there
    is nothing to reconcile *with*.

    A 200 FROM THIS ROUTE DOES NOT MEAN THE FLEET IS SETTLED, and the difference is worth knowing
    before you act on the number. `reconcile_stalled_deployments` **never raises** — by design, so
    that a failure cannot take the lifespan or the scheduled pass down with it — so an unreachable
    ARM is caught PER ROW: `AcaTransientError` defers that row to the next pass, anything else is
    logged. Either way the pass completes and returns a `resolved` count that is simply lower.
    So the two failures are NOT the same answer: unconfigured is a 503, unreachable ARM is a 200
    with rows deferred, visible only as `deployment_reconcile_deferred` / `_failed` in the logs.
    An earlier version of this docstring claimed they were identical; they are not, and a 200 read
    as "everything settled" is exactly the misleading answer it was trying to avoid.

    It opens its OWN sessions through `async_session_factory` rather than borrowing the request's:
    each row is settled and committed independently, so a slow ARM call on row three cannot hold a
    transaction open across the whole pass, and a failure there does not roll back rows one and
    two."""
    try:
        published_apps = get_published_apps()
    except DeployNotConfiguredError as exc:
        raise AppApiError(503, _PUBLISHING_UNAVAILABLE) from exc

    resolved = await reconcile_stalled_deployments(async_session_factory, published_apps)
    await append_audit(
        db,
        actor_id=admin.id,
        action="deploy:reconcile",
        # Fleet-wide, so it belongs to no single app — the `storage:reconcile` shape (a resource
        # TYPE with no id), with `deployment` as the subject.
        resource_type="deployment",
        resource_id=None,
        # Counts only (security.md): never a deployment id, never an app name, never a URL.
        detail={"resolved": resolved},
    )
    await db.commit()
    return DeployReconcileResponse(resolved=resolved)


@router.get(
    "/{app_id}/audit",
    # No 404: read_audit queries the audit log directly (no app existence pre-check),
    # so an unknown app id returns an empty event list (documented as-is).
    responses=error_responses(*_ADMIN_AUTH),
)
async def read_audit(
    app_id: uuid.UUID, admin: CurrentSuperadmin, db: DbSession
) -> AuditListResponse:
    app_str = str(app_id)
    rows = (
        await db.execute(
            sa.select(AuditLog, User.email)
            .outerjoin(User, AuditLog.actor_id == User.id)
            .where(
                sa.or_(
                    AuditLog.resource_id == app_str,
                    AuditLog.detail["appId"].astext == app_str,
                )
            )
            .order_by(AuditLog.created_at.desc())
            .limit(200)
        )
    ).all()
    return AuditListResponse(
        events=[
            AuditEventOut(
                id=row.id,
                actor_id=row.actor_id,
                username=email,
                action=row.action,
                resource_type=row.resource_type,
                resource_id=row.resource_id,
                detail=row.detail,
                count=row.detail.get("count") if isinstance(row.detail, dict) else None,
                created_at=row.created_at,
            )
            for row, email in rows
        ]
    )


# ==============================================================================
# U9 — per-user limits management + feedback read (R28). A sibling `/admin` router
# (the governance router is prefixed `/admin/apps`).
# ==============================================================================

users_router = APIRouter(prefix="/admin", tags=["admin"])

# Context-guardrail constants + resolver now live in `src.services.usage.limits` (the single
# source of truth shared with `/auth/me`, so a per-user override reaches the client).
_FEEDBACK_CAP = 200


def _raw_limits(override: UserLimit | None) -> LimitFields:
    if override is None:
        return LimitFields()
    return LimitFields(
        daily_token_limit=override.daily_token_limit,
        context_soft_limit=override.context_soft_limit,
        context_hard_limit=override.context_hard_limit,
    )


def _effective_limits(override: UserLimit | None) -> LimitFields:
    # Resolved purely from the already-loaded override (no per-user DB re-query): daily via the
    # shared gate resolver so admin and the gate agree; context via the shared resolver so admin
    # and `/auth/me` agree.
    daily = resolve_daily_limit(override.daily_token_limit if override else None)
    soft, hard = effective_context(override)
    return LimitFields(daily_token_limit=daily, context_soft_limit=soft, context_hard_limit=hard)


@users_router.get(
    "/users",
    responses=error_responses(
        (422, ErrorEnvelope, "Invalid pagination cursor or over-long q"), *_ADMIN_AUTH
    ),
)
async def list_users(
    admin: CurrentSuperadmin,
    db: DbSession,
    cursor: CursorQuery = None,
    limit: LimitQuery = DEFAULT_PAGE_SIZE,
    q: SearchQuery = None,
) -> UsersResponse:
    """Keyset page of the roster, newest-first, optionally filtered by a case-insensitive
    email/display-name substring (KD-1 — replaces the unbounded full-table load). Each row
    carries raw + effective limits, the suspension marker, and today's folded token spend.
    Overrides and usage are fetched for the PAGE in one query each — the usage read is a
    single `GROUP BY user_id` aggregate keyed to the IST day, never a per-row N+1 (R9)."""
    after = parse_cursor(cursor)
    search = clean_search(q)
    limit = clean_limit(limit)
    query = sa.select(User)
    if search is not None:
        query = query.where(
            sa.or_(
                User.email.icontains(search, autoescape=True),
                User.display_name.icontains(search, autoescape=True),
            )
        )
    if after is not None:
        query = query.where(User.id < after)
    users = (await db.execute(query.order_by(User.id.desc()).limit(limit + 1))).scalars().all()
    page, next_cursor, has_more = split_keyset(users, limit, key=lambda u: u.id)
    page_ids = [user.id for user in page]

    overrides: dict[uuid.UUID, UserLimit] = {}
    used_today: dict[uuid.UUID, int] = {}
    review_today: dict[uuid.UUID, int] = {}
    if page_ids:
        override_rows = (
            await db.execute(sa.select(UserLimit).where(UserLimit.user_id.in_(page_ids)))
        ).scalars()
        overrides = {row.user_id: row for row in override_rows}
        # `billable_spend` (the cost-weighted spend: fresh + output + cache_read/10 +
        # cache_write*1.25) is the SHARED expression the daily gate's `_used_today` also
        # uses, so the roster agrees with the gate on "used today" by construction
        # (services/usage/gate.py). Grouped by kind (U15): `usageToday` counts `build`
        # rows only — EXACTLY what the gate reads, so the admin comparing it against the
        # cap sees the number the cap actually measures — and review spend is reported
        # as its OWN figure beside it, never folded in. One number that meant two things
        # is how this table went wrong before; still one aggregate query, never an N+1.
        usage_rows = await db.execute(
            sa.select(
                TokenUsage.user_id,
                TokenUsage.kind,
                sa.func.sum(billable_spend()).label("used"),
            )
            .where(TokenUsage.usage_date == ist_today(), TokenUsage.user_id.in_(page_ids))
            .group_by(TokenUsage.user_id, TokenUsage.kind)
        )
        for row in usage_rows:
            bucket = used_today if row.kind is TokenUsageKind.BUILD else review_today
            bucket[row.user_id] = int(row.used)

    out = [
        UserLimitsOut(
            user_id=user.id,
            email=user.email,
            display_name=user.display_name,
            role=role_for(user, settings.superadmin_emails),
            suspended_at=user.suspended_at,
            usage_today=used_today.get(user.id, 0),
            review_usage_today=review_today.get(user.id, 0),
            limits=_raw_limits(overrides.get(user.id)),
            effective_limits=_effective_limits(overrides.get(user.id)),
        )
        for user in page
    ]
    defaults = LimitFields(
        daily_token_limit=settings.DAILY_TOKEN_LIMIT,
        context_soft_limit=DEFAULT_CONTEXT_SOFT,
        context_hard_limit=DEFAULT_CONTEXT_HARD,
    )
    return UsersResponse(defaults=defaults, users=out, next_cursor=next_cursor, has_more=has_more)


@users_router.patch(
    "/users/{user_id}/limits",
    responses=error_responses(
        (400, ErrorEnvelope, "No fields provided or invalid limit value"),
        (404, ErrorEnvelope, "No such user"),
        *_ADMIN_AUTH,
    ),
)
async def set_user_limits(
    user_id: uuid.UUID, body: LimitFields, admin: CurrentSuperadmin, db: DbSession
) -> LimitsPatchResponse:
    provided = body.model_fields_set  # python field names, incl. those set to null
    if not provided:
        raise AppApiError(
            400, "Provide at least one of: dailyTokenLimit, contextSoftLimit, contextHardLimit."
        )
    user = await db.get(User, user_id)
    if user is None:
        raise AppApiError(404, "No such user.")

    changes: dict[str, int | None] = {}
    for field in provided:
        value = getattr(body, field)
        if value is not None and value <= 0:
            raise AppApiError(
                400, f"{to_camel(field)} must be a positive integer, or null to reset to default."
            )
        changes[field] = value
    if (hard := changes.get("context_hard_limit")) is not None and hard > MODEL_CONTEXT_WINDOW:
        raise AppApiError(400, "contextHardLimit cannot exceed the model context window.")
    soft, hard = changes.get("context_soft_limit"), changes.get("context_hard_limit")
    if soft is not None and hard is not None and soft >= hard:
        raise AppApiError(400, "contextSoftLimit must be less than contextHardLimit.")

    # Upsert the sparse override row: set only the provided fields (None clears one).
    upsert = (
        pg_insert(UserLimit)
        .values(user_id=user_id, **changes)
        .on_conflict_do_update(
            constraint="uq_user_limits_user", set_={**changes, "updated_at": sa.func.now()}
        )
    )
    await db.execute(upsert)
    await append_audit(
        db,
        actor_id=admin.id,
        action="limits:set",
        resource_type="user",
        resource_id=str(user_id),
        detail={"fields": sorted(to_camel(f) for f in provided)},
    )
    await db.commit()

    override = (
        await db.execute(sa.select(UserLimit).where(UserLimit.user_id == user_id))
    ).scalar_one_or_none()
    return LimitsPatchResponse(
        user_id=user_id,
        limits=_raw_limits(override),
        effective_limits=_effective_limits(override),
    )


@users_router.post(
    "/users/limits/bulk",
    responses=error_responses(
        (400, ErrorEnvelope, "Invalid limit value, an empty, or an unknown user_ids entry"),
        *_ADMIN_AUTH,
    ),
)
async def bulk_set_user_limits(
    body: BulkLimitsRequest, admin: CurrentSuperadmin, db: DbSession
) -> BulkLimitsResponse:
    """Admin "Global Limits" — set the SAME daily token limit for many users in one
    request, either every user system-wide (`userIds` omitted/null, `confirmAll`
    required) or a hand-picked subset (`userIds` provided). Unlike `set_user_limits`,
    this never resets-to-default (bulk always sets an exact value) and never touches
    `context_soft_limit`/`context_hard_limit` — those stay per-user, per-conversation
    knobs.

    This is a SNAPSHOT, not a standing policy: it writes one row per user targeted
    right now. A user who joins after an "all" apply has no override row and runs at
    `settings.DAILY_TOKEN_LIMIT` until a re-apply — there is no persisted fleet-level
    setting `resolve_daily_limit` consults ahead of that default.

    The "all" scope resolves the roster IN THE INSERT itself (`INSERT ... SELECT ...
    FROM users`) rather than materializing every id into Python first — past ~10,922
    users that overflowed asyncpg's bind-parameter ceiling (3 params/row: the
    client-side `id` default, `user_id`, `daily_token_limit`), and it drops the
    all-scope apply to a single bind parameter. The "selected" scope stays a
    multi-VALUES upsert (the id list is already bounded + validated), sorted for a
    deterministic lock order — `ON CONFLICT DO UPDATE`'s row-lock order follows
    `VALUES` order, and an unordered list let two concurrent applies deadlock.

    ACCEPTED RISK: the "all" scope has no before-image, only a count. Unlike
    "selected" (bounded by the request, cheap to snapshot), "all" is a JSONB-sized
    per-user structure — a different order of object — and at BIAL's realistic scale
    (hundreds of internal users) building that snapshot isn't worth the complexity
    yet. This means a mis-applied "all" scope apply is NOT scriptably reversible:
    only "selected" is. Revisit if the fleet grows large enough that this stops
    being an acceptable trade-off."""
    if body.daily_token_limit <= 0 or body.daily_token_limit > MAX_DAILY_TOKEN_LIMIT:
        raise AppApiError(
            400,
            f"dailyTokenLimit must be a positive integer no greater than {MAX_DAILY_TOKEN_LIMIT}.",
        )
    if body.user_ids is None and not body.confirm_all:
        raise AppApiError(400, "Set confirmAll=true to apply to every user, system-wide.")

    before: list[dict[str, Any]] | None = None

    if body.user_ids is None:
        scope = "all"
        # `suspended_at IS NULL` — a suspended user is excluded, so `updated_count`
        # reflects who is operationally affected, not every row in the table.
        #
        # `id` is generated EXPLICITLY via `uuidv7()` inside the SELECT (invoked once
        # per row by Postgres) rather than left to the column's own default. Leaving it
        # out of `from_select`'s column list doesn't defer to the server default the
        # way a plain multi-row `.values()` insert does: SQLAlchemy still detects the
        # column's Python-side `default=uuid.uuid7` and auto-adds `id` to the INSERT's
        # column list anyway, but a `from_select` has no per-row Python execution to
        # invoke that callable — so it evaluates the default ONCE and binds that SAME
        # id to every row, which collides on the very first conflict-free insert past
        # one row ("duplicate key value violates ... user_limits_pkey"). Confirmed by
        # printing the compiled statement, not assumed.
        upsert = (
            pg_insert(UserLimit)
            .from_select(
                ["id", "user_id", "daily_token_limit"],
                sa.select(sa.func.uuidv7(), User.id, sa.literal(body.daily_token_limit))
                .where(User.suspended_at.is_(None))
                .order_by(User.id),
            )
            .on_conflict_do_update(
                constraint="uq_user_limits_user",
                set_={"daily_token_limit": body.daily_token_limit, "updated_at": sa.func.now()},
            )
        )
        # `CursorResult.rowcount` reports total rows the INSERT processed — both newly
        # inserted and conflict-updated (Postgres's own "INSERT 0 N" command tag counts
        # both) — so this is rows ACTUALLY written, not the size of a pre-fetched
        # roster the all-scope branch no longer materializes into Python.
        result = cast("sa.CursorResult[Any]", await db.execute(upsert))
        updated_count = result.rowcount
    else:
        # Dedup: a repeated id would otherwise make the upsert try to affect the same
        # row twice in one statement, which Postgres refuses ("ON CONFLICT DO UPDATE
        # command cannot affect row a second time").
        target_ids = list(dict.fromkeys(body.user_ids))
        if not target_ids:
            raise AppApiError(400, "No users to update.")
        scope = "selected"

        # Fail closed on an unknown id rather than letting it hit the FK constraint
        # below as an unhandled IntegrityError — a 400 the caller can act on, not
        # a 500. A stale id (the roster changed after the admin loaded the page) is
        # exactly the case this is meant to catch.
        known = (
            (await db.execute(sa.select(User.id).where(User.id.in_(target_ids)))).scalars().all()
        )
        if len(known) != len(target_ids):
            raise AppApiError(400, "One or more user ids are unknown.")

        # Before-image, in the SAME transaction as the write below: the prior
        # daily_token_limit per target, `None` meaning "no row — inherited the
        # default". `user_limits` is sparse, so this apply overwrites both the prior
        # explicit values AND that inherit-the-default state at once; without this, a
        # mis-applied "selected" change is unrecoverable, since neither this request's
        # own body nor the existing `limits:set` audit trail (field names only) can
        # reconstruct it. `scope="all"` stays count-only — that roster is
        # reconstructible from the users table itself.
        # FOR UPDATE: under READ COMMITTED a plain SELECT takes no locks, so a
        # concurrent apply (or the single-user limits modal) could commit between this
        # read and the write below — recording a `before` value that was never
        # actually overwritten, which would corrupt a rollback built from it.
        prior_rows = (
            await db.execute(
                sa.select(UserLimit.user_id, UserLimit.daily_token_limit)
                .where(UserLimit.user_id.in_(target_ids))
                .with_for_update()
            )
        ).all()
        prior_by_id = {row.user_id: row.daily_token_limit for row in prior_rows}
        target_ids = sorted(target_ids)  # deterministic lock order — see docstring
        before = [
            {"userId": str(uid), "dailyTokenLimit": prior_by_id.get(uid)} for uid in target_ids
        ]

        upsert = (
            pg_insert(UserLimit)
            .values(
                [
                    {"user_id": user_id, "daily_token_limit": body.daily_token_limit}
                    for user_id in target_ids
                ]
            )
            .on_conflict_do_update(
                constraint="uq_user_limits_user",
                set_={"daily_token_limit": body.daily_token_limit, "updated_at": sa.func.now()},
            )
        )
        await db.execute(upsert)
        updated_count = len(target_ids)

    detail: dict[str, Any] = {
        "dailyTokenLimit": body.daily_token_limit,
        "userCount": updated_count,
        "scope": scope,
    }
    if before is not None:
        detail["before"] = before
    if scope == "all":
        # Records the deliberate choice to keep `suspended_at IS NULL` above — the
        # "selected" scope has no such filter, so the two scopes cover different
        # populations. Without this, there's no way to answer "who was excluded" later.
        detail["excludesSuspended"] = True
    await append_audit(
        db,
        actor_id=admin.id,
        action="limits:bulk_set",
        resource_type="user",
        resource_id=None,
        # Deliberately omits a top-level raw user_ids list. This is NOT a claim that
        # the detail avoids per-user data generally — the "selected"-scope `before`
        # above already carries one row per target, since it's a recovery aid derived
        # from this request's own validated id list, not a fresh roster fetch.
        detail=detail,
    )
    await db.commit()
    return BulkLimitsResponse(updated_count=updated_count)


# --- local suspension (U10, R10–R14) ---------------------------------------------


async def _get_user_or_404(db: DbSession, user_id: uuid.UUID) -> User:
    user = await db.get(User, user_id)
    if user is None:
        raise AppApiError(404, "No such user.")
    return user


@users_router.post(
    "/users/{user_id}/deactivate",
    responses=error_responses(
        AUTH_401,
        # The RBAC gate's own 403 is the DetailBody shape; this route's 403 (below)
        # is the envelope. OpenAPI allows one schema per status — the envelope is
        # documented since it is this route's own raise.
        (403, ErrorEnvelope, "Target is a super-admin (never suspendable, AE6)"),
        (404, ErrorEnvelope, "No such user"),
        (409, ErrorEnvelope, "User is already suspended"),
    ),
)
async def deactivate_user(
    user_id: uuid.UUID, admin: CurrentSuperadmin, db: DbSession
) -> SuspensionResponse:
    """Immediately block a user (R10/R11, KD-6): stamp `suspended_at`, bump
    `token_version`, and revoke every refresh family — so live sessions, captured
    refresh cookies, and outstanding runner tokens all die NOW, and the login
    callback refuses a fresh Entra sign-in until reactivation."""
    user = await _get_user_or_404(db, user_id)
    # AE6: no super-admin is ever suspendable — and because the CALLER is gated as
    # one, this single allowlist check also covers self-suspension.
    if is_super_duper_admin(user, settings.superadmin_emails):
        raise AppApiError(403, "A super-admin cannot be suspended.")
    if user.suspended_at is not None:
        raise AppApiError(409, "User is already suspended.")

    user.suspended_at = datetime.now(UTC)
    await revoke_all_sessions(db, user.id)
    await append_audit(
        db,
        actor_id=admin.id,
        action="user:deactivate",
        resource_type="user",
        resource_id=str(user_id),
    )
    await db.commit()
    return SuspensionResponse(user_id=user.id, suspended_at=user.suspended_at)


@users_router.post(
    "/users/{user_id}/reactivate",
    responses=error_responses(
        (404, ErrorEnvelope, "No such user"),
        (409, ErrorEnvelope, "User is not suspended"),
        *_ADMIN_AUTH,
    ),
)
async def reactivate_user(
    user_id: uuid.UUID, admin: CurrentSuperadmin, db: DbSession
) -> SuspensionResponse:
    """Restore a suspended user (R12). Clears the marker so login works again —
    deliberately WITHOUT touching `token_version`, so every pre-suspension session
    and token stays dead; the user signs in fresh."""
    user = await _get_user_or_404(db, user_id)
    if user.suspended_at is None:
        raise AppApiError(409, "User is not suspended.")

    user.suspended_at = None
    await append_audit(
        db,
        actor_id=admin.id,
        action="user:reactivate",
        resource_type="user",
        resource_id=str(user_id),
    )
    await db.commit()
    return SuspensionResponse(user_id=user.id, suspended_at=None)


@users_router.post(
    "/users/{user_id}/reset-usage",
    responses=error_responses((404, ErrorEnvelope, "No such user"), *_ADMIN_AUTH),
)
async def reset_user_usage(
    user_id: uuid.UUID, admin: CurrentSuperadmin, db: DbSession
) -> UsageResetResponse:
    """Zero out a user's TODAY-only token usage. Deletes the `token_usage` BUILD row for
    `ist_today()` if one exists — `_used_today` already reads 0 for an absent row
    (services/usage/gate.py), so this is equivalent to zeroing every column and
    simpler than an UPDATE, and leaves no stale row whose timestamps would misleadingly
    predate the reset. `record_usage`'s `INSERT … ON CONFLICT` recreates the row
    cleanly on the user's next turn either way.

    Build row ONLY (U15): the reset exists to let the citizen build again today, and the
    gate reads build spend only — a same-day `review` row changes nothing the cap
    measures, and deleting it would erase the attribution record that is the whole point
    of metering review cost. Scoping by kind also keeps `.first()` below well-defined:
    the `(user_id, usage_date, kind)` uniqueness allows at most ONE build row per day.

    Idempotent (no 409): resetting an already-zero/absent day is a harmless no-op —
    unlike deactivate/reactivate there is no suspended/not-suspended STATE to conflict
    with. Prior days' rows are never touched (only `usage_date == ist_today()` is
    targeted) — this is a "let them start today over" action, not a usage-history
    edit. Unlike deactivate, no super-admin guard: resetting usage isn't unsafe the
    way suspending a super-admin's own access would be.

    `.returning()` captures the row's four token columns in the SAME delete — not a
    separate SELECT first — so the audit trail records what was actually discarded
    (spend reconciliation) without a second round trip or a check-then-act race. A
    `None` result (already-zero/absent day, the idempotent no-op case) is audited
    with no `detail` rather than a made-up all-zero one, so the trail can tell "reset
    real spend" apart from "reset nothing"."""
    user = await _get_user_or_404(db, user_id)
    deleted = (
        await db.execute(
            sa.delete(TokenUsage)
            .where(
                TokenUsage.user_id == user_id,
                TokenUsage.usage_date == ist_today(),
                TokenUsage.kind == TokenUsageKind.BUILD,
            )
            .returning(
                TokenUsage.input_tokens,
                TokenUsage.output_tokens,
                TokenUsage.cache_read_tokens,
                TokenUsage.cache_write_tokens,
            )
        )
    ).first()
    detail = (
        {
            "inputTokens": deleted.input_tokens,
            "outputTokens": deleted.output_tokens,
            "cacheReadTokens": deleted.cache_read_tokens,
            "cacheWriteTokens": deleted.cache_write_tokens,
        }
        if deleted is not None
        else None
    )
    await append_audit(
        db,
        actor_id=admin.id,
        action="usage:reset",
        resource_type="user",
        resource_id=str(user_id),
        detail=detail,
    )
    await db.commit()
    return UsageResetResponse(user_id=user.id, usage_today=0)


@users_router.get("/feedback", responses=error_responses(*_ADMIN_AUTH))
async def read_feedback(admin: CurrentSuperadmin, db: DbSession) -> FeedbackResponse:
    rows = (
        await db.execute(
            sa.select(Feedback, User.email)
            .join(User, Feedback.user_id == User.id)
            .order_by(Feedback.created_at.desc())
            .limit(_FEEDBACK_CAP)
        )
    ).all()
    total = (await db.execute(sa.select(sa.func.count()).select_from(Feedback))).scalar_one()
    return FeedbackResponse(
        feedback=[
            FeedbackItem(
                user_id=row.Feedback.user_id,
                email=row.email,
                message=row.Feedback.message,
                page=row.Feedback.page,
                created_at=row.Feedback.created_at,
            )
            for row in rows
        ],
        total=total,
    )
