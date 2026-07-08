"""Admin danger ops (R27) — the destructive purges behind the super-admin governance
surface. Internal symbols use the witty naming rule (`.claude/rules/naming.md`):
`the_purge` (clear-data), `nuke_app` (hard-delete), `recompute_files` (counter GC).
Public API responses stay professional.

Every op keeps the object-store blobs and the DB rows consistent: blobs are deleted
best-effort in bounded batches BEFORE the metadata that holds their keys is gone (a
residual blob is a bounded storage orphan, never a data loss), and the registry
CASCADE (records/files/tokens FK `app_registry.id`) is relied on for the row cleanup
on a hard-delete. Counters are reset (full purge) or decremented (draft-only).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models.app_file import AppFile, AppFileStatus
from src.db.models.app_registry import AppRegistry
from src.db.models.data_record import DataRecord
from src.services.appserving.quota import adjust_app_quota
from src.services.storage import ObjectStorage, StorageError

_log = structlog.get_logger()

# Blob-delete batch size (Express DELETE_BLOBS_BATCH). A stale-pending row older than
# STALE_PENDING is swept by recompute (Express STALE_PENDING_MS = 1h).
_DELETE_BLOBS_BATCH = 20
STALE_PENDING = timedelta(hours=1)


async def _delete_blobs(storage: ObjectStorage, blob_keys: list[str]) -> None:
    """Best-effort batched blob delete. A store error is logged non-fatally and never
    aborts the op — the metadata is already (about to be) gone, so a residual blob is
    a bounded orphan for a deferred blob-GC, not a failure."""
    for start in range(0, len(blob_keys), _DELETE_BLOBS_BATCH):
        for key in blob_keys[start : start + _DELETE_BLOBS_BATCH]:
            try:
                await storage.delete(key)
            except StorageError:
                _log.warning("admin blob delete failed (non-fatal)", blob_key=key)


async def _purge_records(db: AsyncSession, app_id: uuid.UUID, *, drafts_only: bool) -> int:
    where = [DataRecord.app_id == app_id]
    if drafts_only:
        where.append(DataRecord.created_in_draft.is_(True))
        # DELETE ... RETURNING is authoritative for the rows THIS txn removed — derive the
        # decrement from it (not a separate SELECT that could race) and clamp at zero so a
        # concurrent purge can never drive the counters negative.
        deleted = (
            await db.execute(sa.delete(DataRecord).where(*where).returning(DataRecord.bytes))
        ).all()
        count = len(deleted)
        total_bytes = sum(row.bytes for row in deleted)
        await adjust_app_quota(
            db,
            app_id,
            count_col=AppRegistry.data_count,
            bytes_col=AppRegistry.data_bytes,
            d_count=-count,
            d_bytes=-total_bytes,
            clamp=True,
        )
        return count
    count = (
        await db.execute(sa.select(sa.func.count()).select_from(DataRecord).where(*where))
    ).scalar_one()
    await db.execute(sa.delete(DataRecord).where(*where))
    await db.execute(
        sa.update(AppRegistry).where(AppRegistry.id == app_id).values(data_count=0, data_bytes=0)
    )
    return int(count)


async def _purge_files(
    db: AsyncSession, storage: ObjectStorage, app_id: uuid.UUID, *, drafts_only: bool
) -> int:
    where = [AppFile.app_id == app_id]
    if drafts_only:
        where.append(AppFile.created_in_draft.is_(True))
        # DELETE ... RETURNING is authoritative for the rows removed — derive the decrement
        # from it and clamp at zero so a concurrent purge can't drive the counters negative.
        deleted = (
            await db.execute(
                sa.delete(AppFile).where(*where).returning(AppFile.blob_key, AppFile.size)
            )
        ).all()
        blob_keys = [row.blob_key for row in deleted]
        total_bytes = sum(row.size for row in deleted)
        count = len(deleted)
        await adjust_app_quota(
            db,
            app_id,
            count_col=AppRegistry.file_count,
            bytes_col=AppRegistry.file_bytes,
            d_count=-count,
            d_bytes=-total_bytes,
            clamp=True,
        )
    else:
        rows = (await db.execute(sa.select(AppFile.blob_key).where(*where))).all()
        blob_keys = [row.blob_key for row in rows]
        count = len(rows)
        await db.execute(sa.delete(AppFile).where(*where))
        await db.execute(
            sa.update(AppRegistry)
            .where(AppRegistry.id == app_id)
            .values(file_count=0, file_bytes=0)
        )
    await _delete_blobs(storage, blob_keys)
    return count


async def the_purge(
    db: AsyncSession, storage: ObjectStorage, app_id: uuid.UUID, *, drafts_only: bool
) -> tuple[int, int]:
    """Clear an app's data — records then files+blobs. Returns (records_removed,
    files_removed). `drafts_only` clears only build-time test rows; else everything."""
    records_removed = await _purge_records(db, app_id, drafts_only=drafts_only)
    files_removed = await _purge_files(db, storage, app_id, drafts_only=drafts_only)
    return records_removed, files_removed


async def nuke_app(db: AsyncSession, storage: ObjectStorage, app_id: uuid.UUID) -> None:
    """Hard-delete an app: purge its blobs, then drop the registry row (the CASCADE
    removes records/files/tokens). Blobs go FIRST, while their keys still resolve."""
    blob_keys = [
        row.blob_key
        for row in (
            await db.execute(sa.select(AppFile.blob_key).where(AppFile.app_id == app_id))
        ).all()
    ]
    await _delete_blobs(storage, blob_keys)
    await db.execute(sa.delete(AppRegistry).where(AppRegistry.id == app_id))


async def recompute_files(
    db: AsyncSession, storage: ObjectStorage, app_id: uuid.UUID
) -> tuple[int, int, int]:
    """Rebuild file counters from the `ready` aggregate (fixing drift) and sweep stale
    `pending` rows (crashed uploads, older than 1h). Returns (file_count, file_bytes,
    swept_pending)."""
    file_count, file_bytes = (
        await db.execute(
            sa.select(sa.func.count(), sa.func.coalesce(sa.func.sum(AppFile.size), 0)).where(
                AppFile.app_id == app_id, AppFile.status == AppFileStatus.READY
            )
        )
    ).one()
    cutoff = datetime.now(UTC) - STALE_PENDING
    stale_filter = [
        AppFile.app_id == app_id,
        AppFile.status == AppFileStatus.PENDING,
        AppFile.created_at < cutoff,
    ]
    swept = (await db.execute(sa.select(AppFile.blob_key).where(*stale_filter))).all()
    if swept:
        await db.execute(sa.delete(AppFile).where(*stale_filter))
    await db.execute(
        sa.update(AppRegistry)
        .where(AppRegistry.id == app_id)
        .values(file_count=file_count, file_bytes=file_bytes)
    )
    await _delete_blobs(storage, [row.blob_key for row in swept])
    return int(file_count), int(file_bytes), len(swept)
