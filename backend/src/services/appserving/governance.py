"""Admin danger ops (R27) — the destructive purges behind the super-admin governance
surface. Internal symbols use the witty naming rule (`.claude/rules/naming.md`):
`the_purge` (clear-data) and `nuke_app` (hard-delete). Public API responses stay
professional.

Since the old per-app file model (`app_files`) was retired (OPEN-SANDBOX), these ops
work on DataRecords + the object-store blobs an app owns: `the_purge` clears data
records; `nuke_app` sweeps the app's C4 snapshot bundle AND its per-app Blob container
before dropping the registry row (the CASCADE removes records/tokens). A residual blob
is a bounded storage orphan, never a data loss.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models.app_registry import AppRegistry
from src.db.models.data_record import DataRecord
from src.services.appserving.quota import adjust_app_quota
from src.services.storage import (
    AppContainerStore,
    ObjectStorage,
    snapshot_key,
    sweep_app_containers,
    sweep_blobs,
)


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


async def the_purge(db: AsyncSession, app_id: uuid.UUID, *, drafts_only: bool) -> int:
    """Clear an app's data records. Returns the number removed. `drafts_only` clears only
    build-time test rows; else everything. No object-store sweep — data records carry no blobs
    (the per-app file model was retired)."""
    return await _purge_records(db, app_id, drafts_only=drafts_only)


async def nuke_app(
    db: AsyncSession,
    storage: ObjectStorage,
    app_id: uuid.UUID,
    container_store: AppContainerStore | None,
) -> None:
    """Hard-delete an app: sweep its object-store artifacts — the C4 snapshot bundle AND its
    per-app Blob CONTAINER — then drop the registry row (the CASCADE removes records/tokens). The
    sweeps go FIRST, while the app id still resolves them; the admin danger-op accepts this inline
    ordering (unlike the rollback-safe project cascade, KD-3). Mirrors `delete_project_cascade`'s
    sweep so a hard-delete never orphans the snapshot blob or the per-app container (KTD-7). Both
    sweeps are best-effort and never surface (a residual blob/container is a bounded orphan).

    Both stores are INJECTED (not resolved inline) so a test can swap fakes for each — `storage`
    for the blob sweep, `container_store` for the container sweep. `container_store` is `None` when
    object storage is unconfigured (dev/test), in which case the container sweep is a no-op."""
    await sweep_blobs(storage, [snapshot_key(app_id)])
    await sweep_app_containers(container_store, [app_id])
    await db.execute(sa.delete(AppRegistry).where(AppRegistry.id == app_id))
