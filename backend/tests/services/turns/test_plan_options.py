"""U11 — present_plan_options mechanics: the call DEFERS (the click is the result), the
retry guarantee forces the tool exactly once on a plan-shaped miss, the retry cap
synthesizes a card rather than stranding the user, and resolutions are idempotent,
newest-only, and derived identically by the projection."""

from __future__ import annotations

import asyncio
import contextlib

import pytest
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models.function import (
    AgentInfo,
    DeltaToolCall,
    DeltaToolCalls,
    FunctionModel,
)

from src.db.models.conversation import ConversationMode
from src.services.agent.mode_prompts import PromptContext
from src.services.build_sessions.manager import SessionManager
from src.services.messages.projection import PlanOptionsItem, project_rows
from src.services.messages.store import load_history, load_rows
from src.services.turns.engine import TurnEngine, set_turn_engine_for_tests
from src.services.turns.guard import _mid_reply
from src.services.turns.plan_options import (
    NoPendingOptionsError,
    find_pending,
    newest_card,
    record_build_failure,
    record_build_started,
    resolve,
    resolve_pending_as_refine,
)
from tests.factories import ConversationFactory, UserFactory

_CTX = PromptContext(user_name="Ada", project_name="Visitors", project_description=None)

_PLAN_TEXT = "Here is the plan:\n1. Add a table\n2. Wire the form\n3. Ship it"
_QUESTION_TEXT = "A couple of options exist.\nWhich fields should the visitors table hold?"


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


async def _plan_conversation(db_session):
    user = await UserFactory.create(db_session)
    conv = await ConversationFactory.create(db_session, user.id, mode=ConversationMode.PLAN)
    return user, conv


async def _run_turn(engine, db_session, session_factory, model, user, conv):
    async def _noop() -> None:
        return None

    await engine.start_turn(
        conversation=conv,
        user_id=user.id,
        prompt="plan the visitors app",
        history=[],
        prompt_context=_CTX,
        app_id=None,
        project_id=conv.project_id,
        model=model,
        session_factory=session_factory,
        persist_user_turn=_noop,
        manager=SessionManager(),
    )
    state = engine.peek(conv.id)
    assert state is not None and state.task is not None
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(state.task, timeout=10)
    return state


def _call_options(call_id: str = "opt-1") -> DeltaToolCalls:
    return DeltaToolCalls(
        {0: DeltaToolCall(name="present_plan_options", json_args="{}", tool_call_id=call_id)}
    )


async def test_plan_text_plus_deferred_call_is_the_happy_path(
    _fresh_engine, db_session, session_factory
) -> None:
    calls = {"count": 0}

    async def _stream(messages: list[ModelMessage], info: AgentInfo):
        calls["count"] += 1
        yield _PLAN_TEXT + "\n"
        yield _call_options()

    user, conv = await _plan_conversation(db_session)
    state = await _run_turn(
        _fresh_engine,
        db_session,
        session_factory,
        FunctionModel(stream_function=_stream),
        user,
        conv,
    )
    assert state.status == "completed"
    assert calls["count"] == 1  # tool_choice auto — no retry on the happy path

    # The live card frame rode the stream (pending).
    cards = [f for f in state.ring if f.type == "plan_options"]
    assert len(cards) == 1 and cards[0].item.state == "pending"
    assert cards[0].item.tool_call_id == "opt-1"

    # The pending row carries the plan-time pin meta; the projection derives pending.
    pending = await find_pending(db_session, user_id=user.id, conversation_id=conv.id)
    assert pending is not None and pending.tool_call_id == "opt-1"
    assert pending.synthesized is False
    rows = await load_rows(db_session, user_id=user.id, conversation_id=conv.id)
    items = project_rows(list(rows))
    options = [i for i in items if isinstance(i, PlanOptionsItem)]
    assert len(options) == 1 and options[0].state == "pending"


