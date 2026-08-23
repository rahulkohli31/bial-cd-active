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
from collections.abc import Callable

import pytest
import redis.asyncio as aioredis
import sqlalchemy as sa
from pydantic import SecretStr
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
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
from src.services.build_sessions.alarms import HMR_PROTOCOL_DRIFT_EVENT
from src.services.build_sessions.manager import SessionManager
from src.services.orchestrator.deps import SandboxSession
from src.services.orchestrator.selfheal import VerifyOutcome
from src.services.sandbox import DevStatus, SandboxError, SandboxHandle
from src.services.sandbox.base import CompileReport, CompileState
from src.services.sandbox.client import _ALREADY_RUNNING_PID
from src.services.sandbox.config import SandboxConfig
from src.services.storage import snapshot_key
from src.services.turns import engine as engine_module
from src.services.turns.engine import TurnEngine, _TurnState, set_turn_engine_for_tests
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
    history: list[ModelMessage] | None = None,
    expects_mutation: bool = False,
):
    async def _noop() -> None:
        return None

    turn_id = await engine.start_turn(
        conversation=conv,
        user_id=user.id,
        prompt=prompt,
        history=history or [],
        prompt_context=_CTX,
        app_id=None,
        project_id=project.id,
        model=model,
        session_factory=session_factory,
        persist_user_turn=_noop,
        manager=manager,
        sandbox_client=client,
        expects_mutation=expects_mutation,
    )
    state = engine.peek(conv.id)
    assert state is not None and state.task is not None
    with contextlib.suppress(asyncio.CancelledError):
        await state.task
    return turn_id, state


# --- the save ----------------------------------------------------------------


async def test_a_turn_terminal_frees_the_slot_without_saving(
    _fresh_engine, db_session, session_factory, fake_redis: aioredis.Redis, fake_storage
) -> None:
    """★ THE SAVE MODEL (KTD-5e). The terminal used to snapshot, which made every message a new
    saved version and left the user no way to try something and walk away from it. Saving is
    their click now; the terminal's job is to free the slot and leave the container up.

    Mutation-check: restore the `write_snapshot` call in `finish_turn_sandbox` and this goes
    red."""
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
    assert snapshot_key(state.write_session.app_id) not in fake_storage.objects  # nothing saved
    assert client.torn_down == []  # …and the container is still up, holding the work
    assert manager.active_session_for(user.id) is None  # the slot is free for the next message


