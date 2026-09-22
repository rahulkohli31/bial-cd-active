"""The chat agent runs under TestModel with no network, and ChatDeps scopes tools to the
caller's user_id.

It also owns the SPLIT: the kind's standing contract ships as static instruction parts and
this conversation's own facts as the one dynamic part. Every assertion about the split reads
the parts off the BUILT request (`AgentInfo.model_request_parameters`), because that is what
the provider maps into system blocks — a helper's return value says nothing about what left
the process. Whether a cache marker actually lands on those blocks is
`tests/services/turns/test_prefix_stability.py`'s question; this file owns what the agent
gives it to mark.
"""

from __future__ import annotations

import uuid

import pytest
from pydantic_ai import Agent, RunContext
from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel

from src.core.prompt_blocks import NARRATION_EXAMPLES
from src.db.models.conversation import ChatKind
from src.services.agent.agent import ChatDeps, chat_agent, static_instruction_parts
from src.services.agent.mode_prompts import (
    _GENERIC_SEGMENT,
    _PLAN_SEGMENT,
    _WRITE_SEGMENT,
    PromptContext,
    compose_kind_prompt,
    standing_contract,
)

_ADA = PromptContext(
    user_name="Ada",
    project_name="Visitors",
    project_description="Counts visitors at the east gate.",
)
_BO = PromptContext(user_name="Bo", project_name="Stand board")

_CONTRACT_TAIL = {
    ChatKind.PLAN: _PLAN_SEGMENT,
    ChatKind.BUILD: _WRITE_SEGMENT,
    ChatKind.GENERIC: _GENERIC_SEGMENT,
}


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


# --- the split: a static contract and one dynamic tail --------------------------------------


async def _one_run(kind: ChatKind, context: PromptContext, db_session) -> AgentInfo:
    """One turn-shaped run, returning what the model was handed.

    `instructions=` mirrors `turns/engine.py` exactly. Composing the contract inside the test
    instead would be a test of this test: the defect being guarded against is a CALL SITE that
    passes no static part, and a run that builds its own could never show it."""
    seen: list[AgentInfo] = []

    def respond(_messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(info)
        return ModelResponse(parts=[TextPart(content="ok")])

    await chat_agent.run(
        "what should we build?",
        deps=ChatDeps(db=db_session, user_id=uuid.uuid7(), kind=kind, prompt_context=context),
        model=FunctionModel(respond),
        instructions=static_instruction_parts(kind),
    )
    assert seen, "the model was never called — no request to read the parts off"
    return seen[0]


def _blocks(info: AgentInfo) -> tuple[list[str], list[str]]:
    """(static contents, dynamic contents), in the order the request carries them."""
    parts = info.model_request_parameters.instruction_parts or []
    return (
        [part.content for part in parts if not part.dynamic],
        [part.content for part in parts if part.dynamic],
    )


@pytest.mark.parametrize("kind", list(ChatKind), ids=[k.value for k in ChatKind])
async def test_the_standing_contract_reaches_the_request_as_static_parts(kind, db_session) -> None:
    """★ The property `anthropic_cache_instructions` needs and never had: at least one part the
    library is told is static. With every part dynamic the library resolves its target block to
    `None` and marks nothing at all.

    Mutation check: flip `dynamic=False` to `True` in `static_instruction_parts` and the static
    list empties."""
    static, dynamic = _blocks(await _one_run(kind, _ADA, db_session))
    assert static == list(standing_contract(kind))
    assert len(dynamic) == 1, f"expected one per-conversation part, got {len(dynamic)}"


@pytest.mark.parametrize("kind", list(ChatKind), ids=[k.value for k in ChatKind])
async def test_the_examples_lead_and_the_kind_contract_closes_the_static_run(kind, db_session):
    """Position, asserted on the built request rather than on the composer.

    The examples keep first place — the one thing the prompt's own authoring rule forbids
    moving — and the kind's contract segment is last among the statics, which is where the
    marker lands. Both ends are pinned against the shared constants, so a block edited at its
    source moves with them and a block REORDERED does not."""
    static, _ = _blocks(await _one_run(kind, _ADA, db_session))
    assert static[0] == NARRATION_EXAMPLES
    assert static[-1] == _CONTRACT_TAIL[kind]


async def test_the_static_parts_are_byte_identical_for_two_different_citizens(db_session) -> None:
    """★ THE PROPERTY A CACHED PREFIX RESTS ON. A marker on a block that still carries a
    citizen's name caches nothing across two of them.

    The inequality below is the liveness half: without it this passes just as well when the
    per-conversation part has quietly become empty."""
    ada_static, ada_dynamic = _blocks(await _one_run(ChatKind.PLAN, _ADA, db_session))
    bo_static, bo_dynamic = _blocks(await _one_run(ChatKind.PLAN, _BO, db_session))

    assert ada_static == bo_static
    assert ada_dynamic != bo_dynamic
    for block in ada_static:
        assert "Ada" not in block and "Visitors" not in block


@pytest.mark.parametrize("kind", list(ChatKind), ids=[k.value for k in ChatKind])
async def test_the_identity_sentence_opens_the_per_conversation_tail(kind, db_session) -> None:
    """It moved to the tail, and it is still there — the sentence that tells the model who it is
    working with and on what, now behind the whole standing contract instead of two blocks in."""
    _, dynamic = _blocks(await _one_run(kind, _ADA, db_session))
    assert dynamic[0].startswith(
        'You are the Citizen Developer assistant for BIAL, working with Ada on "Visitors" — '
        "Counts visitors at the east gate."
    )


@pytest.mark.parametrize("kind", list(ChatKind), ids=[k.value for k in ChatKind])
async def test_compose_kind_prompt_renders_exactly_what_the_run_sends(kind, db_session) -> None:
    """★ THE EQUALITY THE PROMPT-SURFACE TESTS REST ON. `compose_kind_prompt` is what the drift
    check and `test_mode_prompts.py` read; the run sends parts. If the two ever disagree, every
    assertion made against the composed string is an assertion about a prompt nobody receives.

    Mutation check: reverse the tuple `standing_contract` returns and this goes red while every
    `in`-style assertion in `test_mode_prompts.py` stays green."""
    info = await _one_run(kind, _ADA, db_session)
    assert info.instructions == compose_kind_prompt(kind, _ADA)


async def test_a_kindless_run_still_ships_its_prompt_verbatim(db_session) -> None:
    """The `describe.py` one-shot path has no contract to stand on, so its whole prompt is the
    single dynamic part — and no static part is invented for it."""
    seen: list[AgentInfo] = []

    def respond(_messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(info)
        return ModelResponse(parts=[TextPart(content="ok")])

    await chat_agent.run(
        "hi",
        deps=ChatDeps(db=db_session, user_id=uuid.uuid7(), system="RELAY-PROMPT"),
        model=FunctionModel(respond),
    )
    static, dynamic = _blocks(seen[0])
    assert static == []
    assert dynamic == ["RELAY-PROMPT"]
