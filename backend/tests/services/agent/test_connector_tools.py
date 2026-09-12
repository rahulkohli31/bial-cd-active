"""`connector_schema` — what the agent gets, and what it gets instead when it cannot have it.

WHAT THIS FILE IS ABOUT. Registration is the gate (`test_toolsets.py` proves that); this file is
about the four things the BODY does, three of which are refusals that registration makes
unreachable. They are asserted rather than assumed, because "unreachable" is a property of today's
call graph and the refusal is what stands if that changes.

THE DIVIDING LINE BETWEEN THE TWO REFUSAL SHAPES IS WHO CAN FIX IT. A `ModelRetry` teaches the
model something it can act on — call again with a name from your instructions. A plain returned
string is the truth about something the model cannot change: an access decision, or a file that is
not in the image. Getting that backwards costs a citizen a round trip discovering a packaging
break, or leaves the model believing a refusal was final when a better argument would have worked.

THE DEPS SHAPE IS LOAD-BEARING. This is the first toolset in the registry that reads `ctx.deps`,
so a test that exercises the body has to build `ChatDeps` — the only shape carrying
`prompt_context`. The surface-listing tests next door use `ReadDeps`/`ToolDeps`, which carry none,
and a run over those deps hits the no-context refusal rather than the answer.
"""

from __future__ import annotations

import uuid
from pathlib import Path
from typing import Any

import pytest
from pydantic_ai import Agent, ModelRetry, RunContext
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.usage import RunUsage
from structlog.testing import capture_logs

from src.db.models.conversation import ChatKind
from src.services.agent.agent import ChatDeps
from src.services.agent.connector_tools import (
    _CATALOGUE,
    _UNAVAILABLE,
    CONNECTOR_TOOLSET,
    MAX_DELIVERIES_PER_CONVERSATION,
    connector_schema,
)
from src.services.agent.mode_prompts import PromptContext
from src.services.messages.projection import CONNECTOR_SCHEMA_TOOL
from tests.fakes import a_connected_system
from tests.services.orchestrator.model_harness import text_turn

SYSTEM = a_connected_system()
SHIPPED = (_CATALOGUE / f"{SYSTEM.key}.txt").read_text(encoding="utf-8")


def _ctx(
    *,
    connected: tuple[Any, ...] | None = (),
    db: Any = None,
    messages: list[ModelMessage] | None = None,
) -> RunContext[Any]:
    """A run context over the deps shape a real turn builds.

    `connected=None` builds the KINDLESS run — `describe.py` composes its own prompt and passes no
    `PromptContext` at all — which is a different absence from "a turn whose project reads
    nothing", and the tool has to survive both. `messages` is the replayed conversation, which the
    tool reads as `ctx.messages`."""
    prompt_context = (
        None
        if connected is None
        else PromptContext(
            user_name="Asha", project_name="Stand board", connected_systems=connected
        )
    )
    deps = ChatDeps(
        user_id=uuid.uuid4(),
        db=db,
        kind=ChatKind.PLAN,
        prompt_context=prompt_context,
    )
    return RunContext(
        deps=deps,
        model=FunctionModel(lambda *_: text_turn("x")),
        usage=RunUsage(),
        messages=messages or [],
    )


# --------------------------------------------------------------------------------------------
# The answer
# --------------------------------------------------------------------------------------------


async def test_a_connected_project_gets_the_shipped_block_byte_for_byte() -> None:
    """No trimming, no capping, no summarising. The block is 14 KB of text every sentence of which
    a claim test holds to the profile, and any transformation on the way out is a claim nothing
    has checked. `_cap_redact_cap` is deliberately not applied: there is no untrusted content here
    and nothing to redact — this file is the platform's own."""
    answer = await connector_schema(_ctx(connected=(SYSTEM,)), SYSTEM.connector.display_name)
    assert answer == SHIPPED
    assert answer.startswith("# GENERATED FILE")


async def test_the_key_and_the_display_name_both_resolve() -> None:
    """The prompt shows the citizen's agent `DICE`; the artefact is named by the stored key. A
    model that reads its instructions passes back the display name, and one that has seen the key
    somewhere passes that. Matching either is parsing at the boundary, not a policy branch."""
    for spelling in (
        SYSTEM.key,
        SYSTEM.key.upper(),
        SYSTEM.connector.display_name,
        SYSTEM.connector.display_name.lower(),
        f"  {SYSTEM.connector.display_name}  ",
    ):
        assert await connector_schema(_ctx(connected=(SYSTEM,)), spelling) == SHIPPED


