"""Sandbox (ACA) provisioning configuration model.

`Settings.sandbox` is typed `SandboxConfig | None`; pydantic-settings validates one
`SANDBOX__*` env block against it (the single config funnel). `| None` keeps dev/test
booting without it — the single prod gate in `src.config` requires it in production.

SESSION-API provisions one Azure Container App sandbox per user against these knobs and
injects the interim app-data credential at provision and on restore. It also sizes the
pool of ready sandboxes by day and by night, in India time.

ACA control-plane auth is managed-identity (`DefaultAzureCredential`): no static
provisioning secret lives here; the supervisor bearer is minted at provision time."""

from __future__ import annotations

from datetime import datetime, time
from typing import TYPE_CHECKING, Annotated, Final, Literal, Self
from zoneinfo import ZoneInfo

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    PositiveFloat,
    PositiveInt,
    SecretStr,
    field_validator,
    model_validator,
)
from pydantic_settings import NoDecode

if TYPE_CHECKING:
    from src.db.models.sandbox_start import SandboxProjectType

#: The platform serves one organisation in one country, so the pool's day is read in this zone and
#: the zone is not a setting.
INDIA: Final = ZoneInfo("Asia/Kolkata")

#: A size above this fails startup, so a mistyped size fails a deploy instead of running dozens of
#: ready containers.
POOL_SIZE_CEILING: Final = 20

_WEEKDAYS: Final = ("mon", "tue", "wed", "thu", "fri", "sat", "sun")

PoolSize = Annotated[int, Field(ge=0, le=POOL_SIZE_CEILING)]


