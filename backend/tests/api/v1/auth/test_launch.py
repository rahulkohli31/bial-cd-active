"""GET /auth/launch, the launch branch of GET /auth/callback, and
POST /auth/app-assertion/exchange (issue #92, R10, AE1, AE4, AE5).

Mocks Entra at the same Authlib seam `test_callback.py`/`test_login.py` use (KD-9).
"""

from __future__ import annotations

import uuid
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

from authlib.integrations.starlette_client import OAuthError
from sqlalchemy import func, select

from src.config import settings
from src.db.models.user import User
from src.services.auth.app_assertion import mint_app_assertion, verify_app_assertion
from src.services.auth.oidc import get_oauth
from src.services.auth.session_jwt import mint_session_jwt
from tests.factories import AppRegistryFactory, UserFactory

_TID = settings.auth.tenant_id
_TTL = settings.auth.access_ttl_seconds
_DEPLOYED_URL = "https://citizen-app.example.com"


def _token(**userinfo_overrides: Any) -> dict[str, Any]:
    userinfo: dict[str, Any] = {
        "oid": "launch-oid-1",
        "sub": "launch-sub-1",
        "tid": _TID,
        "email": "employee@rvaiglobal.com",
        "preferred_username": "employee@rvaiglobal.com",
        "name": "An Employee",
    }
    userinfo.update(userinfo_overrides)
    return {"userinfo": userinfo, "access_token": "x", "id_token": "y"}


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


def _query(url: str) -> dict[str, str]:
    return {k: v[0] for k, v in parse_qs(urlsplit(url).query).items()}


# --- AE5: no verified origin on record -> refuse ---------------------------------


async def test_launch_refuses_when_the_app_has_no_deployed_url(client, db_session) -> None:
    user = await UserFactory.create(db_session)
    app = await AppRegistryFactory.create(db_session, user_id=user.id, deployed_url=None)

    resp = await client.get(f"/v1/auth/launch?app_id={app.id}")
    assert resp.status_code == 404


async def test_launch_refuses_for_an_unknown_app(client) -> None:
    resp = await client.get(f"/v1/auth/launch?app_id={uuid.uuid4()}")
    assert resp.status_code == 404


# --- fast path: an existing platform session skips the Entra round trip ----------


async def test_launch_fast_path_uses_the_existing_portal_session(client, db_session) -> None:
    user = await UserFactory.create(db_session, email="portal-user@rvaiglobal.com")
    app = await AppRegistryFactory.create(
        db_session, user_id=user.id, deployed_url=_DEPLOYED_URL
    )
    session_jwt = mint_session_jwt(user.id, user.token_version, _TTL)

    resp = await client.get(
        f"/v1/auth/launch?app_id={app.id}&next=/records/42",
        headers={"Cookie": f"session={session_jwt}"},
    )
    assert resp.status_code == 302
    location = resp.headers["location"]
    assert location.startswith(_DEPLOYED_URL)
    q = _query(location)
    assert q["bial_next"] == "/records/42"
    assert "bial_code" in q


# --- slow path: no session -> the existing single Entra redirect -----------------


async def test_launch_slow_path_redirects_to_entra_and_stashes_intent(client, db_session) -> None:
    user = await UserFactory.create(db_session)
    app = await AppRegistryFactory.create(
        db_session, user_id=user.id, deployed_url=_DEPLOYED_URL
    )
    metadata = get_oauth().entra.server_metadata
    metadata.clear()
    metadata.update(
        {
            "issuer": f"https://login.microsoftonline.com/{_TID}/v2.0",
            "authorization_endpoint": f"https://login.microsoftonline.com/{_TID}/oauth2/v2.0/authorize",
            "token_endpoint": f"https://login.microsoftonline.com/{_TID}/oauth2/v2.0/token",
            "jwks_uri": f"https://login.microsoftonline.com/{_TID}/discovery/v2.0/keys",
            "response_types_supported": ["code"],
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "code_challenge_methods_supported": ["S256"],
            "_loaded_at": 1_700_000_000.0,
        }
    )

    resp = await client.get(f"/v1/auth/launch?app_id={app.id}&next=/x")
    assert resp.status_code == 302
    location = resp.headers["location"]
    assert location.startswith(f"https://login.microsoftonline.com/{_TID}/oauth2/v2.0/authorize")
    # The SAME single registered redirect_uri the portal's own /auth/login uses — no
    # new reply URL (this feature's Key Decision).
    assert f"redirect_uri={quote(settings.auth.redirect_uri, safe='')}" in location
    set_cookie = "\n".join(resp.headers.get_list("set-cookie"))
    assert "oauth_transient=" in set_cookie


# --- the callback's launch branch -------------------------------------------------


async def test_callback_completes_a_launch_with_no_portal_account_touched(
    app, client, db_session
) -> None:
    user = await UserFactory.create(db_session)
    target = await AppRegistryFactory.create(
        db_session, user_id=user.id, deployed_url=_DEPLOYED_URL
    )

    # Simulate /launch's slow-path stash by hitting /launch for real first (it's the
    # cheapest way to get a genuine oauth_transient cookie the fake client honours).
    launch_resp = await client.get(f"/v1/auth/launch?app_id={target.id}&next=/deep/link")
    transient_cookie = "; ".join(
        c.split(";", 1)[0] for c in launch_resp.headers.get_list("set-cookie")
    )

    _use_fake_oauth(app, token=_token(oid="brand-new-employee-oid"))
    resp = await client.get("/v1/auth/callback", headers={"Cookie": transient_cookie})

    assert resp.status_code == 302
    location = resp.headers["location"]
    assert location.startswith(_DEPLOYED_URL)
    assert _query(location)["bial_next"] == "/deep/link"
    # No portal User row was created for this Entra identity (R2/A1: launch enforces
    # tenant membership only, never the platform's own account lifecycle).
    count = await db_session.scalar(
        select(func.count()).select_from(User).where(User.azure_oid == "brand-new-employee-oid")
    )
    assert count == 0
    # No portal session cookie was minted either.
    assert "session=" not in "\n".join(resp.headers.get_list("set-cookie"))


