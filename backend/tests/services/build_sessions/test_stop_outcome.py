"""The stop, as three named states and as an ask plus a status read.

Two rules run through the whole file:
* "Gone" and "slow" must be provably different before anything is reclaimed: `STOPPED` is a
  positive observation that nothing holds the app, never a deduction from elapsed time.
* The completion barrier sits above every assertion that depends on it — each test waits on
  the stop's own task, or a bounded poll of the real condition, before checking the outcome.

The shape under test is a one-container-two-projects hand-over: the stop is ASKED FOR by one
request and REPORTED BY another, so the manager keeps its own record of having asked — nothing
else could tell a later poll "stopped" from "nothing was running".

WHAT IS BEING STOPPED, since `SessionManager.start` was deleted: `_stop_the_held_session` used to
branch on two kinds of live work, a build session's `run_build` task and a Write turn's workspace;
only the Write branch remains, so every test here drives a real turn on a real `TurnEngine`
(`_HoldsItsOwnUnwind` + the `_fresh_engine` fixture) instead of a fake brain the manager could
cancel directly. The subject is unchanged — the three states, the ask/answer split, one stop per
project — reached the one way production now reaches it.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid

import pytest
import redis.asyncio as aioredis
from pydantic import SecretStr
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models.function import AgentInfo, FunctionModel
from sqlalchemy.ext.asyncio import AsyncSession
from structlog.testing import capture_logs

from src.config import settings
from src.db.models.conversation import ChatKind
from src.db.models.user import User
from src.services.agent.mode_prompts import PromptContext
from src.services.build_sessions import manager as manager_module
from src.services.build_sessions.locks import read_registry
from src.services.build_sessions.manager import (
    BuildSessionConflictError,
    SessionManager,
    StopOutcome,
)
from src.services.build_sessions.snapshot import (
    SNAPSHOT_EXEC_TIMEOUT_SECONDS,
    SNAPSHOT_EXECS,
)
from src.services.sandbox.config import SandboxConfig
from src.services.turns.engine import TurnEngine, set_turn_engine_for_tests
from src.services.turns.guard import _mid_reply
from tests.factories import ConversationFactory, ProjectFactory, UserFactory
from tests.fakes import FakeSandboxClient, FakeStorage

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


async def _mk(db: AsyncSession, email: str) -> tuple[User, uuid.UUID]:
    user = await UserFactory.create(db, email=email)
    project = await ProjectFactory.create(db, user.id)
    return user, project.id


@pytest.fixture(autouse=True)
def _fresh_engine():
    """THE ENGINE THE STOP ACTUALLY REACHES. `_stop_the_held_session` calls
    `get_turn_engine().stop_user_turn_and_wait(...)`, so a test that does not register its own
    engine would stop a global one no turn here is running in and read `NOTHING_WAS_RUNNING`
    off a live session."""
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


class _HoldsItsOwnUnwind:
    """A turn that parks INSIDE its cancellation handler until the test lets it go.

    This is the state every honest-stop test needs and the one no committed test had:
    cancellation is a request, not an event, so between `task.cancel()` and the workspace
    actually being free there is a real window in which the turn is *still running*. A model that
    dies the instant it is cancelled closes that window and every assertion about it passes
    vacuously.

    IT IS A REAL TURN ON THE REAL ENGINE, which is the only kind of work left to stop: the build
    session that used to hold this slot (a `run_build` task the manager cancelled itself) went
    with `SessionManager.start`, and `_stop_the_held_session` has one arm now — ask the turn
    engine to settle the user's turn, then look at whether anything still holds the app. So the
    hold has to live where production's does, inside the streaming model.

    `stepped` says the turn is genuinely under way; `unwinding` says the cancel has been
    delivered and the cleanup has begun; `let_go` is the test's hand on the tap."""

    def __init__(self) -> None:
        self.stepped = asyncio.Event()
        self.unwinding = asyncio.Event()
        self.let_go = asyncio.Event()

    def model(self) -> FunctionModel:
        async def _stall(_messages: list[ModelMessage], _info: AgentInfo):
            yield "working on it"
            self.stepped.set()
            try:
                await asyncio.Event().wait()  # never set: only a cancel ends this
            except asyncio.CancelledError:
                self.unwinding.set()
                await self.let_go.wait()
                raise
            yield "unreachable"  # only a cancel leaves the try

        return FunctionModel(stream_function=_stall)