async def test_a_stopped_turn_leaves_the_work_in_the_container(
    _fresh_engine, db_session, session_factory, fake_redis: aioredis.Redis, fake_storage
) -> None:
    """Stop used to be a save point, because the terminal saved. It is not one now — the work
    stays in the live container and the user decides whether to keep it. What Stop must still
    guarantee is that the container survives, or stopping would destroy what it interrupted."""
    engine = _fresh_engine
    user, project, conv = await _write_conversation(db_session, "wt2@rvaiglobal.com")
    manager, client = SessionManager(), FakeSandboxClient()
    started = asyncio.Event()
    wrote = asyncio.Event()

    async def _stream(messages: list[ModelMessage], _info: AgentInfo):
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
        # Only reachable once the tool ALREADY RAN (request -> tools -> request), so the stop
        # lands after a real mutation rather than racing it.
        started.set()
        await asyncio.sleep(30)
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
    assert client.torn_down == []  # the work is still reachable, and still the user's to keep
    assert manager.active_session_for(user.id) is None


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
    unconditional and this goes red.

    HALF ONE OF A PAIR. Its twin below is the same zero-mutation outcome reached from a
    Build-it click, where it is a failure. The difference is `expects_mutation` and nothing
    else, so both halves must be pinned or a fix to either silently rewrites the other."""
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
    assert state.end_reason is None  # nothing to explain — the turn did what was asked
    assert counts["runs"] == 1  # no CONTINUE_PROMPT second pass either


async def test_a_build_that_wrote_nothing_fails_instead_of_reporting_success(
    _fresh_engine,
    db_session,
    session_factory,
    fake_redis: aioredis.Redis,
    fake_storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★ THE ZERO-FILE BUILD. A live run read a file, said something reassuring, touched
    NOTHING — and the citizen was told "Build complete — your app is live below" over a
    container still serving the 145-character golden template, 65k tokens later. The guard
    above is right for a chat turn and catastrophic for a build: a turn started from a plan
    card was ASKED to build, so a zero-mutation outcome is a failure, not a quiet success.

    The copy must say plainly that nothing was built, and must not claim the work is saved —
    there is no auto-save (KTD-5e), and here there is not even any work to save.

    Mutation-check: restore the bare `return` in the mutation guard and this goes red on
    `status == "completed"`."""
    engine = _fresh_engine
    user, project, conv = await _write_conversation(db_session, "wt13@rvaiglobal.com")
    manager, client = SessionManager(), FakeSandboxClient()

    async def _verify(*_a: object, **_k: object):
        raise AssertionError("a build that changed nothing must not pay for a verify pass")

    monkeypatch.setattr(engine_module, "verify", _verify)
    model, counts = _scripted([[_READ_A_FILE, "All set — your app is live below."]])

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
        prompt="Execute the approved plan below.\n\n1. Build the visitor log",
        expects_mutation=True,
    )

    assert state.status == "failed"
    assert state.end_reason == "build_wrote_nothing"
    message = state.error_message or ""
    assert "nothing" in message.lower()  # named plainly, not as "the assistant hit a problem"
    assert "unchanged" in message  # …and the app's actual state, said out loud
    assert "saved" not in message  # no auto-save exists to claim (KTD-5e)
    assert counts["runs"] == 1  # it ends; it does not nudge the model round again


