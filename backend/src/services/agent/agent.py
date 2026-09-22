"""The Pydantic AI chat agent.

ONE module-level `Agent`, built without a bound model — the Foundry model is passed per-run
(`model=…`) so import never depends on a configured Foundry, and tests inject a `TestModel`.
`ChatDeps` is built per request and scopes any tool to the caller's `user_id` (a dropped scope
predicate is a cross-user leak).

Every run carries a `kind`, and its prompt is built in TWO pieces: the kind's standing contract,
passed per run as static instruction parts by `static_instruction_parts`, and this conversation's
own facts, returned by the callable below from `deps.prompt_context`. Both fields are required —
there is no shape that skips either. All three kinds take this path: a Build turn is an ordinary
turn with more tools, so it composes here like a Plan or a BIAL Chat turn and carries a
`SandboxSession` in `deps.sandbox`.

The text is applied through `instructions`, NOT `system_prompt`: instructions are never baked into
stored message history, so prompts evolve without rewriting history — the same boundary that keeps
ephemeral reminders out of the DB.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

from pydantic_ai import Agent, RunContext
from pydantic_ai.messages import InstructionPart
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models.conversation import ChatKind
from src.services.agent.mode_prompts import (
    PromptContext,
    standing_contract,
    this_conversation,
)
from src.services.agent.read_tools import ReadOnlyWorkspace
from src.services.orchestrator.deps import SandboxSession


@dataclass
class ChatDeps:
    """Per-request agent dependencies. `user_id` scopes any tool to the caller. `kind` and
    `prompt_context` are required: every run composes its prompt from the kind's standing
    contract plus this conversation's own facts, and there is no shape that omits either. `db`
    is OPTIONAL (no tool reads it) since holding a pooled connection across a minutes-long
    Write turn would pin it idle-in-transaction, what short-lived harness sessions avoid.
    `workspace` and BUILD-only `sandbox` are `None` off their paths; both accessors fail-first
    rather than degrade.
    """

    user_id: uuid.UUID
    kind: ChatKind
    prompt_context: PromptContext
    db: AsyncSession | None = None
    workspace: ReadOnlyWorkspace | None = None
    sandbox: SandboxSession | None = None


chat_agent = Agent(deps_type=ChatDeps, retries=2)


def static_instruction_parts(kind: ChatKind) -> list[InstructionPart]:
    """The kind's standing contract, as the run's STATIC instruction parts.

    STATIC IS THE WHOLE POINT, and it is why this is not folded into the callable below.
    `anthropic_cache_instructions` places its breakpoint after the LAST part the library was
    told is static and resolves it to nothing when every part is dynamic — which is exactly
    what a lone `@agent.instructions` callable produces, so the setting placed no marker at all.
    An `InstructionPart(dynamic=False)` is honoured as static, so the contract earns the marker
    and this conversation's own facts stay behind it.

    Passed per run rather than to `Agent(instructions=…)` because the contract follows the
    kind, and there is one agent."""
    return [InstructionPart(content=block, dynamic=False) for block in standing_contract(kind)]


@chat_agent.instructions
def _system_instructions(ctx: RunContext[ChatDeps]) -> str:
    """This conversation's own facts — the dynamic tail, sorted behind every static part."""
    return this_conversation(ctx.deps.prompt_context)
