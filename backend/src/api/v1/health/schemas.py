"""Health domain response schema."""

from __future__ import annotations

from typing import Literal

from src.schemas import CamelModel


class HealthStatus(CamelModel):
    """The `/v1/health` body: `ok` at 200, or `unavailable` at 503 when Postgres or a configured
    Redis does not answer. No dependency is named, because the endpoint is public."""

    status: Literal["ok", "unavailable"]
