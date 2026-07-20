"""Sandbox (ACA) provisioning configuration model.

`Settings.sandbox` is typed `SandboxConfig | None`; pydantic-settings validates one
`SANDBOX__*` env block against it (the single config funnel). The per-user sandbox
runtime is a genuinely-optional integration: `| None` keeps dev/test booting
without it, and the single prod gate in `src.config` requires it in production
(fail-first-python.md).

SESSION-API (Wave 1) provisions one Azure Container App sandbox per user against
these knobs and injects the interim app-data credential (contract C9) at provision
and on restore. Stage 0 freezes the full shape so no Wave-1 track re-opens
`config.py`.

ACA control-plane auth is managed-identity (`DefaultAzureCredential`) — there is no
static provisioning secret to hold here; the per-sandbox supervisor bearer token is
minted at provision time, not configured.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, PositiveFloat, SecretStr


class SandboxConfig(BaseModel):
    """Azure Container Apps provisioning target + the C9 app-data injection base URL.
    Targeting fields are required (no default — fail-first); the sizing/ingress knobs
    keep POC-sensible defaults."""

    # `extra="forbid"` makes a mistyped SANDBOX__* nested key fail at startup instead
    # of silently defaulting (fail-first).
    model_config = ConfigDict(extra="forbid")

    # ACA provisioning target — SESSION-API provisions per-user sandboxes here (Wave 1).
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
    # Windows `az acr build` into ACR (U10 / ADR-0015), e.g.
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

    # The base URL a sandboxed app uses to reach the platform data-service — injected as
    # BIAL_DATA_BASE_URL at provision (C9). The sandbox hits the FastAPI backend DIRECTLY
    # over its public ingress (NOT the portal's `/api` nginx rewrite), so this ends in
    # `/v1`, e.g. https://<platform-host>/v1 — the template concats `/apps/{appId}/records`
    # onto it (C6). Required: a configured sandbox with no data endpoint cannot run
    # generated-app CRUD.
    app_data_base_url: str

    # The Blob base URL a sandboxed app uses to reach its OWN per-app object-storage container —
    # injected as BIAL_BLOB_CONTAINER_URL at provision (C9 §6). A container SAS is signed by
    # account NAME, not host, so the same SAS is valid against any host serving the account; but
    # the INJECTED URL must be a host the SANDBOX can reach (KTD-2). For real Azure the public
    # account host is reachable, so None (= "use `object_store.account_url`") is correct; for local
    # Azurite the control-plane's 127.0.0.1 resolves to the sandbox's OWN localhost, so this is set
    # to the docker-network address (e.g. http://azurite:10000/devstoreaccount1). None = use the
    # signing account's account_url — a defined, correct default (fail-first optional-knob rule).
    blob_base_url: str | None = None

    # ACA sizing (the POC single-sandbox-per-user shape). vCPU cores + memory string.
    cpu: PositiveFloat = 1.0
    memory: str = "2Gi"
    # POC = public ingress (C8); internal/VNet ingress is deferred hardening.
    ingress: Literal["external", "internal"] = "external"
