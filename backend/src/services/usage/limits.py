"""Effective per-user limits — the daily token cap and the per-conversation context
guardrails (soft/hard), resolved with the clamps the portal has always applied.

The SINGLE source of truth shared by the admin `/admin/users` endpoint AND `/auth/me`, so a
superadmin's per-user override reaches the client (the daily badge + the "getting long"
warning) instead of the client silently falling back to the global defaults. Daily reuses the
gate's resolver so the badge and the 429 gate can never diverge.

WHO ENFORCES WHICH. `hard` is the SERVER's: `usage/context_window.enforce_context_limit`
refuses a turn at the route, before anything is persisted. `soft` is the browser's: it is
advisory, it blocks nothing, and it exists to warn the citizen in time to start a new chat
rather than to be told at the wall. This docstring used to call both of them "the values the
client should enforce", which was true of neither — nothing enforced them at all, front or
back, while an administrator was being shown a field promising a hard stop.
"""

from __future__ import annotations

import uuid
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models.user_limit import UserLimit
from src.services.usage.gate import effective_daily_limit

# Context guardrails (Express `limits.js`) — the SPA per-conversation warn/stop thresholds.
MODEL_CONTEXT_WINDOW = 200_000
DEFAULT_CONTEXT_SOFT = 150_000
DEFAULT_CONTEXT_HARD = 200_000

SYSTEM_PROMPT_RESERVE: Final = 8_000
"""Room the context gate holds back for what it cannot see.

The per-run system prompt is composed inside the turn engine, AFTER the gate has decided, so
it is in neither the history nor the prompt the gate measures. Measured at the time of
writing: the Plan segment composes to ~1,800 tokens and the Build segment — the larger — to
~4,400, before the tool schemas the run also carries. 8,000 covers the larger of the two with
room for the schemas and for both to grow.

Without it the gate is quietly permissive at exactly the setting that matters most: the
DEFAULT hard limit equals the model's own window, so a conversation measured at 199,000 would
be waved through into a prompt that is really 204,000 and fail at the model instead — which is
the opaque failure the whole guardrail exists to replace.

IT LIVES HERE, BESIDE THE OTHER WINDOW NUMBERS, rather than in the gate that spends it. The
floor below is derived from it, the gate imports it from here, and having the derivation and
the number in two modules that import each other is not open — `context_window` already reads
`effective_context` from this one."""

CONTEXT_HARD_FLOOR: Final = SYSTEM_PROMPT_RESERVE * 2
"""The lowest per-user chat length that still leaves a usable chat.

AN ADMINISTRATOR MUST NOT BE ABLE TO LOCK A CITIZEN OUT, and below this they could. The gate
charges every conversation `SYSTEM_PROMPT_RESERVE` before it has counted a single word, so a
hard limit at or under the reserve refuses EVERY conversation that person opens — including a
brand-new empty one — and the refusal tells them to start a new chat, which is both untrue and
the one thing that also fails. Only another administrator raising the number gets them working
again, and nothing in the product says that is what happened.

TWICE THE RESERVE, not the reserve plus one: a floor that merely clears the reserve would leave
a chat with room for a sentence and no reply. This leaves the reserve plus an equal amount of
real conversation — tight, deliberate, and still a chat someone can use."""


def effective_context(override: UserLimit | None) -> tuple[int, int]:
    """Resolve (soft, hard) with Express's clamps: hard ≤ the model window and ≥ the floor;
    soft in [1, hard-1]. A non-positive/absent override falls back to the default (0/negative
    never caps to nothing).

    THE FLOOR IS APPLIED AT READ TIME AS WELL AS AT WRITE TIME, and both halves are needed. The
    admin PATCH validator refuses a new value below it, which is where an administrator learns
    why; this clamp is what keeps a value ALREADY stored below the floor — written before the
    validator existed — from locking that citizen out of every chat they own. Validation alone
    would leave the people the defect already reached exactly where it left them."""
    hard_raw = (
        override.context_hard_limit
        if override and override.context_hard_limit and override.context_hard_limit > 0
        else DEFAULT_CONTEXT_HARD
    )
    hard = max(CONTEXT_HARD_FLOOR, min(hard_raw, MODEL_CONTEXT_WINDOW))
    soft_raw = (
        override.context_soft_limit
        if override and override.context_soft_limit and override.context_soft_limit > 0
        else DEFAULT_CONTEXT_SOFT
    )
    soft = max(1, min(soft_raw, hard - 1))
    return soft, hard


async def effective_limits_for(db: AsyncSession, user_id: uuid.UUID) -> tuple[int, int, int]:
    """(daily, context_soft, context_hard) for one user, as the client is told them: `daily`
    drives the badge, `context_soft` the browser's warning, `context_hard` the number the
    SERVER refuses at (sent so the browser can describe the boundary, never so it can police
    it). Loads the user's override once; daily via the gate resolver so the badge and the gate
    agree."""
    daily = await effective_daily_limit(db, user_id)
    override = await db.scalar(select(UserLimit).where(UserLimit.user_id == user_id))
    soft, hard = effective_context(override)
    return daily, soft, hard
