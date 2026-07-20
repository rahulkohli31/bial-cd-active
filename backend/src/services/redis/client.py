"""Async Redis client + app-level lifecycle for the sandbox-coordination pool.

Mirrors the object-storage accessor (`services/storage/accessor.py`): `get_redis()`
reads `settings.redis`, builds ONE pooled `redis.asyncio` client, and memoises it;
`aclose_redis()` closes the pool and drops the singleton on FastAPI lifespan
shutdown (U9). Construction is lazy and None-safe — a dev/test boot with no
`REDIS__*` env never opens a pool (D2), and `aclose_redis()` is a no-op when the
pool was never used (mirrors `aclose_storage`).

`decode_responses=True` so keys and hash fields round-trip as `str` (the C5
builders emit `str`); the DSN is unwrapped from its `SecretStr` only here, at the
SDK boundary (security.md).
"""

from __future__ import annotations

import redis.asyncio as aioredis
import structlog
from redis.backoff import ExponentialWithJitterBackoff
from redis.retry import Retry

from src.services.redis.config import RedisConfig

_log = structlog.get_logger()

_redis_singleton: aioredis.Redis | None = None


class RedisNotConfiguredError(RuntimeError):
    """`get_redis()` was called but no Redis is configured (genuinely-optional in
    dev/test). Mirrors `StorageError` in `services/storage/accessor.py`: a dedicated
    type lets a caller narrow-catch the unset-Redis case instead of a bare
    `RuntimeError`."""


def create_redis(config: RedisConfig) -> aioredis.Redis:
    """Build a pooled async Redis client from a `RedisConfig`. No connection is
    opened here — `redis.asyncio` connects lazily on the first command, so this is
    safe to call without a live server (tests construct + close without connecting).

    The `retry=` is EXPLICIT and load-bearing. `from_url` always constructs a
    connection pool, which skips the branch of `Redis.__init__` that injects
    redis-py's advertised default retry, so every client built here would otherwise
    run `Retry(NoBackoff(), 0)` — a single attempt with no backoff. Two traps this
    deliberately avoids: `retry_on_error` WITHOUT an explicit `retry` yields
    `Retry(NoBackoff(), 1)` (one immediate hot retry, no delay), and
    `retry_on_timeout` is deprecated since redis-py 6.0.0 — neither is passed.
    `BusyLoadingError` subclasses `ConnectionError`, so it is already covered by
    `Retry`'s default supported errors and needs no listing.

    TLS is carried by the DSN scheme (`rediss://`), never by kwargs here, so no TLS
    setting differs between environments — the production gate in `src.config`
    enforces the scheme. redis-py's verification defaults are already correct for
    Azure Cache (CERT_REQUIRED, hostname check, system CA bundle)."""
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
        decode_responses=True,
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


async def aclose_redis() -> None:
    """Close the pooled client and drop the singleton. Wired into the FastAPI
    lifespan shutdown (U9). A no-op when the pool was never opened. The close is
    isolated: if it raises we log it (fail-first.md — never a silent swallow) but
    STILL reset the singleton, so a restart never reuses a half-closed pool."""
    global _redis_singleton
    if _redis_singleton is None:
        return
    try:
        await _redis_singleton.aclose()
    except Exception:
        _log.exception("redis teardown failed during aclose_redis")
    finally:
        _redis_singleton = None


async def reset_redis_for_tests() -> None:
    """Drop the singleton so a suite that builds clients with different configs
    never reuses a stale pool across tests."""
    global _redis_singleton
    if _redis_singleton is not None:
        try:
            await _redis_singleton.aclose()
        except Exception:
            _log.exception("redis teardown failed during reset_redis_for_tests")
    _redis_singleton = None
