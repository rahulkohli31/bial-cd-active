"""Everything the FastAPI control plane needs to boot, in one list.

Read this file to answer "which environment variables does the API need?" — the answer is here and
nowhere else. Fields are grouped under the four tier headers defined in `__init__.py`, and a
field's tier is spelled by its SHAPE, never by a class name:

    REQUIRED               no default            -> fails to construct in EVERY environment
    REQUIRED IN PRODUCTION `X | None = None` + a `_require_<field>_in_production` gate
    FEATURE SWITCH         `X | None = None`     -> unset means the feature is OFF, in prod too
    KNOB                   a working default     -> set only to change behaviour

Behaviourally identical to the pre-U24 mixin composition: same fields, same seven production
gates, same messages.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Self

from pydantic import PositiveInt, field_validator, model_validator
from pydantic_settings import NoDecode

from src.services.appdb.config import AppDatabaseSettings
from src.services.auth.config import AuthConfig
from src.services.deploy.config import DeployConfig
from src.services.redis.config import RedisConfig
from src.services.sandbox.config import SandboxConfig
from src.services.storage.config import StorageConfig
from src.settings.core import CoreSettings
from src.settings.foundry import FoundryConfig


class ApiSettings(CoreSettings):
    """The API role's complete settings manifest.

    Inherits `CoreSettings` — and ONLY `CoreSettings`. A single base is deliberate: pydantic merges
    `model_config` along the MRO with a plain left-to-right `dict.update`, and every `BaseSettings`
    subclass owns a complete config dict with explicit `None` defaults, so multiple bases could
    silently reset `env_file` and `env_nested_delimiter` — a profile that boots, reads no env file
    and ignores every nested `X__Y` variable. With one base there is no merge to clobber it.
    `tests/test_settings_profiles.py` pins it anyway.
    """

    # ============================================================ REQUIRED
    # No default. Missing or partial -> the process does not start, in dev, test and prod alike.

    # An always-on required sub-model (not `| None`): a missing or partial AUTH__* block fails at
    # construction everywhere — never boots half-configured. Required here rather than in
    # `CoreSettings` because a worker has no request to authenticate; putting it in core would make
    # every future background process demand an Entra client id.
    auth: AuthConfig

    # The Entra emails computed to the super-admin role PER REQUEST — no mutable DB role column
    # (ADR-0005). Required, no default: a control-plane with no configured admins is a
    # misconfiguration, so a missing SUPERADMIN_EMAILS — or one normalizing to an EMPTY allowlist —
    # fails at construction, in every environment. `NoDecode` disables pydantic-settings' JSON
    # pre-parse so the env value is a plain comma-separated string.
    superadmin_emails: Annotated[frozenset[str], NoDecode]

    # WHO A CITIZEN ASKS WHEN THE PLATFORM SAYS NO.
    #
    # The at-limit message (R31/U24) has to end in something the reader can actually do, and
    # until this field existed the product had no way to say who. `superadmin_emails` is the
    # nearest thing to an admin roster, and naming one of its entries would publish a
    # colleague's inbox as a support desk without their having agreed to it — while naming all
    # of them turns a dead end into a broadcast. So the support desk is its own fact, set once
    # by whoever runs the deployment.
    #
    # NO DEFAULT, DELIBERATELY, and the consequence is stated here rather than discovered
    # during an incident: this must be set in the App Service configuration BEFORE the release
    # ships, or the API refuses to start. That is the intended behaviour
    # (`.claude/rules/fail-first-python.md`), and it is the cheaper failure by a wide margin. A
    # default would have to be a placeholder address, and a placeholder address sends a citizen
    # who is already stuck to a mailbox nobody reads — a failure that surfaces as silence,
    # weeks later, from the one person least able to escalate it.
    SUPPORT_CONTACT_EMAIL: str

    # ============================================================ REQUIRED IN PRODUCTION
    # `X | None = None` plus a `_require_<field>_in_production` gate below. Dev and test boot
    # without these; production refuses to start and the message names the variables to set.
    #
    # NOTE the asymmetry with `worker.py`, which requires storage, redis and sandbox OUTRIGHT. A
    # gate keyed on ENVIRONMENT is dodged by setting ENVIRONMENT=development, and for the worker
    # that dodge can delete the Azure fleet. The API has no destroy path, so the gate is the
    # correct, weaker tool here.

    object_store: StorageConfig | None = None
    redis: RedisConfig | None = None
    sandbox: SandboxConfig | None = None
    app_db: AppDatabaseSettings | None = None

    # ============================================================ FEATURE SWITCH
    # `X | None = None` with NO gate. Unset means the feature is simply OFF, and that is a
    # legitimate state in EVERY environment INCLUDING production. Do not "fix" one of these by
    # adding a production gate without also making the feature unconditionally visible in the UI.

    # One-click publish — where a citizen's app is BUILT (ACR Tasks) and where it RUNS. A prod gate
    # would make the production backend fail to boot the moment it merged: an outage for a
    # capability nobody has enabled. Add the gate in the same commit that makes the portal show a
    # Deploy control unconditionally.
    deploy: DeployConfig | None = None

    # Azure AI Foundry access. Genuinely optional: dev/test exercise the agent harness with
    # Pydantic AI's TestModel and make no live call, and None means "AI chat not configured".
    foundry: FoundryConfig | None = None

    # Gotenberg sidecar base URL for pptx->PDF deck conversion. None disables deck conversion, so
    # dev/test boot without a Gotenberg sidecar.
    GOTENBERG_URL: str | None = None

    # Built React/Vite SPA directory served by FastAPI when it runs as the whole stack. None =
    # FastAPI serves NO SPA, correct for two-process local dev where Vite serves it on :5173. A
    # value that IS set but has no built `index.html` fails at startup (`_mount_spa`).
    spa_dist_dir: Path | None = None

    # ============================================================ KNOB
    # A working default. Set only to change behaviour.

    # RAGGED EDGE, stated rather than hidden: FRONTEND_URL is a knob with a working dev default,
    # but its production SHAPE is gated by `_require_real_frontend_url_in_production` below,
    # because it feeds security surfaces. It is the one field that does not sit cleanly in one
    # tier — do not add a fifth tier to accommodate it.
    FRONTEND_URL: str = "http://localhost:5173"

    # Global per-user daily token cap — the effective limit when a user has no per-user override
    # row. `PositiveInt` fails a non-positive value at startup, so the daily gate can never be
    # configured to a nonsensical zero/negative cap.
    DAILY_TOKEN_LIMIT: PositiveInt = 1_000_000

    # ============================================================ VALIDATORS
    # Every gate is `_require_<subject>_in_production`, and each name must be DISTINCT: pydantic
    # binds validators by method name, so two sharing one name would silently drop a gate and
    # production would boot misconfigured with all four type gates green. Pydantic runs these in
    # declaration order and the first raise wins, which is why
    # `test_each_api_production_gate_fires_on_its_own` reaches each one by satisfying its
    # predecessors — a single-case test can only ever prove the first.

    @field_validator("superadmin_emails", mode="before")
    @classmethod
    def _normalize_superadmin_emails(cls, value: object) -> frozenset[str]:
        # Accept a comma-separated env string OR a real iterable (the model_validate path in
        # tests). Strip + lowercase each entry and drop blanks so " Admin@BIAL.com " and
        # "admin@bial.com" both match a lowercased user email.
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
            # environment.
            raise ValueError(
                "SUPERADMIN_EMAILS must name at least one super-admin: a control-plane with "
                "no configured admins cannot approve apps or manage users."
            )
        return emails

    @field_validator("SUPPORT_CONTACT_EMAIL")
    @classmethod
    def _reject_a_support_address_nobody_could_write_to(cls, value: str) -> str:
        # A no-default field only guarantees that SOMETHING was supplied, and the shape an
        # operator supplies under time pressure is `SUPPORT_CONTACT_EMAIL=` — present, empty,
        # and accepted by a bare `str`. The whole point of the setting is that the sentence
        # ends in a working address, so an empty or address-shaped-in-name-only value is the
        # same misconfiguration as the missing variable and fails in the same place.
        #
        # Deliberately NOT a full RFC 5322 check. This is a fat-finger guard, not a validator
        # that decides whether a mailbox exists — only sending to it can answer that, and a
        # strict pattern here would reject legitimate addresses at boot for no benefit.
        address = value.strip()
        if address.count("@") != 1 or address.startswith("@") or address.endswith("@"):
            raise ValueError(
                "SUPPORT_CONTACT_EMAIL must be a single email address a citizen can write "
                "to: it is rendered to the user when the platform has to refuse them."
            )
        return address

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
            self.redis.require_tls()
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
    def _refuse_local_docker_builder_in_production(self) -> Self:
        # `local_docker` exists only so a subscription that refuses ACR Tasks can still be
        # tested end to end. It shells out to the host's docker daemon and needs a pushable
        # registry credential in the control plane's own process — both of which the
        # shipping path deliberately avoids. Reaching production with it set means a
        # `.env` travelled further than intended, and failing at startup is a far better
        # outcome than a BIAL host quietly building images.
        if self.is_production and self.deploy is not None:
            if self.deploy.image_builder != "acr_tasks":
                raise ValueError(
                    "DEPLOY__IMAGE_BUILDER must be 'acr_tasks' in production: "
                    "'local_docker' is a development-only builder that shells out to the "
                    "host docker daemon."
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
