"""The pass that holds the pool of ready sandboxes at its size, and the worker task that runs it.

Ledger rows live on the real test database, so every compare-and-set is Postgres's own. Azure is
the same control plane the claim tests use, keyed by container name, and each pass is run at a
fixed instant wherever India time decides the size.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import redis.asyncio as aioredis
import sqlalchemy as sa
from pydantic import SecretStr
from structlog.testing import capture_logs
from taskiq.cli.scheduler.run import is_cron_task_now

import src.db.base as db_base
from src.config import settings
from src.core.alarms import SANDBOX_POOL_BELOW_SIZE_EVENT
from src.db.models.pending_teardown import PendingTeardown
from src.db.models.sandbox_pool import SandboxPoolMember, SandboxPoolState
from src.db.models.user import User
from src.services.build_sessions import pool_pass
from src.services.build_sessions.pool_pass import ROW_DEADLINE, PoolPass, keep_the_pool
from src.services.redis import registry_key
from src.services.redis.keys import REGISTRY_FIELD_APP_NAME
from src.services.sandbox import pool, reset_sandbox_for_tests, set_sandbox_for_tests
from src.services.sandbox.base import a_fresh_sandbox_name
from src.services.sandbox.client import AcaSandboxClient, SandboxNotConfiguredError
from src.services.sandbox.config import SandboxConfig
from src.workers.sandbox_pool import (
    SANDBOX_POOL_CRON,
    SANDBOX_POOL_SCHEDULE_ID,
    keep_the_pool_at_its_size,
)
from tests.factories import UserFactory
from tests.fakes import FakeSandboxClient
from tests.services.sandbox.test_pool import IMAGE, OLD_IMAGE, PoolAca, Supervisors

pytestmark = pytest.mark.usefixtures("empty_sandbox_pool")

READY = SandboxPoolState.READY
FILLING = SandboxPoolState.FILLING
CLAIMED = SandboxPoolState.CLAIMED
RETIRING = SandboxPoolState.RETIRING

#: 09:00 India time on a Monday.
MONDAY_MORNING = datetime(2026, 10, 5, 3, 30, tzinfo=UTC)


def _config(*, day: int, night: int) -> SandboxConfig:
    """Daytime 09:00-19:00 India time, Monday to Friday."""
    return SandboxConfig.model_validate(
        {
            "subscription_id": "s",
            "resource_group": "r",
            "region": "centralindia",
            "managed_environment_name": "aca-env",
            "acr_server": "acr.azurecr.io",
            "acr_username": "acr-user",
            "acr_password": SecretStr("acr-pass"),
            "image_ref": IMAGE,
            "pool_day_size": day,
            "pool_night_size": night,
        }
    )


@pytest.fixture
async def keeper() -> AsyncIterator[SimpleNamespace]:
    aca = PoolAca()
    client = AcaSandboxClient(
        _config(day=0, night=0), transport=httpx.MockTransport(Supervisors()), aca=aca
    )
    yield SimpleNamespace(aca=aca, client=client)
    await client.aclose()


@pytest.fixture
def configured(keeper: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> Any:
    """Make `keeper` this process's sandbox client, under the given sizes, as the worker task
    and the backend's startup find it."""

    def configure(config: SandboxConfig) -> None:
        keeper.client._config = config
        monkeypatch.setattr(settings, "sandbox", config)

    set_sandbox_for_tests(keeper.client)
    yield configure
    reset_sandbox_for_tests()


@pytest.fixture
async def owe() -> AsyncIterator[Callable[[str], Awaitable[None]]]:
    """Owe a teardown of a container, committed as a door commits one."""
    owners: list[uuid.UUID] = []

    async def owe_a_teardown(app_name: str) -> None:
        async with db_base.async_session_factory() as db:
            user = await UserFactory.create(
                db, email=f"pool-owed-{uuid.uuid4().hex[:8]}@rvaiglobal.com"
            )
            db.add(
                PendingTeardown(
                    user_id=user.id,
                    app_id=uuid.uuid4(),
                    app_name=app_name,
                    write_back=True,
                    project_id=uuid.uuid4(),
                    instance_ref=datetime.now(UTC),
                    claimed_until=datetime.now(UTC) + timedelta(minutes=5),
                )
            )
            await db.commit()
            owners.append(user.id)

    yield owe_a_teardown
    async with db_base.async_session_factory() as db:
        await db.execute(sa.delete(User).where(User.id.in_(owners)))
        await db.commit()


