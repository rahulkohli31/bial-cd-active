"""TurnEngine lifecycle tests (U10): detached runs, frame ring, snapshot consolidation,
stop semantics, and the write-before-DONE policy — all at the engine seam with scripted
models (no HTTP; the transport rides `tests/api/v1/conversations/test_turn_stream.py`).

Two later subjects live down the bottom, both about what a turn SAYS about itself rather than
what it produces: the opening acknowledgement and its retraction, and reasoning — which is
requested per kind and is allowed to become exactly one thing on the way out, a working flag.
The one test here that does NOT use a scripted model is the last: the provider's refusal of
budget thinking lives in `AnthropicModel.prepare_request`, which a double never executes."""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
import warnings
from typing import Any

import pytest
import sqlalchemy as sa
from pydantic import SecretStr
from pydantic_ai.exceptions import UserError
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.models.anthropic import AnthropicModel, AnthropicModelSettings
from pydantic_ai.models.function import (
    AgentInfo,
    DeltaThinkingPart,
    DeltaToolCall,
    DeltaToolCalls,
    FunctionModel,
)
from pydantic_ai.providers.anthropic import AnthropicProvider

from src.api.v1.conversations.schemas import StepFrame, TurnStepPart, TurnTextPart
from src.config import settings
from src.db.models.conversation import ChatKind
from src.db.models.message import Message, MessageEntryKind, MessageVisibility
from src.db.models.token_usage import TokenUsage
from src.services.agent.mode_prompts import PromptContext
from src.services.build_sessions.manager import SessionManager, StopOutcome
from src.services.messages.projection import (
    PLAN_OPTIONS_TOOL,
    TURN_TERMINAL_KIND,
    project_rows,
)
from src.services.orchestrator.constants import (
    ADAPTIVE_THINKING,
    BUILD_EFFORT,
    MAX_OUTPUT_TOKENS,
    PLAN_EFFORT,
    TEMPERATURE,
)
from src.services.sandbox.config import SandboxConfig
from src.services.turns import engine as engine_module
from src.services.turns.copy import WRITING_UP_THE_PLAN_LABEL
from src.services.turns.engine import (
    ACK_TEXT,
    ACK_TOOL_CALL_ID,
    TurnEngine,
    _persistable_messages,
    _TurnState,
    set_turn_engine_for_tests,
)
from src.services.turns.guard import ConversationBusyError, _mid_reply
from tests.factories import ConversationFactory, UserFactory
from tests.fakes import FakeSandboxClient

_CTX = PromptContext(user_name="Ada", project_name="Visitors", project_description=None)


@pytest.fixture(autouse=True)
def _sandbox_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    # Every kind pins the project's LIVE container now (R18) — Plan and Ask attach a sandbox
    # exactly like Build does, so these engine-level tests need a configured deployment the
    # same way `test_write_turn.py` already does, or every turn dies at the workspace pin
    # before the model ever runs.
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
    """The R10 liveness lease (Redis) and the sandbox attach's storage reads both need a
    backing fake now that every turn attaches a live container. Pulled in as an autouse
    wrapper around the shared `fake_redis`/`fake_storage` fixtures (`tests/conftest.py`)
    rather than added to every test signature — same effect, none of the churn."""
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


async def _conversation(db_session, kind: ChatKind = ChatKind.PLAN):
    user = await UserFactory.create(db_session)
    conv = await ConversationFactory.create(db_session, user.id, kind=kind)
    return user, conv


def _streaming_text(*chunks: str):
    async def _stream(messages: list[ModelMessage], info: AgentInfo):
        for chunk in chunks:
            yield chunk

    return FunctionModel(stream_function=_stream)


async def _start(engine: TurnEngine, db_session, session_factory, model, *, kind=ChatKind.PLAN):
    user, conv = await _conversation(db_session, kind)
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
        sandbox_client=FakeSandboxClient(),
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
    # U17 — EVERY turn now opens with the harness's acknowledgement, emitted synchronously at
    # `start_turn` before the detached run exists. It is a transient feed row (never persisted,
    # never in `state.steps`), so it shows up here in the ring and nowhere durable.
    #
    # R18 — every kind now pins the project's LIVE container (Plan/Ask attach a sandbox
    # exactly like Build), so the ack is followed by the workspace lifecycle pair
    # (preparing/ready), a compile-state read and a preview-url announce, all BEFORE the
    # model's own text — the same boilerplate a Build turn always carried.
    #
    # THE SECOND `step` IS THE ACKNOWLEDGEMENT BEING TAKEN BACK (U3), and it lands immediately
    # before the first word: the same `tool_call_id`, `phase="finished"`, `hidden` flipped, which
    # is how a row leaves a feed that already filters hidden steps. Without it the opening line
    # sat in the activity group forever and the group never sealed. Asserted here rather than
    # only in its own test because the POSITION is the claim — it is retired as the answer
    # begins, not at the terminal.
    #
    # TWO `text_delta`s, ONE PER DELTA, and that is the hold being gone. Prose used to be
    # accumulated and released in a single frame once the response proved it called no tool,
    # which cost the token-by-token reveal on every turn in both kinds. Nothing is held now:
    # each delta goes out as it lands.
    assert [f.type for f in frames] == [
        "step",
        "workspace",
        "workspace",
        "compile",
        "preview",
        "step",
        "text_delta",
        "text_delta",
        "turn_ended",
    ]
    # ONE BLOCK, THOUGH — the first delta opens it and the second continues it, which is the
    # whole of what `new_block` carries and what makes a stretch of writing render as one
    # paragraph rather than one per token.
    deltas = [f for f in frames if f.type == "text_delta"]
    assert [f.new_block for f in deltas] == [True, False]
    assert state.text_blocks() == ["hello world"]
    # A NONZERO cursor still inside the ring is the resume case the `?turn=&cursor=` route
    # leans on: the tail only, no gap, and nothing at or before the cursor re-delivered.
    tail, tail_gap = engine.frames_since(state, frames[0].seq)
    assert not tail_gap
    assert [f.type for f in tail] == [
        "workspace",
        "workspace",
        "compile",
        "preview",
        "step",
        "text_delta",
        "text_delta",
        "turn_ended",
    ]
    assert all(frame.seq > frames[0].seq for frame in tail)
    # …and a cursor past the ring's newest frame yields nothing at all (settled, replayed).
    assert engine.frames_since(state, frames[-1].seq) == ([], False)
    # WRITE-BEFORE-DONE: the reply row landed (ModelResponse only — the user turn is the
    # route's pre-write, deliberately absent here via the no-op persister).
    rows = (
        await db_session.scalars(
            sa.select(Message).where(Message.conversation_id == conv.id).order_by(Message.seq)
        )
    ).all()
    # TWO ROWS, and the second is U20's durable terminal. The reply is the turn; the terminal
    # is a hidden, payload-less record saying HOW it ended, so a transcript rebuilt without the
    # live stream can tell a finished turn from a running one.
    assert [row.entry_kind for row in rows] == [
        MessageEntryKind.TURN,
        MessageEntryKind.SYSTEM_EVENT,
    ]
    assert rows[0].payload[0]["kind"] == "response"
    # The composed instructions never reach the row (U9's dump-seam strip).
    assert rows[0].payload[0].get("instructions") is None
    assert rows[1].visibility is MessageVisibility.HIDDEN
    # EMPTY PAYLOAD, checked here rather than only in the projection's tests: `load_history`
    # flattens every row's payload including hidden ones, so a terminal row with a message in
    # it would put a blank assistant turn into every later prompt of this conversation.
    assert rows[1].payload == []
    assert rows[1].meta == {
        "kind": TURN_TERMINAL_KIND,
        "turnId": str(state.turn_id),
        "status": "completed",
        "reason": None,
    }


