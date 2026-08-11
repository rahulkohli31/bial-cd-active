"""The two concrete role profiles, and how a process picks one.

`ApiSettings` reproduces today's `Settings` exactly — same fields, same seven production gates,
same messages. `WorkerSettings` composes only what a background worker uses, and requires the
three capabilities it cannot do its job without.

**Every profile MUST redeclare `model_config = SETTINGS_CONFIG`.** pydantic merges `model_config`
along the MRO with a plain left-to-right `dict.update`, and every `BaseSettings` subclass owns a
complete config dict with explicit `None` defaults — so composing mixins silently resets
`env_file` and `env_nested_delimiter` unless the profile reasserts them. The failure is invisible
(the profile boots; it just ignores the env file and every nested `X__Y` variable), so
`tests/test_settings_profiles.py` pins it per profile.
"""

from __future__ import annotations

import os
from typing import Self

from pydantic import model_validator

from src.settings.capabilities import (
    AuthSurface,
    OptionalAppDb,
    OptionalDeploy,
    OptionalFoundry,
    OptionalObjectStore,
    OptionalRedis,
    OptionalSandbox,
    PortalSurface,
    RequiredObjectStore,
    RequiredRedis,
    RequiredSandbox,
)
from src.settings.core import ROLE_ENV_VAR, SETTINGS_CONFIG, CoreSettings


def _require_tls_to_redis(url: str) -> None:
    """TLS to Redis is carried by the DSN SCHEME, not by kwargs (there are no per-environment TLS
    settings anywhere in `services/redis/client.py`), so a profile validator is the only place
    plaintext can be caught.

    Shared by both profiles so they cannot drift into two different opinions about the same
    instance — the worker connects to the same Redis the API does, and now also runs its task
    broker over it. STATIC message only: never interpolate the DSN (SecretStr).
    """
    if not url.startswith("rediss://"):
        raise ValueError(
            "REDIS__URL must use the TLS scheme rediss:// in production: a plaintext redis:// "
            "connection would expose the coordination keys and any DSN-embedded password on the "
            "wire. Azure Cache for Redis serves TLS on port 6380."
        )


class ApiSettings(
    CoreSettings,
    AuthSurface,
    PortalSurface,
    OptionalObjectStore,
    OptionalRedis,
    OptionalSandbox,
    OptionalAppDb,
    OptionalDeploy,
    OptionalFoundry,
):
    """The FastAPI control-plane's settings — behaviourally identical to the pre-U23 `Settings`.

    Every optional integration keeps its `X | None = None` shape plus a production gate, so dev and
    test boot without storage, Redis, a sandbox substrate or an app-database maintenance
    credential, exactly as before.
    """

    model_config = SETTINGS_CONFIG

    @model_validator(mode="after")
    def _require_storage_in_production(self) -> Self:
        # Production persists attachments and cannot run without it. The sanctioned
        # optional-integration prod gate (fail-first-python.md): fail at startup in prod, not at
        # the first artifact write.
        if self.is_production and self.object_store is None:
            raise ValueError(
                "object storage must be configured in production: set "
                "OBJECT_STORE__PROVIDER and the provider's OBJECT_STORE__* credentials."
            )
        return self

    @model_validator(mode="after")
    def _require_redis_in_production(self) -> Self:
        # Production coordinates the sandbox lock/heartbeat/registry through it. Fail at startup,
        # not at the first lock acquire. STATIC message only — never interpolate the DSN.
        if self.is_production and self.redis is None:
            raise ValueError(
                "redis must be configured in production: set REDIS__URL "
                "(and any REDIS__* pool knobs)."
            )
        return self

    @model_validator(mode="after")
    def _require_redis_tls_in_production(self) -> Self:
        if self.is_production and self.redis is not None:
            _require_tls_to_redis(self.redis.url.get_secret_value())
        return self

    @model_validator(mode="after")
    def _require_sandbox_in_production(self) -> Self:
        # Production has no build loop without it.
        if self.is_production and self.sandbox is None:
            raise ValueError(
                "sandbox must be configured in production: set the SANDBOX__* "
                "ACA-provisioning block."
            )
        return self

    @model_validator(mode="after")
    def _require_app_db_in_production(self) -> Self:
        # Production IS the data isolation boundary for every generated app (ADR-0028): an
        # unconfigured prod control plane would create projects that silently never get a
        # database. STATIC message only — never interpolate the maintenance DSN or the at-rest key.
        if self.is_production and self.app_db is None:
            raise ValueError(
                "per-project databases must be configured in production: set "
                "APP_DB__MAINTENANCE_DSN and APP_DB__ENCRYPTION_KEY."
            )
        return self

    @model_validator(mode="after")
    def _require_real_frontend_url_in_production(self) -> Self:
        # FRONTEND_URL keeps its dev default, but it feeds security surfaces — the sandbox
        # frame-ancestors CSP via BIAL_PORTAL_ORIGIN (C8) and postMessage targetOrigin checks — so
        # production booting with the localhost default would silently mis-scope them.
        if self.is_production and not self.FRONTEND_URL.startswith("https://"):
            raise ValueError(
                "FRONTEND_URL must be set to the portal's real https:// origin in "
                "production: the localhost dev default (or any non-https URL) would "
                "mis-scope the sandbox frame-ancestors CSP and postMessage origins."
            )
        return self

    @model_validator(mode="after")
    def _secure_cookies_in_production(self) -> Self:
        # `cookie_secure=None` (derive from is_production) is the usual path, and an explicit False
        # is legitimate in dev/staging over plain http. An explicit False in PRODUCTION drops
        # `Secure` from the session/refresh/CSRF cookies AND their `__Host-`/`__Secure-` prefixes,
        # so the session would ride plain http.
        if self.is_production and self.auth.cookie_secure is False:
            raise ValueError(
                "AUTH__COOKIE_SECURE=false is not allowed in production: it drops the Secure "
                "flag and the __Host-/__Secure- cookie prefixes. Leave it unset (derived from "
                "ENVIRONMENT) or set it to true."
            )
        return self


