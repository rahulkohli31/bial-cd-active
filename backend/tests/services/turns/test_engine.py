"""TurnEngine lifecycle tests (U10): detached runs, frame ring, snapshot consolidation,
stop semantics, and the write-before-DONE policy — all at the engine seam with scripted
models (no HTTP; the transport rides `tests/api/v1/conversations/test_turn_stream.py`)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from typing import Any

import pytest
import sqlalchemy as sa
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, DeltaToolCalls, FunctionModel

from src.db.models.conversation import ConversationMode
from src.db.models.message import Message, MessageEntryKind
from src.db.models.token_usage import TokenUsage
from src.services.agent.mode_prompts import PromptContext
from src.services.build_sessions.manager import SessionManager
from src.services.turns import engine as engine_module
from src.services.turns.engine import (
    TurnEngine,
    _persistable_messages,
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
        project_id=conv.project_id,
        model=model,
        session_factory=session_factory,
        persist_user_turn=_noop_persist,
        manager=SessionManager(),
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
    # A NONZERO cursor still inside the ring is the resume case the `?turn=&cursor=` route
    # leans on: the tail only, no gap, and nothing at or before the cursor re-delivered.
    tail, tail_gap = engine.frames_since(state, frames[0].seq)
    assert not tail_gap
    assert [f.type for f in tail] == ["text_delta", "turn_ended"]
    assert all(frame.seq > frames[0].seq for frame in tail)
    # …and a cursor past the ring's newest frame yields nothing at all (settled, replayed).
    assert engine.frames_since(state, frames[-1].seq) == ([], False)
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
    # A RESUME must not lose them: `hidden` is a render hint the client applies, not a payload
    # filter. Dropping hidden steps here meant a mid-turn reconnect saw fewer steps than a tab
    # that stayed connected, and fewer than the same turn shows on reload.
    snapshot = engine.build_snapshot(state)
    assert [item.label for item in snapshot.steps] == ["Read app/page.tsx"]
    assert snapshot.steps[0].hidden is True


async def test_live_step_frames_are_redacted_like_the_persisted_rows(
    _fresh_engine, db_session, session_factory
) -> None:
    """Redaction lived only at the persistence seam, so a secret in a tool's ARGS or OUTPUT
    was masked on reload and shown in full on the LIVE stream — the reading that reaches the
    user first. Same `redact_secrets`, both sides, so the two can never disagree."""
    secret = "postgresql://appuser:sup3rs3cretpw@db.example/appdb"

    async def _stream(messages: list[ModelMessage], info: AgentInfo):
        if len(messages) == 1:
            yield DeltaToolCalls(
                {
                    0: DeltaToolCall(
                        name="read_file",
                        json_args=json.dumps({"path": f"env/{secret}"}),
                        tool_call_id="call-secret",
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
    assert state is not None
    steps = [f for f in state.ring if f.type == "step"]
    assert steps, "no step frames were emitted"
    wire = " ".join(f"{s.item.detail.args} {s.item.detail.result}" for s in steps)
    assert "sup3rs3cretpw" not in wire
    assert "***" in wire


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


async def test_a_second_stop_cannot_eat_the_terminal_frame(
    _fresh_engine, db_session, session_factory
) -> None:
    """A stop fires a cancel into a task that is still unwinding the first one. If the second
    cancel lands on the await inside the cancellation arm, the arm dies before it can emit
    `turn_ended` — every subscriber then hangs to its stall timeout. Two guards: the repeat
    stop is refused outright, and the terminal is emitted BEFORE the arm's only await."""
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
    while not state.text_parts:
        await asyncio.sleep(0.01)

    # Two stops in the SAME tick — the second lands while the first is still unwinding.
    first = await engine.stop_turn(conv.id, turn_id)
    second = await engine.stop_turn(conv.id, turn_id)
    assert first is True
    assert second is False  # refused: a stop was already asked for

    await _settle(engine, conv.id)
    assert state.status == "stopped"
    # EXACTLY ONE terminal reached the ring, and it is the last thing in it.
    terminals = [frame for frame in state.ring if frame.type == "turn_ended"]
    assert len(terminals) == 1
    assert terminals[0].status == "stopped"
    assert state.ring[-1].type == "turn_ended"


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
    # A persist failure surfaces the specific "could not be saved" copy, not the generic one.
    error_frames = [f for f in state.ring if f.type == "error"]
    assert error_frames[-1].message == engine_module._PERSIST_FAILED_MESSAGE
    assert types[-1] == "turn_ended" and state.ring[-1].status == "failed"
    assert conv.id not in _mid_reply


