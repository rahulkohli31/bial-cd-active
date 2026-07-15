"""Per-app Blob object-storage provisioning for a build session (C9 §6, KTD-3/KTD-4).

`provision_app_storage` ensures the app's Blob container and mints a fresh container-scoped SAS,
returning the two `BIAL_BLOB_*` env vars the sandbox injects into `next dev`. It is called ONLY on
the provision/restore (birth) arms of build-session start — never on attach, which reuses the live
container's original SAS (KTD-3).

`build_app_env` (appdata.py) stays the pure C9 four-var builder; this is the separate, optional
storage half, so a Blob hiccup on a birth path fails the start fail-first (the idempotent container
is reused next start) while a reconnect to a healthy live preview never touches storage.
"""

from __future__ import annotations

import uuid

from src.config import settings
from src.services.storage import get_app_container_store


async def provision_app_storage(app_id: uuid.UUID) -> dict[str, str]:
    """Ensure the app's container + mint a fresh container-scoped SAS; return
    `{BIAL_BLOB_CONTAINER_URL, BIAL_BLOB_SAS}`.

    Returns `{}` when object storage is unconfigured (dev/test) — the store accessor returns None,
    so the feature is gracefully off (KTD-2). A genuine storage error from a *configured* store
    PROPAGATES (fail-first): on the birth path it fails the start before any sandbox handle exists
    (nothing to tear down; the idempotent container is reused on the next start)."""
    store = get_app_container_store()
    if store is None:
        return {}
    await store.ensure_container(app_id)
    sas = await store.mint_container_sas(app_id)
    # The injected container URL must be a host the SANDBOX can reach (KTD-2): the sandbox-facing
    # Blob base if configured, else the signing account's account_url (the None default).
    sandbox = settings.sandbox
    base_url = sandbox.blob_base_url if sandbox is not None else None
    return {
        "BIAL_BLOB_CONTAINER_URL": store.container_url(app_id, base_url=base_url),
        "BIAL_BLOB_SAS": sas,
    }
