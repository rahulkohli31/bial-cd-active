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
from typing import Literal, Self

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings

from src.services.auth.config import AuthConfig
from src.services.storage.config import StorageConfig


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

    # Entra ID + session auth, populated from one AUTH__* env block (ADR-0007).
    # An always-on required sub-model (not `| None`): authentication is the
    # control-plane's first real capability and every environment needs it, so a
    # missing/partial AUTH__* block fails at Settings() construction in dev, test,
    # and prod alike — never boots half-configured (fail-first-python.md).
    auth: AuthConfig

    # Optional knobs with defined defaults.
    FRONTEND_URL: str = "http://localhost:5173"
    BACKEND_URL: str = "http://localhost:8000"

    # Object storage (Azure Blob), populated from one OBJECT_STORE__* env block
    # (StorageConfig is an alias for AzureStorageConfig). This is the sanctioned
    # genuinely-optional integration: `| None` keeps dev/test booting without it,
    # and the single prod gate below requires it in production (fail-first-python.md
    # — storage is its named example). pydantic-settings validating this field IS
    # the one config funnel; there is no hand-written TypeAdapter on the env path.
    object_store: StorageConfig | None = None

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


# Populated from the env file / environment at runtime, which the type checker
# cannot see (mypy's pydantic plugin knows BaseSettings fields are env-sourced;
# ty / pyright do not, hence the suppressions).
settings = Settings()  # ty: ignore[missing-argument]  # pyright: ignore[reportCallIssue]