def test_persistable_keeps_tool_returns_drops_user_and_ephemeral_requests():
    # The #3 fix: a responses-only filter drops the ModelRequest carrying the read_file result,
    # so the reload's dangling-call repair papers a real result over with "interrupted". The
    # tool-return request MUST persist; the pre-persisted user turn and the ephemeral nudge
    # (both UserPromptPart requests) must NOT.
    new_messages: list[ModelMessage] = [
        ModelRequest(parts=[UserPromptPart(content="add a dashboard")]),  # already persisted
        ModelResponse(
            parts=[ToolCallPart(tool_name="read_file", args={"path": "a.tsx"}, tool_call_id="r")]
        ),
        ModelRequest(
            parts=[
                ToolReturnPart(tool_name="read_file", content="export default 1", tool_call_id="r")
            ]
        ),
        ModelResponse(parts=[TextPart(content="Here is the plan.")]),
    ]
    kept = _persistable_messages(new_messages)
    # Both responses and the tool-return request survive; the user prompt is dropped.
    assert [type(m).__name__ for m in kept] == ["ModelResponse", "ModelRequest", "ModelResponse"]
    returns = [
        part
        for message in kept
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, ToolReturnPart)
    ]
    assert len(returns) == 1 and returns[0].tool_call_id == "r"
    # No UserPromptPart fossilizes into a persisted row.
    assert not any(
        isinstance(part, UserPromptPart)
        for message in kept
        if isinstance(message, ModelRequest)
        for part in message.parts
    )


async def _used(db_session, user_id: uuid.UUID) -> int:
    row = await db_session.scalar(sa.select(TokenUsage).where(TokenUsage.user_id == user_id))
    return 0 if row is None else row.input_tokens + row.output_tokens


async def test_completed_turn_bills_the_accumulated_usage(
    _fresh_engine, db_session, session_factory
) -> None:
    engine = _fresh_engine
    user, conv, _ = await _start(
        engine, db_session, session_factory, _streaming_text("hi ", "there")
    )
    await _settle(engine, conv.id)
    state = engine.peek(conv.id)
    assert state is not None and state.status == "completed"
    assert await _used(db_session, user.id) > 0  # billed from the run's usage accumulator


async def test_stopped_turn_still_bills_completed_model_requests(
    _fresh_engine, db_session, session_factory
) -> None:
    # A start→stop loop must not be a free ride: tokens the model already produced in a
    # completed request before the Stop still count toward the daily cap (#5).
    gate = asyncio.Event()

    async def _stall_after_a_tool(messages: list[ModelMessage], info: AgentInfo):
        if len(messages) == 1:
            # Request 1 (a read) COMPLETES — its usage lands in the accumulator.
            yield DeltaToolCalls(
                {
                    0: DeltaToolCall(
                        name="read_file", json_args='{"path": "a.tsx"}', tool_call_id="c1"
                    )
                }
            )
        else:
            yield "thinking "  # request 2 has started streaming — now safe to stop
            await gate.wait()
            yield "never"

    engine = _fresh_engine
    user, conv, turn_id = await _start(
        engine, db_session, session_factory, FunctionModel(stream_function=_stall_after_a_tool)
    )
    state = engine.peek(conv.id)
    assert state is not None
    while not state.text_parts:  # request 1 is done; request 2 is streaming
        await asyncio.sleep(0.01)

    assert await engine.stop_turn(conv.id, turn_id) is True
    await _settle(engine, conv.id)
    assert state.status == "stopped"
    assert await _used(db_session, user.id) > 0  # request 1's tokens were billed on the stop


