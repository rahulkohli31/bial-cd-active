"""U5 — a WRITE turn on the chat engine: the convergence, tested at the engine seam.

Write used to be a build: its own agent, its own harness, its own SSE feed, its own metering.
It is now an ordinary turn that happens to hold the sandbox six, and these tests pin the four
properties that were easiest to lose in the move.

- THE SAVE. `finish_write_turn` runs on every terminal arm, including the cancelled one.
  Without it a Write turn reports success and the reaper deletes the work.
- THE METER. The cap is enforced before EVERY model request, in its own session, and each
  step's tokens are recorded as they are spent — not once at the end, where a build that dies
  at step 40 would have been free.
- THE FOLD. One turn, one billing. The per-step `record_usage` is the ONLY fold; the turn-level
  `_bill_once` must not add a second copy on top.
- THE MUTATION GUARD. A Write turn where the model only read files is an ordinary chat turn.
  It must not pay for a 30s verify pass, and must not be nudged to keep going.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid

import pytest
import redis.asyncio as aioredis
import sqlalchemy as sa
from pydantic import SecretStr
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models.function import (
    AgentInfo,
    DeltaToolCall,
    DeltaToolCalls,
    FunctionModel,
)
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings
from src.db.models.conversation import ConversationMode
from src.db.models.message import Message, MessageEntryKind
from src.db.models.token_usage import TokenUsage
from src.services.agent.mode_prompts import PromptContext
from src.services.build_sessions.manager import SessionManager
from src.services.sandbox.config import SandboxConfig
from src.services.storage import snapshot_key
from src.services.turns.engine import TurnEngine, set_turn_engine_for_tests
from src.services.turns.guard import _mid_reply
from tests.factories import ConversationFactory, ProjectFactory, UserFactory
from tests.fakes import FakeSandboxClient

_CTX = PromptContext(user_name="Ada", project_name="Visitors", project_description=None)


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


async def _write_conversation(db: AsyncSession, email: str):
    user = await UserFactory.create(db, email=email)
    project = await ProjectFactory.create(db, user.id)
    conv = await ConversationFactory.create(
        db, user.id, project_id=project.id, mode=ConversationMode.WRITE
    )
    return user, project, conv


def _scripted(
    turns: list[list[list[tuple[str, str]] | str]],
) -> tuple[FunctionModel, dict[str, int]]:
    """A STREAMING scripted model — one canned response list per `agent.iter` run, so a test
    can drive the self-heal loop's second and third passes.

    Streaming, not `FunctionModel(respond)`, because the engine walks the run node by node and
    calls `node.stream(...)`: that is what produces the live `text_delta` and `step` frames, and
    a non-streaming model cannot serve it. `counts["requests"]` is how many model requests
    actually fired — the number the daily-cap gate exists to bound.

    Each scripted step is either a plain string (a text reply) or a list of `(tool, json_args)`.
    """
    counts = {"requests": 0, "runs": 0}
    scripts = iter(turns)
    current: list[list[tuple[str, str]] | str] = []

    async def _stream(_messages: list[ModelMessage], _info: AgentInfo):
        nonlocal current
        counts["requests"] += 1
        if not current:
            counts["runs"] += 1
            current = list(next(scripts, ["done."]))
        step = current.pop(0)
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


_WROTE_A_FILE = [("write_file", '{"path": "app/page.tsx", "file_text": "x"}')]
_DECLARED_DONE = [("declare_done", '{"summary": "added the column"}')]
_READ_A_FILE = [("read_file", '{"path": "app/page.tsx"}')]


async def _run(
    engine: TurnEngine,
    db: AsyncSession,
    session_factory,
    model,
    *,
    user,
    project,
    conv,
    manager: SessionManager,
    client: FakeSandboxClient,
    prompt: str = "add a status column",
):
    async def _noop() -> None:
        return None

    turn_id = await engine.start_turn(
        conversation=conv,
        user_id=user.id,
        prompt=prompt,
        history=[],
        prompt_context=_CTX,
        app_id=None,
        project_id=project.id,
        model=model,
        session_factory=session_factory,
        persist_user_turn=_noop,
        manager=manager,
        sandbox_client=client,
    )
    state = engine.peek(conv.id)
    assert state is not None and state.task is not None
    with contextlib.suppress(asyncio.CancelledError):
        await state.task
    return turn_id, state


# --- the save ----------------------------------------------------------------


async def test_a_write_turn_saves_its_work_at_the_terminal(
    _fresh_engine, db_session, session_factory, fake_redis: aioredis.Redis, fake_storage
) -> None:
    """★ THE P0. `write_snapshot` is the only thing that pushes the tree to Blob storage, and
    on this branch the only caller was the build harness. Delete the `finish_write_turn` call
    from `_run_turn`'s `finally` and this goes red — which is a Write turn that reports
    success while the reaper quietly deletes the user's app."""
    engine = _fresh_engine
    user, project, conv = await _write_conversation(db_session, "wt1@rvaiglobal.com")
    manager, client = SessionManager(), FakeSandboxClient()
    model, _ = _scripted([[_WROTE_A_FILE, _DECLARED_DONE]])

    _, state = await _run(
        engine,
        db_session,
        session_factory,
        model,
        user=user,
        project=project,
        conv=conv,
        manager=manager,
        client=client,
    )

    assert state.write_session is not None
    assert snapshot_key(state.write_session.app_id) in fake_storage.objects
    # Pardoned, not torn down: the container IS the preview the user is looking at.
    assert client.torn_down == []
    assert manager.active_session_for(user.id) is None  # the slot is free for the next turn


