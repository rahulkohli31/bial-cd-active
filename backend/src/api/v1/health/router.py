"""Public, unauthenticated health endpoint.

Probes the always-on dependency (Postgres) and fails CLOSED (503) so a platform
health gate never routes traffic to an API that cannot reach its database.

It ALSO probes Redis — the build-session coordination store (C5) — but Redis does
NOT fail the endpoint closed. Redis is genuinely optional outside production and,
when it is down, only build sessions are affected while every other route keeps
working; draining the instance would remove working functionality without fixing
anything. So a Redis fault reports `status: "degraded"` at **HTTP 200**. See
`HealthStatus` for the full semantics and the "never key on the word, key on the
code" warning that follows from it.
"""

import asyncio
from typing import Final, Literal

from fastapi import APIRouter, Response, status
from sqlalchemy import text

from src.api.deps import DbSession
from src.api.v1.health.schemas import HealthStatus
from src.services.redis import RedisNotConfiguredError, get_redis

router = APIRouter(prefix="/health", tags=["health"])

# Per-dependency ceiling. A health gate polls this endpoint on a short interval, so
# neither probe may outlive one poll. It also has to sit UNDER Redis's own retry
# budget (4 attempts x 2s socket timeouts + backoff ≈ 16.7s worst case), because
# that budget is a sum rather than a deadline and would otherwise set the bound.
_PROBE_TIMEOUT_SECONDS: Final = 2.0


@router.get(
    "",
    responses={503: {"model": HealthStatus, "description": "A dependency is unreachable"}},
)
async def health_check(response: Response, db: DbSession) -> HealthStatus:
    # Time-bounded so a black-holed DB can't hang the health gate. The broad catch
    # is deliberate: any failure means "unreachable", surfaced as a 503 — this
    # converts the error into a status, it does not swallow it.
    database: Literal["ok", "unreachable"] = "ok"
    healthy = True
    try:
        await asyncio.wait_for(db.execute(text("SELECT 1")), timeout=_PROBE_TIMEOUT_SECONDS)
    except Exception:
        healthy = False
        database = "unreachable"

    # Same shape, one extra state. `get_redis()` raising `RedisNotConfiguredError` is a
    # CERTAIN answer ("this deployment has no Redis"), not an ambiguous one, so it is
    # reported as its own word and leaves the overall status `ok` — collapsing it into
    # `unreachable` would report every Redis-off dev box as broken. Only a configured
    # Redis that fails to answer a PING is `unreachable`. The PING is required: the
    # client connects lazily, so constructing it proves nothing.
    redis_state: Literal["ok", "unreachable", "not_configured"]
    try:
        client = get_redis()
    except RedisNotConfiguredError:
        redis_state = "not_configured"
    else:
        redis_state = "ok"
        try:
            await asyncio.wait_for(client.ping(), timeout=_PROBE_TIMEOUT_SECONDS)
        except Exception:
            redis_state = "unreachable"

    # Only Postgres fails the endpoint CLOSED. Redis degrades the reported status
    # without touching the code — see the `HealthStatus` docstring.
    status_value: Literal["ok", "degraded"] = "ok"
    if not healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        status_value = "degraded"
    elif redis_state == "unreachable":
        status_value = "degraded"

    return HealthStatus(status=status_value, database=database, redis=redis_state)