async def test_failed_turn_bills_usage_the_model_already_produced(
    _fresh_engine, db_session, session_factory, monkeypatch
) -> None:
    # A DB error AFTER the model replied must not silently drop the spend either (#17).
    async def _explode(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("the database went away")

    monkeypatch.setattr(engine_module, "append_batch", _explode)
    engine = _fresh_engine
    user, conv, _ = await _start(engine, db_session, session_factory, _streaming_text("reply"))
    await _settle(engine, conv.id)
    state = engine.peek(conv.id)
    assert state is not None and state.status == "failed"
    assert await _used(db_session, user.id) > 0  # billed despite the persist failure


async def test_write_mode_now_runs_on_the_engine_like_any_other_mode(
    _fresh_engine, db_session, session_factory
) -> None:
    """U5's convergence, asserted at the seam that used to refuse it. Write raised
    `TurnUnsupportedError` here because it had no toolset and no composable prompt — a build's
    mode, not a chat mode. Both of those are false now: Write composes like every other mode
    and carries the sandbox six, so the engine must accept it. The behaviour of the run itself
    lives in `test_write_turn.py`; this pins only that the door is open."""
    engine = _fresh_engine
    user, conv = await _conversation(db_session, ConversationMode.WRITE)
    assert engine.peek(conv.id) is None
    turn_id = await engine.start_turn(
        conversation=conv,
        user_id=user.id,
        prompt="add a field",
        history=[],
        prompt_context=_CTX,
        app_id=None,
        project_id=conv.project_id,
        model=_streaming_text("x"),
        session_factory=session_factory,
        persist_user_turn=_noop_persist,
        manager=SessionManager(),
        # No sandbox client configured: the turn starts and then ends with a NAMED reason
        # rather than being refused at the door. That distinction is the whole unit — a
        # citizen in Write mode gets a running turn and an explanation, not a 400.
        sandbox_client=None,
    )
    state = engine.peek(conv.id)
    assert state is not None and state.task is not None
    await state.task
    assert state.turn_id == turn_id
    assert state.status == "failed"
    assert state.end_reason == "sandbox_unavailable"


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
            project_id=conv.project_id,
            model=_streaming_text("x"),
            session_factory=session_factory,
            persist_user_turn=_noop_persist,
            manager=SessionManager(),
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


# --- #83: stopping a user's turn so another project can have the workspace ----------


async def test_stop_user_turn_and_wait_settles_before_it_returns(
    _fresh_engine, db_session, session_factory
) -> None:
    """THE CONTRACT `stop_turn` CANNOT OFFER, and the reason this exists beside it.

    `stop_turn` returns the instant `task.cancel()` is issued. The #83 "stop and switch" flow
    cannot act on that: its very next steps save the workspace and tear the container down, and
    a turn that is still unwinding still owns that container — releasing underneath it is the
    strand this whole subsystem is written to prevent. So this one WAITS, and the assertion
    that matters is that the task is genuinely done by the time it hands back.

    It is also keyed on the USER rather than a conversation, because the caller is a project
    switch: the refusal names a project, and a Write turn's manager session carries no
    conversation id to look up."""
    gate = asyncio.Event()

    async def _stall(messages: list[ModelMessage], info: AgentInfo):
        yield "partial "
        await gate.wait()
        yield "never"

    engine = _fresh_engine
    user, conv, turn_id = await _start(
        engine, db_session, session_factory, FunctionModel(stream_function=_stall)
    )
    state = engine.peek(conv.id)
    assert state is not None
    while not state.text_parts:  # the run is genuinely streaming
        await asyncio.sleep(0.01)

    stopped = await engine.stop_user_turn_and_wait(user.id, timeout_s=10)

    assert stopped is True
    # SETTLED, not merely asked to settle — this is the whole difference from `stop_turn`.
    assert state.task is not None and state.task.done()
    assert state.status == "stopped"
    assert state.ring[-1].type == "turn_ended" and state.ring[-1].status == "stopped"
    assert conv.id not in _mid_reply  # the busy guard released with the task


async def test_stop_user_turn_and_wait_finds_nothing_when_nothing_runs(
    _fresh_engine, db_session, session_factory
) -> None:
    """False, not an error. The caller's goal is "settled" and it already is — and this is the
    COMMON path, because a build usually finishes while the user is still reading the dialog."""
    user = await UserFactory.create(db_session, email="stopnone@rvaiglobal.com")
    assert await _fresh_engine.stop_user_turn_and_wait(user.id, timeout_s=5) is False


async def test_stop_user_turn_and_wait_leaves_another_users_turn_alone(
    _fresh_engine, db_session, session_factory
) -> None:
    """The slot is per-user, so the scan must be too. A stop that reached across users would
    let one citizen cancel another's build — the sandbox lock is keyed on `user_id` and nothing
    downstream would notice."""
    gate = asyncio.Event()

    async def _stall(messages: list[ModelMessage], info: AgentInfo):
        yield "partial "
        await gate.wait()
        yield "never"

    engine = _fresh_engine
    owner, conv, _ = await _start(
        engine, db_session, session_factory, FunctionModel(stream_function=_stall)
    )
    state = engine.peek(conv.id)
    assert state is not None
    while not state.text_parts:
        await asyncio.sleep(0.01)

    stranger = await UserFactory.create(db_session, email="stopother@rvaiglobal.com")
    assert await engine.stop_user_turn_and_wait(stranger.id, timeout_s=5) is False
    assert state.status == "running"  # the owner's turn is untouched

    gate.set()
    await _settle(engine, conv.id)


async def test_stop_user_turn_and_wait_is_safe_to_repeat(
    _fresh_engine, db_session, session_factory
) -> None:
    """A second call must not fire a second cancel into a task still unwinding the first — that
    lands inside the cleanup arm and can eat the `turn_ended` frame subscribers are waiting on
    (the hazard `test_a_second_stop_cannot_eat_the_terminal_frame` pins for `stop_turn`).
    `stop_requested` is what prevents it, and this holds that guard on the new door too."""
    gate = asyncio.Event()

    async def _stall(messages: list[ModelMessage], info: AgentInfo):
        yield "partial "
        await gate.wait()
        yield "never"

    engine = _fresh_engine
    user, conv, _ = await _start(
        engine, db_session, session_factory, FunctionModel(stream_function=_stall)
    )
    state = engine.peek(conv.id)
    assert state is not None
    while not state.text_parts:
        await asyncio.sleep(0.01)

    assert await engine.stop_user_turn_and_wait(user.id, timeout_s=10) is True
    # The turn has settled, so the repeat finds nothing running — and the terminal survives.
    assert await engine.stop_user_turn_and_wait(user.id, timeout_s=10) is False
    assert state.ring[-1].type == "turn_ended" and state.ring[-1].status == "stopped"
