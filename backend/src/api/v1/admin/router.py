"""Super-admin app-registry governance (R27, R29, R9) — the lifecycle state machine,
danger ops, and the durable clear-data confirm token, all `requires_superadmin`-gated
and audited. Ported from Express `admin/apps-routes.js`, but gated by Plan A's env
allowlist (`requires_superadmin`), NOT Express's `role==='admin'` claim, and approval
stores the CLIENT-compiled artifact — the server never runs Babel (R19/AE4).

The state machine is enforced atomically (`UPDATE ... WHERE status = ANY(allowed)`);
an illegal transition updates zero rows → 409. `enable` carries an explicit
`status==disabled` guard because the `→approved` transition also permits `pending`
(without it, enable could promote an un-compiled pending app past the compile gate).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

import sqlalchemy as sa
from fastapi import APIRouter, Query, status
from pydantic.alias_generators import to_camel
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.api.deps import DbSession
from src.api.deps_rbac import CurrentSuperadmin
from src.api.v1.admin.schemas import (
    AdminAppOut,
    AppListResponse,
    AuditEventOut,
    AuditListResponse,
    ClearDataRequest,
    ClearDataResponse,
    DataSummaryResponse,
    FeedbackItem,
    FeedbackResponse,
    LimitFields,
    LimitsPatchResponse,
    OkResponse,
    PatchAppRequest,
    RecomputeResponse,
    RejectRequest,
    StatusResponse,
    UserLimitsOut,
    UsersResponse,
)
from src.api.v1.apps.files_router import Storage
from src.config import settings
from src.core.errors import AppApiError
from src.db.models.app_registry import STATUS_TRANSITIONS, AppRegistry, AppStatus
from src.db.models.audit import AuditLog
from src.db.models.clear_data_token import (
    CLEAR_TOKEN_TTL_SECONDS,
    ClearDataToken,
    mint_confirm_token,
)
from src.db.models.feedback import Feedback
from src.db.models.user import User
from src.db.models.user_limit import UserLimit
from src.schemas import DetailBody, ErrorEnvelope, error_responses
from src.services.appserving.governance import nuke_app, recompute_files, the_purge
from src.services.audit.log import append_audit
from src.services.rbac.roles import role_for
from src.services.usage.gate import resolve_daily_limit
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
    (401, DetailBody, "Not authenticated"),
    (403, DetailBody, "Super-admin privileges required"),
)


# --- helpers -------------------------------------------------------------------


def _project(app: AppRegistry, owner_username: str | None = None) -> AdminAppOut:
    snapshot = app.approved_snapshot or {}
    return AdminAppOut(
        app_id=app.id,
        name=app.name,
        owner_id=app.user_id,
        owner_username=owner_username,
        status=app.status,
        login_required=app.login_required,
        data_count=app.data_count,
        data_bytes=app.data_bytes,
        file_count=app.file_count,
        file_bytes=app.file_bytes,
        has_approved_snapshot=isinstance(snapshot.get("compiled"), str),
        approved_by=app.approved_by,
        approved_at=app.approved_at,
        rejection_note=app.rejection_note,
        created_at=app.created_at,
        updated_at=app.updated_at,
    )


async def _get_app_or_404(db: DbSession, app_id: uuid.UUID) -> AppRegistry:
    app = await db.get(AppRegistry, app_id)
    if app is None:
        raise AppApiError(404, "App not found.")
    return app


async def _transition(db: DbSession, app_id: uuid.UUID, target: AppStatus, **extra: Any) -> bool:
    """Atomic guarded status transition (Express `setStatus`): update only when the
    current status is a legal source for `target`; zero rows updated → illegal."""
    result = await db.execute(
        sa.update(AppRegistry)
        .where(
            AppRegistry.id == app_id,
            AppRegistry.status.in_(tuple(STATUS_TRANSITIONS[target])),
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
    rows = (
        await db.execute(
            sa.select(AppRegistry, User.email)
            .join(User, AppRegistry.user_id == User.id)
            .where(*where)
            .order_by(AppRegistry.created_at.desc())
            .limit(200)
        )
    ).all()
    return AppListResponse(apps=[_project(app, owner_email) for app, owner_email in rows])


@router.post(
    "/{app_id}/approve",
    responses=error_responses(
        (400, ErrorEnvelope, "No submitted code to approve"),
        (404, ErrorEnvelope, "App not found"),
        (409, ErrorEnvelope, "Only a pending app can be approved"),
        *_ADMIN_AUTH,
    ),
)
async def approve(app_id: uuid.UUID, admin: CurrentSuperadmin, db: DbSession) -> StatusResponse:
    app = await _get_app_or_404(db, app_id)
    if app.status is not AppStatus.PENDING:
        raise AppApiError(409, "Only a pending app can be approved.")
    snapshot = app.source_snapshot or {}
    compiled = snapshot.get("compiled")
    if not isinstance(compiled, str) or not compiled:
        raise AppApiError(400, "No submitted code to approve.")
    now = datetime.now(UTC)
    # Store the CLIENT-compiled artifact as the approved snapshot — NO server compile.
    approved_snapshot = {
        "compiled": compiled,
        "src": snapshot.get("src", ""),
        "entry": snapshot.get("entry", "PreviewApp"),
        "at": now.isoformat(),
        "by": str(admin.id),
    }
    moved = await _transition(
        db,
        app_id,
        AppStatus.APPROVED,
        approved_snapshot=approved_snapshot,
        approved_by=admin.id,
        approved_at=now,
    )
    if not moved:
        raise AppApiError(409, "Could not approve in the current state.")
    await append_audit(
        db, actor_id=admin.id, action="approve", resource_type="app", resource_id=str(app_id)
    )
    await db.commit()
    return StatusResponse(app_id=app_id, status=AppStatus.APPROVED)


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
) -> StatusResponse:
    app = await _get_app_or_404(db, app_id)
    if app.status is not AppStatus.PENDING:
        raise AppApiError(409, "Only a pending app can be rejected.")
    note = (body.note or "")[:1000]
    moved = await _transition(db, app_id, AppStatus.REJECTED, rejection_note=note)
    if not moved:
        raise AppApiError(409, "Could not reject in the current state.")
    await append_audit(
        db, actor_id=admin.id, action="reject", resource_type="app", resource_id=str(app_id)
    )
    await db.commit()
    return StatusResponse(app_id=app_id, status=AppStatus.REJECTED)


@router.patch(
    "/{app_id}",
    responses=error_responses((404, ErrorEnvelope, "App not found"), *_ADMIN_AUTH),
)
async def patch_app(
    app_id: uuid.UUID, body: PatchAppRequest, admin: CurrentSuperadmin, db: DbSession
) -> AdminAppOut:
    app = await _get_app_or_404(db, app_id)
    if body.name is not None:
        app.name = body.name[:120]
    login_flipped = False
    if body.login_required is not None:
        login_flipped = bool(app.login_required) != body.login_required
        app.login_required = body.login_required
    await db.flush()
    # Audit ONLY a loginRequired flip (a name-only change is not audited — Express parity).
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
    await db.refresh(app)
    owner_email = await db.scalar(sa.select(User.email).where(User.id == app.user_id))
    return _project(app, owner_email)


@router.post(
    "/{app_id}/disable",
    responses=error_responses(
        (404, ErrorEnvelope, "App not found"),
        (409, ErrorEnvelope, "Only an approved app can be disabled"),
        *_ADMIN_AUTH,
    ),
)
async def disable(app_id: uuid.UUID, admin: CurrentSuperadmin, db: DbSession) -> StatusResponse:
    await _get_app_or_404(db, app_id)
    if not await _transition(db, app_id, AppStatus.DISABLED):
        raise AppApiError(409, "Only an approved app can be disabled.")
    await append_audit(
        db, actor_id=admin.id, action="disable", resource_type="app", resource_id=str(app_id)
    )
    await db.commit()
    return StatusResponse(app_id=app_id, status=AppStatus.DISABLED)


@router.post(
    "/{app_id}/enable",
    responses=error_responses(
        (404, ErrorEnvelope, "App not found"),
        (409, ErrorEnvelope, "Only a disabled app can be re-enabled"),
        *_ADMIN_AUTH,
    ),
)
async def enable(app_id: uuid.UUID, admin: CurrentSuperadmin, db: DbSession) -> StatusResponse:
    app = await _get_app_or_404(db, app_id)
    # Load-bearing guard: →approved also permits `pending`, so without this an enable
    # could promote an un-compiled pending app past the compile-gated approve path.
    if app.status is not AppStatus.DISABLED:
        raise AppApiError(409, "Only a disabled app can be re-enabled.")
    if not await _transition(db, app_id, AppStatus.APPROVED):
        raise AppApiError(409, "Only a disabled app can be re-enabled.")
    await append_audit(
        db, actor_id=admin.id, action="enable", resource_type="app", resource_id=str(app_id)
    )
    await db.commit()
    return StatusResponse(app_id=app_id, status=AppStatus.APPROVED)


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
        file_count=app.file_count,
        file_bytes=app.file_bytes,
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
    storage: Storage,
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

    records_removed, files_removed = await the_purge(
        db, storage, app_id, drafts_only=body.created_in_draft_only
    )
    await append_audit(
        db,
        actor_id=admin.id,
        action="clear-data",
        resource_type="app",
        resource_id=str(app_id),
        detail={"count": records_removed},
    )
    if files_removed > 0:
        await append_audit(
            db,
            actor_id=admin.id,
            action="file:clear",
            resource_type="app",
            resource_id=str(app_id),
            detail={"count": files_removed},
        )
    await db.commit()
    return ClearDataResponse(app_id=app_id, removed=records_removed, files_removed=files_removed)


@router.post(
    "/{app_id}/recompute-files",
    responses=error_responses((404, ErrorEnvelope, "App not found"), *_ADMIN_AUTH),
)
async def recompute(
    app_id: uuid.UUID, admin: CurrentSuperadmin, db: DbSession, storage: Storage
) -> RecomputeResponse:
    await _get_app_or_404(db, app_id)
    file_count, file_bytes, swept = await recompute_files(db, storage, app_id)
    await append_audit(
        db,
        actor_id=admin.id,
        action="file:gc",
        resource_type="app",
        resource_id=str(app_id),
        detail={"count": swept},
    )
    await db.commit()
    return RecomputeResponse(
        app_id=app_id, file_count=file_count, file_bytes=file_bytes, swept_pending=swept
    )


@router.delete(
    "/{app_id}",
    status_code=status.HTTP_200_OK,
    responses=error_responses((404, ErrorEnvelope, "App not found"), *_ADMIN_AUTH),
)
async def hard_delete(
    app_id: uuid.UUID, admin: CurrentSuperadmin, db: DbSession, storage: Storage
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
    await nuke_app(db, storage, app_id)
    await db.commit()
    return OkResponse(ok=True)


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


@users_router.get("/users", responses=error_responses(*_ADMIN_AUTH))
async def list_users(admin: CurrentSuperadmin, db: DbSession) -> UsersResponse:
    users = (await db.execute(sa.select(User).order_by(User.created_at))).scalars().all()
    overrides = {row.user_id: row for row in (await db.execute(sa.select(UserLimit))).scalars()}
    out: list[UserLimitsOut] = []
    for user in users:
        override = overrides.get(user.id)
        out.append(
            UserLimitsOut(
                user_id=user.id,
                email=user.email,
                display_name=user.display_name,
                role=role_for(user, settings.superadmin_emails),
                limits=_raw_limits(override),
                effective_limits=_effective_limits(override),
            )
        )
    defaults = LimitFields(
        daily_token_limit=settings.DAILY_TOKEN_LIMIT,
        context_soft_limit=DEFAULT_CONTEXT_SOFT,
        context_hard_limit=DEFAULT_CONTEXT_HARD,
    )
    return UsersResponse(defaults=defaults, users=out)


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