async def _a_turn_holding_the_workspace(
    db: AsyncSession,
    engine: TurnEngine,
    session_factory,
    manager: SessionManager,
    client: FakeSandboxClient,
    user: User,
    project_id: uuid.UUID,
    turn: _HoldsItsOwnUnwind,
) -> None:
    """Start a real Write turn on `project_id` and return once it is genuinely streaming.

    The turn pins the project's container through `manager.ensure_sandbox`, so the manager's
    one-per-user slot is held by work the stop can actually reach — which is what every
    assertion below about `STILL_RUNNING` / `STOPPED` is a statement about."""
    conversation = await ConversationFactory.create(
        db, user.id, project_id=project_id, kind=ChatKind.BUILD
    )
    await engine.start_turn(
        conversation=conversation,
        user_id=user.id,
        prompt="build it",
        history=[],
        prompt_context=_CTX,
        app_id=None,
        project_id=project_id,
        model=turn.model(),
        session_factory=session_factory,
        persist_user_turn=_nothing_to_persist,
        manager=manager,
        sandbox_client=client,
    )
    await asyncio.wait_for(turn.stepped.wait(), timeout=10)


async def _nothing_to_persist() -> None:
    """The user's row is the route's job, not the engine's — and no test here reads it."""
    return None


async def _the_slot_is_free(manager: SessionManager, user_id: uuid.UUID) -> None:
    """THE COMPLETION BARRIER for the paths whose stop task has already given up waiting.

    A bounded poll of the real condition — the user losing the one build slot — rather than a
    sleep, because a fixed sleep can only ever be too short (and then reports an absence it never
    waited long enough to observe) or too slow. Raises if the condition never arrives, so a stop
    that genuinely wedges is a finding rather than a hang."""
    for _ in range(2000):
        if user_id not in manager._active_by_user:
            return
        await asyncio.sleep(0.001)
    raise AssertionError("the stop never settled within the barrier's budget")


# --- the budget, derived rather than chosen -------------------------------------------


def test_the_stop_budget_sits_above_the_unwind_each_branch_actually_runs() -> None:
    """The budget is COMPUTED FROM THE PRIMITIVES here, not the module's derived intermediate,
    so it is a check and not a restatement: if the per-exec bound or exec count moves, this
    recomputes the branch's real cost and the budget has to keep up. A budget below either
    branch's real bound reports a healthy stop as one that did not finish.

    ONE OF THE TWO BRANCHES IS NO LONGER REACHED FROM THIS BUDGET, and it is kept anyway. The
    build arm of `_stop_the_held_session` went with `SessionManager.start`, so a stop no longer
    runs `_do_finalize` — but `_do_finalize` still costs exactly this much on the `stop` /
    `force_end` path, and `_STOP_ACTIVE_WORK_TIMEOUT_SECONDS`'s own derivation in `manager.py`
    still names it as the larger of the two it is set from. Dropping the assertion would let the
    constant fall under the number its comment says it clears, silently. The WRITE assertion is
    the live one; the build assertion holds the constant to its own stated derivation.

    Mutation check: make the budget the sum of the recovery autosave and the record again and the
    build-branch assertion goes red while the write-branch one stays green."""
    build_branch = (
        SNAPSHOT_EXECS * SNAPSHOT_EXEC_TIMEOUT_SECONDS
        + manager_module._OUTCOME_WRITE_TIMEOUT_SECONDS
    )
    write_branch = (
        manager_module._RECOVERY_SNAPSHOT_TIMEOUT_SECONDS
        + manager_module._OUTCOME_WRITE_TIMEOUT_SECONDS
    )
    assert build_branch > 0 and write_branch > 0  # liveness: both parts are real numbers
    assert manager_module._STOP_ACTIVE_WORK_TIMEOUT_SECONDS >= build_branch
    assert manager_module._STOP_ACTIVE_WORK_TIMEOUT_SECONDS >= write_branch


# --- the status read -----------------------------------------------------------------