async def test_declaring_done_is_not_evidence_that_anything_was_built(
    _fresh_engine,
    db_session,
    session_factory,
    fake_redis: aioredis.Redis,
    fake_storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★ THE SECOND DOOR INTO THE SAME LIE. The guard above asks "did anything change?", but it
    asked it as `workspace_touched OR done_requested` — and `declare_done` set BOTH flags. So a
    model that wrote not one file and simply declared itself finished satisfied the guard and
    collected "Build complete — your app is live below" over an untouched template: the exact
    outcome the guard exists to prevent, reached by asking the accused for a character
    reference.

    A claim is not a mutation. On a turn that was ASKED to build, only a real write counts.

    Mutation-check: restore `session.workspace_touched = True` in `declare_done` (or put
    `done_requested` back into the build-path condition) and this goes red on `status`."""
    engine = _fresh_engine
    user, project, conv = await _write_conversation(db_session, "wt13b@rvaiglobal.com")
    manager, client = SessionManager(), FakeSandboxClient()

    async def _verify(*_a: object, **_k: object):
        raise AssertionError("a build that changed nothing must not pay for a verify pass")

    monkeypatch.setattr(engine_module, "verify", _verify)
    model, counts = _scripted([[_DECLARED_DONE, "Done — your app is live below."]])

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
        prompt="Execute the approved plan below.\n\n1. Build the visitor log",
        expects_mutation=True,
    )

    assert state.status == "failed"
    assert state.end_reason == "build_wrote_nothing"
    assert "unchanged" in (state.error_message or "")
    assert counts["runs"] == 1  # it ends; it does not nudge the model round again


# --- the self-heal budget's two endings ---------------------------------------


async def test_budget_exhaustion_with_a_green_app_does_not_claim_an_error(
    _fresh_engine,
    db_session,
    session_factory,
    fake_redis: aioredis.Redis,
    fake_storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★ The green ending: every verify passed and the model simply never called
    `declare_done`. The old copy told this user their app "still has an error" — a defect
    hunt with no defect — and claimed the work was "saved" when nothing auto-saves (KTD-5e).
    The copy the user sees must say the app checks out, and that the changes sit in the
    workspace awaiting THEIR Save click."""
    engine = _fresh_engine
    user, project, conv = await _write_conversation(db_session, "wt11@rvaiglobal.com")
    manager, client = SessionManager(), FakeSandboxClient()
    monkeypatch.setattr(engine_module, "SELF_HEAL_MAX_RETRIES", 0)
    model, _ = _scripted([[_WROTE_A_FILE, "made the change."]])

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

    assert state.status == "failed"
    assert state.end_reason == "self_heal_budget_exhausted"
    message = state.error_message or ""
    assert "checks out" in message  # the app is fine, and the copy says so
    assert "error" not in message  # no invented defect to hunt for
    assert "saved" not in message  # no auto-save exists to claim (KTD-5e)
    assert "workspace" in message and "Save" in message  # the truthful keep-it instruction


async def test_budget_exhaustion_with_a_red_app_still_names_the_error(
    _fresh_engine,
    db_session,
    session_factory,
    fake_redis: aioredis.Redis,
    fake_storage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The red ending keeps its honest half — an outstanding error is still reported as
    one — but drops the false "saved" claim alongside it."""
    engine = _fresh_engine
    user, project, conv = await _write_conversation(db_session, "wt12@rvaiglobal.com")
    manager, client = SessionManager(), FakeSandboxClient()
    monkeypatch.setattr(engine_module, "SELF_HEAL_MAX_RETRIES", 0)

    async def _red_verify(*_a: object, **_k: object) -> tuple[VerifyOutcome, int]:
        return VerifyOutcome(green=False, dev_ready=False, error=None, preview_url=None), 0

    monkeypatch.setattr(engine_module, "verify", _red_verify)
    model, _ = _scripted([[_WROTE_A_FILE, "tried my best."]])

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

    assert state.status == "failed"
    assert state.end_reason == "self_heal_budget_exhausted"
    message = state.error_message or ""
    assert "still has an error" in message
    assert "saved" not in message  # honest about the error, honest about the save model too
    assert "workspace" in message


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


def _build_it_shaped_history() -> list[ModelMessage]:
    """The trigger shape KTD-7 root-caused, structural to every Build-it: a DB-loaded
    ModelResponse (provider fields set, as loaded responses have) followed by THREE
    consecutive ModelRequests — the plan-options resolution appended as its own row, the
    mode-switch marker, and the seeded build prompt. pydantic-ai's `_clean_message_history`
    merges the three requests into one, the list shrinks by two, and a cursor measured with
    `len()` on the PRE-clean list overshoots the run's first ModelResponse."""
    return [
        ModelRequest(parts=[UserPromptPart(content="I need a visitor log app")]),
        ModelResponse(
            parts=[
                TextPart(content="Here is the plan."),
                ToolCallPart(
                    tool_name="present_plan_options", args={"options": []}, tool_call_id="plan-1"
                ),
            ],
            model_name="claude-test",
            provider_name="anthropic",
        ),
        ModelRequest(
            parts=[
                ToolReturnPart(
                    tool_name="present_plan_options", content="build", tool_call_id="plan-1"
                )
            ]
        ),
        ModelRequest(parts=[UserPromptPart(content="[mode changed: plan → write]")]),
        ModelRequest(parts=[UserPromptPart(content="Build the visitor log app as planned.")]),
    ]


def _flattened_pairing_violations(rows) -> list[str]:
    """Tool answers persisted with NO same-id tool-call at a strictly earlier flattened
    (row-seq, message, part) position — each one is a stored 400 (KTD-7 / U5's orphan)."""
    calls: dict[str, int] = {}
    violations: list[str] = []
    position = 0
    for row in rows:
        for message in row.payload:
            for part in message.get("parts", []):
                position += 1
                kind = part.get("part_kind")
                if kind == "tool-call":
                    calls.setdefault(part["tool_call_id"], position)
                elif kind in ("tool-return", "retry-prompt") and part.get("tool_name"):
                    called_at = calls.get(part["tool_call_id"])
                    if called_at is None or called_at >= position:
                        violations.append(part["tool_call_id"])
    return violations


async def _all_rows(db: AsyncSession, conv) -> list[Message]:
    return list(
        (
            await db.execute(
                sa.select(Message).where(Message.conversation_id == conv.id).order_by(Message.seq)
            )
        )
        .scalars()
        .all()
    )


async def test_a_build_it_run_persists_every_tool_call_its_answers_need(
    _fresh_engine, db_session, session_factory, fake_redis: aioredis.Redis, fake_storage
) -> None:
    """★ KTD-7 — the write half of the round-3 P0, asserted over RAW rows (`load_history`
    would mask it: its U5 repair drops exactly the orphan this bug mints). With a Build-it-
    shaped history the pre-clean cursor overshoots by two and the run's first ModelResponse —
    the row carrying the write_file tool call — is never persisted; the stored history then
    holds a `tool_result` with no `tool_use` and Anthropic 400s every later turn.

    Mutation-check: restore `persisted_from = len(messages)` as the only cursor origin and
    this goes red."""
    engine = _fresh_engine
    user, project, conv = await _write_conversation(db_session, "wt8@rvaiglobal.com")
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
        history=_build_it_shaped_history(),
        prompt="Build the visitor log app as planned.",
    )

    rows = await _all_rows(db_session, conv)
    assert rows, "the run must persist step rows"
    assert _flattened_pairing_violations(rows) == []
    # The steady state holds from row two onward; row ONE is the regression: it must begin
    # with the run's first ModelResponse, not with that response's now-orphaned returns.
    step_rows = [row for row in rows if row.entry_kind == MessageEntryKind.STEP]
    assert step_rows and step_rows[0].payload[0]["kind"] == "response"


async def test_an_ordinary_write_turn_keeps_its_healthy_row_shape(
    _fresh_engine, db_session, session_factory, fake_redis: aioredis.Redis, fake_storage
) -> None:
    """The fix changes no healthy shape: with ≤2 consecutive requests in the loaded history
    nothing merges, the cursor lands where it always did, and the first step row still begins
    with the run's first ModelResponse."""
    engine = _fresh_engine
    user, project, conv = await _write_conversation(db_session, "wt9@rvaiglobal.com")
    manager, client = SessionManager(), FakeSandboxClient()
    model, _ = _scripted([[_WROTE_A_FILE, _DECLARED_DONE]])
    history: list[ModelMessage] = [
        ModelRequest(parts=[UserPromptPart(content="hello")]),
        ModelResponse(
            parts=[TextPart(content="hi — what shall we build?")],
            model_name="claude-test",
            provider_name="anthropic",
        ),
        ModelRequest(parts=[UserPromptPart(content="[mode changed: ask → write]")]),
    ]

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
        history=history,
    )

    rows = await _all_rows(db_session, conv)
    assert _flattened_pairing_violations(rows) == []
    step_rows = [row for row in rows if row.entry_kind == MessageEntryKind.STEP]
    assert step_rows and step_rows[0].payload[0]["kind"] == "response"