async def test_read_tool_calls_become_step_frames(
    _fresh_engine, db_session, session_factory
) -> None:
    call_id = "call-1"

    async def _stream(messages: list[ModelMessage], info: AgentInfo):
        if len(messages) == 1:
            # First request: call read_file against the freshly-provisioned (empty) fake
            # container — R18 gives even a brand-new project a real one, so this reads the
            # golden template's page, not a synthetic "no app yet" placeholder.
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
    # U17 — the first step frame of any turn is the harness's own acknowledgement row, and U3
    # gave it a second: the retraction that takes it back off the screen. Both are keyed on the
    # reserved ack id, so the agent's own steps are read by id rather than by position — slicing
    # off "the first one" silently swallowed the retraction the moment it was added.
    assert [f.phase for f in _ack_frames(state)] == ["started", "finished"]
    steps = [f for f in state.ring if f.type == "step" and f.tool_call_id == call_id]
    assert [s.phase for s in steps] == ["started", "finished"]
    assert steps[0].item.state == "pending" and steps[1].item.state == "ok"
    # U5 — A READ IS VISIBLE NOW, and this assertion is the inverted twin of the one that
    # stood here ("reads are hidden by default"). The whole class was hidden, which is why a
    # build's activity opened on a write with no account of what the agent had read to get
    # there. `hidden` marks two things now and neither of them is a read: a write to a
    # configuration file, and housekeeping shell commands.
    assert steps[1].item.hidden is False
    # U16 — BOTH OF THESE PINNED THE LEAK (`== "Read app/page.tsx"`), on the LIVE feed and on
    # the resume snapshot. Flipped, not deleted, and each paired with its liveness half: the
    # path is absent AND the friendly area still renders, so a label that collapsed to an empty
    # string could not pass. The two are asserted against the same literal on purpose — live and
    # reload read one translator, and a drift between them is the failure this pair catches.
    assert "app/page.tsx" not in steps[1].item.label
    assert steps[1].item.label == "Looking at your app's main page"
    # A RESUME must not lose a step, whatever its render hint says. `hidden` is applied by the
    # client, not by the payload, and making it a filter HERE once cost a mid-turn reconnect the
    # very steps the live tail and the reload projection both kept. The step in this turn is a
    # read and therefore visible now (U5), so what the snapshot is pinned on below is the pair
    # this test can still prove: the same label live and on resume, and the order.
    #
    # ORDER FIRST, because the snapshot is an ordered list of parts now rather than a step list
    # beside a flat string. The read ran and THEN the model wrote its reply, and a citizen who
    # reattaches has to be handed that sequence — a membership check over the steps alone would
    # pass with the prose ahead of the step it was written after.
    snapshot = engine.build_snapshot(state)
    assert [part.type for part in snapshot.parts] == ["step", "text"]
    assert [part.text for part in snapshot.parts if isinstance(part, TurnTextPart)] == ["done"]
    snapshot_steps = [part for part in snapshot.parts if isinstance(part, TurnStepPart)]
    assert all("app/page.tsx" not in part.item.label for part in snapshot_steps)
    assert [part.item.label for part in snapshot_steps] == ["Looking at your app's main page"]
    # Visible on the resume path too (U5). Live and reload read one translator, so a read that
    # came back on one side and stayed hidden on the other is exactly the drift this pair
    # catches — the same reason the label is asserted twice against one literal.
    assert snapshot_steps[0].item.hidden is False
    # The acknowledgement is NOT among those parts: the read retired it, so a citizen who
    # reattaches after the first step never sees an opening line that is already over.
    assert not any(part.tool_call_id == ACK_TOOL_CALL_ID for part in snapshot_steps)


async def test_the_offer_tools_part_start_event_emits_a_started_step_the_card_then_replaces(
    _fresh_engine, db_session, session_factory
) -> None:
    """U5 — the plan now rides the tool's own argument, so thousands of tokens can stream
    between the block opening and the call resolving. The screen must not go dark for that
    whole window: the provider's `content_block_start` puts the tool's NAME on the wire
    before any argument does, which pydantic-ai surfaces as a `PartStartEvent` carrying a
    `ToolCallPart` with an empty argument — and the engine turns that into a started status
    step. `FunctionToolCallEvent` then REPLACES it with the card, on the SAME `tool_call_id`,
    never both at once: a late subscriber's catch-up snapshot must show the card's row and
    nothing left behind describing the status that preceded it.

    Mutation-check: delete the `ToolCallPart` branch from `_on_event`'s `PartStartEvent` arm
    and the started-step assertions below go red while the completed-run assertions (status,
    card) stay green — proving this test exercises that branch specifically, not just the
    happy path the other plan-options tests already cover."""
    engine = _fresh_engine

    async def _stream(messages: list[ModelMessage], info: AgentInfo):
        # Split across two deltas: the name arrives with an EMPTY argument first — exactly
        # the provider's content_block_start shape — and the argument completes after.
        yield DeltaToolCalls(
            {0: DeltaToolCall(name="present_plan_options", json_args="", tool_call_id="opt-1")}
        )
        yield DeltaToolCalls(
            {
                0: DeltaToolCall(
                    json_args=json.dumps({"plan": "Ship the visitor log."}), tool_call_id="opt-1"
                )
            }
        )

    user, conv, _ = await _start(
        engine, db_session, session_factory, FunctionModel(stream_function=_stream)
    )
    await _settle(engine, conv.id)

    state = engine.peek(conv.id)
    assert state is not None and state.status == "completed"
    ring = list(state.ring)

    step_frames = [f for f in ring if f.type == "step" and f.tool_call_id == "opt-1"]
    plan_frames = [f for f in ring if f.type == "plan_options" and f.item.tool_call_id == "opt-1"]

    # Exactly one started step, never a 'finished' counterpart — the offer DEFERS rather than
    # resolving through the ordinary step lifecycle, so nothing ever closes it out that way.
    assert [f.phase for f in step_frames] == ["started"]
    assert step_frames[0].item.label == WRITING_UP_THE_PLAN_LABEL
    # The frame that opens the block carries no plan text — and now cannot: a step has no
    # field a plan could ride in (U14). Asserted on the plan's own words rather than on the
    # substring "plan", which the tool's name and its label both legitimately contain.
    assert "Ship the visitor log." not in json.dumps(step_frames[0].item.model_dump(mode="json"))

    # The card follows, on the same call id, strictly after the status in wire order.
    assert len(plan_frames) == 1
    assert ring.index(step_frames[0]) < ring.index(plan_frames[0])

    # REPLACED, not accumulated: a client that only ever sees the catch-up snapshot (a late
    # subscribe, or a resume) finds the card's row and nothing describing the status it
    # superseded — never both at once on the same id.
    snapshot = engine.build_snapshot(state)
    assert not any(
        isinstance(part, TurnStepPart) and part.item.tool == PLAN_OPTIONS_TOOL
        for part in snapshot.parts
    )
    # LIVENESS for that absence, and it is the plan itself: the words the offer was called with
    # are pushed as a block of the turn's prose at the call event, just before the card. So the
    # snapshot demonstrably has content, and the withdrawn status is the one thing missing from
    # it rather than everything being missing.
    assert [part.text for part in snapshot.parts if isinstance(part, TurnTextPart)] == [
        "Ship the visitor log."
    ]


