"""GET /auth/sandbox/login + /auth/sandbox/callback — the A1 sandbox login broker.

Mirrors `test_login.py` / `test_callback.py`'s Entra-mocking pattern (KD-9): `get_oauth`
is overridden with a fake whose `authorize_access_token` returns a crafted token dict (or
raises), so the full broker logic runs with no live tenant. `session_manager_dependency`
and `sandbox_or_none_dependency` are ALSO overridden here — this router resolves a live
sandbox handle, not the portal's own session/CSRF machinery, so what it needs stubbed is a
fake manager + a "is the sandbox configured" toggle, not a DB-backed user.
"""

from __future__ import annotations

import uuid
from typing import Any
from urllib.parse import unquote

from authlib.integrations.starlette_client import OAuthError

from src.api.v1.build_sessions.deps import sandbox_or_none_dependency, session_manager_dependency
from src.config import settings
from src.services.auth.oidc import get_oauth
from src.services.auth.sandbox_handoff import verify_sandbox_handoff_token
from src.services.sandbox.base import SandboxHandle

_TID = settings.auth.tenant_id
_APP_ID = uuid.uuid4()

_DISCOVERY = {
    "issuer": f"https://login.microsoftonline.com/{_TID}/v2.0",
    "authorization_endpoint": f"https://login.microsoftonline.com/{_TID}/oauth2/v2.0/authorize",
    "token_endpoint": f"https://login.microsoftonline.com/{_TID}/oauth2/v2.0/token",
    "jwks_uri": f"https://login.microsoftonline.com/{_TID}/discovery/v2.0/keys",
    "response_types_supported": ["code"],
    "grant_types_supported": ["authorization_code", "refresh_token"],
    "code_challenge_methods_supported": ["S256"],
    "_loaded_at": 1_700_000_000.0,
}

HANDLE = SandboxHandle(
    fqdn="app-x.westeurope.azurecontainerapps.io",
    token="sbx-token-xyz",
    app_name="app-x",
    preview_url="https://app-x.westeurope.azurecontainerapps.io/",
    ready=True,
)


def _seed_discovery() -> None:
    metadata = get_oauth().entra.server_metadata
    metadata.clear()
    metadata.update(_DISCOVERY)


def _token(**userinfo_overrides: Any) -> dict[str, Any]:
    userinfo: dict[str, Any] = {
        "oid": "sandbox-viewer-oid",
        "sub": "sandbox-viewer-sub",
        "tid": _TID,
        "email": "citizen@rvaiglobal.com",
        "preferred_username": "citizen@rvaiglobal.com",
    }
    userinfo.update(userinfo_overrides)
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


class _StubManager:
    def __init__(self, handle: SandboxHandle | None) -> None:
        self._handle = handle

    async def live_handle_for_app(self, db: Any, app_id: uuid.UUID, sandbox_client: Any) -> Any:
        return self._handle


def _use_sandbox(app: Any, *, configured: bool, handle: SandboxHandle | None = None) -> None:
    app.dependency_overrides[sandbox_or_none_dependency] = (
        lambda: object() if configured else None
    )
    app.dependency_overrides[session_manager_dependency] = lambda: _StubManager(handle)


async def test_login_redirects_to_entra_and_stashes_the_app_id(app, client) -> None:
    _seed_discovery()
    resp = await client.get(f"/v1/auth/sandbox/login?app_id={_APP_ID}")

    assert resp.status_code == 302
    location = resp.headers["location"]
    assert location.startswith(f"https://login.microsoftonline.com/{_TID}/oauth2/v2.0/authorize")
    assert "code_challenge=" in location
    set_cookie = "\n".join(resp.headers.get_list("set-cookie"))
    assert "oauth_transient=" in set_cookie  # the SAME transient cookie Authlib PKCE state uses
    assert "session=" not in set_cookie  # never the portal's own app session cookie


async def test_callback_without_a_stashed_app_id_is_400(app, client) -> None:
    _use_fake_oauth(app, token=_token())
    resp = await client.get("/v1/auth/sandbox/callback")
    assert resp.status_code == 400


async def test_callback_wrong_tenant_is_403(app, client) -> None:
    _seed_discovery()
    await client.get(f"/v1/auth/sandbox/login?app_id={_APP_ID}")
    _use_fake_oauth(app, token=_token(tid="ffffffff-ffff-ffff-ffff-ffffffffffff"))
    _use_sandbox(app, configured=True, handle=HANDLE)

    resp = await client.get("/v1/auth/sandbox/callback")
    assert resp.status_code == 403


async def test_callback_provider_error_fails_closed_not_500(app, client) -> None:
    _seed_discovery()
    await client.get(f"/v1/auth/sandbox/login?app_id={_APP_ID}")
    _use_fake_oauth(app, error=OAuthError(error="access_denied"))

    resp = await client.get("/v1/auth/sandbox/callback")
    assert resp.status_code == 401


async def test_callback_with_sandbox_unconfigured_is_503(app, client) -> None:
    _seed_discovery()
    await client.get(f"/v1/auth/sandbox/login?app_id={_APP_ID}")
    _use_fake_oauth(app, token=_token())
    _use_sandbox(app, configured=False)

    resp = await client.get("/v1/auth/sandbox/callback")
    assert resp.status_code == 503


async def test_callback_with_no_live_sandbox_is_409(app, client) -> None:
    _seed_discovery()
    await client.get(f"/v1/auth/sandbox/login?app_id={_APP_ID}")
    _use_fake_oauth(app, token=_token())
    _use_sandbox(app, configured=True, handle=None)

    resp = await client.get("/v1/auth/sandbox/callback")
    assert resp.status_code == 409


async def test_callback_success_hands_off_to_the_sandbox_with_a_verifiable_token(
    app, client
) -> None:
    _seed_discovery()
    await client.get(f"/v1/auth/sandbox/login?app_id={_APP_ID}")
    _use_fake_oauth(app, token=_token())
    _use_sandbox(app, configured=True, handle=HANDLE)

    resp = await client.get("/v1/auth/sandbox/callback")

    assert resp.status_code == 302
    location = resp.headers["location"]
    assert location.startswith(f"{HANDLE.preview_url}_sup/auth/complete?token=")
    raw_token = unquote(location.split("token=", 1)[1])
    # Verifiable with the TARGET sandbox's own token — and only that sandbox's token.
    assert verify_sandbox_handoff_token(raw_token, HANDLE.token) == str(_APP_ID)
