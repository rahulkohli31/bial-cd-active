"""POST /admin/users/limits/bulk (admin "Global Limits") — set the same daily token
limit for many users in one request, either every user system-wide or a hand-picked
subset. Super-admin-only, positive-integer-only, never touches the per-conversation
context limits, and always upserts (idempotent on re-run)."""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings
from src.db.models.audit import AuditLog
from src.db.models.user_limit import UserLimit
from src.services.auth.session_jwt import mint_session_jwt
from tests.factories import UserFactory

_TTL = settings.auth.access_ttl_seconds


def _cookie(jwt: str) -> dict[str, str]:
    return {"Cookie": f"session={jwt}"}


async def _admin(db: AsyncSession) -> dict[str, str]:
    user = await UserFactory.create(db, email="admin@bial.com")
    return _cookie(mint_session_jwt(user.id, user.token_version, _TTL))


async def _citizen(db: AsyncSession) -> dict[str, str]:
    user = await UserFactory.create(db, email="plain@rvaiglobal.com")
    return _cookie(mint_session_jwt(user.id, user.token_version, _TTL))


async def _override(db: AsyncSession, user_id) -> UserLimit | None:
    return (
        await db.execute(sa.select(UserLimit).where(UserLimit.user_id == user_id))
    ).scalar_one_or_none()


async def test_non_admin_forbidden(client, db_session) -> None:
    headers = await _citizen(db_session)
    resp = await client.post(
        "/v1/admin/users/limits/bulk", json={"dailyTokenLimit": 500000}, headers=headers
    )
    assert resp.status_code == 403


async def test_non_positive_limit_is_400(client, db_session) -> None:
    headers = await _admin(db_session)
    resp = await client.post(
        "/v1/admin/users/limits/bulk", json={"dailyTokenLimit": 0}, headers=headers
    )
    assert resp.status_code == 400
    resp = await client.post(
        "/v1/admin/users/limits/bulk", json={"dailyTokenLimit": -5}, headers=headers
    )
    assert resp.status_code == 400


async def test_empty_user_ids_list_is_400(client, db_session) -> None:
    headers = await _admin(db_session)
    resp = await client.post(
        "/v1/admin/users/limits/bulk",
        json={"dailyTokenLimit": 500000, "userIds": []},
        headers=headers,
    )
    assert resp.status_code == 400


async def test_selected_scope_touches_only_the_given_users(client, db_session) -> None:
    a = await UserFactory.create(db_session, email="a@rvaiglobal.com")
    b = await UserFactory.create(db_session, email="b@rvaiglobal.com")
    untouched = await UserFactory.create(db_session, email="c@rvaiglobal.com")
    headers = await _admin(db_session)

    resp = await client.post(
        "/v1/admin/users/limits/bulk",
        json={"dailyTokenLimit": 2_000_000, "userIds": [str(a.id), str(b.id)]},
        headers=headers,
    )
    assert resp.status_code == 200
    assert resp.json()["updatedCount"] == 2

    override_a = await _override(db_session, a.id)
    override_b = await _override(db_session, b.id)
    assert override_a is not None and override_a.daily_token_limit == 2_000_000
    assert override_b is not None and override_b.daily_token_limit == 2_000_000
    assert await _override(db_session, untouched.id) is None


async def test_all_scope_upserts_every_existing_user(client, db_session) -> None:
    users = [await UserFactory.create(db_session, email=f"u{i}@rvaiglobal.com") for i in range(3)]
    headers = await _admin(db_session)

    resp = await client.post(
        "/v1/admin/users/limits/bulk", json={"dailyTokenLimit": 750_000}, headers=headers
    )
    assert resp.status_code == 200
    # The admin who made the request is also a user, so the count includes them too —
    # assert on a lower bound (the fixture roster) rather than an exact total.
    assert resp.json()["updatedCount"] >= len(users)

    for user in users:
        override = await _override(db_session, user.id)
        assert override is not None
        assert override.daily_token_limit == 750_000


async def test_re_applying_is_idempotent_and_only_updates_daily_token_limit(
    client, db_session
) -> None:
    target = await UserFactory.create(db_session, email="repeat@rvaiglobal.com")
    headers = await _admin(db_session)

    # Pre-seed an existing override on the OTHER two fields to prove the bulk apply
    # never touches them.
    db_session.add(
        UserLimit(user_id=target.id, context_soft_limit=100_000, context_hard_limit=180_000)
    )
    await db_session.flush()

    first = await client.post(
        "/v1/admin/users/limits/bulk",
        json={"dailyTokenLimit": 1_500_000, "userIds": [str(target.id)]},
        headers=headers,
    )
    second = await client.post(
        "/v1/admin/users/limits/bulk",
        json={"dailyTokenLimit": 1_500_000, "userIds": [str(target.id)]},
        headers=headers,
    )
    assert first.status_code == 200 and second.status_code == 200

    override = await _override(db_session, target.id)
    assert override is not None
    assert override.daily_token_limit == 1_500_000
    # Untouched — a bulk daily-limit apply never modifies the per-conversation knobs.
    assert override.context_soft_limit == 100_000
    assert override.context_hard_limit == 180_000

    # Exactly one row per user — the upsert never duplicated it on the second call.
    count = (
        await db_session.execute(
            sa.select(sa.func.count()).select_from(UserLimit).where(UserLimit.user_id == target.id)
        )
    ).scalar_one()
    assert count == 1


async def test_bulk_apply_is_audited_with_count_and_scope_never_the_raw_ids(
    client, db_session
) -> None:
    a = await UserFactory.create(db_session, email="aud-a@rvaiglobal.com")
    b = await UserFactory.create(db_session, email="aud-b@rvaiglobal.com")
    headers = await _admin(db_session)

    await client.post(
        "/v1/admin/users/limits/bulk",
        json={"dailyTokenLimit": 3_000_000, "userIds": [str(a.id), str(b.id)]},
        headers=headers,
    )
    row = (
        await db_session.execute(sa.select(AuditLog).where(AuditLog.action == "limits:bulk_set"))
    ).scalar_one()
    assert row.resource_type == "user"
    assert row.resource_id is None
    assert row.detail == {"dailyTokenLimit": 3_000_000, "userCount": 2, "scope": "selected"}
    assert "userIds" not in row.detail