async def test_a_part_start_event_for_a_non_offer_tool_emits_no_extra_frame(
    _fresh_engine, db_session, session_factory
) -> None:
    """Deliberately NOT widened to every tool (U5): the other tools resolve fast and already
    emit at `FunctionToolCallEvent`, so widening this branch would double every step row in
    the transcript. Pinned on the RING'S TOTAL FRAME COUNT, not just the step phases, so a
    silent extra frame of any type sneaking in from `PartStartEvent` would be caught too."""
    call_id = "call-1"

    async def _stream(messages: list[ModelMessage], info: AgentInfo):
        if len(messages) == 1:
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
    # Ack opened, ack retired, tool started, tool finished = four step frames, one text delta
    # (the "done" reply) and the terminal — nothing extra rides in from the PartStartEvent that
    # opened this tool's block. The workspace/compile/preview boilerplate (R18 — every kind pins
    # a live container now) is filtered out here: it is unrelated to what THIS test is pinning,
    # and hard-coding its exact shape would make this test fail on a change to that machinery
    # instead of a change to the PartStartEvent branch it actually guards.
    non_lifecycle = [f for f in state.ring if f.type not in {"workspace", "compile", "preview"}]
    assert [f.type for f in non_lifecycle] == [
        "step",
        "step",
        "step",
        "step",
        "text_delta",
        "turn_ended",
    ]


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

    # THIS TEST USED TO PIN A REDACTOR; IT NOW PINS AN ABSENCE, which is the stronger claim and
    # the reason U14 exists. The live frame carried the tool call's arguments, run through
    # `redact_secrets` HERE at the frame boundary so the stream would not show a secret the
    # persistence seam had already masked — and the assertion was that the mask was applied
    # (`"***" in wire`). A boundary redactor is only ever as good as its pattern list, and the
    # thing it was protecting was a payload the browser parsed and rendered nowhere. The
    # arguments are simply not on the frame now, so the secret cannot be masked wrongly, only
    # not sent.
    #
    # `redact_secrets` is NOT retired — `services/messages/store.py` still runs it over every
    # string in the persisted tree, which is where it belongs, and `test_store_roundtrip.py`
    # is what pins that.
    wire = json.dumps([s.item.model_dump(mode="json") for s in steps], ensure_ascii=False)
    assert "sup3rs3cretpw" not in wire
    assert "/etc/secrets" not in wire  # nor the path the call named
    # LIVENESS: the steps genuinely rendered, so the two absences above are about the payload
    # and not about an empty ring.
    assert all(s.item.label.strip() for s in steps)


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
    while not state.text_blocks():  # the first delta proves the run is streaming
        await asyncio.sleep(0.01)

    assert await engine.stop_turn(conv.id, turn_id) is True
    await _settle(engine, conv.id)
    assert state.status == "stopped"
    assert state.ring[-1].type == "turn_ended" and state.ring[-1].status == "stopped"
    # No reply row — none finished; the durable record stays truthful.
    rows = (
        await db_session.scalars(sa.select(Message).where(Message.conversation_id == conv.id))
    ).all()
    # THE USER TURN IS ABSENT (the no-op persister) and no reply row was written — nothing
    # finished. What IS here is U20's terminal, saying the turn stopped: the one durable record
    # a reload can read to know this turn is over rather than still going.
    assert [row.entry_kind for row in rows] == [MessageEntryKind.SYSTEM_EVENT]
    assert rows[0].meta is not None
    assert rows[0].meta["status"] == "stopped"
    assert rows[0].meta["reason"] == "stopped_by_user"
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
    while not state.text_blocks():
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
    while not state.text_blocks():  # request 1 is done; request 2 is streaming
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
    user, conv = await _conversation(db_session, ChatKind.BUILD)
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
    while not state.text_blocks():
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
    # ONE BLOCK holding every chunk, evicted frames included: the deltas all extended the same
    # `TextPart`, so the citizen who fell behind is handed the paragraph whole rather than the
    # tail that happened to survive in the ring.
    assert [part.text for part in snapshot.parts if isinstance(part, TurnTextPart)] == [
        "".join(chunks)
    ]
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


# --- stopping a user's turn so another project can have the workspace --------------


async def test_stop_user_turn_and_wait_settles_before_it_returns(
    _fresh_engine, db_session, session_factory
) -> None:
    """THE CONTRACT `stop_turn` CANNOT OFFER, and the reason this exists beside it.

    `stop_turn` returns the instant `task.cancel()` is issued. The "stop and switch" flow
    cannot act on that: its very next steps save the workspace and tear the container down, and
    a turn that is still unwinding still owns that container — releasing underneath it is the
    strand this whole subsystem is written to prevent. So this one WAITS, and the assertion
    that matters is that the task is genuinely done by the time it hands back.

    It is also keyed on the USER rather than a conversation, because the caller is a project
    switch: the refusal names a project, and a Write turn's manager session carries no
    conversation id to look up.

    UPDATED FOR THE THREE STATES. This used to assert `stopped is True`, which the code returned
    on every path — including the timeout the docstring promised would be reported as still
    running. `STOPPED` is now read off `task.done()`, the same fact the next line checks, so the
    assertion cannot hold for a turn that has not finished unwinding."""
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
    while not state.text_blocks():  # the run is genuinely streaming
        await asyncio.sleep(0.01)

    stopped = await engine.stop_user_turn_and_wait(user.id, timeout_s=10)

    assert stopped is StopOutcome.STOPPED
    # SETTLED, not merely asked to settle — this is the whole difference from `stop_turn`.
    assert state.task is not None and state.task.done()
    assert state.status == "stopped"
    assert state.ring[-1].type == "turn_ended" and state.ring[-1].status == "stopped"
    assert conv.id not in _mid_reply  # the busy guard released with the task


