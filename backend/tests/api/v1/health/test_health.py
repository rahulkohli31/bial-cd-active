"""Health endpoint: one public word, `ok` or `unavailable`, and the reason only in the log.

The endpoint is reachable without signing in, so its body must not describe the platform's
dependencies. Every assertion on a body below is an exact equality for that reason: a field that
names the database or Redis fails it.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError
from structlog.testing import capture_logs
from structlog.typing import EventDict

from src.api.v1.health import router as health_router
from src.db.session import get_db
from src.services.redis import client as redis_client


class _PingRefuses:
    async def ping(self) -> bool:
        raise RedisConnectionError("Error 111 connecting to nope:6379. Connection refused.")


class _BoomSession:
    async def execute(self, *args: object, **kwargs: object) -> object:
        raise RuntimeError("db down")


def _unreachable(logs: list[EventDict]) -> list[tuple[object, object]]:
    # The class name and nothing else: the exception's message can carry a host and port.
    return sorted(
        (e["dependency"], e["error_type"])
        for e in logs
        if e["event"] == "health_dependency_unreachable"
    )


@pytest.fixture
def db_answers_with(app) -> Iterator[Callable[[object], None]]:
    def _install(session: object) -> None:
        async def _db():
            yield session

        app.dependency_overrides[get_db] = _db

    yield _install
    app.dependency_overrides.pop(get_db, None)


async def test_health_is_ok_without_a_redis_fixture(client) -> None:
    """Binds no fixture on purpose — `fake_redis` in `tests/conftest.py` says why.

    A deployment with no Redis configured is a supported one outside production, not a sick API.
    """
    response = await client.get("/v1/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_health_is_ok_when_redis_is_reachable(client, fake_redis) -> None:
    response = await client.get("/v1/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


async def test_health_is_unavailable_when_redis_is_unreachable(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(redis_client, "_redis_singleton", _PingRefuses())

    with capture_logs() as logs:
        response = await client.get("/v1/health")

    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}
    assert _unreachable(logs) == [("redis", "ConnectionError")]


async def test_health_is_unavailable_when_db_down(client, fake_redis, db_answers_with) -> None:
    db_answers_with(_BoomSession())

    with capture_logs() as logs:
        response = await client.get("/v1/health")

    assert response.status_code == 503
    assert response.json() == {"status": "unavailable"}
    assert _unreachable(logs) == [("database", "RuntimeError")]


async def test_health_logs_every_dependency_that_is_down(
    client, monkeypatch: pytest.MonkeyPatch, db_answers_with
) -> None:
    db_answers_with(_BoomSession())
    monkeypatch.setattr(redis_client, "_redis_singleton", _PingRefuses())

    with capture_logs() as logs:
        response = await client.get("/v1/health")

    assert response.status_code == 503
    assert _unreachable(logs) == [("database", "RuntimeError"), ("redis", "ConnectionError")]


async def test_a_probe_that_hangs_is_cut_off_at_the_ceiling(
    client, monkeypatch: pytest.MonkeyPatch, db_answers_with
) -> None:
    class _HangingSession:
        async def execute(self, *args: object, **kwargs: object) -> object:
            await asyncio.sleep(30)
            return None

    class _HangingPing:
        async def ping(self) -> bool:
            await asyncio.sleep(30)
            return True

    monkeypatch.setattr(health_router, "_PROBE_TIMEOUT_SECONDS", 0.05)
    db_answers_with(_HangingSession())
    monkeypatch.setattr(redis_client, "_redis_singleton", _HangingPing())

    with capture_logs() as logs:
        response = await asyncio.wait_for(client.get("/v1/health"), timeout=5)

    assert response.status_code == 503
    assert _unreachable(logs) == [("database", "TimeoutError"), ("redis", "TimeoutError")]


async def test_health_sets_security_headers(client) -> None:
    response = await client.get("/v1/health")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["cache-control"] == "no-store"
