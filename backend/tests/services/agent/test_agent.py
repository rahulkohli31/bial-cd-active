"""The chat agent runs under TestModel with no network, and ChatDeps scopes tools to the
caller's user_id (U12)."""

from __future__ import annotations

import uuid

from pydantic_ai import Agent, RunContext
from pydantic_ai.models.test import TestModel

from src.services.agent.agent import ChatDeps, chat_agent


async def test_agent_runs_under_test_model(db_session) -> None:
    # No live call (conftest sets ALLOW_MODEL_REQUESTS=False); the model is injected per-run.
    result = await chat_agent.run(
        "hello",
        deps=ChatDeps(db=db_session, user_id=uuid.uuid7(), system="be terse"),
        model=TestModel(custom_output_text="hi there"),
    )
    assert result.output == "hi there"


async def test_deps_scope_a_tool_to_the_caller(db_session) -> None:
    # The user-scope pattern: a tool reads ctx.deps.user_id (never a client-supplied id).
    seen: dict[str, uuid.UUID] = {}
    scoped = Agent(deps_type=ChatDeps)

    @scoped.tool
    async def whoami(ctx: RunContext[ChatDeps]) -> str:
        seen["user_id"] = ctx.deps.user_id
        return str(ctx.deps.user_id)

    caller = uuid.uuid7()
    # TestModel calls each available tool once, so the tool observes the scoped deps.
    await scoped.run(
        "go", deps=ChatDeps(db=db_session, user_id=caller, system=""), model=TestModel()
    )
    assert seen["user_id"] == caller
