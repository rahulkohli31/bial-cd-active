"""Cookie name + Secure-prefix environment-awareness tests (KD-4).

`cookie_secure()` derives Secure from `settings.auth.cookie_secure` if set, else
`settings.is_production`. The `__Host-`/`__Secure-` prefixes require Secure, so
they must appear only when `cookie_secure()` is True and be absent otherwise.
"""

from __future__ import annotations

import pytest

import src.services.auth.cookies as cookies
from src.config import Settings

# A minimal valid AUTH__* block, mirroring tests/test_config.py.
_AUTH: dict[str, object] = {
    "tenant_id": "11111111-1111-1111-1111-111111111111",
    "client_id": "22222222-2222-2222-2222-222222222222",
    "client_secret": "unit-test-client-secret",
    "session_secret": "unit-test-session-secret-0123456789abcdef",
    "redirect_uri": "http://localhost:8000/api/v1/auth/callback",
}

# Production Settings requires object storage + redis + sandbox (the prod gates in
# src/config.py).
_AZURE_STORE: dict[str, object] = {
    "provider": "azure",
    "account_url": "https://acct.blob.core.windows.net",
    "container": "b",
    "account_key": "a2V5",
}

# `rediss://` — the production TLS gate in src/config.py refuses a plaintext DSN,
# and this block is only ever used to build a production Settings.
_REDIS: dict[str, object] = {"url": "rediss://cache.example.redis.cache.windows.net:6380/0"}

_SANDBOX: dict[str, object] = {
    "subscription_id": "00000000-0000-0000-0000-000000000000",
    "resource_group": "rg-citizen-dev",
    "region": "centralindia",
    "managed_environment_name": "bial-dev-aca-env",
    "acr_server": "bialgenaicr01.azurecr.io",
    "acr_username": "bialgenaicr01",
    "acr_password": "acr-admin-secret",
    "image_ref": "bialgenaicr01.azurecr.io/citizen-dev-sandbox:latest",
    "app_data_base_url": "https://portal.example/api",
}


def _settings(environment: str, **auth_overrides: object) -> Settings:
    # model_validate builds Settings directly from the dict, bypassing env
    # sources — the same pattern tests/test_config.py uses.
    payload: dict[str, object] = {
        "ENVIRONMENT": environment,
        "DATABASE_URL": "postgresql+asyncpg://u:p@localhost/test",
        "auth": {**_AUTH, **auth_overrides},
    }
    if environment == "production":
        payload["object_store"] = _AZURE_STORE
        payload["redis"] = _REDIS
        payload["sandbox"] = _SANDBOX
        # The FRONTEND_URL prod gate: production requires the portal's real https origin.
        payload["FRONTEND_URL"] = "https://portal.example"
    return Settings.model_validate(payload)


def test_production_cookie_names_carry_host_secure_prefixes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(cookies, "settings", _settings("production"))
    assert cookies.cookie_secure() is True
    assert cookies.session_cookie_name() == "__Host-session"
    assert cookies.refresh_cookie_name() == "__Secure-refresh"
    assert cookies.csrf_cookie_name() == "__Host-csrf"


def test_dev_cookie_names_are_unprefixed(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(cookies, "settings", _settings("development"))
    assert cookies.cookie_secure() is False
    assert cookies.session_cookie_name() == "session"
    assert cookies.refresh_cookie_name() == "refresh"
    assert cookies.csrf_cookie_name() == "csrf"


def test_explicit_cookie_secure_override_wins_over_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A non-prod https staging box: ENVIRONMENT=development but an explicit
    # AUTH__COOKIE_SECURE=true overrides the is_production-derived default.
    monkeypatch.setattr(cookies, "settings", _settings("development", cookie_secure=True))
    assert cookies.cookie_secure() is True
    assert cookies.session_cookie_name() == "__Host-session"
    assert cookies.refresh_cookie_name() == "__Secure-refresh"
    assert cookies.csrf_cookie_name() == "__Host-csrf"
