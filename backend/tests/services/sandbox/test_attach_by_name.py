"""`AcaSandboxClient.attach_by_name` — reaching a container from its NAME alone, with no
registry entry, no `user_id`, and no `token_ref`.

Mirrors `test_aca.py`'s `attach_existing` coverage but drives the by-name path specifically:
the registry may name a different container, or nothing at all, and the handle returned must
still resolve to whatever answers to the given name. The ACA control plane is a local fake
keyed by container name (no Azure touched); the `/_sup/*` layer is an `httpx.MockTransport`.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable

import httpx
import pytest
import redis.asyncio as aioredis
from pydantic import SecretStr

from src.services.redis import REGISTRY_STATE_READY, registry_key
from src.services.redis.keys import REGISTRY_FIELD_APP_NAME, REGISTRY_FIELD_STATE
from src.services.sandbox import client as client_module
from src.services.sandbox.aca import AcaControlPlane, AcaTransientError
from src.services.sandbox.base import SandboxClient, SandboxGoneError, SandboxNotReadyError
from src.services.sandbox.client import AcaSandboxClient
from src.services.sandbox.config import SandboxConfig

Handler = Callable[[httpx.Request], httpx.Response]

USER = uuid.uuid4()
OTHER_NAME = "sbx-" + "1" * 28
TARGET_NAME = "sbx-" + "2" * 28


class FakeAca(AcaControlPlane):
    """A control plane keyed by name, recording nothing but what each seeded container
    answers. Overrides `__init__` so it never builds a credential / mgmt client.

    Unlike `test_aca.py`'s `FakeAca`, this one implements `get_app_env_value` directly: the
    by-name path has no `token_ref` cache to fall back on, so every test here goes through
    the real ARM-env read."""

    def __init__(self) -> None:
        self.envs: dict[str, dict[str, str]] = {}

    def seed(self, name: str, *, token: str) -> None:
        self.envs[name] = {"SUPERVISOR_TOKEN": token}

    def seed_without_token(self, name: str) -> None:
        """A container ARM confirms exists but whose env carries no bearer — the token-read
        arm's own failure, distinct from `get_app_fqdn` finding nothing at all."""
        self.envs[name] = {}

    async def get_app_fqdn(self, *, name: str) -> str | None:
        if name not in self.envs:
            return None
        return f"{name}.westeurope.azurecontainerapps.io"

    async def get_app_env_value(self, *, name: str, key: str) -> str | None:
        return self.envs.get(name, {}).get(key)

    async def aclose(self) -> None:
        return None


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


def _client(aca: FakeAca, handler: Handler | None = None) -> AcaSandboxClient:
    transport = httpx.MockTransport(handler) if handler is not None else None
    return AcaSandboxClient(_config(), transport=transport, aca=aca)


def _healthy_handler(request: httpx.Request) -> httpx.Response:
    if request.url.path.endswith("/dev/status"):
        return httpx.Response(200, json={"running": True, "ready": True, "port": 3000})
    return httpx.Response(200, json={"ok": True})  # /_sup/health probe


async def test_reaches_a_live_container_with_no_registry_entry() -> None:
    aca = FakeAca()
    aca.seed(TARGET_NAME, token="tok-target")
    client = _client(aca, _healthy_handler)

    handle = await client.attach_by_name(app_name=TARGET_NAME)

    assert handle.app_name == TARGET_NAME
    assert handle.token == "tok-target"
    assert handle.ready is True  # the readiness probe, not a hardcoded value
    await client.aclose()


async def test_reaches_the_named_container_even_when_the_registry_names_a_different_one(
    fake_redis: aioredis.Redis,
) -> None:
    aca = FakeAca()
    aca.seed(OTHER_NAME, token="tok-other")
    aca.seed(TARGET_NAME, token="tok-target")
    # The per-user registry still points at the OTHER container — exactly what a project
    # switch leaves behind once the incoming container's record has overwritten the slot.
    await fake_redis.hset(
        registry_key(USER),
        mapping={REGISTRY_FIELD_APP_NAME: OTHER_NAME, REGISTRY_FIELD_STATE: REGISTRY_STATE_READY},
    )
    client = _client(aca, _healthy_handler)

    handle = await client.attach_by_name(app_name=TARGET_NAME)

    assert handle.app_name == TARGET_NAME
    assert handle.token == "tok-target"
    await client.aclose()


async def test_arm_confirms_absent_raises_gone_not_not_ready() -> None:
    aca = FakeAca()  # nothing seeded — ARM has never heard of this name
    client = _client(aca)

    with pytest.raises(SandboxGoneError):
        await client.attach_by_name(app_name=TARGET_NAME)
    await client.aclose()


async def test_arm_reachable_but_supervisor_silent_is_a_reach_failure_not_absence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A single blip must not read as absence — that would let a caller conclude the container
    # is already gone and skip straight to deleting the row, over a container still running.
    monkeypatch.setattr(client_module, "_PROBE_START_SECONDS", 0.001)
    monkeypatch.setattr(client_module, "_PROBE_MAX_SECONDS", 0.002)
    aca = FakeAca()
    aca.seed(TARGET_NAME, token="tok-target")

    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("supervisor down")

    client = _client(aca, handler)

    with pytest.raises(SandboxNotReadyError):
        await client.attach_by_name(app_name=TARGET_NAME)
    await client.aclose()


async def test_container_exists_but_token_unreadable_is_not_ready_not_gone() -> None:
    aca = FakeAca()
    aca.seed_without_token(TARGET_NAME)
    # A HEALTHY transport, deliberately: if the missing-token check did not raise, the
    # reachability probe below it would succeed and mask the gap, letting the test pass for
    # the wrong reason.
    client = _client(aca, _healthy_handler)

    with pytest.raises(SandboxNotReadyError):
        await client.attach_by_name(app_name=TARGET_NAME)
    await client.aclose()


async def test_transient_arm_error_while_confirming_absence_is_not_ready_not_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    aca = FakeAca()

    async def throttled(*, name: str) -> str | None:
        raise AcaTransientError("ACA get was throttled or 5xx'd")

    monkeypatch.setattr(aca, "get_app_fqdn", throttled)
    client = _client(aca)

    with pytest.raises(SandboxNotReadyError):
        await client.attach_by_name(app_name=TARGET_NAME)
    await client.aclose()


def test_attach_by_name_is_not_part_of_the_pinned_abstract_contract() -> None:
    """The C2 abstract set is pinned in `test_base.py`; this asserts the property this unit
    relies on rather than re-running that test."""
    assert "attach_by_name" not in SandboxClient.__abstractmethods__