async def test_the_database_is_never_touched() -> None:
    """★ `ChatDeps.db` IS `None` ON THE BUILD ARM — a Build turn runs for minutes and must not pin
    a pooled connection idle-in-transaction — while the Plan arm passes a live session. A tool that
    needed the database would therefore work on Plan and fail on Build, which is the shape of a
    defect nobody finds until a citizen's build stops mid-turn. Answering identically with `db` at
    `None` is the property that makes one implementation serve both arms."""
    assert await connector_schema(_ctx(connected=(SYSTEM,), db=None), SYSTEM.key) == SHIPPED


# --------------------------------------------------------------------------------------------
# The refusals — the retryable one
# --------------------------------------------------------------------------------------------


async def test_an_unknown_name_is_a_teaching_retry_that_says_what_is_connected() -> None:
    """The model can fix this by calling again, so it is the retryable kind — and the refusal
    names what IS connected rather than only what is not, because "no" without "here is what you
    can have" costs another round trip to discover."""
    with pytest.raises(ModelRetry) as raised:
        await connector_schema(_ctx(connected=(SYSTEM,)), "SAP")
    message = str(raised.value)
    assert "SAP" in message
    assert SYSTEM.connector.display_name in message
    assert "not a system this project can read" in message


# --------------------------------------------------------------------------------------------
# The refusals — the ones the model cannot fix, which must NOT be retried
# --------------------------------------------------------------------------------------------


async def test_a_kindless_run_with_no_prompt_context_is_refused_not_raised() -> None:
    """`describe.py` composes its own prompt and passes no `PromptContext`. It registers no
    toolsets either, so this is unreachable — but a tool that RAISED on a shape the platform
    itself creates would be a 500 in a citizen's turn for something the citizen did not do."""
    answer = await connector_schema(_ctx(connected=None), SYSTEM.key)
    assert "no connected systems resolved" in answer


async def test_a_turn_whose_project_reads_nothing_is_refused_the_same_way() -> None:
    """The ordinary project, where `connected_systems` is empty. Distinct from the kindless run
    above and asserted separately: a `PromptContext` exists here, it just carries nothing."""
    answer = await connector_schema(_ctx(connected=()), SYSTEM.key)
    assert "no connected systems resolved" in answer


async def test_a_system_that_is_not_effectively_on_is_refused_without_the_schema() -> None:
    """★ THE BELT TO REGISTRATION'S BRACES. `connected_systems_for_project` returns only systems
    that are effectively on, so this state cannot arrive from a router — which is exactly why it
    is constructed here rather than trusted. NOT retryable: whether a project may read a system is
    an administrator's decision, and no better argument reaches it."""
    off = a_connected_system(effectively_on=False)
    answer = await connector_schema(_ctx(connected=(off,)), off.key)
    assert answer != SHIPPED
    assert "not switched on for this project" in answer
    assert "GENERATED FILE" not in answer


