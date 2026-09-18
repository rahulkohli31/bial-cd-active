"""Server-authoritative daily-token gate + accounting.

The single source of truth the SPA cannot bypass. Three responsibilities:

* `enforce_daily_limit` — pre-request check: raise `DailyTokenLimitExceededError` (429
  `daily_token_limit_exceeded`) BEFORE any stream byte when `used >= limit` (Express parity).
* `record_usage` — post-response atomic upsert (`INSERT … ON CONFLICT … DO UPDATE`), so
  concurrent increments never lose an update. Does NOT close concurrent overspend — that
  window is open by design (Redis token-bucket hardening deferred).
* `usage_today` — the read behind `GET /v1/usage/today`.

IST day math (`Asia/Kolkata`, fixed +05:30, no DST) mirrors Express `server/usage-repo.js`: the day
key is the IST calendar date, reset is the next IST midnight as a UTC ISO string. `used`
counts `build` rows ONLY — the pre-publish classification review is metered under `review`
for attribution, never against the citizen's cap.

WHY THIS EXISTS: `used` is the COST-WEIGHTED spend (`billable_spend`), not raw tokens.
Under pydantic-ai, `input_tokens` is the grand-total prompt size with `cache_read`/
`cache_write` already folded in as sub-buckets (unlike the raw Anthropic API, where
`input_tokens` excludes cache) — so fresh = input minus both cache classes. Two wrong turns
are pinned here, the second one live in prod: re-ADDING the cache columns double-counts the
prefix (~2x, the Express port's bug), and billing cache reads at face value let one agentic
build book ~956k of a 1M cap on 68 fresh tokens (2026-07-30). The raw four-class ledger
stays untouched — weighting is read-side policy."""

from __future__ import annotations

import datetime
import uuid
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

import sqlalchemy as sa
from fastapi import status
from fastapi.responses import JSONResponse
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings
from src.db.models.token_usage import TokenUsage, TokenUsageKind
from src.db.models.user_limit import UserLimit

# IST is a fixed offset with no daylight saving, so a constant tzinfo is always correct.
IST = datetime.timezone(datetime.timedelta(hours=5, minutes=30))

# The stable machine code the SPA's interceptor keys on — byte-stable.
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


# Cost weights for the two cache classes, matching the Anthropic pricing shape. A cache READ
# costs ~10% of a fresh input token at either TTL tier; a cache WRITE is priced BY THE TIER the
# breakpoint bought, so its weight is looked up from the tier rather than baked into a number.
# SQLAlchemy compiles `/` as true division (`/ CAST(10 AS NUMERIC)`) and `Decimal` params bind as
# NUMERIC, so the arithmetic stays exact and the single outer BIGINT cast rounds ONCE at the end
# — the whole day's total is exact-weighted to within half a token.
_CACHE_READ_DIVISOR = 10  # read ≈ fresh / 10

_CACHE_WRITE_MULTIPLIER_BY_TTL: Final[dict[str, Decimal]] = {
    "5m": Decimal("1.25"),
    "1h": Decimal("2"),
}

# The tier every breakpoint in the platform buys. Restated rather than imported from
# `orchestrator.constants`, which cannot be reached from a service without dragging the API layer
# in behind it (`orchestrator/progress.py`); `tests/services/usage/test_gate.py` pins this against
# the TTL constants that actually set the breakpoints. A `TokenUsage` row carries no per-row TTL,
# so one weight is only correct while the whole codebase buys one tier — which that test also pins.
_BILLED_CACHE_TIER: Final = "1h"
_CACHE_WRITE_MULTIPLIER: Final = _CACHE_WRITE_MULTIPLIER_BY_TTL[_BILLED_CACHE_TIER]


def weighted_spend(
    *,
    input_tokens: int,
    output_tokens: int,
    cache_read_tokens: int,
    cache_write_tokens: int,
) -> int:
    """`billable_spend`'s weighting, applied to an in-memory usage object rather than a row.

    THE SECOND READER OF ONE POLICY, NOT A SECOND POLICY: `billable_spend` is a SQL column
    expression and cannot evaluate against a live `RunUsage`. Both spell the same arithmetic
    from the same two weights, and the test suite pins them to agree — a per-run bound
    weighting differently from the daily meter would mean two ceilings measuring two
    different things while both are described to the citizen as spend. See the module
    docstring's WHY THIS EXISTS for why weighting matters at all."""
    fresh = max(input_tokens - cache_read_tokens - cache_write_tokens, 0)
    return int(
        fresh
        + output_tokens
        + Decimal(cache_read_tokens) / _CACHE_READ_DIVISOR
        + cache_write_tokens * _CACHE_WRITE_MULTIPLIER
    )


def billable_spend() -> sa.ColumnElement[int]:
    """The daily billable token total as a column expression, COST-WEIGHTED per token class:
    `fresh_input + output + cache_read/10 + cache_write × the tier's write weight` (2× while
    `_BILLED_CACHE_TIER` is the 1-hour one).

    THE single source of truth both readers share (`_used_today` here and the admin roster in
    `api/v1/admin/router.py`), so a fix can never half-land with one reader still folding cache.
    `fresh` is input minus both cache classes (clamped at 0 against malformed rows) — see the
    module docstring's WHY THIS EXISTS for why weighting matters. The raw four-class ledger
    is untouched; weighting is read-side policy, so it corrects history too."""
    fresh = sa.func.greatest(
        TokenUsage.input_tokens - TokenUsage.cache_read_tokens - TokenUsage.cache_write_tokens,
        0,
    )
    return sa.cast(
        fresh
        + TokenUsage.output_tokens
        + TokenUsage.cache_read_tokens / _CACHE_READ_DIVISOR
        + TokenUsage.cache_write_tokens * _CACHE_WRITE_MULTIPLIER,
        sa.BigInteger,
    )


async def _used_today(db: AsyncSession, user_id: uuid.UUID, day: datetime.date) -> int:
    """Today's billable BUILD token total for the user (0 when no row yet): the cost-weighted
    spend, via the shared `billable_spend` expression so it can never drift from the admin
    roster's number. The `kind == build` predicate is deliberate and lives HERE, not inside
    `billable_spend`: only build spend is the citizen's to pay — review rows are
    metered for attribution and must never move what this gate (and through it the daily cap,
    default or overridden) decides, or opening the publish dialog would silently spend build
    budget the citizen never chose to spend."""
    total = await db.scalar(
        sa.select(billable_spend()).where(
            TokenUsage.user_id == user_id,
            TokenUsage.usage_date == day,
            TokenUsage.kind == TokenUsageKind.BUILD,
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
    Called BEFORE any stream byte. `used >= limit` matches Express's at-or-over gate."""
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
    kind: TokenUsageKind = TokenUsageKind.BUILD,
) -> None:
    """Atomically fold a turn's token spend into today's row FOR ITS KIND (add in SQL, no
    lost update). Does NOT commit — the caller owns the transaction so the turn-persist and
    the usage write commit together. Parity with Express's atomic `$inc`.

    `kind` defaults to `build` on purpose: every call site that predates the dimension
    is a build writer, so none had to be touched to stay correct. Only the pre-publish
    classification review passes `review` — spend the gate meters but never bills."""
    day = ist_today()
    stmt = (
        pg_insert(TokenUsage)
        .values(
            user_id=user_id,
            usage_date=day,
            kind=kind,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_read_tokens=cache_read_tokens,
            cache_write_tokens=cache_write_tokens,
        )
        .on_conflict_do_update(
            index_elements=[TokenUsage.user_id, TokenUsage.usage_date, TokenUsage.kind],
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
