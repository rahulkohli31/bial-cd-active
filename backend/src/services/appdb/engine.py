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
from sqlalchemy.engine import URL, make_url
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from src.services.appdb.errors import AppDatabaseUnconfiguredError

_log = structlog.get_logger()

_maintenance_engine: AsyncEngine | None = None


def _new_maintenance_engine(url: str | URL) -> AsyncEngine:
    """Construct an AUTOCOMMIT, `NullPool` async engine for cluster/database DDL against `url`.

    Shared by the pinned maintenance engine and the per-database helper so their connect
    posture — isolation, pool, and every hang ceiling — can never diverge.
    """
    return create_async_engine(
        url,
        isolation_level="AUTOCOMMIT",
        poolclass=NullPool,
        # Bound every hang the way every other external client here does (Foundry 10s
        # connect, Redis 2s). asyncpg spelling: `timeout` is the connect ceiling;
        # per-session `statement_timeout`/`lock_timeout` go through `server_settings`.
        # The statement ceiling MUST stay generous — `CREATE DATABASE ... TEMPLATE
        # template0` on Azure Flexible Server can legitimately take several seconds, so
        # 30s is the floor here, not a number to trim. The lock ceiling stays tight (5s):
        # a maintenance statement should never sit blocked on a lock for long.
        connect_args={
            "timeout": 10,
            "server_settings": {
                "statement_timeout": "30000",
                "lock_timeout": "5000",
            },
        },
    )


def get_maintenance_engine() -> AsyncEngine | None:
    """The AUTOCOMMIT maintenance engine, or `None` when no substrate is configured."""
    global _maintenance_engine
    if _maintenance_engine is None:
        from src.config import settings  # lazy: avoid an import cycle via src.config

        if settings.app_db is None:
            return None
        _maintenance_engine = _new_maintenance_engine(
            settings.app_db.maintenance_dsn.get_secret_value()
        )
    return _maintenance_engine


def maintenance_engine_for_database(db_name: str) -> AsyncEngine:
    """A FRESH AUTOCOMMIT/`NullPool` engine bound to ONE app database — the maintenance DSN
    with `database=` swapped to `db_name`. The caller OWNS it and must dispose it.

    Schemas are per-database and `ALTER SCHEMA public` acts on the CURRENT database, so a
    provisioning step that must touch an app database's `public` cannot ride the shared
    maintenance engine (pinned to the maintenance database). Unlike `get_maintenance_engine`
    this is deliberately NOT cached: it is a short-lived, single-statement engine, and caching
    one per app database would leak engines without bound.

    Raises `AppDatabaseUnconfiguredError` when no substrate is configured — its only caller
    runs after `get_maintenance_engine` has already established one, so an unset substrate here
    is a real invariant break, not the supported no-op that `get_maintenance_engine` models.
    """
    from src.config import settings  # lazy: avoid an import cycle via src.config

    if settings.app_db is None:
        raise AppDatabaseUnconfiguredError(
            "per-project databases are not configured: set APP_DB__MAINTENANCE_DSN and "
            "APP_DB__ENCRYPTION_KEY"
        )
    url = make_url(settings.app_db.maintenance_dsn.get_secret_value()).set(database=db_name)
    return _new_maintenance_engine(url)


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
