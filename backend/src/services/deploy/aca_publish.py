"""Azure Container Apps operations for a PUBLISHED app.

A separate client from `services/sandbox/aca.py`, deliberately. The two build genuinely
different container apps and share only the ARM plumbing, and the difference is not
cosmetic — reusing the sandbox envelope here would fail in a way that looks like success:

* The sandbox probes `/_sup/health` on port 8080, because a Caddy proxy and a Python
  supervisor sit in front of `next dev`. A published container runs `node server.js`
  directly. That path 404s forever, the revision never goes healthy, and the ACA create
  still returns an FQDN — so the deploy reports success and the URL 5xx's.
* The sandbox pins `min_replicas=1, max_replicas=1` (exactly one container per user). A
  published app scales to zero.
* The sandbox's image is a config constant; a published app's is a per-deploy digest.

So `sandbox/aca.py` is imported for its ERROR TYPES and its ARM helpers — one shared
classification, one shared long-running-operation ceiling — and nothing else. Its
`_envelope` is untouched, which is also why the sandbox's own tests cannot be broken from
here.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from typing import Any, Final, Protocol

import structlog
from azure.core.exceptions import (
    HttpResponseError,
    ResourceNotFoundError,
    ServiceRequestError,
    ServiceResponseError,
)
from azure.identity import DefaultAzureCredential
from azure.mgmt.appcontainers import ContainerAppsAPIClient
from azure.mgmt.appcontainers import models as aca_models

from src.services.deploy.config import DeployConfig
from src.services.deploy.names import published_app_name, revision_suffix
from src.services.sandbox.aca import (
    AcaError,
    AcaTransientError,
    await_lro,
    fqdn_of,
    is_transient,
)

_log = structlog.get_logger()

# The prefix every published container app carries. Its counterpart is
# `SANDBOX_NAME_PREFIX` ("sbx-"), and the two must never overlap: `list_sandbox_app_names`
# partitions the resource group on exactly this distinction so an orphan sweep can never
# offer to delete a citizen's live application.
PUBLISHED_NAME_PREFIX: Final = "pub-"

# The ACA secret names the container spec references. Values are never inlined in the
# template — a plain `EnvironmentVar` would put the database DSN in `az containerapp show`
# output and in every ARM activity log entry.
_DATABASE_SECRET: Final = "bial-database-url"
_BLOB_SAS_SECRET: Final = "bial-blob-sas"
_ACR_PASSWORD_SECRET: Final = "acr-password"

# Env vars whose values are secrets and must ride a secret reference.
_SECRET_ENV: Final = {
    "BIAL_DATABASE_URL": _DATABASE_SECRET,
    "BIAL_BLOB_SAS": _BLOB_SAS_SECRET,
}

_CONTAINER_NAME: Final = "app"

# Startup probe budget: 60 x 5s = 300s. Migrations run BEFORE the server binds (the
# Dockerfile chains them with `&&`), so this has to out-wait a cold start plus whatever DDL
# the app has accumulated.
_STARTUP_PROBE_PERIOD_SECONDS: Final = 5
_STARTUP_PROBE_FAILURE_THRESHOLD: Final = 60
_STARTUP_PROBE_INITIAL_DELAY_SECONDS: Final = 5
_STARTUP_PROBE_TIMEOUT_SECONDS: Final = 3

# Bounded retry for a transient ARM failure. The failure arm deliberately does NOTHING —
# see `create_or_update`.
_CREATE_ATTEMPTS: Final = 4
_CREATE_BACKOFF_START_SECONDS: Final = 1.0
_CREATE_BACKOFF_MAX_SECONDS: Final = 8.0

# ARM revision provisioning states that mean "stop waiting". Compared case-insensitively —
# see `_state_of` for why the raw value cannot be trusted to be the string it looks like.
_REVISION_HEALTHY: Final = "provisioned"
_REVISION_TERMINAL_FAILURES: Final = frozenset({"failed", "degraded"})


def _state_of(raw: object) -> str | None:
    """Normalize an ARM provisioning/running state to its bare value.

    The SDK hands these back as ENUM MEMBERS, not strings, and `str()` on one yields
    `RevisionProvisioningState.PROVISIONED` — not `Provisioned`. A live deploy therefore
    reported a perfectly healthy revision as unhealthy and would have waited out the whole
    readiness budget before failing a deploy that had already succeeded. Read `.value` when
    it is there, and lower-case so a future casing change is not a third incarnation of this
    same bug."""
    if raw is None:
        return None
    value = getattr(raw, "value", raw)
    return str(value).lower()


@dataclass(frozen=True)
class RevisionState:
    """What ARM says about the revision this deploy created. States are stored normalized
    (bare value, lower-cased) by `_state_of`."""

    name: str
    provisioning_state: str | None
    running_state: str | None

    @property
    def healthy(self) -> bool:
        return self.provisioning_state == _REVISION_HEALTHY

    @property
    def failed(self) -> bool:
        return (self.provisioning_state or "") in _REVISION_TERMINAL_FAILURES


def _did_it_bind(port: int) -> list[aca_models.ContainerAppProbe]:
    """A single TCP startup probe on the app port.

    TCP, NOT HTTP. An HTTP probe against `/` would depend on an agent-authored route
    existing and answering 2xx — so a bug in the citizen's home page would read as "the
    container is dead" and fail an otherwise fine deploy. A TCP probe passes the instant
    `node server.js` binds, which is the honest "it started" signal.

    NO READINESS PROBE: ACA's default (running means ready) is correct here.

    NO LIVENESS PROBE, for the same reason the sandbox refuses one — a failing liveness
    probe makes ACA restart the container, and against an agent-authored route that is a
    flap generator, not a fix. Restarting is strictly worse than reporting.
    """
    return [
        aca_models.ContainerAppProbe(
            type="Startup",
            tcp_socket=aca_models.ContainerAppProbeTcpSocket(port=port),
            initial_delay_seconds=_STARTUP_PROBE_INITIAL_DELAY_SECONDS,
            period_seconds=_STARTUP_PROBE_PERIOD_SECONDS,
            failure_threshold=_STARTUP_PROBE_FAILURE_THRESHOLD,
            timeout_seconds=_STARTUP_PROBE_TIMEOUT_SECONDS,
        )
    ]


def _managed_environment_id(config: DeployConfig) -> str:
    return (
        f"/subscriptions/{config.subscription_id}"
        f"/resourceGroups/{config.resource_group}"
        f"/providers/Microsoft.App/managedEnvironments/{config.managed_environment_name}"
    )


def image_of(app: aca_models.ContainerApp) -> str | None:
    """The image reference the app is ACTUALLY running, read back from ARM.

    This is the reconciler's proof of ownership. After a crash it may promote a deployment
    row to live only when the digest ARM reports matches the one it built — and it may never
    delete a container app it cannot prove it created. Loosely typed by the SDK, so every
    hop is None-guarded and the leaf is coerced."""
    props = app.properties
    template = props.template if props else None
    containers = template.containers if template else None
    if not containers:
        return None
    image = containers[0].image
    return str(image) if image else None


# Three narrow seams rather than one wide one. Each names exactly the ARM operations its
# consumer performs, which makes the blast radius of this feature legible — the reconciler
# can only READ, the teardown can only DELETE — and lets a test stub the two methods a case
# actually exercises instead of six it does not.
#
# `AcaPublishedApps` satisfies all three structurally; nothing needs to declare it.


class PublishedAppProvisioner(Protocol):
    """What the deploy pipeline does: create the app and watch its revision come up."""

    @property
    def config(self) -> DeployConfig: ...

    async def create_or_update(
        self,
        *,
        app_id: uuid.UUID,
        deployment_id: uuid.UUID,
        image: str,
        env: dict[str, str],
        container_url: str | None,
    ) -> str: ...

    async def get_revision(
        self, *, app_id: uuid.UUID, deployment_id: uuid.UUID
    ) -> RevisionState: ...


class PublishedAppReader(Protocol):
    """What the crash reconciler does: ask ARM what is live. READ ONLY, deliberately — a
    reconciler may promote a row it did not write, but it must never be able to delete a
    container app, and the type is where that is guaranteed rather than remembered."""

    async def get_app_fqdn(self, *, app_id: uuid.UUID) -> str | None: ...

    async def get_app_image(self, *, app_id: uuid.UUID) -> str | None: ...


class PublishedAppRemover(Protocol):
    """What the delete paths do."""

    async def delete_app(self, *, app_id: uuid.UUID) -> None: ...


class AcaPublishedApps:
    """Async facade over the sync ACA management client, for published apps."""

    def __init__(self, config: DeployConfig) -> None:
        self._config = config
        self._credential = DefaultAzureCredential()
        self._client = ContainerAppsAPIClient(self._credential, config.subscription_id)

    @property
    def config(self) -> DeployConfig:
        """The block this client was built from. Public so the pipeline can compose an image
        reference from the same registry the container will pull it from — deriving that
        from a second source is how the two drift."""
        return self._config

    # --- the envelope -------------------------------------------------------------

    def envelope(
        self,
        *,
        app_id: uuid.UUID,
        deployment_id: uuid.UUID,
        image: str,
        env: dict[str, str],
        container_url: str | None,
    ) -> aca_models.ContainerApp:
        """The container-app spec for one published deploy.

        Pure — no I/O, no SDK calls — so it can be asserted on directly in tests without
        any Azure at all. That matters: every hazard this module exists to avoid (the
        supervisor probe, the wrong port, a tag instead of a digest, a plaintext DSN) is
        visible in the returned object."""
        c = self._config
        secrets = [
            aca_models.Secret(name=_ACR_PASSWORD_SECRET, value=c.acr_password.get_secret_value())
        ]
        container_env: list[aca_models.EnvironmentVar] = []
        for name, value in sorted(env.items()):
            secret_name = _SECRET_ENV.get(name)
            if secret_name is None:
                container_env.append(aca_models.EnvironmentVar(name=name, value=value))
                continue
            secrets.append(aca_models.Secret(name=secret_name, value=value))
            container_env.append(aca_models.EnvironmentVar(name=name, secret_ref=secret_name))
        if container_url is not None:
            container_env.append(
                aca_models.EnvironmentVar(name="BIAL_BLOB_CONTAINER_URL", value=container_url)
            )

        # The probe port and the ingress port are the SAME knob, read once. Threading it
        # through is what stops a port change from silently leaving the probe knocking on a
        # door nobody answers — the failure mode that makes the sandbox's own probe
        # unusable here.
        probes = _did_it_bind(c.target_port)

        return aca_models.ContainerApp(
            location=c.region,
            properties=aca_models.ContainerAppProperties(
                managed_environment_id=_managed_environment_id(c),
                configuration=aca_models.Configuration(
                    active_revisions_mode="Single",
                    secrets=secrets,
                    registries=[
                        aca_models.RegistryCredentials(
                            server=c.acr_server,
                            username=c.acr_username,
                            password_secret_ref=_ACR_PASSWORD_SECRET,
                        )
                    ],
                    ingress=aca_models.Ingress(
                        # `external=True` means reachable OUTSIDE the Container Apps
                        # environment — `False` restricts it to callers inside the
                        # environment itself. Whether the environment's OWN network
                        # posture (VNet integration, internal load balancer) further
                        # restricts "outside the environment" to the corporate network is
                        # UNCONFIRMED — see `deploy/config.py`'s `ingress` field comment
                        # for the command that settles it. Do not read `external=True`
                        # here as "reachable from the public internet" or "corporate
                        # network only" without checking the live resource first.
                        external=(c.ingress == "external"),
                        target_port=c.target_port,
                        transport="auto",
                        allow_insecure=False,
                    ),
                ),
                template=aca_models.Template(
                    # A NEW revision per deploy. Without this, redeploying an unchanged tree
                    # produces an identical template, ACA creates no revision at all, and the
                    # pipeline polls forever for something that never appears.
                    revision_suffix=revision_suffix(deployment_id),
                    containers=[
                        aca_models.Container(
                            name=_CONTAINER_NAME,
                            image=image,
                            resources=aca_models.ContainerResources(cpu=c.cpu, memory=c.memory),
                            env=container_env,
                            probes=probes,
                        )
                    ],
                    scale=aca_models.Scale(
                        min_replicas=c.min_replicas, max_replicas=c.max_replicas
                    ),
                ),
            ),
        )

    # --- operations ---------------------------------------------------------------

    async def create_or_update(
        self,
        *,
        app_id: uuid.UUID,
        deployment_id: uuid.UUID,
        image: str,
        env: dict[str, str],
        container_url: str | None,
    ) -> str:
        """Create or update the published container app; return its ingress FQDN.

        THE FAILURE ARM DELIBERATELY CLEANS UP NOTHING. The sandbox's equivalent
        (`AcaSandboxClient._create_with_retry`) tears the app down when the retries are
        exhausted, which is right for an ephemeral container nobody is using yet — and
        catastrophic here. A redeploy targets the app that is CURRENTLY SERVING the
        citizen's users; deleting it because ARM was throttled would turn a transient blip
        into an outage with no path back."""
        name = published_app_name(app_id)
        envelope = self.envelope(
            app_id=app_id,
            deployment_id=deployment_id,
            image=image,
            env=env,
            container_url=container_url,
        )

        def _run() -> str:
            poller = self._client.container_apps.begin_create_or_update(
                self._config.resource_group, name, envelope
            )
            fqdn = fqdn_of(await_lro(poller, ceiling=float(self._config.provision_timeout_s)))
            if fqdn is None:
                raise AcaError("ACA returned no ingress FQDN for the published app")
            return fqdn

        backoff = _CREATE_BACKOFF_START_SECONDS
        last: AcaTransientError | None = None
        for attempt in range(1, _CREATE_ATTEMPTS + 1):
            try:
                fqdn: str = await self._call(_run, op="create")
                return fqdn
            except AcaTransientError as exc:
                last = exc
                if attempt == _CREATE_ATTEMPTS:
                    break
                _log.warning(
                    "published_app_create_retrying",
                    app_id=str(app_id),
                    attempt=attempt,
                    error=str(exc),
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, _CREATE_BACKOFF_MAX_SECONDS)
        assert last is not None
        raise last

    async def get_revision(self, *, app_id: uuid.UUID, deployment_id: uuid.UUID) -> RevisionState:
        """Poll the revision this deploy created, by its deterministic name.

        `create_or_update` returning an FQDN proves the APP exists, not that the new
        REVISION is healthy — in Single mode ARM can settle the app while the revision
        fails to activate. This is the only honest readiness signal."""
        app_name = published_app_name(app_id)
        name = f"{app_name}--{revision_suffix(deployment_id)}"

        def _run() -> RevisionState:
            revision = self._client.container_apps_revisions.get_revision(
                self._config.resource_group, app_name, name
            )
            props = revision.properties if revision else None
            return RevisionState(
                name=name,
                provisioning_state=_state_of(props.provisioning_state if props else None),
                running_state=_state_of(props.running_state if props else None),
            )

        try:
            state: RevisionState = await self._call(_run, op="get_revision")
            return state
        except AcaError as exc:
            # A revision that does not exist YET is a normal mid-provision state, not a
            # failure — report it as unknown and let the caller keep polling to its own
            # deadline.
            if isinstance(exc, AcaTransientError):
                raise
            return RevisionState(name=name, provisioning_state=None, running_state=None)

    async def get_app_fqdn(self, *, app_id: uuid.UUID) -> str | None:
        """The published app's FQDN, or `None` when it CONFIRMED does not exist.

        The three-answer discipline is load-bearing for the reconciler: `None` means
        confirmed-absent, a transient ARM blip RAISES. Collapsing those would let a
        throttled request read as "gone" and delete a live application."""

        def _run() -> str | None:
            return fqdn_of(
                self._client.container_apps.get(
                    self._config.resource_group, published_app_name(app_id)
                )
            )

        fqdn: str | None = await self._call(_run, op="get", absent_is_none=True)
        return fqdn

    async def get_app_image(self, *, app_id: uuid.UUID) -> str | None:
        """The image the app is actually running — the reconciler's proof of ownership."""

        def _run() -> str | None:
            return image_of(
                self._client.container_apps.get(
                    self._config.resource_group, published_app_name(app_id)
                )
            )

        image: str | None = await self._call(_run, op="get_image", absent_is_none=True)
        return image

    async def delete_app(self, *, app_id: uuid.UUID) -> None:
        """Idempotent delete. An already-absent app is a no-op."""
        name = published_app_name(app_id)
        if not name.startswith(PUBLISHED_NAME_PREFIX):  # pragma: no cover - defence in depth
            raise AcaError("refusing to delete a container app outside the published namespace")

        def _run() -> None:
            poller = self._client.container_apps.begin_delete(self._config.resource_group, name)
            await_lro(poller, ceiling=float(self._config.provision_timeout_s))

        await self._call(_run, op="delete", absent_is_none=True)

    async def aclose(self) -> None:
        def _close() -> None:
            self._client.close()
            self._credential.close()

        await asyncio.to_thread(_close)

    # --- the shared ARM call shape -------------------------------------------------

    async def _call(self, run: Any, *, op: str, absent_is_none: bool = False) -> Any:
        """One exception-mapping funnel for every ARM call, mirroring `sandbox/aca.py`:

        Returns `Any` rather than a type variable: `T` appears in no parameter, so it could
        only ever be inferred from the call site — which makes it decoration, not a check.
        The callers annotate their own results instead.

        request/response errors and 429/5xx are retryable; any other 4xx is terminal; a 404
        is either absence or a no-op depending on the caller."""
        try:
            return await asyncio.to_thread(run)
        except ResourceNotFoundError:
            if absent_is_none:
                return None
            raise AcaError(f"ACA {op} target not found") from None
        except (ServiceRequestError, ServiceResponseError) as exc:
            raise AcaTransientError(f"ACA {op} request failed") from exc
        except HttpResponseError as exc:
            if exc.status_code == 404 and absent_is_none:
                return None
            if is_transient(exc):
                raise AcaTransientError(f"ACA {op} was throttled or 5xx'd") from exc
            raise AcaError(f"ACA {op} failed") from exc


# --- the process-wide singleton ---------------------------------------------------

_published_apps: AcaPublishedApps | None = None


class DeployNotConfiguredError(Exception):
    """`DEPLOY__*` is unset — publishing is off on this deployment. A supported posture
    (dev, test, and any environment that has not been granted the registry role yet), so
    the route maps this to a documented 503 rather than a 500."""


def get_published_apps() -> AcaPublishedApps:
    global _published_apps
    if _published_apps is None:
        from src.config import settings  # lazy: avoid an import cycle via src.config

        if settings.deploy is None:
            raise DeployNotConfiguredError(
                "publishing is not configured: set the DEPLOY__* block."
            )
        _published_apps = AcaPublishedApps(settings.deploy)
    return _published_apps


async def aclose_published_apps() -> None:
    """Close the mgmt client and the managed-identity credential. Wired into the app
    lifespan — without it this leaks a second `DefaultAzureCredential`'s token cache and
    HTTP pool alongside the sandbox's."""
    global _published_apps
    client, _published_apps = _published_apps, None
    if client is not None:
        await client.aclose()


def set_published_apps_for_tests(client: AcaPublishedApps | None) -> None:
    global _published_apps
    _published_apps = client
