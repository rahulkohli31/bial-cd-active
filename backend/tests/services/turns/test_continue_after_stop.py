"""Continuing a chat after it was stopped: every way a turn stops, then a new message.

Each scenario stops a real turn through the engine, asserts the rows that were actually
written, then sends again the way the send route does: history through `load_history`, the
one loader, and the user's message persisted under the claim. The next turn must complete on a
history the provider would accept.

pydantic-ai repairs a dangling call by itself before every request, so what the scripted model
receives cannot prove the store's repair. The wire check is applied to what `load_history` hands
the engine as well as to what the model receives; the repair tests at the bottom go red if the
store's repair step is removed.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from typing import Any

import pytest
import redis.asyncio as aioredis
from pydantic import SecretStr
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import (
    AgentInfo,
    FunctionModel,
)
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings
from src.db.models.conversation import ChatKind
from src.db.models.message import MessageEntryKind
from src.services.build_sessions.outcome import STOPPED_BY_USER
from src.services.messages.projection import (
    BuildInProgressItem,
    StepItem,
    TurnTerminalItem,
    project_rows,
)
from src.services.messages.store import _INTERRUPTED_RESULT, append_batch, dump_for_row, load_rows
from src.services.sandbox.config import SandboxConfig
from src.services.turns import engine as engine_module
from src.services.turns.engine import (
    STOPPED_AT_A_BOUNDARY,
    TurnEngine,
    TurnNotRunningError,
    publish_cooperative_stop,
    set_turn_engine_for_tests,
)
from src.services.turns.guard import ConversationBusyError, _mid_reply
from tests.continuation import (
    WRITE_ARGS,
    WROTE_THE_PAGE,
    Chat,
    Workspace,
    answering,
    new_chat,
    stalls_after,
    tool_answers,
    user_message,
    user_texts,
    writes_then,
)
from tests.factories import MessageFactory


@pytest.fixture(autouse=True)
def _sandbox_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        settings,
        "sandbox",
        SandboxConfig(
            subscription_id="s",
            resource_group="r",
            region="westeurope",
            managed_environment_name="aca-env",
            acr_server="acr.azurecr.io",
            acr_username="acr-user",
            acr_password=SecretStr("acr-pass"),
            image_ref="acr/img:latest",
        ),
    )


@pytest.fixture(autouse=True)
async def _sandbox_dependencies(fake_redis: aioredis.Redis, fake_storage) -> None:
    """Every turn attaches a live container, which reads the lease store and the object store."""
    return None


@pytest.fixture(autouse=True)
def _fresh_engine():
    _mid_reply.clear()
    engine = TurnEngine()
    set_turn_engine_for_tests(engine)
    yield engine
    set_turn_engine_for_tests(None)
    _mid_reply.clear()


@pytest.fixture
def session_factory(db_session: AsyncSession):
    @contextlib.asynccontextmanager
    async def _session():
        yield db_session

    return lambda: _session()


@pytest.fixture
def _a_brisk_lease(monkeypatch: pytest.MonkeyPatch) -> None:
    """The lease loop carries a cooperative stop into the turn; production looks every 30s."""
    monkeypatch.setattr(engine_module, "LIVENESS_LEASE_RENEW_CADENCE_SECONDS", 0.01)


async def _only_the_question(chat: Chat, text: str) -> bool:
    history = await chat.history()
    return len(history) == 1 and user_texts(history) == [text]


# --- plan chats: the reply is written only when the turn completes ---------------------------


async def test_a_plan_chat_stopped_mid_stream_keeps_only_the_question_and_answers_next_time(
    _fresh_engine: TurnEngine, db_session: AsyncSession, session_factory
) -> None:
    chat = await new_chat(_fresh_engine, db_session, session_factory, ChatKind.PLAN)
    streaming = asyncio.Event()
    turn_id = await chat.send("draft a plan", stalls_after("Here is ", streaming))
    await asyncio.wait_for(streaming.wait(), timeout=10)

    assert await _fresh_engine.stop_turn(chat.conversation.id, turn_id) is True
    state = await chat.settled()

    assert state.status == "stopped"
    assert state.text_blocks() == ["Here is "]
    assert await _only_the_question(chat, "draft a plan")
    seen = await chat.continues("try again")
    assert user_texts(seen) == ["draft a plan", "try again"]


async def test_a_plan_chat_stopped_before_its_first_word_continues(
    _fresh_engine: TurnEngine, db_session: AsyncSession, session_factory
) -> None:
    chat = await new_chat(_fresh_engine, db_session, session_factory, ChatKind.PLAN)
    asked = asyncio.Event()

    async def _silent(_messages: list[ModelMessage], _info: AgentInfo):
        asked.set()
        await asyncio.Event().wait()
        yield "never"

    turn_id = await chat.send("draft a plan", FunctionModel(stream_function=_silent))
    await asyncio.wait_for(asked.wait(), timeout=10)

    assert await _fresh_engine.stop_turn(chat.conversation.id, turn_id) is True
    state = await chat.settled()

    assert state.status == "stopped"
    assert state.text_blocks() == []
    assert await _only_the_question(chat, "draft a plan")
    await chat.continues()


async def test_a_plan_chat_stopped_after_its_last_word_but_before_saving_continues(
    _fresh_engine: TurnEngine,
    db_session: AsyncSession,
    session_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The window between the model finishing and the reply reaching a row: the reply is lost
    whole, never half-written, and the question it answered is still the last thing stored."""
    chat = await new_chat(_fresh_engine, db_session, session_factory, ChatKind.PLAN)
    saving = asyncio.Event()
    real_append = append_batch

    async def _held_reply(db: AsyncSession, **kwargs: Any):
        if kwargs["entry_kind"] is MessageEntryKind.TURN and not saving.is_set():
            saving.set()
            await asyncio.Event().wait()
        return await real_append(db, **kwargs)

    monkeypatch.setattr(engine_module, "append_batch", _held_reply)
    model, _ = answering("The whole plan.")
    turn_id = await chat.send("draft a plan", model)
    await asyncio.wait_for(saving.wait(), timeout=10)

    assert await _fresh_engine.stop_turn(chat.conversation.id, turn_id) is True
    state = await chat.settled()

    assert state.status == "stopped"
    assert state.text_blocks() == ["The whole plan."]
    assert await _only_the_question(chat, "draft a plan")
    await chat.continues()


