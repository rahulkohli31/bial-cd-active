"""The A1 login-gate control var, injected on every BIRTH arm (mirrors `appstorage.py` /
`appdb_env.py`).

`provision_app_login_gate` returns `{"BIAL_LOGIN_REQUIRED": "true" | "false"}`, read from the
app's own `login_required` admin flag. Unlike every other `BIAL_*` var, this one is deliberately
NOT forwarded to the generated app's own process: it is consumed only by the supervisor's root
process (the `/auth/check` gate Caddy's `forward_auth` calls), so it is intentionally absent from
the supervisor's `_INJECTED_ENV` child-allowlist, the template's `.env.example`, `bial-config.ts`,
and the build prompt — a future reader should not "fix" that omission, it is the point.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models.app_registry import AppRegistry


async def provision_app_login_gate(db: AsyncSession, app_id: uuid.UUID) -> dict[str, str]:
    """Read `app_registry.login_required` for `app_id` and return the string-boolean the
    supervisor's `os.environ` read expects. Only takes effect at container birth — toggling
    the flag on an already-live attached container has no effect until the sandbox is next
    reborn (a follow-up, not a bug: see A1)."""
    value = await db.scalar(
        sa.select(AppRegistry.login_required).where(AppRegistry.id == app_id)
    )
    return {"BIAL_LOGIN_REQUIRED": "true" if value else "false"}
