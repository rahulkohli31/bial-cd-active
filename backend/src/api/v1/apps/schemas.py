"""App-lifecycle request/response schemas — provision / submit / status.

camelCase over the wire (via the shared `CamelModel`), matching the SPA/TS
convention the `/api/apps/*` clients already consume.
"""

from __future__ import annotations

import uuid
from datetime import datetime

from src.db.models.app_registry import AppStatus
from src.schemas import CamelModel


class ProvisionRequest(CamelModel):
    # The builder conversation this provision acts from — recorded as the app's head /
    # last-builder-session pointer (KD-4). No longer the app's PK or idempotency key.
    conversation_id: uuid.UUID
    # The project this app belongs to (one app per project, KD-4). REQUIRED — project-first:
    # every app lives in a caller-owned project, and the project is the idempotency key
    # (a repeat provision in the same project reuses its single app). Missing → 422.
    project_id: uuid.UUID


class LifecycleResponse(CamelModel):
    """provision/submit response (appId + appKey + gate + status)."""

    app_id: uuid.UUID
    app_key: str
    login_required: bool
    status: AppStatus


# Submit takes NO request body (APPROVAL R19): the artifact is the app's server-side
# git-bundle snapshot, copied to an immutable submission blob — nothing is
# client-supplied, which is the whole point of the open-sandbox pivot.


class SubmitResponse(CamelModel):
    app_id: uuid.UUID
    status: AppStatus
    # The immutable submission the copy minted (R1/R4): the id the admin's approve
    # must echo back (D5) and the bundle's HEAD commit SHA for provenance.
    submission_id: uuid.UUID
    commit_sha: str
    submitted_at: datetime


class AppStatusResponse(CamelModel):
    # A resolved app ALWAYS has a status and an appKey — an absent/cross-user one is a 404, not
    # a null-signalling 200 (the `status: null` "not provisioned" shim is gone). The submission
    # fields are legitimately absent until the first submit; `rejectionNote` is set solely on
    # the rejected transition (and cleared by the next submit).
    app_id: uuid.UUID
    status: AppStatus
    app_key: str
    login_required: bool
    rejection_note: str | None
    submission_id: uuid.UUID | None
    commit_sha: str | None
    submitted_at: datetime | None


class AppSourceResponse(CamelModel):
    # The project's ONE durable app code (KD-9), read by appId so ANY builder chat in the
    # project can render the preview — not just the chat that first generated it. `source` is
    # the empty string when no code has landed yet ("" = nothing to render, LivePreview's empty
    # state); `entry` names the root component (default 'PreviewApp').
    app_id: uuid.UUID
    source: str
    entry: str
