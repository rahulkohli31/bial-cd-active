"""The concrete-typing factory (ADR-0009) for the Azure Blob backend.

`create_storage(config)` resolves an `AzureStorageConfig` to the *concrete*
`AzureBlobStorage` — F12/hover in the IDE land on the class — under all three
type checkers. Azure Blob is the only provider, so this is a single direct path
with no overloads, `match` dispatch, or provider string sugar.
"""

from __future__ import annotations

from src.services.storage.azure_backend import AzureBlobStorage
from src.services.storage.config import AzureStorageConfig


def create_storage(config: AzureStorageConfig) -> AzureBlobStorage:
    """Resolve the Azure storage config to its backend (`AzureBlobStorage`). No
    client/credential is opened here — the backend's lazy cache opens on first op."""
    return AzureBlobStorage.from_config(config)
