"""Admin danger ops (R27) — the destructive purge behind the super-admin governance
surface. Internal symbols use the witty naming rule (`.claude/rules/naming.md`):
`nuke_app` (hard-delete). Public API responses stay professional.

The old per-app file model (`app_files`, OPEN-SANDBOX) and the shared `data_records`
plane (U6) are both retired, so what an app owns here is object-store blobs only:
`nuke_app` sweeps the app's C4 snapshot bundle, its immutable submission bundles, AND
its per-app Blob container before dropping the registry row. A residual blob is a
bounded storage orphan, never a data loss. The project's own PostgreSQL database is a
POST-COMMIT teardown owned by the caller, not by this module (D10).
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models.app_registry import AppRegistry
from src.services.storage import (
    AppContainerStore,
    ObjectStorage,
    all_keys_under,
    snapshot_key,
    submissions_prefix,
    sweep_app_containers,
    sweep_blobs,
)


async def nuke_app(
    db: AsyncSession,
    storage: ObjectStorage,
    app_id: uuid.UUID,
    container_store: AppContainerStore | None,
) -> None:
    """Hard-delete an app: sweep its object-store artifacts — the C4 snapshot bundle, EVERY
    immutable submission bundle under `submissions/{app_id}/` (R23 — this prefix sweep is also
    the purge lever for the retained-forever submissions), AND its per-app Blob CONTAINER —
    then drop the registry row. The sweeps go FIRST, while
    the app id still resolves them; the admin danger-op accepts this inline ordering (unlike the
    rollback-safe project cascade, KD-3). The sweeps themselves are best-effort and never
    surface (a residual blob/container is a bounded, logged orphan) — but the submissions
    ENUMERATION raises: proceeding past a failed listing would drop the row and strand blobs no
    one can ever find again, so the admin's delete fails retryably instead (fail-first).

    Both stores are INJECTED (not resolved inline) so a test can swap fakes for each — `storage`
    for the blob sweep, `container_store` for the container sweep. `container_store` is `None` when
    object storage is unconfigured (dev/test), in which case the container sweep is a no-op.

    BLOB-ONLY, and staying that way (D10): the project's own PostgreSQL database and login role
    are NOT torn down here. `DROP DATABASE` cannot run inside a transaction block and this
    function deliberately runs inside its caller's, so the database teardown is the CALLER's
    POST-COMMIT step — `salt_the_earth`, after `db.commit()` (see `admin.hard_delete`). Adding it
    here would not merely be misplaced, it would fail."""
    submission_keys = await all_keys_under(storage, submissions_prefix(app_id))
    await sweep_blobs(storage, [snapshot_key(app_id), *submission_keys])
    await sweep_app_containers(container_store, [app_id])
    await db.execute(sa.delete(AppRegistry).where(AppRegistry.id == app_id))
