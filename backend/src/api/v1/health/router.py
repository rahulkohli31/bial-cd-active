"""Public, unauthenticated health endpoint. Probes the always-on dependency
(Postgres) and fails CLOSED (503) so a platform health gate never routes traffic
to an API that cannot reach its database. Redis is deferred this phase (ADR-0011),
so it is not probed yet.
"""

import asyncio
from typing import Literal

from fastapi import APIRouter, Response, status
from sqlalchemy import text

from src.api.deps import DbSession
from src.api.v1.health.schemas import HealthStatus

router = APIRouter(prefix="/health", tags=["health"])


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
        await asyncio.wait_for(db.execute(text("SELECT 1")), timeout=2.0)
    except Exception:
        healthy = False
        database = "unreachable"

    status_value: Literal["ok", "degraded"] = "ok"
    if not healthy:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
        status_value = "degraded"

    return HealthStatus(status=status_value, database=database)
