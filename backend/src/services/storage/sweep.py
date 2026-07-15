"""Post-commit blob sweeping for the delete endpoints (KD-3).

The rows are already committed-deleted when a sweep runs, so NOTHING here may
surface: a raised error would 500 a delete that in fact succeeded and abandon the
remaining keys. The Azure backend wraps most failures into `StorageError`, but
transport-level errors (e.g. `azure.core.exceptions.ServiceResponseError` — request
sent, response lost) escape that hierarchy, so the guard here is deliberately
broad. Every failed key is logged (never silently dropped) so a future blob-GC has
a trail; a residual blob is a bounded orphan, not a failure.
"""

from __future__ import annotations

import asyncio
import uuid

import structlog

from src.services.storage.app_containers import AppContainerStore
from src.services.storage.base import ObjectStorage

_log = structlog.get_logger()

# Trim tail latency on a wide cascade (a project's app files + every conversation's attachments)
# without opening an unbounded fan of connections at the object store. The rows are already
# committed-deleted, so this only speeds the best-effort sweep — it never changes the outcome.
_SWEEP_CONCURRENCY = 8


async def sweep_blobs(storage: ObjectStorage, blob_keys: list[str]) -> None:
    """Best-effort post-commit delete of every key, run concurrently behind a bounded
    semaphore; log-and-continue on ANY failure. Each delete swallows its own error, so one
    dropped key never cancels a sibling and nothing surfaces to 500 an already-committed
    delete (KD-3)."""
    if not blob_keys:
        return
    limiter = asyncio.Semaphore(_SWEEP_CONCURRENCY)

    async def _sweep_one(key: str) -> None:
        async with limiter:
            try:
                await storage.delete(key)
            except Exception:  # noqa: BLE001 — post-commit best-effort: log, never surface
                _log.warning("post_delete_blob_sweep_failed", blob_key=key)

    await asyncio.gather(*(_sweep_one(key) for key in blob_keys))


async def sweep_app_containers(store: AppContainerStore | None, app_ids: list[uuid.UUID]) -> None:
    """Best-effort post-commit delete of every app's per-app Blob container (KTD-7), run
    concurrently behind the same bounded semaphore; log-and-continue on ANY failure so one
    orphaned container never cancels a sibling and nothing surfaces to 500 an already-committed
    project delete (KD-3). Early-returns when the store is disabled (`None`) — even with a
    non-empty id list — because dev/test has no object store to sweep (KTD-2); a residual
    container is a bounded, unmetered orphan, not a failure."""
    if store is None:
        return
    if not app_ids:
        return
    limiter = asyncio.Semaphore(_SWEEP_CONCURRENCY)

    async def _sweep_one(app_id: uuid.UUID) -> None:
        async with limiter:
            try:
                await store.delete_container(app_id)
            except Exception:  # noqa: BLE001 — post-commit best-effort: log, never surface
                _log.warning("post_delete_container_sweep_failed", app_id=str(app_id))

    await asyncio.gather(*(_sweep_one(app_id) for app_id in app_ids))