async def test_refine_resolution_is_idempotent_and_feeds_the_next_run(
    _fresh_engine, db_session, session_factory
) -> None:
    async def _stream(messages: list[ModelMessage], info: AgentInfo):
        yield _PLAN_TEXT + "\n"
        yield _call_options()

    user, conv = await _plan_conversation(db_session)
    await _run_turn(
        _fresh_engine,
        db_session,
        session_factory,
        FunctionModel(stream_function=_stream),
        user,
        conv,
    )

    first = await resolve(
        db_session,
        user_id=user.id,
        conversation_id=conv.id,
        tool_call_id="opt-1",
        choice="refine",
    )
    assert first.already_resolved is False
    second = await resolve(
        db_session,
        user_id=user.id,
        conversation_id=conv.id,
        tool_call_id="opt-1",
        choice="refine",
    )
    assert second.already_resolved is True and second.choice == "refine"

    # The projection reads the SAME record the model will see.
    rows = await load_rows(db_session, user_id=user.id, conversation_id=conv.id)
    options = [i for i in project_rows(list(rows)) if isinstance(i, PlanOptionsItem)]
    assert options[0].state == "refine"

    async def _noop_rehydrate(_attachment_ids):
        raise AssertionError("no attachments in this history")

    history = await load_history(
        db_session, user_id=user.id, conversation_id=conv.id, rehydrate=_noop_rehydrate
    )
    flat = [
        part
        for message in history
        for part in getattr(message, "parts", [])
        if getattr(part, "part_kind", "") == "tool-return"
    ]
    assert any(p.tool_call_id == "opt-1" and p.content == "refine" for p in flat)


async def test_plan_shaped_miss_gets_one_forced_retry(
    _fresh_engine, db_session, session_factory
) -> None:
    seen: list[list[ModelMessage]] = []

    async def _stream(messages: list[ModelMessage], info: AgentInfo):
        seen.append(messages)
        if len(seen) == 1:
            yield _PLAN_TEXT  # narrates the plan, never calls the tool
        else:
            yield _call_options("opt-retry")

    user, conv = await _plan_conversation(db_session)
    state = await _run_turn(
        _fresh_engine,
        db_session,
        session_factory,
        FunctionModel(stream_function=_stream),
        user,
        conv,
    )
    assert state.status == "completed"
    assert len(seen) == 2  # exactly one retry
    # The retry carried the ephemeral nudge as its newest user content…
    nudge_texts = [
        getattr(part, "content", "")
        for part in seen[1][-1].parts
        if getattr(part, "part_kind", "") == "user-prompt"
    ]
    assert any("present_plan_options" in str(text) for text in nudge_texts)
    # …and the nudge never persisted (ModelResponse-only rows).
    rows = await load_rows(db_session, user_id=user.id, conversation_id=conv.id)
    for row in rows:
        for message in row.payload:
            assert "system-note" not in str(message.get("parts", ""))
    # The card exists and is REAL (not synthesized).
    pending = await find_pending(db_session, user_id=user.id, conversation_id=conv.id)
    assert pending is not None and pending.tool_call_id == "opt-retry"
    assert pending.synthesized is False


async def test_retry_cap_synthesizes_the_card(_fresh_engine, db_session, session_factory) -> None:
    async def _stream(messages: list[ModelMessage], info: AgentInfo):
        yield _PLAN_TEXT  # never calls the tool, even when forced

    user, conv = await _plan_conversation(db_session)
    state = await _run_turn(
        _fresh_engine,
        db_session,
        session_factory,
        FunctionModel(stream_function=_stream),
        user,
        conv,
    )
    assert state.status == "completed"
    pending = await find_pending(db_session, user_id=user.id, conversation_id=conv.id)
    assert pending is not None and pending.synthesized is True

    # The synthesized card projects pending, resolves as refine, and projects refine —
    # without ever touching the wire history.
    cards = [f for f in state.ring if f.type == "plan_options"]
    assert len(cards) == 1 and cards[0].item.tool_call_id == pending.tool_call_id
    resolution = await resolve(
        db_session,
        user_id=user.id,
        conversation_id=conv.id,
        tool_call_id=pending.tool_call_id,
        choice="refine",
    )
    assert resolution.already_resolved is False
    rows = await load_rows(
        db_session, user_id=user.id, conversation_id=conv.id, include_hidden=True
    )
    options = [i for i in project_rows(list(rows)) if isinstance(i, PlanOptionsItem)]
    assert options[0].state == "refine"
    for row in rows:  # nothing plan-options-shaped entered any payload
        for message in row.payload:
            assert "present_plan_options" not in str(message.get("parts", ""))


