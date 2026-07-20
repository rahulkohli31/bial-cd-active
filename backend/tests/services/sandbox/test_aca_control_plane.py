"""U2 — the raw ACA control-plane (`AcaControlPlane`, the lower C2 seam) error triage.

The sync `azure-mgmt-appcontainers` client is fully mocked (a `SimpleNamespace` whose
`container_apps` methods return a canned poller or raise a canned Azure exception), so
`create_app` / `delete_app` / `get_app_fqdn` are exercised with NO live Azure. The point
is the exception mapping: `ServiceRequestError`/`ServiceResponseError` and a throttled/5xx
`HttpResponseError` become the retryable `AcaTransientError`; a 4xx (other than the swallowed
404) becomes the terminal `AcaError`; `ResourceNotFoundError`/404 on delete/get is a no-op.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from azure.core.exceptions import (
    HttpResponseError,
    ResourceNotFoundError,
    ServiceRequestError,
    ServiceResponseError,
)
from pydantic import SecretStr

from src.services.sandbox import aca as aca_module
from src.services.sandbox.aca import AcaControlPlane, AcaError, AcaTransientError, _is_transient
from src.services.sandbox.config import SandboxConfig

_ENV = {"BIAL_APP_ID": "x", "SUPERVISOR_TOKEN": "t"}


def _config() -> SandboxConfig:
    return SandboxConfig(
        subscription_id="sub",
        resource_group="rg",
        region="westeurope",
        managed_environment_name="aca-env",
        acr_server="bialgenaicr01.azurecr.io",
        acr_username="acr-user",
        acr_password=SecretStr("acr-pass"),
        image_ref="bialgenaicr01.azurecr.io/citizen-dev-sandbox:latest",
        app_data_base_url="https://platform.example/v1",
    )


def _http_error(status_code: int) -> HttpResponseError:
    err = HttpResponseError(message=f"HTTP {status_code}")
    err.status_code = status_code
    return err


def _control_plane(
    monkeypatch: pytest.MonkeyPatch, container_apps: SimpleNamespace
) -> AcaControlPlane:
    """A real `AcaControlPlane` whose credential + mgmt client are mocked out — the concrete
    methods run, only the SDK boundary is faked."""
    monkeypatch.setattr(aca_module, "DefaultAzureCredential", lambda: SimpleNamespace())
    monkeypatch.setattr(
        aca_module,
        "ContainerAppsAPIClient",
        lambda credential, subscription_id: SimpleNamespace(container_apps=container_apps),
    )
    return AcaControlPlane(_config())


def _raises(method: str, exc: BaseException) -> SimpleNamespace:
    def _boom(*_a: object, **_k: object) -> object:
        raise exc

    return SimpleNamespace(**{method: _boom})


def _fake_app(fqdn: str | None) -> SimpleNamespace:
    return SimpleNamespace(
        properties=SimpleNamespace(
            configuration=SimpleNamespace(ingress=SimpleNamespace(fqdn=fqdn))
        )
    )


# --- _is_transient threshold -------------------------------------------------


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (429, True),
        (500, True),
        (503, True),
        (499, False),
        (428, False),
        (400, False),
        (404, False),
    ],
)
def test_is_transient_threshold(status: int, expected: bool) -> None:
    # Retry only on 429 or >= 500; every other 4xx is terminal.
    assert _is_transient(_http_error(status)) is expected


def test_is_transient_none_status_is_terminal() -> None:
    err = HttpResponseError(message="no status")
    assert err.status_code is None
    assert _is_transient(err) is False


# --- create_app --------------------------------------------------------------


async def test_create_app_returns_ingress_fqdn(monkeypatch: pytest.MonkeyPatch) -> None:
    poller = SimpleNamespace(result=lambda: _fake_app("app-xyz.westeurope.azurecontainerapps.io"))
    ca = SimpleNamespace(begin_create_or_update=lambda *a, **k: poller)
    cp = _control_plane(monkeypatch, ca)
    assert (
        await cp.create_app(name="sbx-x", env=_ENV) == "app-xyz.westeurope.azurecontainerapps.io"
    )


async def test_create_app_missing_fqdn_raises_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    poller = SimpleNamespace(result=lambda: _fake_app(None))
    ca = SimpleNamespace(begin_create_or_update=lambda *a, **k: poller)
    cp = _control_plane(monkeypatch, ca)
    with pytest.raises(AcaError) as ei:
        await cp.create_app(name="sbx-x", env=_ENV)
    assert not isinstance(ei.value, AcaTransientError)  # a bad response is terminal, not retryable


# --- ACR pull auth (G2: the registries block lets ACA pull a private-ACR image) ----


def test_envelope_wires_acr_registry_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    # Without a `registries` credential ACA cannot pull the private sandbox image, so the
    # container-app spec must carry one — with the ACR password stored as an ACA secret and
    # referenced by name (never inlined on the registry credential in the spec).
    cp = _control_plane(monkeypatch, SimpleNamespace())
    props = cp._envelope(_ENV).properties
    assert props is not None
    config = props.configuration
    assert config is not None

    registries = config.registries
    assert registries is not None and len(registries) == 1
    reg = registries[0]
    assert reg.server == "bialgenaicr01.azurecr.io"
    assert reg.username == "acr-user"
    assert reg.password_secret_ref == "acr-password"  # a secret reference, not the plaintext

    secrets = config.secrets
    assert secrets is not None and len(secrets) == 1
    # The referenced secret holds the unwrapped password (SecretStr unwrapped only here, at
    # the SDK boundary — security.md), keyed by the exact name the credential references.
    assert secrets[0].name == reg.password_secret_ref
    assert secrets[0].value == "acr-pass"


@pytest.mark.parametrize(
    "exc",
    [
        ServiceRequestError("request blip"),
        ServiceResponseError("response blip"),
        _http_error(429),
        _http_error(500),
        _http_error(503),
    ],
)
async def test_create_app_maps_transient(
    monkeypatch: pytest.MonkeyPatch, exc: BaseException
) -> None:
    cp = _control_plane(monkeypatch, _raises("begin_create_or_update", exc))
    with pytest.raises(AcaTransientError):
        await cp.create_app(name="sbx-x", env=_ENV)


@pytest.mark.parametrize("status", [400, 404])
async def test_create_app_maps_terminal(monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    cp = _control_plane(monkeypatch, _raises("begin_create_or_update", _http_error(status)))
    with pytest.raises(AcaError) as ei:
        await cp.create_app(name="sbx-x", env=_ENV)
    assert not isinstance(ei.value, AcaTransientError)


# --- delete_app (idempotent — an already-absent app is a no-op) ---------------


@pytest.mark.parametrize("absent", [ResourceNotFoundError("gone"), _http_error(404)])
async def test_delete_app_swallows_absent(
    monkeypatch: pytest.MonkeyPatch, absent: BaseException
) -> None:
    cp = _control_plane(monkeypatch, _raises("begin_delete", absent))
    await cp.delete_app(name="sbx-x")  # no raise — already gone


async def test_delete_app_success(monkeypatch: pytest.MonkeyPatch) -> None:
    poller = SimpleNamespace(result=lambda: None)
    ca = SimpleNamespace(begin_delete=lambda *a, **k: poller)
    cp = _control_plane(monkeypatch, ca)
    await cp.delete_app(name="sbx-x")  # clean delete, no raise


@pytest.mark.parametrize(
    "exc",
    [
        ServiceRequestError("request blip"),
        ServiceResponseError("response blip"),
        _http_error(429),
        _http_error(500),
    ],
)
async def test_delete_app_maps_transient(
    monkeypatch: pytest.MonkeyPatch, exc: BaseException
) -> None:
    cp = _control_plane(monkeypatch, _raises("begin_delete", exc))
    with pytest.raises(AcaTransientError):
        await cp.delete_app(name="sbx-x")


async def test_delete_app_maps_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    cp = _control_plane(monkeypatch, _raises("begin_delete", _http_error(400)))
    with pytest.raises(AcaError) as ei:
        await cp.delete_app(name="sbx-x")
    assert not isinstance(ei.value, AcaTransientError)


# --- get_app_fqdn (absent -> None: the confirmed-gone signal) ----------------


async def test_get_app_fqdn_returns_fqdn(monkeypatch: pytest.MonkeyPatch) -> None:
    ca = SimpleNamespace(get=lambda *a, **k: _fake_app("app-xyz.westeurope.azurecontainerapps.io"))
    cp = _control_plane(monkeypatch, ca)
    assert await cp.get_app_fqdn(name="sbx-x") == "app-xyz.westeurope.azurecontainerapps.io"


@pytest.mark.parametrize("absent", [ResourceNotFoundError("gone"), _http_error(404)])
async def test_get_app_fqdn_absent_returns_none(
    monkeypatch: pytest.MonkeyPatch, absent: BaseException
) -> None:
    cp = _control_plane(monkeypatch, _raises("get", absent))
    assert await cp.get_app_fqdn(name="sbx-x") is None  # confirmed-absent, never a raise


@pytest.mark.parametrize(
    "exc",
    [
        ServiceRequestError("request blip"),
        ServiceResponseError("response blip"),
        _http_error(429),
        _http_error(500),
    ],
)
async def test_get_app_fqdn_maps_transient(
    monkeypatch: pytest.MonkeyPatch, exc: BaseException
) -> None:
    # A transient ARM blip must NOT read as "gone" (that would restore + double-allocate the
    # live original) — it stays retryable.
    cp = _control_plane(monkeypatch, _raises("get", exc))
    with pytest.raises(AcaTransientError):
        await cp.get_app_fqdn(name="sbx-x")


async def test_get_app_fqdn_maps_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    cp = _control_plane(monkeypatch, _raises("get", _http_error(400)))
    with pytest.raises(AcaError) as ei:
        await cp.get_app_fqdn(name="sbx-x")
    assert not isinstance(ei.value, AcaTransientError)
