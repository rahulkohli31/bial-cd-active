"""App-lifecycle request/response schemas — provision / submit / status.

camelCase over the wire (via the shared `CamelModel`), matching the SPA/TS
convention the `/api/apps/*` clients already consume.
"""

from __future__ import annotations

import uuid

from pydantic import Field

from src.db.models.app_registry import AppStatus
from src.schemas import CamelModel


class ProvisionRequest(CamelModel):
    # The builder conversation this app is born from — the idempotency key AND the
    # soft ownership link (Plan A's conversations, concurrent). Required.
    conversation_id: uuid.UUID


class LifecycleResponse(CamelModel):
    """provision/submit response (appId + appKey + gate + status)."""

    app_id: uuid.UUID
    app_key: str
    login_required: bool
    status: AppStatus


class SubmitRequest(CamelModel):
    # The build source (JSX) and the CLIENT-compiled artifact. `entry` names the
    # root component (Express default 'PreviewApp'). Sizes are bounded here and in
    # `validate_artifact` (compiled) — Starlette applies no body cap itself. Empty
    # source is checked in-handler for the exact ported 400 (not a schema 422).
    source: str = Field(max_length=2 * 1024 * 1024)
    entry: str | None = Field(default=None, max_length=200)
    compiled: str = Field(max_length=2 * 1024 * 1024)


class SubmitResponse(CamelModel):
    app_id: uuid.UUID
    status: AppStatus


class AppStatusResponse(CamelModel):
    # `status is None` ⇔ the caller has no such (owner-scoped) app yet — the SPA reads
    # `status ? … : null` as "not provisioned", a normal non-error result (Express parity).
    app_id: uuid.UUID
    status: AppStatus | None
    app_key: str | None
    login_required: bool
    rejection_note: str | None
