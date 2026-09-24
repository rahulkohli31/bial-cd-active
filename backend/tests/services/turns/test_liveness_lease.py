"""The wall-clock liveness lease the turn engine publishes.

Until this lease existed, a build running past 90 seconds was indistinguishable from an
abandoned container to anything except the process running it — the only other shield is
`sweep_all`'s IN-PROCESS `live_users` set, empty everywhere else. So the property these tests
care about above all others is that the signal is legible to a reader holding nothing in common
with the turn but the store: `test_a_sweep_sharing_only_the_store_*` is that verification.

The rest pin five properties extracted the hard way from the preview lease: fail closed on
absent AND absurd, log loudly when the write does not land, disown when the record it belonged
to goes, grant while the lock is held then release, and confirm something scheduled READS it.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator, Callable, Iterator

import fakeredis
import fakeredis.aioredis
import pytest
import redis.asyncio as aioredis
from redis.exceptions import RedisError
from structlog.testing import capture_logs

import src.services.turns.engine as engine_mod
from src.api.v1.build_sessions.schemas import (
    HEARTBEAT_TTL_SECONDS,
    LIVENESS_LEASE_RENEW_CADENCE_SECONDS,
    LIVENESS_LEASE_TTL_SECONDS,
    LOCK_TTL_SECONDS,
)
from src.db.models.conversation import ChatKind
from src.services.build_sessions import locks, reaper
from src.services.build_sessions.manager import BuildSession
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
        kind=ChatKind.BUILD,
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
    # The TTL is mandatory: `ttl` returning -1 means "no expiry" and MUST never happen here.
    await _register(fake_redis, USER)
    before = time.time()
    assert await locks.renew_liveness_lease(fake_redis, USER) is True

    ttl = await fake_redis.ttl(lease_key(USER))
    assert 0 < ttl <= LIVENESS_LEASE_TTL_SECONDS

    deadline = await _stored_deadline(fake_redis, USER)
    # A wall-clock instant, comparable in ANY process — a monotonic reading would be meaningless
    # outside the process that took it and would fail this bound wildly.
    assert before <= deadline - LIVENESS_LEASE_TTL_SECONDS <= time.time()
    assert await locks.liveness_lease_is_held(fake_redis, USER) is True


async def test_each_renewal_pushes_the_deadline_forward(
    fake_redis: aioredis.Redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The deadline has to MOVE on every renewal. Driven off a scripted clock because two real
    # renewals inside one microsecond are indistinguishable.
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
    # A value this module could not have written is evidence of nothing; reaping is the safe
    # direction.
    await fake_redis.set(lease_key(USER), value, ex=LIVENESS_LEASE_TTL_SECONDS)
    assert await locks.liveness_lease_is_held(fake_redis, USER) is False


async def test_a_lapsed_deadline_is_not_held(fake_redis: aioredis.Redis) -> None:
    # Readable, well-formed, and already past — the key's own TTL normally removes it, but the
    # comparison is what makes the answer right in the window before Redis gets around to it.
    await fake_redis.set(lease_key(USER), str(time.time() - 1), ex=LIVENESS_LEASE_TTL_SECONDS)
    assert await locks.liveness_lease_is_held(fake_redis, USER) is False


async def test_a_deadline_beyond_the_grantable_ceiling_is_not_held(
    fake_redis: aioredis.Redis,
) -> None:
    # THE ABSURD-VALUE EDGE: "unexpired" alone would let a bad clock, a hand-edited key, or a
    # future writer using milliseconds buy a reprieve measured in millennia. Nothing may outlive
    # what a FRESH renewal could have granted.
    far_future = time.time() + LIVENESS_LEASE_TTL_SECONDS * 100
    await fake_redis.set(lease_key(USER), str(far_future), ex=LIVENESS_LEASE_TTL_SECONDS)
    assert await locks.liveness_lease_is_held(fake_redis, USER) is False


async def test_a_reader_whose_clock_lags_the_writer_still_reads_the_lease_as_held(
    fake_redis: aioredis.Redis,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # THE SKEW EDGE, and it is the reason the ceiling carries a grace at all: this family exists
    # so a process not running the build can read it, and the writer's clock is never the
    # reader's clock. Without grace, a reader running fractionally behind would call a lease
    # renewed moments ago "absurd" and hand a live build to the reaper.
    # Mutation-check: drop LIVENESS_LEASE_CLOCK_SKEW_GRACE_SECONDS from the comparison
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
    # DISOWN ON REGISTRY REWRITE: with no record there is nothing for the lease to protect, and
    # a lease left behind would spare whatever container the next builder gets. The caller is
    # TOLD (False + a warning) rather than left believing a write landed.
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
    # Single-tenant does not mean single-user: a lease read that ignored the user id would
    # spare the whole fleet on one live build.
    await _register(fake_redis, USER)
    await locks.renew_liveness_lease(fake_redis, USER)
    assert await locks.liveness_lease_is_held(fake_redis, USER) is True
    assert await locks.liveness_lease_is_held(fake_redis, OTHER) is False


# --- the turn engine's renewal task ------------------------------------------


@pytest.mark.usefixtures("instant_cadence")
async def test_the_turn_holds_the_lease_for_as_long_as_it_runs(
    fake_redis: aioredis.Redis,
) -> None:
    # A reader that knows nothing about the turn must still see the lease held throughout.
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
    # GRANT WHILE THE LOCK IS HELD, THEN RELEASE: the turn's `finally` hands the container back
    # so the lease goes with it, and even a process that dies first is bounded by the key's own
    # expiry.
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
    # A turn holding no container has nothing to vouch for; the lease is keyed by USER, so
    # renewing one anyway would let a chat turn in one conversation buy a reprieve for
    # whatever the same user's slot is actually holding elsewhere.
    #
    # Asserted DURING the loop, not after it: `_stop_liveness_lease` deletes the key, so an
    # assertion at the end passes whether or not the guard exists.
    # Mutation check: remove the `state.sandbox is None` return and this stays green if the
    # assertion is moved to the bottom instead.
    await _register(fake_redis, USER)
    state = _TurnState(
        turn_id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        user_id=USER,
        kind=ChatKind.PLAN,
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
    # The other half of the same hazard: the stop path must not DELETE a lease it never wrote,
    # or a turn with no container reaching the shared `finally` strips protection off a build
    # running in another conversation.
    await _register(fake_redis, USER)
    await locks.renew_liveness_lease(fake_redis, USER)  # somebody else's live build
    state = _TurnState(
        turn_id=uuid.uuid4(),
        conversation_id=uuid.uuid4(),
        user_id=USER,
        kind=ChatKind.PLAN,
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

    async def eval(self, *_args: object, **_kwargs: object) -> object:
        # `renew_lock`'s compare-and-swap. Here so the LOCK arm of the loop meets the same
        # blip the lease arm does, rather than an AttributeError that would prove nothing.
        raise RedisError("boom")


@pytest.mark.usefixtures("instant_cadence")
async def test_a_failed_lease_write_is_loud_and_the_turn_carries_on(
    fake_redis: aioredis.Redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A turn must not silently proceed BELIEVING ITSELF PROTECTED. Renewal is best-effort by
    # design, so "loud" is the whole mitigation — a swallowed failure leaves a live build
    # reapable with nothing in the log to explain the teardown afterwards.
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
    # The OTHER failure shape: a reachable store with no sandbox record to attach the lease to.
    # Same greppable event, different `reason`, so the on-call reads the right runbook.
    # Deliberately no registry seeded.
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


# --- the lock + heartbeat that ride the same loop -----------------------------
#
# WHY THEY LIVE HERE AT ALL. `TurnEngine._hold_liveness_lease` is the only renewer of the
# one-sandbox-per-user lock and the heartbeat during a turn: a wall-clock tick, so a turn that
# spends long stretches inside one tool call still gets renewed. These tests are about the CLOCK,
# not the primitives: `test_locks.py` already pins what `renew_lock` and `write_heartbeat` do.


def _write_session(user: uuid.UUID, lock_token: str) -> BuildSession:
    """The manager's registry entry a Write turn carries on `state.write_session`. The renewal
    reads `lock_token` and `session_id`; the rest is honest filler so the type stays truthful
    about what a real session holds."""
    return BuildSession(
        session_id=uuid.uuid7(),
        user_id=user,
        project_id=uuid.uuid4(),
        app_id=uuid.uuid4(),
        lock_token=lock_token,
        handle=SandboxHandle(
            fqdn="x.example",
            token="t",  # noqa: S106 - a fake, never a real bearer
            app_name=a_sandbox_name("x"),
            preview_url="https://x.example/",
            ready=True,
        ),
    )


def _write_turn_state(user: uuid.UUID, lock_token: str) -> _TurnState:
    """A Build turn that took a container AND holds the user's lock — the only shape that may
    renew either of them."""
    state = _turn_state(user, FakeSandboxClient())
    state.write_session = _write_session(user, lock_token)
    return state


@pytest.fixture
def renewals(monkeypatch: pytest.MonkeyPatch) -> list[tuple[str, uuid.UUID, str]]:
    """Every lock/heartbeat call the loop makes, in order — still delegating to the real
    function, so the store's state stays the answer and this only records who asked."""
    calls: list[tuple[str, uuid.UUID, str]] = []
    # Delegating to `locks` rather than to the engine's re-exported names: same objects, and
    # reading them off the engine module is an implicit re-export mypy refuses.
    real_renew = locks.renew_lock
    real_beat = locks.write_heartbeat

    async def spy_renew(redis: aioredis.Redis, user_uuid: uuid.UUID, token: str) -> bool:
        calls.append(("renew_lock", user_uuid, token))
        return await real_renew(redis, user_uuid, token)

    async def spy_beat(redis: aioredis.Redis, user_uuid: uuid.UUID) -> object:
        calls.append(("write_heartbeat", user_uuid, ""))
        return await real_beat(redis, user_uuid)

    monkeypatch.setattr(engine_mod, "renew_lock", spy_renew)
    monkeypatch.setattr(engine_mod, "write_heartbeat", spy_beat)
    return calls


