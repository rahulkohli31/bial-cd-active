"""Super-admin governance + user-limits/feedback schemas.

All request/response models for the two admin routers (`/admin/apps` governance and
`/admin` users/limits/feedback), on the shared `CamelModel` base — camelCase over
the wire, matching the admin SPA panels (`AppRegistryPanel`, `AuditDrawer`, …).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from src.db.models.app_registry import AppStatus
from src.schemas import CamelModel

# --- governance (`/admin/apps`) ------------------------------------------------


class AdminAppOut(CamelModel):
    """The admin projection — NEVER the code blobs or the app key."""

    app_id: uuid.UUID
    name: str
    owner_id: uuid.UUID
    # The owner's human handle (email/display name) so the admin UI can render the Owner cell
    # (`AppRegistryPanel` reads `ownerUsername`); the raw `ownerId` uuid is not user-facing.
    owner_username: str | None
    status: AppStatus
    login_required: bool
    data_count: int
    data_bytes: int
    file_count: int
    file_bytes: int
    has_approved_snapshot: bool
    approved_by: uuid.UUID | None
    approved_at: datetime | None
    rejection_note: str | None
    created_at: datetime
    updated_at: datetime


class AppListResponse(CamelModel):
    apps: list[AdminAppOut]


class StatusResponse(CamelModel):
    app_id: uuid.UUID
    status: AppStatus


class RejectRequest(CamelModel):
    note: str | None = None


class PatchAppRequest(CamelModel):
    name: str | None = None
    login_required: bool | None = None


class DataSummaryResponse(CamelModel):
    app_id: uuid.UUID
    data_count: int
    data_bytes: int
    file_count: int
    file_bytes: int
    confirm_token: str


class ClearDataRequest(CamelModel):
    confirm_token: str
    created_in_draft_only: bool = False


class ClearDataResponse(CamelModel):
    app_id: uuid.UUID
    removed: int
    files_removed: int


class RecomputeResponse(CamelModel):
    app_id: uuid.UUID
    file_count: int
    file_bytes: int
    swept_pending: int


class OkResponse(CamelModel):
    ok: bool


class AuditEventOut(CamelModel):
    id: uuid.UUID
    actor_id: uuid.UUID | None
    # The actor's human handle (email), resolved from `actor_id`, so the admin AuditDrawer can
    # name the actor instead of showing a raw uuid or "anonymous". None if the actor was deleted.
    username: str | None
    action: str
    resource_type: str
    resource_id: str | None
    detail: dict[str, Any] | None
    # The count-bearing detail (records/files cleared, flag flips) surfaced top-level for the UI.
    count: int | None
    created_at: datetime


class AuditListResponse(CamelModel):
    events: list[AuditEventOut]


# --- users / limits / feedback (`/admin`) --------------------------------------


class LimitFields(CamelModel):
    daily_token_limit: int | None = None
    context_soft_limit: int | None = None
    context_hard_limit: int | None = None


class UserLimitsOut(CamelModel):
    user_id: uuid.UUID
    email: str
    display_name: str | None
    role: str
    limits: LimitFields
    effective_limits: LimitFields


class UsersResponse(CamelModel):
    defaults: LimitFields
    users: list[UserLimitsOut]


class LimitsPatchResponse(CamelModel):
    user_id: uuid.UUID
    limits: LimitFields
    effective_limits: LimitFields


class FeedbackItem(CamelModel):
    user_id: uuid.UUID
    email: str
    message: str
    page: str
    created_at: datetime


class FeedbackResponse(CamelModel):
    feedback: list[FeedbackItem]
    total: int