async def test_stop_user_turn_and_wait_finds_nothing_when_nothing_runs(
    _fresh_engine, db_session, session_factory
) -> None:
    """`NOTHING_WAS_RUNNING`, not an error. The caller's goal is "settled" and it already is —
    and this is the COMMON path, because a build usually finishes while the user is still
    reading the dialog. Its own named state rather than the boolean's `False`, which the
    timeout's hardcoded `True` sat beside as an equal and opposite lie."""
    user = await UserFactory.create(db_session, email="stopnone@rvaiglobal.com")
    assert (
        await _fresh_engine.stop_user_turn_and_wait(user.id, timeout_s=5)
        is StopOutcome.NOTHING_WAS_RUNNING
    )


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
    while not state.text_blocks():
        await asyncio.sleep(0.01)

    stranger = await UserFactory.create(db_session, email="stopother@rvaiglobal.com")
    assert (
        await engine.stop_user_turn_and_wait(stranger.id, timeout_s=5)
        is StopOutcome.NOTHING_WAS_RUNNING
    )
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
    while not state.text_blocks():
        await asyncio.sleep(0.01)

    assert await engine.stop_user_turn_and_wait(user.id, timeout_s=10) is StopOutcome.STOPPED
    # The turn has settled, so the repeat finds nothing running — and the terminal survives.
    # `NOTHING_WAS_RUNNING` is the engine's honest answer to a SECOND ask, and it is why the
    # manager keeps its own record of having asked: the hand-over's status read has to keep
    # saying "stopped" across every poll, which the engine alone could not tell it.
    assert (
        await engine.stop_user_turn_and_wait(user.id, timeout_s=10)
        is StopOutcome.NOTHING_WAS_RUNNING
    )
    assert state.ring[-1].type == "turn_ended" and state.ring[-1].status == "stopped"


async def test_a_stop_whose_wait_expires_is_still_running_never_stopped(
    _fresh_engine, db_session, session_factory, monkeypatch
) -> None:
    """*The regression this unit exists for, at the engine seam.* This call used to `return True`
    after its wait expired — the same answer as a clean stop — while its own docstring promised
    that a timeout would read as still running. The caller's next act is to take the container.

    THE TURN IS HELD IN ITS OWN `finally`, not merely cancelled and measured a millisecond later.
    That is the state the whole design turns on: cancellation is a request, and the cleanup that
    actually frees the workspace — the terminal row, the watcher, `finish_turn_sandbox` — runs
    afterwards. A test that measured before the cancel was even delivered would pass whatever the
    code returned, which is how the old assertion survived being wrong.

    NOTHING HERE DEPENDS ON A CLOCK. The turn is parked until this test lets it go, so it cannot
    finish inside any budget; the assertion that it was genuinely unwinding waits for the cleanup
    to be entered rather than assuming the 50 ms was enough.

    And note what this pins about the SCAN: it selects on `status == "running"` at entry only, so
    a stop asked for AFTER `_finish` has run finds nothing — which is why the manager never treats
    this answer as the verdict and re-reads its own slot instead."""
    unwinding = asyncio.Event()
    let_it_finish = asyncio.Event()
    gate = asyncio.Event()

    async def _stall(messages: list[ModelMessage], info: AgentInfo):
        yield "partial "
        await gate.wait()
        yield "never"

    engine = _fresh_engine
    # The first await in the turn's `finally`, held open. Every terminal arm funnels through it,
    # so this is the honest shape of "still unwinding" rather than a fault injected sideways.
    linger_over = engine._write_turn_terminal

    async def _linger(state, factory):
        unwinding.set()
        await let_it_finish.wait()
        await linger_over(state, factory)

    monkeypatch.setattr(engine, "_write_turn_terminal", _linger)

    user, conv, _ = await _start(
        engine, db_session, session_factory, FunctionModel(stream_function=_stall)
    )
    state = engine.peek(conv.id)
    assert state is not None
    while not state.text_blocks():
        await asyncio.sleep(0.01)

    timed_out = await engine.stop_user_turn_and_wait(user.id, timeout_s=0.05)

    assert timed_out is StopOutcome.STILL_RUNNING
    await asyncio.wait_for(unwinding.wait(), timeout=10)  # the cleanup is genuinely under way
    assert state.task is not None and not state.task.done()  # ...and it really has not finished

    # THE BARRIER: let the unwind complete and WAIT for it before asking again, or the second
    # read measures a system that has not finished.
    let_it_finish.set()
    await _settle(engine, conv.id)
    assert (
        await engine.stop_user_turn_and_wait(user.id, timeout_s=10)
        is StopOutcome.NOTHING_WAS_RUNNING
    )


# --- U16: the split audience -------------------------------------------------------------
#
# `BuildError` feeds two readers with opposite needs. These pin the split from both ends: the
# model's half must not have moved, and the citizen's half must exist for every error class.


_RAW_TSC = (
    "\x1b[31mapp/page.tsx\x1b[0m(12,5): error TS2307: Cannot find module "
    "'@/components/VisitorTable' or its corresponding type declarations.\n"
    "app/api/visitors/route.ts(4,1): error TS1005: ';' expected.\n"
)


def test_the_model_still_gets_the_whole_diagnostic_unchanged() -> None:
    """THE OTHER HALF OF U16, and the one that is easy to break by accident.

    The unit removes developer text from the CITIZEN's surfaces. If it also softened `title` or
    trimmed `cleaned_stack`, the self-heal loop would be repairing from prose instead of from a
    compiler diagnostic — a much worse regression than the one being fixed, and an invisible one
    (the build would simply get worse at fixing itself).

    Pinned on a FIXED raw input against literal expected values rather than against
    `declutter`'s own output, so the assertion cannot follow the code it is guarding: the ANSI
    strip, the redaction pass, the `/workspace/` relativization and the `error TS` title scan all
    have to keep producing exactly these bytes, and the repair prompt has to keep carrying them.
    """
    from src.api.v1.build_sessions.schemas import ErrorSource
    from src.services.orchestrator.errors import from_tsc
    from src.services.orchestrator.prompt import build_repair_prompt

    error = from_tsc(_RAW_TSC)

    assert error.source is ErrorSource.TSC
    assert error.title == (
        "app/page.tsx(12,5): error TS2307: Cannot find module '@/components/VisitorTable' "
        "or its corresponding type declarations."
    )
    assert error.cleaned_stack == (
        "app/page.tsx(12,5): error TS2307: Cannot find module "
        "'@/components/VisitorTable' or its corresponding type declarations.\n"
        "app/api/visitors/route.ts(4,1): error TS1005: ';' expected.\n"
    )

    prompt = build_repair_prompt(error)
    assert error.title in prompt
    assert error.cleaned_stack in prompt
    # The line numbers, the module specifier and the second diagnostic all survive into the
    # prompt — the model is handed the same evidence it always was.
    assert "TS2307" in prompt and "TS1005" in prompt