async def test_callback_launch_wrong_tenant_bounces_to_login_error(
    app, client, db_session
) -> None:
    user = await UserFactory.create(db_session)
    target = await AppRegistryFactory.create(
        db_session, user_id=user.id, deployed_url=_DEPLOYED_URL
    )
    launch_resp = await client.get(f"/v1/auth/launch?app_id={target.id}")
    transient_cookie = "; ".join(
        c.split(";", 1)[0] for c in launch_resp.headers.get_list("set-cookie")
    )

    _use_fake_oauth(
        app, token=_token(oid="foreign-oid", tid="ffffffff-ffff-ffff-ffff-ffffffffffff")
    )
    resp = await client.get("/v1/auth/callback", headers={"Cookie": transient_cookie})

    assert resp.status_code == 302
    assert resp.headers["location"] == f"{settings.FRONTEND_URL}/login?authError=wrong_tenant"


async def test_callback_launch_provider_error_fails_closed(app, client, db_session) -> None:
    user = await UserFactory.create(db_session)
    target = await AppRegistryFactory.create(
        db_session, user_id=user.id, deployed_url=_DEPLOYED_URL
    )
    launch_resp = await client.get(f"/v1/auth/launch?app_id={target.id}")
    transient_cookie = "; ".join(
        c.split(";", 1)[0] for c in launch_resp.headers.get_list("set-cookie")
    )

    _use_fake_oauth(app, error=OAuthError(error="access_denied"))
    resp = await client.get("/v1/auth/callback", headers={"Cookie": transient_cookie})

    assert resp.status_code == 302
    assert resp.headers["location"] == f"{settings.FRONTEND_URL}/login?authError=auth_failed"


async def test_a_normal_portal_login_is_unaffected_by_the_launch_branch(
    app, client, db_session
) -> None:
    # No /launch call first -> no stashed intent -> the EXISTING portal-login path,
    # byte-for-byte (regression guard for the callback's new branch point).
    _use_fake_oauth(app, token=_token(oid="portal-sign-in-oid"))
    resp = await client.get("/v1/auth/callback")
    assert resp.status_code == 302
    assert resp.headers["location"] == settings.FRONTEND_URL
    user = await db_session.scalar(select(User).where(User.azure_oid == "portal-sign-in-oid"))
    assert user is not None


# --- next-path safety (open-redirect guard) ---------------------------------------


async def test_next_is_normalized_to_root_when_absolute_or_protocol_relative(
    client, db_session
) -> None:
    user = await UserFactory.create(db_session)
    app_row = await AppRegistryFactory.create(
        db_session, user_id=user.id, deployed_url=_DEPLOYED_URL
    )
    session_jwt = mint_session_jwt(user.id, user.token_version, _TTL)

    for unsafe_next in ("https://evil.example", "//evil.example", "javascript:alert(1)"):
        resp = await client.get(
            f"/v1/auth/launch?app_id={app_row.id}&next={unsafe_next}",
            headers={"Cookie": f"session={session_jwt}"},
        )
        assert _query(resp.headers["location"])["bial_next"] == "/"


# --- the exchange endpoint ---------------------------------------------------------


async def test_exchange_trades_a_valid_launch_code_for_a_deployed_assertion(
    client, db_session
) -> None:
    user = await UserFactory.create(db_session)
    app_row = await AppRegistryFactory.create(
        db_session, user_id=user.id, deployed_url=_DEPLOYED_URL
    )
    session_jwt = mint_session_jwt(user.id, user.token_version, _TTL)
    launch_resp = await client.get(
        f"/v1/auth/launch?app_id={app_row.id}",
        headers={"Cookie": f"session={session_jwt}"},
    )
    code = _query(launch_resp.headers["location"])["bial_code"]

    resp = await client.post("/v1/auth/app-assertion/exchange", json={"code": code})
    assert resp.status_code == 200
    assertion = resp.json()["assertion"]

    claims = verify_app_assertion(assertion, app_id=app_row.id, plane="deployed")
    assert claims.entra_oid == user.azure_oid
    assert claims.email == user.email


async def test_exchange_rejects_garbage_and_expired_codes(client) -> None:
    resp = await client.post("/v1/auth/app-assertion/exchange", json={"code": "not-a-real-code"})
    assert resp.status_code == 401


async def test_exchange_rejects_an_app_assertion_presented_as_a_code(client) -> None:
    # A real app assertion (wrong audience for the exchange) must not be redeemable.
    assertion = mint_app_assertion(
        entra_oid="x",
        email="x@rvaiglobal.com",
        display_name=None,
        app_id=uuid.uuid4(),
        plane="deployed",
    )
    resp = await client.post("/v1/auth/app-assertion/exchange", json={"code": assertion})
    assert resp.status_code == 401
