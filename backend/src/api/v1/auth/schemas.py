"""Auth response schemas.

A deliberate mix (R11 carve-out): only `ProfileLimits` rides the shared `CamelModel`
(it already used the same camel config, so reparenting is byte-identical). `UserProfile`
is intentionally snake_case on the wire (`{id, email, display_name, is_admin, limits}` —
the SPA contract), and the refresh/logout status bodies are single-field literals, so
all three stay plain `BaseModel`. Reparenting any of them would rename wire keys.
"""

from __future__ import annotations

import uuid
from typing import Literal

from pydantic import BaseModel

from src.schemas import CamelModel


class ProfileLimits(CamelModel):
    """The user's EFFECTIVE limits — camelCase for the SPA, which reads `limits.contextSoftLimit`
    / `contextHardLimit` for the "getting long" guardrail and `dailyTokenLimit` for the badge."""

    daily_token_limit: int
    context_soft_limit: int
    context_hard_limit: int


class UserProfile(BaseModel):
    """The current user's public profile — no secrets, no upn/token_version. Snake_case on the
    wire (deliberately NOT reparented onto CamelModel — `display_name`/`is_admin` are the SPA
    contract).

    `is_admin` is a DERIVED, read-only identity hint (email ∈ SUPERADMIN_EMAILS) so the SPA can
    render the admin entry point. It is NOT the authorization gate — every `/v1/admin/*` route is
    still enforced server-side by `requires_superadmin`; a forged `is_admin` buys nothing."""

    id: uuid.UUID
    email: str
    display_name: str | None
    is_admin: bool
    # Derived via User.status() (never stored) — drives RequireAuth's 'pending'
    # branch. In practice this is only ever "pending" or "approved" here: a
    # "disabled" user is already 403'd by current_user's unconditional suspension
    # check before this handler runs.
    status: Literal["pending", "approved", "disabled"]
    # Effective limits so the client reflects a superadmin's per-user override (the daily badge +
    # the per-conversation guardrail) rather than silently using the global defaults.
    limits: ProfileLimits


class RefreshResponse(BaseModel):
    """Body returned on a successful silent refresh."""

    status: Literal["refreshed"]


class LogoutResponse(BaseModel):
    """Body returned once logout has cleared the client cookies."""

    status: Literal["logged_out"]
