"""The Pydantic AI chat agent (R10, R11, U9).

ONE module-level `Agent`, built without a bound model — the Foundry model is passed per-run
(`agent.run_stream(prompt, model=…)`) so import never depends on a configured Foundry (dev/test
boot without it) and tests inject a `TestModel`. `ChatDeps` is built per request and scopes any
tool to the caller's `user_id` (a dropped scope predicate is a cross-user leak).

The per-run system prompt has two sources, selected by `deps.mode` (U9/D4):

- `mode is None` — the relay path (U7): the server composed `deps.system` from the
  conversation's kind (`claude/router.py::_compose_system`); it is applied verbatim.
  TODO(U10/U13): retires with the relay once the turn engine owns all traffic.
- `mode` set — a mode-gated turn (U10 always sets it): BASE + that mode's segment composed
  by `mode_prompts.compose_mode_prompt` from `deps.prompt_context` (+ `deps.approved_plan`
  on a plan-executing Write turn).

Either way the text is applied through `instructions`, NOT `system_prompt`: instructions are
never baked into stored message history, so prompts evolve without rewriting history — the
same boundary that keeps U14's ephemeral reminders out of the DB.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from pydantic_ai import Agent, RunContext
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models.conversation import ConversationMode
from src.services.agent.mode_prompts import PromptContext, compose_mode_prompt
from src.services.agent.read_tools import ReadOnlyWorkspace


@dataclass
class ChatDeps:
    """Per-request agent dependencies. `db` + `user_id` scope any tool to the caller.

    `system` is the relay path's server-composed prompt (mode None). A mode-gated turn
    (U10) sets `mode` + `prompt_context` instead — and `approved_plan` when the Write turn
    executes a user-approved plan. Setting a mode without its context is a programming
    error, caught fail-first at instruction time (never a silently empty prompt).

    `workspace` is the turn-pinned read surface a mode-gated run's toolsets resolve
    through (U10 sets it; the relay path never does — its runs carry no tools).
    """

    db: AsyncSession
    user_id: uuid.UUID
    system: str = ""
    mode: ConversationMode | None = None
    prompt_context: PromptContext | None = None
    approved_plan: str | None = None
    workspace: ReadOnlyWorkspace | None = None


chat_agent = Agent(deps_type=ChatDeps, retries=2)


@chat_agent.instructions
def _system_instructions(ctx: RunContext[ChatDeps]) -> str:
    deps = ctx.deps
    if deps.mode is None:
        # Relay path (U7): the server-composed per-kind prompt, verbatim.
        return deps.system
    if deps.prompt_context is None:
        raise ValueError(f"mode={deps.mode.value} turn composed without a PromptContext.")
    return compose_mode_prompt(deps.mode, deps.prompt_context, approved_plan=deps.approved_plan)
