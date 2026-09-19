"""The Pydantic AI chat agent.

ONE module-level `Agent`, built without a bound model — the Foundry model is passed per-run
(`model=…`) so import never depends on a configured Foundry, and tests inject a `TestModel`.
`ChatDeps` is built per request and scopes any tool to the caller's `user_id` (a dropped scope
predicate is a cross-user leak).

The per-run system prompt has two sources, selected by `deps.kind` (U9/D4):

- `kind is None` — a server-composed prompt applied verbatim. Its last caller,
  `services/projects/describe.py` (`POST /{project_id}/description:generate`), was deleted in
  #191 along with the rest of the Generate Description feature — as of that change nothing
  constructs `ChatDeps` with `kind=None`. The branch is left in place rather than pulled with
  its caller (removing it is a separate cleanup, not part of #191's stated scope).
- `kind` set — a turn on the turn engine (which always sets it), in TWO pieces: the kind's
  standing contract, passed per run as static parts by `static_instruction_parts`, and this
  conversation's own facts, returned by the callable below from `deps.prompt_context`. BOTH
  kinds: a Build turn is an ordinary turn with more tools, so it composes here like a Plan turn
  and carries a `SandboxSession` in `deps.sandbox`.

Either way the text is applied through `instructions`, NOT `system_prompt`: instructions are
never baked into stored message history, so prompts evolve without rewriting history — the
same boundary that keeps ephemeral reminders out of the DB.
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
    """Per-request agent dependencies. `user_id` scopes any tool to the caller. `system` is
    the kindless run's prompt (`describe.py`); a turn-engine run sets `kind` + `prompt_context`
    instead — a kind without context is a fail-first error, never a silent empty prompt. `db`
    is OPTIONAL (no tool reads it) since holding a pooled connection across a minutes-long
    Write turn would pin it idle-in-transaction, what short-lived harness sessions avoid.
    `workspace` and BUILD-only `sandbox` are `None` off their paths; both accessors fail-first
    rather than degrade.
    """

    user_id: uuid.UUID
    db: AsyncSession | None = None
    system: str = ""
    kind: ChatKind | None = None
    prompt_context: PromptContext | None = None
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
    """This conversation's own facts — the dynamic tail, sorted behind every static part.

    A kindless run is the exception: it has no contract to stand on, so its whole prompt is
    this one dynamic part."""
    deps = ctx.deps
    if deps.kind is None:
        # Server-composed prompt, verbatim (the `describe.py` one-shot path).
        return deps.system
    if deps.prompt_context is None:
        raise ValueError(f"kind={deps.kind.value} turn composed without a PromptContext.")
    return this_conversation(deps.prompt_context)
