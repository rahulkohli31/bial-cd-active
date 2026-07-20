"""Raw Azure Container Apps control-plane ops (the C2 lower seam).

A thin async wrapper over the SYNC `azure-mgmt-appcontainers` SDK: create / delete /
get one container app, authenticated by managed identity (`DefaultAzureCredential` —
no static provisioning secret; `SandboxConfig` §doc). The mgmt SDK is synchronous, so
every call is offloaded to a worker thread (`asyncio.to_thread`) rather than blocking
the event loop.

This seam is deliberately THIN and SDK-mocked in SESSION-API's tests (the concrete
`AcaSandboxClient` injects a fake). Track SANDBOX live-validates provision / snapshot /
restore against real Azure THROUGH this code at its acceptance join — including the ACA
container-naming rules and the Managed Environment wiring (the one infra prerequisite
this convention names but does not itself provision).
"""

from __future__ import annotations

import asyncio
from typing import Final

from azure.core.exceptions import (
    HttpResponseError,
    ResourceNotFoundError,
    ServiceRequestError,
    ServiceResponseError,
)
from azure.identity import DefaultAzureCredential
from azure.mgmt.appcontainers import ContainerAppsAPIClient
from azure.mgmt.appcontainers import models as aca_models

from src.services.sandbox.config import SandboxConfig

# The in-container Caddy fronts a single ACA ingress port (8080) and routes /_sup/* to
# the supervisor and everything else to `next dev` (sandbox/Caddyfile).
_INGRESS_TARGET_PORT: Final = 8080

# The ACA secret name the ACR registry credential references for its password. ACA
# secret names must be lowercase-alphanumeric-with-dashes; the ACR password is stored as
# this secret and referenced by `password_secret_ref` so the plaintext is never inlined
# on the registry credential in the container-app spec.
_ACR_PASSWORD_SECRET_NAME: Final = "acr-password"


class AcaError(Exception):
    """A non-retryable ACA control-plane failure (4xx other than 404, bad response)."""


class AcaTransientError(AcaError):
    """A retryable ACA control-plane failure (network blip, throttling, 5xx)."""


def _is_transient(exc: HttpResponseError) -> bool:
    code = exc.status_code
    return code is not None and (code == 429 or code >= 500)


def _managed_environment_id(config: SandboxConfig) -> str:
    # The Managed Environment name is config-driven (`SANDBOX__MANAGED_ENVIRONMENT_NAME`)
    # so the same image works against any provisioned env (e.g. `bial-dev-aca-env`).
    return (
        f"/subscriptions/{config.subscription_id}"
        f"/resourceGroups/{config.resource_group}"
        f"/providers/Microsoft.App/managedEnvironments/{config.managed_environment_name}"
    )


def _fqdn_of(app: aca_models.ContainerApp) -> str | None:
    """Dig the public ingress FQDN out of a container-app model (attributes are loosely
    typed by the SDK, so coerce the leaf to a concrete `str`)."""
    props = app.properties
    configuration = props.configuration if props else None
    ingress = configuration.ingress if configuration else None
    fqdn = ingress.fqdn if ingress else None
    return str(fqdn) if fqdn else None


