"""POST /admin/users/limits/bulk (admin "Global Limits") — set the same daily token
limit for many users in one request, either every user system-wide or a hand-picked
subset. Super-admin-only, positive-integer-only, never touches the per-conversation
context limits, and always upserts (idempotent on re-run)."""

from __future__ import annotations

from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings
from src.db.models.audit import AuditLog
from src.db.models.user_limit import UserLimit
from src.main import create_app
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


async def test_over_the_ceiling_limit_is_400_not_500(client, db_session) -> None:
    # confirmAll: True (rather than userIds: []) so the ceiling clause is the ONLY thing
    # that can produce this 400 — an empty userIds list raises its own (identical-looking)
    # 400 first, which made this test pass even with the ceiling check deleted.
    headers = await _admin(db_session)
    resp = await client.post(
        "/v1/admin/users/limits/bulk",
        json={"dailyTokenLimit": 10**13, "confirmAll": True},
        headers=headers,
    )
    assert resp.status_code == 400


async def test_omitting_user_ids_without_confirm_all_is_400(client, db_session) -> None:
    # Field-ABSENCE is the most destructive input for a fleet-wide mutation — this
    # pins that it does NOT silently resolve to "every user" any more.
    headers = await _admin(db_session)
    resp = await client.post(
        "/v1/admin/users/limits/bulk", json={"dailyTokenLimit": 500000}, headers=headers
    )
    assert resp.status_code == 400


async def test_unknown_user_id_is_400_not_500(client, db_session) -> None:
    import uuid

    known = await UserFactory.create(db_session, email="known@rvaiglobal.com")
    headers = await _admin(db_session)
    resp = await client.post(
        "/v1/admin/users/limits/bulk",
        json={"dailyTokenLimit": 500000, "userIds": [str(known.id), str(uuid.uuid4())]},
        headers=headers,
    )
    assert resp.status_code == 400
    # Fails closed on the WHOLE request — the known user is untouched, not
    # partially updated.
    assert await _override(db_session, known.id) is None


async def test_a_duplicate_user_id_does_not_500_the_upsert(client, db_session) -> None:
    target = await UserFactory.create(db_session, email="dupe@rvaiglobal.com")
    headers = await _admin(db_session)
    resp = await client.post(
        "/v1/admin/users/limits/bulk",
        json={"dailyTokenLimit": 500000, "userIds": [str(target.id), str(target.id)]},
        headers=headers,
    )
    assert resp.status_code == 200
    # Deduped before the upsert — counted once, not twice.
    assert resp.json()["updatedCount"] == 1
    override = await _override(db_session, target.id)
    assert override is not None and override.daily_token_limit == 500000


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
        "/v1/admin/users/limits/bulk",
        json={"dailyTokenLimit": 750_000, "confirmAll": True},
        headers=headers,
    )
    assert resp.status_code == 200
    # The exact total is knowable — this test transaction rolls back per test, so it's
    # the 3 fixture users plus the admin `_admin()` created. A `>=` bound would still
    # pass if the all-scope roster silently dropped a class of users (superadmins,
    # suspended users) — the exact count catches that a lower bound cannot.
    assert resp.json()["updatedCount"] == len(users) + 1

    for user in users:
        override = await _override(db_session, user.id)
        assert override is not None
        assert override.daily_token_limit == 750_000


async def test_all_scope_excludes_suspended_users(client, db_session) -> None:
    active = await UserFactory.create(db_session, email="active@rvaiglobal.com")
    suspended = await UserFactory.create(
        db_session, email="suspended@rvaiglobal.com", suspended_at=datetime.now(UTC)
    )
    headers = await _admin(db_session)

    resp = await client.post(
        "/v1/admin/users/limits/bulk",
        json={"dailyTokenLimit": 750_000, "confirmAll": True},
        headers=headers,
    )
    assert resp.status_code == 200
    assert await _override(db_session, active.id) is not None
    assert await _override(db_session, suspended.id) is None


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


async def test_bulk_apply_is_audited_with_count_scope_and_a_before_image_for_selected(
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
    assert row.detail is not None
    assert row.detail["dailyTokenLimit"] == 3_000_000
    assert row.detail["userCount"] == 2
    assert row.detail["scope"] == "selected"
    assert "userIds" not in row.detail
    # Before-image, so a mis-applied "selected" change can be reconstructed — neither
    # user had a prior override row, so both read `dailyTokenLimit: None` ("inherited
    # the default"), not simply absent from the detail.
    before = sorted(row.detail["before"], key=lambda entry: str(entry["userId"]))
    assert before == sorted(
        [
            {"userId": str(a.id), "dailyTokenLimit": None},
            {"userId": str(b.id), "dailyTokenLimit": None},
        ],
        key=lambda entry: str(entry["userId"]),
    )


async def test_bulk_apply_before_image_reflects_an_actual_prior_value(client, db_session) -> None:
    # The other before-image test only covers "no prior row -> None" — this covers the
    # case an operator's rollback would actually use: a real prior value getting
    # overwritten and captured correctly, not just the absence of one.
    target = await UserFactory.create(db_session, email="aud-prior@rvaiglobal.com")
    db_session.add(UserLimit(user_id=target.id, daily_token_limit=900_000))
    await db_session.flush()
    headers = await _admin(db_session)

    await client.post(
        "/v1/admin/users/limits/bulk",
        json={"dailyTokenLimit": 3_000_000, "userIds": [str(target.id)]},
        headers=headers,
    )
    row = (
        await db_session.execute(sa.select(AuditLog).where(AuditLog.action == "limits:bulk_set"))
    ).scalar_one()
    assert row.detail is not None
    assert row.detail["before"] == [{"userId": str(target.id), "dailyTokenLimit": 900_000}]


async def test_bulk_apply_audit_for_all_scope_is_count_only_no_before_image(
    client, db_session
) -> None:
    await UserFactory.create(db_session, email="aud-c@rvaiglobal.com")
    headers = await _admin(db_session)

    await client.post(
        "/v1/admin/users/limits/bulk",
        json={"dailyTokenLimit": 3_000_000, "confirmAll": True},
        headers=headers,
    )
    row = (
        await db_session.execute(sa.select(AuditLog).where(AuditLog.action == "limits:bulk_set"))
    ).scalar_one()
    assert row.detail is not None
    assert row.detail["scope"] == "all"
    # `all` stays count-only — that roster is reconstructible from the users table
    # itself, unlike a hand-picked "selected" set.
    assert "before" not in row.detail
    # Records the deliberate suspended-user exclusion (router.py's `all` scope filter)
    # so an operator can later answer "who was excluded" without re-deriving it.
    assert row.detail["excludesSuspended"] is True


# --- openapi documentation -------------------------------------------------------


def test_bulk_limits_route_documents_error_codes_in_openapi() -> None:
    paths = create_app().openapi()["paths"]
    post = set(paths["/v1/admin/users/limits/bulk"]["post"]["responses"])
    assert {"400", "401", "403", "500"} <= post
