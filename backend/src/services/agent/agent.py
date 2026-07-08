"""The Pydantic AI chat agent (R10, R11).

ONE module-level `Agent`, built without a bound model — the Foundry model is passed per-run
(`agent.run_stream(prompt, model=…)`) so import never depends on a configured Foundry (dev/test
boot without it) and tests inject a `TestModel`. `ChatDeps` is built per request and scopes any
tool to the caller's `user_id` (a dropped scope predicate is a cross-user leak).

The per-request system prompt (planning/assistant/builder — resolved SPA-side and sent in the
request body) flows in via `deps.system` and is applied through `instructions`, NOT
`system_prompt`: instructions are not baked into stored message history, so the prompt can evolve
without rewriting history.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from pydantic_ai import Agent, RunContext
from sqlalchemy.ext.asyncio import AsyncSession


@dataclass
class ChatDeps:
    """Per-request agent dependencies. `db` + `user_id` scope any tool to the caller; `system`
    is the SPA-supplied per-kind system prompt applied via `instructions`."""

    db: AsyncSession
    user_id: uuid.UUID
    system: str = ""


chat_agent = Agent(deps_type=ChatDeps, retries=2)


@chat_agent.instructions
def _system_instructions(ctx: RunContext[ChatDeps]) -> str:
    # The SPA owns the system prompt (stateless-relay contract); apply it as instructions so it
    # stays out of any persisted history.
    return ctx.deps.system
