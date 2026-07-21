"""U3 — the C5 lock / heartbeat / registry-state primitives (deterministic fakeredis)."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
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
    """U3 — fail CLOSED, but SAY WHICH KIND of closed.

    The fail-closed half is unchanged and non-negotiable: a Redis error never hands out a
    token. What changed is the signal. `None` is reserved for the one certain answer —
    "the lock is genuinely held" — because the caller turns that into a 409 naming a live
    build session. Folding an outage into the same `None` is what made every Redis blip
    surface as "a build session is already active" for a user who had none.

    `LockUnavailableError` SUBCLASSES `RedisError` (the additive `StorageUnconfiguredError`
    shape), so a caller that only knows `except RedisError` still catches it.
    """

    async def boom(*args: object, **kwargs: object) -> object:
        raise RedisError("redis is down")

    monkeypatch.setattr(fake_redis, "set", boom)
    with pytest.raises(locks.LockUnavailableError) as caught:
        await locks.acquire_lock(fake_redis, USER)
    assert isinstance(caught.value, RedisError)  # every existing `except RedisError` still hits
    assert "redis is down" not in str(caught.value)  # no store detail on the way out


async def test_acquire_none_is_reserved_for_a_lock_that_is_genuinely_held(
    fake_redis: aioredis.Redis,
) -> None:
    # The other half of the U3 split, pinned at the same seam: with a HEALTHY Redis, a
    # refused acquire still returns `None` — so the 409 the manager builds from it always
    # describes a real holder. Without this, "raise on error" could be satisfied by raising
    # on everything.
    assert await locks.acquire_lock(fake_redis, USER) is not None
    assert await locks.acquire_lock(fake_redis, USER) is None


async def _boom(*args: object, **kwargs: object) -> object:
    raise RedisError("redis is down")


async def _cancelled(*args: object, **kwargs: object) -> object:
    raise asyncio.CancelledError


async def test_the_one_guard_never_swallows_cancellation(
    fake_redis: aioredis.Redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`acquire_lock` is the single primitive here that catches, and it catches NARROWLY.
    `CancelledError` is a `BaseException`, so it passes through untouched — load-bearing on
    a ~23-minute build endpoint, where a guard widened to `except Exception` would eat a
    cancellation and wedge the task instead of unwinding it. It would also silently convert
    a cancel into a fail-closed `None`, i.e. a phantom "lock already held"."""
    monkeypatch.setattr(fake_redis, "set", _cancelled)
    with pytest.raises(asyncio.CancelledError):
        await locks.acquire_lock(fake_redis, USER)


@pytest.mark.parametrize(
    ("method", "call"),
    [
        ("eval", lambda r: locks.renew_lock(r, USER, "a-token")),
        ("eval", lambda r: locks.release_lock_as_holder(r, USER, "a-token")),
        ("set", lambda r: locks.write_heartbeat(r, USER)),
        ("get", lambda r: locks.reap_lock(r, USER)),
        ("exists", lambda r: locks.lock_is_held(r, USER)),
        ("hgetall", lambda r: locks.read_registry(r, USER)),
        ("exists", lambda r: locks.mark_registry_ending(r, USER)),
        ("delete", lambda r: locks.delete_registry(r, USER)),
    ],
)
async def test_every_primitive_but_acquire_still_surfaces_redis_errors(
    fake_redis: aioredis.Redis,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    call: Callable[[aioredis.Redis], Awaitable[object]],
) -> None:
    """`acquire_lock` is the ONLY primitive in this module that catches `RedisError`. This
    pins every other one bare, so no future guard can be added silently.

    Two distinct reasons live in this one list. Most entries are ANSWER-BEARING, and a
    swallow would manufacture a false answer out of an ambiguous store: `lock_is_held` False
    is fail-OPEN, `read_registry` None is a phantom "no sandbox", `renew_lock` False is a
    phantom "lock lost" that ends a healthy build, and a swallowed `mark_registry_ending`
    lets a concurrent attach reconnect to a container the reaper is about to delete.

    `release_lock_as_holder` and `write_heartbeat` are here for a DIFFERENT reason, and it
    is the one that is easy to get wrong (it was, once): they look like compensation paths
    that deserve a guard, but every caller that wants one already guards at the call site
    (`manager.py:365`, `:1046`, `:873`), and at `manager.py:398` / `:554` the raise is
    precisely what triggers the container teardown. See the two
    `test_relaunch_tears_down_the_container_when_*` tests in `test_manager.py`, which pin
    that behaviour end to end."""
    monkeypatch.setattr(fake_redis, method, _boom)
    with pytest.raises(RedisError):
        await call(fake_redis)


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