async def _pump_until(predicate: Callable[[], bool], *, limit: int = 500) -> None:
    """Bare event-loop turns until the loop has done the thing, rather than a fixed count that
    is either flaky or slow. Bounded so a loop that never gets there fails loudly."""
    for _ in range(limit):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("the renewal loop never reached the expected state")


def _calls(
    renewals: list[tuple[str, uuid.UUID, str]], name: str
) -> list[tuple[str, uuid.UUID, str]]:
    return [call for call in renewals if call[0] == name]


@pytest.mark.usefixtures("instant_cadence")
async def test_a_write_turn_renews_its_own_lock_on_every_tick(
    fake_redis: aioredis.Redis, renewals: list[tuple[str, uuid.UUID, str]]
) -> None:
    # THE REGRESSION THIS GUARDS. The lock is pushed back out to its full TTL on the loop's
    # clock, so a build that goes quiet for fifteen minutes still holds the slot it is working in.
    await _register(fake_redis, USER)
    token = await locks.acquire_lock(fake_redis, USER)
    assert token is not None
    # Wound down to nearly nothing, standing in for the minutes the real incident took to get
    # here.
    await fake_redis.expire(lock_key(USER), 5)

    state = _write_turn_state(USER, token)
    engine = TurnEngine()
    state.lease_task = asyncio.create_task(engine._hold_liveness_lease(state))
    try:
        await _pump_until(lambda: len(_calls(renewals, "renew_lock")) >= 2)
        # The SESSION'S token, never a wildcard: `renew_lock` is a compare-and-swap, so a
        # renewal under any other token silently renews nothing at all.
        assert set(_calls(renewals, "renew_lock")) == {("renew_lock", USER, token)}
        # Restored, not decaying — a TTL running down to zero was the whole defect.
        assert LOCK_TTL_SECONDS - 5 <= await fake_redis.ttl(lock_key(USER)) <= LOCK_TTL_SECONDS
        assert await locks.lock_is_held(fake_redis, USER) is True
    finally:
        await engine._stop_liveness_lease(state)