class WorkerSettings(
    CoreSettings,
    RequiredObjectStore,
    RequiredRedis,
    RequiredSandbox,
    OptionalDeploy,
):
    """The Taskiq worker's settings (ADR-0011, ADR-0029 §9).

    Three capabilities are REQUIRED rather than prod-gated, and the distinction is the point of
    this whole unit: a gate keyed on `ENVIRONMENT` is dodged by setting
    `ENVIRONMENT=development`, which is precisely what an operator does when a new container will
    not boot. These fail in every environment and cannot be talked out of.

    * **object store** — the durable-copy precondition (U14) is unsatisfiable without it, and
      worse, `manager.py` reports an unconfigured store as "CONFIRMED absent", which a destroy
      path reads as "nothing to preserve". This is the single misconfiguration that could delete
      the fleet.
    * **redis** — both the task broker and the spare-list. Without it the worker consumes nothing
      and can prove nothing about ownership.
    * **sandbox** — ARM access. It is how the fleet is enumerated and how containers are deleted;
      it also carries the `region` the tag PATCH body needs as `location`.

    Deliberately ABSENT: `auth`, `superadmin_emails`, the portal surface (no request to
    authenticate, no browser to serve, no admin gate to enforce), `foundry` (runs no model), and
    `app_db` (reclamation reads the *product* database via `DATABASE_URL`; it never touches a
    per-project database). `deploy` is present because deploy reconciliation runs here.
    """

    model_config = SETTINGS_CONFIG

    @model_validator(mode="after")
    def _require_redis_tls_in_production(self) -> Self:
        # Same instance, same rule as the API — and now carrying the task stream as well as the
        # coordination keys.
        if self.is_production:
            _require_tls_to_redis(self.redis.url.get_secret_value())
        return self


def build_profile() -> ApiSettings | WorkerSettings:
    """Construct the profile for THIS process's role, from the environment alone.

    Read from the environment rather than passed in, because on Python 3.14 the POSIX
    multiprocessing start method is `forkserver`: a child inherits nothing, so a settings object
    built in a lifespan or memoized in a parent does not survive into it. Every process builds its
    own.

    The role defaults to `api`, so nothing that exists today changes behaviour — a deployment sets
    `BIAL_ROLE=worker` on the worker's container and nowhere else.
    """
    if os.getenv(ROLE_ENV_VAR, "api").strip().lower() == "worker":
        return WorkerSettings()  # ty: ignore[missing-argument]  # pyright: ignore[reportCallIssue]
    return ApiSettings()  # ty: ignore[missing-argument]  # pyright: ignore[reportCallIssue]
