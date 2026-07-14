"""U3 — the C5 lock / heartbeat / registry-state primitives (deterministic fakeredis)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import pytest
import redis.asyncio as aioredis
from redis.exceptions import RedisError

from src.api.v1.build_sessions.schemas import (
    HEARTBEAT_TTL_SECONDS,
    LOCK_RENEW_CADENCE_SECONDS,
    LOCK_TTL_SECONDS,
)
from src.services.build_sessions import locks
from src.services.redis import REGISTRY_STATE_ENDING, heartbeat_key, registry_key
from src.services.redis.keys import REGISTRY_FIELD_APP_NAME, REGISTRY_FIELD_STATE

USER = uuid.uuid4()
OTHER = uuid.uuid4()


async def test_acquire_is_exclusive_per_user(fake_redis: aioredis.Redis) -> None:
    token = await locks.acquire_lock(fake_redis, USER)
    assert token is not None
    # A second acquire for the same user is refused (the NX one-per-user enforcement).
    assert await locks.acquire_lock(fake_redis, USER) is None
    # A different user acquires independently.
    assert await locks.acquire_lock(fake_redis, OTHER) is not None


async def test_holder_release_is_compare_and_delete(fake_redis: aioredis.Redis) -> None:
    token = await locks.acquire_lock(fake_redis, USER)
    assert token is not None
    # A stale token never releases someone else's lock.
    assert await locks.release_lock_as_holder(fake_redis, USER, "not-the-token") is False
    assert await locks.lock_is_held(fake_redis, USER) is True
    # The holder's own token releases.
    assert await locks.release_lock_as_holder(fake_redis, USER, token) is True
    assert await locks.lock_is_held(fake_redis, USER) is False


async def test_renew_extends_on_match_and_signals_lost_on_mismatch(
    fake_redis: aioredis.Redis,
) -> None:
    token = await locks.acquire_lock(fake_redis, USER)
    assert token is not None
    assert await locks.renew_lock(fake_redis, USER, token) is True
    assert await locks.renew_lock(fake_redis, USER, "stale-token") is False  # lock lost


async def test_heartbeat_sets_ttl_and_reports_alive(fake_redis: aioredis.Redis) -> None:
    expires = await locks.write_heartbeat(fake_redis, USER)
    assert await locks.heartbeat_is_alive(fake_redis, USER) is True
    ttl = await fake_redis.ttl(heartbeat_key(USER))
    assert 0 < ttl <= HEARTBEAT_TTL_SECONDS
    assert expires > datetime.now(UTC)  # a future idle instant


def test_lock_ttl_has_renew_headroom() -> None:
    # The C3-frozen constants (900/300), NOT C5's proposed 300 s lock default: a renew
    # always has head-room, so an active build never drops its lock at the cadence.
    assert LOCK_TTL_SECONDS == 900
    assert LOCK_TTL_SECONDS > LOCK_RENEW_CADENCE_SECONDS


async def test_reap_lock_reclaims_a_drifted_lock(fake_redis: aioredis.Redis) -> None:
    await locks.acquire_lock(fake_redis, USER)  # a crashed session's token — not ours
    assert await locks.lock_is_held(fake_redis, USER) is True
    assert await locks.reap_lock(fake_redis, USER) is True  # value-guarded reclaim
    assert await locks.lock_is_held(fake_redis, USER) is False
    # A second reap on an absent lock is a clean no-op.
    assert await locks.reap_lock(fake_redis, USER) is False


async def test_acquire_fails_closed_on_redis_error(
    fake_redis: aioredis.Redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def boom(*args: object, **kwargs: object) -> object:
        raise RedisError("redis is down")

    monkeypatch.setattr(fake_redis, "set", boom)
    # Fail CLOSED: a Redis error denies (no token), never a silent grant.
    assert await locks.acquire_lock(fake_redis, USER) is None


async def test_registry_state_helpers(fake_redis: aioredis.Redis) -> None:
    # mark_ending on an absent registry never conjures a partial hash.
    await locks.mark_registry_ending(fake_redis, USER)
    assert await locks.read_registry(fake_redis, USER) is None

    await fake_redis.hset(
        registry_key(USER),
        mapping={REGISTRY_FIELD_APP_NAME: "sbx-x", REGISTRY_FIELD_STATE: "ready"},
    )
    await locks.mark_registry_ending(fake_redis, USER)
    reg = await locks.read_registry(fake_redis, USER)
    assert reg is not None
    assert reg[REGISTRY_FIELD_STATE] == REGISTRY_STATE_ENDING

    await locks.delete_registry(fake_redis, USER)
    assert await locks.read_registry(fake_redis, USER) is None