@pytest.mark.usefixtures("instant_cadence")
async def test_the_heartbeat_comes_back_and_stays_while_the_turn_runs(
    fake_redis: aioredis.Redis, renewals: list[tuple[str, uuid.UUID, str]]
) -> None:
    # The observed half of the regression was the heartbeat key vanishing at roughly 100 seconds
    # and never returning — the start seed is written ONCE per turn against a 90 s TTL, and
    # nothing on a clock re-wrote it. Deleted here to stand for that expiry, then the loop must
    # put it back.
    await _register(fake_redis, USER)
    token = await locks.acquire_lock(fake_redis, USER)
    assert token is not None
    await locks.write_heartbeat(fake_redis, USER)  # the once-per-turn seed...
    await fake_redis.delete(heartbeat_key(USER))  # ...which expired, ~100 s in

    state = _write_turn_state(USER, token)
    engine = TurnEngine()
    state.lease_task = asyncio.create_task(engine._hold_liveness_lease(state))
    try:
        await _pump_until(lambda: len(_calls(renewals, "write_heartbeat")) >= 2)
        assert await locks.heartbeat_is_alive(fake_redis, USER) is True
        assert 0 < await fake_redis.ttl(heartbeat_key(USER)) <= HEARTBEAT_TTL_SECONDS
    finally:
        await engine._stop_liveness_lease(state)


