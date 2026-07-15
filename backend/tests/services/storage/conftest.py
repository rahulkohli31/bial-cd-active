"""Storage integration-suite fixtures. The `ready_backend` fixture yields a real
`AzureBlobStorage` against Azurite with a freshly-created container, and skips
cleanly when Azurite isn't reachable so `-m integration` without docker-compose
gives a clear skip rather than a hang. It resets the module client cache around
each test so a client built on one test's event loop is never reused on the next
loop.

These tests touch no database; only the `ready_backend` fixture (and thus the
integration lane) needs Azurite.
"""

from __future__ import annotations

import socket
import uuid
from collections.abc import AsyncIterator

import pytest
from azure.core.exceptions import ResourceExistsError
from pydantic import SecretStr

from src.services.storage import reset_storage_for_tests
from src.services.storage.app_containers import AppContainerStore
from src.services.storage.azure_backend import AzureBlobStorage
from src.services.storage.base import ObjectStorage
from src.services.storage.config import AzureStorageConfig

# Azurite's well-known dev account (public, baked into the image).
AZURITE_ACCOUNT_URL = "http://127.0.0.1:10000/devstoreaccount1"
AZURITE_CONN = (
    "DefaultEndpointsProtocol=http;AccountName=devstoreaccount1;"
    "AccountKey=Eby8vdM02xNOcqFlqUwJPLlmEtlCDXJ1OUzFT50uSRZ6IFsuFq2UVErCz4I6tq/"
    "K1SZFPTOtr/KBHBeksoGMGw==;"
    "BlobEndpoint=http://127.0.0.1:10000/devstoreaccount1;"
)


def _port_open(host: str, port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(0.5)
        return sock.connect_ex((host, port)) == 0


async def _ensure_azure_container(backend: AzureBlobStorage, container: str) -> None:
    state = await backend._state()
    try:
        await state.service_client.create_container(container)
    except ResourceExistsError:
        pass


@pytest.fixture
async def ready_backend() -> AsyncIterator[ObjectStorage]:
    # Fresh client cache per test so a client from a prior test's event loop is
    # never reused on this loop.
    await reset_storage_for_tests()
    if not _port_open("127.0.0.1", 10000):
        pytest.skip(
            "Azurite not reachable on :10000 — `docker compose -f docker-compose.test.yml up -d`"
        )
    container = f"test-{uuid.uuid4().hex[:16]}"
    backend = AzureBlobStorage.from_config(
        AzureStorageConfig(
            account_url=AZURITE_ACCOUNT_URL,
            container=container,
            connection_string=SecretStr(AZURITE_CONN),
        )
    )
    await _ensure_azure_container(backend, container)
    try:
        yield backend
    finally:
        await backend.aclose()
        await reset_storage_for_tests()


@pytest.fixture
async def app_container_store() -> AsyncIterator[AppContainerStore]:
    """An `AppContainerStore` against Azurite (account-key mode — the verified branch), skipping
    cleanly when Azurite isn't reachable. Unlike `ready_backend` it creates no container up front:
    the store's own `ensure_container` is what each test exercises. `container` is a placeholder
    (the config model requires it; `AppContainerStore` never reads it — it manages per-app
    containers itself). Resets the module client cache around each test so no client crosses loops.
    """
    await reset_storage_for_tests()
    if not _port_open("127.0.0.1", 10000):
        pytest.skip(
            "Azurite not reachable on :10000 — `docker compose -f docker-compose.test.yml up -d`"
        )
    store = AppContainerStore(
        AzureStorageConfig(
            account_url=AZURITE_ACCOUNT_URL,
            container="platform",
            connection_string=SecretStr(AZURITE_CONN),
        )
    )
    try:
        yield store
    finally:
        await reset_storage_for_tests()
