"""App-row resolution + the base provision-env builder.

`resolve_app_for_project` maps a project to its single `app_registry` row (minting the
`bial_…` app-key on the first build, reusing it forever after — continuity), scoped by
the owning `user_id` (ADR-0004), and returns the app id. `build_app_env` produces the
two always-present `BIAL_*` env vars the sandbox injects at provision and re-injects on
restore:

* `BIAL_APP_ID` — the app's identity, and the only structural read of `app_env`
  (`sandbox/client.restore_from_snapshot`).
* `BIAL_PORTAL_ORIGIN` — the C8 Caddy `frame-ancestors` origin (fails closed to an empty
  ancestor list when unset). Its value is the bare origin of `settings.FRONTEND_URL`.

`BIAL_APP_CREDENTIAL` and `BIAL_DATA_BASE_URL` are GONE (U6): the shared data plane they
addressed no longer exists, and an app's data now lives in its own PostgreSQL database
reached through `BIAL_DATABASE_URL` (see `appdb_env.py`). The `app_key` COLUMN survives —
`GET /apps/{id}/status` still returns it — it is simply no longer injected.

Both names survive the C1 child-env scrub allowlist (`_BIAL_INJECTED_KEYS`, D5/C6).
The one-app-per-project upsert is REPLICATED inline (KTD-6) rather than extracted from
another domain's router — refactoring it would edit another domain's file (anti-collision).
"""

from __future__ import annotations

import uuid
from urllib.parse import urlsplit

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings
from src.core.errors import AppApiError
from src.db.models.app_registry import AppRegistry, AppStatus, mint_app_key
from src.services.projects import owned_project_or_404
from src.services.sandbox import SandboxNotConfiguredError


async def resolve_app_for_project(
    db: AsyncSession, user_id: uuid.UUID, project_id: uuid.UUID
) -> uuid.UUID:
    """Resolve the project's ONE app (mint on first build, reuse thereafter) and return its
    id. Owner-scoped (ADR-0004). The CALLER owns the commit (U5). The upsert still mints
    `app_key` on insert — the key is read back by `GET /apps/{id}/status`, not by callers
    of this function."""
    project = await owned_project_or_404(db, user_id, project_id)
    # The frozen one-app-per-project upsert (KTD-6): a first build INSERTs + mints the
    # key; a repeat DO-UPDATEs (bumps `updated_at`) and returns the SAME row + original
    # key. The owner-guarded WHERE means the DO-UPDATE only touches the caller's own app.
    # No `conversation_id` here — a build session is project-first, not conversation-bound.
    upsert = (
        pg_insert(AppRegistry)
        .values(
            user_id=user_id,
            project_id=project.id,
            app_key=mint_app_key(),
            status=AppStatus.DRAFT,
        )
        .on_conflict_do_update(
            constraint="uq_app_registry_project",
            set_={"updated_at": sa.func.now()},
            where=(AppRegistry.user_id == user_id),
        )
        .returning(AppRegistry.id)
    )
    try:
        app_id: uuid.UUID | None = (await db.execute(upsert)).scalar_one_or_none()
    except IntegrityError as exc:
        # The project was deleted between the owner check and this INSERT — the loser of
        # that race gets the same non-leaking 404, not a 500.
        if "app_registry_project_id_fkey" in str(exc.orig):
            raise AppApiError(404, "Project not found.") from exc
        raise
    if app_id is None:
        # The project's app belongs to another user — fail closed rather than touch it.
        raise AppApiError(409, "Project app is owned by another user.")
    return app_id


def _origin(url: str) -> str:
    """The bare origin (`scheme://host[:port]`, no path / trailing slash) of a URL, per
    C8 §1 — `FRONTEND_URL` is a plain `str`, not guaranteed path-free."""
    parts = urlsplit(url)
    if parts.scheme and parts.netloc:
        return f"{parts.scheme}://{parts.netloc}"
    return url.rstrip("/")


def build_app_env(app_id: uuid.UUID) -> dict[str, str]:
    """The two always-present `BIAL_*` env vars injected into the sandbox at provision and
    re-injected on restore (the app identity + the C8 `BIAL_PORTAL_ORIGIN`). Requires a
    configured sandbox (the router's 503 gate runs first, so this is reached only in the
    configured path) — the check stays because a sandbox-less caller has no business
    building a sandbox env at all, and it is the seam the 503 test pins."""
    if settings.sandbox is None:
        raise SandboxNotConfiguredError("sandbox is not configured: cannot build app env")
    return {
        "BIAL_APP_ID": str(app_id),
        "BIAL_PORTAL_ORIGIN": _origin(settings.FRONTEND_URL),
    }
