"""The cooperative stop: a Build run ends at its NEXT TOOL-RESULT BOUNDARY, from any process.

Three things are being held apart here, and conflating any two of them is the defect:

* A HARD CUT ends the run wherever it stands. The first two tests characterize it — an
  unanswered tool call in the transcript, repaired on reload by the store's interrupted note —
  because everything below is defined as the difference from it.
* A COOPERATIVE STOP lets the tool in flight finish, records its result, issues no further model
  request, and leaves the loop ABOVE the mutation guard and above `verify`: a stop on a turn that
  had only read files must not be told it built nothing, must not poll a container that is about
  to be destroyed, and must not buy a repair round.
* THE TWO ASKS ARE SEPARATE FLAGS. `stop_requested` means "a cancel has been issued" and gates
  both existing doors; a cooperative ask spelt that way would make the citizen's own Stop button
  a silent no-op. Every test that stops a turn with one pending proves the other still fires.

The ask travels as a CONVERSATION-KEYED Redis key, because the party that raises it is routinely
in another process with no task to reach for — and because the liveness lease that carries it is
per USER, so a user-keyed ask would stop the incoming project's fresh turn instead.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid

import pytest
import redis.asyncio as aioredis
from pydantic import SecretStr
from pydantic_ai.messages import ModelMessage, ModelRequest, ToolReturnPart
from pydantic_ai.models.function import (
    AgentInfo,
    DeltaToolCall,
    DeltaToolCalls,
    FunctionModel,
)
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.v1.build_sessions.schemas import LIVENESS_LEASE_RENEW_CADENCE_SECONDS
from src.config import settings
from src.db.models.conversation import ChatKind
from src.services.agent.mode_prompts import PromptContext
from src.services.build_sessions.manager import SessionManager, StopOutcome
from src.services.build_sessions.outcome import STOPPED_BY_USER
from src.services.messages.store import _INTERRUPTED_RESULT, load_history
from src.services.orchestrator.constants import (
    RUN_COMMAND_SLOW_TIMEOUT_S,
    RUN_WALL_CLOCK_DEADLINE_S,
)
from src.services.redis.keys import cooperative_stop_key
from src.services.sandbox.base import FileCreate, FileOp, FileResult, SandboxHandle
from src.services.sandbox.config import SandboxConfig
from src.services.turns import engine as engine_module
from src.services.turns.engine import (
    COOPERATIVE_STOP_GRACE_S,
    STOPPED_AT_A_BOUNDARY,
    TurnEngine,
    publish_cooperative_stop,
    set_turn_engine_for_tests,
)
from src.services.turns.guard import _mid_reply
from tests.factories import ConversationFactory, ProjectFactory, UserFactory
from tests.fakes import FakeSandboxClient

_CTX = PromptContext(user_name="Ada", project_name="Visitors", project_description=None)

_WROTE_A_FILE = ("write_file", '{"path": "app/page.tsx", "file_text": "x"}')
_READ_A_FILE = ("read_file", '{"path": "app/page.tsx"}')


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
    """The lease loop is what carries the ask into the turn, and it looks once every 30 seconds
    in production. Nothing about the mechanism depends on the cadence, so the tests that prove
    delivery drive it fast rather than waiting out a real tick."""
    monkeypatch.setattr(engine_module, "LIVENESS_LEASE_RENEW_CADENCE_SECONDS", 0.01)


class _StallsInsideTheWrite(FakeSandboxClient):
    """A container whose file write parks until the test lets it go.

    THE TOOL CALL IS IN FLIGHT while it parks, which is the only place a mid-tool-call stop can
    honestly be delivered: a fake that returns immediately closes the window this whole unit is
    about, and every assertion against it would pass vacuously."""

    def __init__(self) -> None:
        super().__init__()
        self.inside = asyncio.Event()
        self.let_go = asyncio.Event()

    async def files(self, handle: SandboxHandle, op: FileOp) -> FileResult:
        if isinstance(op, FileCreate):
            self.inside.set()
            await self.let_go.wait()
        return await super().files(handle, op)


def _scripted(steps: list[list[tuple[str, str]] | str]) -> tuple[FunctionModel, dict[str, int]]:
    """A streaming model that plays one canned step per model request, counting requests.

    The COUNT is the assertion in most of this file: "no further model request" is what a
    boundary stop buys, and a turn that quietly issued one more would otherwise look identical."""
    counts = {"requests": 0}
    remaining = list(steps)

    async def _stream(_messages: list[ModelMessage], _info: AgentInfo):
        counts["requests"] += 1
        step = remaining.pop(0) if remaining else "done."
        if isinstance(step, str):
            yield step
            return
        yield DeltaToolCalls(
            {
                index: DeltaToolCall(
                    name=name,
                    json_args=json_args,
                    tool_call_id=f"c-{name}-{counts['requests']}-{index}",
                )
                for index, (name, json_args) in enumerate(step)
            }
        )

    return FunctionModel(stream_function=_stream), counts


async def _build_chat(db: AsyncSession, email: str, *, kind: ChatKind = ChatKind.BUILD):
    user = await UserFactory.create(db, email=email)
    project = await ProjectFactory.create(db, user.id)
    conversation = await ConversationFactory.create(db, user.id, project_id=project.id, kind=kind)
    return user, project, conversation


async def _nothing_to_persist() -> None:
    return None


async def _start(
    engine: TurnEngine,
    session_factory,
    *,
    user,
    project,
    conversation,
    model,
    manager: SessionManager,
    client: FakeSandboxClient,
    expects_mutation: bool = False,
) -> uuid.UUID:
    return await engine.start_turn(
        conversation=conversation,
        user_id=user.id,
        prompt="add a status column",
        history=[],
        prompt_context=_CTX,
        app_id=None,
        project_id=project.id,
        model=model,
        session_factory=session_factory,
        persist_user_turn=_nothing_to_persist,
        manager=manager,
        sandbox_client=client,
        expects_mutation=expects_mutation,
    )


async def _settled(engine: TurnEngine, conversation_id: uuid.UUID):
    state = engine.peek(conversation_id)
    assert state is not None and state.task is not None
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(state.task, timeout=10)
    return state


async def _no_rehydration(_refs) -> dict[str, tuple[str, str]]:
    """`load_history` takes a rehydrator; nothing in these fixtures carries an attachment."""
    return {}


async def _replayed(db: AsyncSession, user_id: uuid.UUID, conversation_id: uuid.UUID):
    """The conversation as a LATER turn would read it — repaired, which is where the store's
    interrupted note appears for a call that was never answered."""
    return await load_history(
        db, user_id=user_id, conversation_id=conversation_id, rehydrate=_no_rehydration
    )


def _tool_answers(history: list[ModelMessage]) -> list[str]:
    return [
        str(part.content)
        for message in history
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, ToolReturnPart)
    ]


def _verify_ran(state) -> bool:
    """`verify` announces itself with its own step frames, and they are the only frames a
    citizen ever sees for it."""
    return any(getattr(frame, "tool_call_id", "").startswith("verify-") for frame in state.ring)


# --- characterization: what a hard cut does, pinned before anything changed it ---------------


async def test_a_hard_cut_leaves_the_tool_call_for_the_transcript_to_guess_at(
    _fresh_engine: TurnEngine,
    db_session: AsyncSession,
    session_factory,
    fake_redis: aioredis.Redis,
    fake_storage,
) -> None:
    """THE BASELINE, and the error path R11 falls back to when no boundary arrives inside the
    bound: the run is cut where it stands, the turn ends `stopped`, and the tool call already on
    the record has no result — so `messages/store.py` synthesizes one on reload.

    That synthesized note is the whole reason this unit needs no marker of its own. It is also
    the cost a boundary stop avoids, which the happy path below asserts as its own absence."""
    engine = _fresh_engine
    user, project, conversation = await _build_chat(db_session, "sb1@rvaiglobal.com")
    manager, client = SessionManager(), FakeSandboxClient()
    requests = {"n": 0}
    second_request = asyncio.Event()

    async def _stream(_messages: list[ModelMessage], _info: AgentInfo):
        requests["n"] += 1
        if requests["n"] > 1:
            # The step before this one is already on the record, tool call and all; its ANSWERS
            # ride on this request and only reach the transcript when it completes.
            second_request.set()
            await asyncio.Event().wait()  # only a cancel leaves this
        yield DeltaToolCalls(
            {
                0: DeltaToolCall(
                    name="write_file",
                    json_args='{"path": "app/page.tsx", "file_text": "x"}',
                    tool_call_id="c-write-1",
                )
            }
        )

    turn_id = await _start(
        engine,
        session_factory,
        user=user,
        project=project,
        conversation=conversation,
        model=FunctionModel(stream_function=_stream),
        manager=manager,
        client=client,
    )
    await asyncio.wait_for(second_request.wait(), timeout=10)
    # The step is on the record and its answers are not: the run is parked in the request that
    # would have carried them. This is where a real stop lands.
    assert await engine.stop_turn(conversation.id, turn_id) is True
    state = await _settled(engine, conversation.id)

    assert state.status == "stopped"
    assert state.end_reason == STOPPED_BY_USER
    assert state.ring[-1].type == "turn_ended"
    assert _INTERRUPTED_RESULT in _tool_answers(
        await _replayed(db_session, user.id, conversation.id)
    )


async def test_an_ordinary_build_verifies_and_ends_with_nothing_to_explain(
    _fresh_engine: TurnEngine,
    db_session: AsyncSession,
    session_factory,
    fake_redis: aioredis.Redis,
    fake_storage,
) -> None:
    """The liveness half of every absence asserted below. A build that runs to its own end DOES
    pay for a verify pass and DOES carry no `end_reason` — so "no verify, and a named reason"
    is a difference this file can actually observe rather than a fixture that never verifies."""
    engine = _fresh_engine
    user, project, conversation = await _build_chat(db_session, "sb2@rvaiglobal.com")
    manager, client = SessionManager(), FakeSandboxClient()
    model, counts = _scripted(
        [[_WROTE_A_FILE], [("declare_done", '{"summary": "added the column"}')]]
    )

    await _start(
        engine,
        session_factory,
        user=user,
        project=project,
        conversation=conversation,
        model=model,
        manager=manager,
        client=client,
    )
    state = await _settled(engine, conversation.id)

    assert state.status == "completed"
    assert state.end_reason is None
    assert counts["requests"] == 2
    assert _verify_ran(state)


# --- the boundary ---------------------------------------------------------------------------


async def test_a_stop_mid_tool_call_lets_the_call_finish_and_stops_there(
    _fresh_engine: TurnEngine,
    db_session: AsyncSession,
    session_factory,
    fake_redis: aioredis.Redis,
    fake_storage,
    _a_brisk_lease: None,
) -> None:
    """★ THE HAPPY PATH. The ask arrives while a file write is in flight: the write completes,
    its result reaches the transcript, no second model request is made, and the turn ends with a
    named reason and nothing for a later replay to guess at.

    RAISED THROUGH REDIS AND NOTHING ELSE — `publish_cooperative_stop` is a module function that
    never touches the engine's registry, which is what makes the same call work from the worker,
    where that registry is empty.

    Mutation check: drop `cooperative_stop_requested` from the boundary condition in
    `_run_write_once` and the request count goes to two while the reason goes to None."""
    engine = _fresh_engine
    user, project, conversation = await _build_chat(db_session, "sb3@rvaiglobal.com")
    manager, client = SessionManager(), _StallsInsideTheWrite()
    model, counts = _scripted([[_WROTE_A_FILE], [("declare_done", '{"summary": "done"}')]])

    await _start(
        engine,
        session_factory,
        user=user,
        project=project,
        conversation=conversation,
        model=model,
        manager=manager,
        client=client,
    )
    await asyncio.wait_for(client.inside.wait(), timeout=10)

    await publish_cooperative_stop(conversation.id)
    state = engine.peek(conversation.id)
    assert state is not None
    for _ in range(2000):  # the turn's own lease loop is what fetches it
        if state.cooperative_stop_requested:
            break
        await asyncio.sleep(0.001)
    assert state.cooperative_stop_requested, "the ask never reached the turn"

    client.let_go.set()
    state = await _settled(engine, conversation.id)

    assert state.status == "completed"
    assert state.end_reason == STOPPED_AT_A_BOUNDARY
    assert state.error_message is None
    assert counts["requests"] == 1  # …and not one more
    assert not _verify_ran(state)
    # THE CALL COMPLETED AND ITS RESULT IS DURABLE, which is the whole difference from a cut.
    answers = _tool_answers(await _replayed(db_session, user.id, conversation.id))
    assert "Wrote `app/page.tsx`." in answers
    assert _INTERRUPTED_RESULT not in answers


async def test_a_stop_on_a_build_that_wrote_nothing_neither_accuses_nor_repairs(
    _fresh_engine: TurnEngine,
    db_session: AsyncSession,
    session_factory,
    fake_redis: aioredis.Redis,
    fake_storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★ THE EDGE THIS UNIT EXISTS FOR. A turn that was ASKED to build and had only read a file
    when the stop arrived must not be told it built nothing: `build_wrote_nothing` is the verdict
    on a model that finished without writing, not on a platform that took the container away.

    And nothing downstream of it may run either — a `verify` here is thirty seconds spent polling
    a container about to be destroyed, and the repair round behind it is a whole model run
    charged to a citizen who has left.

    Mutation check: move the cooperative-stop exit in `_run_write` below the mutation guard and
    this goes red with `build_wrote_nothing`; move it below `verify` and `_verify_ran` goes
    true."""
    engine = _fresh_engine
    user, project, conversation = await _build_chat(db_session, "sb4@rvaiglobal.com")
    manager, client = SessionManager(), FakeSandboxClient()

    async def _verify(*_a: object, **_k: object):
        raise AssertionError("a container that is about to be destroyed must not be polled")

    monkeypatch.setattr(engine_module, "verify", _verify)
    model, counts = _scripted([[_READ_A_FILE], "having a look."])

    await _start(
        engine,
        session_factory,
        user=user,
        project=project,
        conversation=conversation,
        model=model,
        manager=manager,
        client=client,
        expects_mutation=True,
    )
    state = engine.peek(conversation.id)
    assert state is not None
    state.cooperative_stop_requested = True
    state = await _settled(engine, conversation.id)

    assert state.status == "completed"
    assert state.end_reason == STOPPED_AT_A_BOUNDARY
    assert state.error_message is None
    assert counts["requests"] == 1  # no repair prompt, so no second run
    assert not _verify_ran(state)


