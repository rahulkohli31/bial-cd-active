"""U3 / R75 / R75a / R76 — the one deliberate way the agent speaks in the middle of its work.

WHY A CHANNEL AT ALL. Prose written in the same response as a tool call does not reach the
user, in either kind (U4). That rule is right — it is what stopped 2,397 words of paths and
commands reaching a citizen — but on its own it leaves a turn that runs for two minutes with
nothing to read for the whole of it. `tell_the_user` is what the agent has instead: bounded by
the platform rather than by a sentence in a prompt, and rendered by the platform rather than by
the model choosing to write a paragraph.

★ THE PROPERTY THE WHOLE DESIGN TURNS ON is that the words sit at the position the CALL
occupies, in both emitters. Live order and reload order are then the same order by
construction rather than by two code paths agreeing to stay in step — and the tests that
matter most in this file are the ones that assert the two orders against each other, not the
ones that assert text arrived.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence

import pytest
import sqlalchemy as sa
from pydantic_ai import ModelRetry, RunContext
from pydantic_ai.messages import (
    FunctionToolCallEvent,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.usage import RunUsage
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.v1.conversations.schemas import StepFrame, TextDeltaFrame
from src.db.models.conversation import ChatKind
from src.db.models.message import Message, MessageEntryKind
from src.services.agent.conversation_tools import tell_the_user
from src.services.messages.projection import (
    UPDATE_MAX_CHARS,
    AssistantTextItem,
    DisplayItem,
    StepItem,
    project_rows,
    update_from_args,
)
from src.services.messages.store import append_batch
from src.services.turns.engine import TurnEngine, _TurnState
from tests.factories import ConversationFactory, ProjectFactory, UserFactory

_APP_WORDS = "Adding the status picker next to your search box now."


def _render_ctx() -> RunContext[None]:
    """A context the tool is handed and never reads.

    `RunContext` needs a model and pydantic-ai will not build one without it; a model that
    raises if it is ever asked for a response keeps "this tool calls nothing" honest rather
    than parking a live client in a test."""

    def _never(_messages: list[ModelMessage], _info: AgentInfo) -> ModelResponse:
        raise AssertionError("the voice channel calls no model")

    return RunContext(deps=None, model=FunctionModel(_never), usage=RunUsage())


def _state(kind: ChatKind) -> _TurnState:
    return _TurnState(
        turn_id=uuid.uuid7(),
        conversation_id=uuid.uuid7(),
        user_id=uuid.uuid7(),
        kind=kind,
    )


def _spoke(update: str, call_id: str = "s1") -> FunctionToolCallEvent:
    return FunctionToolCallEvent(
        part=ToolCallPart(
            tool_name="tell_the_user", args=json.dumps({"update": update}), tool_call_id=call_id
        )
    )


def _called(tool: str, args: str, call_id: str) -> FunctionToolCallEvent:
    return FunctionToolCallEvent(
        part=ToolCallPart(tool_name=tool, args=args, tool_call_id=call_id)
    )


def _live_shape(state: _TurnState) -> list[str]:
    """The live feed reduced to what a reader would see happen, in order.

    Text and steps only: this is the sequence a reload has to reproduce, and comparing shapes
    rather than raw frames keeps the two comparable across the frame types only one side has
    (workspace, compile, preview)."""
    shape: list[str] = []
    for frame in state.ring:
        if isinstance(frame, TextDeltaFrame):
            text = frame.text.strip()
            if text:
                shape.append(f"text:{text}")
        elif isinstance(frame, StepFrame) and frame.phase == "started":
            shape.append(f"step:{frame.item.tool}")
    return shape


def _reload_shape(items: Sequence[DisplayItem]) -> list[str]:
    shape: list[str] = []
    for item in items:
        if isinstance(item, AssistantTextItem):
            shape.append(f"text:{item.text.strip()}")
        elif isinstance(item, StepItem):
            shape.append(f"step:{item.tool}")
    return shape


async def _thread(db: AsyncSession, email: str, kind: ChatKind):
    user = await UserFactory.create(db, email=email)
    project = await ProjectFactory.create(db, user.id)
    conversation = await ConversationFactory.create(db, user.id, project_id=project.id, kind=kind)
    return user, conversation


async def _rows(db: AsyncSession, user, conversation) -> list[Message]:
    return list(
        (
            await db.scalars(
                sa.select(Message)
                .where(Message.conversation_id == conversation.id)
                .order_by(Message.seq)
            )
        ).all()
    )


# --- the bound, which is the platform's and not an instruction's ---------------------------


async def test_an_update_within_the_bound_is_acknowledged() -> None:
    """The model is told the words LANDED. Silence, or an echo, leaves it unsure whether to
    say the same thing again — and a repeated update is the one failure this channel can
    produce on its own."""
    assert (
        await tell_the_user(_render_ctx(), _APP_WORDS)
        == "Shown to the user. Carry on with the work."
    )


async def test_an_update_one_character_over_the_bound_is_refused_with_the_number() -> None:
    """★ R76 — the ceiling is enforced in the tool BODY, where nothing can route around it.

    ONE CHARACTER OVER, not obviously over: an off-by-one in the comparison is the mutation
    this is built to kill, and a 10,000-character fixture would pass against `>=`, `>`, and
    a limit accidentally doubled.

    The refusal NAMES the number and the register, so the retry has somewhere to go — a bare
    "too long" leaves the model guessing at a bound it cannot see."""
    with pytest.raises(ModelRetry) as refused:
        await tell_the_user(_render_ctx(), "x" * (UPDATE_MAX_CHARS + 1))
    assert str(UPDATE_MAX_CHARS) in str(refused.value)
    assert str(UPDATE_MAX_CHARS + 1) in str(refused.value)

    # And exactly at the bound is allowed — the other half of the off-by-one.
    assert await tell_the_user(_render_ctx(), "x" * UPDATE_MAX_CHARS)


async def test_an_empty_update_is_refused_rather_than_shown_as_nothing() -> None:
    with pytest.raises(ModelRetry):
        await tell_the_user(_render_ctx(), "   ")


def test_one_rule_decides_what_is_shown_and_both_emitters_read_it() -> None:
    """★ THE SINGLE SOURCE, asserted directly. `update_from_args` is what both emitters call,
    so a refused update renders nowhere without either of them holding its own copy of the
    ceiling. Its answers must line up with the tool body's, or a call the body refused could
    still paint text on a reloaded transcript."""
    assert update_from_args({"update": _APP_WORDS}) == _APP_WORDS
    assert update_from_args(json.dumps({"update": _APP_WORDS})) == _APP_WORDS
    assert update_from_args({"update": "x" * (UPDATE_MAX_CHARS + 1)}) is None
    assert update_from_args({"update": "  "}) is None
    assert update_from_args({"update": 7}) is None
    assert update_from_args("not json at all") is None
    assert update_from_args(None) is None


# --- live and reload, at the same position -------------------------------------------------


@pytest.mark.parametrize("kind", list(ChatKind))
def test_a_spoken_line_reaches_the_live_feed_in_either_kind(kind: ChatKind) -> None:
    """R75 — the channel is on both arms, and behaves identically on both."""
    engine = TurnEngine()
    state = _state(kind)

    engine._on_event(state, _spoke(_APP_WORDS))

    assert _APP_WORDS in "".join(state.text_parts)


@pytest.mark.parametrize("kind", list(ChatKind))
def test_speaking_is_not_a_step_and_never_shows_its_wire_name(kind: ChatKind) -> None:
    """★ The transcript shows what was SAID, never a row announcing that the agent decided to
    say it — and never `Used tell_the_user`, which is what `_step_label`'s fallback prints for
    any tool it does not know. That fallback is the live emitter's too, so a tool without an
    explicit branch leaks its wire name into a citizen's feed on both sides."""
    engine = TurnEngine()
    state = _state(kind)

    engine._on_event(state, _spoke(_APP_WORDS))

    assert _live_shape(state) == [f"text:{_APP_WORDS}"]
    assert state.steps == {}
    assert "tell_the_user" not in "".join(state.text_parts)


