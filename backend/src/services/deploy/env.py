"""The environment a PUBLISHED app runs with.

Three of the four values are identical to what the sandbox gets, and that is the point: the
app the citizen tested is the app that ships, pointed at the same database and the same
object-store container. Only the Blob CREDENTIAL differs, and that difference is
load-bearing.

`provision_app_storage` (the sandbox's builder) mints a SESSION SAS capped at seven days,
because a sandbox never outlives that. A published app does. Handing it a seven-day
credential would give every deployed app a silent, staggered failure exactly one week after
it went live — file uploads breaking on a Tuesday with nothing in the logs to connect it to
a deploy. So publish mints the LONG-LIVED credential instead, which already exists for the
manual runbook and is revocable by construction (it signs against a per-app stored access
policy rather than inlining its own expiry).

One consequence, stated rather than buried: each mint REPLACES the container's whole policy
set, so a redeploy revokes the previous credential — fine, that container is being replaced
— and an admin minting a runbook credential for the same app will cut a live one-click
deploy off from its storage, and vice versa. One live credential per app, by construction.
"""

from __future__ import annotations

import uuid

import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from src.services.storage import get_app_container_store
from src.services.storage.errors import StorageSignError

_log = structlog.get_logger()


class PublishedStorageError(Exception):
    """Object storage is configured but cannot issue a long-lived credential.

    Raised rather than degraded: a published app that silently loses its file storage is
    worse than a deploy that refuses and says why."""


async def build_published_env(
    db: AsyncSession, *, app_id: uuid.UUID, project_id: uuid.UUID
) -> tuple[dict[str, str], str | None]:
    """`(env, container_url)` for the published container.

    `container_url` comes back separately because it is NOT a secret and rides the spec as a
    plain value, while the SAS and the database DSN ride ACA secret references.

    The Blob base is deliberately the SIGNING ACCOUNT's own host, not the sandbox-facing
    override: that override exists so a container inside a local Docker network can reach
    Azurite, and a published app is never in that position. Reusing it would inject a
    development host into production.
    """
    # Lazily imported: `src.services.build_sessions.__init__` reaches the API deps module,
    # which imports back into the partially-initialized package, so a module-level import
    # here makes the cycle depend on which module the interpreter happens to load first.
    # Same accommodation `appdb/provision.py` makes for `src.config`.
    from src.services.build_sessions.appdata import build_app_env
    from src.services.build_sessions.appdb_env import provision_app_database

    env = build_app_env(app_id)
    env |= await provision_app_database(db, project_id)

    store = get_app_container_store()
    if store is None:
        # The supported storage-off deployment (dev/test) — the app simply has no object
        # storage, exactly as `provision_app_storage` behaves.
        return env, None

    await store.ensure_container(app_id)
    try:
        credential = await store.mint_deploy_container_sas(app_id)
    except StorageSignError as exc:
        raise PublishedStorageError(
            "this deployment's storage uses managed identity, which cannot issue a "
            "long-lived credential; a published app needs one"
        ) from exc

    env["BIAL_BLOB_SAS"] = credential.sas
    _log.info(
        "published_storage_credential_minted",
        app_id=str(app_id),
        # Expiry only — never the SAS, never the container URL's query string.
        expires_at=credential.expires_at.isoformat(),
    )
    return env, store.container_url(app_id)