async def _row(
    keeper: SimpleNamespace,
    state: SandboxPoolState,
    *,
    image_ref: str = IMAGE,
    since: datetime | None = None,
) -> str:
    """A container Azure holds for the pool, and its ledger row in `state`. Returns its name."""
    name = a_fresh_sandbox_name()
    keeper.aca.made_for_the_pool(name, token="pool-bearer")
    async with db_base.async_session_factory() as db:
        db.add(
            SandboxPoolMember(
                name=name,
                fqdn=None if state is FILLING else f"{name}.pool.example",
                image_ref=image_ref,
                state=state,
                state_changed_at=since or datetime.now(UTC),
            )
        )
        await db.commit()
    return name


async def _a_pass(keeper: SimpleNamespace, config: SandboxConfig, *, at: datetime) -> PoolPass:
    """One pass at `at`, under `config` as the client's own."""
    keeper.client._config = config
    return await keep_the_pool(keeper.client, at=at)


async def _ledger() -> dict[str, tuple[SandboxPoolState, str]]:
    async with db_base.async_session_factory() as db:
        rows = await db.execute(
            sa.select(SandboxPoolMember.name, SandboxPoolMember.state, SandboxPoolMember.image_ref)
        )
    return {name: (state, image_ref) for name, state, image_ref in rows}


async def _states() -> dict[str, SandboxPoolState]:
    return {name: state for name, (state, _) in (await _ledger()).items()}


async def _its_create_finished(name: str) -> None:
    """What another process's fill does once Azure answers."""
    async with db_base.async_session_factory() as db:
        member_id = await db.scalar(
            sa.select(SandboxPoolMember.id).where(SandboxPoolMember.name == name)
        )
    assert member_id is not None
    assert await pool.mark_ready(member_id, f"{name}.pool.example")


async def _until(condition: Callable[[], Awaitable[bool]]) -> None:
    async with asyncio.timeout(5):
        while not await condition():
            await asyncio.sleep(0.01)


def _events(logged: Sequence[Mapping[str, Any]], event: str) -> list[Mapping[str, Any]]:
    return [entry for entry in logged if entry["event"] == event]


# --- the size by India time ------------------------------------------------------------------


async def test_at_nine_on_a_monday_morning_the_pool_fills_to_the_day_size(keeper) -> None:
    config = _config(day=5, night=1)
    since = MONDAY_MORNING - timedelta(hours=1)
    ready = {await _row(keeper, READY, since=since) for _ in range(2)}

    outcome = await _a_pass(keeper, config, at=MONDAY_MORNING)

    assert (outcome.target, outcome.filled, outcome.retired) == (5, 3, 0)
    assert len(keeper.aca.filled) == 3
    assert await _states() == {name: READY for name in ready | set(keeper.aca.filled)}


async def test_a_minute_before_nine_the_night_size_still_holds(keeper) -> None:
    config = _config(day=5, night=1)
    since = MONDAY_MORNING - timedelta(hours=1)
    older = await _row(keeper, READY, since=since)
    newer = await _row(keeper, READY, since=since + timedelta(minutes=5))

    outcome = await _a_pass(keeper, config, at=MONDAY_MORNING - timedelta(minutes=1))

    assert (outcome.target, outcome.filled, outcome.retired) == (1, 0, 1)
    assert keeper.aca.deleted == [older]
    assert await _states() == {newer: READY}


async def test_at_seven_in_the_evening_the_pool_shrinks_and_no_claimed_container_is_touched(
    keeper,
) -> None:
    """Five ready and three claimed at the end of the day: four ready ones go, and the three
    people working in claimed ones keep them."""
    config = _config(day=5, night=1)
    evening = datetime(2026, 10, 5, 13, 30, tzinfo=UTC)
    since = evening - timedelta(hours=1)
    ready = [await _row(keeper, READY, since=since + timedelta(minutes=i)) for i in range(5)]
    claimed = {await _row(keeper, CLAIMED, since=evening) for _ in range(3)}

    outcome = await _a_pass(keeper, config, at=evening)

    assert (outcome.target, outcome.retired, outcome.deleted, outcome.filled) == (1, 4, 4, 0)
    assert keeper.aca.deleted == ready[:4]
    assert await _states() == {ready[4]: READY} | dict.fromkeys(claimed, CLAIMED)


