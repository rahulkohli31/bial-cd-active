"""FastAPI lifespan: None-safe startup in dev (no REDIS__*/SANDBOX__* env — D2), and
it opens AND PROBES the app-global Redis coordination pool when configured. Shutdown
acloses the Redis pool + the sandbox client + the object store, each a no-op when never
opened.

The probe's whole contract is that it is LOUD but HARMLESS: it must surface a
misconfigured Redis to an operator at deploy time, and it must never take the boot
down with it (a raising probe = a container restart loop over a dependency most
routes do not need). Everything below pins one half of that.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from redis.exceptions import ConnectionError as RedisConnectionError
from structlog.testing import capture_logs

from src.config import settings
from src.main import (
    REDIS_PROBE_CEILING_SECONDS,
    REDIS_PROBE_FAILED_EVENT,
    REDIS_PROBE_OK_EVENT,
    create_app,
    lifespan,
)
from src.services.redis import client as redis_client
from src.services.redis.config import RedisConfig


class _PingRaises:
    """A stand-in client whose PING fails the way an unreachable Redis does."""

    def __init__(self, exc: BaseException) -> None:
        self._exc = exc
        self.pings = 0

    async def ping(self) -> bool:
        self.pings += 1
        raise self._exc


class _PingHangs:
    """A stand-in client whose PING never returns — the black-holed-host shape. Only the
    outer `asyncio.wait_for` ceiling can end this; the retry policy's budget is a SUM of
    per-attempt timeouts, so it would never fire on a socket that simply never answers."""

    def __init__(self) -> None:
        self.pings = 0

    async def ping(self) -> bool:
        self.pings += 1
        await asyncio.Event().wait()
        return True


@pytest.fixture
def redis_configured(monkeypatch: pytest.MonkeyPatch):
    # The probe is gated on `settings.redis is not None`, and the suite deliberately runs
    # with Redis unconfigured (see the first test below), so every probe test has to opt
    # in explicitly — otherwise the branch is unreachable BY CONSTRUCTION and the test
    # proves nothing (`.claude/rules/testing.md`).
    monkeypatch.setattr(settings, "redis", RedisConfig(url=SecretStr("redis://localhost:6379/0")))


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
    # `retry_attempts=0` only so this test does not spend the retry budget dialling a
    # localhost Redis that may or may not exist on the machine running the suite. What
    # it pins is the POOL lifecycle; the probe's own behaviour is pinned below against
    # stand-in clients, with no dependence on the environment.
    monkeypatch.setattr(
        settings,
        "redis",
        RedisConfig(url=SecretStr("redis://localhost:6379/0"), retry_attempts=0),
    )
    app = create_app()
    try:
        async with lifespan(app):
            # Startup opened the app-global coordination pool...
            assert redis_client._redis_singleton is not None
        # ...and shutdown closed it (aclose_redis reset the singleton).
        assert redis_client._redis_singleton is None
    finally:
        await redis_client.reset_redis_for_tests()


async def test_startup_probe_logs_a_distinguishable_event_and_still_serves(
    monkeypatch: pytest.MonkeyPatch, redis_configured
) -> None:
    """Covers AE3. A wrong DSN must reach an OPERATOR at deploy time — not the first
    citizen developer whose build fails — WITHOUT taking the boot down: the API still
    completes its lifespan and answers requests on every route that needs no Redis.
    """
    monkeypatch.setattr(
        "src.main.get_redis",
        lambda: _PingRaises(RedisConnectionError("Error 111 connecting to nope:6379.")),
    )
    app = create_app()
    with capture_logs() as logs:
        async with lifespan(app):
            # The lifespan reached its yield despite the failed probe: boot is not blocked.
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as c:
                # A route that needs neither Redis nor a DB session — the assertion is
                # that the app ANSWERS at all, which a boot-blocking probe would prevent.
                served = await c.get("/openapi.json")

    failures = [entry for entry in logs if entry["event"] == REDIS_PROBE_FAILED_EVENT]
    assert len(failures) == 1
    assert failures[0]["log_level"] == "warning"
    # The event NAME is what an alert rule greps for, so failure must be distinguishable
    # from success rather than one generic "redis" line covering both. (The constants
    # being unequal is checked by mypy, not by an assert — a literal-vs-literal compare
    # is a non-overlapping equality check and therefore a vacuous assertion.)
    assert not [entry for entry in logs if entry["event"] == REDIS_PROBE_OK_EVENT]
    assert failures[0]["error_type"] == "ConnectionError"
    assert served.status_code == 200


async def test_startup_probe_logs_success_when_redis_answers(
    monkeypatch: pytest.MonkeyPatch, redis_configured
) -> None:
    # The mirror image, so the failure test above can actually fail: a healthy Redis
    # logs the OK event and never the warning.
    class _PingOk:
        async def ping(self) -> bool:
            return True

    monkeypatch.setattr("src.main.get_redis", _PingOk)
    app = create_app()
    with capture_logs() as logs:
        async with lifespan(app):
            pass

    assert [entry["event"] for entry in logs if entry["event"] == REDIS_PROBE_OK_EVENT] == [
        REDIS_PROBE_OK_EVENT
    ]
    assert not [entry for entry in logs if entry["event"] == REDIS_PROBE_FAILED_EVENT]


@pytest.mark.parametrize(
    "boom",
    [
        RedisConnectionError("refused"),
        TimeoutError("hung"),
        OSError("dns went sideways"),
        RuntimeError("something nobody predicted"),
    ],
)
async def test_startup_probe_never_raises_out_of_the_lifespan(
    monkeypatch: pytest.MonkeyPatch, redis_configured, boom: BaseException
) -> None:
    """WHATEVER the client raises, the lifespan completes. This is parametrized past the
    obvious Redis errors on purpose: the probe touches a third-party client during boot,
    and a probe that re-raises anything at all converts a Redis wobble into a container
    restart loop — the exact failure mode the non-blocking design exists to prevent."""
    monkeypatch.setattr("src.main.get_redis", lambda: _PingRaises(boom))
    app = create_app()
    async with lifespan(app):
        pass


async def test_startup_probe_is_bounded_by_the_ceiling(
    monkeypatch: pytest.MonkeyPatch, redis_configured
) -> None:
    """The black-holed-host case, and the reason the outer `wait_for` is not decorative.

    A hung server answers nothing, so the retry policy's per-attempt socket timeouts
    would each have to expire in turn — a SUM (≈16.7s), not a deadline. Only the outer
    ceiling turns that into a bound. Elapsed time is asserted against the CONSTANT, not
    a hand-typed number, so raising the ceiling cannot silently invalidate this test.
    """
    hangs = _PingHangs()
    monkeypatch.setattr("src.main.get_redis", lambda: hangs)
    app = create_app()

    started = time.perf_counter()
    with capture_logs() as logs:
        # No TimeoutError escapes: `asyncio.wait_for` raises it, and the probe converts
        # it into the warn event like any other failure.
        async with lifespan(app):
            pass
    elapsed = time.perf_counter() - started

    assert hangs.pings == 1
    assert elapsed < REDIS_PROBE_CEILING_SECONDS + 1.0
    assert elapsed >= REDIS_PROBE_CEILING_SECONDS
    failures = [entry for entry in logs if entry["event"] == REDIS_PROBE_FAILED_EVENT]
    assert len(failures) == 1
    assert failures[0]["error_type"] == "TimeoutError"


async def test_startup_probe_runs_against_the_shared_singleton(
    monkeypatch: pytest.MonkeyPatch, redis_configured
) -> None:
    """Pins that the probe validates the pool REAL CALLERS use, rather than a private
    client of its own — a throwaway probe client would prove a connection nothing else
    ever opens, and `aclose_redis()` tracks only the singleton, so it would also leak
    teardown surface.

    Deliberately NOT asserted here: retry behaviour by counting `.ping()` calls. In
    pinned redis 8.0.1 the retry loop lives at the CONNECTION layer
    (`conn.retry.call_with_retry`), below the public command API, so a stub whose
    `ping()` raises is called exactly once under any `Retry` and such a count would pass
    vacuously. Retry policy is pinned on `client.get_retry()` in the U1 client tests.
    """
    calls: list[str] = []
    ok = _PingRaises(RedisConnectionError("refused"))

    def _spy_get_redis() -> Any:
        calls.append("get_redis")
        return ok

    monkeypatch.setattr("src.main.get_redis", _spy_get_redis)
    app = create_app()
    async with lifespan(app):
        pass

    assert calls == ["get_redis"]
    assert ok.pings == 1


async def test_startup_probe_is_skipped_when_redis_is_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The suite's own baseline (`settings.redis is None`) is a SUPPORTED deployment, not
    # an oversight: probing it would construct a client that `get_redis()` correctly
    # refuses to build, and log a scary warning about a dependency nobody asked for.
    assert settings.redis is None

    def _must_not_be_called() -> Any:  # pragma: no cover - the assertion is that it isn't
        raise AssertionError("the probe must not touch Redis when none is configured")

    monkeypatch.setattr("src.main.get_redis", _must_not_be_called)
    app = create_app()
    with capture_logs() as logs:
        async with lifespan(app):
            pass
    assert not [
        entry
        for entry in logs
        if entry["event"] in (REDIS_PROBE_OK_EVENT, REDIS_PROBE_FAILED_EVENT)
    ]
