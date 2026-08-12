"""U12 / R10 — the wall-clock liveness lease the turn engine publishes (C5 family 4).

WHAT THIS FILE IS FOR. Until this lease existed, a build that had been running for more than
90 seconds was indistinguishable from an abandoned container to anything except the process
running it: the heartbeat is seeded once per turn against a 90 s TTL, and the only other shield
is `sweep_all`'s IN-PROCESS `live_users` set, which is empty everywhere else. So these tests
care about one property above all the others — that the signal is legible to a reader holding
nothing in common with the turn but the store. `test_a_sweep_sharing_only_the_store_*` is that
assertion, and it is the unit's verification criterion.

The rest pin the five properties `azure-is-the-fleet-of-record-tiered-sandbox-reclamation`
(successor to the archived `stay-of-execution-lease-owns-the-container-2026-07-30`)
extracted the hard way from the preview lease: fail closed on absent AND absurd, log loudly when
the write does not land, disown when the record it belonged to goes, grant while the lock is
held then release, and make sure something scheduled actually READS it.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator, Iterator

import fakeredis
import fakeredis.aioredis
import pytest
import redis.asyncio as aioredis
from redis.exceptions import RedisError
from structlog.testing import capture_logs

import src.services.turns.engine as engine_mod
from src.api.v1.build_sessions.schemas import (
    LIVENESS_LEASE_RENEW_CADENCE_SECONDS,
    LIVENESS_LEASE_TTL_SECONDS,
)
from src.db.models.conversation import ConversationMode
from src.services.build_sessions import locks, reaper
from src.services.orchestrator.deps import SandboxSession
from src.services.redis import (
    REGISTRY_STATE_READY,
    heartbeat_key,
    lease_key,
    lock_key,
    registry_key,
)
from src.services.redis.keys import (
    REGISTRY_FIELD_APP_NAME,
    REGISTRY_FIELD_CREATED_AT,
    REGISTRY_FIELD_FQDN,
    REGISTRY_FIELD_STATE,
    REGISTRY_FIELD_TOKEN_REF,
)
from src.services.sandbox import SandboxHandle
from src.services.turns.engine import TurnEngine, _TurnState
from tests.fakes import FakeSandboxClient, a_sandbox_name

USER = uuid.uuid4()
OTHER = uuid.uuid4()


async def _register(
    redis: aioredis.Redis, user: uuid.UUID, app_name: str = a_sandbox_name("x")
) -> None:
    """The registry hash a live sandbox has. The lease is disowned without one, so every
    test that expects a renewal to LAND has to seed this first."""
    await redis.hset(
        registry_key(user),
        mapping={
            REGISTRY_FIELD_APP_NAME: app_name,
            REGISTRY_FIELD_FQDN: f"{app_name}.example",
            REGISTRY_FIELD_TOKEN_REF: "ref-123",
            REGISTRY_FIELD_CREATED_AT: "2026-08-11T00:00:00+00:00",
            REGISTRY_FIELD_STATE: REGISTRY_STATE_READY,
        },
    )


async def _stored_deadline(redis: aioredis.Redis, user: uuid.UUID) -> float:
    """The deadline as it actually sits in the store — read back through Redis rather than
    returned by the writer, because "what a reader in another process would see" is the only
    thing this family is for."""
    raw = await redis.get(lease_key(user))
    assert raw is not None
    return float(raw)


def _turn_state(user: uuid.UUID, client: FakeSandboxClient) -> _TurnState:
    """A Write turn holding a container — the only shape that publishes a lease."""
    state = _TurnState(
        turn_id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        user_id=user,
        mode=ConversationMode.WRITE,
    )
    fqdn = "sbx-x.westeurope.azurecontainerapps.io"
    state.sandbox = SandboxSession(
        sandbox_client=client,
        handle=SandboxHandle(
            fqdn=fqdn,
            token="tok-test",  # noqa: S106 - a fake, never a real bearer
            app_name=a_sandbox_name("x"),
            preview_url=f"https://{fqdn}/",
            ready=True,
        ),
        app_id=uuid.uuid4(),
    )
    return state


@pytest.fixture
def instant_cadence(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Collapse the renewal cadence so the loop can be pumped with bare event-loop turns.

    Patched on the ENGINE module rather than on the schema constant, because that is where the
    loop reads it — patching the definition site would leave the imported name untouched and the
    test would hang on a 30 s sleep."""
    monkeypatch.setattr(engine_mod, "LIVENESS_LEASE_RENEW_CADENCE_SECONDS", 0)
    yield


