"""Health endpoint: happy path, fail-closed on DB down, the Redis field's three
states, and security headers.

The Redis half of this file exists to pin an asymmetry that is easy to "simplify"
away later: Postgres fails the endpoint CLOSED (503) because the API cannot work
without it, while Redis only DEGRADES the reported status at HTTP 200 because a
Redis outage breaks build sessions and nothing else. `degraded` therefore no longer
implies 503 — see the `HealthStatus` docstring.
"""

from __future__ import annotations

import pytest
from redis.exceptions import ConnectionError as RedisConnectionError

from src.db.session import get_db
from src.services.redis import client as redis_client


async def test_health_returns_not_configured_without_a_redis_fixture(client) -> None:
    """Deliberately FIXTURE-FREE (`.claude/rules/testing.md`). `fake_redis` binds the
    client singleton, so with it in place the `not_configured` branch is unreachable BY
    CONSTRUCTION and could never be tested — which is exactly how a previous incident
    hid a 503 on every deployment that had the dependency switched off.

    Redis is genuinely optional outside production, so this IS a supported deployment:
    it must report the certain answer `not_configured` and stay `ok` at HTTP 200. A dev
    box with no Redis is not a sick API.
    """
    response = await client.get("/v1/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok", "redis": "not_configured"}


async def test_health_returns_ok_when_redis_is_reachable(client, fake_redis) -> None:
    # Happy path: DB reachable (real test session via the get_db override) and a Redis
    # that answers PING. No auth required — health is public.
    response = await client.get("/v1/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "database": "ok", "redis": "ok"}


async def test_health_is_degraded_but_200_when_redis_is_unreachable(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE case this unit exists for. A Redis outage must NOT 503: build sessions are
    down, every other route is fine, and draining or restarting this instance would fix
    nothing while taking working functionality away from users. So: honest `degraded`,
    healthy HTTP 200, no restart loop.
    """

    class _PingRefuses:
        async def ping(self) -> bool:
            raise RedisConnectionError("Error 111 connecting to nope:6379. Connection refused.")

    monkeypatch.setattr(redis_client, "_redis_singleton", _PingRefuses())
    response = await client.get("/v1/health")

    assert response.status_code == 200
    assert response.json() == {"status": "degraded", "database": "ok", "redis": "unreachable"}


async def test_health_probes_redis_with_a_real_ping(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Constructing the client proves nothing — `redis.asyncio` connects lazily, which is
    # why a probe that only calls `get_redis()` reports "ok" against a dead server. Pin
    # that a command is actually issued.
    pings: list[int] = []

    class _CountingPing:
        async def ping(self) -> bool:
            pings.append(1)
            return True

    monkeypatch.setattr(redis_client, "_redis_singleton", _CountingPing())
    response = await client.get("/v1/health")

    assert response.json()["redis"] == "ok"
    assert pings == [1]


async def test_health_503_when_db_down(app, client, fake_redis) -> None:
    # A DB failure must fail the probe CLOSED (503) so a platform health gate
    # doesn't route traffic to an API that can't reach its database. Unchanged by the
    # Redis field: a healthy Redis does not rescue a dead Postgres.
    class _BoomSession:
        async def execute(self, *args: object, **kwargs: object) -> object:
            raise RuntimeError("db down")

    async def _boom_db():
        yield _BoomSession()

    app.dependency_overrides[get_db] = _boom_db
    try:
        response = await client.get("/v1/health")
    finally:
        app.dependency_overrides.pop(get_db, None)
    assert response.status_code == 503
    assert response.json() == {"status": "degraded", "database": "unreachable", "redis": "ok"}


async def test_health_sets_security_headers(client) -> None:
    # The security-headers middleware applies to every response.
    response = await client.get("/v1/health")
    assert response.headers["x-content-type-options"] == "nosniff"
    assert response.headers["x-frame-options"] == "DENY"
    assert response.headers["cache-control"] == "no-store"