async def test_the_status_read_says_still_running_until_the_work_has_really_unwound(
    db_session: AsyncSession,
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
    _fresh_engine: TurnEngine,
    session_factory,
) -> None:
    """The headline contract: it flips to `STOPPED` when the work stops, and not one poll before.

    The turn is held inside its own cancellation handler, so between the ask and the release the
    turn is genuinely mid-cleanup — the state that used to be reported as success."""
    user, project_a = await _mk(db_session, "stop1@rvaiglobal.com")
    manager = SessionManager()
    client = FakeSandboxClient()
    turn = _HoldsItsOwnUnwind()

    await _a_turn_holding_the_workspace(
        db_session, _fresh_engine, session_factory, manager, client, user, project_a, turn
    )

    asked = await manager.request_stop_of_active_work(
        db_session, user, project_a, sandbox_client=client
    )
    assert asked is StopOutcome.STILL_RUNNING
    await asyncio.wait_for(turn.unwinding.wait(), timeout=5)

    # Mid-unwind, and it says so — repeatedly, because the browser polls this.
    for _ in range(5):
        state = await manager.stop_state_of_active_work(db_session, user, project_a)
        assert state is StopOutcome.STILL_RUNNING

    # THE BARRIER. Nothing below this line runs until the stop itself says it is finished.
    turn.let_go.set()
    record = manager._stop_records[(user.id, project_a)]
    assert await asyncio.wait_for(record.task, timeout=10) is StopOutcome.STOPPED

    assert await manager.stop_state_of_active_work(db_session, user, project_a) is (
        StopOutcome.STOPPED
    )
    # ...and "stopped" means what the next step needs it to mean: the slot is actually free.
    assert manager.active_session_for(user.id) is None


async def test_the_status_read_never_says_stopped_while_the_turn_is_still_unwinding(
    db_session: AsyncSession,
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
    _fresh_engine: TurnEngine,
    session_factory,
) -> None:
    """*However long it takes* — including after the stop's OWN budget has expired.

    The sharpest version of the rule the readiness-timeout taught: a wait running out says
    nothing about the container. Here the stop task is given a budget far shorter than the unwind,
    so it gives up and settles while the turn is still inside its cleanup. Every poll after that
    must still refuse, because the fact that decides it is the session map, not the clock."""
    user, project_a = await _mk(db_session, "stop2@rvaiglobal.com")
    manager = SessionManager()
    client = FakeSandboxClient()
    turn = _HoldsItsOwnUnwind()

    await _a_turn_holding_the_workspace(
        db_session, _fresh_engine, session_factory, manager, client, user, project_a, turn
    )

    await manager.request_stop_of_active_work(
        db_session, user, project_a, sandbox_client=client, timeout_s=0.05
    )
    record = manager._stop_records[(user.id, project_a)]
    # The stop gave up WAITING — it did not stop stopping, and it did not report success.
    assert await asyncio.wait_for(record.task, timeout=10) is StopOutcome.STILL_RUNNING
    assert turn.unwinding.is_set()

    # The task that was asked to watch this is finished, and the answer is still the honest one.
    for _ in range(10):
        assert await manager.stop_state_of_active_work(db_session, user, project_a) is (
            StopOutcome.STILL_RUNNING
        )

    turn.let_go.set()
    await _the_slot_is_free(manager, user.id)
    assert await manager.stop_state_of_active_work(db_session, user, project_a) is (
        StopOutcome.STOPPED
    )


