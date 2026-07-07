"""current_user + GET /auth/me — cookie auth, fail-closed 401s, revocation (U6)."""

from __future__ import annotations

import uuid

from src.config import settings
from src.db.models.user_limit import UserLimit
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
    # `is_admin` (derived hint) + `limits` (effective) are exposed; upn/token_version stay hidden.
    assert set(resp.json()) == {"id", "email", "display_name", "is_admin", "limits"}


async def test_me_limits_default_when_no_override(client, db_session) -> None:
    """No UserLimit row → the profile carries the global defaults (daily cap + 150k/200k)."""
    user = await UserFactory.create(db_session)
    jwt = mint_session_jwt(user.id, user.token_version, _TTL)
    resp = await client.get("/v1/auth/me", headers=_cookie(jwt))
    limits = resp.json()["limits"]
    assert limits["dailyTokenLimit"] == settings.DAILY_TOKEN_LIMIT
    assert limits["contextSoftLimit"] == 150_000
    assert limits["contextHardLimit"] == 200_000


async def test_me_limits_reflect_per_user_override(client, db_session) -> None:
    """A superadmin's per-user override must reach the client via /auth/me (the drop we fixed):
    the daily badge AND the per-conversation guardrail both read `limits` from this profile."""
    user = await UserFactory.create(db_session)
    db_session.add(
        UserLimit(
            user_id=user.id,
            daily_token_limit=2_000_000,
            context_soft_limit=120_000,
            context_hard_limit=180_000,
        )
    )
    await db_session.flush()
    jwt = mint_session_jwt(user.id, user.token_version, _TTL)
    resp = await client.get("/v1/auth/me", headers=_cookie(jwt))
    limits = resp.json()["limits"]
    assert limits["dailyTokenLimit"] == 2_000_000
    assert limits["contextSoftLimit"] == 120_000
    assert limits["contextHardLimit"] == 180_000


async def test_me_is_admin_false_for_citizen(client, db_session) -> None:
    # UserFactory's default email is not on the test SUPERADMIN_EMAILS allowlist.
    user = await UserFactory.create(db_session)
    jwt = mint_session_jwt(user.id, user.token_version, _TTL)
    resp = await client.get("/v1/auth/me", headers=_cookie(jwt))
    assert resp.json()["is_admin"] is False


async def test_me_is_admin_true_for_superadmin(client, db_session) -> None:
    # An email ON the test SUPERADMIN_EMAILS allowlist (admin@bial.com) → derived admin hint.
    user = await UserFactory.create(db_session, email="admin@bial.com")
    jwt = mint_session_jwt(user.id, user.token_version, _TTL)
    resp = await client.get("/v1/auth/me", headers=_cookie(jwt))
    assert resp.json()["is_admin"] is True
