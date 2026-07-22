"""The per-project DATABASE half of a build session's injected env (ADR-0028, R3/R4).

`provision_app_database` ensures the project's own database + role exist and returns the
single `BIAL_DATABASE_URL` var the sandbox injects into `next dev`. It is called ONLY on
the arms where a container is BORN — start's provision/restore arm and relaunch — never on
attach, which reuses the live container's birth env (KTD-3, exactly like the Blob SAS).

A module of its own rather than a new function in `appdata.py`, for the same reason
`appstorage.py` is one: `build_app_env` is a SYNC, pure builder of the frozen four-var C9
set with no session and no I/O, while this needs `await` and an `AsyncSession`. Folding it
in would make every call site async for a value that is merged over the dict anyway — so
this follows the established split and merges the same way the Blob pair does.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from src.services.appdb.provision import ensure_project_database, sandbox_dsn


async def provision_app_database(db: AsyncSession, project_id: uuid.UUID) -> dict[str, str]:
    """Ensure the project's database + role; return `{"BIAL_DATABASE_URL": <sandbox DSN>}`.

    Returns `{}` when `APP_DB__*` is unconfigured (dev/test) — a no-op merge, so the app
    simply has no persistence (KTD-2), mirroring `provision_app_storage`'s disabled-store
    path. A genuine substrate error PROPAGATES (fail-first): on a birth arm it fails the
    start before any sandbox handle exists, and the whole sequence is idempotent, so the
    next start just re-runs it.

    `ensure_project_database` is idempotent and self-healing, which is what makes this the
    LAZY ensure too: a project created before this feature existed (or while the substrate
    was unconfigured) gets its database on its next build, without a migration or a sweep.

    The injected value is the SANDBOX form — bare `postgresql://`, host-rewritten for the
    container. `control_plane_dsn` must never reach a sandbox.
    """
    record = await ensure_project_database(db, project_id)
    if record is None:
        return {}
    return {"BIAL_DATABASE_URL": sandbox_dsn(record)}
