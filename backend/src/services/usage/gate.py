"""Server-authoritative daily-token gate + accounting (R13, R30).

The single source of truth the SPA cannot bypass. Three responsibilities:

* `enforce_daily_limit` — pre-request check: raise `DailyTokenLimitExceededError` (rendered as
  the byte-stable 429 `daily_token_limit_exceeded`) BEFORE any stream byte when today's
  `used` is at/over the effective cap. `used >= limit`, matching Express's `>=`.
* `record_usage` — post-response atomic upsert (`INSERT … ON CONFLICT … DO UPDATE` with the
  add in SQL) so concurrent increments never lose an update. It does NOT close concurrent
  overspend — that window is open by design (Redis token-bucket hardening deferred).
* `usage_today` — the read behind `GET /v1/usage/today` (used/limit/remaining/resetsAt).

IST day math (`Asia/Kolkata`, a fixed +05:30 with no DST) mirrors `server/usage-repo.js`:
the day key is the IST calendar date, and the reset is the next IST midnight rendered as a
UTC ISO string. `used` is `input_tokens + output_tokens` ONLY (`billable_spend`): under
pydantic-ai `input_tokens` is the GRAND-TOTAL prompt size with the two cache classes already
folded in (`cache_read`/`cache_write` are sub-buckets INSIDE it, not additive siblings — the
opposite of the raw Anthropic API, whose `input_tokens` is exclusive of cache). Re-adding the
cache columns would count the cached prefix twice — the ~2x inflation the port from Express
carried in. The cache columns stay in the ledger for cost/analytics only.
"""

from __future__ import annotations

import datetime
import uuid
from dataclasses import dataclass

import sqlalchemy as sa
from fastapi import status
from fastapi.responses import JSONResponse
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings
from src.db.models.token_usage import TokenUsage
from src.db.models.user_limit import UserLimit

# IST is a fixed offset with no daylight saving, so a constant tzinfo is always correct.
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

# The stable machine code the SPA's interceptor keys on (useClaudeAPI.js) — byte-stable.
DAILY_LIMIT_EXCEEDED_CODE = "daily_token_limit_exceeded"


class DailyTokenLimitExceededError(Exception):
    """Raised by `enforce_daily_limit` when today's usage is at/over the cap. Carries the
    numbers the SPA needs and renders the EXACT Express 429 body via `as_response`."""

    def __init__(self, *, limit: int, used: int) -> None:
        super().__init__("daily token limit exceeded")
        self.limit = limit
        self.used = used

    def as_response(self) -> JSONResponse:
        # Byte-stable with Express `server.js` (message uses en-US thousands grouping via
        # `{:,}`; code/limit/used/remaining keys are what the SPA reads).
        return JSONResponse(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            content={
                "error": {
                    "message": (
                        f"You've reached your daily limit of {self.limit:,} tokens. "
                        "It resets at midnight IST. If you need a higher limit, please "
                        "contact your administrator to enable a higher plan."
                    ),
                    "code": DAILY_LIMIT_EXCEEDED_CODE,
                    "limit": self.limit,
                    "used": self.used,
                    "remaining": 0,
                }
            },
        )


@dataclass(frozen=True)
class UsageSnapshot:
    """Today's usage for one user — the shape behind `GET /v1/usage/today`."""

    used: int
    limit: int
    remaining: int
    resets_at: str


def ist_today(now: datetime.datetime | None = None) -> datetime.date:
    """The IST calendar date for `now` (defaults to the current instant)."""
    moment = now if now is not None else datetime.datetime.now(datetime.UTC)
    return moment.astimezone(IST).date()


def next_ist_midnight_iso(now: datetime.datetime | None = None) -> str:
    """The next IST-midnight after `now`, as a UTC ISO-8601 string (e.g.
    `2026-07-06T18:30:00.000Z` = 00:00 IST). Matches Express `nextIstMidnightIso`: anchor
    at IST midnight, convert to UTC. IST midnight is always :30:00 UTC with zero ms, so the
    `.000Z` suffix is emitted verbatim to byte-match JS `Date.toISOString()`."""
    day = ist_today(now)
    next_midnight_ist = datetime.datetime(
        day.year, day.month, day.day, tzinfo=IST
    ) + datetime.timedelta(days=1)
    utc = next_midnight_ist.astimezone(datetime.UTC)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.000Z")