@pytest.mark.usefixtures("instant_cadence")
async def test_a_turn_holding_no_build_session_renews_nobody_elses_lock(
    fake_redis: aioredis.Redis, renewals: list[tuple[str, uuid.UUID, str]]
) -> None:
    # The guard, and it is the same hazard as the lease's: the lock and the heartbeat are keyed
    # by USER, so a turn that took no build session of its own would vouch for — and extend —
    # whatever this citizen's slot is actually holding in another conversation.
    await _register(fake_redis, USER)
    holder = await locks.acquire_lock(fake_redis, USER)  # somebody else's live build
    assert holder is not None
    await fake_redis.expire(lock_key(USER), 5)

    state = _turn_state(USER, FakeSandboxClient())  # a container, but no build session
    assert state.write_session is None
    engine = TurnEngine()
    with capture_logs() as logs:
        state.lease_task = asyncio.create_task(engine._hold_liveness_lease(state))
        try:
            for _ in range(20):
                await asyncio.sleep(0)
            # LIVENESS FIRST, AND IT IS THE ASSERTION THE MUTANT LANDS ON. Making the guard
            # unconditional turns `write_session.lock_token` into an AttributeError on `None`
            # — and the handler that would log it reads `write_session.session_id`, so it
            # raises too and the whole task dies on its first tick. Emptiness alone is green
            # under that (nothing was called, nothing was renewed); a loop that is still
            # ALIVE and still renewing the lease a full tick later is not.
            assert not state.lease_task.done()
            assert await fake_redis.exists(lease_key(USER)) == 1
            await fake_redis.delete(lease_key(USER))
            for _ in range(20):
                await asyncio.sleep(0)
            assert await fake_redis.exists(lease_key(USER)) == 1  # a second tick came round
            assert renewals == []
            assert await fake_redis.ttl(lock_key(USER)) <= 5  # the holder's TTL, untouched
            assert await fake_redis.exists(heartbeat_key(USER)) == 0
        finally:
            await engine._stop_liveness_lease(state)
    # AND IT DID NOT COMPLAIN EITHER — this line is what the mutation actually lands on.
    # Making the guard unconditional turns `write_session.lock_token` into an AttributeError on
    # `None`, which the loop catches and logs every tick: the three assertions above stay green
    # under that mutant (nothing was called, nothing was renewed), this one goes red.
    assert [entry for entry in logs if entry["event"] == engine_mod.LOCK_RENEW_FAILED_EVENT] == []


