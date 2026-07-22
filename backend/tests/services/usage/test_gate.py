"""Daily-token gate service — IST math, effective-limit resolution, atomic accounting,
and the byte-stable 429 (U6). Runs against the real migrated schema in the rolled-back
per-test transaction.
"""

from __future__ import annotations

import datetime

import sqlalchemy as sa

from src.config import settings
from src.db.models.token_usage import TokenUsage
from src.db.models.user_limit import UserLimit
from src.services.usage.gate import (
    DAILY_LIMIT_EXCEEDED_CODE,
    DailyTokenLimitExceededError,
    effective_daily_limit,
    enforce_daily_limit,
    ist_today,
    next_ist_midnight_iso,
    record_usage,
    usage_today,
)
from tests.factories import UserFactory

_UTC = datetime.UTC


# --- IST day math (pure) ------------------------------------------------------


def test_ist_today_rolls_at_ist_midnight() -> None:
    # 18:29:59 UTC is still 2026-07-06 in IST (23:59:59 IST); 18:30:00 UTC is 2026-07-07.
    assert ist_today(datetime.datetime(2026, 7, 6, 18, 29, 59, tzinfo=_UTC)) == datetime.date(
        2026, 7, 6
    )
    assert ist_today(datetime.datetime(2026, 7, 6, 18, 30, 0, tzinfo=_UTC)) == datetime.date(
        2026, 7, 7
    )


def test_next_ist_midnight_iso_format_and_value() -> None:
    # Midnight IST = 18:30:00 UTC; the ".000Z" suffix byte-matches JS toISOString().
    got = next_ist_midnight_iso(datetime.datetime(2026, 7, 6, 12, 0, 0, tzinfo=_UTC))
    assert got == "2026-07-06T18:30:00.000Z"


def test_next_ist_midnight_iso_after_ist_midnight_rolls_forward() -> None:
    # 19:00 UTC is already 2026-07-07 00:30 IST, so the next reset is 2026-07-07 midnight IST.
    got = next_ist_midnight_iso(datetime.datetime(2026, 7, 6, 19, 0, 0, tzinfo=_UTC))
    assert got == "2026-07-07T18:30:00.000Z"


# --- effective limit resolution ----------------------------------------------


async def test_effective_limit_defaults_to_global(db_session) -> None:
    user = await UserFactory.create(db_session)
    assert await effective_daily_limit(db_session, user.id) == settings.DAILY_TOKEN_LIMIT


async def test_effective_limit_uses_positive_override(db_session) -> None:
    user = await UserFactory.create(db_session)
    db_session.add(UserLimit(user_id=user.id, daily_token_limit=500))
    await db_session.flush()
    assert await effective_daily_limit(db_session, user.id) == 500


async def test_effective_limit_ignores_nonpositive_override(db_session) -> None:
    # A 0/negative override is meaningless — fall back to the default (parity with
    # Express `posIntOr`).
    user = await UserFactory.create(db_session)
    db_session.add(UserLimit(user_id=user.id, daily_token_limit=0))
    await db_session.flush()
    assert await effective_daily_limit(db_session, user.id) == settings.DAILY_TOKEN_LIMIT


# --- the gate (AE2) -----------------------------------------------------------


async def test_enforce_passes_under_limit(db_session) -> None:
    user = await UserFactory.create(db_session)
    db_session.add(UserLimit(user_id=user.id, daily_token_limit=100))
    await record_usage(db_session, user.id, input_tokens=40, output_tokens=10)
    # 50 < 100 — no raise.
    await enforce_daily_limit(db_session, user.id)


async def test_enforce_raises_at_limit(db_session) -> None:
    # AE2: at-or-over the cap blocks (used >= limit).
    user = await UserFactory.create(db_session)
    db_session.add(UserLimit(user_id=user.id, daily_token_limit=50))
    await record_usage(db_session, user.id, input_tokens=30, output_tokens=20)
    try:
        await enforce_daily_limit(db_session, user.id)
    except DailyTokenLimitExceededError as exc:
        assert exc.limit == 50
        assert exc.used == 50
    else:
        raise AssertionError("expected DailyTokenLimitExceededError at the cap")


async def test_used_counts_input_plus_output_only_not_cache(db_session) -> None:
    # F0: pydantic-ai's `input_tokens` ALREADY includes cache_read+cache_write, so the daily
    # total is `input + output` — the cache columns must NOT be re-added (re-adding them was
    # the ~2x double-count). input=40 + output=10 = 50; the +30+25 cache stays out of the sum.
    user = await UserFactory.create(db_session)
    db_session.add(UserLimit(user_id=user.id, daily_token_limit=100))
    await record_usage(
        db_session,
        user.id,
        input_tokens=40,
        output_tokens=10,
        cache_read_tokens=30,
        cache_write_tokens=25,
    )
    snapshot = await usage_today(db_session, user.id)
    assert snapshot.used == 50  # NOT 105 — cache is inside input_tokens already
    # 50 < 100, so the gate does not raise (the old fold would have blocked at 105).
    await enforce_daily_limit(db_session, user.id)