@pytest.mark.parametrize("kind", list(ChatKind))
def test_an_over_long_update_reaches_the_live_feed_nowhere(kind: ChatKind) -> None:
    """The refusal is structural on the live side too: the emitter asks the same function the
    body asks, so there is no window in which over-long text is painted and then retracted."""
    engine = TurnEngine()
    state = _state(kind)

    engine._on_event(state, _spoke("x" * (UPDATE_MAX_CHARS + 1)))

    assert state.text_parts == []
    assert state.steps == {}


@pytest.mark.parametrize("kind", list(ChatKind), ids=[k.value for k in ChatKind])
async def test_live_order_and_reload_order_are_the_same_order(
    db_session: AsyncSession, kind: ChatKind
) -> None:
    """★★ THE SCENARIO THIS DESIGN EXISTS FOR (R75a / R76), and the one that would have caught
    the shape it replaces.

    A turn that reads, speaks, reads again and speaks again must produce a reloaded transcript
    whose ITEM ORDER equals the live FRAME ORDER — step, text, step, text — not merely one
    containing the same words. Rendering the spoken line at the tool RESULT event would pass a
    "the text is there" assertion and fail this one: tool bodies run concurrently and their
    results arrive in completion order, while the projection renders in part order.

    Both emitters read the stored CALL, at the position the call occupies. That is the
    `present_plan_options` shape, and it is why the two orders cannot disagree."""
    user, conversation = await _thread(db_session, f"vc-{kind.value}@rvaiglobal.com", kind)
    engine = TurnEngine()
    state = _state(kind)

    first, second = "Looking at how your visitor list works today.", _APP_WORDS
    events = [
        _called("read_file", '{"path": "app/page.tsx"}', "r1"),
        _spoke(first, "s1"),
        _called("read_file", '{"path": "app/list.tsx"}', "r2"),
        _spoke(second, "s2"),
    ]
    for event in events:
        engine._on_event(state, event)

    # The same response, persisted the way the run would persist it.
    await append_batch(
        db_session,
        user_id=user.id,
        conversation_id=conversation.id,
        messages=[
            ModelResponse(parts=[event.part for event in events]),
            ModelRequest(
                parts=[
                    ToolReturnPart(tool_name="read_file", content="1\tx", tool_call_id="r1"),
                    ToolReturnPart(tool_name="tell_the_user", content="ok", tool_call_id="s1"),
                    ToolReturnPart(tool_name="read_file", content="1\ty", tool_call_id="r2"),
                    ToolReturnPart(tool_name="tell_the_user", content="ok", tool_call_id="s2"),
                ]
            ),
        ],
        entry_kind=MessageEntryKind.TURN,
        kind=kind,
    )
    reloaded = _reload_shape(project_rows(await _rows(db_session, user, conversation)))

    expected = [
        "step:read_file",
        f"text:{first}",
        "step:read_file",
        f"text:{second}",
    ]
    assert _live_shape(state) == expected
    assert reloaded == expected