async def test_a_stop_during_the_model_call_still_waits_for_the_tool_result(
    _fresh_engine: TurnEngine,
    db_session: AsyncSession,
    session_factory,
    fake_redis: aioredis.Redis,
    fake_storage,
) -> None:
    """The boundary is the NEXT TOOL RESULT, not the next opportunity to stop. An ask raised
    while the model is still writing its response does not cut the stream and does not discard
    the call it is composing — the call runs, its result is recorded, and the loop leaves there.

    Mutation check: honour the flag as soon as the model response lands — before the tools node
    runs — and the tool answer disappears from the replay with the interrupted note in its
    place, which is the hard cut this is supposed to differ from."""
    engine = _fresh_engine
    user, project, conversation = await _build_chat(db_session, "sb5@rvaiglobal.com")
    manager, client = SessionManager(), FakeSandboxClient()
    composing = asyncio.Event()
    asked = asyncio.Event()

    async def _stream(_messages: list[ModelMessage], _info: AgentInfo):
        composing.set()
        await asked.wait()
        yield DeltaToolCalls(
            {
                0: DeltaToolCall(
                    name="write_file",
                    json_args='{"path": "app/page.tsx", "file_text": "x"}',
                    tool_call_id="c-write-1",
                )
            }
        )

    await _start(
        engine,
        session_factory,
        user=user,
        project=project,
        conversation=conversation,
        model=FunctionModel(stream_function=_stream),
        manager=manager,
        client=client,
    )
    await asyncio.wait_for(composing.wait(), timeout=10)
    state = engine.peek(conversation.id)
    assert state is not None
    state.cooperative_stop_requested = True
    asked.set()
    state = await _settled(engine, conversation.id)

    assert state.status == "completed"
    assert state.end_reason == STOPPED_AT_A_BOUNDARY
    answers = _tool_answers(await _replayed(db_session, user.id, conversation.id))
    assert "Wrote `app/page.tsx`." in answers
    assert _INTERRUPTED_RESULT not in answers


