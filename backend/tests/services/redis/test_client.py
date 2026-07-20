"""Pool-factory + lifecycle tests for the sandbox-coordination Redis client.

`redis.asyncio` connects lazily, so these construct and close a client WITHOUT a
live Redis server — they exercise the factory + the idempotent teardown, not I/O.
"""

from __future__ import annotations

import pytest
import redis.asyncio as aioredis
from pydantic import SecretStr
from redis.backoff import ExponentialWithJitterBackoff
from redis.exceptions import ConnectionError as RedisConnectionError

from src.services.redis.client import aclose_redis, create_redis, reset_redis_for_tests
from src.services.redis.config import RedisConfig

_URL = SecretStr("redis://localhost:6379/0")


async def test_create_redis_builds_a_pooled_client_without_connecting() -> None:
    client = create_redis(RedisConfig(url=_URL))
    try:
        assert isinstance(client, aioredis.Redis)
    finally:
        await client.aclose()


async def test_create_redis_honours_pool_knobs() -> None:
    client = create_redis(RedisConfig(url=_URL, max_connections=3))
    try:
        assert client.connection_pool.max_connections == 3
    finally:
        await client.aclose()


async def test_create_redis_installs_an_explicit_retry_policy() -> None:
    # THE regression pin. `from_url` always builds a connection pool, which skips the
    # branch of `Redis.__init__` that injects redis-py's advertised default retry, so
    # a client built without an explicit `retry=` silently runs `Retry(NoBackoff(), 0)`
    # — one attempt, no backoff, dead in 0.006s against a dead port. Asserting on the
    # RESOLVED retry (`get_retry()`, i.e. what the connections will actually use), not
    # on the constructor call, is the whole point: a passthrough assertion would stay
    # green even if redis-py dropped the kwarg on the floor.
    client = create_redis(RedisConfig(url=_URL))
    try:
        retry = client.get_retry()
        assert retry is not None
        assert retry.get_retries() == 3
        assert isinstance(retry._backoff, ExponentialWithJitterBackoff)  # noqa: SLF001
    finally:
        await client.aclose()


async def test_the_installed_retry_actually_retries_an_async_operation() -> None:
    """U3 found this the hard way: `get_retry()` reporting the right policy is NOT evidence
    that anything retries.

    `redis.retry.Retry` and `redis.asyncio.retry.Retry` are different classes with the same
    name. The async connection does `await self.retry.call_with_retry(do, ...)` where `do`
    returns a COROUTINE (`redis/asyncio/connection.py:351`). The sync class's
    `call_with_retry` is an ordinary function, so its `try/except` only ever sees the
    coroutine being CREATED — never awaited, never raising — and it hands the coroutine
    straight back to be awaited outside the retry loop. Every assertion on `get_retry()`
    stays green while the client makes exactly one attempt. Measured against a dead port:
    0.012s with the sync class, 0.395s (four attempts) with the async one.

    So this drives the RESOLVED retry object with a `do` that fails once and then succeeds,
    and asserts the caller never sees the failure — the behaviour, not the configuration.
    """
    client = create_redis(RedisConfig(url=_URL))
    try:
        retry = client.get_retry()
        assert retry is not None
        attempts = 0

        async def flaky_once() -> str:
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                raise RedisConnectionError("connection reset by peer")
            return "PONG"

        async def swallow(_exc: Exception) -> None:
            """redis-py's `fail` hook (awaited by the ASYNC Retry — being awaited at all is
            itself proof the async class is installed; the sync one never calls it)."""

        assert await retry.call_with_retry(flaky_once, swallow) == "PONG"
        assert attempts == 2  # one injected failure, one clean retry, no error observed
    finally:
        await client.aclose()


async def test_the_installed_retry_gives_up_after_the_configured_attempts() -> None:
    # The other side of the bound: retries are finite, so a genuinely dead store still
    # fails — 1 initial attempt + `retry_attempts` retries, then the error is re-raised.
    client = create_redis(RedisConfig(url=_URL, retry_attempts=2))
    try:
        retry = client.get_retry()
        assert retry is not None
        attempts = 0

        async def always_down() -> str:
            nonlocal attempts
            attempts += 1
            raise RedisConnectionError("connection refused")

        async def swallow(_exc: Exception) -> None:
            """No-op `fail` hook; the async Retry awaits it between attempts."""

        with pytest.raises(RedisConnectionError):
            await retry.call_with_retry(always_down, swallow)
        assert attempts == 3
    finally:
        await client.aclose()


async def test_create_redis_honours_configured_retry_attempts() -> None:
    client = create_redis(RedisConfig(url=_URL, retry_attempts=7))
    try:
        retry = client.get_retry()
        assert retry is not None
        assert retry.get_retries() == 7
    finally:
        await client.aclose()


async def test_retry_attempts_zero_is_a_valid_escape_hatch() -> None:
    # NonNegativeInt, not PositiveInt: 0 retries = exactly one attempt, the pre-U1
    # behaviour. An operator who wants a coordination call to fail on the first error
    # must be able to say so without editing code.
    client = create_redis(RedisConfig(url=_URL, retry_attempts=0))
    try:
        retry = client.get_retry()
        assert retry is not None
        assert retry.get_retries() == 0
    finally:
        await client.aclose()


async def test_create_redis_honours_socket_timeouts() -> None:
    config = RedisConfig(url=_URL, socket_timeout_seconds=1.5, socket_connect_timeout_seconds=0.25)
    client = create_redis(config)
    try:
        kwargs = client.get_connection_kwargs()
        assert kwargs["socket_timeout"] == 1.5
        assert kwargs["socket_connect_timeout"] == 0.25
        # Azure's load balancer idles a connection out at ~10 minutes; without this a
        # pooled connection can be handed out already dead.
        assert kwargs["health_check_interval"] == 30
    finally:
        await client.aclose()


async def test_create_redis_does_not_set_the_trap_retry_flags() -> None:
    # KD-2's two traps: `retry_on_error` WITHOUT an explicit retry degrades the policy
    # to `Retry(NoBackoff(), 1)` (one immediate hot retry), and `retry_on_timeout` is
    # deprecated since redis-py 6.0.0. Neither may reappear.
    client = create_redis(RedisConfig(url=_URL))
    try:
        kwargs = client.get_connection_kwargs()
        assert not kwargs.get("retry_on_error")
        assert "retry_on_timeout" not in kwargs
    finally:
        await client.aclose()


async def test_aclose_redis_is_a_no_op_when_never_opened() -> None:
    # Mirrors aclose_storage: safe to call on shutdown even if no pool was ever
    # opened (a dev/test boot with no REDIS__* env never opens one — D2).
    await reset_redis_for_tests()
    await aclose_redis()  # must not raise


async def test_reset_redis_for_tests_is_idempotent() -> None:
    await reset_redis_for_tests()
    await reset_redis_for_tests()  # must not raise
