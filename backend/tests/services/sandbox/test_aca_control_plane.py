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
from azure.mgmt.appcontainers import models as aca_models
from pydantic import SecretStr

from src.services.sandbox import aca as aca_module
from src.services.sandbox.aca import AcaControlPlane, AcaError, AcaTransientError, is_transient
from src.services.sandbox.base import SandboxTagError
from src.services.sandbox.config import SandboxConfig

_ENV = {"BIAL_APP_ID": "x", "SUPERVISOR_TOKEN": "t"}
_TAGS = {"bial-kind": "build-sandbox"}


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
    )


def _settled(value: object) -> SimpleNamespace:
    """A finished ARM long-running operation.

    Models the three members of the real `LROPoller` that `await_lro` touches —
    `done()`, `wait()`, `result()` — not just `result()`. The narrower fake used to pass
    while the production code could block a shared worker thread forever, which is exactly
    the bug `await_lro` exists to prevent; a fake that cannot express "not done yet" cannot
    catch it."""
    return SimpleNamespace(done=lambda: True, wait=lambda timeout=None: None, result=lambda: value)


def _never_settles(*, on_wait=None) -> SimpleNamespace:
    """An operation that never completes — a hung ARM request."""
    return SimpleNamespace(
        done=lambda: False,
        wait=on_wait or (lambda timeout=None: None),
        result=lambda: pytest.fail("result() must never be reached on a hung operation"),
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


# --- is_transient threshold -------------------------------------------------


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
    assert is_transient(_http_error(status)) is expected


def test_is_transient_none_status_is_terminal() -> None:
    err = HttpResponseError(message="no status")
    assert err.status_code is None
    assert is_transient(err) is False


# --- create_app --------------------------------------------------------------


async def test_create_app_returns_ingress_fqdn(monkeypatch: pytest.MonkeyPatch) -> None:
    poller = _settled(_fake_app("app-xyz.westeurope.azurecontainerapps.io"))
    ca = SimpleNamespace(begin_create_or_update=lambda *a, **k: poller)
    cp = _control_plane(monkeypatch, ca)
    assert (
        await cp.create_app(name="sbx-x", env=_ENV, tags=_TAGS)
        == "app-xyz.westeurope.azurecontainerapps.io"
    )


async def test_a_hung_arm_operation_gives_the_worker_thread_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`poller.result()` has no timeout, and these calls run on the interpreter's DEFAULT
    executor — six threads on a 2-core plan. Without a ceiling, a handful of wedged ARM
    operations exhausts that pool and stalls every other `asyncio.to_thread` in the process:
    the reaper's deletes, snapshot extraction, offloaded storage calls. A feature nobody is
    using takes the whole control plane down.

    Classified TRANSIENT because the outcome is genuinely unknown — the create may still
    land, so a caller must never respond by "cleaning up" the container app."""
    monkeypatch.setattr(aca_module, "_LRO_CEILING_SECONDS", 0.05)
    monkeypatch.setattr(aca_module, "_LRO_POLL_STEP_SECONDS", 0.01)
    ca = SimpleNamespace(begin_create_or_update=lambda *a, **k: _never_settles())
    cp = _control_plane(monkeypatch, ca)

    with pytest.raises(AcaTransientError):
        await cp.create_app(name="sbx-x", env=_ENV, tags=_TAGS)


async def test_a_hung_delete_is_bounded_too(monkeypatch: pytest.MonkeyPatch) -> None:
    """A cleanup that blocks a thread forever is worse than a leaked container app."""
    monkeypatch.setattr(aca_module, "_LRO_CEILING_SECONDS", 0.05)
    monkeypatch.setattr(aca_module, "_LRO_POLL_STEP_SECONDS", 0.01)
    ca = SimpleNamespace(begin_delete=lambda *a, **k: _never_settles())
    cp = _control_plane(monkeypatch, ca)

    with pytest.raises(AcaTransientError):
        await cp.delete_app(name="sbx-x")


async def test_a_slow_but_settling_operation_still_succeeds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ceiling must not turn "took three polls" into a failure."""
    monkeypatch.setattr(aca_module, "_LRO_POLL_STEP_SECONDS", 0.001)
    polls = {"n": 0}

    def _wait(timeout: float | None = None) -> None:
        polls["n"] += 1

    poller = SimpleNamespace(
        done=lambda: polls["n"] >= 3,
        wait=_wait,
        result=lambda: _fake_app("late.westeurope.azurecontainerapps.io"),
    )
    cp = _control_plane(
        monkeypatch, SimpleNamespace(begin_create_or_update=lambda *a, **k: poller)
    )

    assert (
        await cp.create_app(name="sbx-x", env=_ENV, tags=_TAGS)
        == "late.westeurope.azurecontainerapps.io"
    )
    assert polls["n"] == 3


async def test_create_app_missing_fqdn_raises_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    poller = _settled(_fake_app(None))
    ca = SimpleNamespace(begin_create_or_update=lambda *a, **k: poller)
    cp = _control_plane(monkeypatch, ca)
    with pytest.raises(AcaError) as ei:
        await cp.create_app(name="sbx-x", env=_ENV, tags=_TAGS)
    assert not isinstance(ei.value, AcaTransientError)  # a bad response is terminal, not retryable


# --- ACR pull auth (G2: the registries block lets ACA pull a private-ACR image) ----


def test_envelope_wires_acr_registry_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    # Without a `registries` credential ACA cannot pull the private sandbox image, so the
    # container-app spec must carry one — with the ACR password stored as an ACA secret and
    # referenced by name (never inlined on the registry credential in the spec).
    cp = _control_plane(monkeypatch, SimpleNamespace())
    props = cp._envelope(_ENV, _TAGS).properties
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
        await cp.create_app(name="sbx-x", env=_ENV, tags=_TAGS)


@pytest.mark.parametrize("status", [400, 404])
async def test_create_app_maps_terminal(monkeypatch: pytest.MonkeyPatch, status: int) -> None:
    cp = _control_plane(monkeypatch, _raises("begin_create_or_update", _http_error(status)))
    with pytest.raises(AcaError) as ei:
        await cp.create_app(name="sbx-x", env=_ENV, tags=_TAGS)
    assert not isinstance(ei.value, AcaTransientError)


# --- delete_app (idempotent — an already-absent app is a no-op) ---------------


@pytest.mark.parametrize("absent", [ResourceNotFoundError("gone"), _http_error(404)])
async def test_delete_app_swallows_absent(
    monkeypatch: pytest.MonkeyPatch, absent: BaseException
) -> None:
    cp = _control_plane(monkeypatch, _raises("begin_delete", absent))
    await cp.delete_app(name="sbx-x")  # no raise — already gone


async def test_delete_app_success(monkeypatch: pytest.MonkeyPatch) -> None:
    poller = _settled(None)
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


# --- C10 identity tags: the envelope, the PATCH, and the listing -------------


def test_the_create_envelope_carries_the_identity_tags(monkeypatch: pytest.MonkeyPatch) -> None:
    """ON THE ENVELOPE, not PATCHed on afterwards. A create that succeeded followed by a stamp
    that did not would leave an anonymous container behind — the population ADR-0029 exists to
    collect, manufactured by the code meant to prevent it."""
    cp = _control_plane(monkeypatch, SimpleNamespace())
    tags = {"bial-kind": "build-sandbox", "bial-user-id": "u"}
    envelope = cp._envelope(_ENV, tags)  # noqa: SLF001

    assert envelope.tags == tags


def test_the_create_envelope_refuses_an_over_long_tag(monkeypatch: pytest.MonkeyPatch) -> None:
    # Refused at this seam rather than by an ARM 400 in the middle of a provision.
    cp = _control_plane(monkeypatch, SimpleNamespace())
    with pytest.raises(SandboxTagError):
        cp._envelope(_ENV, {"bial-control-plane": "x" * 257})  # noqa: SLF001


async def test_stamp_tags_uses_patch_and_never_put(monkeypatch: pytest.MonkeyPatch) -> None:
    """THE VERB IS THE WHOLE SAFETY ARGUMENT. `begin_update` is ARM `PATCH` (JSON Merge Patch):
    tags sit outside `template` and `configuration`, so it creates no revision and cannot touch
    container env — and container env is the durable home of the supervisor bearer.
    `begin_create_or_update` is `PUT`; called with this partial body it would REPLACE a live
    sandbox, taking that bearer, its image and its probes with it."""
    seen: dict[str, object] = {}

    def _update(rg: str, name: str, envelope: object) -> SimpleNamespace:
        seen.update(rg=rg, name=name, envelope=envelope)
        return _settled(None)

    def _no_put(*_a: object, **_k: object) -> object:
        return pytest.fail("stamp_tags must PATCH: a PUT replaces the resource and its env")

    cp = _control_plane(
        monkeypatch, SimpleNamespace(begin_update=_update, begin_create_or_update=_no_put)
    )

    await cp.stamp_tags(name="sbx-x", tags={"bial-kind": "build-sandbox"})

    assert seen["rg"] == "rg"
    assert seen["name"] == "sbx-x"
    envelope = seen["envelope"]
    assert isinstance(envelope, aca_models.ContainerApp)
    assert envelope.tags == {"bial-kind": "build-sandbox"}
    # The PATCH body schema marks `location` required; omitting it is a 400 on every stamp.
    assert envelope.location == "westeurope"
    # Nothing else may ride along: a body carrying `properties` is no longer a tag-only merge.
    assert envelope.properties is None


async def test_a_202_stamp_is_polled_to_completion(monkeypatch: pytest.MonkeyPatch) -> None:
    """ARM answers a tag PATCH with 202 often enough that assuming it is synchronous would
    report a stamped fleet while the writes were still in flight — and the destroy flag is
    gated on exactly that report."""
    monkeypatch.setattr(aca_module, "_LRO_POLL_STEP_SECONDS", 0.001)
    polls = {"n": 0}

    def _wait(timeout: float | None = None) -> None:
        polls["n"] += 1

    poller = SimpleNamespace(done=lambda: polls["n"] >= 3, wait=_wait, result=lambda: None)
    cp = _control_plane(monkeypatch, SimpleNamespace(begin_update=lambda *a, **k: poller))

    await cp.stamp_tags(name="sbx-x", tags={"bial-kind": "build-sandbox"})

    assert polls["n"] == 3


async def test_a_hung_stamp_is_bounded_too(monkeypatch: pytest.MonkeyPatch) -> None:
    # The same shared-thread-pool hazard as create and delete: a wedged PATCH must give the
    # worker thread back rather than hold one of six forever.
    monkeypatch.setattr(aca_module, "_LRO_CEILING_SECONDS", 0.05)
    monkeypatch.setattr(aca_module, "_LRO_POLL_STEP_SECONDS", 0.01)
    hung = SimpleNamespace(begin_update=lambda *a, **k: _never_settles())
    cp = _control_plane(monkeypatch, hung)

    with pytest.raises(AcaTransientError):
        await cp.stamp_tags(name="sbx-x", tags={"bial-kind": "build-sandbox"})


@pytest.mark.parametrize(
    "exc",
    [
        ServiceRequestError("request blip"),
        ServiceResponseError("response blip"),
        _http_error(429),
        _http_error(500),
    ],
)
async def test_stamp_tags_maps_transient(
    monkeypatch: pytest.MonkeyPatch, exc: BaseException
) -> None:
    cp = _control_plane(monkeypatch, _raises("begin_update", exc))
    with pytest.raises(AcaTransientError):
        await cp.stamp_tags(name="sbx-x", tags={"bial-kind": "build-sandbox"})


async def test_stamp_tags_maps_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    cp = _control_plane(monkeypatch, _raises("begin_update", _http_error(400)))
    with pytest.raises(AcaError) as ei:
        await cp.stamp_tags(name="sbx-x", tags={"bial-kind": "build-sandbox"})
    assert not isinstance(ei.value, AcaTransientError)


def _listed(name: str, tags: dict[str, str] | None) -> SimpleNamespace:
    """One item as the list endpoint hands it back. `tags=None` models the shape ARM actually
    returns for an untagged app — the key is ABSENT, not `{}`, verified live against every app
    in `bial-dev-rg`, and that shape is the entire orphan population."""
    return SimpleNamespace(name=name, tags=tags)


async def test_list_sandbox_app_tags_keeps_the_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    apps = [
        _listed("sbx-tagged", {"bial-kind": "build-sandbox", "bial-user-id": "u"}),
        _listed("sbx-bare", None),
        _listed("pub-someones-live-app", {"bial-kind": "published-app"}),
        _listed("unrelated-workload", {"team": "someone-else"}),
    ]
    cp = _control_plane(monkeypatch, SimpleNamespace(list_by_resource_group=lambda rg: apps))

    fleet = await cp.list_sandbox_app_tags()

    # Only `sbx-` containers: a published app or a co-tenant workload is never ours to judge.
    assert set(fleet) == {"sbx-tagged", "sbx-bare"}
    assert fleet["sbx-tagged"] == {"bial-kind": "build-sandbox", "bial-user-id": "u"}
    # Absent `tags` normalizes to {} — present, carrying no identity. Two different answers,
    # and the backfill acts on the difference.
    assert fleet["sbx-bare"] == {}


async def test_list_sandbox_app_tags_never_projects_container_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The ARM list endpoint returns plaintext `env[].value` — SUPERVISOR_TOKEN,
    BIAL_DATABASE_URL, BIAL_BLOB_SAS all arrive unrequested (only `configuration.secrets` are
    redacted). Projecting name+tags keeps them out of every caller, log and report by
    construction rather than by everyone downstream remembering."""
    leaky = SimpleNamespace(
        name="sbx-x",
        tags={"bial-kind": "build-sandbox"},
        properties=SimpleNamespace(
            template=SimpleNamespace(
                containers=[
                    SimpleNamespace(
                        env=[SimpleNamespace(name="SUPERVISOR_TOKEN", value="super-secret")]
                    )
                ]
            )
        ),
    )
    cp = _control_plane(monkeypatch, SimpleNamespace(list_by_resource_group=lambda rg: [leaky]))

    fleet = await cp.list_sandbox_app_tags()

    assert "super-secret" not in str(fleet)


@pytest.mark.parametrize(
    "exc",
    [
        ServiceRequestError("request blip"),
        ServiceResponseError("response blip"),
        _http_error(429),
        _http_error(500),
    ],
)
async def test_list_sandbox_app_tags_maps_transient(
    monkeypatch: pytest.MonkeyPatch, exc: BaseException
) -> None:
    # A truncated fleet reading as "nothing left to stamp" is the false green the destroy flag
    # is gated on. Refuse rather than under-report.
    cp = _control_plane(monkeypatch, _raises("list_by_resource_group", exc))
    with pytest.raises(AcaTransientError):
        await cp.list_sandbox_app_tags()


async def test_list_sandbox_app_tags_maps_terminal(monkeypatch: pytest.MonkeyPatch) -> None:
    cp = _control_plane(monkeypatch, _raises("list_by_resource_group", _http_error(403)))
    with pytest.raises(AcaError) as ei:
        await cp.list_sandbox_app_tags()
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
