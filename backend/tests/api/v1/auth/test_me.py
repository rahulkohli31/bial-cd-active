"""current_user + GET /auth/me — cookie auth, fail-closed 401s, revocation (U6)."""

from __future__ import annotations

import uuid

from src.config import settings
from src.services.auth.session_jwt import mint_session_jwt
from tests.factories import UserFactory

_TTL = settings.auth.access_ttl_seconds


def _cookie(jwt: str) -> dict[str, str]:
    # The session cookie is named "session" in dev (no __Host- prefix over http).
    return {"Cookie": f"session={jwt}"}


async def test_valid_session_returns_profile(client, db_session) -> None:
    user = await UserFactory.create(db_session, email="me@rvaiglobal.com", display_name="Me")
    jwt = mint_session_jwt(user.id, user.token_version, _TTL)

    resp = await client.get("/v1/auth/me", headers=_cookie(jwt))
    assert resp.status_code == 200
    body = resp.json()
    assert body["id"] == str(user.id)
    assert body["email"] == "me@rvaiglobal.com"
    assert body["display_name"] == "Me"


async def test_no_cookie_returns_401(client) -> None:
    resp = await client.get("/v1/auth/me")
    assert resp.status_code == 401


async def test_malformed_cookie_returns_401_not_500(client) -> None:
    # Guards the AuthError->401 translation: an uncaught AuthError would 500 via
    # the composition-root catch-all.
    resp = await client.get("/v1/auth/me", headers=_cookie("not-a-jwt"))
    assert resp.status_code == 401


async def test_expired_jwt_returns_401(client, db_session) -> None:
    user = await UserFactory.create(db_session)
    expired = mint_session_jwt(user.id, user.token_version, ttl_seconds=-3600)
    resp = await client.get("/v1/auth/me", headers=_cookie(expired))
    assert resp.status_code == 401


async def test_unknown_user_returns_401(client) -> None:
    # A well-formed JWT for a user id that isn't in the DB.
    jwt = mint_session_jwt(uuid.uuid7(), 0, _TTL)
    resp = await client.get("/v1/auth/me", headers=_cookie(jwt))
    assert resp.status_code == 401


async def test_stale_token_version_returns_401(client, db_session) -> None:
    # Simulate a post-logout revocation: the JWT carries an older token_version
    # than the user's current value (KD-6).
    user = await UserFactory.create(db_session, token_version=1)
    stale = mint_session_jwt(user.id, 0, _TTL)  # minted against version 0
    resp = await client.get("/v1/auth/me", headers=_cookie(stale))
    assert resp.status_code == 401


async def test_me_response_exposes_no_secret_fields(client, db_session) -> None:
    user = await UserFactory.create(db_session, upn="secret-upn@rvaiglobal.com")
    jwt = mint_session_jwt(user.id, user.token_version, _TTL)
    resp = await client.get("/v1/auth/me", headers=_cookie(jwt))
    assert set(resp.json()) == {"id", "email", "display_name"}