async def test_a_missing_artefact_tells_the_model_to_stop_and_logs_the_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★ THE ONE ERROR CLASS THE MODEL CANNOT CAUSE, and the only one where the right answer is
    "stop", not "try again".

    A missing artefact is a packaging break — the file did not make it into the image — so
    retrying spends the citizen's turn discovering it twice. What the model is told instead is the
    thing that matters: do not guess column names. What the SERVER is told is the path, because
    that is the one fact a human needs and the one the model has no use for.

    The loader is `@cache`d, so the cache is cleared around this test: a poisoned entry would
    otherwise make every later test in the process read from nowhere."""
    from src.services.agent import connector_tools

    connector_tools._load.cache_clear()
    monkeypatch.setattr(connector_tools, "_CATALOGUE", Path("/nonexistent/catalogue"))
    try:
        # `capture_logs`, not `caplog`: the app configures structlog with its own renderer and
        # a filtering bound logger, so these events never reach the stdlib `logging` tree that
        # `caplog` reads — a `caplog` assertion here would be permanently, silently empty.
        with capture_logs() as captured:
            answer = await connector_schema(_ctx(connected=(SYSTEM,)), SYSTEM.key)
    finally:
        connector_tools._load.cache_clear()

    assert "not available on this server" in answer
    assert "Do not guess column names" in answer
    assert "/nonexistent/catalogue" not in answer, "the path must not reach the model"
    errors = [e for e in captured if e["event"] == "connector_catalogue_unreadable"]
    assert len(errors) == 1
    assert errors[0]["log_level"] == "error"
    assert errors[0]["path"] == "/nonexistent/catalogue/dice.txt"
    assert errors[0]["connector_key"] == SYSTEM.key


# --------------------------------------------------------------------------------------------
# What the model is told the tool IS — the docstring is prompt copy, sent on every request
# --------------------------------------------------------------------------------------------


async def _registered_description() -> str:
    """The tool description as pydantic-ai hands it to the model, off the REGISTERED definition
    rather than off `__doc__` — the two can differ, and only one of them reaches the model."""
    ctx: RunContext[Any] = RunContext(
        deps=None, model=FunctionModel(lambda *_: text_turn("x")), usage=RunUsage()
    )
    tools = await CONNECTOR_TOOLSET.get_tools(ctx)
    return tools[CONNECTOR_SCHEMA_TOOL].tool_def.description or ""


async def test_the_toolset_registers_exactly_one_tool_under_the_shared_name() -> None:
    """One tool, not two. The second — a per-column value lookup — was retired when the column cut
    left the block able to answer for every column it can, so a `connector_column_values`
    appearing here again is a design decision, not an addition."""
    ctx: RunContext[Any] = RunContext(
        deps=None, model=FunctionModel(lambda *_: text_turn("x")), usage=RunUsage()
    )
    assert set(await CONNECTOR_TOOLSET.get_tools(ctx)) == {CONNECTOR_SCHEMA_TOOL}


async def test_the_description_tells_the_model_when_to_call_it_and_what_not_to_copy() -> None:
    """★ THE THREE THINGS THIS COPY HAS TO DO, and the third is a disclosure rule.

    A deployed app is listed org-wide, which is a wider audience than the one person whose access
    an administrator approved — so KPI formulas and SLA targets are for writing the query with,
    never for reproducing in source or on screen."""
    description = await _registered_description()
    lowered = description.lower()
    # What it gives.
    assert "every column with its type" in lowered
    # When to call it — including the iteration case, which is the realistic failure: the files
    # hold column names, so the cheap path is to imitate rather than fetch.
    assert "before writing any code that reads the connected data" in lowered
    assert "already reads it" in lowered
    # What must not leave the block.
    assert "kpi formulas" in lowered
    assert "sla targets" in lowered
    assert "source" in lowered


# --------------------------------------------------------------------------------------------
# The registered surface, driven through a real agent run
# --------------------------------------------------------------------------------------------


async def test_the_model_is_handed_the_tool_and_its_description_together() -> None:
    """Listed the way `test_toolsets.py` lists a surface — through `AgentInfo.function_tools` on a
    real run — so this asserts what the MODEL sees rather than what the registry holds."""
    seen: dict[str, Any] = {}

    def respond(_messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen["tools"] = {tool.name: tool.description or "" for tool in info.function_tools}
        return text_turn("noted")

    agent: Agent[ChatDeps, str] = Agent(deps_type=ChatDeps)
    await agent.run(
        "what data can I read?",
        deps=_ctx(connected=(SYSTEM,)).deps,
        model=FunctionModel(respond),
        toolsets=[CONNECTOR_TOOLSET],
    )
    assert set(seen["tools"]) == {CONNECTOR_SCHEMA_TOOL}
    assert seen["tools"][CONNECTOR_SCHEMA_TOOL].strip()


async def test_the_tool_names_no_connector_in_its_own_source() -> None:
    """R9's rule, checked where it is easiest to break. `tests/test_connector_naming_is_generic.py`
    already walks all of `backend/src`, so this is not the enforcement — it is the note that says
    why the artefact is `f"{key}.txt"` and why the refusals interpolate `display_name` rather than
    spelling a system out."""
    source = Path(connector_schema.__code__.co_filename).read_text(encoding="utf-8")
    assert SYSTEM.key not in source.lower().replace("connector_", "")


# --------------------------------------------------------------------------------------------
# The ceiling — the conversation's own history is the state
# --------------------------------------------------------------------------------------------


def _conversation_that_already_holds(*results: str) -> list[ModelMessage]:
    """A replayed conversation in which each of `results` came back from a `connector_schema`
    call, shaped the way `load_history` hands it over: a user turn, the call, its return, and the
    model's answer."""
    history: list[ModelMessage] = []
    for n, content in enumerate(results):
        call_id = f"schema-{n}"
        history += [
            ModelRequest(parts=[UserPromptPart(content=f"turn {n}")]),
            ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name=CONNECTOR_SCHEMA_TOOL,
                        args={"system": SYSTEM.key},
                        tool_call_id=call_id,
                    )
                ]
            ),
            ModelRequest(
                parts=[
                    ToolReturnPart(
                        tool_name=CONNECTOR_SCHEMA_TOOL, content=content, tool_call_id=call_id
                    )
                ]
            ),
            ModelResponse(parts=[TextPart(content="noted")]),
        ]
    return history


