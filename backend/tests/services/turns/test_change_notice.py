"""The one push that survives: a persisted row saying the app crossed between working and not.

Everything else the platform used to tell the model about the app's state is a pull now. This
is the exception, and it is narrow on purpose — the expensive case, where the platform KNOWS the
app is down and the citizen is asking why nothing happened. The properties that make it safe are
all here:

- it is written at the TURN'S END, so it replays behind the citizen's persisted prompt rather
  than ahead of it (a row written at turn start would break every later turn's prefix);
- it is EDGE-TRIGGERED between KNOWN states, so a still-broken app is not re-announced and an
  unanswerable probe announces nothing in either position;
- the citizen reads it as an ordinary system line.

The readings are driven through the REGISTERED TOOL rather than by setting the turn state: the
thing under test is whether the fact the model was handed is the fact the turn acted on, and a
test that assigns `state.app_reading` itself proves only that a dataclass holds values.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Iterator
from typing import Any

import pytest
import sqlalchemy as sa
from pydantic import SecretStr
from pydantic_ai.messages import ModelMessage, ModelRequest, UserPromptPart
from pydantic_ai.models.function import AgentInfo, DeltaToolCall, DeltaToolCalls, FunctionModel

from src.config import settings
from src.db.models.conversation import ChatKind
from src.db.models.harness_counter import HarnessCount, HarnessCounter
from src.db.models.message import Message, MessageEntryKind
from src.services.agent import toolsets as toolsets_module
from src.services.agent.mode_prompts import PromptContext
from src.services.build_sessions.manager import SessionManager
from src.services.messages.projection import (
    APP_CHANGE_NOTICE_KIND,
    APP_STATE_META_KEY,
    TURN_TERMINAL_KIND,
    AssistantTextItem,
    project_rows,
)
from src.services.messages.store import append_batch, load_history
from src.services.orchestrator.selfheal import AppState
from src.services.sandbox.config import SandboxConfig
from src.services.turns.copy import APP_STOPPED_WORKING_TEXT, APP_WORKING_AGAIN_TEXT
from src.services.turns.engine import TurnEngine, set_turn_engine_for_tests
from src.services.turns.guard import _mid_reply
from tests.factories import ConversationFactory, UserFactory
from tests.fakes import FakeSandboxClient

_CTX = PromptContext(user_name="Ada", project_name="Visitors", project_description=None)


@pytest.fixture(autouse=True)
def _sandbox_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every turn pins the project's live container; without a configured deployment the turn
    dies at the workspace pin and every assertion below would hold over an empty table."""
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
async def _sandbox_dependencies(fake_redis, fake_storage) -> None:
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
def session_factory(db_session):
    @contextlib.asynccontextmanager
    async def _session():
        yield db_session

    return lambda: _session()


@pytest.fixture
def scripted_probe(monkeypatch: pytest.MonkeyPatch):
    """What `check_the_app` finds, scripted per turn.

    Patched at the probe rather than at the tool: the tool's own plumbing — the memo, the
    callback that hands the reading to the turn — is part of what is under test here, and a
    stubbed tool would skip exactly that."""
    scripted: list[AppState] = []

    async def _read(*_args, **_kwargs) -> AppState:
        return scripted[-1]

    monkeypatch.setattr(toolsets_module, "read_the_app_state", _read)
    return scripted


def _looking_model() -> FunctionModel:
    """A turn that calls `check_the_app` and then answers."""
    steps: Iterator[int] = iter(range(2))

    async def _stream(_messages: list[ModelMessage], _info: AgentInfo):
        if next(steps, 1) == 0:
            yield DeltaToolCalls(
                {0: DeltaToolCall(name="check_the_app", json_args="{}", tool_call_id="look")}
            )
        else:
            yield "had a look."

    return FunctionModel(stream_function=_stream)


def _incurious_model() -> FunctionModel:
    """A turn that answers from history and never asks the platform anything."""

    async def _stream(_messages: list[ModelMessage], _info: AgentInfo):
        yield "it still works, I think."

    return FunctionModel(stream_function=_stream)