@pytest.mark.parametrize(
    "at",
    [
        pytest.param(datetime(2026, 10, 4, 23, 0, tzinfo=UTC), id="monday-0430-india-time"),
        pytest.param(datetime(2026, 10, 3, 6, 30, tzinfo=UTC), id="saturday-noon-india-time"),
    ],
)
async def test_the_night_size_holds_before_the_day_starts_and_at_the_weekend(
    keeper, at: datetime
) -> None:
    outcome = await _a_pass(keeper, _config(day=5, night=1), at=at)

    assert (outcome.target, outcome.filled) == (1, 1)


# --- the image swap --------------------------------------------------------------------------


async def test_an_image_change_is_swapped_in_new_before_old_without_dipping_below_the_size(
    keeper,
) -> None:
    """Two replacements are already filling elsewhere as the change lands. An old container
    goes only once a new one is ready to take its place, so the ready count never drops below
    the size, and when the swap ends none runs the old image."""
    config = _config(day=5, night=5)
    old = {await _row(keeper, READY, image_ref=OLD_IMAGE) for _ in range(5)}
    elsewhere = [await _row(keeper, FILLING) for _ in range(2)]
    ready_counts: list[int] = []

    async def the_ready_count() -> None:
        ready_counts.append(await pool.ready_count())

    keeper.aca.on_each_call = the_ready_count

    first = await _a_pass(keeper, config, at=datetime.now(UTC))
    await _its_create_finished(elsewhere[0])
    second = await _a_pass(keeper, config, at=datetime.now(UTC))
    await _its_create_finished(elsewhere[1])
    third = await _a_pass(keeper, config, at=datetime.now(UTC))

    assert [(p.filled, p.retired) for p in (first, second, third)] == [(3, 0), (0, 4), (0, 1)]
    assert len(ready_counts) == 8
    assert min(ready_counts) >= 5
    assert set(keeper.aca.deleted) == old
    ledger = await _ledger()
    assert set(ledger.values()) == {(READY, IMAGE)}
    assert len(ledger) == 5


# --- rows left behind ------------------------------------------------------------------------


async def test_a_create_left_filling_past_its_deadline_is_deleted_and_nothing_is_filled(
    keeper,
) -> None:
    config = _config(day=5, night=5)
    at = datetime.now(UTC)
    stuck = await _row(keeper, FILLING, since=at - ROW_DEADLINE - timedelta(minutes=1))

    with capture_logs() as logged:
        outcome = await _a_pass(keeper, config, at=at)

    assert (outcome.overdue, outcome.deleted, outcome.filled) == (True, 1, 0)
    assert keeper.aca.deleted == [stuck]
    assert keeper.aca.create_attempts == []
    assert await _ledger() == {}
    [alarm] = _events(logged, SANDBOX_POOL_BELOW_SIZE_EVENT)
    assert (alarm["target"], alarm["ready"], alarm["overdue"]) == (5, 0, True)


async def test_a_claim_inside_its_deadline_survives_and_one_past_it_nothing_names_is_deleted(
    keeper, fake_redis: aioredis.Redis
) -> None:
    """A claim inside its deadline may still be writing the registry. One past it never did, so
    no project work was ever put in its container."""
    at = datetime.now(UTC)
    in_flight = await _row(keeper, CLAIMED, since=at - timedelta(minutes=1))
    abandoned = await _row(keeper, CLAIMED, since=at - ROW_DEADLINE - timedelta(minutes=1))

    outcome = await _a_pass(keeper, _config(day=0, night=0), at=at)

    assert outcome.deleted == 1
    assert keeper.aca.deleted == [abandoned]
    assert await _states() == {in_flight: CLAIMED}


async def test_a_starts_create_left_claimed_past_its_deadline_holds_back_no_fill(
    keeper, fake_redis: aioredis.Redis
) -> None:
    """A start's own create is held claimed until its record is written. One left past its
    deadline is cleared like any claim nothing names, and the pool fills as usual: a stuck start
    is no sign that Azure is refusing the pool's creates.

    Mutation check: count a claimed row past its deadline toward stopping the fills and none is
    made, and the alarm fires."""
    at = datetime.now(UTC)
    stuck = await _row(keeper, CLAIMED, since=at - ROW_DEADLINE - timedelta(minutes=1))

    with capture_logs() as logged:
        outcome = await _a_pass(keeper, _config(day=2, night=2), at=at)

    assert (outcome.deleted, outcome.filled, outcome.overdue) == (1, 2, False)
    assert keeper.aca.deleted == [stuck]
    assert _events(logged, SANDBOX_POOL_BELOW_SIZE_EVENT) == []