class AcaControlPlane:
    """Async facade over the sync ACA management client. One instance per configured
    sandbox; holds the managed-identity credential + the mgmt client for its lifetime."""

    def __init__(self, config: SandboxConfig) -> None:
        self._config = config
        self._credential = DefaultAzureCredential()
        self._client = ContainerAppsAPIClient(self._credential, config.subscription_id)

    def _envelope(self, env: dict[str, str]) -> aca_models.ContainerApp:
        c = self._config
        return aca_models.ContainerApp(
            location=c.region,
            properties=aca_models.ContainerAppProperties(
                managed_environment_id=_managed_environment_id(c),
                configuration=aca_models.Configuration(
                    active_revisions_mode="Single",
                    # ACR pull auth (admin-credential path): the password rides an ACA
                    # secret, and the registry credential references it by name so the
                    # plaintext is never inlined on the registry entry. Without this a
                    # private-ACR image cannot be pulled and the revision never starts.
                    secrets=[
                        aca_models.Secret(
                            name=_ACR_PASSWORD_SECRET_NAME,
                            value=c.acr_password.get_secret_value(),
                        )
                    ],
                    registries=[
                        aca_models.RegistryCredentials(
                            server=c.acr_server,
                            username=c.acr_username,
                            password_secret_ref=_ACR_PASSWORD_SECRET_NAME,
                        )
                    ],
                    ingress=aca_models.Ingress(
                        external=(c.ingress == "external"),
                        target_port=_INGRESS_TARGET_PORT,
                        transport="auto",
                    ),
                ),
                template=aca_models.Template(
                    containers=[
                        aca_models.Container(
                            name="sandbox",
                            image=c.image_ref,
                            resources=aca_models.ContainerResources(cpu=c.cpu, memory=c.memory),
                            env=[
                                aca_models.EnvironmentVar(name=k, value=v) for k, v in env.items()
                            ],
                        )
                    ],
                    # Single-replica POC (C5/C7): exactly one container per user.
                    scale=aca_models.Scale(min_replicas=1, max_replicas=1),
                ),
            ),
        )

    async def create_app(self, *, name: str, env: dict[str, str]) -> str:
        """Create (or update) the container app; return its public ingress FQDN
        (host-only, no scheme). Retryable failures raise `AcaTransientError`."""
        envelope = self._envelope(env)

        def _run() -> str:
            poller = self._client.container_apps.begin_create_or_update(
                self._config.resource_group, name, envelope
            )
            fqdn = _fqdn_of(poller.result())
            if fqdn is None:
                raise AcaError("ACA returned no ingress FQDN")
            return fqdn

        try:
            return await asyncio.to_thread(_run)
        except (ServiceRequestError, ServiceResponseError) as exc:
            raise AcaTransientError("ACA create request failed") from exc
        except HttpResponseError as exc:
            if _is_transient(exc):
                raise AcaTransientError("ACA create was throttled or 5xx'd") from exc
            raise AcaError("ACA create failed") from exc

    async def delete_app(self, *, name: str) -> None:
        """Idempotent delete of the container app: an already-absent app is a no-op
        (404 / ResourceNotFound swallowed). Retryable failures raise `AcaTransientError`."""

        def _run() -> None:
            poller = self._client.container_apps.begin_delete(self._config.resource_group, name)
            poller.result()

        try:
            await asyncio.to_thread(_run)
        except ResourceNotFoundError:
            return
        except (ServiceRequestError, ServiceResponseError) as exc:
            raise AcaTransientError("ACA delete request failed") from exc
        except HttpResponseError as exc:
            if exc.status_code == 404:
                return
            if _is_transient(exc):
                raise AcaTransientError("ACA delete was throttled or 5xx'd") from exc
            raise AcaError("ACA delete failed") from exc

    async def get_app_fqdn(self, *, name: str) -> str | None:
        """The container app's ingress FQDN, or `None` when the app does not exist —
        the confirmed-absent signal `attach_existing` uses to tell a torn-down container
        (→ restore) apart from a transient network blip (→ retry)."""

        def _run() -> str | None:
            return _fqdn_of(self._client.container_apps.get(self._config.resource_group, name))

        try:
            return await asyncio.to_thread(_run)
        except ResourceNotFoundError:
            return None
        except (ServiceRequestError, ServiceResponseError) as exc:
            # A transient ARM blip is NOT "confirmed gone" — surface it as retryable so the
            # attach caller maps it to SandboxNotReadyError, never SandboxGoneError (which
            # would trigger a restore + double-allocate the live original).
            raise AcaTransientError("ACA get request failed") from exc
        except HttpResponseError as exc:
            if exc.status_code == 404:
                return None
            if _is_transient(exc):
                raise AcaTransientError("ACA get was throttled or 5xx'd") from exc
            raise AcaError("ACA get failed") from exc

    async def aclose(self) -> None:
        """Close the mgmt client + the managed-identity credential (both sync)."""

        def _close() -> None:
            self._client.close()
            self._credential.close()

        await asyncio.to_thread(_close)


def create_aca_control_plane(config: SandboxConfig) -> AcaControlPlane:
    return AcaControlPlane(config)
