"""Orchestrator-test scaffolding.

The autouse `_no_live_model` guard forbids any live model request for the whole package
(mirrors `tests/services/agent/conftest.py`): the `FunctionModel` never hits the network, but an
accidental real call fails loudly instead of billing Foundry.

`build_tool_agent` is the LOCAL driver for the sandbox toolset — see its docstring for why the
driver is local now. The harness fixtures once defined here — `make_orchestrator`,
`make_provider`, `billing_factory` — were deleted along with
`services/orchestrator/harness.py`, and nothing replaced them.
"""

from __future__ import annotations

import pytest
from pydantic_ai import Agent, RunContext, models

from src.services.orchestrator.deps import SandboxSession
from src.services.orchestrator.tools import sandbox_toolset
from tests.fakes import ToolDeps


@pytest.fixture(autouse=True)
def _no_live_model():
    previous = models.ALLOW_MODEL_REQUESTS
    models.ALLOW_MODEL_REQUESTS = False
    yield
    models.ALLOW_MODEL_REQUESTS = previous


def _sandbox_of(ctx: RunContext[ToolDeps]) -> SandboxSession:
    """The accessor the sandbox toolset resolves its session through."""
    return ctx.deps.sandbox


def build_tool_agent() -> Agent[ToolDeps, str]:
    """A LOCAL agent over the sandbox toolset — the driver `tools.py`'s tests run through.

    LOCAL BECAUSE THE MODULE-LEVEL ONE IS GONE, NOT BECAUSE THIS IS A SHORTCUT.
    `orchestrator/agent.py`'s `build_agent` existed only for the standalone build harness, and
    it was deleted with it. The toolset it was built over did not go anywhere: `sandbox_toolset`
    is the SAME factory a live Write chat turn composes its surface from
    (`services/agent/toolsets.py` → `toolsets_for_kind(ChatKind.BUILD, …)`), resolved through
    the same `SandboxSession` accessor shape. So an `Agent` constructed here over that factory
    drives the real tool bodies through the real reflection path — which is what these tests
    were ever asserting about.

    Deliberately NO system prompt: `build_agent` carried `BUILD_SYSTEM_PROMPT` as `instructions`,
    and that prompt is now `mode_prompts.compose_kind_prompt(ChatKind.BUILD, …)`, tested in
    `test_prompt.py`. Nothing in this file's tests reads the instructions, and binding a prompt
    here would invite an assertion about a string this driver, not production, chose.

    `retries=2` is what the deleted `build_agent` was constructed with, and it is load-bearing: a
    `ModelRetry` raised by a tool (an enriched `edit_file` failure, a blocked SQL command) has to
    be reflected back to the model IN-RUN, which is exactly what several of these tests assert
    on. Carried over rather than re-chosen.

    The pattern is `tests/services/agent/test_toolsets.py`'s — a per-test `Agent` over the
    factory under test."""
    return Agent(deps_type=ToolDeps, retries=2, toolsets=[sandbox_toolset(_sandbox_of)])
