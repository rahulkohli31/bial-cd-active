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

from src.services.redis.config import RedisConfig

_log = structlog.get_logger()

_redis_singleton: aioredis.Redis | None = None


def create_redis(config: RedisConfig) -> aioredis.Redis:
    """Build a pooled async Redis client from a `RedisConfig`. No connection is
    opened here — `redis.asyncio` connects lazily on the first command, so this is
    safe to call without a live server (tests construct + close without connecting)."""
    return aioredis.Redis.from_url(
        config.url.get_secret_value(),
        max_connections=config.max_connections,
        socket_timeout=config.socket_timeout_seconds,
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
            raise RuntimeError(
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