# --- the stops that already existed, still working --------------------------------------------


async def test_the_citizen_can_still_cut_a_turn_with_a_cooperative_stop_pending(
    _fresh_engine: TurnEngine,
    db_session: AsyncSession,
    session_factory,
    fake_redis: aioredis.Redis,
    fake_storage,
) -> None:
    """★ THE SEPARATION OF THE TWO FLAGS, asserted where folding them would show. `stop_turn`
    returns early when `stop_requested` is already True, so a cooperative ask spelt as that flag
    would answer the citizen's Stop button "already settled" and leave the turn running — which
    is exactly what nobody pressing Stop would ever discover.

    Mutation check: raise `stop_requested` here instead — which is what folding the two fields
    into one would do — and `stop_turn` returns False."""
    engine = _fresh_engine
    user, project, conversation = await _build_chat(db_session, "sb6@rvaiglobal.com")
    manager, client = SessionManager(), _StallsInsideTheWrite()
    model, _ = _scripted([[_WROTE_A_FILE]])

    turn_id = await _start(
        engine,
        session_factory,
        user=user,
        project=project,
        conversation=conversation,
        model=model,
        manager=manager,
        client=client,
    )
    await asyncio.wait_for(client.inside.wait(), timeout=10)
    state = engine.peek(conversation.id)
    assert state is not None
    state.cooperative_stop_requested = True

    assert await engine.stop_turn(conversation.id, turn_id) is True
    client.let_go.set()
    state = await _settled(engine, conversation.id)

    assert state.status == "stopped"
    assert state.end_reason == STOPPED_BY_USER


