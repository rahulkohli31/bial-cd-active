"""In-process rate-limit substrate (R31).

A small fixed-window limiter mirroring the Express `express-rate-limit` wiring
(`portal/server/{feedback,attachments,app-parse}.js`): a per-key request counter
that resets every window, a 429 whose body matches the ported `{"error":{"message"}}`
contract, and the SAME ordering rule — the limiter runs AFTER the key it keys on is
resolved. `rate_limit(...)` takes the key as a FastAPI dependency, so FastAPI
resolves it (and any `current_user` / app id it needs) before the gate body runs.

Store caveat (parity with express-rate-limit's default MemoryStore): the counter
lives in THIS worker's memory, so the ceiling is PER-REPLICA. Correct under a
single replica; a multi-replica deployment needs a shared (Redis) store — deferred
(ADR-0011) until the platform scales out. `install_rate_limiting` logs this
assumption at startup.
"""

from __future__ import annotations

import time
from collections.abc import Awaitable, Callable

import structlog
from fastapi import Depends, FastAPI, Request, status
from fastapi.responses import JSONResponse

logger = structlog.get_logger()

# A key resolver: any FastAPI-compatible dependency returning the bucket key. Kept
# as a dependency (not a bare Request->str) so a user/app-scoped key can itself
# depend on current_user / app resolution, and FastAPI guarantees those run first.
KeyDependency = Callable[..., str] | Callable[..., Awaitable[str]]

# How many hits between amortized eviction sweeps of expired buckets (bounds memory
# without turning every hit into an O(n) scan).
_SWEEP_EVERY = 1024


class RateLimitExceededError(Exception):
    """Raised by a rate-limit dependency when a key exceeds its window ceiling.
    Rendered to the ported `{"error":{"message"}}` 429 by
    `rate_limit_exception_handler` (registered via `install_rate_limiting`)."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class InProcessRateLimiter:
    """A fixed-window per-key counter (parity with express-rate-limit's MemoryStore).

    Within one `window_seconds` window a key may be hit up to `limit` times; the
    `limit + 1`-th hit in the same window is denied. The window resets lazily on the
    first hit after it elapses. `clock` is injectable so tests drive window rollover
    deterministically without sleeping. Single event-loop / worker: `hit` is
    synchronous and never awaits, so no lock is needed (like Node's single thread).
    """

    def __init__(
        self,
        *,
        limit: int,
        window_seconds: float,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._limit = limit
        self._window = window_seconds
        self._clock = clock
        # key -> (window_start, count_in_window)
        self._buckets: dict[str, tuple[float, int]] = {}
        # Amortized eviction: a key never hit again would otherwise linger forever
        # (unbounded growth). Sweep expired windows every `_SWEEP_EVERY` hits rather
        # than on every call, so the hot path stays O(1) on average.
        self._hits_since_sweep = 0

    def _evict_expired(self, now: float) -> None:
        expired = [k for k, (start, _) in self._buckets.items() if now - start >= self._window]
        for k in expired:
            del self._buckets[k]

    def hit(self, key: str) -> bool:
        """Register one request for `key`; return True if allowed, False if the
        window ceiling is exceeded."""
        now = self._clock()
        self._hits_since_sweep += 1
        if self._hits_since_sweep >= _SWEEP_EVERY:
            self._hits_since_sweep = 0
            self._evict_expired(now)
        window_start, count = self._buckets.get(key, (now, 0))
        if now - window_start >= self._window:
            # The window elapsed — start a fresh one.
            window_start, count = now, 0
        count += 1
        self._buckets[key] = (window_start, count)
        return count <= self._limit


def rate_limit(
    key: KeyDependency,
    *,
    limit: int,
    window_seconds: float,
    message: str,
) -> Callable[[str], Awaitable[None]]:
    """Build a FastAPI dependency enforcing `limit` requests per `window_seconds`
    per resolved key. `key` is itself a dependency (so it can read `current_user` /
    the resolved app id); FastAPI runs it first, honoring the "limiter after key"
    order. On exceed, raises `RateLimitExceededError(message)` → a 429 `{"error":{"message"}}`.
    Each call owns its own counter, so distinct limiters (feedback vs attachment) do
    not share buckets."""
    limiter = InProcessRateLimiter(limit=limit, window_seconds=window_seconds)

    # `= Depends(key)` (not `Annotated[str, Depends(key)]`): under
    # `from __future__ import annotations` the annotation is a STRING FastAPI
    # resolves against the module globals, where the closure-local `key` is
    # invisible — so the Depends must live in the default value, not the annotation.
    async def dependency(bucket_key: str = Depends(key)) -> None:
        if not limiter.hit(bucket_key):
            raise RateLimitExceededError(message)

    return dependency


async def rate_limit_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """Render `RateLimitExceededError` as the ported 429 body `{"error":{"message":...}}`
    (the shape the SPA already receives from the Express limiters). Typed as
    `Exception` to match Starlette's handler signature; narrows before reading."""
    message = exc.message if isinstance(exc, RateLimitExceededError) else "Too many requests."
    return JSONResponse(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        content={"error": {"message": message}},
    )


def install_rate_limiting(app: FastAPI) -> None:
    """Register the 429 handler and log the single-replica assumption at startup.
    Called once from the app factory; the in-process store is per-worker, so a
    multi-replica deployment needs a shared store (Redis, deferred)."""
    app.add_exception_handler(RateLimitExceededError, rate_limit_exception_handler)
    logger.info(
        "rate_limit_store_in_process",
        detail=(
            "In-process rate-limit store: the ceiling is per-replica. A multi-replica "
            "deployment needs a shared (Redis) store — deferred (ADR-0011)."
        ),
    )