async def test_cap_boundary_uses_the_corrected_total(db_session) -> None:
    # The `>=` gate fires on the CORRECTED total. input=100 (incl. cache) + output=50 = 150:
    # limit 160 passes, limit 150 blocks (at-or-over), and `used` is 150 not 200.
    user = await UserFactory.create(db_session)
    await record_usage(
        db_session,
        user.id,
        input_tokens=100,
        output_tokens=50,
        cache_read_tokens=30,
        cache_write_tokens=20,
    )
    limit = UserLimit(user_id=user.id, daily_token_limit=160)
    db_session.add(limit)
    await db_session.flush()
    await enforce_daily_limit(db_session, user.id)  # 150 < 160 — no raise

    limit.daily_token_limit = 150
    await db_session.flush()
    try:
        await enforce_daily_limit(db_session, user.id)
    except DailyTokenLimitExceededError as exc:
        assert exc.used == 150  # input+output, cache not re-added
    else:
        raise AssertionError("expected a raise at used == limit (>=)")


async def test_used_is_input_plus_output_when_no_cache(db_session) -> None:
    # Behaviour is unchanged when caching is absent: used == input + output.
    user = await UserFactory.create(db_session)
    await record_usage(db_session, user.id, input_tokens=70, output_tokens=30)
    snapshot = await usage_today(db_session, user.id)
    assert snapshot.used == 100


async def test_used_is_zero_with_no_row(db_session) -> None:
    # No usage row yet → 0, not an error (the read is a scalar select that returns None).
    user = await UserFactory.create(db_session)
    snapshot = await usage_today(db_session, user.id)
    assert snapshot.used == 0


async def test_record_usage_stores_all_four_columns_raw(db_session) -> None:
    # The fix is read-side ONLY: record_usage still persists every class raw, so the cache
    # columns remain available for the deferred cost/analytics meter — they are just excluded
    # from the cap total. This guards against a "fix at write time" regression.
    user = await UserFactory.create(db_session)
    await record_usage(
        db_session,
        user.id,
        input_tokens=100,
        output_tokens=50,
        cache_read_tokens=30,
        cache_write_tokens=20,
    )
    row = await db_session.scalar(sa.select(TokenUsage).where(TokenUsage.user_id == user.id))
    assert row is not None
    assert (
        row.input_tokens,
        row.output_tokens,
        row.cache_read_tokens,
        row.cache_write_tokens,
    ) == (100, 50, 30, 20)


# --- atomic accounting --------------------------------------------------------


async def test_record_usage_accumulates(db_session) -> None:
    # Two increments fold together (add-in-SQL upsert) — no lost update.
    user = await UserFactory.create(db_session)
    await record_usage(db_session, user.id, input_tokens=10, output_tokens=3)
    await record_usage(db_session, user.id, input_tokens=5, output_tokens=2, cache_read_tokens=1)
    snapshot = await usage_today(db_session, user.id)
    # (10+5) input + (3+2) output = 20; the cache_read=1 is NOT re-added (it is already
    # inside input_tokens under pydantic-ai's inclusive semantics).
    assert snapshot.used == 20


# --- read snapshot ------------------------------------------------------------


async def test_usage_today_snapshot(db_session) -> None:
    user = await UserFactory.create(db_session)
    db_session.add(UserLimit(user_id=user.id, daily_token_limit=1000))
    await record_usage(db_session, user.id, input_tokens=100, output_tokens=50)
    snapshot = await usage_today(db_session, user.id)
    assert snapshot.used == 150
    assert snapshot.limit == 1000
    assert snapshot.remaining == 850
    assert snapshot.resets_at.endswith("Z")


# --- 429 rendering (byte-stable contract) -------------------------------------


def test_daily_limit_exceeded_renders_stable_body() -> None:
    resp = DailyTokenLimitExceededError(limit=1_000_000, used=1_000_050).as_response()
    assert resp.status_code == 429
    import json

    body = json.loads(bytes(resp.body))
    assert body["error"]["code"] == DAILY_LIMIT_EXCEEDED_CODE
    assert body["error"]["limit"] == 1_000_000
    assert body["error"]["used"] == 1_000_050
    assert body["error"]["remaining"] == 0
    # en-US thousands grouping in the message (matches Express toLocaleString).
    assert "1,000,000" in body["error"]["message"]