async def test_the_take_back_door_cuts_a_turn_with_a_cooperative_stop_pending(
    _fresh_engine: TurnEngine,
    db_session: AsyncSession,
    session_factory,
    fake_redis: aioredis.Redis,
    fake_storage,
) -> None:
    """The other existing door, and the same rule: the citizen on this path is waiting for their
    own workspace back, so the cut outranks a boundary that may be a cold install away. It must
    not read a pending ask as "already settled" either — `stop_user_turn_and_wait` guards its
    `task.cancel()` behind `stop_requested`, and nothing else."""
    engine = _fresh_engine
    user, project, conversation = await _build_chat(db_session, "sb7@rvaiglobal.com")
    manager, client = SessionManager(), _StallsInsideTheWrite()
    model, _ = _scripted([[_WROTE_A_FILE]])

    await _start(
        engine,
        session_factory,
        user=user,
        project=project,
        conversation=conversation,
        model=model,
        manager=manager,
        client=client,
    )
    await asyncio.wait_for(client.inside.wait(), timeout=10)
    state = engine.peek(conversation.id)
    assert state is not None
    state.cooperative_stop_requested = True
    client.let_go.set()

    assert await engine.stop_user_turn_and_wait(user.id, timeout_s=10) is StopOutcome.STOPPED
    assert state.status == "stopped"
    assert state.end_reason == STOPPED_BY_USER