async def test_two_questions_in_a_row_reach_the_model_as_one_valid_user_turn(
    _fresh_engine: TurnEngine, db_session: AsyncSession, session_factory
) -> None:
    """A stopped plan turn leaves the question with no answer after it, so the next send puts
    two user messages side by side. They must reach the model merged or alternating, in order."""
    chat = await new_chat(_fresh_engine, db_session, session_factory, ChatKind.PLAN)
    streaming = asyncio.Event()
    turn_id = await chat.send("first question", stalls_after("Thinking", streaming))
    await asyncio.wait_for(streaming.wait(), timeout=10)
    await _fresh_engine.stop_turn(chat.conversation.id, turn_id)
    await chat.settled()

    seen = await chat.continues("second question")

    assert user_texts(seen) == ["first question", "second question"]
    assert not any(isinstance(message, ModelResponse) for message in seen)


# --- build chats: each step is written as it completes ---------------------------------------


async def test_a_build_chat_cut_while_its_tool_runs_leaves_no_trace_of_the_call(
    _fresh_engine: TurnEngine, db_session: AsyncSession, session_factory
) -> None:
    """Characterizes the earliest cut: the step that asked for the write is stored only once its
    tools have run, so a cut inside the tool stores neither the call nor an answer, and the next
    turn starts from the question."""
    workspace = Workspace(hold_writes=True)
    chat = await new_chat(
        _fresh_engine, db_session, session_factory, ChatKind.BUILD, workspace=workspace
    )
    turn_id = await chat.send("add a page", writes_then(asyncio.Event(), then="done"))
    await asyncio.wait_for(workspace.inside.wait(), timeout=10)

    assert await _fresh_engine.stop_turn(chat.conversation.id, turn_id) is True
    state = await chat.settled()

    assert state.status == "stopped"
    assert workspace.written == []
    assert await _only_the_question(chat, "add a page")
    await chat.continues()


