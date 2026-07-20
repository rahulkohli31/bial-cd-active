"""FastAPI lifespan: None-safe startup in dev (no REDIS__*/SANDBOX__* env — D2), and
it opens the app-global Redis coordination pool when configured. Shutdown acloses the
Redis pool + the sandbox client + the object store, each a no-op when never opened."""

from __future__ import annotations

import pytest
from pydantic import SecretStr

from src.config import settings
from src.main import create_app, lifespan
from src.services.redis import client as redis_client
from src.services.redis.config import RedisConfig


async def test_lifespan_boots_and_shuts_down_with_no_coordination_config() -> None:
    # Dev/test: settings.redis is None → startup opens no pool; shutdown acloses
    # redis/sandbox/storage, each a no-op when never used. Must not raise (D2).
    assert settings.redis is None
    await redis_client.reset_redis_for_tests()
    app = create_app()
    async with lifespan(app):
        # Startup opened nothing — no REDIS__* env, so the pool stays closed.
        assert redis_client._redis_singleton is None
    # Shutdown ran cleanly and left the pool closed.
    assert redis_client._redis_singleton is None


async def test_lifespan_opens_and_closes_redis_pool_when_configured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await redis_client.reset_redis_for_tests()
    monkeypatch.setattr(settings, "redis", RedisConfig(url=SecretStr("redis://localhost:6379/0")))
    app = create_app()
    try:
        async with lifespan(app):
            # Startup opened the app-global coordination pool...
            assert redis_client._redis_singleton is not None
        # ...and shutdown closed it (aclose_redis reset the singleton).
        assert redis_client._redis_singleton is None
    finally:
        await redis_client.reset_redis_for_tests()