async def _a_turn(
    engine: TurnEngine,
    db_session,
    session_factory,
    conversation,
    user_id: uuid.UUID,
    prompt: str,
    model: FunctionModel,
) -> None:
    """One real turn, driven the way the send route drives it — prompt row first, then the run."""

    async def _persist_prompt() -> None:
        await append_batch(
            db_session,
            user_id=user_id,
            conversation_id=conversation.id,
            messages=[ModelRequest(parts=[UserPromptPart(content=prompt)])],
            entry_kind=MessageEntryKind.TURN,
            kind=ChatKind.PLAN,
        )

    history = await load_history(
        db_session,
        user_id=user_id,
        conversation_id=conversation.id,
        rehydrate=_no_refs,
    )
    await engine.start_turn(
        conversation=conversation,
        user_id=user_id,
        prompt=prompt,
        history=history,
        prompt_context=_CTX,
        app_id=None,
        project_id=conversation.project_id,
        manager=SessionManager(),
        model=model,
        session_factory=session_factory,
        persist_user_turn=_persist_prompt,
        sandbox_client=FakeSandboxClient(),
    )
    state = engine.peek(conversation.id)
    assert state is not None and state.task is not None
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(state.task, timeout=10)


async def _no_refs(attachment_ids) -> dict[str, tuple[str, str]]:
    raise AssertionError(f"unexpected rehydration of {list(attachment_ids)!r}")


async def _rows(db_session, conversation_id: uuid.UUID) -> list[Message]:
    return list(
        (
            await db_session.execute(
                sa.select(Message)
                .where(Message.conversation_id == conversation_id)
                .order_by(Message.seq)
            )
        )
        .scalars()
        .all()
    )


def _notices(rows: list[Message]) -> list[dict[str, Any]]:
    """The change-notice rows' `meta`, in order."""
    return [
        row.meta
        for row in rows
        if isinstance(row.meta, dict) and row.meta.get("kind") == APP_CHANGE_NOTICE_KIND
    ]


async def _a_conversation(db_session):
    user = await UserFactory.create(db_session)
    conversation = await ConversationFactory.create(db_session, user.id, kind=ChatKind.PLAN)
    return user, conversation


# --- the edge ------------------------------------------------------------------------------


async def test_an_app_that_stops_serving_lands_exactly_one_notice(
    _fresh_engine, db_session, session_factory, scripted_probe
) -> None:
    """★ THE CASE THE PUSH EXISTS FOR: the citizen says nothing happened, and the platform
    already knows the app is down."""
    user, conv = await _a_conversation(db_session)

    scripted_probe.append(AppState.LIVE)
    await _a_turn(
        _fresh_engine, db_session, session_factory, conv, user.id, "add a chart", _looking_model()
    )
    scripted_probe.append(AppState.NOT_SERVING)
    await _a_turn(
        _fresh_engine,
        db_session,
        session_factory,
        conv,
        user.id,
        "nothing changed",
        _looking_model(),
    )

    notices = _notices(await _rows(db_session, conv.id))
    assert len(notices) == 1
    assert notices[0]["text"] == "Your app stopped working after the last change."


async def test_the_notice_lands_behind_the_prompt_it_is_about_and_before_the_terminal(
    _fresh_engine, db_session, session_factory, scripted_probe
) -> None:
    """★ THE REASON THE TIMING IS FORCED. `persist_user_turn` runs before the turn has taken any
    reading, so a notice written at turn START would sit AHEAD of the citizen's persisted prompt
    — and next turn the prompt replays with the notice's bytes gone from in front of it, which
    shifts every message after them and misses the cache on the whole request.

    Mutation check: move the `_write_change_notice` call from the `finally` to just after
    `persist_user_turn` and the first assertion goes red."""
    user, conv = await _a_conversation(db_session)
    scripted_probe.append(AppState.LIVE)
    await _a_turn(
        _fresh_engine, db_session, session_factory, conv, user.id, "add a chart", _looking_model()
    )
    scripted_probe.append(AppState.NOT_SERVING)
    await _a_turn(
        _fresh_engine, db_session, session_factory, conv, user.id, "it is blank", _looking_model()
    )

    rows = await _rows(db_session, conv.id)
    notice_seq = next(
        row.seq
        for row in rows
        if isinstance(row.meta, dict) and row.meta.get("kind") == APP_CHANGE_NOTICE_KIND
    )
    prompts = [
        row.seq
        for row in rows
        if row.entry_kind is MessageEntryKind.TURN
        and any(
            part.get("part_kind") == "user-prompt"
            for message in row.payload
            if isinstance(message, dict)
            for part in message.get("parts", [])
            if isinstance(part, dict)
        )
    ]
    assert prompts, "the citizen's own prompt rows are missing — the fixture, not the notice"
    assert notice_seq > max(prompts)

    terminals = [
        row.seq
        for row in rows
        if isinstance(row.meta, dict) and row.meta.get("kind") == TURN_TERMINAL_KIND
    ]
    assert notice_seq < max(terminals), (
        "the notice landed after this turn's terminal row, which carries the very reading it "
        "compares against"
    )


