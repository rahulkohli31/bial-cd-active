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
import datetime as dt
import time
from typing import Any, Final

from azure.core.exceptions import (
    HttpResponseError,
    ResourceNotFoundError,
    ServiceRequestError,
    ServiceResponseError,
)
from azure.identity import DefaultAzureCredential
from azure.mgmt.appcontainers import ContainerAppsAPIClient
from azure.mgmt.appcontainers import models as aca_models

from src.services.sandbox.base import SANDBOX_NAME_PREFIX, FleetMember, checked_tags
from src.services.sandbox.config import SandboxConfig

# The in-container Caddy fronts a single ACA ingress port (8080) and routes /_sup/* to
# the supervisor and everything else to `next dev` (sandbox/Caddyfile).
_INGRESS_TARGET_PORT: Final = 8080

# The ACA secret name the ACR registry credential references for its password. ACA
# secret names must be lowercase-alphanumeric-with-dashes; the ACR password is stored as
# this secret and referenced by `password_secret_ref` so the plaintext is never inlined
# on the registry credential in the container-app spec.
_ACR_PASSWORD_SECRET_NAME: Final = "acr-password"

# Probe target. This MUST be the supervisor's `/health` behind Caddy's `/_sup/*` block, and
# NOT `/`. At container start nothing is listening on the `next dev` port at all — the control
# plane starts it later via `/dev/start` — so a probe at `/` would get a Caddy 502 forever, the
# revision would never go healthy, and a latency fix would become a total outage. `/health` is
# also the supervisor's ONE unauthenticated route, so the probe needs no bearer token (which it
# could not have anyway: the token is minted per sandbox and lives in the control-plane process).
# `handle_path` strips the prefix, so `/_sup/health` arrives at the supervisor as `/health`.
_SUPERVISOR_HEALTH_PATH: Final = "/_sup/health"

# Without an explicit startup probe ACA applies its own default readiness grace before it will
# route to a new revision (~8s measured on this subscription), which the sandbox pays on EVERY
# provision. The supervisor is a small Python process that answers `/health` almost immediately
# once Caddy is up, so polling at 1s lets the revision go ready as soon as it genuinely is.
_STARTUP_PROBE_PERIOD_SECONDS: Final = 1
# 30 x 1s. Generous against a cold image pull and a slow node, and it only ever costs this much
# on a container that is failing anyway — a healthy one passes on the first or second poll.
_STARTUP_PROBE_FAILURE_THRESHOLD: Final = 30
_READINESS_PROBE_PERIOD_SECONDS: Final = 5
_READINESS_PROBE_FAILURE_THRESHOLD: Final = 3
_PROBE_TIMEOUT_SECONDS: Final = 2


def _are_you_up_yet() -> list[aca_models.ContainerAppProbe]:
    """Startup + readiness probes against the supervisor's unauthenticated `/health`.

    DELIBERATELY NO LIVENESS PROBE. A failing liveness probe makes ACA RESTART the container,
    and a sandbox holds the citizen's un-snapshotted work — a self-restarting sandbox would
    silently discard whatever the agent had written since the last snapshot, to fix a symptom
    the control plane already handles (`selfheal`'s dead-child rescue restarts `next dev`
    without touching the workspace). Restarting is strictly worse than reporting. If you are
    here to "complete the set", that is the argument you have to beat.

    Note both probes watch the SUPERVISOR, never the generated app. `next dev` being up is not
    a container-health question — it is `/dev/status`'s job, and post-U6 that answer means "a
    request was actually served", which a container-level probe has no business deciding.
    """
    knock = aca_models.ContainerAppProbeHttpGet(
        path=_SUPERVISOR_HEALTH_PATH,
        port=_INGRESS_TARGET_PORT,
        scheme="HTTP",
    )
    return [
        aca_models.ContainerAppProbe(
            type="Startup",
            http_get=knock,
            period_seconds=_STARTUP_PROBE_PERIOD_SECONDS,
            failure_threshold=_STARTUP_PROBE_FAILURE_THRESHOLD,
            timeout_seconds=_PROBE_TIMEOUT_SECONDS,
        ),
        aca_models.ContainerAppProbe(
            type="Readiness",
            http_get=knock,
            period_seconds=_READINESS_PROBE_PERIOD_SECONDS,
            failure_threshold=_READINESS_PROBE_FAILURE_THRESHOLD,
            timeout_seconds=_PROBE_TIMEOUT_SECONDS,
        ),
    ]


