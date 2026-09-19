"""Sandbox (ACA) provisioning configuration model.

`Settings.sandbox` is typed `SandboxConfig | None`; pydantic-settings validates one
`SANDBOX__*` env block against it (the single config funnel). `| None` keeps dev/test
booting without it — the single prod gate in `src.config` requires it in production.

SESSION-API provisions one Azure Container App sandbox per user against these knobs and
injects the interim app-data credential at provision and on restore. This shape is
FROZEN — nothing should reopen this file to change it.

ACA control-plane auth is managed-identity (`DefaultAzureCredential`): no static
provisioning secret lives here; the supervisor bearer is minted at provision time."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, PositiveFloat, PositiveInt, SecretStr


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

    # --- fleet reclamation --------------------------------------------------
    # TWO FLAGS, NOT ONE, and the split is the whole safety posture. `reclaim_enabled` turns the
    # PASS on — it enumerates, classifies, and reports what it would do. `reclaim_destroy` is what
    # lets it act. Collapsing them into one switch would mean the only way to see what reclamation
    # would do is to let it do it, and there would be no state in which an operator can read a
    # candidate list before agreeing to it.
    #
    # Both default OFF, in every environment. `reclaim_destroy` additionally must not be flipped
    # until the ARM tag backfill reports zero untagged sandboxes — an untagged
    # container is escalate-only, so flipping early buys a feature that reclaims nothing while
    # every check reads green.
    reclaim_enabled: bool = False
    reclaim_destroy: bool = False
    # THE SWEEP THAT HAS ALWAYS RUN, and this default is the whole point of the flag existing
    # separately. `sweep_all` → `reconcile_user` → `reap_user` predates these reclaim flags
    # entirely: it was an UNFLAGGED `while True` in the API lifespan, running wherever a
    # sandbox was configured, and it does almost all of the deleting. Porting it onto the
    # scheduler changes WHERE it runs; hanging it off `reclaim_enabled` would have changed
    # WHETHER it runs, and since that flag ships off in every environment, deploying this
    # release would have stopped all reaping while every health check read green. The only
    # symptom would have been the bill.
    #
    # So: on by default, because a port must not change behaviour. It remains a real kill switch
    # for an operator who needs to stop the timer, and it gates the CLOCK only — reconcile-on-
    # start and `POST /v1/build-sessions/internal/reap` are unaffected. (That path used to be
    # written `/v1/internal/reap` here, which 404s: the route is on the build-sessions router.)
    sweep_enabled: bool = True
    # The fleet size at which a pass raises the cost alarm. Not a limit — nothing is refused
    # at this number; it is the point at which a human should be told the fleet is larger than
    # anyone intended.
    reclaim_fleet_alarm_threshold: PositiveInt = 25
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
