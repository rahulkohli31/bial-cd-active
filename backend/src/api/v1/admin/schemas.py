"""Super-admin governance + user-limits/feedback schemas.

All request/response models for the two admin routers (`/admin/apps` governance and
`/admin` users/limits/feedback), on the shared `CamelModel` base — camelCase over
the wire, matching the admin SPA panels (`AppRegistryPanel`, `AuditDrawer`, …).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from pydantic import Field

from src.db.models.app_registry import AppStatus
from src.schemas import CamelModel

# --- governance (`/admin/apps`) ------------------------------------------------


class AdminAppOut(CamelModel):
    """The admin projection — NEVER the code blobs, the app key, or a signed URL
    (a bearer credential is minted only by the dedicated download endpoint, R15)."""

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
    # Derived from the approved pin (`approved_submission_id is not None`) — the old
    # JSX-snapshot derivation is gone with the column it read.
    has_approved_snapshot: bool
    # The submission under review (R16): what the reviewer inspects, and the id
    # approve must echo back (D5).
    submission_id: uuid.UUID | None
    commit_sha: str | None
    submitted_at: datetime | None
    # The approved pin (R4): the artifact the runbook operator deploys — the SHA is
    # their identity check after cloning the downloaded bundle.
    approved_submission_id: uuid.UUID | None
    approved_commit_sha: str | None
    approved_by: uuid.UUID | None
    approved_at: datetime | None
    # The manual-runbook marker (R17, D7): `redeploy_needed` is exact —
    # `approved_submission_id != deployed_submission_id` — so an approved-but-
    # undeployed app and a re-approved-since-deploy app both surface it.
    deployed_at: datetime | None
    redeploy_needed: bool
    rejection_note: str | None
    created_at: datetime
    updated_at: datetime


class AppListResponse(CamelModel):
    apps: list[AdminAppOut]


class AdminAppStatusResponse(CamelModel):
    app_id: uuid.UUID
    status: AppStatus


class ApproveRequest(CamelModel):
    # The submission id the admin ACTUALLY reviewed (D5): the guarded UPDATE adds
    # `AND source_submission_id = :submission_id`, so a re-submit between the
    # admin's review and their click updates zero rows → 409, never a silent
    # promotion of an unreviewed bundle.
    submission_id: uuid.UUID


class BundleUrlResponse(CamelModel):
    """The audited out-of-band review download (R15). `url` is a short-TTL bearer
    credential — the SPA uses it immediately and never stores it; it is likewise
    never written to the audit trail."""

    url: str
    submission_id: uuid.UUID
    commit_sha: str | None
    expires_in_seconds: int


class MarkDeployedResponse(CamelModel):
    app_id: uuid.UUID
    deployed_submission_id: uuid.UUID
    deployed_at: datetime


class DeployCredentialResponse(CamelModel):
    """The long-lived per-app Blob credential the go-live runbook injects into the deployed
    container as `BIAL_BLOB_CONTAINER_URL` + `BIAL_BLOB_SAS` (U2/R2). `sas` is a 365-day bearer
    credential: the admin pastes it straight into an ACA secret and it is NEVER logged, NEVER
    written to the audit trail (the audit row carries the expiry, not the token), and never part
    of any list projection. `expiresAt` comes from the app's stored access policy — deleting that
    policy revokes this credential (the runbook's incident-response lever)."""

    container_url: str
    sas: str
    expires_at: datetime


class RejectRequest(CamelModel):
    # Bounded at the boundary: an over-long note used to be sliced to 1000 chars in the handler,
    # so the admin's reasoning was silently truncated and they never learned it happened.
    note: str | None = Field(default=None, max_length=1000)


class PatchAppRequest(CamelModel):
    # Same rule as `RejectRequest.note` — a 422 beats a silent chop to 120 chars.
    name: str | None = Field(default=None, max_length=120)
    login_required: bool | None = None


class DataSummaryResponse(CamelModel):
    app_id: uuid.UUID
    data_count: int
    data_bytes: int
    confirm_token: str


class ClearDataRequest(CamelModel):
    confirm_token: str
    created_in_draft_only: bool = False


class ClearDataResponse(CamelModel):
    app_id: uuid.UUID
    removed: int


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
    # Local suspension marker (R10): null = active. Surfaced so the roster shows
    # who is blocked without a per-user read.
    suspended_at: datetime | None
    # Today's folded token spend (all four classes, IST day) — one page-wide
    # aggregate feeds this, never a per-row query (R9).
    usage_today: int
    limits: LimitFields
    effective_limits: LimitFields


class UsersResponse(CamelModel):
    """The roster page. Keyset envelope fields (KD-1) are additive next to the
    original `{defaults, users}` shape — a called-out SPA contract change (U9)."""

    defaults: LimitFields
    users: list[UserLimitsOut]
    next_cursor: str | None
    has_more: bool


class SuspensionResponse(CamelModel):
    user_id: uuid.UUID
    suspended_at: datetime | None


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
