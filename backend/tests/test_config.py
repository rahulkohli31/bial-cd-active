"""Config fail-first startup-gate tests."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from src.config import Settings
from src.services.auth.config import AuthConfig

# A minimal valid AUTH__* block. `auth` is a required sub-model now, so every
# Settings constructed here needs one (a partial block fails its inner required
# fields — fail-first-python.md). session_secret is >= 32 chars (the validator).
_AUTH: dict[str, object] = {
    "tenant_id": "11111111-1111-1111-1111-111111111111",
    "client_id": "22222222-2222-2222-2222-222222222222",
    "client_secret": "unit-test-client-secret",
    "session_secret": "unit-test-session-secret-0123456789abcdef",
    "redirect_uri": "http://localhost:8000/api/v1/auth/callback",
}

# Minimal required fields so Settings validates without reading a real env file.
# model_validate runs full pydantic validation over the dict WITHOUT touching the
# env sources, so it avoids the typed-kwargs path ty/pyright cannot narrow.
_BASE_ENV: dict[str, object] = {
    "ENVIRONMENT": "development",
    "DATABASE_URL": "postgresql+asyncpg://u:p@localhost/test",
    "auth": _AUTH,
}


# A minimal valid object-store block, needed anywhere a production Settings is
# constructed (the prod gate requires storage in production). Azure Blob is the
# only provider.
_AZURE_STORE: dict[str, object] = {
    "provider": "azure",
    "account_url": "https://acct.blob.core.windows.net",
    "container": "b",
    "account_key": "a2V5",
}


def _settings(**overrides: object) -> Settings:
    return Settings.model_validate({**_BASE_ENV, **overrides})


def test_valid_settings_construct() -> None:
    s = _settings()
    assert s.ENVIRONMENT == "development"
    assert s.is_production is False
    # Optional knobs carry their defaults.
    assert s.FRONTEND_URL == "http://localhost:5173"


def test_is_production_true_in_production() -> None:
    # Production requires storage (the prod gate below), so supply it here.
    assert _settings(ENVIRONMENT="production", object_store=_AZURE_STORE).is_production is True


def test_production_requires_object_store() -> None:
    # Prod gate (fail-first-python.md): storage is optional in dev/test but the
    # single sanctioned optional-integration prod gate requires it in production.
    with pytest.raises(ValidationError):
        _settings(ENVIRONMENT="production")


def test_object_store_optional_in_development() -> None:
    # The same missing block is fine in development — it boots without storage.
    assert _settings().object_store is None


def test_environment_is_required() -> None:
    # Fail-first regression guard: ENVIRONMENT has NO default, so an absent env var
    # fails at Settings() construction. (Asserted on the field, not via a partial
    # model_validate, because pydantic-settings still backfills from the dotenv
    # source during validation.)
    assert Settings.model_fields["ENVIRONMENT"].is_required()


def test_database_url_is_required() -> None:
    assert Settings.model_fields["DATABASE_URL"].is_required()


def test_invalid_environment_literal_rejected() -> None:
    # The closed Literal rejects anything outside the three known environments.
    with pytest.raises(ValidationError):
        _settings(ENVIRONMENT="prod")


def test_unknown_key_forbidden() -> None:
    # extra="forbid": a typo'd env key crashes at startup instead of silently
    # falling back to a default.
    with pytest.raises(ValidationError):
        _settings(TOTALLY_BOGUS="x")


# --- Entra ID auth config (R21) ----------------------------------------------


def test_auth_is_required() -> None:
    # `auth` is an always-on required sub-model — no default. A missing AUTH__*
    # block fails at Settings() construction in every environment.
    assert Settings.model_fields["auth"].is_required()


@pytest.mark.parametrize(
    "field", ["tenant_id", "client_id", "client_secret", "session_secret", "redirect_uri"]
)
def test_auth_inner_fields_required(field: str) -> None:
    # Each Entra credential/id carries NO default: a partial AUTH__* block never
    # boots half-configured.
    assert AuthConfig.model_fields[field].is_required()


def test_auth_missing_inner_field_raises() -> None:
    # Validate AuthConfig DIRECTLY (a plain BaseModel, no env sources) so the
    # dropped key isn't silently backfilled from .env.test — the same reason the
    # ENVIRONMENT check above asserts on the field instead of a partial validate.
    partial = {k: v for k, v in _AUTH.items() if k != "tenant_id"}
    with pytest.raises(ValidationError):
        AuthConfig.model_validate(partial)


def test_auth_optional_ttls_default() -> None:
    auth = _settings().auth
    assert auth.access_ttl_seconds == 900
    assert auth.refresh_ttl_seconds == 604800
    assert auth.absolute_session_seconds == 28800
    assert auth.session_cookie_max_age == 600
    # None -> derive Secure from is_production at the cookie-setting site.
    assert auth.cookie_secure is None


def test_auth_unknown_key_forbidden() -> None:
    # extra="forbid" on the nested model too: a mistyped AUTH__* key fails fast.
    with pytest.raises(ValidationError):
        _settings(auth={**_AUTH, "totally_bogus": "x"})


def test_auth_secrets_are_masked() -> None:
    auth = _settings().auth
    # SecretStr masks in repr/str (never leaks into logs / ValidationError).
    assert "unit-test-client-secret" not in repr(auth.client_secret)
    assert "unit-test-session-secret" not in repr(auth)
    # ...but the plaintext is retrievable at the boundary.
    assert auth.client_secret.get_secret_value() == "unit-test-client-secret"


def test_auth_session_secret_min_length_enforced() -> None:
    with pytest.raises(ValidationError):
        _settings(auth={**_AUTH, "session_secret": "too-short"})


def test_auth_redirect_uri_must_be_absolute() -> None:
    with pytest.raises(ValidationError):
        _settings(auth={**_AUTH, "redirect_uri": "/v1/auth/callback"})


def test_auth_server_metadata_url_derived_from_tenant() -> None:
    auth = _settings().auth
    assert auth.server_metadata_url == (
        "https://login.microsoftonline.com/"
        "11111111-1111-1111-1111-111111111111/v2.0/.well-known/openid-configuration"
    )