async def test_one_earlier_delivery_does_not_trip_the_ceiling() -> None:
    """The ceiling is two, not one — owner ruling, 2026-09-11. A second full copy is allowed."""
    history = _conversation_that_already_holds(SHIPPED)
    answer = await connector_schema(_ctx(connected=(SYSTEM,), messages=history), SYSTEM.key)
    assert answer == SHIPPED


async def test_past_the_ceiling_the_model_is_pointed_back_at_the_copy_it_has() -> None:
    """★ THE CASE THE CEILING EXISTS FOR. A plain string, not a `ModelRetry` — calling again is the
    one thing the model should not do — and a WARNING beside it, because a model asking again for
    something its context already holds twice is the clearest sign that the history this turn
    replayed did not reach it the way the list says."""
    history = _conversation_that_already_holds(*[SHIPPED] * MAX_DELIVERIES_PER_CONVERSATION)
    with capture_logs() as captured:
        answer = await connector_schema(_ctx(connected=(SYSTEM,), messages=history), SYSTEM.key)
    assert answer != SHIPPED
    assert "GENERATED FILE" not in answer
    assert "already in this conversation" in answer
    warnings = [e for e in captured if e["event"] == "connector_schema_already_delivered"]
    assert len(warnings) == 1
    assert warnings[0]["log_level"] == "warning"
    assert warnings[0]["deliveries"] == MAX_DELIVERIES_PER_CONVERSATION
    assert warnings[0]["connector_key"] == SYSTEM.key


async def test_a_refusal_is_not_a_delivery() -> None:
    """Counting `connector_schema` RESULTS rather than deliveries would spend the ceiling on calls
    that handed over nothing — the unreadable-file answer and the not-switched-on refusal come
    back under the same tool name."""
    refusals = [_UNAVAILABLE, f"{SYSTEM.connector.display_name} is not switched on here."] * 3
    history = _conversation_that_already_holds(*refusals)
    answer = await connector_schema(_ctx(connected=(SYSTEM,), messages=history), SYSTEM.key)
    assert answer == SHIPPED


@pytest.mark.parametrize(
    ("earlier", "full_schema_again"),
    [
        (0, True),
        (MAX_DELIVERIES_PER_CONVERSATION - 1, True),
        (MAX_DELIVERIES_PER_CONVERSATION, False),
    ],
)
async def test_the_ceiling_reads_the_history_a_real_run_replays(
    earlier: int, full_schema_again: bool
) -> None:
    """★ THE MECHANISM, NOT A HAND-BUILT CONTEXT. The ceiling rests on pydantic-ai handing a tool
    the same history the turn passed in as `message_history` — if that stopped being true the
    ceiling would silently never fire and every test above would still pass. So the history goes
    in exactly as the engine passes it, and the answer is read off what the model got back."""
    received: dict[str, Any] = {}

    def respond(messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        last = messages[-1]
        returned = (
            [part for part in last.parts if isinstance(part, ToolReturnPart)]
            if isinstance(last, ModelRequest)
            else []
        )
        if returned:
            received["answer"] = returned[0].content
            return text_turn("done")
        return ModelResponse(
            parts=[
                ToolCallPart(
                    tool_name=CONNECTOR_SCHEMA_TOOL,
                    args={"system": SYSTEM.key},
                    tool_call_id="this-turn",
                )
            ]
        )

    agent: Agent[ChatDeps, str] = Agent(deps_type=ChatDeps)
    await agent.run(
        "add a column",
        deps=_ctx(connected=(SYSTEM,)).deps,
        model=FunctionModel(respond),
        toolsets=[CONNECTOR_TOOLSET],
        message_history=_conversation_that_already_holds(*[SHIPPED] * earlier),
    )
    assert (received["answer"] == SHIPPED) is full_schema_again
