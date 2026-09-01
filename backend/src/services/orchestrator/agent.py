"""The module-level build `Agent` (KD-2).

ONE `Agent`, built WITHOUT a bound model — the Foundry model is passed per-run
(`agent.iter(..., model=…)`) so import never needs a configured Foundry (dev/test boot without
it) and tests inject a `FunctionModel`. Mirrors `services/agent/agent.py`. The tool surface is
the `sandbox_toolset` FACTORY from `tools.py` (the generated `WRITE_TOOL_SURFACE`), constructed
here over the `BuildDeps` accessor — the same toolset a Write chat turn builds over its own deps,
so there is exactly one tool body in the tree.
"""

from __future__ import annotations

from pydantic_ai import Agent, RunContext

from src.services.orchestrator.deps import BuildDeps, SandboxSession
from src.services.orchestrator.prompt import BUILD_SYSTEM_PROMPT
from src.services.orchestrator.tools import sandbox_toolset


def _sandbox_of(ctx: RunContext[BuildDeps]) -> SandboxSession:
    """The harness accessor the sandbox toolset resolves its session through."""
    return ctx.deps.sandbox


build_agent = Agent(deps_type=BuildDeps, retries=2, toolsets=[sandbox_toolset(_sandbox_of)])
"""The build agent — no bound model (KD-2), `deps_type=BuildDeps`, per-tool retries=2 so a
`ModelRetry` from a tool (e.g. an enriched str_replace failure, KD-5) is reflected back to the
model in-run before it becomes a hard error."""


@build_agent.instructions
def _build_system_prompt() -> str:
    # The system prompt is FROZEN (not per-request, unlike a conversation turn's), applied via
    # `instructions` so it stays out of any persisted message history.
    return BUILD_SYSTEM_PROMPT
