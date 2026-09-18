"""Admin danger ops — the destructive purge behind the super-admin governance
surface. Internal symbols use the witty naming rule: `nuke_app` (hard-delete).
Public API responses stay professional.

The old per-app file model (`app_files`) and the shared `data_records` plane are both
retired, so what an app owns here is object-store blobs plus what it published: `nuke_app`
sweeps the app's snapshot bundle, its immutable submission bundles, its per-app Blob
container, its published container app AND its container-registry repository before
dropping the registry row. A residual blob is a storage orphan an operator must clear by
hand — nothing on this path is on a timer — never a data loss. The project's own PostgreSQL
database is a POST-COMMIT teardown owned by the caller, not by this module.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models.app_registry import AppRegistry
from src.services.deploy.registry_delete import (
    app_ids_that_could_have_an_image,
    sweep_app_repositories,
)
from src.services.deploy.teardown import sweep_published_apps
from src.services.storage import (
    AppContainerStore,
    ObjectStorage,
    all_keys_under,
    recovery_key,
    snapshot_key,
    submissions_prefix,
    sweep_app_containers,
    sweep_blobs,
    version_prefix,
)


async def nuke_app(
    db: AsyncSession,
    storage: ObjectStorage,
    app_id: uuid.UUID,
    container_store: AppContainerStore | None,
) -> list[tuple[str, str]]:
    """Hard-delete an app: sweep its object-store artifacts — the snapshot bundle, EVERY
    immutable submission bundle under `submissions/{app_id}/` (this prefix sweep is also the
    purge lever for the retained-forever submissions), AND its per-app Blob CONTAINER — then drop
    the registry row. The sweeps go FIRST, while the app id still resolves them; the admin
    danger-op accepts this inline ordering (unlike the rollback-safe project cascade). The sweeps
    themselves are best-effort and never surface (a residual blob/container is a bounded, logged
    orphan) — but the submissions ENUMERATION raises: proceeding past a failed listing would drop
    the row and strand blobs no one can ever find again, so the admin's delete fails retryably
    instead (fail-first).

    Both stores are INJECTED (not resolved inline) so a test can swap fakes for each — `storage`
    for the blob sweep, `container_store` for the container sweep. `container_store` is `None` when
    object storage is unconfigured (dev/test), in which case the container sweep is a no-op.

    IT ANSWERS WITH SURVIVORS, in `record_what_survived`'s `(artefact, id)` shape, so the caller
    can file the one audit row naming what outlived the delete. Every sweep below already reports
    what it could not destroy and this only stops throwing those answers away: an admin
    hard-delete used to be the one destructive lever on the platform that kept no record of a
    leaked blob, container, published app or registry repository. Empty means nothing survived.

    BLOB-ONLY, and staying that way: the project's own PostgreSQL database and login role are
    NOT torn down here. `DROP DATABASE` cannot run inside a transaction block and this function
    deliberately runs inside its caller's, so the database teardown is the CALLER's POST-COMMIT
    step — `salt_the_earth`, after `db.commit()` (see `admin.hard_delete`). Adding it here would
    not merely be misplaced, it would fail."""
    submission_keys = await all_keys_under(storage, submissions_prefix(app_id))
    # Every saved version too. The list offers two and deletes none, so the prefix holds one
    # bundle per save the app ever had — each a full source tree.
    version_keys = await all_keys_under(storage, version_prefix(app_id))
    survivors: list[tuple[str, str]] = []
    # `recovery_key` alongside `snapshot_key`: both carry the app's whole tree, and a hard
    # delete that leaves one of them behind has not deleted the app.
    survivors.extend(
        ("blob", key)
        for key in await sweep_blobs(
            storage,
            [snapshot_key(app_id), recovery_key(app_id), *submission_keys, *version_keys],
        )
    )
    survivors.extend(
        ("app_container", str(container_id))
        for container_id in await sweep_app_containers(container_store, [app_id])
    )
    # The published container app too, and BEFORE the row goes: after the delete there is
    # nothing left that names the running container, and the sandbox reaper cannot reach it
    # (it sweeps the Redis registry, which a published app is deliberately never in). An
    # admin who hard-deletes an app must not leave it serving that app's data.
    # DELIBERATELY NOT NARROWED, unlike the registry sweep below, and the asymmetry is the
    # point. Both could in principle be filtered to apps with a deployment row — a container,
    # like an image, is only ever created by a pipeline that owns one. But the two failures are
    # not the same size: an image left in the registry costs storage, while a container left
    # standing SERVES THE DELETED APP'S DATA and bills for it, and nothing automatic comes for
    # it. Asking ACA about an app that was never published costs one no-op call; not asking
    # about one that was costs a live container. Over-asking is the cheap direction here and the
    # expensive one below.
    survivors.extend(("published_app", str(i)) for i in await sweep_published_apps([app_id]))
    # ...and the IMAGE it was built from. The citizen's own delete destroys the
    # registry repository; if this path did not, the admin lever — the one whose dialog says
    # "destroyed permanently" — would leave behind exactly what the softer path removes, and
    # the image still carries the app's compiled tree. Lazy import of `settings` because a
    # module-level one is a cycle (the `local_images.py` precedent); `settings.deploy is None`
    # means publishing is off and nothing was ever built.
    from src.config import settings  # lazy: a module-level import is a cycle

    # ...and the IMAGE it was built from, from the same narrowed set.
    survivors.extend(
        ("registry_repository", repo)
        for repo in await sweep_app_repositories(
            await app_ids_that_could_have_an_image(db, [app_id]), config=settings.deploy
        )
    )
    await db.execute(sa.delete(AppRegistry).where(AppRegistry.id == app_id))
    return survivors
