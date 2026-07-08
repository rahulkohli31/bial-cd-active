"""Usage HTTP endpoints — the SPA's daily-cap badge read (R30).

`GET /v1/usage/today` returns the caller's used/limit/remaining and the next reset instant,
byte-matching the Express `GET /api/usage/today` contract (`server.js`) the SPA's
`fetchUsageToday` (`src/utils/usage.js`) consumes: exactly `used`, `limit`, `remaining`,
`resetsAt`. Authentication only (`current_user`) — usage is a per-user read, not gated.
The daily-limit ENFORCEMENT gate lives on the chat path (U13), not here.
"""

from __future__ import annotations

from fastapi import APIRouter
from pydantic import BaseModel, Field

from src.api.deps import CurrentUser, DbSession
from src.services.usage.gate import usage_today

router = APIRouter(prefix="/usage", tags=["usage"])


class UsageTodayResponse(BaseModel):
    """Today's token usage for the caller — the Express `/api/usage/today` shape."""

    used: int
    limit: int
    remaining: int
    # Snake-case field with a camelCase serialization alias so the wire key is
    # `resetsAt` (the SPA contract) while the Python attribute stays PEP-8 (N815).
    # FastAPI serializes response models by_alias, so the emitted key is `resetsAt`.
    resets_at: str = Field(serialization_alias="resetsAt")


@router.get("/today")
async def usage_today_endpoint(user: CurrentUser, db: DbSession) -> UsageTodayResponse:
    snapshot = await usage_today(db, user.id)
    return UsageTodayResponse(
        used=snapshot.used,
        limit=snapshot.limit,
        remaining=snapshot.remaining,
        resets_at=snapshot.resets_at,
    )