async def test_a_refused_update_renders_nothing_on_reload_either(
    db_session: AsyncSession,
) -> None:
    """The reload half of the refusal, and it does NOT consult the stored tool return.

    Reading the return would have been the obvious way to tell a refused call from an accepted
    one — and it answers the wrong question: a turn cut short before the return landed still
    SAID the words, and the citizen saw them. The argument is what decides, on both sides."""
    user, conversation = await _thread(db_session, "vc-refused@rvaiglobal.com", ChatKind.BUILD)
    await append_batch(
        db_session,
        user_id=user.id,
        conversation_id=conversation.id,
        messages=[
            ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="tell_the_user",
                        args=json.dumps({"update": "x" * (UPDATE_MAX_CHARS + 1)}),
                        tool_call_id="s1",
                    ),
                    ToolCallPart(
                        tool_name="read_file", args='{"path": "app/page.tsx"}', tool_call_id="r1"
                    ),
                ]
            ),
        ],
        entry_kind=MessageEntryKind.TURN,
        kind=ChatKind.BUILD,
    )
    items = project_rows(await _rows(db_session, user, conversation))

    assert [i for i in items if isinstance(i, AssistantTextItem)] == []
    # LIVENESS: the row projected at all — an empty text list is also what a crashed
    # projection returns.
    assert [i.tool for i in items if isinstance(i, StepItem)] == ["read_file"]


async def test_the_spoken_line_survives_a_response_that_also_wrote_prose(
    db_session: AsyncSession,
) -> None:
    """★ THE INTERACTION WITH U4, in one row. A response that narrates, speaks and calls a
    tool loses the narration and keeps the spoken line — which is the whole point of having a
    channel: the drop takes the words the model wrote at itself and leaves the words it
    deliberately addressed to the user."""
    user, conversation = await _thread(db_session, "vc-both@rvaiglobal.com", ChatKind.PLAN)
    await append_batch(
        db_session,
        user_id=user.id,
        conversation_id=conversation.id,
        messages=[
            ModelResponse(
                parts=[
                    TextPart(content="Let me check the Drizzle schema in db/schema.ts."),
                    ToolCallPart(
                        tool_name="tell_the_user",
                        args=json.dumps({"update": _APP_WORDS}),
                        tool_call_id="s1",
                    ),
                    ToolCallPart(
                        tool_name="read_file", args='{"path": "db/schema.ts"}', tool_call_id="r1"
                    ),
                ]
            ),
        ],
        entry_kind=MessageEntryKind.TURN,
        kind=ChatKind.PLAN,
    )
    items = project_rows(await _rows(db_session, user, conversation))

    assert [i.text for i in items if isinstance(i, AssistantTextItem)] == [_APP_WORDS]
    assert "Drizzle" not in " ".join(i.text for i in items if isinstance(i, AssistantTextItem))
