"""Pool-factory + lifecycle tests for the sandbox-coordination Redis client.

`redis.asyncio` connects lazily, so these construct and close a client WITHOUT a
live Redis server — they exercise the factory + the idempotent teardown, not I/O.
"""

from __future__ import annotations

import redis.asyncio as aioredis
from pydantic import SecretStr

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


async def test_aclose_redis_is_a_no_op_when_never_opened() -> None:
    # Mirrors aclose_storage: safe to call on shutdown even if no pool was ever
    # opened (a dev/test boot with no REDIS__* env never opens one — D2).
    await reset_redis_for_tests()
    await aclose_redis()  # must not raise


async def test_reset_redis_for_tests_is_idempotent() -> None:
    await reset_redis_for_tests()
    await reset_redis_for_tests()  # must not raise
