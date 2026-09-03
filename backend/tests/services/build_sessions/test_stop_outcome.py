"""U9 — the stop, as three named states and as an ask plus a status read.

THE DEFECT THESE EXIST FOR. `stop_active_work` returned `True` on both of its branches
unconditionally, and `stop_user_turn_and_wait` did the same, while all three docstrings promised
that a timeout would be reported as *still running*. Five committed tests asserted that hardcoded
success and every one of them exercised the happy path, so the lie was invisible: a stop that had
not finished handed the caller the same answer as one that had, and the caller's very next act is
to take the container.

Two rules run through the whole file, both bought expensively:

* **"Gone" and "slow" must be provably different before anything is reclaimed.** A readiness
  timeout once condemned a live container and a restore destroyed a citizen's unsaved work
  (`docs/solutions/logic-errors/readiness-timeout-triggers-destructive-sandbox-restore-2026-08-02`).
  So `STOPPED` here is never a deduction from elapsed time — it is a positive observation that
  nothing holds the app, read from the map `release_project_sandbox` itself refuses on.
* **The completion barrier sits above every assertion that depends on it.** Assertions appended
  over time land at the bottom of a block, which is often ABOVE the wait they need
  (`docs/solutions/best-practices/e2e-harness-measure-after-the-barrier-and-refuse-vacuous-passes-2026-08-02`).
  Each test below waits on the stop's own task — or on a bounded poll of the real condition —
  before it asks whether the stop worked.

And the shape being tested is a one-container-two-projects hand-over, which is the scope split
`docs/solutions/architecture-patterns/one-scope-became-two-2026-09-01` warns about: the stop is
now ASKED FOR by one request and REPORTED BY another, so the thing that starts it is no longer the
thing that watches it. That is why the manager keeps its own record of having asked — nothing else
could tell a later poll "stopped" from "nothing was running".
"""

from __future__ import annotations

import asyncio
import base64
import uuid

import pytest
import redis.asyncio as aioredis
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession
from structlog.testing import capture_logs

from src.config import settings
from src.db.models.user import User
from src.services.build_sessions.manager import (
    BuildSessionConflictError,
    SessionManager,
    StopOutcome,
)
from src.services.build_sessions.snapshot import (
    SNAPSHOT_EXEC_TIMEOUT_SECONDS,
    SNAPSHOT_EXECS,
)
from src.services.sandbox import ExecResult
from src.services.sandbox.config import SandboxConfig
from src.services.storage import snapshot_key
from tests.factories import ProjectFactory, UserFactory
from tests.fakes import FakeBrain, FakeSandboxClient, FakeStorage


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


def _bundles_to(client: FakeSandboxClient, sha: str) -> FakeSandboxClient:
    """Script a container that is AT `sha` and hands back a PARSEABLE bundle.

    Needed because a stopped build still runs its snapshot step, and "no work was lost" is
    asserted against what that step actually stored. The default fake answers every exec with
    empty stdout, which would make a save that stored nothing indistinguishable from one that
    worked — the exact ambiguity these tests exist to remove."""
    bundle = base64.b64encode(b"# v2 git bundle\n" + sha.encode() + b" HEAD\n\nPACK").decode()

    def handler(cmd: list[str]) -> ExecResult:
        if cmd[0] == "sh" and "rev-parse" in cmd[-1]:
            return ExecResult(stdout=f"{sha}\n@@@@3", stderr="", exit=0)
        return ExecResult(stdout=bundle, stderr="", exit=0)

    client.exec_handler = handler
    return client