async def test_a_genuinely_empty_delta_still_noops(
    _fresh_engine, db_session, session_factory, fake_redis: aioredis.Redis
) -> None:
    # With the cursor origin fixed, `if not delta` genuinely means "nothing new" — the persist
    # returns the advanced cursor and writes no row.
    engine = _fresh_engine
    user, project, conv = await _write_conversation(db_session, "wt10@rvaiglobal.com")
    state = _TurnState(
        turn_id=uuid.uuid4(),
        conversation_id=conv.id,
        user_id=user.id,
        mode=ConversationMode.WRITE,
    )
    history: list[ModelMessage] = [ModelRequest(parts=[UserPromptPart(content="x")])]

    cursor = await engine._persist_write_step(
        state, history=history, persisted_from=1, session_factory=session_factory
    )

    assert cursor == 1
    assert await _all_rows(db_session, conv) == []


# --- U2: the dev server boots when the turn attaches -------------------------


class _NoFreeLunchSandbox(FakeSandboxClient):
    """A container whose dev server does NOT run until somebody starts it.

    The shared fake answers `/dev/status` with `ready=True` unconditionally, which quietly
    hands every test a preview nobody paid for — and would let U2's regression guard pass
    without U2. This one couples status to start the way a real supervisor does, so "who
    started the server, and when" becomes an observable fact rather than a fixture's gift.
    """

    def __init__(
        self,
        *,
        fail_starts: int = 0,
        already_running: bool = False,
        on_start: Callable[[], None] | None = None,
    ) -> None:
        super().__init__()
        self.dev_starts: list[str] = []
        self.serving = already_running
        self._fail_starts = fail_starts
        self._on_start = on_start

    async def dev_start(
        self, handle: SandboxHandle, *, cmd: list[str] | None = None, cwd: str | None = None
    ) -> int:
        self.dev_starts.append(handle.app_name)
        if self._on_start is not None:
            self._on_start()
        if self._fail_starts > 0:
            self._fail_starts -= 1
            raise SandboxError("dev/start failed with status 502")
        was_serving, self.serving = self.serving, True
        # The unowned-server 409 the real client maps to `_ALREADY_RUNNING_PID`.
        return _ALREADY_RUNNING_PID if was_serving else 4321

    async def dev_status(self, handle: SandboxHandle) -> DevStatus:
        return DevStatus(running=self.serving, ready=self.serving, port=3000)