def test_every_error_class_reaches_the_citizen_with_a_sentence_and_an_action() -> None:
    """TABLE-DRIVEN OVER `ErrorSource`, deliberately — including `CLIENT`.

    A per-source mapping is exactly the kind of table that grows a member with no row, and the
    failure mode is silent: the frame serializes, the portal renders, and the citizen reads a
    blank error. Iterating the enum rather than a hand-written list means a new member fails
    HERE, on the day it is added.

    Both halves are asserted non-empty, not just the sentence. An error status with no next step
    is the failure this unit exists to close; a nicer sentence that still dead-ends is the same
    dead end in a quieter voice."""
    from src.api.v1.build_sessions.schemas import ErrorSource
    from src.api.v1.conversations.schemas import DiagnosticFrame
    from src.services.orchestrator.errors import user_facing

    assert len(list(ErrorSource)) == 4  # the table below is exhaustive, and stays that way

    for source in ErrorSource:
        copy = user_facing(source)
        assert copy.message.strip(), source
        assert copy.action.strip(), source

        # A producer that knows only the model's half — every producer today — still emits a
        # frame carrying both citizen-facing fields, filled from the class. It cannot pass the
        # model's half at all: `title` and `cleaned_stack` are not fields on this frame (U14).
        frame = DiagnosticFrame(seq=1, source=source)
        assert frame.user_message == copy.message
        assert frame.user_action == copy.action

        # …and the pair survives the camelCase wire hop the portal parses.
        wire = frame.model_dump(by_alias=True)
        assert wire["userMessage"] == copy.message
        assert wire["userAction"] == copy.action


def test_a_producer_may_speak_for_itself_without_losing_the_action() -> None:
    """The derivation is a floor, not a ceiling: a caller with a better sentence keeps it, and
    the half it did NOT supply is still filled rather than left blank."""
    from src.api.v1.build_sessions.schemas import ErrorSource
    from src.api.v1.conversations.schemas import DiagnosticFrame
    from src.services.orchestrator.errors import user_facing

    frame = DiagnosticFrame(
        seq=1,
        source=ErrorSource.SERVER,
        user_message="Your visitor list couldn't load.",
    )
    assert frame.user_message == "Your visitor list couldn't load."
    assert frame.user_action == user_facing(ErrorSource.SERVER).action


def test_the_client_report_never_rides_out_on_the_frame() -> None:
    """U13's `exclude=True` is what structurally stops a browser stack reaching a person, and
    U16 renders the CLIENT class rather than skipping it — so the guard matters more, not less.
    Pinned on the SERIALIZATION, because that is the only thing egress actually looks at."""
    from src.api.v1.build_sessions.schemas import ErrorSource
    from src.services.orchestrator.errors import from_client

    error = from_client(
        "TypeError: undefined is not a function\n  at Visitors (page-8f2.js:1:920)"
    )

    assert error.agent_only_detail is not None
    assert "page-8f2.js" in error.agent_only_detail  # the agent still gets everything
    assert error.source is ErrorSource.CLIENT
    assert error.cleaned_stack == ""
    dumped = error.model_dump()
    assert "agent_only_detail" not in dumped
    assert "page-8f2.js" not in json.dumps(dumped)


# --- U20: exactly one durable terminal, per turn -------------------------------------------


async def _terminal_rows(db_session, conversation_id) -> list[Message]:
    """Every turn-terminal row on a conversation, in seq order — matched on `meta.kind` rather
    than on position, because the point of the assertion is HOW MANY there are."""
    rows = (
        await db_session.scalars(
            sa.select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.seq)
        )
    ).all()
    return [
        row
        for row in rows
        if isinstance(row.meta, dict) and row.meta.get("kind") == TURN_TERMINAL_KIND
    ]


async def test_a_stopped_turn_leaves_exactly_one_terminal_even_when_stopped_twice(
    _fresh_engine, db_session, session_factory
) -> None:
    """★ U20's integration scenario: no turn writes two terminal rows.

    THE SECOND STOP IS THE INTERESTING HALF. `stop_turn` answers False the second time — the
    task is already gone — but a design that wrote the row from each terminal ARM rather than
    from the single `finally` would be one refactor away from two rows for one turn, and a
    consumer counting terminals to decide whether a turn is over would then be reading a
    conversation with more endings than turns."""
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
    while not state.text_blocks():
        await asyncio.sleep(0)

    assert await engine.stop_turn(conv.id, turn_id) is True
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(state.task, timeout=10)
    assert await engine.stop_turn(conv.id, turn_id) is False
    gate.set()

    terminals = await _terminal_rows(db_session, conv.id)
    assert len(terminals) == 1
    assert terminals[0].meta is not None
    assert terminals[0].meta["turnId"] == str(turn_id)
    assert terminals[0].meta["status"] == "stopped"


async def test_a_turn_that_never_reaches_a_terminal_writes_no_row(
    _fresh_engine, db_session, session_factory
) -> None:
    """The ended-unknown case, produced rather than simulated.

    A turn still in flight has no terminal row — which is exactly what a process killed
    mid-turn leaves behind, because the code that would write one never runs. The absence IS
    the signal, so it has to be true of a genuinely-running turn and not only of a fixture that
    forgot to write one."""
    gate = asyncio.Event()

    async def _stall(messages: list[ModelMessage], info: AgentInfo):
        yield "partial "
        await gate.wait()

    engine = _fresh_engine
    _, conv, turn_id = await _start(
        engine, db_session, session_factory, FunctionModel(stream_function=_stall)
    )
    state = engine.peek(conv.id)
    assert state is not None
    while not state.text_blocks():  # LIVENESS: the run really is mid-flight
        await asyncio.sleep(0)

    assert await _terminal_rows(db_session, conv.id) == []

    # …and once it does end, exactly one appears — the same assertion from the other side, so
    # this cannot pass by never writing a terminal at all.
    assert await engine.stop_turn(conv.id, turn_id) is True
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(state.task, timeout=10)
    gate.set()
    assert len(await _terminal_rows(db_session, conv.id)) == 1


# --- U3: the acknowledgement is RETRACTED, never merely forgotten --------------------------
#
# "Getting started on that…" is the platform's own row, emitted synchronously inside
# `start_turn` so that something is on screen before the first model request — a cold provision
# can run half a minute, and that window is the whole reason the row exists. Clearing
# `state.acknowledgement` retired it from the catch-up snapshot, so a client that subscribed
# LATER never saw it; a client already connected had received it as a live step frame and never
# learned it was over. It sat in the turn's activity group as a step that never resolved, and a
# group with an unresolved step never seals.
#
# The retraction rides `hidden` rather than a new frame kind: the wire union is closed and the
# browser drops what it does not recognise, so re-emitting the same `tool_call_id` with
# `phase="finished"` and `hidden=True` replaces the row in place and it leaves a feed that
# already filters hidden steps. These three tests cover the three sites that fire it — the first
# real step, the first prose, and the terminal — because a turn that reached none of them is
# exactly the turn that used to be left with the row still spinning.


def _ack_frames(state: _TurnState) -> list[StepFrame]:
    """Every frame belonging to the harness's opening row, in wire order."""
    return [
        frame
        for frame in state.ring
        if isinstance(frame, StepFrame) and frame.tool_call_id == ACK_TOOL_CALL_ID
    ]


