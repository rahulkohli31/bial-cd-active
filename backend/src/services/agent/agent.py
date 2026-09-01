"""The Pydantic AI chat agent (R10, R11, U9).

ONE module-level `Agent`, built without a bound model — the Foundry model is passed per-run
(`agent.run_stream(prompt, model=…)`) so import never depends on a configured Foundry (dev/test
boot without it) and tests inject a `TestModel`. `ChatDeps` is built per request and scopes any
tool to the caller's `user_id` (a dropped scope predicate is a cross-user leak).

The per-run system prompt has two sources, selected by `deps.kind` (U9/D4):

- `kind is None` — the relay path (U7): the server composed `deps.system` and it is applied
  verbatim. TODO: retires with the relay once the turn engine owns all traffic.
- `kind` set — a turn on the turn engine (which always sets it): BASE + that kind's segment
  composed by `mode_prompts.compose_kind_prompt` from `deps.prompt_context`. BOTH kinds: a
  Build turn is an ordinary turn with more tools (U5's convergence), so it composes here like
  a Plan turn and carries a `SandboxSession` in `deps.sandbox`.

Either way the text is applied through `instructions`, NOT `system_prompt`: instructions are
never baked into stored message history, so prompts evolve without rewriting history — the
same boundary that keeps U14's ephemeral reminders out of the DB.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from pydantic_ai import Agent, RunContext
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models.conversation import ChatKind
from src.services.agent.mode_prompts import PromptContext, compose_kind_prompt
from src.services.agent.read_tools import ReadOnlyWorkspace
from src.services.orchestrator.deps import SandboxSession


@dataclass
class ChatDeps:
    """Per-request agent dependencies. `user_id` scopes any tool to the caller.

    `system` is the relay path's server-composed prompt (kind None). A turn on the turn
    engine sets `kind` + `prompt_context` instead. Setting a kind without its context is a
    programming error, caught fail-first at instruction time (never a silently empty
    prompt).

    `db` is OPTIONAL because no tool reads it (the relay and describe paths pass one only
    because they already hold it). A Write turn runs for minutes, and holding a pooled
    connection open across a model call would pin it idle-in-transaction for the whole
    build — the exact thing the build harness opens short-lived sessions to avoid.

    `workspace` is the turn-pinned read surface a run's toolsets resolve through (the turn
    engine sets it; the relay path never does — its runs carry no tools). `sandbox` is set on
    a BUILD turn only — the live session the eight sandbox tools act through. Both are `None`
    off their paths, and both accessors fail-first rather than degrade.
    """

    user_id: uuid.UUID
    db: AsyncSession | None = None
    system: str = ""
    kind: ChatKind | None = None
    prompt_context: PromptContext | None = None
    workspace: ReadOnlyWorkspace | None = None
    sandbox: SandboxSession | None = None


chat_agent = Agent(deps_type=ChatDeps, retries=2)


@chat_agent.instructions
def _system_instructions(ctx: RunContext[ChatDeps]) -> str:
    deps = ctx.deps
    if deps.kind is None:
        # Relay path (U7): the server-composed prompt, verbatim.
        return deps.system
    if deps.prompt_context is None:
        raise ValueError(f"kind={deps.kind.value} turn composed without a PromptContext.")
    return compose_kind_prompt(deps.kind, deps.prompt_context)
