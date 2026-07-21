"""Config fail-first startup-gate tests."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.config import FoundryConfig, Settings
from src.services.appdb.config import AppDatabaseSettings
from src.services.auth.config import AuthConfig
from src.services.redis.config import RedisConfig

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
    # superadmin_emails is required (no default) — every Settings built here needs it.
    "superadmin_emails": ["admin@bial.com"],
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

# Minimal valid REDIS__* / SANDBOX__* blocks, needed anywhere a production Settings
# is constructed (the prod gates require both in production, alongside storage).
# TLS scheme: `_prod_settings` must clear the production rediss:// gate too, so the
# shared prod block carries the Azure-shaped DSN. Tests probing the gate override it.
_REDIS: dict[str, object] = {
    "url": "rediss://cache.example.redis.cache.windows.net:6380/0",
}

_SANDBOX: dict[str, object] = {
    "subscription_id": "00000000-0000-0000-0000-000000000000",
    "resource_group": "rg-citizen-dev",
    "region": "centralindia",
    "managed_environment_name": "bial-dev-aca-env",
    "acr_server": "bialgenaicr01.azurecr.io",
    "acr_username": "bialgenaicr01",
    "acr_password": "acr-admin-secret",
    "image_ref": "bialgenaicr01.azurecr.io/citizen-dev-sandbox:latest",
}

# Minimal valid APP_DB__* block (per-project databases, ADR-0028) — the fourth
# optional-integration prod gate. Both inner fields are required, no-default: the
# maintenance DSN points at a NEUTRAL maintenance database, and the key is a real
# urlsafe-base64 32-byte Fernet key (Fernet validates it at construction, so a made-up
# string would fail the first encrypt rather than here).
_APP_DB: dict[str, object] = {
    "maintenance_dsn": "postgresql+asyncpg://maint:maint-secret@db.example:5432/postgres",
    "encryption_key": "hcaMBs3ozLPY6Ekz7VOr09gDdZ5w6EUUQhbEmr0iJHY=",
}


def _settings(**overrides: object) -> Settings:
    return Settings.model_validate({**_BASE_ENV, **overrides})


def _prod_settings(**overrides: object) -> Settings:
    # A production Settings must clear every optional-integration prod gate at once
    # (storage + redis + sandbox + app_db), so this helper always supplies all four.
    # Tests that probe a single gate override just that one (e.g. `object_store=None`)
    # so the intended gate — not an incidental one — is what fires.
    prod: dict[str, object] = {
        "ENVIRONMENT": "production",
        "object_store": _AZURE_STORE,
        "redis": _REDIS,
        "sandbox": _SANDBOX,
        "app_db": _APP_DB,
        # The FRONTEND_URL prod gate rejects the localhost dev default in production, so a
        # production Settings always supplies the real https origin (tests that probe the
        # gate override it back).
        "FRONTEND_URL": "https://portal.example",
    }
    return _settings(**{**prod, **overrides})


def test_valid_settings_construct() -> None:
    s = _settings()
    assert s.ENVIRONMENT == "development"
    assert s.is_production is False
    # Optional knobs carry their defaults.
    assert s.FRONTEND_URL == "http://localhost:5173"


def test_is_production_true_in_production() -> None:
    # Production requires storage + redis + sandbox (the prod gates), so supply all.
    assert _prod_settings().is_production is True


def test_production_requires_object_store() -> None:
    # Prod gate (fail-first-python.md): storage is optional in dev/test but the
    # single sanctioned optional-integration prod gate requires it in production.
    # redis/sandbox are supplied so the STORAGE gate is the one that fires.
    with pytest.raises(ValidationError):
        _prod_settings(object_store=None)


def test_production_requires_redis() -> None:
    # Same optional-with-prod-gate shape as storage: redis is optional in dev/test
    # but required in production (it coordinates the sandbox lock/heartbeat/registry).
    with pytest.raises(ValidationError, match="redis must be configured in production"):
        _prod_settings(redis=None)


def test_production_requires_sandbox() -> None:
    # The per-user sandbox runtime is optional in dev/test but required in production
    # (no build loop without it).
    with pytest.raises(ValidationError, match="sandbox must be configured in production"):
        _prod_settings(sandbox=None)


def test_production_requires_app_db() -> None:
    # Per-project databases ARE the generated apps' isolation boundary in production
    # (ADR-0028): booting prod without a maintenance credential would create projects
    # that silently never get a database. Same optional-with-prod-gate shape as the
    # three above; the others are supplied so the APP_DB gate is the one that fires.
    with pytest.raises(ValidationError, match="per-project databases must be configured"):
        _prod_settings(app_db=None)


def test_app_db_optional_outside_production() -> None:
    # The fixture-off state is a SUPPORTED deployment, not an oversight: with no APP_DB
    # block the provisioner no-ops and projects work without a database. Asserted as an
    # explicit None override plus the field default, because unlike redis/sandbox the
    # suite's own `.env.test` DOES configure APP_DB (the provisioning tests need a real
    # substrate) and `model_validate` still merges the ambient env sources.
    assert Settings.model_fields["app_db"].default is None
    assert _settings(app_db=None).app_db is None


def test_app_db_block_validates_when_present() -> None:
    s = _settings(app_db=_APP_DB)
    assert s.app_db is not None
    # Knobs carry their defined defaults.
    assert s.app_db.sandbox_dsn_host is None
    assert s.app_db.statement_timeout_ms == 30_000
    assert s.app_db.idle_in_transaction_timeout_ms == 60_000


def test_app_db_inner_fields_required() -> None:
    # Fail-first: neither the maintenance credential nor the at-rest key has a default —
    # a placeholder would mean "provision with no credential" or "store passwords in clear".
    for field in ("maintenance_dsn", "encryption_key"):
        assert AppDatabaseSettings.model_fields[field].is_required()


def test_app_db_rejects_unknown_nested_key() -> None:
    # extra="forbid" is per-sub-model, not inherited: a mistyped APP_DB__* key must fail
    # at startup rather than being silently absorbed.
    with pytest.raises(ValidationError):
        _settings(app_db={**_APP_DB, "bogus": "x"})


def test_app_db_secrets_are_masked() -> None:
    s = _prod_settings()
    assert s.app_db is not None
    rendered = repr(s.app_db)
    assert "maint-secret" not in rendered
    assert "hcaMBs3ozLPY6Ekz7VOr09gDdZ5w6EUUQhbEmr0iJHY=" not in rendered


def test_app_db_gate_message_never_leaks_the_maintenance_dsn() -> None:
    # The gate fires while the block is ABSENT, but a neighbouring validator failure must
    # not echo a supplied DSN either — pydantic reflects validator messages into
    # ValidationError, and thus into logs. STATIC messages only.
    dsn = "postgresql+asyncpg://maint:sup3r-secret-maint-pw@db.example:5432/postgres"
    with pytest.raises(ValidationError) as caught:
        _prod_settings(app_db={**_APP_DB, "maintenance_dsn": dsn}, object_store=None)
    rendered = str(caught.value)
    assert "sup3r-secret-maint-pw" not in rendered


def test_redis_and_sandbox_optional_in_development() -> None:
    # The whole point of D2: dev/test boot with NO REDIS__*/SANDBOX__* env — the
    # existing auth/chat/runner suite must not need new configuration.
    s = _settings()
    assert s.redis is None
    assert s.sandbox is None


def test_production_boots_with_redis_and_sandbox() -> None:
    s = _prod_settings()
    assert s.redis is not None
    assert s.sandbox is not None
    assert s.sandbox.image_ref == "bialgenaicr01.azurecr.io/citizen-dev-sandbox:latest"


def test_redis_rejects_unknown_nested_key() -> None:
    # extra="forbid": a mistyped REDIS__* key fails at startup (no silent absorption).
    with pytest.raises(ValidationError):
        _settings(redis={**_REDIS, "bogus": "x"})


def test_sandbox_rejects_unknown_nested_key() -> None:
    with pytest.raises(ValidationError):
        _settings(sandbox={**_SANDBOX, "bogus": "x"})


def test_sandbox_requires_acr_credentials() -> None:
    # Fail-first: ACR pull auth is an always-on dependency of a configured sandbox (ACA
    # cannot pull the private image without a registries credential), so the creds carry
    # NO default — a SANDBOX block missing them fails at construction, never boots
    # half-configured able to provision a container that can't pull its own image.
    without_acr = {
        k: v
        for k, v in _SANDBOX.items()
        if k not in {"acr_server", "acr_username", "acr_password"}
    }
    with pytest.raises(ValidationError):
        _settings(sandbox=without_acr)


def test_sandbox_acr_password_is_masked() -> None:
    # SecretStr on the ACR pull password — repr must not leak the admin credential.
    s = _prod_settings()
    assert s.sandbox is not None
    assert "acr-admin-secret" not in repr(s.sandbox)


def test_redis_url_is_masked() -> None:
    # SecretStr on the DSN — repr must not leak the URL (it may embed a password).
    s = _prod_settings()
    assert s.redis is not None
    assert "cache.example.redis.cache.windows.net" not in repr(s.redis)


# --- Redis TLS production gate (KD-4) ----------------------------------------


def test_plaintext_redis_is_fine_outside_production() -> None:
    # The gate is is_production-only: local dev on a Homebrew Redis stays a no-op
    # pass. `rediss://` against plaintext local Redis does NOT fail fast — it blocks
    # for the whole connect timeout and then raises a misleading TimeoutError — so
    # forcing TLS everywhere would be actively worse than useless.
    s = _settings(redis={"url": "redis://localhost:6379/0"})
    assert s.redis is not None
    assert s.redis.url.get_secret_value() == "redis://localhost:6379/0"


def test_production_requires_tls_redis_url() -> None:
    # TLS is carried by the SCHEME (no TLS kwargs anywhere in the pool factory), so
    # this validator is the only thing standing between production and a plaintext
    # coordination channel.
    with pytest.raises(ValidationError, match="rediss://"):
        _prod_settings(redis={"url": "redis://cache.example.com:6379/0"})


def test_production_tls_gate_message_never_leaks_the_dsn() -> None:
    # The DSN may embed an access key, and pydantic reflects validator messages into
    # ValidationError (and thus into logs). STATIC message only.
    dsn = "redis://:sup3r-secret-access-key@cache.example.com:6379/0"
    with pytest.raises(ValidationError) as caught:
        _prod_settings(redis={"url": dsn})
    rendered = str(caught.value)
    assert dsn not in rendered
    assert "sup3r-secret-access-key" not in rendered


def test_production_accepts_tls_redis_url() -> None:
    s = _prod_settings(redis={"url": "rediss://cache.example.com:6380/0"})
    assert s.redis is not None
    assert s.redis.url.get_secret_value().startswith("rediss://")


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


# --- Daily token limit (R13/R30) ---------------------------------------------


def test_daily_token_limit_default() -> None:
    # Optional knob with a defined default (the standard plan) — mirrors Express.
    assert _settings().DAILY_TOKEN_LIMIT == 1_000_000


def test_daily_token_limit_rejects_nonpositive() -> None:
    # PositiveInt: a zero/negative cap is a misconfiguration and fails at startup.
    with pytest.raises(ValidationError):
        _settings(DAILY_TOKEN_LIMIT=0)


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


# --- Super-admin allowlist (R7) ----------------------------------------------


def test_superadmin_emails_required() -> None:
    # No default (fail-first): a missing SUPERADMIN_EMAILS fails at construction.
    assert Settings.model_fields["superadmin_emails"].is_required()


def test_superadmin_emails_normalized_from_list() -> None:
    # Surrounding whitespace + mixed case normalize to a lowercased frozenset.
    s = _settings(superadmin_emails=[" Admin@BIAL.com ", "SUPER@bial.com"])
    assert s.superadmin_emails == frozenset({"admin@bial.com", "super@bial.com"})


def test_superadmin_emails_parsed_from_comma_string() -> None:
    # The env path is a plain comma-separated string (NoDecode disables JSON parse);
    # blanks and trailing commas are dropped.
    s = _settings(superadmin_emails="a@bial.com, b@bial.com ,")
    assert s.superadmin_emails == frozenset({"a@bial.com", "b@bial.com"})


def test_superadmin_emails_reject_non_iterable() -> None:
    with pytest.raises(ValidationError):
        _settings(superadmin_emails=123)


@pytest.mark.parametrize("blank", ["", "   ", ",", " , ,", []])
def test_superadmin_emails_reject_empty_allowlist(blank: object) -> None:
    # A control-plane with ZERO super-admins is a misconfiguration in EVERY environment, not a
    # permissive default: nobody could approve an app or suspend a user. The field comment
    # already claimed a missing value fails at construction — an empty one now does too.
    with pytest.raises(ValidationError):
        _settings(superadmin_emails=blank)


# --- FRONTEND_URL production gate (feeds the sandbox frame-ancestors CSP, C8) --


def test_frontend_url_localhost_default_rejected_in_production() -> None:
    # Production booting with the :5173 dev default would mis-scope the sandbox
    # frame-ancestors CSP (BIAL_PORTAL_ORIGIN) — refuse to boot instead.
    with pytest.raises(ValidationError, match="FRONTEND_URL"):
        _prod_settings(FRONTEND_URL="http://localhost:5173")


def test_frontend_url_non_https_rejected_in_production() -> None:
    with pytest.raises(ValidationError, match="FRONTEND_URL"):
        _prod_settings(FRONTEND_URL="http://portal.example")


def test_frontend_url_https_boots_in_production() -> None:
    assert _prod_settings().FRONTEND_URL == "https://portal.example"


def test_frontend_url_default_fine_outside_production() -> None:
    # The dev default stays legitimate for two-process local dev (the gate is prod-only).
    assert _settings().FRONTEND_URL == "http://localhost:5173"


# --- Session-cookie hardening in production (fail-closed) ---------------------


def test_cookie_secure_false_in_production_raises() -> None:
    # An explicit override would drop `Secure` AND the `__Host-`/`__Secure-` prefixes, so the
    # session cookie would ride plain http. Refuse to boot rather than run degraded.
    # redis/sandbox supplied so the COOKIE gate is the one that fires, not an
    # incidental storage/redis/sandbox gate.
    with pytest.raises(ValidationError):
        _prod_settings(auth={**_AUTH, "cookie_secure": False})


def test_cookie_secure_true_or_unset_in_production_boots() -> None:
    unset = _prod_settings()
    assert unset.auth.cookie_secure is None  # derived from is_production at the cookie site
    explicit = _prod_settings(auth={**_AUTH, "cookie_secure": True})
    assert explicit.auth.cookie_secure is True


@pytest.mark.parametrize("environment", ["development", "staging"])
def test_cookie_secure_false_outside_production_boots(environment: str) -> None:
    # Local/staging dev over plain http is legitimate — the guard is production-only.
    assert (
        _settings(ENVIRONMENT=environment, auth={**_AUTH, "cookie_secure": False}).auth is not None
    )


# --- Foundry (optional integration) + Gotenberg (optional knob) --------------


def test_foundry_optional_defaults_none() -> None:
    # Genuinely-optional: dev/test boot without a Foundry block.
    assert _settings().foundry is None


def test_gotenberg_url_optional_defaults_none() -> None:
    # None has a DEFINED meaning: deck conversion disabled.
    assert _settings().GOTENBERG_URL is None


def test_foundry_block_validates_when_present() -> None:
    s = _settings(
        foundry={"resource": "r", "deployment": "d", "auth_mode": "api_key", "api_key": "k"}
    )
    assert s.foundry is not None
    assert s.foundry.resource == "r"
    assert s.foundry.deployment == "d"
    assert s.foundry.api_key is not None
    assert s.foundry.api_key.get_secret_value() == "k"


def test_foundry_api_key_required_in_key_mode() -> None:
    # api_key mode without a key fails the cross-field validator (fail-first).
    with pytest.raises(ValidationError):
        _settings(foundry={"resource": "r", "deployment": "d", "auth_mode": "api_key"})


def test_foundry_entra_mode_needs_no_key() -> None:
    s = _settings(foundry={"resource": "r", "deployment": "d", "auth_mode": "entra"})
    assert s.foundry is not None
    assert s.foundry.auth_mode == "entra"
    assert s.foundry.api_key is None


def test_foundry_inner_fields_required() -> None:
    for field in ("resource", "deployment"):
        assert FoundryConfig.model_fields[field].is_required()


def test_foundry_unknown_key_forbidden() -> None:
    # extra="forbid" on the nested model: a mistyped FOUNDRY__* key fails fast.
    with pytest.raises(ValidationError):
        _settings(foundry={"resource": "r", "deployment": "d", "totally_bogus": "x"})


def test_foundry_secret_masked() -> None:
    s = _settings(foundry={"resource": "r", "deployment": "d", "api_key": "super-secret-key"})
    assert s.foundry is not None
    # SecretStr masks in repr (never leaks into logs / ValidationError).
    assert "super-secret-key" not in repr(s.foundry)


# --- Postgres auth mode (Azure Entra vs password; ADR-0027) ------------------


def test_db_auth_mode_defaults_to_password() -> None:
    # Default is correct for local Docker Postgres + tests — no Azure to boot.
    assert _settings().DB_AUTH_MODE == "password"


def test_db_auth_mode_accepts_entra() -> None:
    assert _settings(DB_AUTH_MODE="entra").DB_AUTH_MODE == "entra"


def test_db_auth_mode_rejects_unknown_literal() -> None:
    # Closed Literal: a typo can't silently degrade DB auth to some undefined mode.
    with pytest.raises(ValidationError):
        _settings(DB_AUTH_MODE="managed_identity")


def test_db_entra_client_id_defaults_none() -> None:
    # None -> system-assigned identity / the DefaultAzureCredential chain.
    assert _settings().DB_ENTRA_CLIENT_ID is None


def test_db_entra_client_id_accepts_value() -> None:
    mi = "00000000-0000-0000-0000-000000000000"
    assert _settings(DB_ENTRA_CLIENT_ID=mi).DB_ENTRA_CLIENT_ID == mi


# --- Sample env files (R4: an operator with only the samples can boot) --------

# `backend/` — tests/ lives directly under it.
_BACKEND_ROOT = Path(__file__).resolve().parent.parent
_SAMPLE_ENV_FILES = (".env.example", ".env.test.example")


def _boot_from(sample: str, probe: str) -> subprocess.CompletedProcess[str]:
    """Import `src.config` in a CLEAN subprocess with `ENV_FILE=<sample>` and print
    `probe`. A subprocess is not ceremony here: `Settings.model_validate` still merges
    the ambient env sources (the developer's own `.env`/`.env.test`), so an in-process
    check would let the developer's real config quietly supply anything the sample
    forgot — the exact drift this test exists to catch. Only a scrubbed environment
    proves the SAMPLE ALONE boots. Nothing below transcribes a key: pydantic-settings
    walks the file."""
    return subprocess.run(  # noqa: S603
        [sys.executable, "-c", f"from src.config import settings; print({probe})"],
        cwd=_BACKEND_ROOT,
        # Scrubbed: PATH only, plus the sample under test. No inherited DATABASE_URL,
        # no inherited REDIS__*, no ENV_FILE from the suite's own conftest.
        env={"PATH": os.environ["PATH"], "ENV_FILE": sample},
        capture_output=True,
        text=True,
        check=False,
    )


@pytest.mark.parametrize("sample", _SAMPLE_ENV_FILES)
def test_sample_env_file_boots_a_valid_settings(sample: str) -> None:
    # R4: a fresh checkout with ONLY the checked-in templates must boot. A missing
    # required key, a REDIS__* line naming a knob RedisConfig does not declare
    # (extra="forbid"), or any other drift fails this at import time.
    done = _boot_from(sample, "settings.ENVIRONMENT")
    assert done.returncode == 0, f"{sample} does not boot:\n{done.stderr}"
    assert done.stdout.strip() == "development"


def test_dev_sample_env_file_configures_redis() -> None:
    # .env.example must ship a LIVE REDIS__URL: unset boots the API but leaves every
    # build-session call raising RedisNotConfiguredError — the "operator followed the
    # sample and the build path is silently dead" outcome R4 kills.
    # (.env.test.example deliberately keeps it commented: the unset path is the
    # baseline tests/test_lifespan.py asserts, so the two samples differ on purpose.)
    done = _boot_from(".env.example", "settings.redis and settings.redis.url.get_secret_value()")
    assert done.returncode == 0, done.stderr
    assert done.stdout.strip().startswith("redis://")


@pytest.mark.parametrize("sample", _SAMPLE_ENV_FILES)
def test_sample_env_redis_keys_all_map_to_a_declared_field(sample: str) -> None:
    # RedisConfig is extra="forbid", so a REDIS__* line naming a knob that does not
    # exist is not a documentation typo — it HARD-FAILS boot the moment an operator
    # uncomments it. The commented knobs are invisible to the boot test above, so
    # walk every REDIS__ line, live or commented, and pin it to a real field.
    declared = {name.upper() for name in RedisConfig.model_fields}
    lines = (_BACKEND_ROOT / sample).read_text(encoding="utf-8").splitlines()
    referenced = {
        line.lstrip("# ").split("=", 1)[0].strip().removeprefix("REDIS__")
        for line in lines
        if line.lstrip("# ").startswith("REDIS__") and "=" in line
    }
    assert referenced, f"{sample} documents no REDIS__* key at all"
    assert referenced <= declared, (
        f"{sample} names REDIS__* keys with no RedisConfig field: {referenced - declared}"
    )