async def test_an_unchanged_app_lands_nothing(
    _fresh_engine, db_session, session_factory, scripted_probe
) -> None:
    """EDGE-TRIGGERED, NOT LEVEL-TRIGGERED. An app that is still down next turn has not changed
    again, and a notice repeated every turn is the nagging this whole track removes."""
    user, conv = await _a_conversation(db_session)
    for prompt in ("add a chart", "and a filter", "and sort it"):
        scripted_probe.append(AppState.NOT_SERVING)
        await _a_turn(
            _fresh_engine, db_session, session_factory, conv, user.id, prompt, _looking_model()
        )

    assert _notices(await _rows(db_session, conv.id)) == []


async def test_the_first_reading_in_a_conversation_announces_nothing(
    _fresh_engine, db_session, session_factory, scripted_probe
) -> None:
    """There is nothing on record for it to differ from, and "the app is down" is not the same
    statement as "the app stopped working after the last change"."""
    user, conv = await _a_conversation(db_session)
    scripted_probe.append(AppState.NOT_SERVING)
    await _a_turn(
        _fresh_engine,
        db_session,
        session_factory,
        conv,
        user.id,
        "build me an app",
        _looking_model(),
    )

    rows = await _rows(db_session, conv.id)
    assert _notices(rows) == []
    # LIVENESS: the turn really did take a reading, so the absence above is the edge rule and
    # not a turn that quietly never looked.
    stamped = [
        row.meta[APP_STATE_META_KEY]
        for row in rows
        if isinstance(row.meta, dict) and APP_STATE_META_KEY in row.meta
    ]
    assert stamped == [AppState.NOT_SERVING.value]


async def test_a_reading_the_probe_could_not_answer_is_no_reading(
    _fresh_engine, db_session, session_factory, scripted_probe
) -> None:
    """★ `unknown` IS THE PROBE FAILING, NOT THE APP CHANGING — in either position.

    It announces nothing when it is the newest reading, and it does not displace what was last
    known, so the real reading that follows it is still measured against the last real one.

    Mutation check: make `_is_serving` answer `False` for `AppState.UNKNOWN` and the first
    assertion goes red (the blink is announced as a crash)."""
    user, conv = await _a_conversation(db_session)
    scripted_probe.append(AppState.LIVE)
    await _a_turn(
        _fresh_engine, db_session, session_factory, conv, user.id, "add a chart", _looking_model()
    )
    scripted_probe.append(AppState.UNKNOWN)
    await _a_turn(
        _fresh_engine, db_session, session_factory, conv, user.id, "and a filter", _looking_model()
    )
    assert _notices(await _rows(db_session, conv.id)) == []

    scripted_probe.append(AppState.NOT_SERVING)
    await _a_turn(
        _fresh_engine, db_session, session_factory, conv, user.id, "it is blank", _looking_model()
    )
    notices = _notices(await _rows(db_session, conv.id))
    assert len(notices) == 1
    assert notices[0]["text"] == "Your app stopped working after the last change."


