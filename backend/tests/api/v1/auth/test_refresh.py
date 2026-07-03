"""POST /auth/refresh — rotation, reuse-detection, CSRF, absolute lifetime (U7)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import httpx
from sqlalchemy import func, select

from src.db.models.refresh_token import RefreshToken
from src.services.auth.csrf import issue_csrf_token
from src.services.auth.refresh import generate_refresh_token, hash_refresh_token, issue_new_family
from tests.factories import UserFactory


def _set_cookies(resp: httpx.Response) -> dict[str, str]:
    return {raw.split("=", 1)[0].strip(): raw for raw in resp.headers.get_list("set-cookie")}


def _cookie_value(raw: str) -> str:
    return raw.split("=", 1)[1].split(";", 1)[0]


def _headers(*, refresh: str, csrf: str) -> dict[str, str]:
    return {"Cookie": f"refresh={refresh}; csrf={csrf}", "X-CSRF-Token": csrf}


async def _setup(db_session):
    user = await UserFactory.create(db_session)
    raw = await issue_new_family(db_session, user.id)
    csrf = issue_csrf_token(user.id, user.token_version)
    return user, raw, csrf


async def test_happy_refresh_rotates_and_sets_cookies(client, db_session) -> None:
    user, raw, csrf = await _setup(db_session)

    resp = await client.post("/v1/auth/refresh", headers=_headers(refresh=raw, csrf=csrf))

    assert resp.status_code == 200
    cookies = _set_cookies(resp)
    assert {"session", "refresh", "csrf"} <= set(cookies)
    # The new refresh token differs from the one presented.
    assert _cookie_value(cookies["refresh"]) != raw
    # The old token is consumed.
    old = await db_session.scalar(
        select(RefreshToken).where(RefreshToken.token_hash == hash_refresh_token(raw))
    )
    await db_session.refresh(old)
    assert old.used_at is not None


async def test_reuse_returns_401_and_revokes_family(client, db_session) -> None:
    user, raw, csrf = await _setup(db_session)
    # First refresh succeeds and yields a successor.
    first = await client.post("/v1/auth/refresh", headers=_headers(refresh=raw, csrf=csrf))
    successor = _cookie_value(_set_cookies(first)["refresh"])

    # Replaying the ORIGINAL token -> reuse -> 401 + whole family revoked (AE2).
    replay = await client.post("/v1/auth/refresh", headers=_headers(refresh=raw, csrf=csrf))
    assert replay.status_code == 401

    revoked = await db_session.scalar(
        select(func.count())
        .select_from(RefreshToken)
        .where(RefreshToken.user_id == user.id, RefreshToken.revoked.is_(False))
    )
    assert revoked == 0  # no active tokens remain

    # The formerly-valid successor is now dead too.
    after = await client.post("/v1/auth/refresh", headers=_headers(refresh=successor, csrf=csrf))
    assert after.status_code == 401


async def test_missing_csrf_returns_403(client, db_session) -> None:
    _user, raw, _csrf = await _setup(db_session)
    resp = await client.post("/v1/auth/refresh", headers={"Cookie": f"refresh={raw}"})
    assert resp.status_code == 403


async def test_missing_refresh_cookie_returns_401(client) -> None:
    resp = await client.post("/v1/auth/refresh")
    assert resp.status_code == 401


async def test_unknown_refresh_token_returns_401(client, db_session) -> None:
    user = await UserFactory.create(db_session)
    csrf = issue_csrf_token(user.id, user.token_version)
    resp = await client.post(
        "/v1/auth/refresh", headers=_headers(refresh=generate_refresh_token(), csrf=csrf)
    )
    assert resp.status_code == 401


async def test_absolute_expiry_returns_401(client, db_session) -> None:
    user = await UserFactory.create(db_session)
    raw = generate_refresh_token()
    now = datetime.now(tz=UTC)
    db_session.add(
        RefreshToken(
            user_id=user.id,
            family_id=user.id,  # any uuid; a single-token family
            token_hash=hash_refresh_token(raw),
            expires_at=now + timedelta(days=7),
            absolute_expires_at=now - timedelta(minutes=1),  # past the 8h cap
        )
    )
    await db_session.flush()
    csrf = issue_csrf_token(user.id, user.token_version)
    resp = await client.post("/v1/auth/refresh", headers=_headers(refresh=raw, csrf=csrf))
    assert resp.status_code == 401
