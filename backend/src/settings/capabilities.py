"""One mixin per capability, so a role declares what it uses and nothing more.

Two rules govern this file, and both were established by measurement rather than preference:

1. **Mixins subclass `BaseSettings`, never plain `BaseModel`.** Mixing a `BaseModel` base into a
   `BaseSettings` profile makes `model_config` an incompatible override (`ConfigDict` vs
   `SettingsConfigDict`) and pyright rejects the profile. Every base being `BaseSettings` keeps
   the declared type consistent. The clobbering that a naive `BaseSettings` mixin would cause is
   handled by redeclaring the full `model_config` on each concrete profile — see `core.py`.

2. **A capability that some role needs UNCONDITIONALLY gets its own `Required*` mixin.** Pydantic
   cannot un-require an inherited field, and more importantly a prod gate is the wrong tool: a
   gate keyed on `ENVIRONMENT` is dodged by setting `ENVIRONMENT=development`, which is exactly
   the lever an operator reaches for when a process will not boot. Requiring the field outright
   fails in every environment and cannot be talked out of.

The `Optional*` variants preserve today's API behaviour byte for byte: `X | None = None` plus a
production gate on the profile. The `| None` union is also load-bearing mechanically — it is what
makes pydantic-settings treat the field as complex and run `explode_env_vars`, which is how the
nested `X__Y` env block is assembled at all.
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated, Literal, Self

from pydantic import (
    BaseModel,
    ConfigDict,
    NonNegativeInt,
    PositiveFloat,
    PositiveInt,
    SecretStr,
    field_validator,
    model_validator,
)
from pydantic_settings import BaseSettings, NoDecode

from src.services.appdb.config import AppDatabaseSettings
from src.services.auth.config import AuthConfig
from src.services.deploy.config import DeployConfig
from src.services.redis.config import RedisConfig
from src.services.sandbox.config import SandboxConfig
from src.services.storage.config import StorageConfig


class FoundryConfig(BaseModel):
    """Azure AI Foundry (Claude) model access — the ONLY sanctioned path to the model; the public
    Anthropic API is never used (R11 / AE5). Populated from one `FOUNDRY__*` env block, mirroring
    `services/auth/config.py`.

    `extra="forbid"` fails a mistyped nested key at startup; `SecretStr` masks the key; required
    inner fields carry NO default (fail-first). Exactly one auth mode: a static API key
    (`auth_mode="api_key"`, needs `api_key`) or an Entra token provider (`auth_mode="entra"`,
    managed identity — no static secret).

    Lives here rather than in a `services/foundry/` package (which does not exist) because it is
    the only nested config with no service module of its own. Re-exported from `src/config.py`,
    which five call sites import it from.
    """

    # `extra="forbid"` makes a mistyped FOUNDRY__* nested key fail at startup instead of silently
    # defaulting (fail-first). This — not the profile's own extra="forbid" — is what actually
    # catches an env-var typo.
    model_config = ConfigDict(extra="forbid")

    # The Foundry resource (account) name — the `resource=` handed to AsyncAnthropicFoundry.
    resource: str
    # The model DEPLOYMENT name inside the resource — AnthropicModel(deployment).
    deployment: str
    # Auth-mode knob: a default is fine — it names a defined behaviour the wiring branches on.
    auth_mode: Literal["api_key", "entra"] = "api_key"
    # Present only in api_key mode; the validator enforces the pairing.
    api_key: SecretStr | None = None

    # Anti-hang socket timeouts for the SHARED model client (the planning-chat relay AND the build
    # harness both build it via `build_foundry_client`). These bound one SDK request, NOT the whole
    # build turn — the harness owns that budget (`RUN_WALL_CLOCK_DEADLINE_S`, checked BETWEEN
    # iterations, never mid-stream), which is exactly why a finite `read` is needed: without it a
    # dead socket hangs mid-stream forever. `read_timeout_s` is httpx's per-CHUNK idle timeout on a
    # streamed response (it resets on every received byte), so it must out-wait the longest
    # legitimate GAP between model chunks in a build turn — Anthropic keeps the stream alive with
    # periodic pings, so real gaps are small; 120s is generous enough that a bursty build turn
    # never false-fails, yet finite enough that a genuinely dead socket surfaces as a catchable
    # APITimeoutError instead of a hang. Sized to the harness, not the snappy relay, because ONE
    # client serves both.
    read_timeout_s: PositiveFloat = 120.0
    # `connect` stays tight — pure anti-hang, no legitimate reason to wait long to open a socket.
    connect_timeout_s: PositiveFloat = 10.0
    # Built-in SDK retries for connection errors + 408/409/429/5xx (honouring `Retry-After`). Kept
    # at the SDK's own default (2): the harness fires up to `MODEL_TURN_CEILING` requests per run
    # under `RUN_WALL_CLOCK_DEADLINE_S`, so a larger N would multiply worst-case wall-clock under
    # Foundry pressure and eat into the planning chat's stall-watchdog window. 0 disables retries
    # (a defined, valid deployment choice).
    max_retries: NonNegativeInt = 2

    @model_validator(mode="after")
    def _api_key_required_in_key_mode(self) -> Self:
        # STATIC message only — never echo the secret (pydantic reflects validator messages into
        # ValidationError, and thus into logs).
        if self.auth_mode == "api_key" and self.api_key is None:
            raise ValueError(
                "FOUNDRY__API_KEY is required when FOUNDRY__AUTH_MODE=api_key; "
                "set FOUNDRY__AUTH_MODE=entra for managed-identity auth instead."
            )
        return self


# --------------------------------------------------------------------------- object storage


class OptionalObjectStore(BaseSettings):
    """Object storage as the API sees it: absent is a valid dev/test deployment, and the profile's
    production gate is what requires it in prod."""

    object_store: StorageConfig | None = None


class RequiredObjectStore(BaseSettings):
    """Object storage as the WORKER sees it: there is no deployment where a reclamation worker
    without a bundle store is correct.

    THIS IS A SAFETY PROPERTY, NOT TIDINESS. `manager.py` returns "CONFIRMED absent" when the store
    is unconfigured — correct for its original caller (do not offer a restore that cannot work) and
    catastrophic for a destroy path, which reads it as "nothing to preserve, safe to delete". A
    worker booted without storage would delete the whole fleet while believing it had verified each
    container. Required rather than prod-gated so `ENVIRONMENT=development` cannot dodge it.
    """

    object_store: StorageConfig


# --------------------------------------------------------------------------- redis


class OptionalRedis(BaseSettings):
    """Redis coordination (one-sandbox-per-user lock · idle heartbeat · sandbox registry — C5) as
    the API sees it: dev/test boot without it, production is gated on the profile."""

    redis: RedisConfig | None = None


class RequiredRedis(BaseSettings):
    """Redis as the WORKER sees it. It is both the task broker and the spare-list, so a worker
    without it consumes nothing and can prove nothing — refusing at boot is strictly better than
    a process that looks healthy and does no work."""

    redis: RedisConfig


# --------------------------------------------------------------------------- sandbox / ARM


class OptionalSandbox(BaseSettings):
    """The per-user sandbox runtime on ACA (C2/C4), carrying the C9 app-data base URL injected into
    generated apps. Optional-with-prod-gate as the API sees it."""

    sandbox: SandboxConfig | None = None


class RequiredSandbox(BaseSettings):
    """ARM access as the WORKER sees it — this is how it enumerates and deletes container apps, so
    without it there is no reclamation at all.

    It also carries `subscription_id`, `resource_group` and `region`; `region` is not incidental —
    the ARM tag PATCH body requires `location`.
    """

    sandbox: SandboxConfig


# --------------------------------------------------------------------------- shared optional


class OptionalDeploy(BaseSettings):
    """One-click publish — where a citizen's app is BUILT (ACR Tasks) and where it RUNS.

    Optional with NO production gate, deliberately: `deploy is None` means "publishing is off",
    which is correct for dev, test, and any staging not yet granted the registry role. A prod gate
    would make the production backend fail to boot the moment it merged — an outage for a
    capability nobody has enabled. Add the gate in the same commit that makes the portal show a
    Deploy control unconditionally.

    Composed by the worker too: deploy reconciliation runs there, and it reaches ARM through this
    block.
    """

    deploy: DeployConfig | None = None


class OptionalAppDb(BaseSettings):
    """Per-project database provisioning (ADR-0028): the maintenance role's DSN, the at-rest key
    for app-role passwords, and the policy knobs.

    Deliberately NOT composed into the worker. Reclamation reads the *product* database through
    `DATABASE_URL` (core) to answer "does a matching app record exist"; it never touches a
    per-project database. Requiring a maintenance credential in a process that cannot use it would
    be the union-of-everything problem this split exists to remove. A future worker that does
    provisioning composes this mixin then.
    """

    app_db: AppDatabaseSettings | None = None


class OptionalFoundry(BaseSettings):
    """Azure AI Foundry access. Genuinely optional: dev/test boot without it (the agent harness is
    exercised with Pydantic AI's TestModel, no live call), and None means "AI chat not configured".

    No production gate, matching `deploy` — the rationale is recorded in the source rather than
    inferred, and it should not be "fixed" by adding one.
    """

    foundry: FoundryConfig | None = None


# --------------------------------------------------------------------------- API-only surfaces


class AuthSurface(BaseSettings):
    """Entra ID + session auth, and the super-admin allowlist. API-only.

    Both fields are REQUIRED, and that is why this is a separate mixin rather than part of core:
    authentication and RBAC are the API's first real capabilities and every API environment needs
    them, but a worker has no request to authenticate and no admin surface to gate. Putting them
    in `CoreSettings` would make every future background process require an Entra client id.
    """

    # An always-on required sub-model (not `| None`): a missing or partial AUTH__* block fails at
    # construction in dev, test and prod alike — never boots half-configured.
    auth: AuthConfig

    # The Entra emails computed to the super-admin role PER REQUEST — no mutable DB role column
    # (ADR-0005). Required, no default: a control-plane with no configured admins is a
    # misconfiguration, so a missing SUPERADMIN_EMAILS — or one normalizing to an EMPTY allowlist —
    # fails at construction, in every environment. `NoDecode` disables pydantic-settings' JSON
    # pre-parse so the env value is a plain comma-separated string.
    superadmin_emails: Annotated[frozenset[str], NoDecode]

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


class PortalSurface(BaseSettings):
    """Everything that exists because the API serves a browser. API-only by definition — a worker
    has no origin to scope, no SPA to mount, and no per-user token budget to enforce."""

    FRONTEND_URL: str = "http://localhost:5173"
    BACKEND_URL: str = "http://localhost:8000"

    # Global per-user daily token cap — the effective limit when a user has no per-user override
    # row. An optional knob with a DEFINED default (the "standard plan"). `PositiveInt` fails a
    # non-positive value at startup, so the daily gate can never be configured to a nonsensical
    # zero/negative cap.
    DAILY_TOKEN_LIMIT: PositiveInt = 1_000_000

    # Gotenberg sidecar base URL for pptx→PDF deck conversion. Optional with a DEFINED None meaning
    # (the fail-first "optional knob" exception): deck conversion is disabled when unset, so
    # dev/test boot without a Gotenberg sidecar.
    GOTENBERG_URL: str | None = None

    # Built React/Vite SPA directory served by FastAPI when it runs as the whole stack. Optional
    # with a DEFINED None meaning: None = FastAPI serves NO SPA, correct for two-process local dev
    # where Vite serves the SPA on :5173. A value that is set but has no built `index.html` fails
    # at startup (`_mount_spa`).
    spa_dist_dir: Path | None = None
