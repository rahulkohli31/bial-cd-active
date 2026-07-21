"""The maintenance engine: a second, AUTOCOMMIT, unpooled async engine used ONLY for
cluster-level DDL (`CREATE`/`DROP DATABASE`, `CREATE`/`DROP ROLE`, `GRANT`, `COMMENT`).

Three properties, each load-bearing:

* **AUTOCOMMIT** — `CREATE DATABASE` and `DROP DATABASE` cannot run inside a transaction
  block. There is no isolation-level precedent anywhere else in `src/`; this is net-new.
* **`NullPool`** — pytest-asyncio runs a per-function event loop and an asyncpg connection
  is loop-bound (`tests/conftest.py:37-40`). An unpooled engine holds no connection between
  calls, so the same engine object is safe across loops.
* **Lazy** — built on first use behind an accessor, never at import. `tests/conftest.py`
  rebinds only `src.db.base.engine`; a module-global engine constructed at import time
  would escape that and bind to whichever loop imported it first.

`get_maintenance_engine()` returns **`None`** when `APP_DB__*` is unset, deliberately
copying `get_app_container_store()` (`services/storage/accessor.py:52-67`) rather than the
raising `get_storage()`: a project with no database is a supported deployment — the app
just has no persistence — so callers branch on `None` (KTD-2 divergence, documented there).
Resolve it lazily INSIDE a route body's error seam, never as an eager `Depends`
(`docs/solutions/design-patterns/eager-fastapi-depends-bypasses-in-body-error-seam-2026-07-21.md`,
commit 6be7a9c).

The maintenance identity is a password role by decision (ADR-0027 scope note: Entra token
auth covers the control-plane `DATABASE_URL` identity only), so `attach_entra_token` is
deliberately NOT applied here.
"""

from __future__ import annotations

import structlog
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

_log = structlog.get_logger()

_maintenance_engine: AsyncEngine | None = None


def get_maintenance_engine() -> AsyncEngine | None:
    """The AUTOCOMMIT maintenance engine, or `None` when no substrate is configured."""
    global _maintenance_engine
    if _maintenance_engine is None:
        from src.config import settings  # lazy: avoid an import cycle via src.config

        if settings.app_db is None:
            return None
        _maintenance_engine = create_async_engine(
            settings.app_db.maintenance_dsn.get_secret_value(),
            isolation_level="AUTOCOMMIT",
            poolclass=NullPool,
        )
    return _maintenance_engine


async def aclose_maintenance_engine() -> None:
    """Dispose the engine and drop the singleton (FastAPI lifespan shutdown).

    Isolated like `aclose_storage`: a failing dispose is logged (never silently swallowed)
    but the singleton is STILL reset, so a restart never reuses a half-closed engine.
    """
    global _maintenance_engine
    engine, _maintenance_engine = _maintenance_engine, None
    if engine is None:
        return
    try:
        await engine.dispose()
    except Exception:
        _log.exception("app-database maintenance engine teardown failed")


async def reset_maintenance_engine_for_tests() -> None:
    """Drop the cached engine so a suite that monkeypatches `settings.app_db` never reuses
    an engine built from the previous configuration."""
    await aclose_maintenance_engine()