async def _renew_a_while(state: _TurnState, *, ticks: int = 20) -> None:
    """Run the engine's renewal task for a handful of zero-delay iterations, then stop it the
    way the turn's `finally` does."""
    engine = TurnEngine()
    state.lease_task = asyncio.create_task(engine._hold_liveness_lease(state))
    for _ in range(ticks):
        await asyncio.sleep(0)
    await engine._stop_liveness_lease(state)


# --- the primitive: TTL, wall clock, and the two fail-closed edges -----------


async def test_a_renewal_writes_a_wall_clock_deadline_under_a_mandatory_ttl(
    fake_redis: aioredis.Redis,
) -> None:
    # The TTL is the whole reason this family is safe to add: the registry hash's LACK of one
    # is the root cause of ADR-0029, so a lease with no expiry would be the same bug in a new
    # key. `ttl` returning -1 means "no expiry" and MUST never happen here.
    await _register(fake_redis, USER)
    before = time.time()
    assert await locks.renew_liveness_lease(fake_redis, USER) is True

    ttl = await fake_redis.ttl(lease_key(USER))
    assert 0 < ttl <= LIVENESS_LEASE_TTL_SECONDS

    deadline = await _stored_deadline(fake_redis, USER)
    # A wall-clock instant, comparable in ANY process — not a monotonic reading, which is
    # meaningless outside the process that took it and would fail this bound wildly.
    assert before <= deadline - LIVENESS_LEASE_TTL_SECONDS <= time.time()
    assert await locks.liveness_lease_is_held(fake_redis, USER) is True


