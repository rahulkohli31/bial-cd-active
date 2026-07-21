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
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Query, status
from pydantic.alias_generators import to_camel
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.api.deps import ContainerStore, DbSession, Storage
from src.api.deps_rbac import CurrentSuperadmin
from src.api.v1.admin.schemas import (
    AdminAppOut,
    AdminAppStatusResponse,
    AppListResponse,
    ApproveRequest,
    AttachmentReclaimSummary,
    AuditEventOut,
    AuditListResponse,
    BundleUrlResponse,
    ClearDataRequest,
    ClearDataResponse,
    DataSummaryResponse,
    DeployCredentialResponse,
    FeedbackItem,
    FeedbackResponse,
    LimitFields,
    LimitsPatchResponse,
    MarkDeployedRequest,
    MarkDeployedResponse,
    PatchAppRequest,
    PrefixReconcileCounts,
    RejectRequest,
    StorageReconcileResponse,
    SuspensionResponse,
    UserLimitsOut,
    UsersResponse,
)
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
from src.db.models.app_registry import STATUS_TRANSITIONS, AppRegistry, AppStatus
from src.db.models.attachment import Attachment
from src.db.models.audit import AuditLog
from src.db.models.clear_data_token import (
    CLEAR_TOKEN_TTL_SECONDS,
    ClearDataToken,
    mint_confirm_token,
)
from src.db.models.feedback import Feedback
from src.db.models.project import Project
from src.db.models.token_usage import TokenUsage
from src.db.models.user import User
from src.db.models.user_limit import UserLimit
from src.schemas import AUTH_401, DetailBody, ErrorEnvelope, OkResponse, error_responses
from src.services.appserving.governance import nuke_app, the_purge
from src.services.attachments import AttachmentReclaimResult, reclaim_orphaned_attachments
from src.services.audit.log import append_audit
from src.services.auth.refresh import revoke_all_sessions
from src.services.rbac.roles import is_super_duper_admin, role_for
from src.services.storage import (
    ObjectStorage,
    StorageError,
    StorageSignError,
    StorageUnconfiguredError,
    get_storage,
    submission_key,
)
from src.services.storage.reconcile import (
    PrefixCounts,
    StorageReconcileReport,
    reconcile_orphaned_storage,
)
from src.services.usage.gate import ist_today, resolve_daily_limit
from src.services.usage.limits import (
    DEFAULT_CONTEXT_HARD,
    DEFAULT_CONTEXT_SOFT,
    MODEL_CONTEXT_WINDOW,
    effective_context,
)

router = APIRouter(prefix="/admin/apps", tags=["admin"])

# Every admin route is gated by `requires_superadmin`, which layers after
# `current_user`: an unauthenticated caller gets 401 and a non-super-admin 403, both
# bare `HTTPException` -> `{"detail"}` (documented as `DetailBody`). The routes' own
# raises are `AppApiError` -> `ErrorEnvelope`. This shared pair is spread into each
# route's `responses=` alongside that route's own explicit 4xx.
_ADMIN_AUTH = (
    AUTH_401,
    (403, DetailBody, "Super-admin privileges required"),
)


# --- helpers -------------------------------------------------------------------


