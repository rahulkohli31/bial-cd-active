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
    # A resolved app ALWAYS has a status and an appKey — an absent/cross-user one is a 404, not
    # a null-signalling 200 (the `status: null` "not provisioned" shim is gone). Only
    # `rejectionNote` is legitimately absent (set solely on the rejected transition).
    app_id: uuid.UUID
    status: AppStatus
    app_key: str
    login_required: bool
    rejection_note: str | None


class AppSourceResponse(CamelModel):
    # The project's ONE durable app code (KD-9), read by appId so ANY builder chat in the
    # project can render the preview — not just the chat that first generated it. `source` is
    # the empty string when no code has landed yet ("" = nothing to render, LivePreview's empty
    # state); `entry` names the root component (default 'PreviewApp').
    app_id: uuid.UUID
    source: str
    entry: str
