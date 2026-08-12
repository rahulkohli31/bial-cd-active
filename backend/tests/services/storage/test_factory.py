"""The concrete-typing factory contract (ADR-0009), enforced two ways:

1. STATICALLY — the `assert_type(...)` call the three type checkers verify when
   they analyze this file (run in CI): `create_storage` on an `AzureStorageConfig`
   resolves to the CONCRETE `AzureBlobStorage`, not the base `ObjectStorage`.
2. AT RUNTIME — `isinstance` asserts the factory returns `AzureBlobStorage`, and
   that an incomplete `ObjectStorage` subclass is not instantiable (ABC
   enforcement).

The `assert_type` is placed on the bare call expression BEFORE any `isinstance`
narrowing, so it genuinely tests the return type and not a narrowed one.
"""

from __future__ import annotations

from typing import assert_type

import pytest
from pydantic import SecretStr

from src.services.storage.azure_backend import AzureBlobStorage
from src.services.storage.base import ObjectStorage
from src.services.storage.config import AzureStorageConfig, StorageConfig
from src.services.storage.factory import create_storage


def _azure_cfg() -> AzureStorageConfig:
    return AzureStorageConfig(
        account_url="https://a.blob.core.windows.net",
        container="c",
        account_key=SecretStr("key"),
    )


# --- The factory resolves to the CONCRETE backend ----------------------------


def test_azure_config_is_concrete() -> None:
    backend = create_storage(_azure_cfg())
    assert_type(backend, AzureBlobStorage)
    assert isinstance(backend, AzureBlobStorage)
    assert backend.provider == "azure"


def test_storage_config_alias_resolves_to_azure() -> None:
    # `StorageConfig` is a plain alias for `AzureStorageConfig` (the type of
    # `settings.object_store`), so a `StorageConfig`-typed site still resolves to
    # the concrete backend. Mirrors production: `create_storage(settings.object_store)`.
    def via_field(cfg: StorageConfig) -> ObjectStorage:
        backend = create_storage(cfg)
        assert_type(backend, AzureBlobStorage)
        return backend

    assert isinstance(via_field(_azure_cfg()), AzureBlobStorage)


# --- Runtime: ABC enforcement ------------------------------------------------


def test_incomplete_backend_is_not_instantiable() -> None:
    class Incomplete(ObjectStorage):
        """Implements none of the abstractmethods → must not be instantiable."""

    # The ABC marks every unimplemented method abstract...
    assert "put" in Incomplete.__abstractmethods__
    # ...so instantiation raises TypeError at runtime. The intentional abstract
    # instantiation is suppressed per-checker (the runtime behavior is the point).
    with pytest.raises(TypeError):
        Incomplete(provider="azure")  # type: ignore[abstract]  # pyright: ignore[reportAbstractUsage]