class _HoldsItsOwnUnwind(FakeBrain):
    """A build that parks INSIDE its cancellation handler until the test lets it go.

    This is the state every honest-stop test needs and the one no committed test had:
    cancellation is a request, not an event, so between `task.cancel()` and the workspace
    actually being free there is a real window in which the turn is *still running*. A brain that
    dies the instant it is cancelled closes that window and every assertion about it passes
    vacuously.

    `stepped` says the build is genuinely under way; `unwinding` says the cancel has been
    delivered and the cleanup has begun; `let_go` is the test's hand on the tap."""

    def __init__(self) -> None:
        super().__init__()
        self.stepped = asyncio.Event()
        self.unwinding = asyncio.Event()
        self.let_go = asyncio.Event()

    async def __call__(self, session_id, user_id, sandbox_client, on_progress):
        self.stepped.set()
        try:
            await asyncio.Event().wait()  # never set: only a cancel ends this
        except asyncio.CancelledError:
            self.unwinding.set()
            await self.let_go.wait()
            raise
        raise RuntimeError("halted by the test")  # unreachable: only a cancel leaves the try


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
    """★ THE RULE THE NUMBER IS SUPPOSED TO OBEY, checked against the parts rather than trusted.

    A budget BELOW the unwind's own bounds reports a healthy stop as one that did not finish —
    which is what the retired 30 s did, and what the first attempt at deriving it did again for
    the branch it was written to fix: it counted the build's 10 s record and missed the snapshot
    `_do_finalize` writes first, an unbounded call whose parts are four execs of two minutes.

    COMPUTED FROM THE PRIMITIVES, not from the intermediate the module derives, so it is a check
    and not a restatement: if the per-exec bound or the number of execs moves, this recomputes
    the branch's real cost and the budget has to keep up.

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
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """The headline contract: it flips to `STOPPED` when the work stops, and not one poll before.

    The brain is held inside its own cancellation handler, so between the ask and the release the
    turn is genuinely mid-cleanup — the state that used to be reported as success."""
    user, project_a = await _mk(db_session, "stop1@rvaiglobal.com")
    manager = SessionManager()
    client = _bundles_to(FakeSandboxClient(), "1" * 40)
    brain = _HoldsItsOwnUnwind()

    await manager.start(
        db_session, user, project_a, "build it", run_build=brain, sandbox_client=client
    )
    await brain.stepped.wait()

    asked = await manager.request_stop_of_active_work(
        db_session, user, project_a, sandbox_client=client
    )
    assert asked is StopOutcome.STILL_RUNNING
    await asyncio.wait_for(brain.unwinding.wait(), timeout=5)

    # Mid-unwind, and it says so — repeatedly, because the browser polls this.
    for _ in range(5):
        state = await manager.stop_state_of_active_work(db_session, user, project_a)
        assert state is StopOutcome.STILL_RUNNING

    # THE BARRIER. Nothing below this line runs until the stop itself says it is finished.
    brain.let_go.set()
    record = manager._stop_records[(user.id, project_a)]
    assert await asyncio.wait_for(record.task, timeout=10) is StopOutcome.STOPPED

    assert await manager.stop_state_of_active_work(db_session, user, project_a) is (
        StopOutcome.STOPPED
    )
    # ...and "stopped" means what the next step needs it to mean: the slot is actually free.
    assert manager.active_session_for(user.id) is None


async def test_the_status_read_never_says_stopped_while_the_turn_is_still_unwinding(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """*However long it takes* — including after the stop's OWN budget has expired.

    The sharpest version of the rule the readiness-timeout P0 taught: a wait running out says
    nothing about the container. Here the stop task is given a budget far shorter than the unwind,
    so it gives up and settles while the turn is still inside its cleanup. Every poll after that
    must still refuse, because the fact that decides it is the session map, not the clock."""
    user, project_a = await _mk(db_session, "stop2@rvaiglobal.com")
    manager = SessionManager()
    client = _bundles_to(FakeSandboxClient(), "2" * 40)
    brain = _HoldsItsOwnUnwind()

    await manager.start(
        db_session, user, project_a, "build it", run_build=brain, sandbox_client=client
    )
    await brain.stepped.wait()

    await manager.request_stop_of_active_work(
        db_session, user, project_a, sandbox_client=client, timeout_s=0.05
    )
    record = manager._stop_records[(user.id, project_a)]
    # The stop gave up WAITING — it did not stop stopping, and it did not report success.
    assert await asyncio.wait_for(record.task, timeout=10) is StopOutcome.STILL_RUNNING
    assert brain.unwinding.is_set()

    # The task that was asked to watch this is finished, and the answer is still the honest one.
    for _ in range(10):
        assert await manager.stop_state_of_active_work(db_session, user, project_a) is (
            StopOutcome.STILL_RUNNING
        )

    brain.let_go.set()
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
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """*The regression.* This is the exact call that used to answer `True` after its wait expired.

    Asserted on `stop_active_work` itself rather than through the router, because that is where
    the hardcoded success lived — one `return True` per branch, below a docstring promising the
    opposite. A caller acting on the old answer releases a container out from under a task that is
    still inside its `finally`."""
    user, project_a = await _mk(db_session, "stop4@rvaiglobal.com")
    manager = SessionManager()
    client = _bundles_to(FakeSandboxClient(), "3" * 40)
    brain = _HoldsItsOwnUnwind()

    await manager.start(
        db_session, user, project_a, "build it", run_build=brain, sandbox_client=client
    )
    await brain.stepped.wait()

    timed_out = await manager.stop_active_work(
        db_session, user, project_a, sandbox_client=client, timeout_s=0.05
    )

    # `STOPPED` is what the old code returned here, on both branches, unconditionally.
    assert timed_out is StopOutcome.STILL_RUNNING
    assert brain.unwinding.is_set()  # ...and the turn really was mid-cleanup, not merely slow
    assert manager.active_session_for(user.id) is not None
    # The container is NOT taken while that is the answer, which is what makes it worth reporting.
    with pytest.raises(BuildSessionConflictError):
        await manager.release_project_sandbox(db_session, user, project_a, sandbox_client=client)

    brain.let_go.set()
    await _the_slot_is_free(manager, user.id)


async def test_a_stop_longer_than_a_request_still_completes_and_is_reported_correctly(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """Nothing is held open for the length of a stop, which is what makes the budget affordable.

    The proof is structural rather than a stopwatch: the ask returns while its own stop task is
    still pending and the workspace is still held. So no request's lifetime bounds the stop, and
    the gateway's request timeout — a number owned by the client's network and recorded nowhere in
    this repo — stops constraining the design."""
    user, project_a = await _mk(db_session, "stop5@rvaiglobal.com")
    manager = SessionManager()
    client = _bundles_to(FakeSandboxClient(), "4" * 40)
    brain = _HoldsItsOwnUnwind()

    await manager.start(
        db_session, user, project_a, "build it", run_build=brain, sandbox_client=client
    )
    await brain.stepped.wait()

    asked = await manager.request_stop_of_active_work(
        db_session, user, project_a, sandbox_client=client
    )
    record = manager._stop_records[(user.id, project_a)]

    assert asked is StopOutcome.STILL_RUNNING
    assert not record.task.done()  # the ask returned FIRST — nothing waited for the stop
    assert manager.active_session_for(user.id) is not None

    brain.let_go.set()
    assert await asyncio.wait_for(record.task, timeout=10) is StopOutcome.STOPPED
    assert await manager.stop_state_of_active_work(db_session, user, project_a) is (
        StopOutcome.STOPPED
    )


async def test_a_dropped_connection_mid_stop_loses_no_work_and_takes_no_container(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """The citizen's tab dies mid-hand-over. Nothing about the stop was theirs to lose.

    Two halves. WHILE it is unwinding, the container is untouched and the release still refuses,
    so a dropped connection cannot leave a workspace half-taken. AFTER it settles, asking again
    picks the answer up exactly where it was — which is only possible because the stop is a
    detached task and the ask was recorded, not because anything guessed from elapsed time."""
    user, project_a = await _mk(db_session, "stop6@rvaiglobal.com")
    manager = SessionManager()
    client = _bundles_to(FakeSandboxClient(), "5" * 40)
    brain = _HoldsItsOwnUnwind()

    session = await manager.start(
        db_session, user, project_a, "build it", run_build=brain, sandbox_client=client
    )
    await brain.stepped.wait()
    await manager.request_stop_of_active_work(db_session, user, project_a, sandbox_client=client)
    record = manager._stop_records[(user.id, project_a)]
    await asyncio.wait_for(brain.unwinding.wait(), timeout=5)

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
    brain.let_go.set()
    assert await asyncio.wait_for(record.task, timeout=10) is StopOutcome.STOPPED
    assert await manager.stop_state_of_active_work(db_session, user, project_a) is (
        StopOutcome.STOPPED
    )
    assert snapshot_key(session.app_id) in fake_storage.objects


async def test_two_racing_transfers_for_one_citizen_end_with_one_container(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """Two tabs, one workspace, one stop.

    The hazard is the scope split: whoever asks is no longer whoever watches, so two asks could
    easily become two stops — a second `task.cancel()` landing inside a cleanup already under way,
    which is how a terminal frame gets eaten, and two teardowns of one container. One record per
    project is what prevents it, and both callers are told the same true thing."""
    user, project_a = await _mk(db_session, "stop7@rvaiglobal.com")
    manager = SessionManager()
    client = _bundles_to(FakeSandboxClient(), "6" * 40)
    brain = _HoldsItsOwnUnwind()

    await manager.start(
        db_session, user, project_a, "build it", run_build=brain, sandbox_client=client
    )
    await brain.stepped.wait()

    both = await asyncio.gather(
        manager.request_stop_of_active_work(db_session, user, project_a, sandbox_client=client),
        manager.request_stop_of_active_work(db_session, user, project_a, sandbox_client=client),
    )

    assert list(both) == [StopOutcome.STILL_RUNNING, StopOutcome.STILL_RUNNING]
    assert len(manager._stop_records) == 1  # one stop, not one per tab
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

    brain.let_go.set()
    assert await asyncio.wait_for(record.task, timeout=10) is StopOutcome.STOPPED

    # ONE container destroyed, and only the one that was provisioned.
    assert len(client.provisioned) == 1
    assert client.torn_down == client.provisioned
    assert await manager.stop_state_of_active_work(db_session, user, project_a) is (
        StopOutcome.STOPPED
    )


async def test_a_stop_that_breaks_is_logged_against_the_citizen_it_broke_for(
    db_session: AsyncSession,
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A detached stop that raises names WHO it failed for, not just that something failed.

    The callback exists because an un-retrieved task exception surfaces only as a warning at
    collection time, attached to nothing anyone is looking at. A line with no keys is barely
    better: nothing in this service binds structlog contextvars, so a detached task inherits no
    request scope, and an operator watching "stop of active work failed" repeat cannot tell
    which citizen is stuck or which container is still holding a workspace.

    Mutation check: drop the identifiers from the `_log.error` call and the key assertions go red
    while the message assertion stays green."""
    user, project_a = await _mk(db_session, "stop8@rvaiglobal.com")
    manager = SessionManager()
    client = _bundles_to(FakeSandboxClient(), "7" * 40)
    brain = _HoldsItsOwnUnwind()

    await manager.start(
        db_session, user, project_a, "build it", run_build=brain, sandbox_client=client
    )
    await brain.stepped.wait()
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
    brain.let_go.set()
    retry = manager._stop_records[(user.id, project_a)]
    assert await asyncio.wait_for(retry.task, timeout=10) is StopOutcome.STOPPED


# --- the two predicates, held apart --------------------------------------------------


async def test_the_write_only_flag_is_still_derived_from_the_toolset_alone(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """A DIRECT pin on `building`'s predicate, so widening it goes red here rather than in a
    citizen's face.

    `_writing_session_holds` is narrow ON PURPOSE and the manager records why: every mode pins the
    container, so the broad predicate is true throughout an ordinary Ask or Plan turn — and using
    it here put a hammer icon and two Stop buttons in front of someone who had asked a question,
    and short-circuited the escape hatch that lets a pristine container be reclaimed without a
    dialog about nothing.

    The hand-over's wider fact is a SEPARATE field for exactly that reason, so the temptation to
    widen this one is gone. These four assertions are what says the two predicates are genuinely
    different questions and not a copy waiting to be deduplicated."""
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