async def test_clarifying_question_turns_are_never_retried(
    _fresh_engine, db_session, session_factory
) -> None:
    calls = {"count": 0}

    async def _stream(messages: list[ModelMessage], info: AgentInfo):
        calls["count"] += 1
        yield _QUESTION_TEXT

    user, conv = await _plan_conversation(db_session)
    state = await _run_turn(
        _fresh_engine,
        db_session,
        session_factory,
        FunctionModel(stream_function=_stream),
        user,
        conv,
    )
    assert state.status == "completed"
    assert calls["count"] == 1  # a question is a legitimate planning turn — no forcing
    assert await find_pending(db_session, user_id=user.id, conversation_id=conv.id) is None


async def test_only_the_newest_pending_is_actionable(
    _fresh_engine, db_session, session_factory
) -> None:
    round_ = {"n": 0}

    async def _stream(messages: list[ModelMessage], info: AgentInfo):
        round_["n"] += 1
        yield _PLAN_TEXT + "\n"
        yield _call_options(f"opt-{round_['n']}")

    user, conv = await _plan_conversation(db_session)
    model = FunctionModel(stream_function=_stream)
    await _run_turn(_fresh_engine, db_session, session_factory, model, user, conv)
    # The user keeps typing → implicit refine resolves card 1; a second plan presents card 2.
    implicit = await resolve_pending_as_refine(
        db_session, user_id=user.id, conversation_id=conv.id
    )
    assert implicit is not None and implicit.choice == "refine"
    engine2 = TurnEngine()
    set_turn_engine_for_tests(engine2)
    await _run_turn(engine2, db_session, session_factory, model, user, conv)

    pending = await find_pending(db_session, user_id=user.id, conversation_id=conv.id)
    assert pending is not None and pending.tool_call_id == "opt-2"
    with pytest.raises(NoPendingOptionsError):
        await resolve(
            db_session,
            user_id=user.id,
            conversation_id=conv.id,
            tool_call_id="opt-missing",
            choice="refine",
        )
    # Resolving the SUPERSEDED card is refused; its stored resolution (refine) answers
    # idempotently instead.
    superseded = await resolve(
        db_session,
        user_id=user.id,
        conversation_id=conv.id,
        tool_call_id="opt-1",
        choice="refine",
    )
    assert superseded.already_resolved is True


async def test_build_failed_resolution_rearms_the_card(
    _fresh_engine, db_session, session_factory
) -> None:
    async def _stream(messages: list[ModelMessage], info: AgentInfo):
        yield _PLAN_TEXT + "\n"
        yield _call_options()

    user, conv = await _plan_conversation(db_session)
    await _run_turn(
        _fresh_engine,
        db_session,
        session_factory,
        FunctionModel(stream_function=_stream),
        user,
        conv,
    )
    await resolve(
        db_session,
        user_id=user.id,
        conversation_id=conv.id,
        tool_call_id="opt-1",
        choice="build_failed",
        reason="lock_held",
    )
    rows = await load_rows(db_session, user_id=user.id, conversation_id=conv.id)
    options = [i for i in project_rows(list(rows)) if isinstance(i, PlanOptionsItem)]
    assert options[0].state == "build_failed"
    assert options[0].reason == "lock_held"  # the card re-arms; never resolved-with-no-build