@pytest.mark.parametrize("held_by", ["registry", "owed_teardown"])
async def test_a_claim_past_its_deadline_that_somebody_still_holds_keeps_its_container(
    keeper, fake_redis: aioredis.Redis, owe, held_by: str
) -> None:
    """A claim whose registry write landed and whose row could not be deleted: the container is
    somebody's workspace, or has their work being saved out of it. Only the row goes."""
    at = datetime.now(UTC)
    stuck = await _row(keeper, CLAIMED, since=at - ROW_DEADLINE - timedelta(minutes=1))
    if held_by == "registry":
        await fake_redis.hset(registry_key(uuid.uuid4()), mapping={REGISTRY_FIELD_APP_NAME: stuck})
    else:
        await owe(stuck)

    outcome = await _a_pass(keeper, _config(day=0, night=0), at=at)

    assert outcome.deleted == 0
    assert keeper.aca.deleted == []
    assert await _ledger() == {}


async def test_a_delete_azure_refuses_is_left_retiring_and_retried_by_the_next_pass(
    keeper,
) -> None:
    config = _config(day=0, night=0)
    member = await _row(keeper, READY)
    keeper.aca.refuses_to_delete.add(member)

    first = await _a_pass(keeper, config, at=datetime.now(UTC))
    assert (first.retired, first.deleted) == (1, 0)
    assert await _states() == {member: RETIRING}

    keeper.aca.refuses_to_delete.clear()
    second = await _a_pass(keeper, config, at=datetime.now(UTC))

    assert second.deleted == 1
    assert keeper.aca.deleted == [member, member]
    assert await _ledger() == {}


