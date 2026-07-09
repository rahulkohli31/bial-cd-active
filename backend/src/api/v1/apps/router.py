"""App-lifecycle endpoints — owner-facing provision / submit / status (R18, R4).

The builder flow: `provision` mints (or reuses) the project's ONE draft + publishable
appKey (one app per project, KD-4); `submit` snapshots the client-compiled build and
moves draft→pending; `status` is an owner-scoped read.
All three authenticate the owner via `current_user` and are scoped by `user_id`
(ADR-0004) — a cross-user read is a 404, never a leak.

Compilation is the CLIENT's job (R19/AE4): submit carries the browser-produced
compiled artifact, which `validate_artifact` checks for presence/shape and stores
in the source snapshot — the server never runs Babel. Approval (admin, U8) promotes
that snapshot to the approved artifact the runner serves.

Errors use the ported `{"error": {"message": ...}}` shape (`AppApiError`) the SPA
already consumes, not the auth endpoints' `{"detail": ...}`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import sqlalchemy as sa
from fastapi import APIRouter, status
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.api.deps import CurrentUser, DbSession
from src.api.v1.apps.schemas import (
    AppStatusResponse,
    LifecycleResponse,
    ProvisionRequest,
    SubmitRequest,
    SubmitResponse,
)
from src.core.errors import AppApiError
from src.db.models.app_registry import (
    STATUS_TRANSITIONS,
    AppRegistry,
    AppStatus,
    mint_app_key,
)
from src.schemas import AUTH_401, ErrorEnvelope, error_responses
from src.services.appserving.artifact import ArtifactError, validate_artifact
from src.services.audit.log import append_audit
from src.services.projects import owned_project_or_404

router = APIRouter(prefix="/apps", tags=["apps"])


# All three routes authenticate via `current_user` (bare HTTPException 401 ->
# `{"detail"}`), so each documents 401 via the shared `AUTH_401` (DetailBody) spec; the
# routes' own raises are `AppApiError` -> `ErrorEnvelope`.


@router.post(
    "/provision",
    status_code=status.HTTP_201_CREATED,
    responses=error_responses(
        AUTH_401,
        (404, ErrorEnvelope, "Project not found (or not owned by the caller)"),
        (409, ErrorEnvelope, "Project app is owned by another user"),
    ),
)
async def provision(body: ProvisionRequest, user: CurrentUser, db: DbSession) -> LifecycleResponse:
    """Mint (or reuse) the project's ONE draft + appKey — one app per project (KD-4).

    Provision targets a PROJECT, not a conversation: `project_id` is REQUIRED
    (project-first — every app lives in a caller-owned project) and is the idempotency
    key. If the project already has an app (`uq_app_registry_project`) the SAME row + its
    original appKey are returned (the continuity case — a new builder session builds against
    the existing app), and the acting conversation is recorded as the head/last-builder
    pointer; otherwise the single project app is minted. The SPA addresses submit/status by
    the RETURNED appId. The appKey is a publishable `secrets.token_urlsafe` label, never a
    raw UUID (ADR-0006)."""
    project = await owned_project_or_404(db, user.id, body.project_id)
    # Upsert on the one-app-per-project constraint: a first provision INSERTs the app; a
    # repeat in the same project DO-UPDATEs (advancing the head conversation), so provision
    # stays idempotent per project and races collapse onto the single row. The owner-guarded
    # WHERE means the DO-UPDATE only touches the caller's own app (defense in depth — the
    # project is already owner-validated).
    upsert = (
        pg_insert(AppRegistry)
        .values(
            user_id=user.id,
            project_id=project.id,
            conversation_id=body.conversation_id,
            app_key=mint_app_key(),
            status=AppStatus.DRAFT,
        )
        .on_conflict_do_update(
            constraint="uq_app_registry_project",
            set_={"conversation_id": body.conversation_id, "updated_at": sa.func.now()},
            where=(AppRegistry.user_id == user.id),
        )
        .returning(
            AppRegistry.id,
            AppRegistry.app_key,
            AppRegistry.login_required,
            AppRegistry.status,
        )
    )
    row = (await db.execute(upsert)).one_or_none()
    if row is None:
        # The project's app belongs to another user (an ownership-invariant violation):
        # fail closed rather than touch or reveal it (ADR-0004).
        raise AppApiError(status.HTTP_409_CONFLICT, "Project app is owned by another user.")
    await db.commit()
    return LifecycleResponse(
        app_id=row.id,
        app_key=row.app_key,
        login_required=row.login_required,
        status=row.status,
    )


async def _owned_app_or_404(db: DbSession, app_id: uuid.UUID, user_id: uuid.UUID) -> AppRegistry:
    """Load an app scoped to its owner, or fail closed with a non-leaking 404 (a
    cross-user id is indistinguishable from a missing one)."""
    app = await db.get(AppRegistry, app_id)
    if app is None or app.user_id != user_id:
        raise AppApiError(status.HTTP_404_NOT_FOUND, "App not found.")
    return app


@router.post(
    "/{app_id}/submit",
    responses=error_responses(
        (400, ErrorEnvelope, "Empty source or invalid compiled artifact"),
        AUTH_401,
        (404, ErrorEnvelope, "App not found"),
        (409, ErrorEnvelope, "App cannot be submitted in its current state"),
    ),
)
async def submit(
    app_id: uuid.UUID, body: SubmitRequest, user: CurrentUser, db: DbSession
) -> SubmitResponse:
    """Snapshot the client-compiled build and move draft→pending (audited).

    A submit is legal from draft/rejected/approved (resubmit paths) and is a no-op
    refresh from pending; from disabled it is rejected (409). The transition is a
    single atomic guarded UPDATE — an illegal source state updates zero rows."""
    await _owned_app_or_404(db, app_id, user.id)  # existence + ownership (404)

    if not body.source.strip():
        raise AppApiError(
            status.HTTP_400_BAD_REQUEST, "Nothing to submit — generate an app first."
        )
    try:
        compiled = validate_artifact(body.compiled)
    except ArtifactError as exc:
        raise AppApiError(status.HTTP_400_BAD_REQUEST, str(exc)) from exc

    snapshot = {
        "src": body.source,
        "entry": body.entry or "PreviewApp",
        "compiled": compiled,
        "at": datetime.now(UTC).isoformat(),
    }
    # A submit is allowed from the pending-transition sources PLUS pending itself
    # (a re-submit refresh). Disabled falls outside → the guarded UPDATE matches
    # nothing → 409. Scoped by user_id too (the mutation carries the ownership
    # predicate, ADR-0004).
    submit_from = tuple(STATUS_TRANSITIONS[AppStatus.PENDING] | {AppStatus.PENDING})
    moved = await db.execute(
        sa.update(AppRegistry)
        .where(
            AppRegistry.id == app_id,
            AppRegistry.user_id == user.id,
            AppRegistry.status.in_(submit_from),
        )
        .values(status=AppStatus.PENDING, source_snapshot=snapshot)
        .returning(AppRegistry.id)
    )
    if moved.first() is None:
        raise AppApiError(
            status.HTTP_409_CONFLICT, "This app cannot be submitted in its current state."
        )

    await append_audit(
        db, actor_id=user.id, action="submit", resource_type="app", resource_id=str(app_id)
    )
    await db.commit()
    return SubmitResponse(app_id=app_id, status=AppStatus.PENDING)


@router.get(
    "/{app_id}/status",
    # read_status never raises — an absent/cross-user app returns 200 `{status: null}`
    # (documented as-is, not normalized to 404). Only the inherited 401 is documented.
    responses=error_responses(AUTH_401),
)
async def read_status(app_id: uuid.UUID, user: CurrentUser, db: DbSession) -> AppStatusResponse:
    """Owner-scoped lifecycle read. An absent (or cross-user, indistinguishable) app yields a
    200 `{status: null}` — the SPA's "not provisioned" signal — NOT a 404, so a genuine
    5xx/auth failure stays a real error the client surfaces (Express parity)."""
    app = await db.get(AppRegistry, app_id)
    if app is None or app.user_id != user.id:
        return AppStatusResponse(
            app_id=app_id,
            status=None,
            app_key=None,
            login_required=False,
            rejection_note=None,
        )
    return AppStatusResponse(
        app_id=app.id,
        status=app.status,
        app_key=app.app_key,
        login_required=app.login_required,
        rejection_note=app.rejection_note,
    )
