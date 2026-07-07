"""In-process rate-limit substrate tests (R31).

Two layers: the `InProcessRateLimiter` fixed-window counter is unit-tested with an
injected clock (deterministic window rollover, key isolation, cap), and the
`rate_limit` FastAPI dependency is exercised over HTTP to assert the ported 429
`{"error":{"message"}}` contract and per-key enforcement.
"""

from __future__ import annotations

import httpx
from fastapi import Depends, FastAPI, Request

from src.services.ratelimit import InProcessRateLimiter, install_rate_limiting, rate_limit


class _FakeClock:
    """A driveable monotonic clock so window rollover is tested without sleeping."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


# --- InProcessRateLimiter (unit) ---------------------------------------------


def test_allows_up_to_limit_then_blocks() -> None:
    clock = _FakeClock()
    limiter = InProcessRateLimiter(limit=3, window_seconds=60, clock=clock)
    assert [limiter.hit("k") for _ in range(3)] == [True, True, True]
    assert limiter.hit("k") is False  # the 4th hit in the same window


def test_window_rollover_resets_the_counter() -> None:
    clock = _FakeClock()
    limiter = InProcessRateLimiter(limit=1, window_seconds=60, clock=clock)
    assert limiter.hit("k") is True
    assert limiter.hit("k") is False
    clock.advance(60)  # the window elapses
    assert limiter.hit("k") is True  # counter reset


def test_distinct_keys_are_isolated() -> None:
    clock = _FakeClock()
    limiter = InProcessRateLimiter(limit=1, window_seconds=60, clock=clock)
    assert limiter.hit("a") is True
    assert limiter.hit("b") is True  # b's bucket is independent of a's
    assert limiter.hit("a") is False  # a is now at its cap


# --- rate_limit dependency over HTTP -----------------------------------------

_MESSAGE = "Too many requests. Please slow down."


def _build_app(limit: int) -> FastAPI:
    app = FastAPI()
    install_rate_limiting(app)

    def _key(request: Request) -> str:
        # Key by a header so a test can exercise distinct-key isolation.
        return request.headers.get("x-bucket", "global")

    limiter = rate_limit(_key, limit=limit, window_seconds=60, message=_MESSAGE)

    @app.get("/limited", dependencies=[Depends(limiter)])
    async def _limited() -> dict[str, str]:
        return {"ok": "yes"}

    return app


async def _get(client: httpx.AsyncClient, **headers: str) -> httpx.Response:
    return await client.get("/limited", headers=headers)


async def test_http_enforces_cap_and_matches_ported_429_shape() -> None:
    transport = httpx.ASGITransport(app=_build_app(limit=2))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await _get(client)).status_code == 200
        assert (await _get(client)).status_code == 200
        blocked = await _get(client)
    assert blocked.status_code == 429
    # The ported Express contract shape: {"error": {"message": ...}}.
    assert blocked.json() == {"error": {"message": _MESSAGE}}


async def test_http_distinct_keys_isolated() -> None:
    transport = httpx.ASGITransport(app=_build_app(limit=1))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        assert (await _get(client, **{"x-bucket": "u1"})).status_code == 200
        # A different key has its own bucket — u2 is not blocked by u1's usage.
        assert (await _get(client, **{"x-bucket": "u2"})).status_code == 200
        # u1 is now at its cap.
        assert (await _get(client, **{"x-bucket": "u1"})).status_code == 429
