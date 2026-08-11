"""The published-app container spec.

`envelope()` is a pure function and the ARM models construct entirely offline, so every
hazard this module exists to avoid can be asserted directly on the returned object with no
Azure, no mocks and no network. That is the point: each of these assertions corresponds to
a failure that would otherwise present as a SUCCESSFUL deploy serving a broken URL.

The exception-mapping tests mirror `tests/services/sandbox/test_aca_control_plane.py` — the
SDK is a `SimpleNamespace`, and what is under test is the triage, not Azure.
"""

from __future__ import annotations

import uuid
from enum import StrEnum
from types import SimpleNamespace
from typing import Any, cast

import pytest
from azure.core.exceptions import (
    HttpResponseError,
    ResourceNotFoundError,
    ServiceRequestError,
    ServiceResponseError,
)
from azure.mgmt.appcontainers import models as aca_models
from pydantic import SecretStr

from src.services.deploy import aca_publish as publish_module
from src.services.deploy.aca_publish import (
    PUBLISHED_NAME_PREFIX,
    AcaPublishedApps,
    RevisionState,
    image_of,
)
from src.services.deploy.config import DeployConfig
from src.services.deploy.names import image_reference, published_app_name, revision_suffix
from src.services.sandbox.aca import AcaError, AcaTransientError
from src.services.sandbox.base import SANDBOX_NAME_PREFIX

_APP_ID = uuid.UUID("11111111-2222-3333-4444-555555555555")
_DEPLOY_ID = uuid.UUID("aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee")
_DIGEST = "sha256:" + "ab" * 32
_ENV = {
    "BIAL_APP_ID": str(_APP_ID),
    "BIAL_PORTAL_ORIGIN": "https://portal.bial.example",
    "BIAL_DATABASE_URL": "postgresql://app:s3cret@pg.example/app_db?sslmode=require",
    "BIAL_BLOB_SAS": "sv=2024&sig=verysecret",
}


def _config(**overrides: Any) -> DeployConfig:
    base: dict[str, Any] = {
        "acr_server": "bialgenaicr.azurecr.io",
        "acr_name": "bialgenaicr",
        "acr_resource_group": "BIAL-GENAI-AIML-RG",
        "acr_subscription_id": "sub-acr",
        "acr_username": "bialgenaicr",
        "acr_password": SecretStr("acr-pass"),
        "subscription_id": "sub",
        "resource_group": "rg",
        "region": "centralindia",
        "managed_environment_name": "bial-citizen-dev-aca-env",
    }
    values: dict[str, Any] = {**base, **overrides}
    return DeployConfig(**values)


def _client(monkeypatch: pytest.MonkeyPatch, container_apps: object = None) -> AcaPublishedApps:
    monkeypatch.setattr(publish_module, "DefaultAzureCredential", lambda: SimpleNamespace())
    monkeypatch.setattr(
        publish_module,
        "ContainerAppsAPIClient",
        lambda credential, subscription_id: SimpleNamespace(
            container_apps=container_apps, container_apps_revisions=container_apps
        ),
    )
    return AcaPublishedApps(_config())


def _envelope(*, config: dict[str, Any] | None = None, **overrides: object):
    """A client with only its config, so `envelope()` can be exercised without touching a
    credential or a mgmt client — it is a pure function of the config and its arguments."""
    client = AcaPublishedApps.__new__(AcaPublishedApps)
    client._config = _config(**(config or {}))
    return client.envelope(
        app_id=_APP_ID,
        deployment_id=_DEPLOY_ID,
        image=image_reference(
            acr_server="bialgenaicr.azurecr.io",
            repository_prefix="citizen-apps",
            app_id=_APP_ID,
            digest=_DIGEST,
        ),
        env=dict(_ENV),
        container_url="https://acct.blob.core.windows.net/app-x",
        **overrides,
    )