def _project(
    app: AppRegistry, project_name: str, owner_username: str | None = None
) -> AdminAppOut:
    return AdminAppOut(
        app_id=app.id,
        name=project_name,
        owner_id=app.user_id,
        owner_username=owner_username,
        status=app.status,
        login_required=app.login_required,
        data_count=app.data_count,
        data_bytes=app.data_bytes,
        has_approved_snapshot=app.approved_submission_id is not None,
        submission_id=app.source_submission_id,
        commit_sha=app.source_commit_sha,
        submitted_at=app.submitted_at,
        approved_submission_id=app.approved_submission_id,
        approved_commit_sha=app.approved_commit_sha,
        approved_by=app.approved_by,
        approved_at=app.approved_at,
        deployed_at=app.deployed_at,
        deployed_url=app.deployed_url,
        # Exact and clock-skew-free (D7): ids, not timestamps. False for a
        # never-approved app (None == None); True for approved-but-undeployed.
        redeploy_needed=app.approved_submission_id != app.deployed_submission_id,
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
    return AppListResponse(
        apps=[_project(app, project_name, owner_email) for app, project_name, owner_email in rows]
    )


@router.post(
    "/{app_id}/approve",
    responses=error_responses(
        (404, ErrorEnvelope, "App not found"),
        (409, ErrorEnvelope, "Not pending, re-submitted since review, or artifact missing"),
        (503, ErrorEnvelope, "Storage temporarily unavailable"),
        *_ADMIN_AUTH,
    ),
)
async def approve(
    app_id: uuid.UUID,
    body: ApproveRequest,
    admin: CurrentSuperadmin,
    db: DbSession,
    storage: Storage,
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
    # Captured BEFORE any commit (never read ORM attributes across one). If a
    # re-submit lands after this read, the guarded UPDATE below refuses — and
    # submission ids are never reused, so on success this SHA belongs to the
    # reviewed submission.
    commit_sha = app.source_commit_sha

    # R11 — verify the reviewed artifact still exists before pinning it, so an app
    # can never reach APPROVED with a bundle that 404s at runbook time. Fail
    # closed: a storage ERROR is ambiguity, not absence (503, not 409).
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
    await append_audit(
        db,
        actor_id=admin.id,
        action="approve",
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
        *_ADMIN_AUTH,
    ),
)
async def disable(
    app_id: uuid.UUID, admin: CurrentSuperadmin, db: DbSession
) -> AdminAppStatusResponse:
    await _get_app_or_404(db, app_id)
    if not await _transition(db, app_id, AppStatus.DISABLED):
        raise AppApiError(409, "Only an approved app can be disabled.")
    await append_audit(
        db, actor_id=admin.id, action="disable", resource_type="app", resource_id=str(app_id)
    )
    await db.commit()
    return AdminAppStatusResponse(app_id=app_id, status=AppStatus.DISABLED)


@router.post(
    "/{app_id}/enable",
    responses=error_responses(
        (404, ErrorEnvelope, "App not found"),
        (409, ErrorEnvelope, "Only a disabled app can be re-enabled"),
        *_ADMIN_AUTH,
    ),
)
async def enable(
    app_id: uuid.UUID, admin: CurrentSuperadmin, db: DbSession
) -> AdminAppStatusResponse:
    app = await _get_app_or_404(db, app_id)
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
    app_id: uuid.UUID, admin: CurrentSuperadmin, db: DbSession, storage: Storage
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
    "/{app_id}/mark-deployed",
    responses=error_responses(
        (404, ErrorEnvelope, "App not found"),
        (409, ErrorEnvelope, "Only an approved app can be marked deployed"),
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
    await _get_app_or_404(db, app_id)
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


@router.get(
    "/{app_id}/data-summary",
    responses=error_responses((404, ErrorEnvelope, "App not found"), *_ADMIN_AUTH),
)
async def data_summary(
    app_id: uuid.UUID, admin: CurrentSuperadmin, db: DbSession
) -> DataSummaryResponse:
    app = await _get_app_or_404(db, app_id)
    # Prune expired tokens, then mint a fresh single-use one (Express pruneClearTokens).
    await db.execute(sa.delete(ClearDataToken).where(ClearDataToken.expires_at < sa.func.now()))
    token = mint_confirm_token()
    db.add(
        ClearDataToken(
            token=token,
            app_id=app_id,
            actor_id=admin.id,
            expires_at=datetime.now(UTC) + timedelta(seconds=CLEAR_TOKEN_TTL_SECONDS),
        )
    )
    await db.commit()
    return DataSummaryResponse(
        app_id=app_id,
        data_count=app.data_count,
        data_bytes=app.data_bytes,
        confirm_token=token,
    )


@router.post(
    "/{app_id}/clear-data",
    # No 404: clear-data does not pre-check the app exists — an unknown app id simply
    # finds no token to redeem and returns 400 (documented as-is, not normalized).
    responses=error_responses(
        (400, ErrorEnvelope, "Invalid or expired confirmation"), *_ADMIN_AUTH
    ),
)
async def clear_data(
    app_id: uuid.UUID,
    body: ClearDataRequest,
    admin: CurrentSuperadmin,
    db: DbSession,
) -> ClearDataResponse:
    # Atomic single-use redeem: app-bound, unexpired, unused → stamp used_at. A replay
    # / expired / foreign token updates zero rows (fails closed). The redeem shares the
    # purge's transaction, so a failed purge rolls back the redeem (retryable).
    redeemed = await db.execute(
        sa.update(ClearDataToken)
        .where(
            ClearDataToken.token == body.confirm_token,
            ClearDataToken.app_id == app_id,
            ClearDataToken.actor_id == admin.id,
            ClearDataToken.used_at.is_(None),
            ClearDataToken.expires_at > sa.func.now(),
        )
        .values(used_at=sa.func.now())
        .returning(ClearDataToken.id)
    )
    if redeemed.first() is None:
        raise AppApiError(400, "Invalid or expired confirmation. Please retry.")

    records_removed = await the_purge(db, app_id, drafts_only=body.created_in_draft_only)
    await append_audit(
        db,
        actor_id=admin.id,
        action="clear-data",
        resource_type="app",
        resource_id=str(app_id),
        detail={"count": records_removed},
    )
    await db.commit()
    return ClearDataResponse(app_id=app_id, removed=records_removed)


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
    app = await _get_app_or_404(db, app_id)
    # Audit BEFORE destruction — the accountability row (no FK to the app) survives.
    await append_audit(
        db,
        actor_id=admin.id,
        action="app:delete",
        resource_type="app",
        resource_id=str(app_id),
        detail={"count": app.data_count},
    )
    await nuke_app(db, storage, app_id, container_store)
    await db.commit()
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


def reconcile_storage_or_none() -> ObjectStorage | None:
    """Storage for the reconcile sweep, resolved None-tolerantly — the same idiom as
    `container_store_dependency`. It resolves EAGERLY like any dependency but never raises at
    solve time: an unconfigured store (`StorageUnconfiguredError`) comes back as `None` and the
    body maps it to the documented 503, instead of the undocumented dependency-solve 500 an eager
    `Storage` (`get_storage()`) annotation raised (same failure class as FIX 1's `RedisDep`). A
    test still swaps a fake via `dependency_overrides`."""
    try:
        return get_storage()
    except StorageUnconfiguredError:
        return None


# None-tolerant, unlike the shared `Storage` — the reconcile sweep maps an unset store to 503.
ReconcileStorageDep = Annotated[ObjectStorage | None, Depends(reconcile_storage_or_none)]


@router.post(
    "/reconcile-storage",
    responses=error_responses(
        (503, ErrorEnvelope, "Storage temporarily unavailable"), *_ADMIN_AUTH
    ),
)
async def reconcile_storage(
    admin: CurrentSuperadmin, db: DbSession, storage: ReconcileStorageDep
) -> StorageReconcileResponse:
    """Sweep the whole object store against the database and reclaim ownerless, past-grace blobs
    (R11/R12/R13) — the recovery lever for a cleanup that a `_log.warning` was the only trail of.

    OPERATOR-INVOKED (KD-7): there is NO scheduler in this repo, and an in-process one was
    deliberately rejected. A superadmin drives this endpoint by hand (headlessly too — the admin
    router declares no CSRF, so `curl -b "session=<jwt>"` works); a grace-period sweep nothing
    calls reclaims nothing.

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
    which arrives here as `storage is None` (the None-tolerant `reconcile_storage_or_none`
    dependency) rather than the solve-time 500 an eager `Storage` (`get_storage()`) annotation
    raised."""
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
            "ownerlessSubmissions": report.ownerless_submissions,
            "reclaimedAttachments": reclaim.reclaimed,
            "reclaimedBytes": reclaim.freed_bytes,
            "reclaimedKeys": reclaim.swept_keys,
        },
    )
    await db.commit()
    return _reconcile_response(report, reclaim)


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
    if page_ids:
        override_rows = (
            await db.execute(sa.select(UserLimit).where(UserLimit.user_id.in_(page_ids)))
        ).scalars()
        overrides = {row.user_id: row for row in override_rows}
        # Fold ALL FOUR token classes so the roster agrees with the daily gate
        # (`_used_today`, services/usage/gate.py) on what "used today" means.
        spend = (
            TokenUsage.input_tokens
            + TokenUsage.output_tokens
            + TokenUsage.cache_read_tokens
            + TokenUsage.cache_write_tokens
        )
        usage_rows = await db.execute(
            sa.select(TokenUsage.user_id, sa.func.sum(spend).label("used"))
            .where(TokenUsage.usage_date == ist_today(), TokenUsage.user_id.in_(page_ids))
            .group_by(TokenUsage.user_id)
        )
        used_today = {row.user_id: int(row.used) for row in usage_rows}

    out = [
        UserLimitsOut(
            user_id=user.id,
            email=user.email,
            display_name=user.display_name,
            role=role_for(user, settings.superadmin_emails),
            suspended_at=user.suspended_at,
            usage_today=used_today.get(user.id, 0),
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
