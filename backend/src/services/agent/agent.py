"""The Pydantic AI chat agent.

ONE module-level `Agent`, built without a bound model — the Foundry model is passed per-run
(`model=…`) so import never depends on a configured Foundry, and tests inject a `TestModel`.
`ChatDeps` is built per request and scopes any tool to the caller's `user_id` (a dropped scope
predicate is a cross-user leak).

The per-run system prompt has two sources, selected by `deps.kind`:

- `kind is None` — a server-composed prompt applied verbatim. Its last caller,
  `services/projects/describe.py` (`POST /{project_id}/description:generate`), was deleted
  along with the rest of the Generate Description feature — nothing constructs `ChatDeps`
  with `kind=None` today. The branch is left in place rather than pulled with its caller;
  removing it is a separate cleanup.
- `kind` set — a turn on the turn engine (which always sets it): BASE + that kind's segment
  composed by `mode_prompts.compose_kind_prompt` from `deps.prompt_context`. BOTH kinds: a
  Build turn is an ordinary turn with more tools, so it composes here like a Plan turn and
  carries a `SandboxSession` in `deps.sandbox`.

Either way the text is applied through `instructions`, NOT `system_prompt`: instructions are
never baked into stored message history, so prompts evolve without rewriting history.
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


@chat_agent.instructions
def _system_instructions(ctx: RunContext[ChatDeps]) -> str:
    deps = ctx.deps
    if deps.kind is None:
        # Server-composed prompt, verbatim (the `describe.py` one-shot path).
        return deps.system
    if deps.prompt_context is None:
        raise ValueError(f"kind={deps.kind.value} turn composed without a PromptContext.")
    return compose_kind_prompt(deps.kind, deps.prompt_context)