async def test_an_unknown_already_on_record_announces_nothing_either(
    _fresh_engine, db_session, session_factory, scripted_probe
) -> None:
    """The other half of the same rule, on a row this engine does not write: both sides of the
    comparison have to be KNOWN, so a stored `unknown` blocks rather than being read as a state
    the app was once in."""
    user, conv = await _a_conversation(db_session)
    await append_batch(
        db_session,
        user_id=user.id,
        conversation_id=conv.id,
        messages=[],
        entry_kind=MessageEntryKind.SYSTEM_EVENT,
        kind=ChatKind.PLAN,
        meta={"kind": TURN_TERMINAL_KIND, APP_STATE_META_KEY: AppState.UNKNOWN.value},
    )
    scripted_probe.append(AppState.NOT_SERVING)
    await _a_turn(
        _fresh_engine, db_session, session_factory, conv, user.id, "it is blank", _looking_model()
    )

    assert _notices(await _rows(db_session, conv.id)) == []


async def test_an_app_that_comes_back_says_so(
    _fresh_engine, db_session, session_factory, scripted_probe
) -> None:
    """Both edges ship together. A notice that only ever reports the bad crossing leaves the last
    thing on record saying the app is broken, which the assistant then repeats at someone whose
    app is fine."""
    user, conv = await _a_conversation(db_session)
    scripted_probe.append(AppState.NOT_SERVING)
    await _a_turn(
        _fresh_engine, db_session, session_factory, conv, user.id, "add a chart", _looking_model()
    )
    scripted_probe.append(AppState.NOT_SERVING)
    await _a_turn(
        _fresh_engine, db_session, session_factory, conv, user.id, "try again", _looking_model()
    )
    scripted_probe.append(AppState.LIVE)
    await _a_turn(
        _fresh_engine, db_session, session_factory, conv, user.id, "and again", _looking_model()
    )

    notices = _notices(await _rows(db_session, conv.id))
    assert [notice["text"] for notice in notices] == ["Your app is working again."]


async def test_the_starter_page_still_counts_as_serving(
    _fresh_engine, db_session, session_factory, scripted_probe
) -> None:
    """An app that answers but still shows the starter template is a working app that has not
    been built yet — a different sentence and a different unit's problem. Announcing "your app
    stopped working" at that citizen would be false."""
    user, conv = await _a_conversation(db_session)
    scripted_probe.append(AppState.LIVE)
    await _a_turn(
        _fresh_engine, db_session, session_factory, conv, user.id, "add a chart", _looking_model()
    )
    scripted_probe.append(AppState.STILL_THE_TEMPLATE)
    await _a_turn(
        _fresh_engine, db_session, session_factory, conv, user.id, "and a filter", _looking_model()
    )

    assert _notices(await _rows(db_session, conv.id)) == []


# --- what the citizen and the model each get ------------------------------------------------


async def test_the_notice_renders_in_the_feed_as_an_ordinary_system_line(
    _fresh_engine, db_session, session_factory, scripted_probe
) -> None:
    """The citizen reads the sentence. It is drawn like any other platform line — no card, no
    banner, nothing to press — so it is reachable and announced exactly as the rest of the
    transcript is."""
    user, conv = await _a_conversation(db_session)
    scripted_probe.append(AppState.LIVE)
    await _a_turn(
        _fresh_engine, db_session, session_factory, conv, user.id, "add a chart", _looking_model()
    )
    scripted_probe.append(AppState.NOT_SERVING)
    await _a_turn(
        _fresh_engine, db_session, session_factory, conv, user.id, "it is blank", _looking_model()
    )

    items = project_rows(await _rows(db_session, conv.id))
    spoken = [
        item.text
        for item in items
        if isinstance(item, AssistantTextItem) and item.text == APP_STOPPED_WORKING_TEXT
    ]
    assert spoken == [APP_STOPPED_WORKING_TEXT]