def _properties(envelope: aca_models.ContainerApp) -> aca_models.ContainerAppProperties:
    """Narrow the SDK's Optional chain once.

    Every leaf on a generated ARM model is `X | None`, so asserting at each access would bury
    the actual subject of these tests under None-guards. The assertions belong here — if the
    envelope is missing a whole section, that IS the failure and it should be reported once."""
    props = envelope.properties
    assert props is not None
    return props


def _configuration(envelope: aca_models.ContainerApp) -> aca_models.Configuration:
    config = _properties(envelope).configuration
    assert config is not None
    return config


def _template(envelope: aca_models.ContainerApp) -> aca_models.Template:
    template = _properties(envelope).template
    assert template is not None
    return template


def _container(envelope: aca_models.ContainerApp) -> aca_models.Container:
    containers = _template(envelope).containers
    assert containers
    return containers[0]


def _probes(envelope: aca_models.ContainerApp) -> list[aca_models.ContainerAppProbe]:
    probes = _container(envelope).probes
    assert probes is not None
    return list(probes)


def _env(envelope: aca_models.ContainerApp) -> dict[str, aca_models.EnvironmentVar]:
    variables = _container(envelope).env
    assert variables is not None
    return {v.name: v for v in variables if v.name}


def _ingress(envelope: aca_models.ContainerApp) -> aca_models.Ingress:
    ingress = _configuration(envelope).ingress
    assert ingress is not None
    return ingress


def _tcp_port(probe: aca_models.ContainerAppProbe) -> int | None:
    assert probe.tcp_socket is not None
    return probe.tcp_socket.port


# --- the probe: the trap that fails as a success ------------------------------------


def test_the_probe_does_not_look_for_a_supervisor() -> None:
    """A published container has no Caddy and no supervisor. If this ever regressed to the
    sandbox's probe, the revision would never go healthy, ACA would still return an FQDN,
    and the deploy would report success over a URL that 5xx's forever."""
    probes = _probes(_envelope())

    assert len(probes) == 1
    assert probes[0].http_get is None
    assert probes[0].tcp_socket is not None


def test_the_probe_targets_the_app_port_not_the_sandbox_port() -> None:
    probes = _probes(_envelope())
    assert _tcp_port(probes[0]) == 3000
    assert _tcp_port(probes[0]) != 8080


def test_the_probe_port_follows_the_ingress_port() -> None:
    """One knob, read once — a port change must not leave the probe knocking on a door
    nobody answers."""
    envelope = _envelope(config={"target_port": 4123})
    assert _tcp_port(_probes(envelope)[0]) == 4123
    assert _ingress(envelope).target_port == 4123


def test_there_is_no_liveness_probe() -> None:
    """A failing liveness probe makes ACA RESTART the container. Against an agent-authored
    home page that is a flap generator, not a fix."""
    assert all(p.type != "Liveness" for p in _probes(_envelope()))


# --- the image: pinned, never tagged -------------------------------------------------


def test_the_image_is_digest_pinned() -> None:
    """ACA resolves a TAG once, at revision creation, and never notices a later push — a
    tagged app looks deployed while serving whatever the tag meant back then."""
    image = _container(_envelope()).image
    assert image is not None and "@sha256:" in image


# --- secrets: never inline in the template -------------------------------------------


def test_the_database_url_rides_a_secret_reference() -> None:
    """A plain EnvironmentVar would put the DSN in `az containerapp show` output and in
    every ARM activity log entry."""
    env = _env(_envelope())

    assert env["BIAL_DATABASE_URL"].value is None
    assert env["BIAL_DATABASE_URL"].secret_ref == "bial-database-url"


def test_the_blob_sas_rides_a_secret_reference() -> None:
    env = _env(_envelope())
    assert env["BIAL_BLOB_SAS"].value is None
    assert env["BIAL_BLOB_SAS"].secret_ref == "bial-blob-sas"


def test_no_secret_value_appears_as_a_plain_env_var() -> None:
    plain = [v.value for v in _env(_envelope()).values() if v.value is not None]
    assert not any("s3cret" in v for v in plain)
    assert not any("verysecret" in v for v in plain)


