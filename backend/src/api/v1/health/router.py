"""Public, unauthenticated health endpoint: `ok`, or `unavailable` at 503 when Postgres or a
configured Redis does not answer. The body names no dependency; the server log does."""

import asyncio
from collections.abc import Awaitable
from typing import Final

import structlog
from fastapi import APIRouter, Response, status
from sqlalchemy import text

from src.api.deps import DbSession
from src.api.v1.health.schemas import HealthStatus
from src.services.redis import RedisNotConfiguredError, get_redis

router = APIRouter(prefix="/health", tags=["health"])

logger = structlog.get_logger()

# Per-dependency ceiling. A health gate polls this endpoint on a short interval, so
# neither probe may outlive one poll. It also has to sit UNDER Redis's own retry
# budget (4 attempts x 2s socket timeouts + backoff ≈ 16.7s worst case), because
# that budget is a sum rather than a deadline and would otherwise set the bound.
_PROBE_TIMEOUT_SECONDS: Final = 2.0


async def _answers(dependency: str, probe: Awaitable[object]) -> bool:
    # The broad catch converts any failure into "unavailable"; the log keeps the cause.
    try:
        await asyncio.wait_for(probe, timeout=_PROBE_TIMEOUT_SECONDS)
    except Exception as exc:
        logger.warning(
            "health_dependency_unreachable", dependency=dependency, error_type=type(exc).__name__
        )
        return False
    return True


@router.get(
    "",
    responses={503: {"model": HealthStatus, "description": "The service is unavailable"}},
)
async def health_check(response: Response, db: DbSession) -> HealthStatus:
    try:
        redis = get_redis()
    except RedisNotConfiguredError:
        redis = None  # Supported outside production; production refuses to start without Redis.
    probes = [_answers("database", db.execute(text("SELECT 1")))]
    if redis is not None:
        # A PING, because the client connects lazily and constructing it proves nothing.
        probes.append(_answers("redis", redis.ping()))
    # Every probe runs, so the log names each dependency that is down.
    if all(await asyncio.gather(*probes)):
        return HealthStatus(status="ok")
    response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return HealthStatus(status="unavailable")
