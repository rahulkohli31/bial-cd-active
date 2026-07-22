"""Super-admin roster (U9, R8–R9, KD-1): keyset-paginated + searchable `list_users`
replacing the unbounded full-table load; per-user "today's usage" as `input + output`
(the shared `billable_spend`) by ONE page-wide aggregate keyed to the IST day (no N+1);
suspension state surfaced. The limit-PATCH itself is covered by `test_limits_feedback.py`."""

from __future__ import annotations

import datetime

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings
from src.db.models.token_usage import TokenUsage
from src.main import create_app
from src.services.auth.session_jwt import mint_session_jwt
from src.services.usage.gate import IST, _used_today, ist_today, record_usage
from tests.factories import UserFactory

_TTL = settings.auth.access_ttl_seconds


def _cookie(jwt: str) -> dict[str, str]:
    return {"Cookie": f"session={jwt}"}


async def _admin(db: AsyncSession) -> dict[str, str]:
    user = await UserFactory.create(db, email="admin@bial.com")
    return _cookie(mint_session_jwt(user.id, user.token_version, _TTL))


async def _roster(client, headers, **params):
    resp = await client.get("/v1/admin/users", headers=headers, params=params)
    assert resp.status_code == 200
    return resp.json()


# --- envelope + openapi ----------------------------------------------------------


def test_list_users_documents_422_in_openapi() -> None:
    paths = create_app().openapi()["paths"]
    assert {"401", "403", "422", "500"} <= set(paths["/v1/admin/users"]["get"]["responses"])


async def test_envelope_shape(client, db_session) -> None:
    headers = await _admin(db_session)
    body = await _roster(client, headers)
    assert set(body) == {"defaults", "users", "nextCursor", "hasMore"}
    row = body["users"][0]
    assert {"suspendedAt", "usageToday", "limits", "effectiveLimits", "role"} <= set(row)


# --- keyset pagination -----------------------------------------------------------


async def test_roster_pages_walk_without_dup_or_skip(client, db_session) -> None:
    for i in range(4):
        await UserFactory.create(db_session, email=f"citizen{i}@rvaiglobal.com")
    headers = await _admin(db_session)  # the 5th user in the roster

    seen: list[str] = []
    cursor: str | None = None
    for _ in range(10):  # bounded walk; 5 users at limit=2 → 3 pages
        params: dict[str, str | int] = {"limit": 2}
        if cursor is not None:
            params["cursor"] = cursor
        body = await _roster(client, headers, **params)
        seen.extend(u["email"] for u in body["users"])
        if not body["hasMore"]:
            assert body["nextCursor"] is None
            break
        cursor = body["nextCursor"]
    assert len(seen) == 5  # every user exactly once — no duplicates, no skips
    assert len(set(seen)) == 5

    # Newest-first: UUIDv7 ids sort by creation time, so the admin (created last)
    # leads the first page.
    first = await _roster(client, headers, limit=2)
    assert first["users"][0]["email"] == "admin@bial.com"


async def test_search_persists_across_pages(client, db_session) -> None:
    for i in range(3):
        await UserFactory.create(db_session, email=f"match{i}@rvaiglobal.com")
    await UserFactory.create(db_session, email="other@rvaiglobal.com")
    headers = await _admin(db_session)

    page1 = await _roster(client, headers, q="match", limit=2)
    assert [u["email"] for u in page1["users"]] == [
        "match2@rvaiglobal.com",
        "match1@rvaiglobal.com",
    ]
    assert page1["hasMore"] is True
    page2 = await _roster(client, headers, q="match", limit=2, cursor=page1["nextCursor"])
    assert [u["email"] for u in page2["users"]] == ["match0@rvaiglobal.com"]
    assert page2["hasMore"] is False


# --- search ----------------------------------------------------------------------


async def test_search_matches_email_and_display_name(client, db_session) -> None:
    await UserFactory.create(db_session, email="alice@rvaiglobal.com")
    await UserFactory.create(db_session, email="bob@rvaiglobal.com", display_name="Alice Wonder")
    await UserFactory.create(db_session, email="carol@rvaiglobal.com", display_name="Carol")
    headers = await _admin(db_session)

    body = await _roster(client, headers, q="ALICE")  # case-insensitive
    assert {u["email"] for u in body["users"]} == {"alice@rvaiglobal.com", "bob@rvaiglobal.com"}


async def test_search_wildcards_are_literal(client, db_session) -> None:
    await UserFactory.create(db_session, email="someone@rvaiglobal.com")
    headers = await _admin(db_session)
    # autoescape: a raw `%` matches only a LITERAL percent (nobody), never everything.
    body = await _roster(client, headers, q="%")
    assert body["users"] == []


# --- parameter validation (R7) ---------------------------------------------------


