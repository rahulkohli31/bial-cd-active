"""U3 — the reaper ordering + reconciliation sweep (deterministic fakeredis + a fake C2
client). Asserts the KTD-3 drifted-lock reclaim, the mark-ending-before-teardown order,
and sweep idempotency/timer-safety."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import redis.asyncio as aioredis

from src.api.v1.build_sessions.schemas import RELAUNCH_PREVIEW_STAY_SECONDS
from src.services.build_sessions import locks, reaper
from src.services.redis import (
    REGISTRY_STATE_ENDING,
    REGISTRY_STATE_READY,
    heartbeat_key,
    lock_key,
    registry_key,
)
from src.services.redis.keys import (
    REGISTRY_FIELD_APP_NAME,
    REGISTRY_FIELD_CREATED_AT,
    REGISTRY_FIELD_FQDN,
    REGISTRY_FIELD_PREVIEW_STAY_UNTIL,
    REGISTRY_FIELD_STATE,
    REGISTRY_FIELD_TOKEN_REF,
)
from src.services.sandbox import SandboxError, SandboxHandle
from tests.fakes import FakeSandboxClient

USER = uuid.uuid4()
OTHER = uuid.uuid4()
LOCK_TTL = 900
HB_TTL = 90


async def _seed(
    redis: aioredis.Redis,
    user: uuid.UUID,
    *,
    app_name: str = "sbx-x",
    with_lock: bool = True,
    with_heartbeat: bool = True,
) -> None:
    await redis.hset(
        registry_key(user),
        mapping={
            REGISTRY_FIELD_APP_NAME: app_name,
            REGISTRY_FIELD_FQDN: f"{app_name}.example",
            REGISTRY_FIELD_TOKEN_REF: "ref-123",
            REGISTRY_FIELD_CREATED_AT: "2026-07-14T00:00:00+00:00",
            REGISTRY_FIELD_STATE: REGISTRY_STATE_READY,
        },
    )
    if with_lock:
        await redis.set(lock_key(user), "some-crashed-token", ex=LOCK_TTL)
    if with_heartbeat:
        await redis.set(heartbeat_key(user), "beat", ex=HB_TTL)


class OrderTrackingClient(FakeSandboxClient):
    """Records the registry `state` observed AT teardown time — proves mark-ending runs
    BEFORE teardown (the guard a concurrent attach depends on)."""

    def __init__(self, redis: aioredis.Redis, user: uuid.UUID) -> None:
        super().__init__()
        self._redis = redis
        self._user = user
        self.state_at_teardown: str | None = None

    async def teardown(self, handle: SandboxHandle) -> None:
        reg = await self._redis.hgetall(registry_key(self._user))
        value = reg.get(REGISTRY_FIELD_STATE)
        self.state_at_teardown = value.decode() if isinstance(value, bytes) else value
        await super().teardown(handle)


async def test_reap_user_marks_ending_before_teardown_then_releases(
    fake_redis: aioredis.Redis,
) -> None:
    await _seed(fake_redis, USER)
    client = OrderTrackingClient(fake_redis, USER)
    assert await reaper.reap_user(fake_redis, USER, client) is True
    assert client.state_at_teardown == REGISTRY_STATE_ENDING  # marked BEFORE teardown
    assert "sbx-x" in client.torn_down
    assert await locks.read_registry(fake_redis, USER) is None  # registry cleared
    assert await locks.lock_is_held(fake_redis, USER) is False  # lock released AFTER teardown


async def test_reconcile_reaps_on_expired_lock(fake_redis: aioredis.Redis) -> None:
    await _seed(fake_redis, USER, with_lock=False, with_heartbeat=False)
    client = FakeSandboxClient()
    assert await reaper.reconcile_user(fake_redis, USER, client, has_live_session=False) is True
    assert "sbx-x" in client.torn_down
    assert await locks.read_registry(fake_redis, USER) is None


async def test_reconcile_reaps_on_lapsed_heartbeat(fake_redis: aioredis.Redis) -> None:
    await _seed(fake_redis, USER, with_lock=True, with_heartbeat=False)
    client = FakeSandboxClient()
    assert await reaper.reconcile_user(fake_redis, USER, client, has_live_session=False) is True
    assert await locks.read_registry(fake_redis, USER) is None


async def test_reconcile_leaves_a_live_session_untouched(fake_redis: aioredis.Redis) -> None:
    await _seed(fake_redis, USER, with_lock=True, with_heartbeat=True)
    client = FakeSandboxClient()
    # A session this process still owns is never reaped by heartbeat lapse.
    assert await reaper.reconcile_user(fake_redis, USER, client, has_live_session=True) is False
    assert client.torn_down == []
    assert await locks.read_registry(fake_redis, USER) is not None
    # Even with no in-proc session, both-alive is left alone (bounded by the heartbeat TTL).
    assert await reaper.reconcile_user(fake_redis, USER, client, has_live_session=False) is False


# --- #10/R3: the certified-dead reap-through ----------------------------------
#
# `lock_is_held AND heartbeat_is_alive` is a FACADE, not liveness: a process that died
# mid-build leaves both lingering up to their TTLs. Whether the facade may be trusted
# depends on what the CALLER knows, so the flag is a caller assertion, not a heuristic:
# reconcile-on-start (under `_start_lock_for`, `_active_by_user` checked, single replica)
# certifies nobody is alive and reaps through; the sweep certifies nothing and keeps the
# shield (it is what protects an in-flight start's pre-adopt seeded heartbeat).


async def test_certified_dead_reaps_through_a_lingering_lock_and_heartbeat(
    fake_redis: aioredis.Redis,
) -> None:
    # THE WALKTHROUGH 409 (#10): dead session, lock + heartbeat still lingering. The
    # certified reconcile reaps the ghost and the immediately-following acquire succeeds —
    # the user is never told a build is running when nothing is.
    await _seed(fake_redis, USER, with_lock=True, with_heartbeat=True)
    client = FakeSandboxClient()
    assert (
        await reaper.reconcile_user(
            fake_redis, USER, client, has_live_session=False, certified_dead=True
        )
        is True
    )
    assert "sbx-x" in client.torn_down  # the ghost's container is executed, not orphaned
    assert await locks.read_registry(fake_redis, USER) is None
    assert await locks.acquire_lock(fake_redis, USER) is not None  # no 409 on a phantom


async def test_the_sweep_never_certifies_and_still_trusts_the_facade(
    fake_redis: aioredis.Redis,
) -> None:
    # The sweep holds neither certifying fact, so lock+heartbeat MUST still shield — that
    # window is exactly where an in-flight start lives between its heartbeat seed and its
    # `_active_by_user` registration. `sweep_all` has no certified_dead parameter at all;
    # this pins that its inner reconcile keeps the default.
    await _seed(fake_redis, USER, with_lock=True, with_heartbeat=True)
    client = FakeSandboxClient()
    assert await reaper.sweep_all(fake_redis, client) == 0
    assert client.torn_down == []
    assert await locks.read_registry(fake_redis, USER) is not None


async def test_certification_never_overrides_an_in_process_session(
    fake_redis: aioredis.Redis,
) -> None:
    # has_live_session=True wins over everything, certification included: the in-process
    # session IS liveness, not a facade — a caller that passes both has contradicted
    # itself, and the safe reading wins.
    await _seed(fake_redis, USER, with_lock=True, with_heartbeat=True)
    client = FakeSandboxClient()
    assert (
        await reaper.reconcile_user(
            fake_redis, USER, client, has_live_session=True, certified_dead=True
        )
        is False
    )
    assert client.torn_down == []
    assert await locks.read_registry(fake_redis, USER) is not None


async def test_reconcile_reclaims_drifted_lock_and_next_start_acquires(
    fake_redis: aioredis.Redis,
) -> None:
    # KTD-3 core: registry + a LIVE lock + an ABSENT heartbeat + no in-proc session.
    await _seed(fake_redis, USER, with_lock=True, with_heartbeat=False)
    client = FakeSandboxClient()
    assert await locks.lock_is_held(fake_redis, USER) is True
    assert await reaper.reconcile_user(fake_redis, USER, client, has_live_session=False) is True
    # The value-guarded reaper release DELETED the still-live lock (never the holder helper).
    assert await locks.lock_is_held(fake_redis, USER) is False
    # The immediately-following start acquire succeeds — no 409 on a phantom session.
    assert await locks.acquire_lock(fake_redis, USER) is not None


async def test_sweep_all_reaps_lapsed_and_is_idempotent(fake_redis: aioredis.Redis) -> None:
    await _seed(fake_redis, USER, with_lock=True, with_heartbeat=False)  # lapsed -> reapable
    await _seed(fake_redis, OTHER, app_name="sbx-y", with_lock=True, with_heartbeat=True)  # live
    client = FakeSandboxClient()
    assert await reaper.sweep_all(fake_redis, client) == 1  # only USER
    assert await locks.read_registry(fake_redis, USER) is None
    assert await locks.read_registry(fake_redis, OTHER) is not None
    # A second immediate sweep is a clean no-op (idempotent / timer-safe, KTD-3).
    assert await reaper.sweep_all(fake_redis, client) == 0


async def test_sweep_all_skips_live_users(fake_redis: aioredis.Redis) -> None:
    await _seed(fake_redis, USER, with_lock=True, with_heartbeat=False)
    client = FakeSandboxClient()
    # A user with a live in-proc session is never reaped, so its registry survives.
    assert await reaper.sweep_all(fake_redis, client, live_users={USER}) == 0
    assert await locks.read_registry(fake_redis, USER) is not None


async def test_reaper_teardown_failure_keeps_state_for_retry(fake_redis: aioredis.Redis) -> None:
    await _seed(fake_redis, USER, with_lock=True, with_heartbeat=False)
    client = FakeSandboxClient()
    client.teardown_error = SandboxError("teardown boom")
    assert await reaper.reap_user(fake_redis, USER, client) is False
    # Teardown failed -> registry + lock KEPT for a later sweep (never orphan a live box).
    assert await locks.read_registry(fake_redis, USER) is not None
    assert await locks.lock_is_held(fake_redis, USER) is True


# --- #43: the relaunched preview's stay of execution --------------------------
#
# A relaunched preview holds no lock and renews no heartbeat, so it trips the
# lock+heartbeat guard the instant its seeded beat lapses. Its bounded stay is what keeps
# the background sweep off it — and reconcile-on-start is deliberately NOT fooled by that
# stay, because the incoming build needs the one-per-user slot. These two behaviours are
# opposite ON PURPOSE; the pair below is the regression guard against "simplifying" them.


async def _seed_preview(
    redis: aioredis.Redis, user: uuid.UUID, *, stay: str, app_name: str = "sbx-preview"
) -> None:
    """A relaunched preview as it actually sits in Redis: registry + a raw stay value, NO
    lock (the relaunch released it) and NO heartbeat (nothing renews it)."""
    await _seed(redis, user, app_name=app_name, with_lock=False, with_heartbeat=False)
    await redis.hset(registry_key(user), REGISTRY_FIELD_PREVIEW_STAY_UNTIL, stay)


def _in(seconds: float) -> str:
    return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat()


async def test_sweep_spares_a_preview_inside_its_stay(fake_redis: aioredis.Redis) -> None:
    await _seed_preview(fake_redis, USER, stay=_in(600))
    client = FakeSandboxClient()
    assert await reaper.sweep_all(fake_redis, client) == 0
    assert client.torn_down == []
    assert await locks.read_registry(fake_redis, USER) is not None


async def test_sweep_reaps_a_preview_once_its_stay_lapses(fake_redis: aioredis.Redis) -> None:
    # A past deadline is a LAPSED lease — no sleeping out a real 30-minute TTL.
    await _seed_preview(fake_redis, USER, stay=_in(-1))
    client = FakeSandboxClient()
    assert await reaper.sweep_all(fake_redis, client) == 1
    assert "sbx-preview" in client.torn_down
    assert await locks.read_registry(fake_redis, USER) is None


async def test_start_reconcile_reaps_through_a_current_stay(fake_redis: aioredis.Redis) -> None:
    # THE CRUX. reconcile-on-start defaults to honor_stay=False and reaps the preview even
    # mid-lease: the incoming build claims the slot, so sparing the preview would leave its
    # container running under a registry entry the new build is about to overwrite.
    await _seed_preview(fake_redis, USER, stay=_in(600))
    client = FakeSandboxClient()
    assert await reaper.reconcile_user(fake_redis, USER, client, has_live_session=False) is True
    assert "sbx-preview" in client.torn_down  # torn down, NOT orphaned
    assert await locks.read_registry(fake_redis, USER) is None
    assert await locks.acquire_lock(fake_redis, USER) is not None  # the slot is free


async def test_a_normal_build_session_is_unaffected_by_the_stay_check(
    fake_redis: aioredis.Redis,
) -> None:
    # No stay field at all (a real build never grants one): both paths behave exactly as
    # they did before the lease existed — live is spared, lapsed is reaped.
    await _seed(fake_redis, USER, with_lock=True, with_heartbeat=True)  # live build
    client = FakeSandboxClient()
    assert await reaper.sweep_all(fake_redis, client) == 0
    assert await reaper.reconcile_user(fake_redis, USER, client, has_live_session=False) is False

    await _seed(fake_redis, OTHER, app_name="sbx-y", with_lock=True, with_heartbeat=False)
    assert await reaper.sweep_all(fake_redis, client) == 1  # lapsed heartbeat → still reaped
    assert await locks.read_registry(fake_redis, OTHER) is None


async def test_a_malformed_stay_is_lapsed_not_a_reprieve(fake_redis: aioredis.Redis) -> None:
    # FAIL CLOSED: garbage or an empty stay buys NOTHING. An un-reaped container is a real
    # resource leak, so an unreadable lease must never grant an unbounded reprieve.
    await _seed_preview(fake_redis, USER, stay="not-a-timestamp")
    await _seed_preview(fake_redis, OTHER, stay="", app_name="sbx-empty")
    assert await locks.stay_of_execution_is_current(fake_redis, USER) is False
    assert await locks.stay_of_execution_is_current(fake_redis, OTHER) is False
    client = FakeSandboxClient()
    assert await reaper.sweep_all(fake_redis, client) == 2
    assert await locks.read_registry(fake_redis, USER) is None
    assert await locks.read_registry(fake_redis, OTHER) is None


async def test_an_absent_stay_reads_as_lapsed(fake_redis: aioredis.Redis) -> None:
    # No registry at all, and a registry with no stay field: both False (reapable).
    assert await locks.stay_of_execution_is_current(fake_redis, USER) is False
    await _seed(fake_redis, USER, with_lock=False, with_heartbeat=False)
    assert await locks.stay_of_execution_is_current(fake_redis, USER) is False


async def test_grant_stay_never_conjures_a_registry(fake_redis: aioredis.Redis) -> None:
    # Guarded on existence exactly like mark_registry_ending: a user with no sandbox must
    # not end up with a one-field registry hash that the sweep would then try to tear down.
    deadline = await locks.grant_stay_of_execution(fake_redis, USER, ttl_seconds=60)
    assert deadline > datetime.now(UTC)
    assert await locks.read_registry(fake_redis, USER) is None
    # With a registry present the deadline lands on the hash and reads back as current.
    await _seed(fake_redis, USER, with_lock=False, with_heartbeat=False)
    await locks.grant_stay_of_execution(fake_redis, USER, ttl_seconds=60)
    assert await locks.stay_of_execution_is_current(fake_redis, USER) is True


async def test_a_naive_stay_stamp_is_read_as_utc(fake_redis: aioredis.Redis) -> None:
    # A tz-naive stamp must not blow up, and must be interpreted as UTC — never as the
    # HOST's local time. Seeding a single naive stamp of the CURRENT instant does not prove
    # that: read as local it lands a whole UTC offset away from now, which on UTC itself
    # and on every host EAST of it still reads as lapsed — so the assertion holds while the
    # tz handling is wrong. Measured against a local-reading mutation: UTC and +05:30 both
    # stayed green; only a westward host went red. CI runs on UTC, i.e. exactly where the
    # single-stamp version proves nothing.
    #
    # The PAIR is what pins it, on a host at any UTC offset. Both stamps are derived from
    # UTC and sit 10 minutes either side of it, which is far outside any real offset's
    # ability to flip a verdict by accident:
    #   * naive UTC now+10min  -> True  as UTC; on any host EAST of UTC (e.g. +05:30),
    #                                   reading it as local shifts it into the PAST -> False.
    #   * naive UTC now-10min  -> False as UTC; on any host WEST of UTC (e.g. -08:00),
    #                                   reading it as local shifts it into the FUTURE -> True.
    # So one of the two goes red the moment the stamp is read as local time anywhere off UTC.
    naive_utc_now = datetime.now(UTC).replace(tzinfo=None)
    await _seed_preview(fake_redis, USER, stay=(naive_utc_now + timedelta(minutes=10)).isoformat())
    assert await locks.stay_of_execution_is_current(fake_redis, USER) is True
    await _seed_preview(
        fake_redis, OTHER, stay=(naive_utc_now - timedelta(minutes=10)).isoformat()
    )
    assert await locks.stay_of_execution_is_current(fake_redis, OTHER) is False


async def test_an_absurdly_distant_stay_is_lapsed_not_an_unbounded_reprieve(
    fake_redis: aioredis.Redis,
) -> None:
    # FAIL CLOSED on the OTHER side too. A year-9999 stamp is perfectly parseable, so a
    # bare `deadline > now` check hands a container nobody owns a reprieve measured in
    # millennia — the unbounded reprieve the fail-closed contract exists to forbid, reached
    # THROUGH the parse instead of around it. The window is bounded on both sides:
    # now < deadline <= now + RELAUNCH_PREVIEW_STAY_SECONDS.
    await _seed_preview(fake_redis, USER, stay="9999-12-31T23:59:59+00:00")
    assert await locks.stay_of_execution_is_current(fake_redis, USER) is False
    # ...and the sweep actually reaps it, rather than sparing it until the year 9999.
    client = FakeSandboxClient()
    assert await reaper.sweep_all(fake_redis, client) == 1
    assert "sbx-preview" in client.torn_down
    assert await locks.read_registry(fake_redis, USER) is None
    # One second past the ceiling is already too far — the bound is the lease length itself,
    # not a generous "looks sane" heuristic.
    await _seed_preview(fake_redis, OTHER, stay=_in(RELAUNCH_PREVIEW_STAY_SECONDS + 60))
    assert await locks.stay_of_execution_is_current(fake_redis, OTHER) is False
    # A stay AT the maximum grantable length is still honored (the ceiling is inclusive) —
    # the clamp must not shave real leases, only absurd ones.
    await _seed_preview(fake_redis, OTHER, stay=_in(RELAUNCH_PREVIEW_STAY_SECONDS - 1))
    assert await locks.stay_of_execution_is_current(fake_redis, OTHER) is True