async def test_a_build_chat_cut_after_its_tool_ran_is_repaired_with_the_interrupted_note(
    _fresh_engine: TurnEngine, db_session: AsyncSession, session_factory
) -> None:
    """The step with the call is stored; its answer rides on the next model request and is lost
    with it. The stored history carries an unanswered call, and the next send must close it."""
    chat = await new_chat(_fresh_engine, db_session, session_factory, ChatKind.BUILD)
    second_request = asyncio.Event()
    turn_id = await chat.send("add a page", writes_then(second_request))
    await asyncio.wait_for(second_request.wait(), timeout=10)

    assert await _fresh_engine.stop_turn(chat.conversation.id, turn_id) is True
    state = await chat.settled()

    assert state.status == "stopped"
    stored = await chat.stored_messages()
    stored_calls = [
        part["tool_call_id"]
        for message in stored
        for part in message["parts"]
        if part["part_kind"] == "tool-call"
    ]
    stored_answers = [
        part["tool_call_id"]
        for message in stored
        for part in message["parts"]
        if part["part_kind"] == "tool-return"
    ]
    assert stored_calls == ["c-write-1"]
    assert stored_answers == []
    assert tool_answers(await chat.history()) == {"c-write-1": _INTERRUPTED_RESULT}
    seen = await chat.continues()
    assert tool_answers(seen)["c-write-1"] == _INTERRUPTED_RESULT


async def test_a_tool_that_ran_but_was_never_recorded_is_not_reported_as_not_executed(
    _fresh_engine: TurnEngine, db_session: AsyncSession, session_factory
) -> None:
    workspace = Workspace()
    chat = await new_chat(
        _fresh_engine, db_session, session_factory, ChatKind.BUILD, workspace=workspace
    )
    second_request = asyncio.Event()
    turn_id = await chat.send("add a page", writes_then(second_request))
    await asyncio.wait_for(second_request.wait(), timeout=10)
    await _fresh_engine.stop_turn(chat.conversation.id, turn_id)
    await chat.settled()

    assert workspace.written == ["app/page.tsx"]
    told = tool_answers(await chat.history())["c-write-1"]
    assert "not executed" not in told
    assert "may have" in told


async def test_a_build_chat_stopped_cooperatively_keeps_the_real_answer(
    _fresh_engine: TurnEngine,
    db_session: AsyncSession,
    session_factory,
    _a_brisk_lease: None,
) -> None:
    workspace = Workspace(hold_writes=True)
    chat = await new_chat(
        _fresh_engine, db_session, session_factory, ChatKind.BUILD, workspace=workspace
    )
    await chat.send("add a page", writes_then(asyncio.Event(), then="done"))
    await asyncio.wait_for(workspace.inside.wait(), timeout=10)

    await publish_cooperative_stop(chat.conversation.id)
    state = _fresh_engine.peek(chat.conversation.id)
    assert state is not None
    for _ in range(2000):
        if state.cooperative_stop_requested:
            break
        await asyncio.sleep(0.001)
    assert state.cooperative_stop_requested, "the ask never reached the turn"
    workspace.let_go.set()
    state = await chat.settled()

    assert state.status == "completed"
    assert state.end_reason == STOPPED_AT_A_BOUNDARY
    assert workspace.written == ["app/page.tsx"]
    answers = tool_answers(await chat.history())
    assert answers == {"c-write-1": WROTE_THE_PAGE}
    await chat.continues()


# --- the stop itself ---------------------------------------------------------------------------


async def test_a_second_stop_answers_already_settled_and_changes_nothing(
    _fresh_engine: TurnEngine, db_session: AsyncSession, session_factory
) -> None:
    chat = await new_chat(_fresh_engine, db_session, session_factory, ChatKind.PLAN)
    streaming = asyncio.Event()
    turn_id = await chat.send("draft a plan", stalls_after("Here ", streaming))
    await asyncio.wait_for(streaming.wait(), timeout=10)
    assert await _fresh_engine.stop_turn(chat.conversation.id, turn_id) is True
    await chat.settled()
    rows_before = await chat.stored_messages()

    assert await _fresh_engine.stop_turn(chat.conversation.id, turn_id) is False

    assert await chat.stored_messages() == rows_before
    await chat.continues()


