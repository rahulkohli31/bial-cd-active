"""Daily-token gate service — IST math, effective-limit resolution, atomic accounting,
and the byte-stable 429. Runs against the real migrated schema in the rolled-back
per-test transaction.
"""

from __future__ import annotations

import datetime
from decimal import Decimal

import pytest
import sqlalchemy as sa

from src.config import settings
from src.db.models.token_usage import TokenUsage, TokenUsageKind
from src.db.models.user_limit import UserLimit
from src.services.classification.constants import CACHE_TTL as REVIEW_CACHE_TTL
from src.services.orchestrator.constants import CACHE_TTL
from src.services.usage import gate
from src.services.usage.gate import (
    DAILY_LIMIT_EXCEEDED_CODE,
    DailyTokenLimitExceededError,
    billable_spend,
    effective_daily_limit,
    enforce_daily_limit,
    ist_today,
    next_ist_midnight_iso,
    record_usage,
    usage_today,
    weighted_spend,
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


# --- the gate -----------------------------------------------------------


async def test_enforce_passes_under_limit(db_session) -> None:
    user = await UserFactory.create(db_session)
    db_session.add(UserLimit(user_id=user.id, daily_token_limit=100))
    await record_usage(db_session, user.id, input_tokens=40, output_tokens=10)
    # 50 < 100 — no raise.
    await enforce_daily_limit(db_session, user.id)


async def test_enforce_raises_at_limit(db_session) -> None:
    # At-or-over the cap blocks (used >= limit).
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


async def test_used_is_cost_weighted_across_token_classes(db_session) -> None:
    # The cap bills each class at its cost weight: fresh input + output at face value, cache
    # reads at 1/10, cache writes at the 1h tier's 200%. input=1000 is the pydantic-ai GRAND
    # TOTAL with cr=800 + cw=100 inside it → fresh=100. used = 100 + 50 + 800/10 + 100*2 = 430
    # — not the 1050 face-value fold that let one cached-prefix build book ~956k on 68 fresh
    # tokens.
    user = await UserFactory.create(db_session)
    db_session.add(UserLimit(user_id=user.id, daily_token_limit=1_000))
    await record_usage(
        db_session,
        user.id,
        input_tokens=1_000,
        output_tokens=50,
        cache_read_tokens=800,
        cache_write_tokens=100,
    )
    snapshot = await usage_today(db_session, user.id)
    assert snapshot.used == 430
    # 430 < 1000, so the gate does not raise (the face-value fold would have booked 1050).
    await enforce_daily_limit(db_session, user.id)


async def test_cap_boundary_uses_the_weighted_total(db_session) -> None:
    # The `>=` gate fires on the WEIGHTED total. input=100 (incl. cr=30, cw=20) + output=50:
    # fresh=50, used = 50 + 50 + 30/10 + 20*2 = 143. Limit 145 passes, 143 blocks
    # (at-or-over), and the 429 carries used=143 — the same number the header meter shows.
    user = await UserFactory.create(db_session)
    await record_usage(
        db_session,
        user.id,
        input_tokens=100,
        output_tokens=50,
        cache_read_tokens=30,
        cache_write_tokens=20,
    )
    limit = UserLimit(user_id=user.id, daily_token_limit=145)
    db_session.add(limit)
    await db_session.flush()
    await enforce_daily_limit(db_session, user.id)  # 143 < 145 — no raise

    limit.daily_token_limit = 143
    await db_session.flush()
    try:
        await enforce_daily_limit(db_session, user.id)
    except DailyTokenLimitExceededError as exc:
        assert exc.used == 143  # the weighted total, same as usage_today
    else:
        raise AssertionError("expected a raise at used == limit (>=)")


async def test_malformed_cache_totals_clamp_fresh_at_zero(db_session) -> None:
    # Defensive: a row where cr+cw > input (impossible under pydantic-ai semantics, but the
    # columns are independent) must not go NEGATIVE on fresh — GREATEST clamps it to 0 and the
    # weighted shares still count. input=40, cr=30, cw=25 → fresh=max(40-55,0)=0;
    # used = 0 + 10 + 30/10 + 25*2 = 63.
    user = await UserFactory.create(db_session)
    await record_usage(
        db_session,
        user.id,
        input_tokens=40,
        output_tokens=10,
        cache_read_tokens=30,
        cache_write_tokens=25,
    )
    snapshot = await usage_today(db_session, user.id)
    assert snapshot.used == 63


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
    # The weighting is read-side ONLY: record_usage still persists every class raw, so the
    # ledger stays the unweighted truth (and a future weight change re-prices history for
    # free). This guards against a "fix at write time" regression.
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


# --- the cache-write weight follows the tier the platform buys ----------------


def test_the_write_weight_table_states_both_of_anthropics_prices() -> None:
    # Anthropic prices a cache WRITE by the TTL bought at the breakpoint: 1.25x base input at
    # the 5-minute tier, 2x at the 1-hour one. A read is a tenth at either tier, which is why
    # only the write needs a table. Literal on both sides — a weight recomputed from the code
    # under test cannot fail.
    assert gate._CACHE_WRITE_MULTIPLIER_BY_TTL == {"5m": Decimal("1.25"), "1h": Decimal("2")}
    assert gate._CACHE_READ_DIVISOR == 10


def test_the_meter_charges_the_tier_the_platform_actually_buys() -> None:
    # The defect this closes: the meter weighed cache writes at 1.25x — the 5-minute rate —
    # while every breakpoint bought the 1-hour tier. The meter restates its tier rather than
    # importing it (`orchestrator.constants` drags the API layer in behind it), so THIS is the
    # joint: the tier the meter bills must be the tier both agents buy.
    assert gate._BILLED_CACHE_TIER == CACHE_TTL  # the build loop's breakpoints
    assert gate._BILLED_CACHE_TIER == REVIEW_CACHE_TTL  # the pre-publish review's
    assert gate._CACHE_WRITE_MULTIPLIER == Decimal("2")


@pytest.mark.parametrize(("ttl", "expected"), [("1h", 1_137), ("5m", 987)])
async def test_one_fixed_row_weights_to_the_stated_total_at_each_tier(
    db_session, monkeypatch, ttl: str, expected: int
) -> None:
    # One row, both tiers, every expectation a literal. input=1000 is the pydantic-ai GRAND
    # TOTAL with cr=403 + cw=200 inside it → fresh=397; output 300; reads 403/10 = 40.3.
    #   1h: 397 + 300 + 40.3 + 200*2    = 1137.3 → 1137
    #   5m: 397 + 300 + 40.3 + 200*1.25 =  987.3 →  987
    # The fractional read is deliberate: both readers must round ONCE, at the very end.
    monkeypatch.setattr(gate, "_CACHE_WRITE_MULTIPLIER", gate._CACHE_WRITE_MULTIPLIER_BY_TTL[ttl])
    user = await UserFactory.create(db_session)
    await record_usage(
        db_session,
        user.id,
        input_tokens=1_000,
        output_tokens=300,
        cache_read_tokens=403,
        cache_write_tokens=200,
    )
    assert (await usage_today(db_session, user.id)).used == expected
    # The in-process reader the run bound uses spells the same arithmetic against the same row.
    assert (
        weighted_spend(
            input_tokens=1_000,
            output_tokens=300,
            cache_read_tokens=403,
            cache_write_tokens=200,
        )
        == expected
    )


async def test_the_daily_gate_and_the_admin_roster_read_the_same_number(db_session) -> None:
    # Two screens, one expression. The roster (`api/v1/admin/router.py`) sums `billable_spend()`
    # grouped by kind over the IST day; the gate reads the same expression for the build row.
    # Spelled here the way the roster spells it, so a weight that moved for one reader and not
    # the other surfaces as a disagreement rather than as two plausible numbers on two screens.
    # Build: fresh=100 + output 50 + 800/10 + 100*2 = 430.
    # Review: fresh=50 + output 10 + 100/10 + 50*2 = 170 — reported beside it, never folded in.
    user = await UserFactory.create(db_session)
    await record_usage(
        db_session,
        user.id,
        input_tokens=1_000,
        output_tokens=50,
        cache_read_tokens=800,
        cache_write_tokens=100,
    )
    await record_usage(
        db_session,
        user.id,
        input_tokens=200,
        output_tokens=10,
        cache_read_tokens=100,
        cache_write_tokens=50,
        kind=TokenUsageKind.REVIEW,
    )
    rows = await db_session.execute(
        sa.select(TokenUsage.kind, sa.func.sum(billable_spend()).label("used"))
        .where(TokenUsage.usage_date == ist_today(), TokenUsage.user_id == user.id)
        .group_by(TokenUsage.kind)
    )
    roster = {row.kind: int(row.used) for row in rows}
    assert roster == {TokenUsageKind.BUILD: 430, TokenUsageKind.REVIEW: 170}
    assert (await usage_today(db_session, user.id)).used == 430


# --- atomic accounting --------------------------------------------------------


async def test_record_usage_accumulates(db_session) -> None:
    # Two increments fold together (add-in-SQL upsert) — no lost update.
    user = await UserFactory.create(db_session)
    await record_usage(db_session, user.id, input_tokens=10, output_tokens=3)
    await record_usage(db_session, user.id, input_tokens=5, output_tokens=2, cache_read_tokens=1)
    snapshot = await usage_today(db_session, user.id)
    # Folded row: input=15 (incl. cr=1) + output=5 → fresh=14 + 5 + 1/10 = 19.1, and the
    # single final BIGINT cast rounds the day total to 19.
    assert snapshot.used == 19


# --- the kind dimension: review spend is metered, never billed ------------


async def test_default_kind_is_build_so_existing_call_sites_stay_exact(db_session) -> None:
    # Every call site that predates the kind dimension records WITHOUT a kind and must keep
    # its exact behaviour: the default lands the spend on the `build` row — the one the gate reads.
    user = await UserFactory.create(db_session)
    await record_usage(db_session, user.id, input_tokens=10, output_tokens=5)
    row = await db_session.scalar(sa.select(TokenUsage).where(TokenUsage.user_id == user.id))
    assert row is not None
    assert row.kind is TokenUsageKind.BUILD


async def test_review_spend_is_a_second_row_and_the_build_row_is_untouched(db_session) -> None:
    # Same user, same day: review spend lands in its OWN row keyed by the kind — never
    # folded into the build row's counters (the upsert's conflict target includes kind).
    user = await UserFactory.create(db_session)
    await record_usage(db_session, user.id, input_tokens=100, output_tokens=40)
    await record_usage(
        db_session, user.id, input_tokens=7, output_tokens=3, kind=TokenUsageKind.REVIEW
    )
    rows = (
        (await db_session.execute(sa.select(TokenUsage).where(TokenUsage.user_id == user.id)))
        .scalars()
        .all()
    )
    by_kind = {row.kind: row for row in rows}
    assert set(by_kind) == {TokenUsageKind.BUILD, TokenUsageKind.REVIEW}
    build, review = by_kind[TokenUsageKind.BUILD], by_kind[TokenUsageKind.REVIEW]
    assert (build.input_tokens, build.output_tokens) == (100, 40)  # untouched by the review
    assert (review.input_tokens, review.output_tokens) == (7, 3)


async def test_review_spend_accumulates_in_its_own_row(db_session) -> None:
    # The atomic fold works PER KIND: two review records merge into one review row —
    # the widened `(user_id, usage_date, kind)` uniqueness is the upsert's target.
    user = await UserFactory.create(db_session)
    await record_usage(
        db_session, user.id, input_tokens=5, output_tokens=1, kind=TokenUsageKind.REVIEW
    )
    await record_usage(
        db_session, user.id, input_tokens=4, output_tokens=2, kind=TokenUsageKind.REVIEW
    )
    rows = (
        (await db_session.execute(sa.select(TokenUsage).where(TokenUsage.user_id == user.id)))
        .scalars()
        .all()
    )
    assert len(rows) == 1  # folded, not a second review row
    assert (rows[0].input_tokens, rows[0].output_tokens) == (9, 3)


async def test_review_spend_never_moves_the_gate(db_session) -> None:
    # The core property: a citizen at 99% of their cap is NOT pushed over it by a
    # review — the gate's number is unchanged by ANY amount of review spend, so opening
    # the publish dialog can never spend build budget the citizen never chose to spend.
    user = await UserFactory.create(db_session)
    db_session.add(UserLimit(user_id=user.id, daily_token_limit=100))
    await record_usage(db_session, user.id, input_tokens=90, output_tokens=9)  # 99 of 100
    await record_usage(
        db_session,
        user.id,
        input_tokens=1_000_000,
        output_tokens=500_000,
        kind=TokenUsageKind.REVIEW,
    )
    snapshot = await usage_today(db_session, user.id)
    assert snapshot.used == 99  # the review's 1.5M tokens are invisible to the meter
    await enforce_daily_limit(db_session, user.id)  # 99 < 100 — still allowed to build


async def test_review_spend_alone_over_the_cap_never_blocks_building(db_session) -> None:
    # Review spend past the cap ON ITS OWN: the citizen still builds — the cap measures
    # build spend only, and a heavy review day must not refuse their next build.
    user = await UserFactory.create(db_session)
    db_session.add(UserLimit(user_id=user.id, daily_token_limit=100))
    await record_usage(
        db_session,
        user.id,
        input_tokens=10_000,
        output_tokens=10_000,
        kind=TokenUsageKind.REVIEW,
    )
    snapshot = await usage_today(db_session, user.id)
    assert snapshot.used == 0  # no build spend today, whatever reviews cost
    await enforce_daily_limit(db_session, user.id)  # no raise
    # …and building still records normally afterwards.
    await record_usage(db_session, user.id, input_tokens=30, output_tokens=10)
    assert (await usage_today(db_session, user.id)).used == 40


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