def test_non_secret_env_stays_plain() -> None:
    """Over-secreting is its own bug: these are read by the browser-facing shim and are not
    credentials."""
    env = _env(_envelope())
    assert env["BIAL_APP_ID"].value == str(_APP_ID)
    assert env["BIAL_PORTAL_ORIGIN"].value == "https://portal.bial.example"
    container_url = env["BIAL_BLOB_CONTAINER_URL"].value
    assert container_url is not None and container_url.endswith("/app-x")


# --- scale, revisions, ingress --------------------------------------------------------


def test_published_apps_scale_to_zero() -> None:
    scale = _template(_envelope()).scale
    assert scale is not None
    assert scale.min_replicas == 0
    assert scale.max_replicas == 2


def test_every_deploy_forces_a_new_revision() -> None:
    """Without a per-deploy suffix an unchanged redeploy produces an identical template,
    ACA creates no revision, and the pipeline polls forever for one that never appears."""
    assert _template(_envelope()).revision_suffix == revision_suffix(_DEPLOY_ID)


def test_single_revision_mode() -> None:
    assert _configuration(_envelope()).active_revisions_mode == "Single"


def test_ingress_is_external_at_the_app_level() -> None:
    """Verified: `external=True`, reachable outside the Container Apps environment.
    UNCONFIRMED, and deliberately not asserted here: whether the managed environment's own
    network posture (VNet integration, internal load balancer) further restricts that to
    the corporate network — see the comment on the `Ingress(...)` call this reads for the
    command that would settle it."""
    ingress = _ingress(_envelope())
    assert ingress.external is True
    assert ingress.allow_insecure is False


def test_the_container_is_not_named_sandbox() -> None:
    assert _container(_envelope()).name == "app"


def test_the_acr_password_is_a_secret_reference_on_the_registry() -> None:
    registries = _configuration(_envelope()).registries
    assert registries is not None
    assert registries[0].password_secret_ref == "acr-password"
    assert not hasattr(registries[0], "password_value")


# --- naming and the reaper ------------------------------------------------------------


def test_the_published_namespace_cannot_overlap_the_sandbox_one() -> None:
    assert PUBLISHED_NAME_PREFIX != SANDBOX_NAME_PREFIX
    assert not published_app_name(_APP_ID).startswith(SANDBOX_NAME_PREFIX)


# --- revision state -------------------------------------------------------------------


def test_an_enum_provisioning_state_reads_as_healthy() -> None:
    """FOUND IN A LIVE DEPLOY. The SDK hands these back as ENUM MEMBERS, and `str()` on one
    yields `RevisionProvisioningState.PROVISIONED` — not `Provisioned`. The revision was
    perfectly healthy and reported unhealthy, which would have burned the whole readiness
    budget and then failed a deploy that had already succeeded."""

    class RevisionProvisioningState(StrEnum):
        PROVISIONED = "Provisioned"
        FAILED = "Failed"

    assert publish_module._state_of(RevisionProvisioningState.PROVISIONED) == "provisioned"
    healthy = RevisionState(
        name="r",
        provisioning_state=publish_module._state_of(RevisionProvisioningState.PROVISIONED),
        running_state=None,
    )
    assert healthy.healthy is True
    assert healthy.failed is False


def test_an_enum_failure_state_reads_as_failed() -> None:
    class RevisionProvisioningState(StrEnum):
        FAILED = "Failed"

    failed = RevisionState(
        name="r",
        provisioning_state=publish_module._state_of(RevisionProvisioningState.FAILED),
        running_state=None,
    )
    assert failed.failed is True
    assert failed.healthy is False


@pytest.mark.parametrize("raw", ["Provisioned", "provisioned", "PROVISIONED"])
def test_casing_never_decides_health(raw: str) -> None:
    """Lower-cased on the way in so a future casing change is not a third incarnation of the
    same bug."""
    assert RevisionState(
        name="r", provisioning_state=publish_module._state_of(raw), running_state=None
    ).healthy


