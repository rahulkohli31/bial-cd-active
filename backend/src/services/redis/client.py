"""Async Redis client + app-level lifecycle for the sandbox-coordination pool.

Mirrors the object-storage accessor (`services/storage/accessor.py`): `get_redis()`
reads `settings.redis`, builds ONE pooled `redis.asyncio` client, and memoises it;
`aclose_redis()` closes the pool and drops the singleton on FastAPI lifespan shutdown.
Construction is lazy and None-safe — a dev/test boot with no `REDIS__*` env never opens
a pool, and `aclose_redis()` is a no-op then (mirrors `aclose_storage`). Keys/fields
round-trip as `str` (`decode_responses=True`); the DSN is unwrapped from its `SecretStr`
only here, at the SDK boundary."""

from __future__ import annotations

import redis.asyncio as aioredis
import structlog
from redis.asyncio.retry import Retry
from redis.backoff import ExponentialWithJitterBackoff

from src.services.redis.config import RedisConfig

_log = structlog.get_logger()

_redis_singleton: aioredis.Redis | None = None
# The binary twin. A SECOND pool over the SAME instance and database — see `get_redis_bytes()`.
_redis_bytes_singleton: aioredis.Redis | None = None


class RedisNotConfiguredError(RuntimeError):
    """`get_redis()` was called but no Redis is configured (genuinely-optional in
    dev/test). Mirrors `StorageError` in `services/storage/accessor.py`: a dedicated
    type lets a caller narrow-catch the unset-Redis case instead of a bare
    `RuntimeError`."""


def create_redis(config: RedisConfig, *, decode_responses: bool = True) -> aioredis.Redis:
    """Build a pooled async Redis client; no connection opens until the first command.

    `decode_responses=False` builds the BINARY twin — see `get_redis_bytes()` for why it has to
    be a second client rather than a per-command flag.

    `retry=` is EXPLICIT and load-bearing — `from_url` skips the branch that injects
    redis-py's default retry, so an implicit client gets one attempt, no backoff (and
    `retry_on_error` alone still gives only one immediate retry). MUST be
    `redis.asyncio.retry.Retry`, never the sync `redis.retry.Retry`: the sync class's
    `call_with_retry` isn't a coroutine function, so it hands the coroutine back
    unawaited and silently retries nothing."""
    return aioredis.Redis.from_url(
        config.url.get_secret_value(),
        max_connections=config.max_connections,
        socket_timeout=config.socket_timeout_seconds,
        socket_connect_timeout=config.socket_connect_timeout_seconds,
        retry=Retry(
            ExponentialWithJitterBackoff(
                base=config.retry_backoff_base_seconds,
                cap=config.retry_backoff_cap_seconds,
            ),
            retries=config.retry_attempts,
        ),
        # Azure's load balancer silently idles a connection out at ~10 minutes; a
        # 30s health check keeps a pooled connection from being handed out dead.
        health_check_interval=30,
        decode_responses=decode_responses,
    )


def get_redis() -> aioredis.Redis:
    """The configured Redis client (app-level singleton). Raises if Redis is unset
    (genuinely-optional in dev/test; the prod gate in `src.config` requires it), so
    a caller never silently gets a None (fail-first)."""
    global _redis_singleton
    if _redis_singleton is None:
        from src.config import settings  # lazy: avoid an import cycle via src.config

        if settings.redis is None:
            raise RedisNotConfiguredError(
                "redis is not configured: set REDIS__URL, or call get_redis() only "
                "where redis is configured (it is required in production)."
            )
        _redis_singleton = create_redis(settings.redis)
    return _redis_singleton


def get_redis_bytes() -> aioredis.Redis:
    """The BINARY client — the same instance, the same database, a second pool that does not
    decode. Raises when Redis is unset, exactly as `get_redis()` does.

    WHY THIS IS A SECOND CLIENT AND NOT A FLAG ON A CALL. `decode_responses` is a property of the
    CONNECTION, applied by redis-py's parser before any command result is handed back — there is
    no per-command override. With it on, a reply is `bytes.decode("utf-8")` with `errors="strict"`,
    so a parquet file either raises `UnicodeDecodeError` or, worse for the bytes that happen to be
    valid UTF-8, comes back as a `str` that re-encodes to something else entirely. That is silent
    corruption of the one thing this feature copies verbatim.

    A LATIN-1 ROUND TRIP WOULD ALSO WORK AND IS NOT WHAT THIS DOES. `bytes.decode("latin-1")` is a
    bijection over 0–255, so it would survive — but only by making every writer and every reader
    remember to spell the same codec, forever, on a path where forgetting is undetectable until
    someone opens the file. A second pool costs one connection pool and cannot be forgotten.

    Everything ELSE in this namespace is text — locks, heartbeats, the registry hash, the lease,
    the starting marker, the taskiq stream — so the decoded client stays the default and this one
    is reached for only by the code that stores file bytes."""
    global _redis_bytes_singleton
    if _redis_bytes_singleton is None:
        from src.config import settings  # lazy: avoid an import cycle via src.config

        if settings.redis is None:
            raise RedisNotConfiguredError(
                "redis is not configured: set REDIS__URL, or call get_redis_bytes() only "
                "where redis is configured (it is required in production)."
            )
        _redis_bytes_singleton = create_redis(settings.redis, decode_responses=False)
    return _redis_bytes_singleton


async def _aclose_one(client: aioredis.Redis | None, *, during: str) -> None:
    if client is None:
        return
    try:
        await client.aclose()
    except Exception:
        _log.exception("redis teardown failed", during=during)


async def aclose_redis() -> None:
    """Close BOTH pooled clients and drop both singletons. Wired into the FastAPI
    lifespan shutdown. A no-op for whichever pool was never opened. Each close is
    isolated: if it raises we log it — never a silent swallow — but
    STILL reset the singletons, so a restart never reuses a half-closed pool."""
    global _redis_singleton, _redis_bytes_singleton
    try:
        await _aclose_one(_redis_singleton, during="aclose_redis")
        await _aclose_one(_redis_bytes_singleton, during="aclose_redis_bytes")
    finally:
        _redis_singleton = None
        _redis_bytes_singleton = None


async def reset_redis_for_tests() -> None:
    """Drop both singletons so a suite that builds clients with different configs
    never reuses a stale pool across tests."""
    global _redis_singleton, _redis_bytes_singleton
    await _aclose_one(_redis_singleton, during="reset_redis_for_tests")
    await _aclose_one(_redis_bytes_singleton, during="reset_redis_bytes_for_tests")
    _redis_singleton = None
    _redis_bytes_singleton = None
