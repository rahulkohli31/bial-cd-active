"""Effective per-user limits — the daily token cap (Plan A's live gate) + the SPA
per-conversation context guardrails (soft/hard), resolved with Express's clamps.

The SINGLE source of truth shared by the admin `/admin/users` endpoint AND `/auth/me`, so a
superadmin's per-user override reaches the client (the daily badge + the "getting long" guardrail
banner) instead of the client silently falling back to the global defaults. Daily reuses the
gate's resolver so the badge and the 429 gate can never diverge.
"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models.user_limit import UserLimit
from src.services.usage.gate import effective_daily_limit

# Context guardrails (Express `limits.js`) — the SPA per-conversation warn/stop thresholds.
MODEL_CONTEXT_WINDOW = 200_000
DEFAULT_CONTEXT_SOFT = 150_000
DEFAULT_CONTEXT_HARD = 200_000

def effective_context(override: UserLimit | None) -> tuple[int, int]:
    """Resolve (soft, hard) with Express's clamps: hard ≤ the model window; soft in [1, hard-1].
    A non-positive/absent override falls back to the default (0/negative never caps to nothing)."""
    hard_raw = (
        override.context_hard_limit
        if override and override.context_hard_limit and override.context_hard_limit > 0
        else DEFAULT_CONTEXT_HARD
    )
    hard = min(hard_raw, MODEL_CONTEXT_WINDOW)
    soft_raw = (
        override.context_soft_limit
        if override and override.context_soft_limit and override.context_soft_limit > 0
        else DEFAULT_CONTEXT_SOFT
    )
    soft = max(1, min(soft_raw, hard - 1))
    return soft, hard


async def effective_limits_for(db: AsyncSession, user_id: uuid.UUID) -> tuple[int, int, int]:
    """(daily, context_soft, context_hard) for one user — the values the client should enforce.
    Loads the user's override once; daily via the gate resolver so the badge and the gate agree."""
    daily = await effective_daily_limit(db, user_id)
    override = await db.scalar(select(UserLimit).where(UserLimit.user_id == user_id))
    soft, hard = effective_context(override)
    return daily, soft, hard
