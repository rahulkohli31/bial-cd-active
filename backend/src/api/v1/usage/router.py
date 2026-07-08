"""Usage HTTP endpoints — the SPA's daily-cap badge read (R30).

`GET /v1/usage/today` returns the caller's used/limit/remaining and the next reset instant,
byte-matching the Express `GET /api/usage/today` contract (`server.js`) the SPA's
`fetchUsageToday` (`src/utils/usage.js`) consumes: exactly `used`, `limit`, `remaining`,
`resetsAt`. Authentication only (`current_user`) — usage is a per-user read, not gated.
The daily-limit ENFORCEMENT gate lives on the chat path (U13), not here.
"""

from __future__ import annotations

from fastapi import APIRouter

from src.api.deps import CurrentUser, DbSession
from src.api.v1.usage.schemas import UsageTodayResponse
from src.schemas import DetailBody, error_responses
from src.services.usage.gate import usage_today

router = APIRouter(prefix="/usage", tags=["usage"])


@router.get(
    "/today",
    # 401 originates in the `current_user` dependency (bare HTTPException ->
    # `{"detail": ...}`), so it is documented here even though the raise is external.
    responses=error_responses((401, DetailBody, "Not authenticated")),
)
async def usage_today_endpoint(user: CurrentUser, db: DbSession) -> UsageTodayResponse:
    snapshot = await usage_today(db, user.id)
    return UsageTodayResponse(
        used=snapshot.used,
        limit=snapshot.limit,
        remaining=snapshot.remaining,
        resets_at=snapshot.resets_at,
    )