async def test_a_plan_turn_is_cut_at_once_and_never_waits_for_a_boundary(
    _fresh_engine: TurnEngine,
    db_session: AsyncSession,
    session_factory,
    fake_redis: aioredis.Redis,
    fake_storage,
    _a_brisk_lease: None,
) -> None:
    """★ PLAN TURNS ARE OUT OF SCOPE, EXPLICITLY. A Plan turn is one `chat_agent.run` with no
    node walk, so it has no tool-result boundary to end at: an ask left standing against it would
    be honoured by nothing and consumed by nothing. It is not read, and the turn is cut.

    Switching mid-chat is the COMMON case, which is why this matters beyond tidiness — a wait
    here would put the bounded grace on the ordinary path rather than the error path.

    Mutation check: drop the `ChatKind.BUILD` guard in `_hold_liveness_lease` and the flag
    assertion goes red."""
    engine = _fresh_engine
    user, project, conversation = await _build_chat(
        db_session, "sb8@rvaiglobal.com", kind=ChatKind.PLAN
    )
    manager, client = SessionManager(), FakeSandboxClient()
    streaming = asyncio.Event()

    async def _stream(_messages: list[ModelMessage], _info: AgentInfo):
        streaming.set()
        yield "thinking about it"
        await asyncio.Event().wait()  # only a cancel leaves this

    await _start(
        engine,
        session_factory,
        user=user,
        project=project,
        conversation=conversation,
        model=FunctionModel(stream_function=_stream),
        manager=manager,
        client=client,
    )
    await asyncio.wait_for(streaming.wait(), timeout=10)
    await publish_cooperative_stop(conversation.id)
    state = engine.peek(conversation.id)
    assert state is not None
    for _ in range(50):  # several lease ticks at the brisk cadence
        await asyncio.sleep(0.001)

    assert state.cooperative_stop_requested is False
    assert await fake_redis.get(cooperative_stop_key(conversation.id)) is not None

    assert await engine.stop_user_turn_and_wait(user.id, timeout_s=10) is StopOutcome.STOPPED
    assert state.status == "stopped"
    assert state.ring[-1].type == "turn_ended"


