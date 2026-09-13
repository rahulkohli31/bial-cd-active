"""App-row resolution + the base provision-env builder.

`resolve_app_for_project` maps a project to its single `app_registry` row — minting the
`bial_…` app-key on first build, reusing it forever after — scoped by the owning `user_id`.
`build_app_env` returns the two vars injected at provision and re-injected on
restore: `BIAL_APP_ID`, the only structural read of `app_env`, and `BIAL_PORTAL_ORIGIN`,
the Caddy `frame-ancestors` origin, which fails closed when unset.

`BIAL_BASE_PATH` and `BIAL_APPS_HOSTNAME` belong to `sandbox/client._provision_container`:
`deploy/env.py` calls `build_app_env` too, so a base path added here would ship an `sbx-`
value into published containers built with a `pub-` one. The database half: `appdb_env.py`.
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

# THE SWITCHED-OFF REFUSAL, in one place because two surfaces say it: the gate
# below, and the pre-read in `api/v1/conversations/turns.py` that lets a citizen read this
# sentence at the moment of sending rather than meet it as a dead turn. The pre-read is a
# MESSAGE, not a second enforcement point — remove it and the platform still refuses here.
#
# NO REMEDY IS OFFERED because the citizen has none: only an administrator can undo this, and
# suggesting a retry would send them round a loop that cannot end. It matches the switched-off
# copy the workspace rail already renders, and neither sentence mentions publishing — a
# never-published draft can be switched off too, and telling its owner that nothing can be
# published tells them nothing about why their workspace will not start.
APP_SWITCHED_OFF = (
    "An administrator switched this app off. You cannot make changes to it until they "
    "switch it back on."
)
# Machine-readable so the browser can tell this from the workspace CONFLICTS that share its
# status family — different cause, and this one has no remedy to retry.
APP_SWITCHED_OFF_CODE = "app_switched_off"


async def resolve_app_for_project(
    db: AsyncSession, user_id: uuid.UUID, project_id: uuid.UUID
) -> uuid.UUID:
    """Resolve the project's ONE app (mint on first build, reuse thereafter) and return its
    id. Owner-scoped. The CALLER owns the commit. The upsert still mints
    `app_key` on insert — the key is read back by `GET /apps/{id}/status`, not by callers
    of this function.

    THIS IS WHERE A SWITCHED-OFF APP STOPS, and it is the ONE place it stops.
    Every door into a container comes through here, and there are exactly two of them:
    `relaunch_preview` — the explicit start control the citizen presses — and
    `ensure_sandbox`, which `services/turns/engine.py` routes EVERY turn kind through, Ask,
    Plan and Build alike. So one status check closes both, and there is no second enforcement
    point to keep in step with this one. (There was a third, `_start_locked`, behind the
    orphaned bare `POST` on the build-sessions collection; the legacy build stack was deleted,
    taking both with it.
    Grep `await resolve_app_for_project` before adding a caller — a new one inherits this
    refusal, which is the point, and must not be written to route around it.)

    SAVE IS DELIBERATELY NOT GATED, and that omission is load-bearing rather than an
    oversight. `save_project_snapshot` reads its app id through `existing_app_id`, never
    through this function, so it is structurally out of reach of this refusal — and it must
    stay that way. Save is the only thing that writes a citizen's work to durable storage
    and containers are ephemeral (the reaper destroys idle ones), so refusing it in the one
    window where it matters — an administrator flips the switch while the owner holds
    unsaved work in a live container — permanently destroys that work. The accepted trade is
    that a disabled app's snapshot may advance by one commit: nothing consumes it, because
    publish still refuses (`deploy/router.py`), approval pins a submission rather than the
    saved head, and the app is off the live roster and out of the catalog.

    THE STATUS IS READ FROM THE UPSERT'S OWN `RETURNING`, not from a SELECT before it. A
    read-then-upsert would decide on a status the row had already stopped holding; this
    decides on the row the statement actually touched. On the refusal path the `updated_at`
    bump the DO-UPDATE made is never committed — the caller owns the commit and every one of
    them raises straight past it, with `get_db` rolling the request transaction back."""
    project = await owned_project_or_404(db, user_id, project_id)
    # The frozen one-app-per-project upsert: a first build INSERTs + mints the
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
        .returning(AppRegistry.id, AppRegistry.status)
    )
    try:
        resolved = (await db.execute(upsert)).first()
    except IntegrityError as exc:
        # The project was deleted between the owner check and this INSERT — the loser of
        # that race gets the same non-leaking 404, not a 500.
        if "app_registry_project_id_fkey" in str(exc.orig):
            raise AppApiError(404, "Project not found.") from exc
        raise
    if resolved is None:
        # The project's app belongs to another user — fail closed rather than touch it.
        raise AppApiError(409, "Project app is owned by another user.")
    app_id: uuid.UUID = resolved.id
    app_status: AppStatus = resolved.status
    # THE GATE. A fresh INSERT returns DRAFT, so this can only ever fire on a row that was
    # already there and already switched off.
    if app_status is AppStatus.DISABLED:
        raise AppApiError(409, APP_SWITCHED_OFF, code=APP_SWITCHED_OFF_CODE)
    return app_id


def _origin(url: str) -> str:
    """The bare origin (`scheme://host[:port]`, no path / trailing slash) of a URL —
    `FRONTEND_URL` is a plain `str`, not guaranteed path-free."""
    parts = urlsplit(url)
    if parts.scheme and parts.netloc:
        return f"{parts.scheme}://{parts.netloc}"
    return url.rstrip("/")


def build_app_env(app_id: uuid.UUID) -> dict[str, str]:
    """The two always-present `BIAL_*` env vars injected into the sandbox at provision and
    re-injected on restore (the app identity + the `BIAL_PORTAL_ORIGIN`). Requires a
    configured sandbox (the router's 503 gate runs first, so this is reached only in the
    configured path) — the check stays because a sandbox-less caller has no business
    building a sandbox env at all, and it is the seam the 503 test pins."""
    if settings.sandbox is None:
        raise SandboxNotConfiguredError("sandbox is not configured: cannot build app env")
    return {
        "BIAL_APP_ID": str(app_id),
        "BIAL_PORTAL_ORIGIN": _origin(settings.FRONTEND_URL),
    }
