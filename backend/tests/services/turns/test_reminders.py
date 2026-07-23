"""U14 (D3): ephemeral mode reminders — cadence, switch reset, and the persistence
boundary. The reminder rides `message_history` only; `new_messages()` structurally
excludes injected history, so the DB must stay reminder-free by CONSTRUCTION — the
engine-level tests here prove both sides (the model saw it, the rows never did)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid

import pytest
import sqlalchemy as sa
from pydantic_ai.messages import (
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel

from src.db.models.conversation import ConversationMode
from src.db.models.message import Message
from src.services.agent.mode_prompts import PromptContext, mode_reminder
from src.services.messages.store import mode_switch_marker_text
from src.services.turns.engine import (
    REMINDER_FULL_EVERY,
    REMINDER_NUDGE_EVERY,
    TurnEngine,
    _reminder_text,
    set_turn_engine_for_tests,
)
from src.services.turns.guard import _mid_reply
from tests.factories import ConversationFactory, UserFactory

_CTX = PromptContext(user_name="Ada", project_name="Visitors", project_description=None)

# --- history builders ----------------------------------------------------------------


def _turn(n: int) -> list[ModelMessage]:
    return [
        ModelRequest(parts=[UserPromptPart(content=f"question {n}")]),
        ModelResponse(parts=[TextPart(content=f"answer {n}")]),
    ]


def _turns(count: int) -> list[ModelMessage]:
    return [message for n in range(count) for message in _turn(n)]


def _marker(
    old: ConversationMode = ConversationMode.ASK,
    new: ConversationMode = ConversationMode.PLAN,
) -> ModelRequest:
    """The real marker prose (store.py's), so the detector is tested against what the
    switch endpoint actually persists."""
    return ModelRequest(parts=[UserPromptPart(content=mode_switch_marker_text(old, new))])


# --- the cadence ---------------------------------------------------------------------


def test_fresh_conversation_first_turn_is_silent() -> None:
    # The per-run instructions are right there — no reminder on top of them.
    assert _reminder_text(ConversationMode.ASK, []) is None


@pytest.mark.parametrize("ordinal", [1, 2, 3, 5, 7, 9])
def test_between_cadence_points_is_silent(ordinal: int) -> None:
    assert _reminder_text(ConversationMode.PLAN, _turns(ordinal)) is None


@pytest.mark.parametrize("ordinal", [REMINDER_NUDGE_EVERY, 12, 20])
def test_nudge_rides_every_fourth_turn(ordinal: int) -> None:
    assert _reminder_text(ConversationMode.PLAN, _turns(ordinal)) == mode_reminder(
        ConversationMode.PLAN, full=False
    )


@pytest.mark.parametrize("ordinal", [REMINDER_FULL_EVERY, 16, 24])
def test_full_reminder_rides_every_eighth_turn(ordinal: int) -> None:
    assert _reminder_text(ConversationMode.ASK, _turns(ordinal)) == mode_reminder(
        ConversationMode.ASK, full=True
    )


def test_mode_switch_gets_an_immediate_full_reminder() -> None:
    # The new mode's FIRST turn: history actively contradicts the fresh toolset.
    history = [*_turns(3), _marker()]
    assert _reminder_text(ConversationMode.PLAN, history) == mode_reminder(
        ConversationMode.PLAN, full=True
    )


def test_mode_switch_resets_the_cadence() -> None:
    # 7 turns, switch, 1 turn: without the reset this would be the 8th turn (full);
    # with it, it is turn 1 of the new mode — silence.
    history = [*_turns(7), _marker(), *_turn(7)]
    assert _reminder_text(ConversationMode.PLAN, history) is None
    # …and the NEWEST marker wins when the user switched twice.
    twice = [*_turns(2), _marker(), *_turns(REMINDER_NUDGE_EVERY), _marker()]
    assert _reminder_text(ConversationMode.ASK, twice) == mode_reminder(
        ConversationMode.ASK, full=True
    )


def test_tool_return_requests_do_not_advance_the_cadence() -> None:
    # Plan-options resolutions persist as ToolReturnPart-only requests — mechanics,
    # not conversation. Three of them on top of 4 real turns still reads as turn 4.
    resolutions: list[ModelMessage] = [
        ModelRequest(
            parts=[
                ToolReturnPart(
                    tool_name="present_plan_options", content="refine", tool_call_id=f"c{n}"
                )
            ]
        )
        for n in range(3)
    ]
    history = [*_turns(REMINDER_NUDGE_EVERY), *resolutions]
    assert _reminder_text(ConversationMode.PLAN, history) == mode_reminder(
        ConversationMode.PLAN, full=False
    )


def test_reminders_speak_no_forbidden_fruit() -> None:
    # Same R13 bar as the static segments: positive voice only — a reminder that bans
    # absent tools would teach the model to reason about capabilities it doesn't have.
    for mode in ConversationMode:
        for full in (True, False):
            lowered = mode_reminder(mode, full=full).lower()
            for phrase in ("do not", "don't", "never", "cannot", "can't", "must not"):
                assert phrase not in lowered, f"{phrase!r} crept into {mode.value} reminder"


# --- the engine seam: injected into the run, never into a row ------------------------


@pytest.fixture(autouse=True)
def _fresh_engine():
    _mid_reply.clear()
    engine = TurnEngine()
    set_turn_engine_for_tests(engine)
    yield engine
    set_turn_engine_for_tests(None)
    _mid_reply.clear()


@pytest.fixture
def session_factory(db_session):
    @contextlib.asynccontextmanager
    async def _session():
        yield db_session

    return lambda: _session()


async def _noop_persist() -> None:
    return None


async def _settle(engine: TurnEngine, conversation_id: uuid.UUID) -> None:
    state = engine.peek(conversation_id)
    assert state is not None and state.task is not None
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(state.task, timeout=10)


async def _run_with_history(
    engine: TurnEngine, db_session, session_factory, history: list[ModelMessage]
) -> tuple[list[list[ModelMessage]], uuid.UUID]:
    """One Ask turn through the real engine with a capturing model: returns every
    request-message list the model saw, and the conversation id."""
    seen: list[list[ModelMessage]] = []

    async def _stream(messages: list[ModelMessage], info: AgentInfo):
        seen.append(list(messages))
        yield "noted."

    user = await UserFactory.create(db_session)
    conv = await ConversationFactory.create(db_session, user.id, mode=ConversationMode.ASK)
    await engine.start_turn(
        conversation=conv,
        user_id=user.id,
        prompt="and one more thing",
        history=history,
        prompt_context=_CTX,
        app_id=None,
        model=FunctionModel(stream_function=_stream),
        session_factory=session_factory,
        persist_user_turn=_noop_persist,
    )
    await _settle(engine, conv.id)
    return seen, conv.id


async def test_cadence_turn_rides_the_reminder_into_the_model_request(
    _fresh_engine, db_session, session_factory
) -> None:
    seen, _ = await _run_with_history(
        _fresh_engine, db_session, session_factory, _turns(REMINDER_NUDGE_EVERY)
    )
    prompts = [
        part.content
        for message in seen[0]
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, UserPromptPart)
    ]
    # The reminder sits at the tail of history, just before this turn's prompt.
    assert prompts[-2] == mode_reminder(ConversationMode.ASK, full=False)
    assert prompts[-1] == "and one more thing"


async def test_between_cadence_points_the_model_sees_no_reminder(
    _fresh_engine, db_session, session_factory
) -> None:
    seen, _ = await _run_with_history(_fresh_engine, db_session, session_factory, _turns(3))
    dumped = ModelMessagesTypeAdapter.dump_json(seen[0]).decode()
    assert "system-note" not in dumped


async def test_reminders_never_reach_a_persisted_row(
    _fresh_engine, db_session, session_factory
) -> None:
    # Run AT a cadence point (the reminder demonstrably rode the request above) and
    # prove the durable side stayed clean — the `new_messages()` boundary, not a filter.
    _, conversation_id = await _run_with_history(
        _fresh_engine, db_session, session_factory, _turns(REMINDER_NUDGE_EVERY)
    )
    rows = (
        await db_session.scalars(
            sa.select(Message).where(Message.conversation_id == conversation_id)
        )
    ).all()
    assert rows, "the reply row must have landed"
    dumped = json.dumps([row.payload for row in rows])
    assert "system-note" not in dumped
    assert "mode is active" not in dumped