class AcaError(Exception):
    """A non-retryable ACA control-plane failure (4xx other than 404, bad response)."""


class AcaTransientError(AcaError):
    """A retryable ACA control-plane failure (network blip, throttling, 5xx)."""


def is_transient(exc: HttpResponseError) -> bool:
    """429 or 5xx is worth retrying; every other status is terminal.

    PUBLIC because `services/deploy/aca_publish.py` builds a different container-app shape
    against the same ARM surface and must classify failures identically. A second copy of
    this — or of `await_lro` below — would drift from the fix it implements."""
    code = exc.status_code
    return code is not None and (code == 429 or code >= 500)


# Ceiling on ONE ARM long-running operation (create / delete).
#
# `poller.result()` has no timeout of its own, and every call here runs inside
# `asyncio.to_thread`, which draws from the interpreter's DEFAULT executor —
# `min(32, cpu_count + 4)`, i.e. **six threads on a 2-core App Service plan**. A handful of
# hung ARM operations therefore does not merely stall provisioning: it exhausts the shared
# pool and stalls every other `to_thread` in the process — the reaper's deletes, snapshot
# extraction, offloaded storage calls. A whole-process outage caused by one wedged request.
#
# Note WHERE the bound has to live. Wrapping the `await` in `asyncio.timeout` bounds the
# wait but not the thread: cancelling the await leaves the worker blocked forever, and the
# thread is the resource that actually leaks. So the loop below polls INSIDE the worker and
# returns it to the pool.
_LRO_CEILING_SECONDS: Final = 300.0
_LRO_POLL_STEP_SECONDS: Final = 5.0


def await_lro(poller: Any, *, ceiling: float | None = None) -> Any:
    """Block on an ARM long-running operation, but never past `ceiling`.

    Raises `AcaTransientError` on expiry because the outcome is genuinely UNKNOWN — the
    operation may still land. That classification is deliberate: both callers
    (`begin_create_or_update`, `begin_delete`) are safe to repeat, and treating an unknown
    outcome as a terminal failure would invite a caller to "clean up" a container app that
    is in the middle of being created successfully.

    `ceiling=None` resolves the module default AT CALL TIME rather than through a default
    argument. That is not a style preference: a default argument binds once, at definition,
    so `_LRO_CEILING_SECONDS` could never be lowered for a test — and a test that thought it
    had shortened the ceiling would instead sit through the real 300s and still pass, taking
    five minutes to assert something that should take milliseconds."""
    limit = _LRO_CEILING_SECONDS if ceiling is None else ceiling
    deadline = time.monotonic() + limit
    while not poller.done():
        if time.monotonic() >= deadline:
            raise AcaTransientError(
                f"ACA long-running operation did not settle within {limit:.0f}s"
            )
        poller.wait(timeout=_LRO_POLL_STEP_SECONDS)
    return poller.result()


def _managed_environment_id(config: SandboxConfig) -> str:
    # The Managed Environment name is config-driven (`SANDBOX__MANAGED_ENVIRONMENT_NAME`)
    # so the same image works against any provisioned env (e.g. `bial-dev-aca-env`).
    return (
        f"/subscriptions/{config.subscription_id}"
        f"/resourceGroups/{config.resource_group}"
        f"/providers/Microsoft.App/managedEnvironments/{config.managed_environment_name}"
    )


