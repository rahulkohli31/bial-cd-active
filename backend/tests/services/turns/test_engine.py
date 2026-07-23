"""TurnEngine lifecycle tests (U10): detached runs, frame ring, snapshot consolidation,
stop semantics, and the write-before-DONE policy — all at the engine seam with scripted
models (no HTTP; the transport rides `tests/api/v1/conversations/test_turn_stream.py`)."""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from typing import Any

import pytest
import sqlalchemy as sa
from pydantic_ai.messages import (
    ModelMessage,
)
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, DeltaToolCalls, FunctionModel

from src.db.models.conversation import ConversationMode
from src.db.models.message import Message, MessageEntryKind
from src.services.agent.mode_prompts import PromptContext
from src.services.turns import engine as engine_module
from src.services.turns.engine import (
    TurnEngine,
    TurnUnsupportedError,
    set_turn_engine_for_tests,
)
from src.services.turns.guard import ConversationBusyError, _mid_reply
from tests.factories import ConversationFactory, UserFactory

_CTX = PromptContext(user_name="Ada", project_name="Visitors", project_description=None)


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


async def _conversation(db_session, mode: ConversationMode = ConversationMode.ASK):
    user = await UserFactory.create(db_session)
    conv = await ConversationFactory.create(db_session, user.id, mode=mode)
    return user, conv


def _streaming_text(*chunks: str):
    async def _stream(messages: list[ModelMessage], info: AgentInfo):
        for chunk in chunks:
            yield chunk

    return FunctionModel(stream_function=_stream)


async def _start(
    engine: TurnEngine, db_session, session_factory, model, *, mode=ConversationMode.ASK
):
    user, conv = await _conversation(db_session, mode)
    turn_id = await engine.start_turn(
        conversation=conv,
        user_id=user.id,
        prompt="hi there",
        history=[],
        prompt_context=_CTX,
        app_id=None,
        model=model,
        session_factory=session_factory,
        persist_user_turn=_noop_persist,
    )
    return user, conv, turn_id


async def _noop_persist() -> None:
    return None


async def _settle(engine: TurnEngine, conversation_id: uuid.UUID) -> None:
    state = engine.peek(conversation_id)
    assert state is not None and state.task is not None
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(state.task, timeout=10)


async def test_text_turn_streams_deltas_then_terminal(
    _fresh_engine, db_session, session_factory
) -> None:
    engine = _fresh_engine
    user, conv, _ = await _start(
        engine, db_session, session_factory, _streaming_text("hello ", "world")
    )
    await _settle(engine, conv.id)

    state = engine.peek(conv.id)
    assert state is not None and state.status == "completed"
    frames, gap = engine.frames_since(state, 0)
    assert not gap
    assert [f.type for f in frames] == ["text_delta", "text_delta", "turn_ended"]
    assert state.text_so_far() == "hello world"
    # WRITE-BEFORE-DONE: the reply row landed (ModelResponse only — the user turn is the
    # route's pre-write, deliberately absent here via the no-op persister).
    rows = (
        await db_session.scalars(
            sa.select(Message).where(Message.conversation_id == conv.id).order_by(Message.seq)
        )
    ).all()
    assert len(rows) == 1
    assert rows[0].entry_kind is MessageEntryKind.TURN
    assert rows[0].payload[0]["kind"] == "response"
    # The composed mode instructions never reach the row (U9's dump-seam strip).
    assert rows[0].payload[0].get("instructions") is None


async def test_read_tool_calls_become_step_frames(
    _fresh_engine, db_session, session_factory
) -> None:
    call_id = "call-1"

    async def _stream(messages: list[ModelMessage], info: AgentInfo):
        if len(messages) == 1:
            # First request: call read_file (hidden read; EmptyProjectWorkspace answers
            # the truthful no-app-yet result, state ok).
            yield DeltaToolCalls(
                {
                    0: DeltaToolCall(
                        name="read_file",
                        json_args='{"path": "app/page.tsx"}',
                        tool_call_id=call_id,
                    )
                }
            )
        else:
            yield "done"

    engine = _fresh_engine
    _, conv, _ = await _start(
        engine, db_session, session_factory, FunctionModel(stream_function=_stream)
    )
    await _settle(engine, conv.id)

    state = engine.peek(conv.id)
    assert state is not None and state.status == "completed"
    steps = [f for f in state.ring if f.type == "step"]
    assert [s.phase for s in steps] == ["started", "finished"]
    assert steps[0].tool_call_id == call_id and steps[1].tool_call_id == call_id
    assert steps[0].item.state == "pending" and steps[1].item.state == "ok"
    assert steps[1].item.hidden is True  # reads are hidden by default
    assert steps[1].item.label == "Read app/page.tsx"
    assert "No app exists yet" in (steps[1].item.detail.result or "")