# --- the ask cannot reach past the turn it was aimed at ----------------------------------------


async def test_an_ask_for_another_conversation_leaves_this_turn_alone(
    _fresh_engine: TurnEngine,
    db_session: AsyncSession,
    session_factory,
    fake_redis: aioredis.Redis,
    fake_storage,
    _a_brisk_lease: None,
) -> None:
    """★ WHY THE KEY IS NOT THE LEASE. The liveness lease is per USER and the registry hash is
    per user; an ask riding either would be read by whichever of this citizen's turns looked
    first — including the fresh turn on the project they just opened, which is the one turn a
    switch must never stop. Keyed by conversation, a stop aimed elsewhere is invisible here.

    Mutation check: publish the ask against this turn's own conversation instead and it stops at
    its first tool result — which is what makes the absence below an observation rather than a
    fixture that could never have stopped."""
    engine = _fresh_engine
    user, project, conversation = await _build_chat(db_session, "sb9@rvaiglobal.com")
    manager, client = SessionManager(), FakeSandboxClient()
    model, counts = _scripted(
        [[_WROTE_A_FILE], [("declare_done", '{"summary": "added the column"}')]]
    )

    await publish_cooperative_stop(uuid.uuid7())  # somebody else's chat, same citizen
    await _start(
        engine,
        session_factory,
        user=user,
        project=project,
        conversation=conversation,
        model=model,
        manager=manager,
        client=client,
    )
    state = await _settled(engine, conversation.id)

    assert state.cooperative_stop_requested is False
    assert state.status == "completed"
    assert state.end_reason is None
    assert counts["requests"] == 2  # it ran to its own end


async def test_the_ask_dies_with_the_turn_it_was_aimed_at(
    _fresh_engine: TurnEngine,
    db_session: AsyncSession,
    session_factory,
    fake_redis: aioredis.Redis,
    fake_storage,
) -> None:
    """An ask that arrives in the seconds a turn was already unwinding is never read, and
    PRESENCE OF THE KEY IS THE ASK — left behind, it would end the citizen's very next message
    in this conversation at its first tool result, on the strength of a stop meant for a turn
    that was already over.

    The lease cadence is left at its production value on purpose: the loop has taken its one
    tick before the ask is published and will not look again, which is exactly the shape being
    guarded. Mutation check: remove the withdrawal from `_stop_liveness_lease` and the key
    survives the turn."""
    engine = _fresh_engine
    user, project, conversation = await _build_chat(db_session, "sb10@rvaiglobal.com")
    manager, client = SessionManager(), _StallsInsideTheWrite()
    model, _ = _scripted([[_WROTE_A_FILE]])

    turn_id = await _start(
        engine,
        session_factory,
        user=user,
        project=project,
        conversation=conversation,
        model=model,
        manager=manager,
        client=client,
    )
    await asyncio.wait_for(client.inside.wait(), timeout=10)
    await publish_cooperative_stop(conversation.id)
    assert await fake_redis.get(cooperative_stop_key(conversation.id)) is not None

    await engine.stop_turn(conversation.id, turn_id)
    client.let_go.set()
    state = await _settled(engine, conversation.id)

    assert state.status == "stopped"
    assert await fake_redis.get(cooperative_stop_key(conversation.id)) is None


