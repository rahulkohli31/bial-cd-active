"""Usage domain response schema."""

from __future__ import annotations

from src.schemas import CamelModel


class UsageTodayResponse(CamelModel):
    """Today's token usage for the caller — the Express `/api/usage/today` shape
    `{used, limit, remaining, resetsAt}`. `resets_at` -> `resetsAt` comes from the
    camel base's `alias_generator`, replacing the old per-field `serialization_alias`.
    """

    used: int
    limit: int
    remaining: int
    resets_at: str