async def test_a_stopped_write_turn_still_saves(
    _fresh_engine, db_session, session_factory, fake_redis: aioredis.Redis, fake_storage
) -> None:
    """★ The cancelled path — the one an unshielded save silently drops. A user hits Stop
    precisely when they want to KEEP what has been written so far; losing it here is the
    worst version of this bug because it looks deliberate."""
    engine = _fresh_engine
    user, project, conv = await _write_conversation(db_session, "wt2@rvaiglobal.com")
    manager, client = SessionManager(), FakeSandboxClient()
    started = asyncio.Event()

    # Write a file, then BLOCK. Stop lands while the run is mid-flight, which is the only
    # state where an unshielded save would be cancelled out from under itself.
    wrote = asyncio.Event()

    async def _stream(messages: list[ModelMessage], _info: AgentInfo):
        started.set()
        if not wrote.is_set():
            wrote.set()
            yield DeltaToolCalls(
                {
                    0: DeltaToolCall(
                        name="write_file",
                        json_args='{"path": "app/page.tsx", "file_text": "x"}',
                        tool_call_id="c-write-1",
                    )
                }
            )
            return
        await asyncio.sleep(30)  # cancelled by the stop
        yield "unreachable"

    async def _noop() -> None:
        return None

    turn_id = await engine.start_turn(
        conversation=conv,
        user_id=user.id,
        prompt="build the thing",
        history=[],
        prompt_context=_CTX,
        app_id=None,
        project_id=project.id,
        model=FunctionModel(stream_function=_stream),
        session_factory=session_factory,
        persist_user_turn=_noop,
        manager=manager,
        sandbox_client=client,
    )
    state = engine.peek(conv.id)
    assert state is not None and state.task is not None
    await asyncio.wait_for(started.wait(), timeout=5)
    await engine.stop_turn(conv.id, turn_id)
    with contextlib.suppress(asyncio.CancelledError):
        await state.task

    assert state.status == "stopped"
    assert state.write_session is not None
    assert snapshot_key(state.write_session.app_id) in fake_storage.objects


# --- the meter and the fold --------------------------------------------------