async def test_a_status_read_for_a_project_nobody_asked_about_is_nothing_was_running(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """`STOPPED` needs BOTH halves: nothing holding the app, AND a stop that was asked for.

    Without the second, a client that never started a hand-over would read "stopped" off a project
    that had simply never been touched, and believe it had performed a transfer it never began."""
    user, project_a = await _mk(db_session, "stop3@rvaiglobal.com")
    manager = SessionManager()

    assert await manager.stop_state_of_active_work(db_session, user, project_a) is (
        StopOutcome.NOTHING_WAS_RUNNING
    )


# --- the timeout, which is the whole point -------------------------------------------


async def test_a_stop_that_times_out_is_reported_as_still_running_never_as_success(
    db_session: AsyncSession,
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
    _fresh_engine: TurnEngine,
    session_factory,
) -> None:
    """*The regression*: `stop_active_work` used to answer `True` after its wait expired.

    Asserted directly on `stop_active_work` rather than through the router, since that is where
    the hardcoded success lived. A caller acting on the old answer releases a container out from
    under a task that is still inside its `finally`."""
    user, project_a = await _mk(db_session, "stop4@rvaiglobal.com")
    manager = SessionManager()
    client = FakeSandboxClient()
    turn = _HoldsItsOwnUnwind()

    await _a_turn_holding_the_workspace(
        db_session, _fresh_engine, session_factory, manager, client, user, project_a, turn
    )

    timed_out = await manager.stop_active_work(
        db_session, user, project_a, sandbox_client=client, timeout_s=0.05
    )

    assert timed_out is StopOutcome.STILL_RUNNING
    assert turn.unwinding.is_set()  # ...and the turn really was mid-cleanup, not merely slow
    assert manager.active_session_for(user.id) is not None
    # The container is NOT taken while that is the answer, which is what makes it worth reporting.
    with pytest.raises(BuildSessionConflictError):
        await manager.release_project_sandbox(db_session, user, project_a, sandbox_client=client)

    turn.let_go.set()
    await _the_slot_is_free(manager, user.id)


async def test_a_stop_longer_than_a_request_still_completes_and_is_reported_correctly(
    db_session: AsyncSession,
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
    _fresh_engine: TurnEngine,
    session_factory,
) -> None:
    """Nothing is held open for the length of a stop, which is what makes the budget affordable.

    The proof is structural rather than a stopwatch: the ask returns while its own stop task is
    still pending and the workspace is still held. So no request's lifetime bounds the stop, and
    the gateway's request timeout — a number owned by the client's network and recorded nowhere in
    this repo — stops constraining the design."""
    user, project_a = await _mk(db_session, "stop5@rvaiglobal.com")
    manager = SessionManager()
    client = FakeSandboxClient()
    turn = _HoldsItsOwnUnwind()

    await _a_turn_holding_the_workspace(
        db_session, _fresh_engine, session_factory, manager, client, user, project_a, turn
    )

    asked = await manager.request_stop_of_active_work(
        db_session, user, project_a, sandbox_client=client
    )
    record = manager._stop_records[(user.id, project_a)]

    assert asked is StopOutcome.STILL_RUNNING
    assert not record.task.done()  # the ask returned FIRST — nothing waited for the stop
    assert manager.active_session_for(user.id) is not None

    turn.let_go.set()
    assert await asyncio.wait_for(record.task, timeout=10) is StopOutcome.STOPPED
    assert await manager.stop_state_of_active_work(db_session, user, project_a) is (
        StopOutcome.STOPPED
    )


async def test_a_dropped_connection_mid_stop_loses_no_work_and_takes_no_container(
    db_session: AsyncSession,
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
    _fresh_engine: TurnEngine,
    session_factory,
) -> None:
    """The citizen's tab dies mid-hand-over. Nothing about the stop was theirs to lose.

    Two halves. WHILE it is unwinding, the container is untouched and the release still refuses,
    so a dropped connection cannot leave a workspace half-taken. AFTER it settles, asking again
    picks the answer up exactly where it was — which is only possible because the stop is a
    detached task and the ask was recorded, not because anything guessed from elapsed time.

    WHAT "LOST NOTHING" MEANS ON THIS PATH. A stopped BUILD ran `_do_finalize`, whose step 1
    pushed the saved snapshot, and this ended by finding it in storage. A stopped TURN takes the
    other ending: `finish_turn_sandbox` PARDONS the container rather than tearing it down,
    deliberately — a Write turn's container is the preview the citizen is looking at, and the
    turn ending is not a reason for their app to vanish. So the tree that held their work is
    still running behind a lease, which is asserted here instead: nothing torn down and the
    registry — the sweep's only map to it — still there. Weaker in no direction that matters: on
    the build path the container went and the bundle was all that survived; here the container
    itself survives."""
    user, project_a = await _mk(db_session, "stop6@rvaiglobal.com")
    manager = SessionManager()
    client = FakeSandboxClient()
    turn = _HoldsItsOwnUnwind()

    await _a_turn_holding_the_workspace(
        db_session, _fresh_engine, session_factory, manager, client, user, project_a, turn
    )
    await manager.request_stop_of_active_work(db_session, user, project_a, sandbox_client=client)
    record = manager._stop_records[(user.id, project_a)]
    await asyncio.wait_for(turn.unwinding.wait(), timeout=5)

    # THE DROP: the caller that was polling goes away mid-read.
    poller = asyncio.create_task(manager.stop_state_of_active_work(db_session, user, project_a))
    poller.cancel()
    with pytest.raises(asyncio.CancelledError):
        await poller

    # The stop is untouched by that, and so is the workspace.
    assert not record.task.done()
    assert client.torn_down == []
    with pytest.raises(BuildSessionConflictError):
        await manager.release_project_sandbox(db_session, user, project_a, sandbox_client=client)

    # THE BARRIER, then the resume: a fresh read answers, and nothing was lost on the way.
    turn.let_go.set()
    assert await asyncio.wait_for(record.task, timeout=10) is StopOutcome.STOPPED
    assert await manager.stop_state_of_active_work(db_session, user, project_a) is (
        StopOutcome.STOPPED
    )
    assert client.torn_down == []  # pardoned, not destroyed — the workspace outlived the stop
    assert await read_registry(fake_redis, user.id) is not None  # ...and is still findable


async def test_two_racing_transfers_for_one_citizen_end_with_one_container(
    db_session: AsyncSession,
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
    monkeypatch: pytest.MonkeyPatch,
    _fresh_engine: TurnEngine,
    session_factory,
) -> None:
    """Two tabs, one workspace, one stop: two asks could race into two cancels, so one record per
    project must catch both. The barrier sits inside `existing_app_id`, after its real DB round
    trip (see the inline comments there for why that is the race-safe spot); counts are of
    `_stop_the_held_session` CALLS, not `len(_stop_records)`, which reads `1` either way.

    Mutation check: put `await asyncio.sleep(0)` between reading `in_flight` and storing the
    record in `request_stop_of_active_work` and the second tab starts a second stop —
    `stops_started` grows to two while every other assertion here stays green.
    (Verified by injecting it and watching this test — and only this test — go red.)"""
    user, project_a = await _mk(db_session, "stop7@rvaiglobal.com")
    manager = SessionManager()
    client = FakeSandboxClient()
    turn = _HoldsItsOwnUnwind()

    await _a_turn_holding_the_workspace(
        db_session, _fresh_engine, session_factory, manager, client, user, project_a, turn
    )

    real_app_id = manager_module.existing_app_id
    # Two parties, and only the first two asks are held: the sequential third ask further down
    # must not wait for a partner that is never coming.
    at_the_check = asyncio.Barrier(2)
    parked: list[int] = []

    async def _holds_both_tabs_at_the_check(db, user_id, project_id):
        app_id = await real_app_id(db, user_id, project_id)
        # The read is DONE and the connection is free, so the other tab can take it, finish its
        # own read, and arrive here too. Nothing suspends between this return and the record
        # write, which is exactly why both tabs being here is a real race and not a staged one.
        if len(parked) < 2:
            parked.append(1)
            # BOUNDED, so a change that lets either ask return before it reaches the barrier
            # FAILS this test instead of hanging the suite on a wait nothing can release.
            await asyncio.wait_for(at_the_check.wait(), timeout=10)
        return app_id

    monkeypatch.setattr(manager_module, "existing_app_id", _holds_both_tabs_at_the_check)

    stops_started: list[uuid.UUID] = []
    real_stop = manager._stop_the_held_session

    async def _counting_stop(user_id, app_id, **kwargs):
        stops_started.append(app_id)
        return await real_stop(user_id, app_id, **kwargs)

    monkeypatch.setattr(manager, "_stop_the_held_session", _counting_stop)

    both = await asyncio.gather(
        manager.request_stop_of_active_work(db_session, user, project_a, sandbox_client=client),
        manager.request_stop_of_active_work(db_session, user, project_a, sandbox_client=client),
    )

    assert list(both) == [StopOutcome.STILL_RUNNING, StopOutcome.STILL_RUNNING]
    assert len(manager._stop_records) == 1  # one KEY…
    record = manager._stop_records[(user.id, project_a)]

    # A THIRD ask, sequentially, JOINS the same stop rather than starting another — the identity
    # check is the assertion, because a second stop would be indistinguishable from this one by
    # count alone (it would simply replace the record) while firing a second cancel into a
    # cleanup already under way.
    assert (
        await manager.request_stop_of_active_work(
            db_session, user, project_a, sandbox_client=client
        )
        is StopOutcome.STILL_RUNNING
    )
    assert manager._stop_records[(user.id, project_a)].task is record.task

    # THE BARRIER FIRST, THEN THE COUNT — the rule this whole file runs on. A second stop task
    # would be parked inside the same unwind, and asking about it before letting the turn go
    # would leave this test hanging on its own failure instead of reporting it.
    turn.let_go.set()
    assert await asyncio.wait_for(record.task, timeout=10) is StopOutcome.STOPPED

    # …and only ONE stop was ever started behind that key.
    assert stops_started == [record.app_id]

    # ONE container, and it is still standing. Two stops would have been two endings against
    # the same workspace; one ending means one provision and one pardon. `torn_down == []` is
    # the pardon (`finish_turn_sandbox` never destroys a turn's container — see the drop test),
    # so a second stop firing into the finished cleanup is what this pair would catch.
    assert len(client.provisioned) == 1
    assert client.torn_down == []
    assert await manager.stop_state_of_active_work(db_session, user, project_a) is (
        StopOutcome.STOPPED
    )


async def test_a_stop_that_breaks_is_logged_against_the_citizen_it_broke_for(
    db_session: AsyncSession,
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
    monkeypatch: pytest.MonkeyPatch,
    _fresh_engine: TurnEngine,
    session_factory,
) -> None:
    """A detached stop that raises names WHO it failed for, not just that something failed.

    An un-retrieved task exception surfaces only as a warning at collection time, and nothing here
    binds structlog contextvars, so a detached task inherits no request scope and an operator
    watching "stop of active work failed" repeat cannot tell which citizen or container it is.

    Mutation check: drop the identifiers from the `_log.error` call and the key assertions go
    red while the message assertion stays green."""
    user, project_a = await _mk(db_session, "stop8@rvaiglobal.com")
    manager = SessionManager()
    client = FakeSandboxClient()
    turn = _HoldsItsOwnUnwind()

    await _a_turn_holding_the_workspace(
        db_session, _fresh_engine, session_factory, manager, client, user, project_a, turn
    )
    session = manager.active_session_for(user.id)
    assert session is not None
    app_id = session.app_id

    real_stop = manager._stop_the_held_session

    async def _breaks(_user_id, _app_id, **_kwargs):
        raise RuntimeError("the stop itself broke")

    monkeypatch.setattr(manager, "_stop_the_held_session", _breaks)
    with capture_logs() as logs:
        assert (
            await manager.request_stop_of_active_work(
                db_session, user, project_a, sandbox_client=client
            )
            is StopOutcome.STILL_RUNNING
        )
        record = manager._stop_records[(user.id, project_a)]
        with pytest.raises(RuntimeError):
            await asyncio.wait_for(record.task, timeout=5)
        await asyncio.sleep(0)  # the done-callback runs on the next loop pass

    failures = [line for line in logs if line["event"] == "stop of active work failed"]
    assert len(failures) == 1
    assert failures[0]["user_id"] == str(user.id)
    assert failures[0]["project_id"] == str(project_a)
    assert failures[0]["app_id"] == str(app_id)

    # AND THE MANAGER IS NOT WEDGED BY IT: a broken stop leaves the record settled rather than
    # in flight, so the next ask starts a real one and the container is still given up.
    monkeypatch.setattr(manager, "_stop_the_held_session", real_stop)
    assert (
        await manager.request_stop_of_active_work(
            db_session, user, project_a, sandbox_client=client
        )
        is StopOutcome.STILL_RUNNING
    )
    turn.let_go.set()
    retry = manager._stop_records[(user.id, project_a)]
    assert await asyncio.wait_for(retry.task, timeout=10) is StopOutcome.STOPPED


# --- the two predicates, held apart --------------------------------------------------


async def test_the_write_only_flag_is_still_derived_from_the_toolset_alone(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """A DIRECT pin on `building`'s predicate, so widening it goes red here rather than in a
    citizen's face.

    `_writing_session_holds` is narrow ON PURPOSE: the broader "is anything live" predicate is
    true throughout an ordinary Ask or Plan turn too, so widening this one would put Stop
    controls in front of someone who only asked a question. The hand-over's wider fact stays a
    SEPARATE field for exactly that reason — these four assertions pin the two predicates as
    genuinely different questions, not a copy waiting to be deduplicated."""
    user, project_a = await _mk(db_session, "stop8@rvaiglobal.com")
    user_b, project_b = await _mk(db_session, "stop9@rvaiglobal.com")
    manager = SessionManager()
    client = FakeSandboxClient()

    asking = await manager.ensure_sandbox(
        db_session, user, project_a, sandbox_client=client, may_write=False
    )
    assert manager._writing_session_holds(user.id, asking.app_id) is False
    assert manager._live_session_holds(user.id, asking.app_id) is True

    writing = await manager.ensure_sandbox(
        db_session, user_b, project_b, sandbox_client=client, may_write=True
    )
    assert manager._writing_session_holds(user_b.id, writing.app_id) is True
    assert manager._live_session_holds(user_b.id, writing.app_id) is True