@pytest.mark.usefixtures("instant_cadence")
async def test_a_lock_lost_under_a_live_build_is_loud_and_the_turn_carries_on(
    fake_redis: aioredis.Redis, renewals: list[tuple[str, uuid.UUID, str]]
) -> None:
    # The lock lapsed and was re-acquired under a build that is still working, so the CAS
    # renewal matches nothing. Ending the turn here would destroy the very work the lock was
    # protecting, so it carries on — but the slot may now be double-allocated, and an on-call
    # engineer looking at a torn-down container needs this sentence in the log.
    await _register(fake_redis, USER)
    await locks.acquire_lock(fake_redis, USER)  # a token that is not ours
    state = _write_turn_state(USER, "not-the-holders-token-any-more")
    engine = TurnEngine()
    with capture_logs() as logs:
        state.lease_task = asyncio.create_task(engine._hold_liveness_lease(state))
        try:
            await _pump_until(lambda: len(_calls(renewals, "renew_lock")) >= 3)
            # NOT raised and NOT ended: the loop is still going three renewals later.
            assert not state.lease_task.done()
        finally:
            await engine._stop_liveness_lease(state)
    lost = [entry for entry in logs if entry["event"] == engine_mod.LOCK_LOST_EVENT]
    assert lost and all(entry["log_level"] == "warning" for entry in lost)
    assert {entry["user_id"] for entry in lost} == {str(USER)}
    # The heartbeat is written anyway: a lost lock must not ALSO read as an idle container and
    # earn the turn a teardown on top of it.
    assert _calls(renewals, "write_heartbeat")


@pytest.mark.usefixtures("instant_cadence")
async def test_a_store_that_refuses_the_lock_renewal_is_loud_and_the_turn_still_terminates(
    fake_redis: aioredis.Redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A Redis blip may not take a ten-minute build down, so the lock arm is best-effort exactly
    # like the lease arm — and, exactly like it, never silent. Both arms are hit here because
    # both reach the same refusing store, which is also the point of catching them separately:
    # neither failure costs the other its renewal.
    monkeypatch.setattr(engine_mod, "get_redis", lambda: _RefusingRedis())
    state = _write_turn_state(USER, "tok")
    with capture_logs() as logs:
        await _renew_a_while(state, ticks=8)
    lock_failures = [
        entry for entry in logs if entry["event"] == engine_mod.LOCK_RENEW_FAILED_EVENT
    ]
    lease_failures = [
        entry for entry in logs if entry["event"] == engine_mod.LEASE_RENEW_FAILED_EVENT
    ]
    # More than one of each: the loop RETRIED rather than dying on the first blip. A single
    # entry would also be consistent with the task raising straight out of the loop.
    assert len(lock_failures) > 1
    assert len(lease_failures) > 1
    assert all(entry["log_level"] == "error" for entry in lock_failures)
    # The turn reached its terminal all the same — the `finally` ran and the task is gone.
    assert state.lease_task is None


# --- the assertion the in-process set could never make ------------------------
#
# Two Redis CLIENTS over one store, sharing no Python object with the turn: `sweep_all` reaches
# the lease through the store alone, and `live_users` is forced empty so the in-process shield
# cannot be what spares anything. A genuine second OS process isn't available in this lane (the
# suite runs on fakeredis with no real Redis server), so this is the strongest available form
# of the claim.


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
    # Keeps the test above honest: with the lease lapsed the very same sweep DOES reap. Without
    # this, a sweep that spared everything unconditionally would pass it too.
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
    # A cadence at or above the TTL lapses the lease between renewals under a healthy build —
    # the sweep then reaps mid-build. Same head-room reasoning as LOCK_RENEW_CADENCE_SECONDS
    # vs LOCK_TTL_SECONDS.
    assert LIVENESS_LEASE_RENEW_CADENCE_SECONDS * 2 <= LIVENESS_LEASE_TTL_SECONDS


async def test_the_lease_key_is_environment_scoped_and_disjoint_from_its_neighbours() -> None:
    # The lease obeys the environment scoping `src/services/redis/keys.py` sets, and the family
    # discriminator keeps it from ever being read as the lock or the heartbeat.
    assert lease_key(USER).startswith("bial:development:sandbox:lease:")
    assert len({lease_key(USER), lock_key(USER), heartbeat_key(USER), registry_key(USER)}) == 4