async def test_a_stop_naming_another_turn_is_refused_and_the_running_turn_finishes(
    _fresh_engine: TurnEngine, db_session: AsyncSession, session_factory
) -> None:
    """The route answers this refusal with a 409 (pinned in the turn-stream tests)."""
    chat = await new_chat(_fresh_engine, db_session, session_factory, ChatKind.PLAN)
    gate = asyncio.Event()

    async def _paced(_messages: list[ModelMessage], _info: AgentInfo):
        yield "half "
        await gate.wait()
        yield "and the rest"

    await chat.send("draft a plan", FunctionModel(stream_function=_paced))
    state = _fresh_engine.peek(chat.conversation.id)
    assert state is not None
    while not state.text_blocks():
        await asyncio.sleep(0.01)

    with pytest.raises(TurnNotRunningError):
        await _fresh_engine.stop_turn(chat.conversation.id, uuid.uuid7())

    assert state.status == "running"
    assert not state.stop_requested
    gate.set()
    state = await chat.settled()
    assert state.status == "completed"
    history = await chat.history()
    assert isinstance(history[-1], ModelResponse)
    assert [p.content for p in history[-1].parts if isinstance(p, TextPart)] == [
        "half and the rest"
    ]


async def test_a_stopped_chat_reloads_with_no_running_turn_and_nothing_left_in_progress(
    _fresh_engine: TurnEngine, db_session: AsyncSession, session_factory
) -> None:
    """What a reload reads after a cut: no `activeTurn`, no in-progress anchor, and a stored
    terminal that says the turn stopped — so a step whose answer was lost is the last thing
    before an ending, not a card still waiting."""
    chat = await new_chat(_fresh_engine, db_session, session_factory, ChatKind.BUILD)
    second_request = asyncio.Event()
    turn_id = await chat.send("add a page", writes_then(second_request))
    await asyncio.wait_for(second_request.wait(), timeout=10)
    await _fresh_engine.stop_turn(chat.conversation.id, turn_id)
    await chat.settled()

    assert _fresh_engine.active_turn_info(chat.conversation.id) is None
    rows = await load_rows(
        db_session, user_id=chat.user.id, conversation_id=chat.conversation.id, include_hidden=True
    )
    items = project_rows(rows)
    assert not any(isinstance(item, BuildInProgressItem) for item in items)
    assert isinstance(items[-1], TurnTerminalItem)
    assert items[-1].terminal == "stopped"
    assert items[-1].reason == STOPPED_BY_USER
    pending = [i for i in items if isinstance(i, StepItem) and i.state == "pending"]
    assert all(item.seq < items[-1].seq for item in pending)


