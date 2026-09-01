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


class ChatKindInfo(BaseModel):
    """One entry in the U16/R73 catalogue (`services.agent.toolsets.CHAT_KIND_CATALOGUE`) —
    the value/name/description every surface that names a chat kind reads instead of writing
    its own. Plain `BaseModel` like `UserProfile`, not `CamelModel`: three lowercase
    single-word field names have no snake/camel seam to cross."""

    value: str
    name: str
    description: str


class UserProfile(BaseModel):
    """The current user's public profile — no secrets, no upn/token_version. Snake_case on the
    wire (deliberately NOT reparented onto CamelModel — `display_name`/`is_admin` are the SPA
    contract).

    `is_admin` is a DERIVED, read-only identity hint (email ∈ SUPERADMIN_EMAILS) so the SPA can
    render the admin entry point. It is NOT the authorization gate — every `/v1/admin/*` route is
    still enforced server-side by `requires_superadmin`; a forged `is_admin` buys nothing.

    `chat_kinds` rides this ONCE-CACHED bootstrap rather than a dedicated endpoint (R73): it is
    the whole catalogue of what a Plan chat and a Build chat ARE, so the composer, the history
    list and the help page all read the same two descriptions instead of each spelling its own."""

    id: uuid.UUID
    email: str
    display_name: str | None
    is_admin: bool
    # Effective limits so the client reflects a superadmin's per-user override (the daily badge +
    # the per-conversation guardrail) rather than silently using the global defaults.
    limits: ProfileLimits
    chat_kinds: list[ChatKindInfo]


class RefreshResponse(BaseModel):
    """Body returned on a successful silent refresh."""

    status: Literal["refreshed"]


class LogoutResponse(BaseModel):
    """Body returned once logout has cleared the client cookies."""

    status: Literal["logged_out"]
