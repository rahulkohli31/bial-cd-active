"""GET /auth/callback — fail-closed validation, provisioning, session mint (U5).

Entra is mocked at the Authlib seam (KD-9): `get_oauth` is overridden with a fake
whose `authorize_access_token` returns a crafted token dict (or raises), so the
full callback logic runs with no live tenant.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from authlib.integrations.starlette_client import OAuthError
from sqlalchemy import func, select

from src.config import settings
from src.db.models.refresh_token import RefreshToken
from src.db.models.user import User
from src.services.auth.oidc import get_oauth
from src.services.auth.refresh import hash_refresh_token
from tests.factories import UserFactory

_TID = settings.auth.tenant_id
_ABSENT = object()


def _token(**userinfo_overrides: Any) -> dict[str, Any]:
    userinfo: dict[str, Any] = {
        "oid": "entra-oid-new",
        "sub": "entra-sub-new",
        "tid": _TID,
        "email": "citizen@rvaiglobal.com",
        "preferred_username": "citizen@rvaiglobal.com",
        "name": "A Citizen",
    }
    userinfo.update(userinfo_overrides)
    userinfo = {k: v for k, v in userinfo.items() if v is not _ABSENT}
    # The access/id tokens are present in the response but MUST NOT be persisted.
    return {
        "userinfo": userinfo,
        "access_token": "entra-access-secret",
        "id_token": "entra-id-jwt",
    }


class _FakeEntra:
    def __init__(self, *, token: dict[str, Any] | None, error: Exception | None) -> None:
        self._token = token
        self._error = error

    async def authorize_access_token(self, request: Any) -> dict[str, Any]:
        if self._error is not None:
            raise self._error
        assert self._token is not None
        return self._token


class _FakeOAuth:
    def __init__(self, entra: _FakeEntra) -> None:
        self.entra = entra


def _use_fake_oauth(
    app: Any, *, token: dict[str, Any] | None = None, error: Exception | None = None
) -> None:
    app.dependency_overrides[get_oauth] = lambda: _FakeOAuth(_FakeEntra(token=token, error=error))


def _set_cookies(resp: httpx.Response) -> dict[str, str]:
    return {raw.split("=", 1)[0].strip(): raw for raw in resp.headers.get_list("set-cookie")}


def _cookie_value(raw: str) -> str:
    return raw.split("=", 1)[1].split(";", 1)[0]


# --- provisioning (AE5) --------------------------------------------------------


async def test_first_signin_provisions_user_and_sets_cookies(app, client, db_session) -> None:
    _use_fake_oauth(app, token=_token(oid="brand-new-oid"))
    resp = await client.get("/v1/auth/callback")

    assert resp.status_code == 302
    assert resp.headers["location"] == settings.FRONTEND_URL

    user = await db_session.scalar(select(User).where(User.azure_oid == "brand-new-oid"))
    assert user is not None
    assert user.email == "citizen@rvaiglobal.com"
    assert user.upn == "citizen@rvaiglobal.com"
    assert user.token_version == 0

    # Exactly one refresh-token family row for the new user.
    token_count = await db_session.scalar(
        select(func.count()).select_from(RefreshToken).where(RefreshToken.user_id == user.id)
    )
    assert token_count == 1

    cookies = _set_cookies(resp)
    assert {"session", "refresh", "csrf"} <= set(cookies)


async def test_returning_signin_updates_profile_preserves_token_version(
    app, client, db_session
) -> None:
    existing = await UserFactory.create(
        db_session, azure_oid="returning-oid", email="old@rvaiglobal.com", token_version=5
    )
    _use_fake_oauth(app, token=_token(oid="returning-oid", email="new@rvaiglobal.com"))
    resp = await client.get("/v1/auth/callback")

    assert resp.status_code == 302
    # No second row; the same user is updated in place.
    count = await db_session.scalar(
        select(func.count()).select_from(User).where(User.azure_oid == "returning-oid")
    )
    assert count == 1
    await db_session.refresh(existing)
    assert existing.email == "new@rvaiglobal.com"
    assert existing.token_version == 5  # revocation state preserved


# --- fail-closed paths (AE1 / AE4) ---------------------------------------------


async def test_wrong_tenant_redirects_to_login_error(app, client, db_session) -> None:
    _use_fake_oauth(
        app, token=_token(oid="foreign-oid", tid="ffffffff-ffff-ffff-ffff-ffffffffffff")
    )
    resp = await client.get("/v1/auth/callback")

    assert resp.status_code == 302
    assert resp.headers["location"] == f"{settings.FRONTEND_URL}/login?authError=wrong_tenant"
    assert resp.headers.get_list("set-cookie") == []  # no session minted
    assert await db_session.scalar(select(User).where(User.azure_oid == "foreign-oid")) is None


async def test_missing_userinfo_redirects_to_login_error(app, client, db_session) -> None:
    _use_fake_oauth(app, token={"access_token": "x"})  # no userinfo -> unvalidated
    resp = await client.get("/v1/auth/callback")

    assert resp.status_code == 302
    assert resp.headers["location"] == f"{settings.FRONTEND_URL}/login?authError=invalid_callback"
    assert resp.headers.get_list("set-cookie") == []
    assert await db_session.scalar(select(func.count()).select_from(User)) == 0


async def test_cancelled_consent_does_not_500(app, client) -> None:
    _use_fake_oauth(app, error=OAuthError(error="access_denied"))
    resp = await client.get("/v1/auth/callback")

    assert resp.status_code == 302
    assert resp.headers["location"] == f"{settings.FRONTEND_URL}/login?authError=auth_failed"
    assert resp.headers.get_list("set-cookie") == []


# --- optional-email handling (KD-3) --------------------------------------------


async def test_missing_email_provisions_via_preferred_username(app, client, db_session) -> None:
    _use_fake_oauth(app, token=_token(oid="no-email-oid", email=_ABSENT))
    resp = await client.get("/v1/auth/callback")

    assert resp.status_code == 302
    assert resp.headers["location"] == settings.FRONTEND_URL
    user = await db_session.scalar(select(User).where(User.azure_oid == "no-email-oid"))
    assert user is not None
    assert user.email == "citizen@rvaiglobal.com"  # fell back to preferred_username


async def test_missing_email_and_upn_rejected(app, client, db_session) -> None:
    _use_fake_oauth(app, token=_token(oid="nada-oid", email=_ABSENT, preferred_username=_ABSENT))
    resp = await client.get("/v1/auth/callback")

    assert resp.status_code == 302
    assert resp.headers["location"] == f"{settings.FRONTEND_URL}/login?authError=invalid_callback"
    assert await db_session.scalar(select(User).where(User.azure_oid == "nada-oid")) is None


# --- cookie attributes + no-token-persistence ----------------------------------


async def test_cookie_attributes_follow_the_matrix(app, client) -> None:
    _use_fake_oauth(app, token=_token(oid="cookie-oid"))
    resp = await client.get("/v1/auth/callback")
    cookies = _set_cookies(resp)

    # session: HttpOnly, root path.
    assert "HttpOnly" in cookies["session"]
    assert "Path=/;" in cookies["session"] or cookies["session"].rstrip().endswith("Path=/")
    # refresh: HttpOnly, path-scoped to the refresh endpoint, NO Domain (host-only).
    assert "HttpOnly" in cookies["refresh"]
    assert "Path=/api/v1/auth/refresh" in cookies["refresh"]
    assert "Domain=" not in cookies["refresh"]
    # csrf: readable by JS -> NOT HttpOnly.
    assert "HttpOnly" not in cookies["csrf"]


async def test_only_refresh_hash_is_persisted_not_entra_tokens(app, client, db_session) -> None:
    _use_fake_oauth(app, token=_token(oid="hash-oid"))
    resp = await client.get("/v1/auth/callback")

    user = await db_session.scalar(select(User).where(User.azure_oid == "hash-oid"))
    assert user is not None
    row = await db_session.scalar(select(RefreshToken).where(RefreshToken.user_id == user.id))
    assert row is not None
    # The stored hash matches the raw cookie token -> only the hash is at rest, and
    # it is NOT either Entra token.
    raw_refresh = _cookie_value(_set_cookies(resp)["refresh"])
    assert row.token_hash == hash_refresh_token(raw_refresh)
    assert row.token_hash not in ("entra-access-secret", "entra-id-jwt")


@pytest.mark.parametrize("field", ["oid", "sub"])
async def test_missing_oid_or_sub_rejected(app, client, field: str) -> None:
    _use_fake_oauth(app, token=_token(**{field: _ABSENT}))
    resp = await client.get("/v1/auth/callback")
    assert resp.headers["location"] == f"{settings.FRONTEND_URL}/login?authError=invalid_callback"