async def test_the_model_reads_the_notice_as_something_it_was_told(
    _fresh_engine, db_session, session_factory, scripted_probe
) -> None:
    """★ NOT AS SOMETHING IT SAID. The statement is the platform's, and replaying it as assistant
    text would put a claim about the app in the model's own mouth — which is how a transcript
    ends up with the assistant confidently repeating a fact nobody gave it."""
    user, conv = await _a_conversation(db_session)
    scripted_probe.append(AppState.LIVE)
    await _a_turn(
        _fresh_engine, db_session, session_factory, conv, user.id, "add a chart", _looking_model()
    )
    scripted_probe.append(AppState.NOT_SERVING)
    await _a_turn(
        _fresh_engine, db_session, session_factory, conv, user.id, "it is blank", _looking_model()
    )

    replayed = await load_history(
        db_session, user_id=user.id, conversation_id=conv.id, rehydrate=_no_refs
    )
    said = [
        part.content
        for message in replayed
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, UserPromptPart) and isinstance(part.content, str)
    ]
    assert APP_STOPPED_WORKING_TEXT in said


def test_both_sentences_are_the_ones_the_plan_wrote() -> None:
    """Pinned as literals, here rather than only through the constants the engine reads, so an
    edit to either sentence is a deliberate one."""
    assert APP_STOPPED_WORKING_TEXT == "Your app stopped working after the last change."
    assert APP_WORKING_AGAIN_TEXT == "Your app is working again."


# --- detection ------------------------------------------------------------------------------


async def _counted(db_session, counter: HarnessCounter) -> int:
    return len(
        (
            await db_session.execute(
                sa.select(HarnessCount.id).where(HarnessCount.name == counter.value)
            )
        )
        .scalars()
        .all()
    )


async def test_the_counters_separate_a_turn_that_looked_from_one_that_did_not(
    _fresh_engine, db_session, session_factory, scripted_probe
) -> None:
    """★ BOTH SIDES ARE TOOL-CALL FACTS. The incurious turn below answers in prose that sounds
    exactly like a report about the app, and it counts as a turn that never looked — because
    nothing here reads what anybody said.

    Counted as a DELTA rather than an absolute, since these counters are deployment-wide and the
    test database carries whatever earlier tests left behind."""
    user, conv = await _a_conversation(db_session)
    took_before = await _counted(db_session, HarnessCounter.APP_READING_TAKEN)
    missed_before = await _counted(db_session, HarnessCounter.APP_READING_MISSING)

    scripted_probe.append(AppState.LIVE)
    await _a_turn(
        _fresh_engine, db_session, session_factory, conv, user.id, "add a chart", _looking_model()
    )
    await _a_turn(
        _fresh_engine,
        db_session,
        session_factory,
        conv,
        user.id,
        "does it work?",
        _incurious_model(),
    )

    assert await _counted(db_session, HarnessCounter.APP_READING_TAKEN) == took_before + 1
    assert await _counted(db_session, HarnessCounter.APP_READING_MISSING) == missed_before + 1


async def test_a_turn_that_wrote_nothing_leaves_no_record_that_somebody_built_here(
    _fresh_engine, db_session, session_factory, scripted_probe
) -> None:
    """★ THE BUILD RECORD IS WRITTEN FOR A BUILD, NEVER FOR A LOOK.

    `WORKSPACE_WAS_WRITTEN` means "somebody built here" and the row is permanent, so a turn that
    merely HELD the workspace — a Plan turn, a turn that stopped before writing, a turn that only
    looked — must leave none. One written for a look would say a project nobody has built in has
    been, and nothing later can take it back.

    The reading counters beside it are deliberately still written here: they measure whether the
    turn looked, which this turn did.

    Mutation check: gate the write on `state.write_session` instead of `workspace_touched` and
    this goes red while the reading assertion above stays green."""
    user, conv = await _a_conversation(db_session)
    built_before = await _counted(db_session, HarnessCounter.WORKSPACE_WAS_WRITTEN)
    took_before = await _counted(db_session, HarnessCounter.APP_READING_TAKEN)

    scripted_probe.append(AppState.LIVE)
    await _a_turn(
        _fresh_engine,
        db_session,
        session_factory,
        conv,
        user.id,
        "how does it look?",
        _looking_model(),
    )

    assert await _counted(db_session, HarnessCounter.WORKSPACE_WAS_WRITTEN) == built_before
    # Liveness: the turn really did reach its terminal, so the absence above is an absence
    # rather than a turn that never counted anything at all.
    assert await _counted(db_session, HarnessCounter.APP_READING_TAKEN) == took_before + 1
