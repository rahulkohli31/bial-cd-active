"""Post-commit blob sweeping for the delete endpoints.

The rows are already committed-deleted when a sweep runs, so NOTHING here may
surface: a raised error would 500 a delete that in fact succeeded and abandon the
remaining keys. The Azure backend wraps most failures into `StorageError`, but
transport-level errors (e.g. `azure.core.exceptions.ServiceResponseError` — request
sent, response lost) escape that hierarchy, so the guard here is deliberately
broad.

WHAT HAPPENS TO A KEY THAT FAILS, precisely: it is named on
`TEARDOWN_ARTEFACT_SURVIVED_EVENT` and returned to the caller, and then a human deletes it or
nobody does. This module used to say a residual blob was "a bounded orphan" for "a future
blob-GC" — the trail exists, but no timer reads it: the only scheduled destroyer in this
codebase is the sandbox reap, and it runs solely in production. The delete path turns the
return into one audit row naming what survived, so the leak is countable rather than merely
logged.
"""

from __future__ import annotations

import asyncio
import uuid

import structlog

from src.core.alarms import TEARDOWN_ARTEFACT_SURVIVED_EVENT
from src.services.storage.app_containers import AppContainerStore
from src.services.storage.base import ObjectStorage

_log = structlog.get_logger()

# Trim tail latency on a wide cascade (a project's app files + every conversation's attachments)
# without opening an unbounded fan of connections at the object store. The rows are already
# committed-deleted, so this only speeds the best-effort sweep — it never changes the outcome.
_SWEEP_CONCURRENCY = 8


async def sweep_blobs(
    storage: ObjectStorage, blob_keys: list[str], *, concurrency: int = _SWEEP_CONCURRENCY
) -> list[str]:
    """Best-effort post-commit delete of every key, run concurrently behind a bounded
    semaphore; log-and-continue on ANY failure. Each delete swallows its own error, so one
    dropped key never cancels a sibling and nothing surfaces to 500 an already-committed
    delete.

    RETURNS THE KEYS THAT SURVIVED. The return is additive — a caller that only wants
    the sweep can still ignore it — and it exists because the delete path owes the record a
    list of what is still out there, which a log line the record cannot read does not give
    it.

    `concurrency` DEFAULTS TO THE INTERACTIVE FIGURE, which is tuned for one citizen pressing one
    button. A bulk unattended caller may raise it; because the rows are already committed-deleted,
    doing so only shortens the sweep and can never change its outcome."""
    if not blob_keys:
        return []
    limiter = asyncio.Semaphore(concurrency)
    survived: list[str] = []

    async def _sweep_one(key: str) -> None:
        async with limiter:
            try:
                await storage.delete(key)
            except Exception:  # noqa: BLE001 — post-commit best-effort: log, never surface
                survived.append(key)
                _log.warning(
                    TEARDOWN_ARTEFACT_SURVIVED_EVENT,
                    artefact="blob",
                    artefact_id=key,
                    reason="the object store refused or could not be reached",
                )

    await asyncio.gather(*(_sweep_one(key) for key in blob_keys))
    return survived


async def sweep_app_containers(
    store: AppContainerStore | None, app_ids: list[uuid.UUID]
) -> list[uuid.UUID]:
    """Best-effort post-commit delete of every app's per-app Blob container, run
    concurrently behind the same bounded semaphore; log-and-continue on ANY failure so one
    orphaned container never cancels a sibling and nothing surfaces to 500 an already-committed
    project delete. Returns the app ids whose container may still exist.

    Early-returns NO SURVIVORS when the store is disabled (`None`) — even with a non-empty id
    list — because dev/test has no object store to sweep, so there is no container to have
    survived. That is the one skip here that is genuinely a no-op rather than a leak."""
    if store is None:
        return []
    if not app_ids:
        return []
    limiter = asyncio.Semaphore(_SWEEP_CONCURRENCY)
    survived: list[uuid.UUID] = []

    async def _sweep_one(app_id: uuid.UUID) -> None:
        async with limiter:
            try:
                await store.delete_container(app_id)
            except Exception:  # noqa: BLE001 — post-commit best-effort: log, never surface
                survived.append(app_id)
                _log.warning(
                    TEARDOWN_ARTEFACT_SURVIVED_EVENT,
                    artefact="app_container",
                    artefact_id=str(app_id),
                    reason="the object store refused or could not be reached",
                )

    await asyncio.gather(*(_sweep_one(app_id) for app_id in app_ids))
    return survived
