"""Typed application configuration (fail-first).

All config flows through the `Settings(BaseSettings)` object. A required setting is
a field with NO default, so pydantic-settings raises `ValidationError` at
`Settings()` construction when it is missing — the app fails at startup in every
environment rather than booting in dev and exploding in prod
(`.claude/rules/fail-first-python.md`). `ENVIRONMENT` is a closed `Literal` for the
same reason: a default would silently disable every `is_production` safety gate.

`SecretStr` on `DATABASE_URL` masks the embedded password in repr/str/validation
errors; reads unwrap it via `.get_secret_value()` (a grep-able audit trail of
where the plaintext is used). It is masking, not encryption.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    PositiveInt,
    SecretStr,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, NoDecode

from src.services.auth.config import AuthConfig
from src.services.redis.config import RedisConfig
from src.services.sandbox.config import SandboxConfig
from src.services.storage.config import StorageConfig


class FoundryConfig(BaseModel):
    """Azure AI Foundry (Claude) model access — the ONLY sanctioned path to the
    model; the public Anthropic API is never used (R11 / AE5). Populated from one
    `FOUNDRY__*` env block, mirroring `services/auth/config.py`. Consumed by the
    Pydantic AI agent harness (U12/U13); defined here now so model/endpoint/auth
    flow through the single typed config funnel.

    `extra="forbid"` fails a mistyped nested key at startup; `SecretStr` masks the
    key; required inner fields carry NO default (fail-first). Exactly one auth mode:
    a static API key (`auth_mode="api_key"`, needs `api_key`) or an Entra token
    provider (`auth_mode="entra"`, managed identity — no static secret).
    """

    # `extra="forbid"` makes a mistyped FOUNDRY__* nested key fail at startup
    # instead of silently defaulting (fail-first).
    model_config = ConfigDict(extra="forbid")

    # The Foundry resource (account) name — the `resource=` handed to
    # AsyncAnthropicFoundry. Required, no default.
    resource: str
    # The model DEPLOYMENT name inside the resource — AnthropicModel(deployment).
    deployment: str
    # Auth-mode knob: a default is fine — it names a defined behaviour the U12
    # wiring branches on (static key vs managed-identity token provider).
    auth_mode: Literal["api_key", "entra"] = "api_key"
    # Present only in api_key mode; the validator enforces the pairing.
    api_key: SecretStr | None = None

    @model_validator(mode="after")
    def _api_key_required_in_key_mode(self) -> Self:
        # STATIC message only — never echo the secret (pydantic reflects validator
        # messages into ValidationError, and thus logs).
        if self.auth_mode == "api_key" and self.api_key is None:
            raise ValueError(
                "FOUNDRY__API_KEY is required when FOUNDRY__AUTH_MODE=api_key; "
                "set FOUNDRY__AUTH_MODE=entra for managed-identity auth instead."
            )
        return self


class Settings(BaseSettings):
    # ENV_FILE selects the source file so tests can load .env.test (a separate
    # database) without a second config variable. `__` nests future sub-models
    # (OBJECT_STORE__PROVIDER -> object_store.provider). `forbid` makes a mistyped
    # env key crash at startup instead of silently falling back to a default.
    model_config = {
        "env_file": os.getenv("ENV_FILE", ".env"),
        "env_file_encoding": "utf-8",
        "env_nested_delimiter": "__",
        "extra": "forbid",
    }

    # Required, no defaults — fail fast at startup if unset.
    ENVIRONMENT: Literal["development", "staging", "production"]
    DATABASE_URL: SecretStr

    # Super-admin RBAC allowlist (R7): the Entra emails computed to the super-admin
    # role PER REQUEST — no mutable DB role column (ADR-0005 drift cleared, KTD).
    # Required, no default (fail-first): a control-plane with no configured admins
    # is a misconfiguration, so a missing SUPERADMIN_EMAILS — or one that normalizes
    # to an EMPTY allowlist — fails at Settings() construction, in every environment.
    # `NoDecode` disables pydantic-settings' JSON pre-parse so the env
    # value is a plain comma-separated string; the validator normalizes case +
    # whitespace so membership matches the Entra-supplied email case-insensitively.
    superadmin_emails: Annotated[frozenset[str], NoDecode]

    # Entra ID + session auth, populated from one AUTH__* env block (ADR-0007).
    # An always-on required sub-model (not `| None`): authentication is the
    # control-plane's first real capability and every environment needs it, so a
    # missing/partial AUTH__* block fails at Settings() construction in dev, test,
    # and prod alike — never boots half-configured (fail-first-python.md).
    auth: AuthConfig

    # Optional knobs with defined defaults.
    #
    # How DATABASE_URL authenticates. "password" (default) = the password embedded in
    # the DSN (local Docker Postgres, tests). "entra" = Azure Database for PostgreSQL
    # Flexible Server with Microsoft Entra — no static password; a short-lived access
    # token is fetched per new connection via managed identity and presented as the
    # password (db/base.py::attach_entra_token), with TLS pinned verify-full. Mirrors
    # FOUNDRY__AUTH_MODE. The default is correct for dev/test/staging, so it stays a
    # plain knob — no prod gate (ADR-0027).
    DB_AUTH_MODE: Literal["password", "entra"] = "password"
    # User-assigned managed-identity client id for DB_AUTH_MODE=entra. None -> the
    # system-assigned identity / DefaultAzureCredential chain (AzureCliCredential via
    # `az login` locally, ManagedIdentityCredential in Azure). Ignored in password mode.
    DB_ENTRA_CLIENT_ID: str | None = None

    FRONTEND_URL: str = "http://localhost:5173"
    BACKEND_URL: str = "http://localhost:8000"

    # Global per-user daily token cap (R13/R30) — the effective limit when a user
    # has no per-user override row (UserLimit). An optional knob with a DEFINED
    # default (the "standard plan"), mirroring Express `DEFAULT_DAILY_TOKEN_LIMIT`
    # (server/limits.js). `PositiveInt` fails a non-positive value at startup, so
    # the daily gate can never be configured to a nonsensical zero/negative cap.
    DAILY_TOKEN_LIMIT: PositiveInt = 1_000_000

    # Object storage (Azure Blob), populated from one OBJECT_STORE__* env block
    # (StorageConfig is an alias for AzureStorageConfig). This is the sanctioned
    # genuinely-optional integration: `| None` keeps dev/test booting without it,
    # and the single prod gate below requires it in production (fail-first-python.md
    # — storage is its named example). pydantic-settings validating this field IS
    # the one config funnel; there is no hand-written TypeAdapter on the env path.
    object_store: StorageConfig | None = None

    # Azure AI Foundry (Claude) access — a genuinely-optional integration
    # (`| None = None`): dev/test boot without it (the agent harness is exercised
    # with Pydantic AI's TestModel, no live call), and None means "AI chat not
    # configured". The production gate lands with the chat endpoint (U13) that first
    # consumes it — gating prod here would fail-first for a capability not yet wired.
    foundry: FoundryConfig | None = None

    # Redis coordination (one-sandbox-per-user lock · idle heartbeat · sandbox
    # registry — contract C5), populated from one REDIS__* env block. A
    # genuinely-optional integration (`| None`): dev/test boot without it (no build
    # loop is exercised there), and the single prod gate below requires it in
    # production (fail-first-python.md). Frozen full-shape in Stage 0 so no Wave-1
    # track re-opens this file (D2).
    redis: RedisConfig | None = None

    # Per-user sandbox runtime on Azure Container Apps (contracts C2/C4), populated
    # from one SANDBOX__* env block, and carrying the C9 app-data-service base URL
    # injected into generated apps. Same optional-with-prod-gate shape as `redis`
    # and `object_store` — dev/test boot without it; production requires it (D2).
    sandbox: SandboxConfig | None = None

    # Gotenberg sidecar base URL for pptx→PDF deck conversion. Optional with a
    # DEFINED None meaning (the fail-first "optional knob" exception): deck
    # conversion is disabled when unset (deck uploads are rejected), so dev/test
    # boot without a Gotenberg sidecar.
    GOTENBERG_URL: str | None = None

    # Built React/Vite SPA directory served by FastAPI when it runs as the whole
    # stack (the no-Node image; U10). Optional with a DEFINED None meaning (the
    # fail-first "optional knob" exception): None = FastAPI serves NO SPA, which is
    # correct for two-process local dev where Vite serves the SPA on :5173. Set it
    # (e.g. /app/dist in the container) to mount StaticFiles + the history fallback.
    # A value that is set but has no built `index.html` fails at startup (`_mount_spa`).
    spa_dist_dir: Path | None = None

    @field_validator("superadmin_emails", mode="before")
    @classmethod
    def _normalize_superadmin_emails(cls, value: object) -> frozenset[str]:
        # Accept a comma-separated env string OR a real iterable (the model_validate
        # path in tests). Strip + lowercase each entry and drop blanks so
        # " Admin@BIAL.com " and "admin@bial.com" both match a lowercased user email.
        if isinstance(value, str):
            parts: list[str] = value.split(",")
        elif isinstance(value, (list, tuple, set, frozenset)):
            parts = [str(item) for item in value]
        else:
            raise ValueError(
                "SUPERADMIN_EMAILS must be a comma-separated string or a list of emails."
            )
        emails = frozenset(part.strip().lower() for part in parts if part.strip())
        if not emails:
            # An EMPTY allowlist is the same misconfiguration as a missing one — nobody could
            # approve an app or suspend a user — so it fails at construction too, in every
            # environment. (Fail-first: the "is any deployment where this empty value is
            # correct?" test answers no.)
            raise ValueError(
                "SUPERADMIN_EMAILS must name at least one super-admin: a control-plane with "
                "no configured admins cannot approve apps or manage users."
            )
        return emails

    @property
    def is_production(self) -> bool:
        return self.ENVIRONMENT == "production"

    @model_validator(mode="after")
    def _require_storage_in_production(self) -> Self:
        # Storage is genuinely-optional (| None) so dev/test boot without it, but
        # production persists attachments and cannot run without it. The single
        # sanctioned optional-integration prod gate (fail-first-python.md): fail at
        # startup in prod, not at the first artifact write.
        if self.is_production and self.object_store is None:
            raise ValueError(
                "object storage must be configured in production: set "
                "OBJECT_STORE__PROVIDER and the provider's OBJECT_STORE__* credentials."
            )
        return self

    @model_validator(mode="after")
    def _require_redis_in_production(self) -> Self:
        # Redis is genuinely-optional (| None) so dev/test boot without it, but
        # production coordinates the sandbox lock/heartbeat/registry through it and
        # cannot run without it. The single sanctioned optional-integration prod gate
        # (fail-first-python.md): fail at startup in prod, not at the first lock
        # acquire. STATIC message only — never interpolate the DSN (SecretStr).
        if self.is_production and self.redis is None:
            raise ValueError(
                "redis must be configured in production: set REDIS__URL "
                "(and any REDIS__* pool knobs)."
            )
        return self

    @model_validator(mode="after")
    def _require_sandbox_in_production(self) -> Self:
        # The per-user sandbox runtime is genuinely-optional (| None) so dev/test boot
        # without it, but production has no build loop without it. Same prod gate shape
        # as storage/redis. STATIC message only — never interpolate any SANDBOX__* value.
        if self.is_production and self.sandbox is None:
            raise ValueError(
                "sandbox must be configured in production: set the SANDBOX__* "
                "ACA-provisioning block and SANDBOX__APP_DATA_BASE_URL."
            )
        return self

    @model_validator(mode="after")
    def _secure_cookies_in_production(self) -> Self:
        # `cookie_secure=None` (derive from is_production) is the usual path, and an explicit
        # False is legitimate in dev/staging over plain http. An explicit False in PRODUCTION
        # drops `Secure` from the session/refresh/CSRF cookies AND their `__Host-`/`__Secure-`
        # prefixes (services/auth/cookies.py) — the session would ride plain http. Fail at
        # startup instead of booting a permissively-degraded control-plane. Cookie `Path`
        # scoping is untouched by this gate (ADR-0007).
        if self.is_production and self.auth.cookie_secure is False:
            raise ValueError(
                "AUTH__COOKIE_SECURE=false is not allowed in production: it drops the Secure "
                "flag and the __Host-/__Secure- cookie prefixes. Leave it unset (derived from "
                "ENVIRONMENT) or set it to true."
            )
        return self


# Populated from the env file / environment at runtime, which the type checker
# cannot see (mypy's pydantic plugin knows BaseSettings fields are env-sourced;
# ty / pyright do not, hence the suppressions).
settings = Settings()  # ty: ignore[missing-argument]  # pyright: ignore[reportCallIssue]
