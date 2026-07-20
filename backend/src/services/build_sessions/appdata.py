"""C9 app-data credential mint + the provision-env builder.

`resolve_app_for_project` maps a project to its single `app_registry` row (minting the
`bial_…` app-key on the first build, reusing it forever after — continuity), scoped by
the owning `user_id` (ADR-0004). `build_app_env` produces the exact four `BIAL_*` env
vars the sandbox injects at provision and re-injects on restore:

* `BIAL_APP_ID` / `BIAL_APP_CREDENTIAL` / `BIAL_DATA_BASE_URL` — the frozen C9 set the
  generated app uses to reach the platform data-service.
* `BIAL_PORTAL_ORIGIN` — the C8 Caddy `frame-ancestors` origin (fails closed to an empty
  ancestor list when unset). Its value is the bare origin of `settings.FRONTEND_URL`.

All four names survive the C1 child-env scrub allowlist (`_BIAL_INJECTED_KEYS`, D5/C6).
The one-app-per-project upsert is REPLICATED inline (KTD-6) rather than extracted from
`apps/router.provision` — refactoring it would edit another domain's file (anti-collision).
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
) -> tuple[uuid.UUID, str]:
    """Resolve the project's ONE app (mint on first build, reuse thereafter) and return
    `(app_id, app_key)`. Owner-scoped (ADR-0004). The CALLER owns the commit (U5)."""
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
        .returning(AppRegistry.id, AppRegistry.app_key)
    )
    try:
        row = (await db.execute(upsert)).one_or_none()
    except IntegrityError as exc:
        # The project was deleted between the owner check and this INSERT — the loser of
        # that race gets the same non-leaking 404, not a 500.
        if "app_registry_project_id_fkey" in str(exc.orig):
            raise AppApiError(404, "Project not found.") from exc
        raise
    if row is None:
        # The project's app belongs to another user — fail closed rather than touch it.
        raise AppApiError(409, "Project app is owned by another user.")
    return row.id, row.app_key


def _origin(url: str) -> str:
    """The bare origin (`scheme://host[:port]`, no path / trailing slash) of a URL, per
    C8 §1 — `FRONTEND_URL` is a plain `str`, not guaranteed path-free."""
    parts = urlsplit(url)
    if parts.scheme and parts.netloc:
        return f"{parts.scheme}://{parts.netloc}"
    return url.rstrip("/")


def build_app_env(app_id: uuid.UUID, app_key: str) -> dict[str, str]:
    """The four `BIAL_*` env vars injected into the sandbox at provision and re-injected
    on restore (C9 + the C8 `BIAL_PORTAL_ORIGIN`). Requires a configured sandbox (the
    router's 503 gate runs first, so this is reached only in the configured path)."""
    sandbox = settings.sandbox
    if sandbox is None:
        raise SandboxNotConfiguredError("sandbox is not configured: cannot build app env")
    return {
        "BIAL_APP_ID": str(app_id),
        "BIAL_APP_CREDENTIAL": app_key,
        "BIAL_DATA_BASE_URL": sandbox.app_data_base_url,
        "BIAL_PORTAL_ORIGIN": _origin(settings.FRONTEND_URL),
    }