async def test_a_send_while_the_stop_is_still_settling_is_refused_then_admitted(
    _fresh_engine: TurnEngine,
    db_session: AsyncSession,
    session_factory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The claim is let go only once the stopped turn has written its ending, so a send in that
    window gets the ordinary busy refusal — and writes nothing — rather than a second run."""
    chat = await new_chat(_fresh_engine, db_session, session_factory, ChatKind.PLAN)
    streaming = asyncio.Event()
    turn_id = await chat.send("draft a plan", stalls_after("Here ", streaming))
    await asyncio.wait_for(streaming.wait(), timeout=10)
    ending = asyncio.Event()
    let_it_end = asyncio.Event()
    real_terminal = _fresh_engine._write_turn_terminal

    async def _held_terminal(state: Any, factory: Any) -> None:
        ending.set()
        await let_it_end.wait()
        await real_terminal(state, factory)

    monkeypatch.setattr(_fresh_engine, "_write_turn_terminal", _held_terminal)
    await _fresh_engine.stop_turn(chat.conversation.id, turn_id)
    await asyncio.wait_for(ending.wait(), timeout=10)
    stored_before = await chat.stored_messages()

    model, seen = answering()
    with pytest.raises(ConversationBusyError):
        await chat.send("are you there?", model)

    assert seen == []
    assert await chat.stored_messages() == stored_before
    let_it_end.set()
    await chat.settled()
    await chat.continues("are you there?")


# --- repair: stored rows the ordinary path would never write -----------------------------------


async def _seeded_buildnew_chat(
    engine: TurnEngine,
    db: AsyncSession,
    session_factory: Any,
    *batches: list[ModelMessage],
) -> Chat:
    chat = await new_chat(engine, db, session_factory, ChatKind.BUILD)
    for seq, batch in enumerate(batches):
        await MessageFactory.create(
            db, chat.user.id, chat.conversation.id, seq=seq, payload=dump_for_row(batch)
        )
    return chat


def _calls(*ids: str, args: str = WRITE_ARGS) -> ModelResponse:
    return ModelResponse(
        parts=[ToolCallPart(tool_name="write_file", args=args, tool_call_id=i) for i in ids]
    )


def _answer(tool_call_id: str, content: str = WROTE_THE_PAGE) -> ModelRequest:
    return ModelRequest(
        parts=[ToolReturnPart(tool_name="write_file", content=content, tool_call_id=tool_call_id)]
    )


async def test_repair_closes_a_call_cut_off_mid_arguments(
    _fresh_engine: TurnEngine, db_session: AsyncSession, session_factory
) -> None:
    chat = await _seeded_buildnew_chat(
        _fresh_engine,
        db_session,
        session_factory,
        [user_message("add a page")],
        [_calls("c-1", args='{"path": "app/pa')],
    )

    assert tool_answers(await chat.history()) == {"c-1": _INTERRUPTED_RESULT}
    seen = await chat.continues()
    assert tool_answers(seen) == {"c-1": _INTERRUPTED_RESULT}


async def test_repair_closes_a_call_left_unanswered_before_a_later_question(
    _fresh_engine: TurnEngine, db_session: AsyncSession, session_factory
) -> None:
    chat = await _seeded_buildnew_chat(
        _fresh_engine,
        db_session,
        session_factory,
        [user_message("add a page")],
        [_calls("c-1")],
        [user_message("anything?")],
        [ModelResponse(parts=[TextPart(content="Still here.")])],
    )

    assert tool_answers(await chat.history()) == {"c-1": _INTERRUPTED_RESULT}
    await chat.continues()


async def test_repair_drops_an_answer_whose_call_was_never_stored(
    _fresh_engine: TurnEngine, db_session: AsyncSession, session_factory
) -> None:
    chat = await _seeded_buildnew_chat(
        _fresh_engine,
        db_session,
        session_factory,
        [user_message("add a page"), ModelResponse(parts=[TextPart(content="On it.")])],
        [_answer("c-ghost")],
    )

    assert tool_answers(await chat.history()) == {}
    seen = await chat.continues()
    assert "c-ghost" not in tool_answers(seen)


async def test_repair_keeps_one_answer_when_a_call_was_answered_twice(
    _fresh_engine: TurnEngine, db_session: AsyncSession, session_factory
) -> None:
    chat = await _seeded_buildnew_chat(
        _fresh_engine,
        db_session,
        session_factory,
        [user_message("add a page"), _calls("c-1"), _answer("c-1")],
        [_answer("c-1", "Wrote it a second time.")],
        [ModelResponse(parts=[TextPart(content="Done.")])],
    )

    history = await chat.history()
    returns = [
        part
        for message in history
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, ToolReturnPart)
    ]
    assert [part.content for part in returns] == [WROTE_THE_PAGE]
    await chat.continues()


async def test_repair_moves_a_late_answer_back_next_to_its_call(
    _fresh_engine: TurnEngine, db_session: AsyncSession, session_factory
) -> None:
    chat = await _seeded_buildnew_chat(
        _fresh_engine,
        db_session,
        session_factory,
        [user_message("add a page"), _calls("c-1")],
        [user_message("and a footer")],
        [ModelResponse(parts=[TextPart(content="Sure.")])],
        [_answer("c-1")],
    )

    history = await chat.history()
    call_at = next(
        index
        for index, message in enumerate(history)
        if isinstance(message, ModelResponse)
        and any(isinstance(part, ToolCallPart) for part in message.parts)
    )
    assert tool_answers([history[call_at + 1]]) == {"c-1": WROTE_THE_PAGE}
    await chat.continues()