def _preview_ready_frames(state: _TurnState) -> list[object]:
    return [
        f
        for f in state.ring
        if getattr(f, "type", None) == "preview" and getattr(f, "state", None) == "ready"
    ]


async def test_dev_start_fires_at_attach_before_the_model_runs(
    _fresh_engine, db_session, session_factory, fake_redis: aioredis.Redis, fake_storage
) -> None:
    """★ U2 (R2). Next's first route compile is 5-7s and it used to start only after the whole
    model run plus a `tsc`. Booting it at attach overlaps that compile with the model's own
    first request. The assertion is on ORDERING, not on a call count: `dev_start` must land
    before request one, or the overlap it exists to buy never happens."""
    engine = _fresh_engine
    user, project, conv = await _write_conversation(db_session, "wt11@rvaiglobal.com")
    manager = SessionManager()
    model, counts = _scripted([[_WROTE_A_FILE, _DECLARED_DONE]])
    requests_when_started: list[int] = []
    client = _NoFreeLunchSandbox(on_start=lambda: requests_when_started.append(counts["requests"]))

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

    assert requests_when_started == [0], "dev_start must precede the model's first request"
    assert len(client.dev_starts) == 1, "a healthy attach starts the server exactly once"


async def test_a_read_only_turn_starts_the_dev_server_too(
    _fresh_engine, db_session, session_factory, fake_redis: aioredis.Redis, fake_storage
) -> None:
    """★ THE REGRESSION GUARD for a whole class. A Write turn where the model only READ files
    trips the mutation guard and returns before verify — and verify was the only thing that
    ever started the dev server. So this turn produced no preview at all, ever. Attaching is
    now what starts it, so reading is enough."""
    engine = _fresh_engine
    user, project, conv = await _write_conversation(db_session, "wt12@rvaiglobal.com")
    manager, client = SessionManager(), _NoFreeLunchSandbox()
    model, _ = _scripted([[_READ_A_FILE, "it renders the visitor table."]])

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
        prompt="what does the page do?",
    )

    assert client.dev_starts, "a read-only turn skips verify — attach must start the server"
    assert client.serving, "…and the server must actually be up when the turn ends"
    assert state.sandbox is not None and not state.sandbox.workspace_touched, (
        "guard the premise: this turn wrote nothing, so the mutation guard skipped verify"
    )