async def test_stop_cancels_and_leaves_truthful_record(
    _fresh_engine, db_session, session_factory
) -> None:
    gate = asyncio.Event()

    async def _stall(messages: list[ModelMessage], info: AgentInfo):
        yield "partial "
        await gate.wait()
        yield "never"

    engine = _fresh_engine
    _, conv, turn_id = await _start(
        engine, db_session, session_factory, FunctionModel(stream_function=_stall)
    )
    state = engine.peek(conv.id)
    assert state is not None
    while not state.text_parts:  # the first delta proves the run is streaming
        await asyncio.sleep(0.01)

    assert await engine.stop_turn(conv.id, turn_id) is True
    await _settle(engine, conv.id)
    assert state.status == "stopped"
    assert state.ring[-1].type == "turn_ended" and state.ring[-1].status == "stopped"
    # No reply row — none finished; the durable record stays truthful.
    rows = (
        await db_session.scalars(sa.select(Message).where(Message.conversation_id == conv.id))
    ).all()
    assert rows == []
    assert conv.id not in _mid_reply  # the guard released with the task
    # Stopping the already-settled turn is a no-op, not an error.
    assert await engine.stop_turn(conv.id, turn_id) is False


async def test_persist_failure_fails_the_turn_loudly(
    _fresh_engine, db_session, session_factory, monkeypatch
) -> None:
    async def _explode(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("the database went away")

    monkeypatch.setattr(engine_module, "append_batch", _explode)
    engine = _fresh_engine
    _, conv, _ = await _start(engine, db_session, session_factory, _streaming_text("reply"))
    await _settle(engine, conv.id)

    state = engine.peek(conv.id)
    assert state is not None and state.status == "failed"
    types = [f.type for f in state.ring]
    assert "error" in types  # in-band, before the terminal
    assert types[-1] == "turn_ended" and state.ring[-1].status == "failed"
    assert conv.id not in _mid_reply


async def test_write_mode_is_unsupported_until_u12(
    _fresh_engine, db_session, session_factory
) -> None:
    engine = _fresh_engine
    user, conv = await _conversation(db_session, ConversationMode.WRITE)
    with pytest.raises(TurnUnsupportedError):
        await engine.start_turn(
            conversation=conv,
            user_id=user.id,
            prompt="build it",
            history=[],
            prompt_context=_CTX,
            app_id=None,
            model=_streaming_text("x"),
            session_factory=session_factory,
            persist_user_turn=_noop_persist,
        )
    assert conv.id not in _mid_reply  # nothing claimed on refusal


async def test_second_start_is_busy_and_first_still_completes(
    _fresh_engine, db_session, session_factory
) -> None:
    gate = asyncio.Event()

    async def _stall(messages: list[ModelMessage], info: AgentInfo):
        yield "thinking "
        await gate.wait()
        yield "done"

    engine = _fresh_engine
    user, conv, _ = await _start(
        engine, db_session, session_factory, FunctionModel(stream_function=_stall)
    )
    with pytest.raises(ConversationBusyError):
        await engine.start_turn(
            conversation=conv,
            user_id=user.id,
            prompt="again",
            history=[],
            prompt_context=_CTX,
            app_id=None,
            model=_streaming_text("x"),
            session_factory=session_factory,
            persist_user_turn=_noop_persist,
        )
    gate.set()
    await _settle(engine, conv.id)
    state = engine.peek(conv.id)
    assert state is not None and state.status == "completed"


async def test_active_turn_info_only_while_running(
    _fresh_engine, db_session, session_factory
) -> None:
    gate = asyncio.Event()

    async def _stall(messages: list[ModelMessage], info: AgentInfo):
        yield "живой "
        await gate.wait()
        yield "поток"

    engine = _fresh_engine
    _, conv, turn_id = await _start(
        engine, db_session, session_factory, FunctionModel(stream_function=_stall)
    )
    state = engine.peek(conv.id)
    assert state is not None
    while not state.text_parts:
        await asyncio.sleep(0.01)
    info = engine.active_turn_info(conv.id)
    assert info is not None and info.turn_id == turn_id and info.last_seq >= 1
    gate.set()
    await _settle(engine, conv.id)
    assert engine.active_turn_info(conv.id) is None


async def test_ring_eviction_degrades_to_snapshot_not_gap_loss(
    _fresh_engine, db_session, session_factory, monkeypatch
) -> None:
    # A tiny ring forces eviction; the consolidated snapshot still carries the FULL text,
    # so a subscriber that fell behind loses nothing (the review's buffer-eviction gap).
    monkeypatch.setattr(engine_module, "RING_MAXLEN", 4)
    chunks = [f"c{i} " for i in range(12)]
    engine = _fresh_engine
    _, conv, _ = await _start(engine, db_session, session_factory, _streaming_text(*chunks))
    await _settle(engine, conv.id)

    state = engine.peek(conv.id)
    assert state is not None
    frames, gap = engine.frames_since(state, 0)
    assert gap  # the early frames were evicted — replay alone would lie
    snapshot = engine.build_snapshot(state)
    assert snapshot.text_so_far == "".join(chunks)
    assert snapshot.turn_status == "completed"
    assert snapshot.seq == state.seq


async def test_ended_turn_expires_after_ttl(
    _fresh_engine, db_session, session_factory, monkeypatch
) -> None:
    engine = _fresh_engine
    _, conv, _ = await _start(engine, db_session, session_factory, _streaming_text("bye"))
    await _settle(engine, conv.id)
    assert engine.peek(conv.id) is not None
    monkeypatch.setattr(engine_module, "ENDED_TURN_TTL_S", 0.0)
    assert engine.peek(conv.id) is None  # lazily evicted; the DB is the record now