def resolve_daily_limit(daily_override: int | None) -> int:
    """Pure resolver for the daily cap from an already-loaded override value: the override
    if positive, else the global default (no DB). Lets a caller that has already fetched the
    override map resolve without a per-user re-query (avoids the admin list N+1)."""
    if daily_override is not None and daily_override > 0:
        return daily_override
    return settings.DAILY_TOKEN_LIMIT


async def effective_daily_limit(db: AsyncSession, user_id: uuid.UUID) -> int:
    """The user's effective daily cap: their override if set to a positive value, else the
    global default. Mirrors `resolveUserLimits` (a non-positive/absent override → default)."""
    override = await db.scalar(
        sa.select(UserLimit.daily_token_limit).where(UserLimit.user_id == user_id)
    )
    return resolve_daily_limit(override)


def billable_spend() -> sa.ColumnElement[int]:
    """The daily billable token total as a column expression: `input + output`, nothing else.

    THE single source of truth both readers share (`_used_today` here and the admin roster in
    `api/v1/admin/router.py`), so a fix can never half-land with one reader still folding cache.
    Under pydantic-ai `input_tokens` is the grand-total prompt size — `cache_read`/`cache_write`
    are sub-buckets ALREADY inside it, not additive siblings — so adding the cache columns back
    would double-count the cached prefix (the ~2x inflation F0 caught). The cache columns stay
    split for cost/analytics; they simply never re-enter the cap total.
    """
    return TokenUsage.input_tokens + TokenUsage.output_tokens


async def _used_today(db: AsyncSession, user_id: uuid.UUID, day: datetime.date) -> int:
    """Today's billable token total for the user (0 when no row yet): `input + output`, via the
    shared `billable_spend` expression so it can never drift from the admin roster's number."""
    total = await db.scalar(
        sa.select(billable_spend()).where(
            TokenUsage.user_id == user_id, TokenUsage.usage_date == day
        )
    )
    # `db.scalar` is typed Any and returns None when no row exists; pin a concrete int.
    return int(total) if total is not None else 0


async def usage_today(db: AsyncSession, user_id: uuid.UUID) -> UsageSnapshot:
    """Read used/limit/remaining/resetsAt for the caller (no mutation)."""
    day = ist_today()
    used = await _used_today(db, user_id, day)
    limit = await effective_daily_limit(db, user_id)
    return UsageSnapshot(
        used=used,
        limit=limit,
        remaining=max(0, limit - used),
        resets_at=next_ist_midnight_iso(),
    )


async def enforce_daily_limit(db: AsyncSession, user_id: uuid.UUID) -> None:
    """Raise `DailyTokenLimitExceededError` when today's usage is at/over the effective cap.
    Called BEFORE any stream byte (U13). `used >= limit` matches Express's at-or-over gate."""
    day = ist_today()
    used = await _used_today(db, user_id, day)
    limit = await effective_daily_limit(db, user_id)
    if used >= limit:
        raise DailyTokenLimitExceededError(limit=limit, used=used)


async def record_usage(
    db: AsyncSession,
    user_id: uuid.UUID,
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> None:
    """Atomically fold a turn's token spend into today's row (add in SQL, no lost update).
    Does NOT commit — the caller owns the transaction so the turn-persist and the usage
    write commit together (U13). Parity with Express's atomic `$inc`."""
    day = ist_today()
    stmt = (
        pg_insert(TokenUsage)
        .values(
            user_id=user_id,
            usage_date=day,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
        )
        .on_conflict_do_update(
            index_elements=[TokenUsage.user_id, TokenUsage.usage_date],
            set_={
                "input_tokens": TokenUsage.input_tokens + input_tokens,
                "output_tokens": TokenUsage.output_tokens + output_tokens,
                "cache_read_tokens": TokenUsage.cache_read_tokens + cache_read_tokens,
                "cache_write_tokens": TokenUsage.cache_write_tokens + cache_write_tokens,
                "updated_at": sa.func.now(),
            },
        )
    )
    await db.execute(stmt)
