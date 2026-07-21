"""Sever a project database from its app, and (irreversibly) salt the earth.

Two entry points sharing one primitive:

* `sever()` — the kill-switch half. Order is load-bearing: lock the door (`NOLOGIN`,
  `REVOKE CONNECT`) BEFORE kicking anyone out (`pg_terminate_backend`), so a pooled client
  that immediately reconnects finds it locked. Terminating first would just hand the pool a
  fresh, fully-privileged connection. It RAISES on failure — a `disable` that silently
  failed to sever would be a lie told to an operator.
* `salt_the_earth()` — sever, then `DROP DATABASE ... WITH (FORCE)`, then `DROP ROLE`. It
  runs POST-commit on the delete paths, where the registry row is already gone and there is
  nothing left to roll back, so it is best-effort per step with its own logging and
  NEVER raises (`.claude/rules/naming.md`: the name encodes the behaviour).

Both are idempotent, both take plain name scalars (captured pre-commit by the caller —
`services/projects/delete.py` shape), and both classify errors by SQLSTATE, never by
message text: "already gone" and "transient Azure name-lock" read identically in prose and
completely differently in code.

`restore_login()` is `sever`'s inverse for the admin `enable` lever.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Final

import sqlalchemy as sa
import structlog
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection

from src.services.appdb.engine import get_maintenance_engine
from src.services.appdb.names import quote_identifier

_log = structlog.get_logger()

# "The object was already gone" — the idempotency signals for the drop half.
_UNDEFINED_DATABASE: Final = "3D000"  # invalid_catalog_name
_UNDEFINED_OBJECT: Final = "42704"  # role does not exist
# "The object is still busy" — transient, retryable, NOT already-gone. Azure's ~30s
# post-delete name lock surfaces here too, which is exactly why the two are told apart by
# code: a caller must never read "busy" as "done".
_OBJECT_IN_USE: Final = "55006"
_DEPENDENT_OBJECTS: Final = "2BP01"

_ALREADY_GONE: Final = frozenset({_UNDEFINED_DATABASE, _UNDEFINED_OBJECT})


async def sever(*, db_name: str, role_name: str) -> bool:
    """Lock the app out of its database and terminate its live sessions.

    Returns False (no-op) when no substrate is configured; True once the door is locked.
    Raises the underlying error if any step fails — the caller is a kill-switch and must
    not report success on a half-severed database.
    """
    engine = get_maintenance_engine()
    if engine is None:
        _log.debug("app_database_sever_skipped_unconfigured", db_name=db_name)
        return False
    quoted_db = quote_identifier(db_name)
    quoted_role = quote_identifier(role_name)
    async with engine.connect() as conn:
        # 1 + 2: the door. Both are pure grants/attributes — they take effect for the NEXT
        # connection attempt, which is why they must precede the terminate.
        await conn.execute(sa.text(f"ALTER ROLE {quoted_role} NOLOGIN"))
        await conn.execute(sa.text(f"REVOKE CONNECT ON DATABASE {quoted_db} FROM {quoted_role}"))
        # 3, LAST: evict whoever is already inside.
        killed = await _terminate_backends(conn, db_name=db_name)
    _log.info("app_database_severed", db_name=db_name, role_name=role_name, backends_killed=killed)
    return True


async def restore_login(*, db_name: str, role_name: str) -> bool:
    """`sever`'s inverse for the admin `enable` lever: re-grant CONNECT, then LOGIN.

    Mirror-image order — open the inner door before the outer one — so no window exists in
    which the role can log in but not reach its database.
    """
    engine = get_maintenance_engine()
    if engine is None:
        _log.debug("app_database_restore_skipped_unconfigured", db_name=db_name)
        return False
    quoted_db = quote_identifier(db_name)
    quoted_role = quote_identifier(role_name)
    async with engine.connect() as conn:
        await conn.execute(sa.text(f"GRANT CONNECT ON DATABASE {quoted_db} TO {quoted_role}"))
        await conn.execute(sa.text(f"ALTER ROLE {quoted_role} LOGIN"))
    _log.info("app_database_login_restored", db_name=db_name, role_name=role_name)
    return True


async def salt_the_earth(*, db_name: str, role_name: str) -> None:
    """Irreversibly destroy a project's database and its role. Never raises.

    Post-commit best-effort (the registry row is already gone by the time this runs), so a
    failure here leaves an orphan for the reconciler to sweep — strictly better than
    exploding a delete endpoint that has already committed.
    """
    engine = get_maintenance_engine()
    if engine is None:
        _log.debug("app_database_drop_skipped_unconfigured", db_name=db_name)
        return
    async with engine.connect() as conn:
        async with _best_effort("sever", db_name=db_name):
            await _sever_on(conn, db_name=db_name, role_name=role_name)
        # WITH (FORCE) terminates whatever reconnected between the sever and here — the
        # relaunched preview and the deployed container hold no build lock, so the
        # delete-time guard does not cover them and this is the actual guarantee.
        async with _best_effort("drop_database", db_name=db_name):
            await _drop_database(conn, db_name=db_name)
        # The role can only be dropped after the database it owns is gone.
        async with _best_effort("drop_role", db_name=db_name):
            await _drop_role(conn, role_name=role_name)
    _log.info("app_database_salted", db_name=db_name, role_name=role_name)


async def _drop_database(conn: AsyncConnection, *, db_name: str) -> None:
    await conn.execute(sa.text(f"DROP DATABASE {quote_identifier(db_name)} WITH (FORCE)"))


async def _drop_role(conn: AsyncConnection, *, role_name: str) -> None:
    await conn.execute(sa.text(f"DROP ROLE {quote_identifier(role_name)}"))


async def _sever_on(conn: AsyncConnection, *, db_name: str, role_name: str) -> None:
    quoted_db = quote_identifier(db_name)
    quoted_role = quote_identifier(role_name)
    await conn.execute(sa.text(f"ALTER ROLE {quoted_role} NOLOGIN"))
    await conn.execute(sa.text(f"REVOKE CONNECT ON DATABASE {quoted_db} FROM {quoted_role}"))
    await _terminate_backends(conn, db_name=db_name)


async def _terminate_backends(conn: AsyncConnection, *, db_name: str) -> int:
    # The one place a name IS a bindable value rather than an identifier: `datname` is a
    # column comparison, so it goes through a parameter like any other query.
    result = await conn.execute(
        sa.text(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = :db_name AND pid <> pg_backend_pid()"
        ),
        {"db_name": db_name},
    )
    return len(result.fetchall())


@asynccontextmanager
async def _best_effort(label: str, **context: str) -> AsyncIterator[None]:
    """Run one teardown step, swallowing nothing silently: an already-gone object is logged
    at debug (that IS the idempotent outcome), anything else at warning with its SQLSTATE
    so an operator can tell a transient lock from a real failure."""
    try:
        yield
    except DBAPIError as exc:
        code = _sqlstate(exc)
        if code in _ALREADY_GONE:
            _log.debug(f"app_database_{label}_already_gone", sqlstate=code, **context)
            return
        _log.warning(
            f"app_database_{label}_failed",
            sqlstate=code,
            transient=code in {_OBJECT_IN_USE, _DEPENDENT_OBJECTS},
            **context,
        )
    except Exception:
        _log.exception(f"app_database_{label}_failed", **context)


def _sqlstate(exc: DBAPIError) -> str | None:
    code = getattr(exc.orig, "sqlstate", None)
    return code if isinstance(code, str) else None