async def test_keep_refining_after_build_failed_is_recordable(
    _fresh_engine, db_session, session_factory
) -> None:
    # A re-armed build_failed card is NOT terminal: clicking "Keep refining" (or an implicit
    # free-text refine) must record, not read back build_failed as if the card were resolved.
    async def _stream(messages: list[ModelMessage], info: AgentInfo):
        yield _PLAN_TEXT + "\n"
        yield _call_options()

    user, conv = await _plan_conversation(db_session)
    await _run_turn(
        _fresh_engine,
        db_session,
        session_factory,
        FunctionModel(stream_function=_stream),
        user,
        conv,
    )
    await record_build_failure(
        db_session,
        user_id=user.id,
        conversation_id=conv.id,
        tool_call_id="opt-1",
        reason="lock_held",
    )
    # The card is still open — find_pending returns it, and an explicit refine records.
    pending = await find_pending(db_session, user_id=user.id, conversation_id=conv.id)
    assert pending is not None and pending.tool_call_id == "opt-1"

    resolution = await resolve(
        db_session,
        user_id=user.id,
        conversation_id=conv.id,
        tool_call_id="opt-1",
        choice="refine",
    )
    assert resolution.already_resolved is False  # it RECORDED, not replayed build_failed
    assert resolution.choice == "refine"

    rows = await load_rows(
        db_session, user_id=user.id, conversation_id=conv.id, include_hidden=True
    )
    options = [i for i in project_rows(list(rows)) if isinstance(i, PlanOptionsItem)]
    assert options[0].state == "refine"  # the later refine overlay supersedes build_failed


async def test_build_started_after_a_raced_refine_writes_no_second_wire_return(
    _fresh_engine, db_session, session_factory
) -> None:
    # The Build-it vs turn-start race (#2): a concurrent turn-start resolves the card as
    # "refine" (a real ToolReturnPart) while the build is starting. record_build_started must
    # then record the build as a system overlay — NOT a second ToolReturnPart — so the loaded
    # history carries exactly one return for the card and the conversation never wedges.
    async def _stream(messages: list[ModelMessage], info: AgentInfo):
        yield _PLAN_TEXT + "\n"
        yield _call_options()

    user, conv = await _plan_conversation(db_session)
    await _run_turn(
        _fresh_engine,
        db_session,
        session_factory,
        FunctionModel(stream_function=_stream),
        user,
        conv,
    )
    # The racing writer's implicit refine (a real wire return for the card).
    await resolve(
        db_session, user_id=user.id, conversation_id=conv.id, tool_call_id="opt-1", choice="refine"
    )
    rows = await load_rows(
        db_session, user_id=user.id, conversation_id=conv.id, include_hidden=True
    )
    card = newest_card(list(rows))
    assert card is not None
    # Build-it re-checks and finds the card already answered → overlay, not a 2nd return.
    await record_build_started(
        db_session,
        user_id=user.id,
        conversation_id=conv.id,
        pending=card,
        answered_already=True,
    )

    async def _noop_rehydrate(_attachment_ids):
        raise AssertionError("no attachments in this history")

    history = await load_history(
        db_session, user_id=user.id, conversation_id=conv.id, rehydrate=_noop_rehydrate
    )
    returns = [
        part
        for message in history
        for part in getattr(message, "parts", [])
        if getattr(part, "part_kind", "") == "tool-return" and part.tool_call_id == "opt-1"
    ]
    assert len(returns) == 1  # exactly one wire return — no duplicate tool_result to reject
    # NEWEST WINS BY ROW SEQ. The build overlay was recorded AFTER the raced refine return, so
    # the card projects "build" — which is the truth the user can see (a build is running).
    # The merge used to be source-ordered, so the earlier refine silently outranked the later
    # overlay and the card claimed the user had chosen to keep refining.
    fresh_rows = await load_rows(
        db_session, user_id=user.id, conversation_id=conv.id, include_hidden=True
    )
    projected = [i for i in project_rows(list(fresh_rows)) if isinstance(i, PlanOptionsItem)]
    assert projected[0].state == "build"
