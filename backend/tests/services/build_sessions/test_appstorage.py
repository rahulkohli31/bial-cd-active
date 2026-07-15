"""provision_app_storage — the birth-path Blob provisioning half (C9 §6, KTD-2/KTD-3).

Network-free: a fake AppContainerStore stands in for the store accessor, so the env-dict shape, the
sandbox-facing base URL (KTD-2), the disabled-store {} path, and fail-first propagation are all
observable without Azure.
"""

from __future__ import annotations

import uuid

import pytest

from src.config import settings
from src.services.build_sessions.appstorage import provision_app_storage
from src.services.sandbox.config import SandboxConfig
from src.services.storage import APP_CONTAINER_SAS_TTL, AppContainerStore
from src.services.storage.errors import StorageError

_APP = uuid.UUID("019f1c00-0000-7000-8000-0000000000bb")
_DEFAULT_BASE = "https://acct.blob.core.windows.net"


class _FakeStore(AppContainerStore):
    """Records ensure/mint calls and returns a canned SAS; overrides `__init__` so no config or
    client is needed. `container_url` mirrors the real join without a real account config."""

    def __init__(
        self, *, sas: str = "sv=x&sig=SIG", ensure_error: Exception | None = None
    ) -> None:
        self.ensured: list[uuid.UUID] = []
        self.minted: list[uuid.UUID] = []
        self._sas = sas
        self._ensure_error = ensure_error

    async def ensure_container(self, app_id: uuid.UUID) -> None:
        if self._ensure_error is not None:
            raise self._ensure_error
        self.ensured.append(app_id)

    async def mint_container_sas(
        self, app_id: uuid.UUID, *, ttl: object = APP_CONTAINER_SAS_TTL
    ) -> str:
        self.minted.append(app_id)
        return self._sas

    def container_url(self, app_id: uuid.UUID, *, base_url: str | None = None) -> str:
        base = (base_url if base_url is not None else _DEFAULT_BASE).rstrip("/")
        return f"{base}/app-{app_id}"


def _patch_store(monkeypatch: pytest.MonkeyPatch, store: AppContainerStore | None) -> None:
    monkeypatch.setattr(
        "src.services.build_sessions.appstorage.get_app_container_store", lambda: store
    )


def _sandbox(blob_base_url: str | None) -> SandboxConfig:
    return SandboxConfig(
        subscription_id="s",
        resource_group="r",
        region="westeurope",
        managed_environment_name="aca-env",
        image_ref="acr/img:latest",
        app_data_base_url="https://platform.example/v1",
        blob_base_url=blob_base_url,
    )


async def test_returns_exactly_the_two_blob_vars(monkeypatch: pytest.MonkeyPatch) -> None:
    store = _FakeStore(sas="sv=2021&sig=ABC123")
    _patch_store(monkeypatch, store)
    monkeypatch.setattr(settings, "sandbox", _sandbox(None))

    env = await provision_app_storage(_APP)

    assert set(env) == {"BIAL_BLOB_CONTAINER_URL", "BIAL_BLOB_SAS"}
    assert env["BIAL_BLOB_SAS"] == "sv=2021&sig=ABC123"
    assert env["BIAL_BLOB_CONTAINER_URL"] == f"{_DEFAULT_BASE}/app-{_APP}"
    assert store.ensured == [_APP]  # container ensured before mint
    assert store.minted == [_APP]


async def test_uses_sandbox_facing_base_url(monkeypatch: pytest.MonkeyPatch) -> None:
    # KTD-2: the injected URL must be a host the sandbox reaches (the docker-network Azurite
    # address locally), NOT the control-plane's account_url.
    store = _FakeStore()
    _patch_store(monkeypatch, store)
    monkeypatch.setattr(settings, "sandbox", _sandbox("http://azurite:10000/devstoreaccount1"))

    env = await provision_app_storage(_APP)

    assert env["BIAL_BLOB_CONTAINER_URL"] == f"http://azurite:10000/devstoreaccount1/app-{_APP}"


async def test_account_url_fallback_when_sandbox_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    # Store CONFIGURED but settings.sandbox is None — a real non-prod state, since object storage
    # and sandbox are independently-optional settings. The injected URL must fall back to the
    # store's own account_url default (base_url=None), NOT a sandbox-facing host (the KTD-2 knob is
    # simply absent here). Covers the `sandbox is None` side of the ternary.
    store = _FakeStore()
    _patch_store(monkeypatch, store)
    monkeypatch.setattr(settings, "sandbox", None)

    env = await provision_app_storage(_APP)

    assert env["BIAL_BLOB_CONTAINER_URL"] == f"{_DEFAULT_BASE}/app-{_APP}"


async def test_returns_empty_when_store_disabled(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_store(monkeypatch, None)  # object storage unconfigured (dev/test) — feature off
    assert await provision_app_storage(_APP) == {}


async def test_reraises_storage_error_from_ensure(monkeypatch: pytest.MonkeyPatch) -> None:
    # A genuine error from a CONFIGURED store propagates (fail-first) — not swallowed to {}.
    store = _FakeStore(ensure_error=StorageError("container create failed"))
    _patch_store(monkeypatch, store)
    monkeypatch.setattr(settings, "sandbox", _sandbox(None))

    with pytest.raises(StorageError):
        await provision_app_storage(_APP)
    assert store.minted == []  # never reached the mint after ensure blew up
