"""Effective per-user limits — the daily token cap and per-conversation context guardrails
(soft/hard), resolved with the clamps the portal has always applied.

The SINGLE source of truth shared by the admin `/admin/users` endpoint and `/auth/me`, so a
superadmin's per-user override reaches the client instead of it falling back to the global
defaults. Daily reuses the gate's resolver so the badge and the 429 gate can never diverge.

WHO ENFORCES WHICH: `hard` is the SERVER's — `usage/context_window.enforce_context_limit`
refuses a turn before anything is persisted. `soft` is the browser's: advisory only, it blocks
nothing, and exists to warn the citizen in time to start a new chat rather than be told at the
wall.
"""

from __future__ import annotations

import uuid
from typing import Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models.user_limit import UserLimit
from src.services.usage.gate import effective_daily_limit

# Context guardrails — the per-conversation warn/stop thresholds.
#
# THE WINDOW IS MEASURED, NOT READ OFF A PAGE. `1_000_000` is the figure the deployment itself
# named when it refused an oversized prompt through the exact production chain
# (`AsyncAnthropicFoundry` → `AnthropicProvider` → `AnthropicModel` → pydantic-ai `Agent`)
# against `claude-opus-4-7` on `bial-genai-vibecoding2`: `prompt is too long: 1963668 tokens >
# 1000000 maximum`, reproduced twice.
# The `200_000` it replaces was inherited from the Express prototype and was never checked
# against this deployment — it made the per-chat ceiling five times smaller than what the
# platform actually serves.
#
# IT IS A CLAMP, WHICH IS WHY IT MOVES WITH THE CEILING. `effective_context` caps `hard` at this
# number, so raising the ceiling and leaving the window behind makes the new ceiling silently
# inert. And it is the boundary an administrator's number is validated against, so it must be
# what the provider will actually serve rather than what we hope it will.
MODEL_CONTEXT_WINDOW = 1_000_000
# The ceiling sits at HALF the served window, deliberately. It is the number a citizen's chat is
# refused at, not the number the provider breaks at, and leaving that much headroom means a
# conversation is ended by our own guardrail — with a sentence that tells them what to do — long
# before it can hit a provider error we cannot phrase.
DEFAULT_CONTEXT_SOFT = 375_000
DEFAULT_CONTEXT_HARD = 500_000

SYSTEM_PROMPT_RESERVE: Final = 8_000
"""What a run costs before the citizen has typed anything — the per-run system prompt and the
tool schemas that ride with it.

Measured at the time of writing: the Plan segment composes to ~1,800 tokens and the Build
segment — the larger — to ~4,400, before the tool schemas. 8,000 covers the larger of the two
with room for the schemas and for both to grow.

★ NOTHING HOLDS IT BACK ANY MORE, AND THAT IS AN IMPROVEMENT RATHER THAN A LOSS. This used to
be a reserve in the literal sense: the gate estimated a conversation and then added this,
because the system prompt was composed after the gate had decided and the estimate could not
see it. The gate now reads the token count the PROVIDER reported for a completed turn, and that
count is of the whole prompt — system segment, tool schemas and all. There is nothing left for
it to be blind to, so there is nothing to reserve.

IT SURVIVES AS A SIZE, NOT AS A CHARGE. The floor below is derived from it, and the admin
panel's `contextLimits.SYSTEM_PROMPT_RESERVE` is its browser twin, so an administrator's lowest
settable ceiling is expressed in the same unit on both sides."""

CONTEXT_HARD_FLOOR: Final = SYSTEM_PROMPT_RESERVE * 2
"""The lowest per-user chat length that still leaves a usable chat.

AN ADMINISTRATOR MUST NOT BE ABLE TO LOCK A CITIZEN OUT, and below this they could. Every run
spends `SYSTEM_PROMPT_RESERVE` on its system prompt and tool schemas before the citizen has
typed a word, and the provider counts that in the very first turn it reports — so a hard limit
at or under the reserve refuses that person's SECOND message in every chat they open, forever,
whatever they wrote in the first. Only another administrator raising the number gets them
working again, and nothing in the product says that is what happened.

TWICE THE RESERVE, not the reserve plus one: a floor that merely clears the reserve would leave
a chat with room for a sentence and no reply. This leaves the reserve plus an equal amount of
real conversation — tight, deliberate, and still a chat someone can use."""


def effective_context(override: UserLimit | None) -> tuple[int, int]:
    """Resolve (soft, hard) with Express's clamps: hard ≤ model window and ≥ the floor; soft in
    [1, hard-1]. A non-positive/absent override falls back to the default.

    THE FLOOR IS APPLIED AT READ TIME AS WELL AS WRITE TIME — both are needed. The admin PATCH
    validator refuses a new value below it; this clamp is what keeps a value ALREADY stored below
    the floor (written before the validator existed) from locking that citizen out of every chat
    they own."""
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
