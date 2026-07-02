"""POST /auth/logout — token_version bump, family revoke, cookie clear (U7)."""

from __future__ import annotations

import httpx
from sqlalchemy import func, select

from src.config import settings
from src.db.models.refresh_token import RefreshToken
from src.services.auth.csrf import issue_csrf_token
from src.services.auth.refresh import issue_new_family
from src.services.auth.session_jwt import mint_session_jwt
from tests.factories import UserFactory

_TTL = settings.auth.access_ttl_seconds


def _set_cookies(resp: httpx.Response) -> dict[str, str]:
    return {raw.split("=", 1)[0].strip(): raw for raw in resp.headers.get_list("set-cookie")}


def _headers(*, jwt: str, csrf: str) -> dict[str, str]:
    return {"Cookie": f"session={jwt}; csrf={csrf}", "X-CSRF-Token": csrf}


async def _active_families(db_session, user_id) -> int:
    return await db_session.scalar(
        select(func.count())
        .select_from(RefreshToken)
        .where(RefreshToken.user_id == user_id, RefreshToken.revoked.is_(False))
    )


async def test_logout_bumps_version_revokes_family_and_clears_cookies(client, db_session) -> None:
    user = await UserFactory.create(db_session)
    await issue_new_family(db_session, user.id)
    jwt = mint_session_jwt(user.id, user.token_version, _TTL)
    csrf = issue_csrf_token(user.id, user.token_version)

    resp = await client.post("/v1/auth/logout", headers=_headers(jwt=jwt, csrf=csrf))
    assert resp.status_code == 200

    await db_session.refresh(user)
    assert user.token_version == 1  # bumped from 0
    assert await _active_families(db_session, user.id) == 0

    cookies = _set_cookies(resp)
    assert {"session", "refresh", "csrf"} <= set(cookies)
    assert all("Max-Age=0" in cookies[name] for name in ("session", "refresh", "csrf"))


async def test_prelogout_session_jwt_rejected_afterward(client, db_session) -> None:
    user = await UserFactory.create(db_session)
    jwt = mint_session_jwt(user.id, user.token_version, _TTL)
    csrf = issue_csrf_token(user.id, user.token_version)

    await client.post("/v1/auth/logout", headers=_headers(jwt=jwt, csrf=csrf))
    # The same JWT (token_version 0) no longer authenticates (KD-6).
    me = await client.get("/v1/auth/me", headers={"Cookie": f"session={jwt}"})
    assert me.status_code == 401


async def test_missing_csrf_returns_403(client, db_session) -> None:
    user = await UserFactory.create(db_session)
    jwt = mint_session_jwt(user.id, user.token_version, _TTL)
    resp = await client.post("/v1/auth/logout", headers={"Cookie": f"session={jwt}"})
    assert resp.status_code == 403


async def test_logout_without_session_returns_401(client) -> None:
    resp = await client.post("/v1/auth/logout")
    assert resp.status_code == 401


async def test_logout_is_idempotent(client, db_session) -> None:
    user = await UserFactory.create(db_session)
    jwt0 = mint_session_jwt(user.id, 0, _TTL)
    csrf0 = issue_csrf_token(user.id, 0)
    first = await client.post("/v1/auth/logout", headers=_headers(jwt=jwt0, csrf=csrf0))
    assert first.status_code == 200

    # A fresh session for the bumped version logs out again with no error (the
    # revoke is a no-op, the bump is monotonic).
    jwt1 = mint_session_jwt(user.id, 1, _TTL)
    csrf1 = issue_csrf_token(user.id, 1)
    second = await client.post("/v1/auth/logout", headers=_headers(jwt=jwt1, csrf=csrf1))
    assert second.status_code == 200
    await db_session.refresh(user)
    assert user.token_version == 2