def fqdn_of(app: aca_models.ContainerApp) -> str | None:
    """Dig the public ingress FQDN out of a container-app model (attributes are loosely
    typed by the SDK, so coerce the leaf to a concrete `str`)."""
    props = app.properties
    configuration = props.configuration if props else None
    ingress = configuration.ingress if configuration else None
    fqdn = ingress.fqdn if ingress else None
    return str(fqdn) if fqdn else None


def _fleet_member_of(app: aca_models.ContainerApp) -> FleetMember:
    """Project one SDK `ContainerApp` down to the five fields a reclamation pass may judge on.

    THE NARROWING IS THE POINT (see `FleetMember`): the list payload carries every app's container
    env in plaintext, so anything that survives this function is something a log line or an
    operator report may end up holding. Read only what is named here.

    NEVER RAISE WITH THE PAYLOAD IN THE MESSAGE. That rule is easy to state and easy to lose:
    validation errors routinely echo the offending object, and the offending object here is the
    thing carrying `SUPERVISOR_TOKEN`. So every leaf is coerced defensively rather than parsed
    strictly — a missing `properties`, a null ingress, an unparseable timestamp all degrade to
    `None`, which the tier logic already treats as "cannot be judged ⇒ escalate". Failing closed on
    a malformed item is correct here; failing loudly with its contents is not.

    `app.name` is checked by the caller before we get here (the prefix filter), so the `str()` is a
    coercion of the SDK's loose typing, not a fallback."""
    props = app.properties
    running = getattr(props, "running_status", None) if props else None
    created = getattr(app.system_data, "created_at", None) if app.system_data else None
    return FleetMember(
        name=str(app.name),
        tags={str(k): str(v) for k, v in (app.tags or {}).items()},
        running_status=_plain(running),
        fqdn=fqdn_of(app),
        arm_created_at=created if isinstance(created, dt.datetime) else None,
    )


def _plain(value: object) -> str | None:
    """Coerce an SDK leaf to a plain string, unwrapping an enum to its VALUE.

    `azure-mgmt-appcontainers` types `running_status` as `ContainerAppRunningStatus`, not `str`,
    and `str()` on a Python enum yields `"ContainerAppRunningStatus.RUNNING"` — the class name
    and the member, not the wire value. Found by running the enumerator against the real dev
    fleet, and invisible to every test here because a fake returns the plain string the real
    client does not. That is the exact shape of "a fake that certifies a fiction": the projection
    promises plain strings, so an operator report would have carried a Python repr where it
    claimed to carry Azure's own status."""
    if value is None:
        return None
    unwrapped = getattr(value, "value", value)
    text = str(unwrapped)
    return text or None


def _env_value_of(app: aca_models.ContainerApp, key: str) -> str | None:
    """One environment variable off the container app's sandbox container, or `None` when the
    app carries no such variable (attributes are loosely typed by the SDK, so coerce the leaf).

    This is how a supervisor bearer survives a control-plane restart. The token is minted per
    container and injected here at create (`_provision_container`), so the container app spec —
    not the control-plane process — is its durable home. Reading it back is strictly cheaper
    than the alternative the absence used to trigger, which was tearing the container down and
    restoring it from the last snapshot."""
    props = app.properties
    template = props.template if props else None
    containers = template.containers if template else None
    if not containers:
        return None
    env = containers[0].env
    if not env:
        return None
    for var in env:
        if var.name == key:
            value = var.value
            return str(value) if value is not None else None
    return None