async def test_an_already_serving_container_neither_raises_nor_double_frames(
    _fresh_engine, db_session, session_factory, fake_redis: aioredis.Redis, fake_storage
) -> None:
    """Attaching to a container whose dev server is already up is the common case on the second
    message of a conversation. The supervisor answers 409, the client maps it to the
    already-running sentinel, and the turn must neither raise nor draw a second preview."""
    engine = _fresh_engine
    user, project, conv = await _write_conversation(db_session, "wt13@rvaiglobal.com")
    manager = SessionManager()
    client = _NoFreeLunchSandbox(already_running=True)
    model, _ = _scripted([[_WROTE_A_FILE, _DECLARED_DONE]])
    # Without this the test is vacuous: a container that was ALREADY serving needs no start, so
    # every assertion below would hold on a build that never calls `dev_start` at all.

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

    assert client.dev_starts, "the attach must have actually exercised the already-running arm"
    assert state.end_reason != "sandbox_unavailable", "the 409 arm must not end the turn"
    assert len(_preview_ready_frames(state)) <= 1, "the frame is claimed once, not per emitter"


async def test_a_dev_start_blip_is_swallowed_and_selfheal_still_rescues(
    _fresh_engine, db_session, session_factory, fake_redis: aioredis.Redis, fake_storage
) -> None:
    """★ R6 — every new step fails open. `dev_start` at attach is an OPTIMIZATION; a supervisor
    blip there must cost the preview a few seconds, never the turn. `verify`'s dead-child
    rescue is the backstop, and it still runs."""
    engine = _fresh_engine
    user, project, conv = await _write_conversation(db_session, "wt14@rvaiglobal.com")
    manager = SessionManager()
    model, counts = _scripted([[_WROTE_A_FILE, _DECLARED_DONE]])
    # A raw call COUNT cannot tell the two worlds apart — `_try_try_again` retries a failed
    # rescue, so verify alone also produces two calls. WHEN each one fired is the discriminator:
    # the attach start happens before request one, the rescue only after the model has run.
    requests_when_started: list[int] = []
    client = _NoFreeLunchSandbox(
        fail_starts=1, on_start=lambda: requests_when_started.append(counts["requests"])
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

    assert state.end_reason != "sandbox_unavailable", "a start blip must not fail the turn"
    assert requests_when_started[0] == 0, "the swallowed start is the one at attach"
    assert len(requests_when_started) == 2, "verify's dead-child rescue is still the backstop"
    assert requests_when_started[1] > 0, "…and it ran after the model, not instead of the attach"
    assert client.serving, "and it got the server up"


# --- the conversation guard's release ----------------------------------------


async def test_a_cancel_during_the_sandbox_terminal_still_frees_the_conversation(
    _fresh_engine, db_session, session_factory, fake_redis: aioredis.Redis, fake_storage
) -> None:
    """★ THE GUARD RELEASE. `_run_turn`'s `finally` runs `finish_turn_sandbox` under
    `asyncio.shield`, and the surrounding `suppress(Exception)` does NOT catch
    `CancelledError` — it is a `BaseException`. So a cancel delivered while that call is in
    flight propagates straight out of the block.

    Flat, that skipped `release_conversation` altogether, and the guard never expires on its
    own (`guard.py`) — so every later turn in the conversation answered 409 for the rest of the
    process's life. The release lives in its own nested `finally` for exactly this.

    Mutation-check: un-nest the release in `_run_turn`'s `finally` and this goes red.
    """
    engine = _fresh_engine
    user, project, conv = await _write_conversation(db_session, "wtguard@rvaiglobal.com")
    client = FakeSandboxClient()
    in_terminal = asyncio.Event()
    never = asyncio.Event()

    class HangingTerminal(SessionManager):
        async def finish_turn_sandbox(self, session, sandbox_client, *, touched: bool) -> None:
            in_terminal.set()
            await never.wait()  # the cancel lands about here

    manager = HangingTerminal()
    model, _ = _scripted([[_WROTE_A_FILE, _DECLARED_DONE]])

    async def _noop() -> None:
        return None

    await engine.start_turn(
        conversation=conv,
        user_id=user.id,
        prompt="add a status column",
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
    assert conv.id in _mid_reply, "the turn holds the claim while it runs"

    await asyncio.wait_for(in_terminal.wait(), timeout=5)
    state.task.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await state.task

    # The claim is gone even though the terminal never returned. Without this the user's next
    # message in this conversation is refused forever, with nothing to clear it but a restart.
    assert conv.id not in _mid_reply

    # Let the SHIELDED terminal finish. `asyncio.shield` deliberately does not cancel what it
    # wraps, so the cancel above leaves `finish_turn_sandbox` still parked on `never` — an
    # orphan task that outlives the test and gets torn down with the loop. Releasing it keeps
    # this test from leaving pending-task noise on whatever runs next.
    never.set()
    await asyncio.sleep(0)


# --- R17/R18: the compile signal's channel to the portal --------------------------------------
#
# The consumer half lives in the container; what is pinned here is the CHANNEL — that the state
# reaches a subscribed client as a turn frame, on change only, and that a signal we cannot read
# never travels as good news. Exercised directly against the watcher's per-poll step rather than
# through a whole timed turn: the loop's own 1s cadence is not the contract, the frames are.


def _compile_frames(state: _TurnState) -> list[object]:
    return [f for f in state.ring if getattr(f, "type", None) == "compile"]


def _compile_state_of(frame: object) -> object:
    return getattr(frame, "state", None)


def _a_turn_state() -> _TurnState:
    return _TurnState(
        turn_id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        user_id=uuid.uuid4(),
        mode=ConversationMode.WRITE,
    )


def _a_sandbox(client: FakeSandboxClient) -> SandboxSession:
    return SandboxSession(
        sandbox_client=client,
        handle=SandboxHandle(
            fqdn="app-xyz.example",
            token="tok",
            app_name="sbx-abc",
            preview_url="https://app-xyz.example/",
            ready=True,
        ),
        app_id=uuid.uuid4(),
    )


async def test_the_compile_state_reaches_a_subscriber_as_a_turn_frame(
    _fresh_engine,
) -> None:
    engine, state = _fresh_engine, _a_turn_state()
    client = FakeSandboxClient()
    client.compile_report = CompileReport(state=CompileState.FAILED, errors=("boom",))

    await engine._poll_compile_state(state, _a_sandbox(client))

    assert [_compile_state_of(f) for f in _compile_frames(state)] == [CompileState.FAILED]


async def test_the_frame_is_emitted_on_change_not_on_every_poll(_fresh_engine) -> None:
    """The ring is sized for narrative and the watcher polls once a second for the whole build.
    A frame per poll would be several hundred per turn and would evict the story it carries —
    the compile state is a LEVEL, not an event."""
    engine, state = _fresh_engine, _a_turn_state()
    client = FakeSandboxClient()
    sandbox = _a_sandbox(client)

    client.compile_report = CompileReport(state=CompileState.BUILDING)
    await engine._poll_compile_state(state, sandbox)
    await engine._poll_compile_state(state, sandbox)
    await engine._poll_compile_state(state, sandbox)
    client.compile_report = CompileReport(state=CompileState.CLEAN)
    await engine._poll_compile_state(state, sandbox)

    assert client.compile_polls == 4, "guard the premise: every poll really did ask"
    assert [_compile_state_of(f) for f in _compile_frames(state)] == [
        CompileState.BUILDING,
        CompileState.CLEAN,
    ]


async def test_an_unknown_reading_travels_as_unknown_and_never_as_clean(_fresh_engine) -> None:
    """The fleet case. A container whose image predates `/dev/compile` answers 404 on every
    poll, which the client maps to `unknown`. That must reach the pane as `unknown` — the value
    it HOLDS its cover on — rather than being dropped, which the pane would read as nothing
    having changed since the last `clean`."""
    engine, state = _fresh_engine, _a_turn_state()
    client = FakeSandboxClient()
    sandbox = _a_sandbox(client)

    client.compile_report = CompileReport(state=CompileState.CLEAN)
    await engine._poll_compile_state(state, sandbox)
    client.compile_report = CompileReport(state=CompileState.UNKNOWN, reason="endpoint_absent")
    await engine._poll_compile_state(state, sandbox)

    assert [_compile_state_of(f) for f in _compile_frames(state)] == [
        CompileState.CLEAN,
        CompileState.UNKNOWN,
    ]


async def test_the_protocol_drift_alarm_fires_once_per_connect_not_once_per_poll(
    _fresh_engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ The canary. Defensive parsing is what keeps a bundler upgrade from crashing the
    consumer, and it is exactly what would make an upstream rename SILENT — so the one signal
    that says the protocol moved has to be loud, and has to be loud once. Keyed on the connect
    generation because a drifted container polls forever: an alarm per poll is an alarm nobody
    reads by the second minute."""
    engine, state = _fresh_engine, _a_turn_state()
    client = FakeSandboxClient()
    sandbox = _a_sandbox(client)
    raised: list[tuple[str, dict[str, object]]] = []
    monkeypatch.setattr(
        engine_module._log,
        "warning",
        lambda event, **kw: raised.append((event, kw)),
    )

    drifted = CompileReport(
        state=CompileState.UNKNOWN, reason="no_recognised_frame", connect_generation=7
    )
    client.compile_report = drifted
    await engine._poll_compile_state(state, sandbox)
    await engine._poll_compile_state(state, sandbox)
    await engine._poll_compile_state(state, sandbox)

    assert [e for e, _ in raised] == [HMR_PROTOCOL_DRIFT_EVENT]
    assert raised[0][1]["connect_generation"] == 7
    assert raised[0][1]["app_name"] == "sbx-abc"

    # A RECONNECT is a new fact about the protocol, so it earns a second alarm.
    client.compile_report = CompileReport(
        state=CompileState.UNKNOWN, reason="no_recognised_frame", connect_generation=8
    )
    await engine._poll_compile_state(state, sandbox)
    assert [e for e, _ in raised] == [HMR_PROTOCOL_DRIFT_EVENT, HMR_PROTOCOL_DRIFT_EVENT]


async def test_a_socket_that_is_merely_down_is_not_reported_as_protocol_drift(
    _fresh_engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`unknown` has several causes and only one of them means the vocabulary moved. Alarming
    on all of them would make the canary fire on every container restart and be muted."""
    engine, state = _fresh_engine, _a_turn_state()
    client = FakeSandboxClient()
    raised: list[str] = []
    monkeypatch.setattr(engine_module._log, "warning", lambda event, **kw: raised.append(event))

    for reason in ("endpoint_absent", "disconnected", "transport_error", "connected_no_frame_yet"):
        client.compile_report = CompileReport(state=CompileState.UNKNOWN, reason=reason)
        await engine._poll_compile_state(state, _a_sandbox(client))

    assert raised == []


async def test_a_compile_poll_never_takes_the_preview_watcher_down(_fresh_engine) -> None:
    """The watcher that carries this also owns crash detection. `compile_state` is specified
    never to raise, and this is the assertion that the emit path does not reintroduce one —
    trading a covered preview for an undetected dead dev server would be a bad trade."""
    engine, state = _fresh_engine, _a_turn_state()
    client = FakeSandboxClient()
    client.compile_report = CompileReport(state=CompileState.UNKNOWN, reason="malformed_body")

    await engine._poll_compile_state(state, _a_sandbox(client))

    assert state.compile_state is CompileState.UNKNOWN