def test_an_absent_state_is_neither_healthy_nor_failed() -> None:
    """A revision that does not exist YET is a normal mid-provision state — the caller keeps
    polling rather than concluding either way."""
    unknown = RevisionState(
        name="r", provisioning_state=publish_module._state_of(None), running_state=None
    )
    assert unknown.healthy is False
    assert unknown.failed is False


# --- reading state back ---------------------------------------------------------------


def test_image_of_reads_the_running_reference() -> None:
    # SimpleNamespace stands in for the SDK model: `image_of` walks loosely-typed leaves and
    # None-guards every hop, which is exactly the shape being pinned here.
    app = cast(
        Any,
        SimpleNamespace(
            properties=SimpleNamespace(
                template=SimpleNamespace(containers=[SimpleNamespace(image="reg/app@sha256:beef")])
            )
        ),
    )
    assert image_of(app) == "reg/app@sha256:beef"


def test_image_of_is_none_safe_at_every_hop() -> None:
    assert image_of(cast(Any, SimpleNamespace(properties=None))) is None
    assert image_of(cast(Any, SimpleNamespace(properties=SimpleNamespace(template=None)))) is None


# --- error triage ---------------------------------------------------------------------


async def test_a_missing_app_is_confirmed_absent_not_an_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`None` means CONFIRMED gone. The reconciler acts on that, so it must never be what a
    throttled request looks like."""

    def _boom(*_a: object, **_k: object) -> object:
        raise ResourceNotFoundError(message="gone")

    client = _client(monkeypatch, SimpleNamespace(get=_boom))
    assert await client.get_app_fqdn(app_id=_APP_ID) is None


@pytest.mark.parametrize("exc", [ServiceRequestError(message="net"), ServiceResponseError("net")])
async def test_a_network_blip_raises_rather_than_reporting_absence(
    monkeypatch: pytest.MonkeyPatch, exc: BaseException
) -> None:
    """Collapsing this into `None` would let a blip read as "gone" and delete a live app."""

    def _boom(*_a: object, **_k: object) -> object:
        raise exc

    client = _client(monkeypatch, SimpleNamespace(get=_boom))
    with pytest.raises(AcaTransientError):
        await client.get_app_fqdn(app_id=_APP_ID)


async def test_a_terminal_4xx_is_not_retryable(monkeypatch: pytest.MonkeyPatch) -> None:
    err = HttpResponseError(message="bad")
    err.status_code = 400

    def _boom(*_a: object, **_k: object) -> object:
        raise err

    client = _client(monkeypatch, SimpleNamespace(get=_boom))
    with pytest.raises(AcaError) as caught:
        await client.get_app_fqdn(app_id=_APP_ID)
    assert not isinstance(caught.value, AcaTransientError)


async def test_a_failed_create_never_deletes_the_live_app(monkeypatch: pytest.MonkeyPatch) -> None:
    """THE most important behaviour in this module. The sandbox's equivalent tears the app
    down when retries are exhausted — correct for an ephemeral container nobody is using,
    catastrophic for a redeploy that targets the app currently serving the citizen's users.
    A throttled ARM request must never become an outage with no path back."""
    monkeypatch.setattr(publish_module, "_CREATE_ATTEMPTS", 2)
    monkeypatch.setattr(publish_module, "_CREATE_BACKOFF_START_SECONDS", 0.0)
    deleted: list[str] = []

    def _throttled(*_a: object, **_k: object) -> object:
        err = HttpResponseError(message="429")
        err.status_code = 429
        raise err

    container_apps = SimpleNamespace(
        begin_create_or_update=_throttled,
        begin_delete=lambda *a, **k: deleted.append(a[1]),
    )
    client = _client(monkeypatch, container_apps)

    with pytest.raises(AcaTransientError):
        await client.create_or_update(
            app_id=_APP_ID,
            deployment_id=_DEPLOY_ID,
            image="reg/app@sha256:beef",
            env={},
            container_url=None,
        )
    assert deleted == []
