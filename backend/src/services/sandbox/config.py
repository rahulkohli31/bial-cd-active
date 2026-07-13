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

from pydantic import BaseModel, ConfigDict, PositiveFloat


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
    # The pre-baked sandbox image (golden template + supervisor + Caddy) built by the
    # Windows `az acr build` into ACR (U10 / ADR-0015), e.g.
    # bialgenaicr01.azurecr.io/citizen-dev-sandbox:latest.
    image_ref: str
    # The base URL a sandboxed app uses to reach the platform data-service over public
    # ingress — injected as BIAL_DATA_BASE_URL at provision (C9), e.g.
    # https://<portal-host>/api. Required: a configured sandbox with no data endpoint
    # cannot run generated-app CRUD.
    app_data_base_url: str

    # ACA sizing (the POC single-sandbox-per-user shape). vCPU cores + memory string.
    cpu: PositiveFloat = 1.0
    memory: str = "2Gi"
    # POC = public ingress (C8); internal/VNet ingress is deferred hardening.
    ingress: Literal["external", "internal"] = "external"
