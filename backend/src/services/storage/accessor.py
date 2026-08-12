"""App-level storage accessor + lifecycle for the Azure Blob backend. Two caching
layers, kept distinct:

1. *Backend level* (the source of truth for open SDK clients) — the Azure
   backend's config-fingerprint client cache (`azure_backend`).
2. *App level* — `get_storage()` reads `settings.object_store`, calls
   `create_storage` ONCE, and memoises the returned backend. It opens no clients
   of its own; it just holds a reference into layer 1.

`get_storage()` returns the base `ObjectStorage` because `settings.object_store`
is statically the storage port — the honest dynamic contract (concrete types are
for code holding a named config). It lazy-imports `settings` so this module can be
re-exported from the package `__init__` without an import cycle through
`src.config` (which itself imports `storage.config`).
"""

from __future__ import annotations

import structlog

from src.services.storage import azure_backend
from src.services.storage.app_containers import AppContainerStore
from src.services.storage.base import ObjectStorage
from src.services.storage.errors import StorageUnconfiguredError
from src.services.storage.factory import create_storage

_log = structlog.get_logger()

_backend_singleton: ObjectStorage | None = None
_app_container_store_singleton: AppContainerStore | None = None


def get_storage() -> ObjectStorage:
    """The configured backend (layer-2 singleton). Raises `StorageUnconfiguredError` if
    storage is unset (genuinely-optional in dev/test; the prod gate in `src.config`
    requires it) — a `StorageError` subtype, so a caller that only cares that storage is
    unusable keeps its existing catch, while one that must distinguish "no store exists"
    from "the store failed" branches on the type instead of the message."""
    global _backend_singleton
    if _backend_singleton is None:
        from src.config import settings  # lazy: avoid an import cycle via src.config

        if settings.object_store is None:
            raise StorageUnconfiguredError(
                "storage is not configured: set OBJECT_STORE__PROVIDER and the provider's "
                "OBJECT_STORE__* credentials, or call get_storage() only where storage is set"
            )
        _backend_singleton = create_storage(settings.object_store)
    return _backend_singleton


def get_app_container_store() -> AppContainerStore | None:
    """The per-app container store (layer-2 singleton), or **`None` when object storage is
    unconfigured** (dev/test). This deliberately DIVERGES from `get_storage()`, which *raises*
    when unset (KTD-2): per-app storage is a gracefully-disable-able feature, so callers branch
    on `None` (`storage off → skip`) rather than swallowing a real error. Owns no client — the
    store resolves the shared `azure_backend` client per-op — so caching it here is cheap and it
    needs no `aclose` of its own; `aclose_storage` / `reset_storage_for_tests` just drop the ref.
    """
    global _app_container_store_singleton
    if _app_container_store_singleton is None:
        from src.config import settings  # lazy: avoid an import cycle via src.config

        if settings.object_store is None:
            return None
        _app_container_store_singleton = AppContainerStore(settings.object_store)
    return _app_container_store_singleton


async def aclose_storage() -> None:
    """Close every cached Azure client (layer 1 — and the Azure credential) and
    drop the layer-2 singleton. Wired into the FastAPI lifespan shutdown.

    The close is isolated: if it raises we log it (fail-first.md — never a silent
    swallow) but STILL reset the singletons, so a restart never reuses a
    half-closed backend."""
    global _backend_singleton, _app_container_store_singleton
    try:
        await azure_backend.close_all_clients()
    except Exception:
        _log.exception("azure storage teardown failed during aclose_storage")
    finally:
        # The container store owns no client (it borrows the shared one just closed), so there is
        # nothing to close — only the reference to drop, alongside the backend singleton.
        _backend_singleton = None
        _app_container_store_singleton = None


async def reset_storage_for_tests() -> None:
    """Reset BOTH layers so a suite that builds backends with different configs
    never reuses a stale client across tests."""
    global _backend_singleton, _app_container_store_singleton
    _backend_singleton = None
    _app_container_store_singleton = None
    await azure_backend.reset_client_for_tests()