async def test_each_renewal_pushes_the_deadline_forward(
    fake_redis: aioredis.Redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A ten-minute build renews many times over; the deadline has to MOVE each time or the
    # lease lapses under a build that is still running. Driven off a scripted clock because
    # two real renewals inside one microsecond are indistinguishable.
    await _register(fake_redis, USER)
    clock = iter([1_000.0, 1_030.0, 1_060.0])
    monkeypatch.setattr(locks, "_wall_clock_now", lambda: next(clock))

    deadlines: list[float] = []
    for _ in range(3):
        assert await locks.renew_liveness_lease(fake_redis, USER) is True
        deadlines.append(await _stored_deadline(fake_redis, USER))
    assert deadlines == sorted(deadlines) and deadlines[0] < deadlines[-1]


async def test_an_absent_lease_is_not_held(fake_redis: aioredis.Redis) -> None:
    # Fail CLOSED on absent: no lease means no protection, never "assume a build".
    assert await locks.liveness_lease_is_held(fake_redis, USER) is False


@pytest.mark.parametrize("value", ["", "   ", "soon", "not-a-number", "NaN"])
async def test_an_unreadable_lease_is_not_held(fake_redis: aioredis.Redis, value: str) -> None:
    # A value this module could not have written is evidence of nothing. Reaping is the safe
    # direction: the cost is a container the next prompt rebuilds, against an unbounded bill.
    await fake_redis.set(lease_key(USER), value, ex=LIVENESS_LEASE_TTL_SECONDS)
    assert await locks.liveness_lease_is_held(fake_redis, USER) is False


async def test_a_lapsed_deadline_is_not_held(fake_redis: aioredis.Redis) -> None:
    # Readable, well-formed, and already in the past — the ordinary end of a lease whose
    # writer stopped renewing. The key's own TTL normally removes it; the comparison is what
    # makes the answer right in the window before Redis gets around to it.
    await fake_redis.set(lease_key(USER), str(time.time() - 1), ex=LIVENESS_LEASE_TTL_SECONDS)
    assert await locks.liveness_lease_is_held(fake_redis, USER) is False


async def test_a_deadline_beyond_the_grantable_ceiling_is_not_held(
    fake_redis: aioredis.Redis,
) -> None:
    # THE ABSURD-VALUE EDGE, and it is not decoration. "Unexpired" alone would let a bad clock,
    # a hand-edited key or a future writer using milliseconds buy a container a reprieve
    # measured in millennia — the unbounded hold the TTL exists to prevent, reached through the
    # parse instead of around it. Nothing may outlive what a FRESH renewal could have granted.
    far_future = time.time() + LIVENESS_LEASE_TTL_SECONDS * 100
    await fake_redis.set(lease_key(USER), str(far_future), ex=LIVENESS_LEASE_TTL_SECONDS)
    assert await locks.liveness_lease_is_held(fake_redis, USER) is False


async def test_a_reader_whose_clock_lags_the_writer_still_reads_the_lease_as_held(
    fake_redis: aioredis.Redis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # THE SKEW EDGE, and it is the reason the ceiling carries a grace at all. This family
    # exists SO THAT a process which is not running the build can read it — which means the
    # writer's clock and the reader's clock are never the same clock. The writer stores
    # `its_now + TTL`; a reader running even fractionally behind computes a lower ceiling and,
    # without grace, calls a lease renewed moments ago "absurd" and hands a live build to the
    # reaper. Mutation-check: drop LIVENESS_LEASE_CLOCK_SKEW_GRACE_SECONDS from the comparison
    # in `liveness_lease_is_held` and this goes red while every other lease test stays green.
    await _register(fake_redis, USER)
    writer_now = 1_000_000.0
    monkeypatch.setattr(locks, "_wall_clock_now", lambda: writer_now)
    assert await locks.renew_liveness_lease(fake_redis, USER) is True

    # The sweep, one second of NTP drift behind the API process that wrote the lease.
    monkeypatch.setattr(locks, "_wall_clock_now", lambda: writer_now - 1.0)
    assert await locks.liveness_lease_is_held(fake_redis, USER) is True

    # The grace is bounded, not a blank cheque: a writer using milliseconds is still absurd.
    monkeypatch.setattr(locks, "_wall_clock_now", lambda: writer_now)
    await fake_redis.set(
        lease_key(USER),
        str(writer_now + LIVENESS_LEASE_TTL_SECONDS * 1000),
        ex=LIVENESS_LEASE_TTL_SECONDS,
    )
    assert await locks.liveness_lease_is_held(fake_redis, USER) is False


async def test_a_renewal_without_a_registry_does_not_land_and_says_so(
    fake_redis: aioredis.Redis,
) -> None:
    # DISOWN ON REGISTRY REWRITE. A lease belongs to a sandbox record; with no record there is
    # nothing for it to protect, and a lease left behind would spare whatever container the
    # next builder gets. The caller is TOLD (False + a warning) rather than left believing a
    # write landed — a turn that thinks it is protected while nothing is written is exactly
    # the failure this family exists to remove.
    with capture_logs() as logs:
        assert await locks.renew_liveness_lease(fake_redis, USER) is False
    assert await fake_redis.exists(lease_key(USER)) == 0
    assert any(entry["log_level"] == "warning" for entry in logs)


async def test_releasing_the_lease_is_idempotent(fake_redis: aioredis.Redis) -> None:
    await _register(fake_redis, USER)
    await locks.renew_liveness_lease(fake_redis, USER)
    await locks.release_liveness_lease(fake_redis, USER)
    assert await locks.liveness_lease_is_held(fake_redis, USER) is False
    await locks.release_liveness_lease(fake_redis, USER)  # a second release is a no-op


async def test_one_users_lease_never_answers_for_another(fake_redis: aioredis.Redis) -> None:
    # Single-tenant does not mean single-user: the lease is keyed by the OWNING user, and a
    # lease read that ignored the user id would spare the whole fleet on one live build.
    await _register(fake_redis, USER)
    await locks.renew_liveness_lease(fake_redis, USER)
    assert await locks.liveness_lease_is_held(fake_redis, USER) is True
    assert await locks.liveness_lease_is_held(fake_redis, OTHER) is False


# --- the turn engine's renewal task ------------------------------------------


@pytest.mark.usefixtures("instant_cadence")
async def test_the_turn_holds_the_lease_for_as_long_as_it_runs(
    fake_redis: aioredis.Redis,
) -> None:
    # The happy path a ten-minute build walks: the task keeps the lease held throughout, and
    # a reader that knows nothing about the turn sees it held.
    await _register(fake_redis, USER)
    state = _turn_state(USER, FakeSandboxClient())
    engine = TurnEngine()
    state.lease_task = asyncio.create_task(engine._hold_liveness_lease(state))
    try:
        for _ in range(10):
            await asyncio.sleep(0)
            assert await locks.liveness_lease_is_held(fake_redis, USER) is True
    finally:
        await engine._stop_liveness_lease(state)


@pytest.mark.usefixtures("instant_cadence")
async def test_stopping_the_turn_releases_the_lease_and_it_cannot_outlive_one_ttl(
    fake_redis: aioredis.Redis,
) -> None:
    # GRANT WHILE THE LOCK IS HELD, THEN RELEASE. The turn's `finally` hands the container
    # back, so the lease goes with it — no indefinite hold. And even if the process died
    # before it could, the key's own expiry bounds the hold to one TTL.
    await _register(fake_redis, USER)
    state = _turn_state(USER, FakeSandboxClient())
    engine = TurnEngine()
    state.lease_task = asyncio.create_task(engine._hold_liveness_lease(state))
    for _ in range(5):
        await asyncio.sleep(0)
    # Mid-turn the hold is already BOUNDED: even a process that dies here without ever
    # reaching its `finally` cannot pin the container for longer than this.
    assert 0 < await fake_redis.ttl(lease_key(USER)) <= LIVENESS_LEASE_TTL_SECONDS

    await engine._stop_liveness_lease(state)
    assert await locks.liveness_lease_is_held(fake_redis, USER) is False
    assert await fake_redis.exists(lease_key(USER)) == 0
    assert state.lease_task is None
    # Idempotent: the turn's `finally` is reached by five different arms.
    await engine._stop_liveness_lease(state)


@pytest.mark.usefixtures("instant_cadence")
async def test_a_turn_that_never_attached_a_container_publishes_nothing(
    fake_redis: aioredis.Redis,
) -> None:
    # A turn holding no container has nothing to vouch for. The lease is keyed by USER, so
    # renewing one anyway would let a chat turn in one conversation buy a reprieve for
    # whatever the same user's slot is actually holding somewhere else.
    #
    # Asserted DURING the loop, not after it: `_stop_liveness_lease` deletes the key, so an
    # assertion at the end passes whether or not the guard exists. (It did, before this
    # comment was written — the mutation that proves the point is removing the
    # `state.sandbox is None` return and watching this test stay green with the assertion
    # at the bottom.)
    await _register(fake_redis, USER)
    state = _TurnState(
        turn_id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        user_id=USER,
        mode=ConversationMode.ASK,
    )
    engine = TurnEngine()
    state.lease_task = asyncio.create_task(engine._hold_liveness_lease(state))
    try:
        for _ in range(10):
            await asyncio.sleep(0)
            assert await fake_redis.exists(lease_key(USER)) == 0
    finally:
        await engine._stop_liveness_lease(state)


async def test_a_turn_that_published_nothing_revokes_nothing(
    fake_redis: aioredis.Redis,
) -> None:
    # The other half of the same hazard: the stop path must not DELETE a lease it never
    # wrote. A turn whose task was never started (no container) reaching the shared `finally`
    # would otherwise strip the protection off a build running in another conversation.
    await _register(fake_redis, USER)
    await locks.renew_liveness_lease(fake_redis, USER)  # somebody else's live build
    state = _TurnState(
        turn_id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        user_id=USER,
        mode=ConversationMode.ASK,
    )
    assert state.lease_task is None
    await TurnEngine()._stop_liveness_lease(state)
    assert await locks.liveness_lease_is_held(fake_redis, USER) is True


class _RefusingRedis:
    """A store that answers every write with an error — the Redis blip, not a bug."""

    async def exists(self, *_args: object) -> int:
        raise RedisError("boom")

    async def set(self, *_args: object, **_kwargs: object) -> bool:
        raise RedisError("boom")


@pytest.mark.usefixtures("instant_cadence")
async def test_a_failed_lease_write_is_loud_and_the_turn_carries_on(
    fake_redis: aioredis.Redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A turn must not silently proceed BELIEVING ITSELF PROTECTED. The renewal is best-effort
    # by design — a Redis blip may not take a ten-minute build down — so "loud" is the whole
    # mitigation, and a swallowed failure would leave a live build reapable with nothing in
    # the log to explain the teardown afterwards.
    monkeypatch.setattr(engine_mod, "get_redis", lambda: _RefusingRedis())
    state = _turn_state(USER, FakeSandboxClient())
    with capture_logs() as logs:
        await _renew_a_while(state, ticks=6)
    failures = [entry for entry in logs if entry["event"] == engine_mod.LEASE_RENEW_FAILED_EVENT]
    assert [entry["reason"] for entry in failures] == ["store_unavailable"] * len(failures)
    # More than one: the loop RETRIED rather than dying on the first blip, which is the
    # "carries on" half. A single entry would also be consistent with the task raising out.
    assert len(failures) > 1
    assert await fake_redis.exists(lease_key(USER)) == 0


@pytest.mark.usefixtures("instant_cadence")
async def test_a_renewal_with_nothing_to_protect_is_loud_in_its_own_words(
    fake_redis: aioredis.Redis,
) -> None:
    # The OTHER failure shape, and it must not be reported as the first: a reachable store
    # that had no sandbox record to attach the lease to. Same greppable event so one alert
    # covers "is anything protecting live builds right now", different `reason` so the
    # on-call reads the right runbook. Deliberately no registry seeded.
    state = _turn_state(USER, FakeSandboxClient())
    with capture_logs() as logs:
        await _renew_a_while(state, ticks=6)
    reasons = {
        entry.get("reason")
        for entry in logs
        if entry["event"] == engine_mod.LEASE_RENEW_FAILED_EVENT
    }
    assert reasons == {"no_registry"}
    assert await fake_redis.exists(lease_key(USER)) == 0


# --- the assertion the in-process set could never make ------------------------
#
# Two Redis CLIENTS over one store, sharing no Python object with the turn. That is the
# structural content of "another process": `sweep_all` reaches the lease through the store
# alone, holding none of the turn's in-memory state, and `live_users` is forced empty so the
# in-process shield cannot be what spares anything. (A genuine second OS process is not
# available in this lane — the suite runs on fakeredis and `.env.test` configures no Redis
# server — so this is the strongest available form of the claim.)


@pytest.fixture
async def two_clients_one_store() -> AsyncIterator[tuple[aioredis.Redis, aioredis.Redis]]:
    """(the turn's client, the sweep's client) — independent connections to one store."""
    from src.services.redis import client as _redis_client

    server = fakeredis.FakeServer()
    turn_side = fakeredis.aioredis.FakeRedis(server=server, decode_responses=True)
    sweep_side = fakeredis.aioredis.FakeRedis(server=server, decode_responses=True)
    _redis_client._redis_singleton = turn_side
    yield turn_side, sweep_side
    await turn_side.flushall()
    await turn_side.aclose()
    await sweep_side.aclose()
    _redis_client._redis_singleton = None


@pytest.mark.usefixtures("instant_cadence")
async def test_a_sweep_sharing_only_the_store_spares_a_held_lease(
    two_clients_one_store: tuple[aioredis.Redis, aioredis.Redis],
) -> None:
    turn_side, sweep_side = two_clients_one_store
    await _register(turn_side, USER)
    # Deliberately NO lock and NO heartbeat: 90 seconds into any build that pair has lapsed,
    # and the lease is then the only thing between a live build and a teardown.
    state = _turn_state(USER, FakeSandboxClient())
    engine = TurnEngine()
    state.lease_task = asyncio.create_task(engine._hold_liveness_lease(state))
    try:
        for _ in range(5):
            await asyncio.sleep(0)
        client = FakeSandboxClient()
        # `live_users` forced empty — the in-process shield is not available to explain this.
        result = await reaper.sweep_all(sweep_side, client, live_users=set())
        assert result == reaper.SweepResult(reaped=0, failed=0)
        assert client.torn_down == []
        assert await locks.read_registry(sweep_side, USER) is not None
    finally:
        await engine._stop_liveness_lease(state)


async def test_a_sweep_sharing_only_the_store_reaps_a_lapsed_lease(
    two_clients_one_store: tuple[aioredis.Redis, aioredis.Redis],
) -> None:
    # The other half of the claim, and the one that keeps the first honest: with the lease
    # lapsed the very same sweep DOES reap. Without this, a sweep that spared everything
    # unconditionally would pass the test above.
    turn_side, sweep_side = two_clients_one_store
    await _register(turn_side, USER)
    await turn_side.set(lease_key(USER), str(time.time() - 1), ex=LIVENESS_LEASE_TTL_SECONDS)
    client = FakeSandboxClient()
    result = await reaper.sweep_all(sweep_side, client, live_users=set())
    assert result.reaped == 1
    assert a_sandbox_name("x") in client.torn_down
    assert await locks.read_registry(sweep_side, USER) is None


# --- the cadence has to fit inside the TTL -----------------------------------


def test_the_renewal_cadence_leaves_head_room_inside_the_ttl() -> None:
    # A cadence at or above the TTL means the lease lapses between renewals under a perfectly
    # healthy build — the sweep then reaps mid-build and the log says the container was idle.
    # Same head-room reasoning as LOCK_RENEW_CADENCE_SECONDS vs LOCK_TTL_SECONDS (C3).
    assert LIVENESS_LEASE_RENEW_CADENCE_SECONDS * 2 <= LIVENESS_LEASE_TTL_SECONDS


async def test_the_lease_key_is_environment_scoped_and_disjoint_from_its_neighbours() -> None:
    # R22: a process pointed at the wrong Redis must not read another environment's lease and
    # spare — or fail to spare — the wrong fleet. And the family discriminator keeps a lease
    # from ever being read as the lock or the heartbeat.
    assert lease_key(USER).startswith("bial:development:sandbox:lease:")
    assert len({lease_key(USER), lock_key(USER), heartbeat_key(USER), registry_key(USER)}) == 4