async def test_a_claim_landing_between_the_passes_look_and_its_retire_keeps_its_container(
    keeper, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The pass judges from a reading of the ledger, and a start may claim the very row it is
    about to retire; the retire's compare-and-set is what stops it."""
    since = datetime.now(UTC) - timedelta(hours=1)
    rows = [await _row(keeper, READY, since=since + timedelta(minutes=i)) for i in range(6)]
    real_reading = pool.the_ledger

    async def a_start_claims_right_after_the_reading() -> list[SandboxPoolMember]:
        reading = await real_reading()
        claimed = await pool.claim(IMAGE)
        assert claimed is not None and claimed.name == rows[0]
        return reading

    monkeypatch.setattr(pool, "the_ledger", a_start_claims_right_after_the_reading)

    outcome = await _a_pass(keeper, _config(day=5, night=5), at=datetime.now(UTC))

    assert outcome.retired == 0
    assert keeper.aca.deleted == []
    assert (await _states())[rows[0]] is CLAIMED


# --- Azure refusing --------------------------------------------------------------------------


async def test_while_azure_refuses_creates_each_pass_asks_once_and_raises_the_alarm(
    keeper,
) -> None:
    config = _config(day=5, night=5)
    keeper.aca.refuses_to_create = True
    start = datetime.now(UTC)

    for minute in range(3):
        asked_before = len(keeper.aca.create_attempts)
        with capture_logs() as logged:
            outcome = await _a_pass(keeper, config, at=start + timedelta(minutes=minute))
        assert len(keeper.aca.create_attempts) - asked_before == 1
        assert (outcome.filled, outcome.refused) == (0, True)
        [alarm] = _events(logged, SANDBOX_POOL_BELOW_SIZE_EVENT)
        assert (alarm["target"], alarm["ready"], alarm["refused"]) == (5, 0, True)

    keeper.aca.refuses_to_create = False
    with capture_logs() as logged:
        recovered = await _a_pass(keeper, config, at=start + timedelta(minutes=3))

    assert (recovered.filled, recovered.ready) == (5, 5)
    assert _events(logged, SANDBOX_POOL_BELOW_SIZE_EVENT) == []


async def test_the_alarm_never_fires_while_the_size_is_zero(keeper) -> None:
    at = datetime.now(UTC)
    await _row(keeper, FILLING, since=at - ROW_DEADLINE - timedelta(minutes=1))

    with capture_logs() as logged:
        outcome = await _a_pass(keeper, _config(day=0, night=0), at=at)

    assert (outcome.overdue, outcome.deleted) == (True, 1)
    assert _events(logged, SANDBOX_POOL_BELOW_SIZE_EVENT) == []


# --- one pass at a time ----------------------------------------------------------------------


async def test_two_passes_at_once_run_one_between_them(keeper, configured) -> None:
    """The old and the new worker's schedulers both tick during a deploy."""
    configured(_config(day=2, night=2))
    keeper.aca.fills_wait_for = asyncio.Event()

    with capture_logs() as logged:
        passes = [asyncio.create_task(keep_the_pool_at_its_size()) for _ in range(2)]
        done, _ = await asyncio.wait(passes, timeout=5, return_when=asyncio.FIRST_COMPLETED)
        assert len(done) == 1, "both passes are running at once"
        keeper.aca.fills_wait_for.set()
        await asyncio.wait_for(asyncio.gather(*passes), timeout=5)

    assert len(keeper.aca.filled) == 2
    assert len(_events(logged, pool_pass.POOL_PASS_LOCKED_OUT_EVENT)) == 1
    assert len(_events(logged, pool_pass.POOL_PASS_EVENT)) == 1


async def test_a_backend_starting_on_a_new_image_swaps_the_pool_without_waiting_for_it(
    keeper, configured
) -> None:
    """Startup goes on while the first replacement is still being made, and the worker's pass in
    that same minute stands down rather than starting a second swap."""
    from src.main import create_app, lifespan

    configured(_config(day=5, night=5))
    old = {await _row(keeper, READY, image_ref=OLD_IMAGE) for _ in range(5)}
    keeper.aca.fills_wait_for = asyncio.Event()

    async def swapped() -> bool:
        return list((await _ledger()).values()).count((READY, IMAGE)) == 5

    with capture_logs() as logged:
        async with lifespan(create_app()):
            await asyncio.wait_for(keeper.aca.fill_began.wait(), timeout=5)
            assert keeper.aca.filling_now == 1, "startup waited for the swap"
            await asyncio.wait_for(keep_the_pool_at_its_size(), timeout=5)
            assert len(keeper.aca.filled) == 1
            keeper.aca.fills_wait_for.set()
            await _until(swapped)

    assert len(keeper.aca.filled) == 5
    assert len(_events(logged, pool_pass.POOL_PASS_LOCKED_OUT_EVENT)) == 1
    assert {name for name, (state, _) in (await _ledger()).items() if state is READY} >= old


# --- the worker task -------------------------------------------------------------------------


async def test_each_tick_logs_one_line_even_at_a_size_of_zero(keeper, configured) -> None:
    """A silent minute is how an operator tells the pass is not running."""
    configured(_config(day=0, night=0))

    with capture_logs() as logged:
        await keep_the_pool_at_its_size()

    [line] = [entry for entry in logged if entry["event"].startswith("sandbox_pool_pass")]
    assert line["event"] == pool_pass.POOL_PASS_EVENT
    assert (line["target"], line["ready"], line["filled"]) == (0, 0, 0)


async def test_a_worker_with_no_sandbox_says_so_and_does_nothing() -> None:
    assert settings.sandbox is None

    with capture_logs() as logged:
        await keep_the_pool_at_its_size()

    [line] = [entry for entry in logged if entry["event"].startswith("sandbox_pool_pass")]
    assert (line["event"], line["reason"]) == ("sandbox_pool_pass_disabled", "unconfigured")


async def test_a_pass_with_a_sandbox_client_that_cannot_hold_a_pool_refuses(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The pass creates and deletes through the Azure client's own bound, which no other client
    has, so one configured with any other refuses rather than running without it.

    Mutation check: drop the client's type check and the pass fails some other way."""
    monkeypatch.setattr(settings, "sandbox", _config(day=1, night=1))
    set_sandbox_for_tests(FakeSandboxClient())
    try:
        with pytest.raises(SandboxNotConfiguredError):
            await pool_pass.run_pool_pass()
    finally:
        reset_sandbox_for_tests()


def test_the_pass_is_scheduled_every_minute_under_a_pinned_id() -> None:
    """A schedule without an id gets a fresh one per process start, useless to correlate."""
    assert keep_the_pool_at_its_size.labels["schedule"] == [
        {"cron": SANDBOX_POOL_CRON, "schedule_id": SANDBOX_POOL_SCHEDULE_ID}
    ]
    minute = datetime(2026, 10, 5, 3, 30, tzinfo=UTC)
    assert all(
        is_cron_task_now(SANDBOX_POOL_CRON, minute + timedelta(minutes=ahead))
        for ahead in range(3)
    )
