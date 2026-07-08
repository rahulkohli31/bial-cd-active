"""Health domain response schema."""

from __future__ import annotations

from typing import Literal

from src.schemas import CamelModel


class HealthStatus(CamelModel):
    """The `/v1/health` body. Both fields are single-word, so the camel base is a
    no-op and the wire shape `{status, database}` is byte-identical."""

    status: Literal["ok", "degraded"]
    database: Literal["ok", "unreachable"]
