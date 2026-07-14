"""The module-level build `Agent` (KD-2).

ONE `Agent`, built WITHOUT a bound model — the Foundry model is passed per-run
(`agent.iter(..., model=…)`) so import never needs a configured Foundry (dev/test boot without
it) and tests inject a `FunctionModel`. Mirrors `services/agent/agent.py`. The five tools live in
`tools.py`; importing this package registers them (see `__init__.py`).
"""

from __future__ import annotations

from pydantic_ai import Agent

from src.services.orchestrator.deps import BuildDeps
from src.services.orchestrator.prompt import BUILD_SYSTEM_PROMPT

build_agent = Agent(deps_type=BuildDeps, retries=2)
"""The build agent — no bound model (KD-2), `deps_type=BuildDeps`, per-tool retries=2 so a
`ModelRetry` from a tool (e.g. an enriched str_replace failure, KD-5) is reflected back to the
model in-run before it becomes a hard error."""


@build_agent.instructions
def _build_system_prompt() -> str:
    # The system prompt is FROZEN (not per-request, unlike the chat relay), applied via
    # `instructions` so it stays out of any persisted message history.
    return BUILD_SYSTEM_PROMPT