class AcaControlPlane:
    """Async facade over the sync ACA management client. One instance per configured
    sandbox; holds the managed-identity credential + the mgmt client for its lifetime."""

    def __init__(self, config: SandboxConfig) -> None:
        self._config = config
        self._credential = DefaultAzureCredential()
        self._client = ContainerAppsAPIClient(self._credential, config.subscription_id)

    def _envelope(self, env: dict[str, str], tags: dict[str, str]) -> aca_models.ContainerApp:
        c = self._config
        return aca_models.ContainerApp(
            location=c.region,
            # C10 identity, ON THE ENVELOPE rather than PATCHed on afterwards. A container is
            # judgeable-without-Redis from the FIRST MOMENT it exists, with no window in which a
            # create that succeeded and a follow-up stamp that did not leaves an anonymous
            # container behind — which is the exact population ADR-0029 exists to collect.
            tags=checked_tags(tags),
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
                            probes=_are_you_up_yet(),
                        )
                    ],
                    # Single-replica POC (C5/C7): exactly one container per user.
                    scale=aca_models.Scale(min_replicas=1, max_replicas=1),
                ),
            ),
        )

    async def create_app(self, *, name: str, env: dict[str, str], tags: dict[str, str]) -> str:
        """Create (or update) the container app; return its public ingress FQDN
        (host-only, no scheme). Retryable failures raise `AcaTransientError`.

        `tags` is REQUIRED, not defaulted (`fail-first.md`). There is no deployment in which an
        untagged sandbox is correct — an untagged container is an anonymous container — and a
        default would let a new call site create one silently."""
        envelope = self._envelope(env, tags)

        def _run() -> str:
            poller = self._client.container_apps.begin_create_or_update(
                self._config.resource_group, name, envelope
            )
            fqdn = fqdn_of(await_lro(poller))
            if fqdn is None:
                raise AcaError("ACA returned no ingress FQDN")
            return fqdn

        try:
            return await asyncio.to_thread(_run)
        except (ServiceRequestError, ServiceResponseError) as exc:
            raise AcaTransientError("ACA create request failed") from exc
        except HttpResponseError as exc:
            if is_transient(exc):
                raise AcaTransientError("ACA create was throttled or 5xx'd") from exc
            raise AcaError("ACA create failed") from exc

    async def delete_app(self, *, name: str) -> None:
        """Idempotent delete of the container app: an already-absent app is a no-op
        (404 / ResourceNotFound swallowed). Retryable failures raise `AcaTransientError`."""

        def _run() -> None:
            poller = self._client.container_apps.begin_delete(self._config.resource_group, name)
            await_lro(poller)

        try:
            await asyncio.to_thread(_run)
        except ResourceNotFoundError:
            return
        except (ServiceRequestError, ServiceResponseError) as exc:
            raise AcaTransientError("ACA delete request failed") from exc
        except HttpResponseError as exc:
            if exc.status_code == 404:
                return
            if is_transient(exc):
                raise AcaTransientError("ACA delete was throttled or 5xx'd") from exc
            raise AcaError("ACA delete failed") from exc

    async def list_sandbox_fleet(self) -> list[FleetMember]:
        """Every sandbox container app ARM knows about, projected to what may be judged on (R3).

        THE ONLY AZURE-SIDE VIEW OF THE FLEET, and it exists because the reaper has none.
        `sweep_all` enumerates from the Redis registry, so it can only ever collect containers it
        already has a record of — a sandbox whose registry entry is gone (a flushed Redis, a
        different Redis, a container predating the registry) is invisible to it FOREVER and bills
        until a human notices. One such container ran for twelve days. Inverting that authority is
        the whole of ADR-0029, and this method is where the inversion happens.

        ONE ENUMERATION, NOT THREE. It replaces a name-only lister and a name→tags lister that
        walked the same page set and threw away different halves of it. Every caller now reads the
        same projection, so no two of them can disagree about the fleet, and the fleet is walked
        once per pass instead of once per question.

        PAGING IS THE SDK'S JOB, not ours. `list_by_resource_group` returns an
        `ItemPaged[ContainerApp]` and follows `nextLink` itself; iterating it to exhaustion inside
        the worker thread is what makes a two-page fleet return as one list. "Follow the URI
        verbatim" is unsatisfiable through this surface and does not need to be satisfied.

        Filtered to `SANDBOX_NAME_PREFIX`, so the platform never reports on — let alone offers to
        delete — the published apps and unrelated workloads sharing the resource group.

        A TRUNCATED FLEET MUST NEVER READ AS A CLEAN ONE. Transient ARM failures raise
        `AcaTransientError` rather than returning a short list: a half-enumerated fleet reporting
        "no orphans" (or "nothing left to stamp") is the worst possible output of this function,
        because it is indistinguishable from success and it is what the destroy flag rests on."""

        def _run() -> list[FleetMember]:
            apps = self._client.container_apps.list_by_resource_group(self._config.resource_group)
            return [
                _fleet_member_of(a)
                for a in apps
                if a.name and a.name.startswith(SANDBOX_NAME_PREFIX)
            ]

        try:
            return await asyncio.to_thread(_run)
        except (ServiceRequestError, ServiceResponseError) as exc:
            raise AcaTransientError("ACA list request failed") from exc
        except HttpResponseError as exc:
            if is_transient(exc):
                raise AcaTransientError("ACA list was throttled or 5xx'd") from exc
            raise AcaError("ACA list failed") from exc

    async def stamp_tags(self, *, name: str, tags: dict[str, str]) -> None:
        """MERGE identity tags onto an existing container app (C10 §1.4).

        `begin_update` is ARM `PATCH`, documented as JSON Merge Patch: it adds and overwrites the
        keys given and leaves every other tag alone. `begin_create_or_update` is `PUT` and would
        REPLACE the resource — do not reach for it here. That is not a style preference: tags sit
        outside `properties.template` and `properties.configuration`, so a PATCH creates no
        revision and cannot disturb container env, and container env is the durable home of the
        supervisor bearer. A PUT built from a partial body would take a citizen's live sandbox
        down.

        `location` is passed because the PATCH body schema marks it required, not because anything
        is being moved.

        The result is polled through `await_lro` like every other ARM operation here — ARM answers
        a tag PATCH with 202 often enough that assuming it is synchronous would report success on
        work that had not landed."""
        envelope = aca_models.ContainerApp(location=self._config.region, tags=checked_tags(tags))

        def _run() -> None:
            poller = self._client.container_apps.begin_update(
                self._config.resource_group, name, envelope
            )
            await_lro(poller)

        try:
            await asyncio.to_thread(_run)
        except (ServiceRequestError, ServiceResponseError) as exc:
            raise AcaTransientError("ACA tag update request failed") from exc
        except HttpResponseError as exc:
            if is_transient(exc):
                raise AcaTransientError("ACA tag update was throttled or 5xx'd") from exc
            raise AcaError("ACA tag update failed") from exc

    async def get_app_fqdn(self, *, name: str) -> str | None:
        """The container app's ingress FQDN, or `None` when the app does not exist —
        the confirmed-absent signal `attach_existing` uses to tell a torn-down container
        (→ restore) apart from a transient network blip (→ retry)."""

        def _run() -> str | None:
            return fqdn_of(self._client.container_apps.get(self._config.resource_group, name))

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
            if is_transient(exc):
                raise AcaTransientError("ACA get was throttled or 5xx'd") from exc
            raise AcaError("ACA get failed") from exc

    async def get_app_env_value(self, *, name: str, key: str) -> str | None:
        """One environment variable off a live container app, or `None` when the app is absent
        or carries no such variable. The caller disambiguates those two with `get_app_fqdn`;
        they are only conflated here because the SDK gives one shape for both.

        NEVER log the returned value — this is how the supervisor bearer is recovered
        (`security.md`: never log credential values)."""

        def _run() -> str | None:
            app = self._client.container_apps.get(self._config.resource_group, name)
            return _env_value_of(app, key)

        try:
            return await asyncio.to_thread(_run)
        except ResourceNotFoundError:
            return None
        except (ServiceRequestError, ServiceResponseError) as exc:
            raise AcaTransientError("ACA get request failed") from exc
        except HttpResponseError as exc:
            if exc.status_code == 404:
                return None
            if is_transient(exc):
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