async def test_bad_params_rejected_422(client, db_session) -> None:
    headers = await _admin(db_session)
    for bad_limit in (0, 101):
        resp = await client.get("/v1/admin/users", headers=headers, params={"limit": bad_limit})
        assert resp.status_code == 422
        # The SAME `{error:{message}}` shape as the cursor/q 422s — never FastAPI's
        # native `{detail:[...]}` body (one endpoint, one 422 shape).
        assert resp.json() == {"error": {"message": "limit must be between 1 and 100."}}
    assert (
        await client.get("/v1/admin/users", headers=headers, params={"cursor": "not-a-uuid"})
    ).status_code == 422
    assert (
        await client.get("/v1/admin/users", headers=headers, params={"q": "x" * 201})
    ).status_code == 422


# --- today's usage ---------------------------------------------------------------


async def test_usage_today_counts_input_plus_output_not_cache(client, db_session) -> None:
    spender = await UserFactory.create(db_session, email="spender@rvaiglobal.com")
    idle = await UserFactory.create(db_session, email="idle@rvaiglobal.com")
    await record_usage(
        db_session,
        spender.id,
        input_tokens=100,
        output_tokens=20,
        cache_read_tokens=3,
        cache_write_tokens=4,
    )
    headers = await _admin(db_session)

    body = await _roster(client, headers)
    by_email = {u["email"]: u for u in body["users"]}
    # input + output = 120; the cache classes are inside input_tokens already, not re-added.
    assert by_email["spender@rvaiglobal.com"]["usageToday"] == 120
    assert by_email["idle@rvaiglobal.com"]["usageToday"] == 0
    assert idle.suspended_at is None  # sanity: fresh users active


async def test_roster_usage_today_agrees_with_the_daily_gate(client, db_session) -> None:
    # Decision 2 (F0): the roster and the daily gate share ONE billable-spend expression, so
    # they can never drift. Seed a cache-heavy row and assert the roster's usageToday equals
    # the gate's `_used_today` for the same user/day — a half-landed fix (one reader corrected,
    # the other not) would break this exactly.
    user = await UserFactory.create(db_session, email="agree@rvaiglobal.com")
    await record_usage(
        db_session,
        user.id,
        input_tokens=200,
        output_tokens=40,
        cache_read_tokens=50,
        cache_write_tokens=60,
    )
    headers = await _admin(db_session)

    body = await _roster(client, headers)
    roster_used = {u["email"]: u["usageToday"] for u in body["users"]}["agree@rvaiglobal.com"]
    gate_used = await _used_today(db_session, user.id, ist_today())
    assert roster_used == gate_used == 240  # 200 input + 40 output; cache not re-added


async def test_usage_respects_ist_day_boundary(client, db_session) -> None:
    user = await UserFactory.create(db_session, email="night-owl@rvaiglobal.com")
    yesterday = ist_today(datetime.datetime.now(IST) - datetime.timedelta(days=1))
    db_session.add(
        TokenUsage(
            user_id=user.id,
            usage_date=yesterday,
            input_tokens=999,
            output_tokens=0,
            cache_read_tokens=0,
            cache_write_tokens=0,
        )
    )
    await db_session.flush()
    headers = await _admin(db_session)

    body = await _roster(client, headers)
    row = next(u for u in body["users"] if u["email"] == "night-owl@rvaiglobal.com")
    assert row["usageToday"] == 0  # yesterday's spend never bleeds into today


async def test_usage_is_one_aggregate_query_not_n_plus_one(client, db_session) -> None:
    for i in range(10):
        user = await UserFactory.create(db_session, email=f"bulk{i}@rvaiglobal.com")
        await record_usage(db_session, user.id, input_tokens=i, output_tokens=0)
    headers = await _admin(db_session)

    statements: list[str] = []

    def _log(state) -> None:
        statements.append(str(state.statement))

    event.listen(db_session.sync_session, "do_orm_execute", _log)
    try:
        body = await _roster(client, headers, limit=25)
    finally:
        event.remove(db_session.sync_session, "do_orm_execute", _log)
    assert len(body["users"]) == 11
    # Exactly ONE statement touches token_usage (the GROUP BY aggregate) and ONE
    # touches user_limits — page-size-independent, so the roster cannot N+1.
    assert sum("token_usage" in s for s in statements) == 1
    assert sum("user_limits" in s for s in statements) == 1


# --- suspension surfaced (column via U10a; endpoints tested in U10) ---------------


async def test_suspended_at_is_surfaced(client, db_session) -> None:
    frozen = await UserFactory.create(db_session, email="frozen@rvaiglobal.com")
    frozen.suspended_at = datetime.datetime(2026, 7, 9, 6, 0, tzinfo=datetime.UTC)
    await db_session.flush()
    headers = await _admin(db_session)

    body = await _roster(client, headers)
    by_email = {u["email"]: u for u in body["users"]}
    assert by_email["frozen@rvaiglobal.com"]["suspendedAt"] == "2026-07-09T06:00:00Z"
    assert by_email["admin@bial.com"]["suspendedAt"] is None
