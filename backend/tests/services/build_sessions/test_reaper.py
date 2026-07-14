"""U3 — the reaper ordering + reconciliation sweep (deterministic fakeredis + a fake C2
client). Asserts the KTD-3 drifted-lock reclaim, the mark-ending-before-teardown order,
and sweep idempotency/timer-safety."""

from __future__ import annotations

import uuid

import redis.asyncio as aioredis

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