async def test_the_first_real_step_retracts_the_acknowledgement(
    _fresh_engine, db_session, session_factory
) -> None:
    """Mutation check: delete the `_retire_acknowledgement` call from `_open_step` and the
    retraction frame disappears while every other assertion here stays green — the ack
    survives to the terminal instead, which is the open-group defect wearing a later
    timestamp."""

    async def _stream(messages: list[ModelMessage], info: AgentInfo):
        if len(messages) == 1:
            yield DeltaToolCalls(
                {
                    0: DeltaToolCall(
                        name="read_file", json_args='{"path": "app/page.tsx"}', tool_call_id="r-1"
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
    opened, retired = _ack_frames(state)
    # THE FIRST-FRAME GUARANTEE, unchanged and pinned here as the premise of the retraction:
    # `seq == 1` is only reachable if the row was emitted inside `start_turn`, before
    # `asyncio.create_task` — so "before any model work" is structural rather than a timing hope.
    assert opened.seq == 1 and opened.phase == "started" and opened.item.hidden is False
    assert opened.item.label == ACK_TEXT
    # …and the retraction is the SAME row coming back hidden, not a blank replacement: the label
    # survives, so a client keying on `tool_call_id` replaces one row rather than drawing a
    # second empty one.
    assert retired.phase == "finished" and retired.item.hidden is True
    assert retired.item.label == ACK_TEXT
    # BEFORE the step it makes way for, on the wire. A retraction that arrived after the step
    # would leave the opening line under the first thing the agent did.
    ring = list(state.ring)
    first_step = next(f for f in ring if f.type == "step" and f.tool_call_id == "r-1")
    assert ring.index(retired) < ring.index(first_step)
    assert state.acknowledgement is None


async def test_a_turn_that_only_writes_prose_retracts_the_acknowledgement_too(
    _fresh_engine, db_session, session_factory
) -> None:
    """The plain-answer turn, which the first-step site cannot reach.

    A question answered in words calls nothing, so if the only retraction were the one at the
    first step this turn would keep "Getting started on that…" underneath an answer the citizen
    is already reading — and the board's rule for that turn is that it shows nothing at all
    beyond the answer."""
    engine = _fresh_engine
    _, conv, _ = await _start(
        engine, db_session, session_factory, _streaming_text("Here is the answer.")
    )
    await _settle(engine, conv.id)

    state = engine.peek(conv.id)
    assert state is not None and state.status == "completed"
    opened, retired = _ack_frames(state)
    assert opened.phase == "started" and retired.phase == "finished"
    assert retired.item.hidden is True
    ring = list(state.ring)
    first_word = next(f for f in ring if f.type == "text_delta")
    assert ring.index(retired) < ring.index(first_word)
    assert state.acknowledgement is None
    # LIVENESS for that absence: the answer really did arrive, so "no ack is pending" is a
    # statement about a turn that spoke, not about a turn where nothing happened at all.
    assert state.text_blocks() == ["Here is the answer."]


async def test_a_turn_that_neither_spoke_nor_acted_still_retracts_the_acknowledgement(
    _fresh_engine, db_session, session_factory
) -> None:
    """THE ARM ONLY `_finish` CAN COVER, and the one the defect actually lived in.

    A turn that dies at the workspace pin never reaches a tool call and never writes a word, so
    neither of the other two sites fires. Produced rather than simulated: a Write turn with no
    sandbox client configured ends with a named reason before the model is ever asked anything —
    which is exactly the shape of a cold provision that fails, the window the opening row exists
    to cover in the first place."""
    engine = _fresh_engine
    user, conv = await _conversation(db_session, ChatKind.BUILD)
    await engine.start_turn(
        conversation=conv,
        user_id=user.id,
        prompt="add a field",
        history=[],
        prompt_context=_CTX,
        app_id=None,
        project_id=conv.project_id,
        model=_streaming_text("never reached"),
        session_factory=session_factory,
        persist_user_turn=_noop_persist,
        manager=SessionManager(),
        sandbox_client=None,
    )
    await _settle(engine, conv.id)

    state = engine.peek(conv.id)
    assert state is not None and state.status == "failed"
    # LIVENESS: the turn genuinely produced nothing of its own — no step, no prose — so the two
    # earlier retraction sites were never reached and the terminal's is the only one that could
    # have fired.
    assert state.text_blocks() == []
    assert state.steps == {}
    opened, retired = _ack_frames(state)
    assert opened.phase == "started" and retired.phase == "finished"
    assert retired.item.hidden is True
    # BEFORE the terminal, which is what makes it reach a subscriber at all: the transport
    # closes on `turn_ended`, so a retraction emitted after it is one nobody would ever read.
    ring = list(state.ring)
    terminal = ring[-1]
    assert terminal.type == "turn_ended"
    assert ring.index(retired) < ring.index(terminal)
    assert state.acknowledgement is None


# --- U4: reasoning, as the status line and nothing else ------------------------------------
#
# Reasoning is requested adaptively with an effort level per kind, and the WHOLE of what it is
# allowed to become on the way out is a boolean: "the agent is working". The blocks themselves
# are stored — the provider rejects a later turn whose tool call has no preceding reasoning
# block — and are never projected, never framed, and never sent to the browser.

_REASONING = "Private deliberation the citizen must never be shown."


def _capturing_model() -> tuple[FunctionModel, list[dict[str, Any]]]:
    """A model that records the settings it was ACTUALLY handed, then answers in one word.

    `AgentInfo.model_settings` is what the run passed down to the model — the same object a real
    provider would translate into a request — so asserting on it pins the wiring rather than the
    constant, which a test reading `constants.py` back to itself would not.

    Copied into a plain dict because the provider-specific keys live on `AnthropicModelSettings`
    rather than on the base `ModelSettings` the handler is typed with; the shape being asserted
    is the wire one, not the TypedDict."""
    seen: list[dict[str, Any]] = []

    async def _stream(messages: list[ModelMessage], info: AgentInfo):
        seen.append(dict(info.model_settings or {}))
        yield "done"

    return FunctionModel(stream_function=_stream), seen


async def test_a_plan_run_asks_for_adaptive_thinking_at_medium_effort(
    _fresh_engine, db_session, session_factory
) -> None:
    """Asserted on what the run HANDED the model, not on the constant: a settings site that
    stopped passing the thinking knobs altogether would still read `PLAN_EFFORT == "medium"`
    in `constants.py` while every turn ran with reasoning off."""
    engine = _fresh_engine
    model, seen = _capturing_model()
    _, conv, _ = await _start(engine, db_session, session_factory, model, kind=ChatKind.PLAN)
    await _settle(engine, conv.id)

    state = engine.peek(conv.id)
    assert state is not None and state.status == "completed"
    assert len(seen) == 1  # LIVENESS: a request really did fire, so `seen` is not empty by luck
    assert seen[0]["anthropic_thinking"] == ADAPTIVE_THINKING
    assert seen[0]["anthropic_effort"] == PLAN_EFFORT
    # THE OWNER'S RULING, SPELLED OUT rather than deferred to the constant it is stored in: a
    # change that swapped the two effort levels over would satisfy the assertions above and
    # still reverse the decision. A plan is a conversation about what to build with the person
    # still in it, so it thinks at medium.
    assert PLAN_EFFORT == "medium"
    # ADAPTIVE, NOT A TOKEN BUDGET, and the reason is the deployed model rather than taste —
    # see `test_the_deployed_model_takes_adaptive_thinking_and_refuses_a_budget` below.
    assert ADAPTIVE_THINKING == {"type": "adaptive"}


async def test_a_build_run_asks_for_the_same_thinking_at_high_effort(
    _fresh_engine, db_session, session_factory
) -> None:
    """The Build fork runs its own node loop with its own model-settings site, which is exactly
    how the two could drift: a change made in one place and not the other is invisible until a
    build starts thinking like a chat.

    The model here reads nothing and writes nothing, so the mutation guard returns before the
    verify pass — an ordinary chat turn that happened to hold the write tools. That keeps this
    test about the settings and not about the self-heal loop, which `test_write_turn.py` owns."""
    engine = _fresh_engine
    model, seen = _capturing_model()
    _, conv, _ = await _start(engine, db_session, session_factory, model, kind=ChatKind.BUILD)
    await _settle(engine, conv.id)

    state = engine.peek(conv.id)
    assert state is not None and state.status == "completed"
    assert len(seen) == 1
    assert seen[0]["anthropic_thinking"] == ADAPTIVE_THINKING
    assert seen[0]["anthropic_effort"] == BUILD_EFFORT
    # HIGHER THAN A PLAN, and both halves of that comparison are asserted: a build is where the
    # thinking is spent on something that has to compile, so it gets the harder setting — and a
    # regression that pointed both sites at one constant would pass every assertion above.
    assert BUILD_EFFORT == "high"
    assert BUILD_EFFORT != PLAN_EFFORT


async def test_reasoning_becomes_a_working_flag_and_never_its_words(
    _fresh_engine, db_session, session_factory
) -> None:
    """★ THE STATUS, AND ONLY THE STATUS.

    Three reasoning events produce ONE `working` frame, because `_set_working` frames a CHANGE
    rather than an event — a frame per delta would push thousands of identical frames through a
    ring sized for a turn's whole narrative, evicting the turn's actual content to say the same
    thing over and over.

    Mutation check: drop the `if state.working == working: return` guard and the frame list
    below grows one entry per reasoning delta, plus a redundant stand-down at the terminal;
    push the reasoning text through `_push_text` from the `ThinkingPart` arm instead of setting
    the flag and the wire assertion goes red while the frame count stays green."""

    async def _stream(messages: list[ModelMessage], info: AgentInfo):
        # One reasoning block across three deltas: the first opens the part, the rest extend it.
        yield {0: DeltaThinkingPart(content=_REASONING)}
        yield {0: DeltaThinkingPart(content=" Second thought.")}
        yield {0: DeltaThinkingPart(content=" Third thought.", signature="sig-1")}
        yield "Here is what I would build."

    engine = _fresh_engine
    _, conv, _ = await _start(
        engine, db_session, session_factory, FunctionModel(stream_function=_stream)
    )
    await _settle(engine, conv.id)

    state = engine.peek(conv.id)
    assert state is not None and state.status == "completed"
    working = [f for f in state.ring if f.type == "working"]
    assert [f.working for f in working] == [True, False]
    # CLEARED BY THE WORDS, and the ordering is the claim: the status goes down before the first
    # syllable of the answer, so a citizen never reads a reply underneath "still thinking".
    ring = list(state.ring)
    first_word = next(f for f in ring if f.type == "text_delta")
    assert ring.index(working[1]) < ring.index(first_word)
    # NOTHING THE MODEL THOUGHT LEFT THE BACKEND. Asserted over the whole serialized ring rather
    # than over the working frames alone: the failure this guards against is the text arriving
    # somewhere else — a text delta, a step label, the terminal — not a `working` frame growing
    # a field.
    wire = json.dumps([f.model_dump(mode="json") for f in ring], ensure_ascii=False)
    assert _REASONING not in wire
    assert "Second thought." not in wire and "sig-1" not in wire
    # LIVENESS: the turn's visible answer did reach the wire, so the absences above are about
    # reasoning specifically and not about a turn that emitted nothing.
    assert state.text_blocks() == ["Here is what I would build."]
    assert "Here is what I would build." in wire


async def test_a_step_takes_the_working_status_down(
    _fresh_engine, db_session, session_factory
) -> None:
    """The other half of "cleared by anything that is not thinking". A turn that thinks and then
    acts without saying a word would otherwise keep the status up under a running step, which
    reads as the agent still deciding while it is already doing."""

    async def _stream(messages: list[ModelMessage], info: AgentInfo):
        if len(messages) == 1:
            yield {0: DeltaThinkingPart(content=_REASONING, signature="sig-1")}
            yield {0: DeltaThinkingPart(content=" Still weighing it.")}
            yield DeltaToolCalls(
                {
                    1: DeltaToolCall(
                        name="read_file", json_args='{"path": "app/page.tsx"}', tool_call_id="r-1"
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
    working = [f for f in state.ring if f.type == "working"]
    assert [f.working for f in working] == [True, False]
    ring = list(state.ring)
    step_started = next(
        f for f in ring if f.type == "step" and f.tool_call_id == "r-1" and f.phase == "started"
    )
    assert ring.index(working[1]) < ring.index(step_started)
    wire = json.dumps([f.model_dump(mode="json") for f in ring], ensure_ascii=False)
    assert _REASONING not in wire and "Still weighing it." not in wire
    # LIVENESS: the step the status made way for is genuinely on the wire.
    assert step_started.item.label == "Looking at your app's main page"


async def test_a_reattach_mid_reasoning_reads_the_status_and_the_terminal_takes_it_back(
    _fresh_engine, db_session, session_factory
) -> None:
    """The catch-up snapshot is the ONLY way a subscriber learns about a frame that fired before
    it connected, and a citizen who reattaches while the model is thinking would otherwise get a
    still screen — the exact silence the whole status exists to cover.

    And the status cannot outlive the turn. A turn that ended while the last thing it did was
    think — a stop, a failure mid-reasoning — would leave "working" under a turn that is over,
    which is why `_finish` clears it rather than relying on prose or a step arriving first."""
    gate = asyncio.Event()

    async def _stream(messages: list[ModelMessage], info: AgentInfo):
        yield {0: DeltaThinkingPart(content=_REASONING, signature="sig-1")}
        await gate.wait()
        yield "never"

    engine = _fresh_engine
    _, conv, turn_id = await _start(
        engine, db_session, session_factory, FunctionModel(stream_function=_stream)
    )
    state = engine.peek(conv.id)
    assert state is not None
    while not state.working:  # the model is genuinely mid-reasoning
        await asyncio.sleep(0.01)

    mid = engine.build_snapshot(state)
    assert mid.working is True
    assert mid.turn_status == "running"
    # …and the snapshot carries the STATUS, never the words. Serialized whole for the same
    # reason the live assertion is: the text could only arrive by riding some other field.
    assert _REASONING not in json.dumps(mid.model_dump(mode="json"), ensure_ascii=False)
    # LIVENESS, and a second fact worth pinning: reasoning does NOT retract the opening
    # acknowledgement, so this mid-flight snapshot has real content in it — the citizen who
    # reattaches here sees the opening row and the working status, not an empty turn.
    assert [part.tool_call_id for part in mid.parts if isinstance(part, TurnStepPart)] == [
        ACK_TOOL_CALL_ID
    ]

    assert await engine.stop_turn(conv.id, turn_id) is True
    await _settle(engine, conv.id)

    assert state.status == "stopped"
    assert state.working is False
    assert engine.build_snapshot(state).working is False
    working = [f for f in state.ring if f.type == "working"]
    assert [f.working for f in working] == [True, False]
    # The stand-down reaches the wire BEFORE the terminal closes the transport — a `working`
    # frame after `turn_ended` is one no subscriber would ever read.
    ring = list(state.ring)
    assert ring.index(working[1]) < ring.index(ring[-1])
    assert ring[-1].type == "turn_ended"


async def test_reasoning_is_kept_for_the_provider_and_projected_to_nobody(
    _fresh_engine, db_session, session_factory
) -> None:
    """★ BOTH HALVES OF THE ONE RULE, which is why they are asserted together: the blocks are
    STORED and they are SHOWN TO NOBODY. Storing them is not an oversight to be tidied away —
    the provider rejects a later turn whose tool call has no preceding reasoning block, so a
    transcript that dropped them would break the next turn rather than the current one.

    Mutation check: add a `thinking` arm to `_project_response_parts` that appends an
    `AssistantTextItem` and the projection assertion goes red while the payload one stays green;
    filter `ThinkingPart` out of `_persistable_messages` and the reverse happens."""

    async def _stream(messages: list[ModelMessage], info: AgentInfo):
        yield {0: DeltaThinkingPart(content=_REASONING, signature="sig-1")}
        yield "Here is what I would build."

    engine = _fresh_engine
    _, conv, _ = await _start(
        engine, db_session, session_factory, FunctionModel(stream_function=_stream)
    )
    await _settle(engine, conv.id)

    state = engine.peek(conv.id)
    assert state is not None and state.status == "completed"
    rows = (
        await db_session.scalars(
            sa.select(Message).where(Message.conversation_id == conv.id).order_by(Message.seq)
        )
    ).all()

    # THE PAYLOAD KEEPS IT, signature and all. The signature matters as much as the content: a
    # block whose signature was rewritten fails replay, which is why `store._redact_tree` exempts
    # both fields from the secret masker rather than trusting the masker to leave them alone.
    parts = [part for row in rows for message in row.payload for part in message.get("parts", [])]
    thinking = [part for part in parts if part.get("part_kind") == "thinking"]
    assert len(thinking) == 1
    assert thinking[0]["content"] == _REASONING
    assert thinking[0]["signature"] == "sig-1"

    # …AND THE PROJECTION DRAWS NOTHING FROM IT. There is no reasoning item in the display union
    # at all, so the assertion is that no item carries the words — the shape a reasoning item
    # would have to take if one were ever added by accident.
    items = project_rows(rows)
    drawn = json.dumps([item.model_dump(mode="json") for item in items], ensure_ascii=False)
    assert _REASONING not in drawn
    assert "sig-1" not in drawn
    # LIVENESS: the same rows DO project the turn's visible answer, so the absence above is
    # about reasoning and not about a projection that returned nothing.
    assert "Here is what I would build." in drawn


# --- U4: the guard that only the real provider model executes -------------------------------

_DEPLOYED_MODEL = "claude-opus-4-7"
"""The deployment name `.env` carries, spelled out because the Foundry block is genuinely optional
and the test lane boots without one (`test_config.py::test_foundry_optional_defaults_none`).

Naming a model cannot be avoided here: the refusal below is a property of the model's PROFILE, so
a test of it has to have a model to ask."""


def _configured_deployment() -> str:
    """The deployment this platform is pointed at, falling back to the name it ships with.

    Read from config first so a repointed deployment is tested rather than ignored — the whole
    value of this test is that it asks the model we actually talk to."""
    foundry = settings.foundry
    return foundry.deployment if foundry is not None else _DEPLOYED_MODEL


def test_the_deployed_model_takes_adaptive_thinking_and_refuses_a_budget() -> None:
    """★ THE REAL PROVIDER MODEL, NOT A DOUBLE, and that is the entire point of this test.

    The refusal lives in `AnthropicModel.prepare_request`: the deployed model's profile disallows
    budget thinking, and the library raises BEFORE the request rather than letting the gateway
    return a 400. A `FunctionModel` never executes any of that, so every other test in this file
    would go green on a settings combination the live gateway rejects — which is exactly how a
    build could ship thinking-off and nobody would know until a citizen's turn failed.

    No network is touched: constructing an `AnthropicModel` and preparing a request are both
    local, so the provider takes a dummy key and never opens a socket."""
    model = AnthropicModel(
        _configured_deployment(), provider=AnthropicProvider(api_key="not-a-real-key")
    )
    params = ModelRequestParameters()

    # THE WHOLE SETTINGS OBJECT THE CHAT RUN BUILDS, not a reduced one: what is being asked is
    # whether the combination this code sends survives, and a combination is exactly the kind of
    # thing that can be individually valid and jointly refused.
    with warnings.catch_warnings():
        # This model's profile also strips sampling settings — `temperature` is dropped and a
        # UserWarning is raised saying so. Silenced rather than asserted: it is the library's
        # policy about a knob this plan does not touch, and pinning it here would turn a profile
        # change into a red test about reasoning.
        warnings.simplefilter("ignore", UserWarning)
        prepared, _ = model.prepare_request(
            AnthropicModelSettings(
                max_tokens=MAX_OUTPUT_TOKENS,
                temperature=TEMPERATURE,
                anthropic_thinking=ADAPTIVE_THINKING,
                anthropic_effort=PLAN_EFFORT,
            ),
            params,
        )
    # WHAT ASKS FOR REASONING SURVIVES, both halves of it: adaptive thinking is what turns it on
    # at all, and the effort is what decides how much.
    assert prepared is not None
    survived: dict[str, Any] = dict(prepared)
    assert survived["anthropic_thinking"] == ADAPTIVE_THINKING
    assert survived["anthropic_effort"] == PLAN_EFFORT
    # …and the output clamp with them, which is the other reason these settings exist: without it
    # the provider default of 4096 cuts a long plan off mid-argument.
    assert survived["max_tokens"] == MAX_OUTPUT_TOKENS

    # …and the shape we deliberately do NOT use is refused here rather than by the gateway. If
    # this stops raising, the deployment has moved to a model with a different profile and
    # `ADAPTIVE_THINKING`'s premise needs re-checking — not the assertion relaxing.
    with pytest.raises(UserError, match="budget_tokens"):
        model.prepare_request(
            AnthropicModelSettings(
                anthropic_thinking={"type": "enabled", "budget_tokens": 10_000}
            ),
            params,
        )
