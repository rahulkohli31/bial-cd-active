"""The environment a PUBLISHED app runs with.

The values match the sandbox — same DB, same object-store container, and the same connector
coordinates when the project has been granted them — so the app the citizen tested is the app that
ships. Only the Blob CREDENTIAL differs, and it is load-bearing:
the sandbox's builder mints a 7-day SESSION SAS, but a published app outlives that, so publish
mints the LONG-LIVED credential instead (the one that already exists for the manual runbook;
revocable via a per-app stored access policy rather than an inlined expiry).

CONSEQUENCE: each mint REPLACES the container's whole policy set — a redeploy revokes the
previous credential (fine, it's being replaced), but minting a runbook credential for the same
app cuts off a live one-click deploy's storage, and vice versa. One live credential per app.
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
    db: AsyncSession, *, app_id: uuid.UUID, project_id: uuid.UUID, user_id: uuid.UUID
) -> tuple[dict[str, str], str | None]:
    """`(env, container_url)` for the published container.

    `container_url` comes back separately: it is NOT a secret and rides the spec as a plain
    value, while the SAS and DB DSN ride ACA secret references.
    The Blob base is deliberately the SIGNING ACCOUNT's own host, not the sandbox-facing
    override (which exists only so a container on a local Docker network can reach Azurite) —
    reusing it would inject a development host into production.

    `user_id` IS THE OWNERSHIP CLAIM, not a convenience. The connector coordinates are granted
    per person and per project, so the builder below needs to know whose project this is; an
    app published by somebody who never had access to a connector must not carry a credential
    to it. It is threaded in rather than looked up here for the reason the whole module is
    written this way: the caller already holds it, and a second lookup is a second chance to
    scope it wrongly.

    NO WINDOW DATES, for the same reason the sandbox gets none: a deployed app is uncapped by
    ruling, so a build-time window injected into it would be a limit that means nothing. It
    still inherits the connector's day-late ceiling, which is a fact about the data rather than
    a grant.
    """
    # Lazily imported: `src.services.build_sessions.__init__` reaches the API deps module,
    # which imports back into the partially-initialized package, so a module-level import
    # here makes the cycle depend on which module the interpreter happens to load first.
    # Same accommodation `appdb/provision.py` makes for `src.config`.
    from src.services.build_sessions.appconnector_env import build_connector_env
    from src.services.build_sessions.appdata import build_app_env
    from src.services.build_sessions.appdb_env import provision_app_database

    env = build_app_env(app_id)
    env |= await provision_app_database(db, project_id)
    # THE SAME GATE THE SANDBOX USES, through the same function: a lake configured, the
    # connector switched on for this project, and its owner's access approved. Not inferred from
    # the lake being configured platform-wide — that would hand every published app on the
    # platform a credential to BIAL's flight data.
    env |= await build_connector_env(db, user_id=user_id, project_id=project_id)

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
