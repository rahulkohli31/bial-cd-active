"""Admin "reset today's usage" (POST /admin/users/{id}/reset-usage): lets a
super-admin let a user start today over without waiting for the IST midnight
rollover. Deletes just the `token_usage` row for `ist_today()` — `_used_today`
already reads 0 for an absent row, so this is a true "as if they never chatted
today" reset, not a historical edit. Idempotent (no 409: there is no
suspended/not-suspended-style state to conflict with), audited, and — unlike
deactivate — carries no super-admin guard, since resetting usage isn't unsafe."""

from __future__ import annotations

import datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings
from src.db.models.audit import AuditLog
from src.db.models.token_usage import TokenUsage
from src.db.models.user import User
from src.main import create_app
from src.services.auth.session_jwt import mint_session_jwt
from src.services.usage.gate import IST, ist_today, record_usage
from tests.factories import UserFactory

_TTL = settings.auth.access_ttl_seconds


def _cookie(user: User) -> dict[str, str]:
    return {"Cookie": f"session={mint_session_jwt(user.id, user.token_version, _TTL)}"}


async def _admin(db: AsyncSession) -> dict[str, str]:
    return _cookie(await UserFactory.create(db, email="admin@bial.com"))


async def _reset(client, headers, user_id) -> Any:
    return await client.post(f"/v1/admin/users/{user_id}/reset-usage", headers=headers)


async def _roster_row(client, headers, email: str) -> dict[str, Any]:
    resp = await client.get("/v1/admin/users", headers=headers)
    assert resp.status_code == 200
    return next(u for u in resp.json()["users"] if u["email"] == email)


async def test_reset_zeroes_todays_usage(client, db_session) -> None:
    spender = await UserFactory.create(db_session, email="spender@rvaiglobal.com")
    await record_usage(db_session, spender.id, input_tokens=100, output_tokens=20)
    await db_session.flush()
    admin_headers = await _admin(db_session)

    assert (await _roster_row(client, admin_headers, "spender@rvaiglobal.com"))["usageToday"] > 0

    resp = await _reset(client, admin_headers, spender.id)
    assert resp.status_code == 200
    body = resp.json()
    assert body["userId"] == str(spender.id)
    assert body["usageToday"] == 0

    assert (await _roster_row(client, admin_headers, "spender@rvaiglobal.com"))["usageToday"] == 0


async def test_reset_does_not_touch_other_days(client, db_session) -> None:
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
    await record_usage(db_session, user.id, input_tokens=50, output_tokens=10)  # today
    await db_session.flush()
    admin_headers = await _admin(db_session)

    assert (await _reset(client, admin_headers, user.id)).status_code == 200

    # Today's row is gone; yesterday's row (the historical ledger) is untouched.
    today_row = await db_session.scalar(
        select(TokenUsage).where(
            TokenUsage.user_id == user.id, TokenUsage.usage_date == ist_today()
        )
    )
    assert today_row is None
    yesterday_row = await db_session.scalar(
        select(TokenUsage).where(
            TokenUsage.user_id == user.id, TokenUsage.usage_date == yesterday
        )
    )
    assert yesterday_row is not None
    assert yesterday_row.input_tokens == 999


async def test_reset_on_a_user_with_no_usage_is_idempotent_not_a_conflict(
    client, db_session
) -> None:
    fresh = await UserFactory.create(db_session, email="fresh@rvaiglobal.com")
    admin_headers = await _admin(db_session)

    resp = await _reset(client, admin_headers, fresh.id)
    assert resp.status_code == 200  # no 409 — nothing to conflict with
    assert resp.json()["usageToday"] == 0

    # A second reset is equally harmless.
    assert (await _reset(client, admin_headers, fresh.id)).status_code == 200


async def test_super_admin_can_reset_their_own_usage(client, db_session) -> None:
    # Unlike deactivate (AE6), resetting usage carries no self/peer-admin guard.
    admin = await UserFactory.create(db_session, email="admin@bial.com")
    await record_usage(db_session, admin.id, input_tokens=10, output_tokens=5)
    await db_session.flush()

    resp = await _reset(client, _cookie(admin), admin.id)
    assert resp.status_code == 200
    assert resp.json()["usageToday"] == 0


async def test_unknown_user_404(client, db_session) -> None:
    from uuid import uuid4

    admin_headers = await _admin(db_session)
    assert (await _reset(client, admin_headers, uuid4())).status_code == 404


async def test_reset_is_audited(client, db_session) -> None:
    user = await UserFactory.create(db_session, email="ledger@rvaiglobal.com")
    admin_headers = await _admin(db_session)
    await _reset(client, admin_headers, user.id)

    action = await db_session.scalar(
        select(AuditLog.action).where(
            AuditLog.resource_type == "user", AuditLog.resource_id == str(user.id)
        )
    )
    assert action == "usage:reset"


async def test_citizen_is_forbidden(client, db_session) -> None:
    plain = await UserFactory.create(db_session, email="plain@rvaiglobal.com")
    other = await UserFactory.create(db_session, email="other@rvaiglobal.com")
    assert (await _reset(client, _cookie(plain), other.id)).status_code == 403


def test_reset_usage_documents_error_codes_in_openapi() -> None:
    paths = create_app().openapi()["paths"]
    codes = set(paths["/v1/admin/users/{user_id}/reset-usage"]["post"]["responses"])
    assert {"401", "403", "404", "500"} <= codes
    assert "409" not in codes  # idempotent — no state to conflict with