class SandboxConfig(BaseModel):
    """Azure Container Apps provisioning target + the app-data injection base URL.
    Targeting fields are required (no default — fail-first); the sizing/ingress knobs
    keep POC-sensible defaults."""

    # `extra="forbid"` makes a mistyped SANDBOX__* nested key fail at startup instead
    # of silently defaulting (fail-first).
    model_config = ConfigDict(extra="forbid")

    # ACA provisioning target — SESSION-API provisions per-user sandboxes here.
    subscription_id: str
    resource_group: str
    region: str
    # The ACA Managed Environment the per-user sandboxes run in (the one infra
    # prerequisite this config names but does not itself provision). Required, no
    # default (fail-first): the provisioned env name varies per deployment
    # (e.g. `bial-dev-aca-env`), so a wrong/absent name makes `provision_new` look up a
    # non-existent managedEnvironment and fail — config-driven, never hardcoded in aca.py.
    managed_environment_name: str
    # The pre-baked sandbox image (golden template + supervisor + Caddy) built by the
    # Windows `az acr build` into ACR, e.g.
    # bialgenaicr01.azurecr.io/citizen-dev-sandbox:latest.
    image_ref: str
    # ACR pull auth for the private sandbox image. ACA cannot pull from a private ACR
    # without a `registries` credential, so these are required (no default — fail-first):
    # a configured sandbox whose image lives in a private registry cannot start without
    # them. This is the admin-credential path (ACR admin-enabled); the managed-identity +
    # AcrPull alternative would replace these three with an `identity` reference.
    # `acr_server` is the login server (`<registry>.azurecr.io`, the host of `image_ref`);
    # `acr_password` is a SecretStr, unwrapped only at the ACA SDK boundary and injected
    # as an ACA secret referenced by the registry credential (never inlined in the spec).
    acr_server: str
    acr_username: str
    acr_password: SecretStr

    # The Blob base URL a sandboxed app uses to reach its OWN per-app object-storage container —
    # injected as BIAL_BLOB_CONTAINER_URL at provision. A container SAS is signed by
    # account NAME, not host, so the same SAS is valid against any host serving the account; but
    # the INJECTED URL must be a host the SANDBOX can reach. For real Azure the public
    # account host is reachable, so None (= "use `object_store.account_url`") is correct; for local
    # Azurite the control-plane's 127.0.0.1 resolves to the sandbox's OWN localhost, so this is set
    # to the docker-network address (e.g. http://azurite:10000/devstoreaccount1). None = use the
    # signing account's account_url — a defined, correct default (fail-first optional-knob rule).
    blob_base_url: str | None = None

    # THE KILL SWITCH FOR THE SCHEDULED SWEEP. `sweep_all` → `reconcile_user` → `reap_user` does
    # almost all of the fleet's deleting, on a timer; an operator who needs to stop that timer
    # flips this. It gates the CLOCK only — reconcile-on-start and
    # `POST /v1/build-sessions/internal/reap` are unaffected. On by default: this is a live
    # sweep, not a report-only preview.
    sweep_enabled: bool = True
    # --- the absolute age ceiling -------------------------------------------
    # THE ONE RULE THAT DOES NOT ASK WHETHER ANYTHING IS CLAIMING THE CONTAINER. Every other
    # sparing signal — the lease, the starting marker, the lock, the stay — is a claim, so a
    # container held open by a JAMMED claim is spared by definition and no amount of tier logic
    # reaches it. Presence renewal makes that population reachable in one more way: a tab left
    # open renews forever. This is what bounds both.
    #
    # `drain_after_hours` is measured from the CONTAINER's age, not the registry record's: the
    # registry birthday is re-stamped at each registration and is dropped by the failed-teardown
    # arm, so reading it would hand a fresh ceiling to precisely the containers the ceiling exists
    # to collect. `reaper.py` reads the ARM tag and falls back to the registry only when ARM
    # cannot answer — one tag read per spared user per pass, and only while this flag is on.
    #
    # ALWAYS ON, and deliberately not a flag. Two hours is the screen ceiling the platform
    # commits to, and a deployment that switched it off would have no bound on a left-open tab
    # at all — which is the one population this exists for.
    drain_after_hours: PositiveInt = 2

    # ACA sizing (the POC single-sandbox-per-user shape). vCPU cores + memory string.
    cpu: PositiveFloat = 1.0
    memory: str = "2Gi"
    # POC = public ingress; internal/VNet ingress is deferred hardening.
    ingress: Literal["external", "internal"] = "external"

    # --- the pool of ready sandboxes ----------------------------------------
    # How many to hold by day and by night. 0 holds none, and a start then creates its own.
    pool_day_size: PoolSize = 0
    pool_night_size: PoolSize = 0
    # The same for the pool of containers made with the lake's identity, which only a connector
    # project's start claims. Both processes hold the same values.
    pool_connector_day_size: PoolSize = 0
    pool_connector_night_size: PoolSize = 0
    # India time, `HH:MM`. The day runs from the start up to, not including, the end.
    pool_day_start: time = time(9, 0)
    pool_day_end: time = time(19, 0)
    # The days that count as daytime, as comma-separated three-letter names in any case, e.g.
    # `mon,tue,wed,thu,fri`. Held as `datetime.weekday()` numbers.
    pool_day_days: Annotated[frozenset[int], NoDecode] = frozenset(range(5))

    @field_validator("pool_day_days", mode="before")
    @classmethod
    def _read_day_names(cls, value: object) -> object:
        if not isinstance(value, str):
            return value
        names = [name.strip().lower() for name in value.split(",") if name.strip()]
        unknown = [name for name in names if name not in _WEEKDAYS]
        if not names or unknown:
            raise ValueError(f"pool_day_days takes names from {', '.join(_WEEKDAYS)}")
        return frozenset(_WEEKDAYS.index(name) for name in names)

    @model_validator(mode="after")
    def _the_day_is_a_span_of_india_time(self) -> Self:
        # An offset on either end would make the comparison below raise on every start.
        if self.pool_day_start.tzinfo is not None or self.pool_day_end.tzinfo is not None:
            raise ValueError("pool_day_start and pool_day_end are India time, without an offset")
        if self.pool_day_start >= self.pool_day_end:
            raise ValueError("pool_day_start must come before pool_day_end")
        return self

    def pool_size_at(self, instant: datetime, *, project_type: SandboxProjectType) -> int:
        """How many ready sandboxes the `project_type` pool should hold at `instant`, an aware
        datetime."""
        local = instant.astimezone(INDIA)
        daytime = (
            local.weekday() in self.pool_day_days
            and self.pool_day_start <= local.time() < self.pool_day_end
        )
        # The enum's value, not its member: importing the ORM here would reach `src.config`, which
        # imports this module.
        if project_type == "connector":
            return self.pool_connector_day_size if daytime else self.pool_connector_night_size
        return self.pool_day_size if daytime else self.pool_night_size