# --- the bound, and the hole a stop inside an `except` arm used to fall through ---------------


def test_the_bound_is_derived_from_the_slow_tool_budget_not_the_run_deadline() -> None:
    """R11's wait is sized by WHAT STANDS BETWEEN THE ASK AND THE BOUNDARY — the tool call in
    flight — and the longest legitimate one is a slow `run_command`, which a cold-base install
    routinely spends in full. The run deadline bounds a whole build with its repair rounds, so a
    wait cut from it would spare a wedged container for half an hour to save a boundary that was
    never coming.

    Mutation check: derive the grace from `RUN_WALL_CLOCK_DEADLINE_S` and both assertions move."""
    assert COOPERATIVE_STOP_GRACE_S >= RUN_COMMAND_SLOW_TIMEOUT_S
    # …plus the delivery latency, because an ask raised in another process is only seen when the
    # turn's own lease loop next looks.
    assert COOPERATIVE_STOP_GRACE_S >= RUN_COMMAND_SLOW_TIMEOUT_S + (
        LIVENESS_LEASE_RENEW_CADENCE_SECONDS
    )
    assert COOPERATIVE_STOP_GRACE_S < RUN_WALL_CLOCK_DEADLINE_S


async def test_a_cancel_inside_an_except_arm_still_reaches_the_turns_ending(
    _fresh_engine: TurnEngine,
    db_session: AsyncSession,
    fake_redis: aioredis.Redis,
    fake_storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★ THE SIBLING-`except` HOLE, driven with a REAL `task.cancel()` rather than a flag.

    A turn already unwinding through one `except` arm bills before it finishes. A cancellation
    delivered in that await is not the `except asyncio.CancelledError` arm's to catch — a sibling
    `except` never catches what another one is unwinding through — so it escaped past `_finish`
    entirely: no terminal frame, no terminal row, and a turn every subscriber reads as still
    running until its own stall timeout. No stop may leave a turn reported as running.

    Mutation check: call `_bill_once` directly from the persist-failed arm again and
    `state.status` reads "running" here."""
    engine = _fresh_engine
    user, project, conversation = await _build_chat(
        db_session, "sb11@rvaiglobal.com", kind=ChatKind.PLAN
    )
    manager, client = SessionManager(), FakeSandboxClient()
    persist_failed = asyncio.Event()
    billing = asyncio.Event()

    async def _no_room(*_a: object, **_k: object):
        persist_failed.set()
        raise RuntimeError("the transcript could not be written")

    monkeypatch.setattr(engine_module, "append_batch", _no_room)

    @contextlib.asynccontextmanager
    async def _session():
        # The FIRST session opened after the persist blew up is the billing one, which is the
        # await the cancellation has to land in for this to be testing anything.
        if persist_failed.is_set() and not billing.is_set():
            billing.set()
            await asyncio.Event().wait()
        yield db_session

    await _start(
        engine,
        lambda: _session(),
        user=user,
        project=project,
        conversation=conversation,
        model=FunctionModel(stream_function=_streams_a_sentence),
        manager=manager,
        client=client,
    )
    await asyncio.wait_for(billing.wait(), timeout=10)
    state = engine.peek(conversation.id)
    assert state is not None and state.task is not None

    state.task.cancel()
    state = await _settled(engine, conversation.id)

    assert state.status == "stopped"
    assert state.end_reason == STOPPED_BY_USER
    assert state.ring[-1].type == "turn_ended"
    assert conversation.id not in _mid_reply  # …and the conversation is not wedged shut


async def _streams_a_sentence(_messages: list[ModelMessage], _info: AgentInfo):
    yield "here is what I found."