async def test_the_daily_cap_is_checked_before_every_model_request(
    _fresh_engine,
    db_session,
    session_factory,
    fake_redis: aioredis.Redis,
    fake_storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★ The route checks the cap once, at the top. A build runs for minutes and dozens of
    requests, so a per-route check lets one turn spend a whole day's budget after passing it.
    Move the gate out of the node walk and this goes red: the run keeps firing requests long
    after the cap was crossed."""
    engine = _fresh_engine
    user, project, conv = await _write_conversation(db_session, "wt3@rvaiglobal.com")
    manager, client = SessionManager(), FakeSandboxClient()
    seen: list[uuid.UUID] = []

    async def _gate(_db, user_id: uuid.UUID) -> None:
        seen.append(user_id)
        if len(seen) >= 3:  # the cap is crossed on the third ASK, before the request fires
            from src.services.usage.gate import DailyTokenLimitExceededError

            raise DailyTokenLimitExceededError(limit=1000, used=1200)

    monkeypatch.setattr("src.services.turns.engine.enforce_daily_limit", _gate)
    if True:
        model, counts = _scripted(
            [
                [
                    _READ_A_FILE,
                    [("read_file", '{"path": "app/layout.tsx"}')],
                    [("read_file", '{"path": "package.json"}')],
                    "done.",
                ]
            ]
        )
        _, state = await _run(
            engine,
            db_session,
            session_factory,
            model,
            user=user,
            project=project,
            conv=conv,
            manager=manager,
            client=client,
        )

    assert len(seen) == 3  # gate ran per request…
    assert counts["requests"] == 2  # …and the third request NEVER fired
    assert state.status == "failed"
    assert state.end_reason == "quota_exceeded"
    # The copy names tomorrow, not support: a spent budget is not a malfunction.
    assert "budget" in (state.error_message or "")


async def test_one_write_turn_folds_its_usage_exactly_once(
    _fresh_engine, db_session, session_factory, fake_redis: aioredis.Redis, fake_storage
) -> None:
    """★ The double-count. Per-step `record_usage` is the ONLY fold; passing the turn's
    accumulator to the run as well would make `_bill_once` add a second full copy at the
    terminal and silently double every build's daily spend. Restore `usage=turn_usage` on the
    Write run and the total here doubles."""
    engine = _fresh_engine
    user, project, conv = await _write_conversation(db_session, "wt4@rvaiglobal.com")
    manager, client = SessionManager(), FakeSandboxClient()
    model, _ = _scripted([[_DECLARED_DONE]])

    await _run(
        engine,
        db_session,
        session_factory,
        model,
        user=user,
        project=project,
        conv=conv,
        manager=manager,
        client=client,
    )

    rows = (
        (await db_session.execute(sa.select(TokenUsage).where(TokenUsage.user_id == user.id)))
        .scalars()
        .all()
    )
    # One row per MODEL STEP — never a second, turn-level row on top of them.
    assert len(rows) == 1


# --- the mutation guard ------------------------------------------------------


async def test_a_read_only_write_turn_is_just_a_chat_turn(
    _fresh_engine,
    db_session,
    session_factory,
    fake_redis: aioredis.Redis,
    fake_storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★ The model answered a question in Write mode without touching anything. Verifying
    that would spend 30 seconds of the user's time and a `tsc` run to confirm nothing changed,
    then nudge the model to keep going on a task it had already finished. Make `verify`
    unconditional and this goes red."""
    engine = _fresh_engine
    user, project, conv = await _write_conversation(db_session, "wt5@rvaiglobal.com")
    manager, client = SessionManager(), FakeSandboxClient()
    verified: list[int] = []

    async def _verify(*_a: object, **_k: object):
        verified.append(1)
        raise AssertionError("verify must not run for a turn that changed nothing")

    monkeypatch.setattr("src.services.turns.engine.verify", _verify)
    if True:
        model, counts = _scripted([[_READ_A_FILE, "It renders the visitor list."]])
        _, state = await _run(
            engine,
            db_session,
            session_factory,
            model,
            user=user,
            project=project,
            conv=conv,
            manager=manager,
            client=client,
            prompt="what does the home page do?",
        )

    assert verified == []
    assert state.status == "completed"
    assert counts["runs"] == 1  # no CONTINUE_PROMPT second pass either


# --- persistence -------------------------------------------------------------


async def test_no_write_step_row_ever_carries_a_user_prompt(
    _fresh_engine, db_session, session_factory, fake_redis: aioredis.Redis, fake_storage
) -> None:
    """★ The duplicate-seed defect, killed structurally. The user's prompt is already durable
    (written before the run), so a step row that also carried it would render a SECOND user
    bubble in the transcript. Drop `_persistable_messages` from `_persist_write_step` and this
    goes red."""
    engine = _fresh_engine
    user, project, conv = await _write_conversation(db_session, "wt6@rvaiglobal.com")
    manager, client = SessionManager(), FakeSandboxClient()
    model, _ = _scripted([[_WROTE_A_FILE, _DECLARED_DONE]])

    await _run(
        engine,
        db_session,
        session_factory,
        model,
        user=user,
        project=project,
        conv=conv,
        manager=manager,
        client=client,
    )

    rows = (
        (
            await db_session.execute(
                sa.select(Message)
                .where(Message.conversation_id == conv.id)
                .where(Message.entry_kind == MessageEntryKind.STEP)
            )
        )
        .scalars()
        .all()
    )
    assert rows, "the turn's steps must be durable"
    for row in rows:
        kinds = {
            part.get("part_kind") for message in row.payload for part in message.get("parts", [])
        }
        assert "user-prompt" not in kinds, f"step row {row.seq} carries a user prompt"
