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

import structlog

from src.services.storage.base import ObjectStorage

_log = structlog.get_logger()


async def sweep_blobs(storage: ObjectStorage, blob_keys: list[str]) -> None:
    """Best-effort post-commit delete of every key; log-and-continue on ANY failure."""
    for key in blob_keys:
        try:
            await storage.delete(key)
        except Exception:  # noqa: BLE001 — post-commit best-effort: log, never surface
            _log.warning("post_delete_blob_sweep_failed", blob_key=key)
