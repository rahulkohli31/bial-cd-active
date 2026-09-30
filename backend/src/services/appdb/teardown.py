"""Sever a project database from its app, and (irreversibly) salt the earth.

Two entry points share one primitive:

* `sever()` — the kill-switch half. RAISES on failure (a silently half-severed database
  would be a lie told to an operator).
* `salt_the_earth()` — sever, then DROP DATABASE, then DROP ROLE. Runs POST-commit, where
  the registry row is already gone and there is nothing to roll back, so it is best-effort
  per step and NEVER raises.

WHY THIS EXISTS: `sever`'s internal order is load-bearing — lock the door (`NOLOGIN`,
`REVOKE CONNECT`) BEFORE kicking anyone out (`pg_terminate_backend`). Terminating first
would just hand a pooled client that immediately reconnects a fresh, privileged connection.

Both levers are idempotent and classify errors by SQLSTATE, never message text — "already
gone" and "transient Azure name-lock" read identically in prose, differently in code.
`restore_login()` is `sever`'s inverse. `teardown_handles()` is the only thing here that
touches the ORM: every lever runs where the registry row is gone or about to be, so names
must be read out as plain scalars before the caller's commit.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Final

import sqlalchemy as sa
import structlog
from sqlalchemy.exc import DBAPIError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from src.core.alarms import TEARDOWN_ARTEFACT_SURVIVED_EVENT
from src.db.models.project_database import ProjectDatabase
from src.services.appdb.engine import get_maintenance_engine
from src.services.appdb.names import quote_identifier

_log = structlog.get_logger()


@dataclass(frozen=True)
class TeardownHandles:
    """The two names every lever in this module needs, as plain strings.

    Frozen and scalar-only ON PURPOSE (the `ProjectCascadeCleanup` value-type idiom): the
    delete paths read these BEFORE their commit and use them AFTER it, and touching an ORM
    attribute across a commit triggers lazy I/O on a closed greenlet.
    Deleting the project cascades the `project_databases` row away, so post-commit there is
    nothing left to read them from either way.
    """

    db_name: str
    role_name: str


async def teardown_handles(db: AsyncSession, project_id: uuid.UUID) -> TeardownHandles | None:
    """The project's database + role names, or `None` when it has no registry row.

    `None` is a legitimately-absent result, not an error channel: a project provisioned
    before per-project databases existed (or on a deployment with `APP_DB__*` unset) simply
    has no row, and every caller treats that as a clean no-op. The names are read from the
    row rather than re-derived from `project_id` so a row minted under an older derivation
    still tears down correctly (`db_name`/`role_name` are STORED for exactly this reason).
    """
    row = (
        await db.execute(
            sa.select(ProjectDatabase.db_name, ProjectDatabase.role_name).where(
                ProjectDatabase.project_id == project_id
            )
        )
    ).one_or_none()
    return None if row is None else TeardownHandles(db_name=row.db_name, role_name=row.role_name)


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

    Only sessions of roles this one holds the privileges of are terminated, which covers every
    session the app can hold. Anything else inside — an autovacuum worker, another role's session
    — is not the app, and Postgres would refuse to terminate it.

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


async def salt_the_earth(*, db_name: str, role_name: str) -> bool:
    """Irreversibly destroy a project's database and its role. Never raises.

    Post-commit best-effort (the registry row is already gone by the time this runs), so a
    failure here leaves the database standing rather than exploding a delete endpoint that has
    already committed.

    RETURNS WHETHER THE EARTH IS ACTUALLY SALTED. `True` means every step succeeded or
    found its object already gone; `False` means a copy of the citizen's data is still on the
    cluster after they asked for it to be destroyed, and the caller records that. The orphan is
    not swept automatically: `appdb/reconcile.py` is operator-invoked and, by its own docstring,
    REPORT-ONLY — it deletes nothing, ever. Nothing automatic collects this.

    An unconfigured substrate returns `True`: with `APP_DB__*` unset no database was ever
    provisioned, so there is nothing to have survived (the same reading `sweep_app_containers`
    gives a `None` store)."""
    engine = get_maintenance_engine()
    if engine is None:
        _log.debug("app_database_drop_skipped_unconfigured", db_name=db_name)
        return True
    try:
        async with engine.connect() as conn:
            async with _best_effort("sever", db_name=db_name) as sever_step:
                await _sever_on(conn, db_name=db_name, role_name=role_name)
            # The relaunched preview and the deployed container hold no build lock, so the
            # delete-time guard does not cover them: the sever, and the second eviction inside
            # the drop, are the actual guarantee.
            async with _best_effort("drop_database", db_name=db_name) as drop_step:
                await _drop_database(conn, db_name=db_name)
            # The role can only be dropped after the database it owns is gone.
            async with _best_effort("drop_role", db_name=db_name) as role_step:
                await _drop_role(conn, role_name=role_name)
    except (SQLAlchemyError, OSError) as exc:
        # The per-step guards above cover a statement failing; they do NOT cover the
        # `connect()` ITSELF failing on an unreachable cluster (a bare OSError before the
        # driver wraps it, or a SQLAlchemy connect error). The whole point of the name is
        # that this never raises — a post-commit delete must not 500 — so swallow it. Error
        # TYPE only: the exception text can carry the maintenance DSN/password (the
        # discipline `_scrubbed_role_failure` keeps).
        _log.warning(
            TEARDOWN_ARTEFACT_SURVIVED_EVENT,
            artefact="app_database",
            artefact_id=db_name,
            reason="the database cluster could not be reached",
            error_type=type(exc).__name__,
        )
        return False
    # THE ROLE COUNTS AS PART OF THE ARTEFACT. A dropped database whose login role survives is
    # still a leak — the role is a credential holder, and the next provision derives a fresh
    # name rather than reusing it, so nothing will ever pick this one up.
    if not (sever_step.ok and drop_step.ok and role_step.ok):
        _log.warning(
            TEARDOWN_ARTEFACT_SURVIVED_EVENT,
            artefact="app_database",
            artefact_id=db_name,
            reason="a teardown step failed (see the app_database_* event beside this one)",
        )
        return False
    _log.info("app_database_salted", db_name=db_name, role_name=role_name)
    return True


async def _drop_database(conn: AsyncConnection, *, db_name: str) -> None:
    # No FORCE: it refuses outright when an autovacuum worker is inside. A plain DROP stops the
    # worker itself, and waits up to 5 s for any other session before reporting the database busy.
    drop = sa.text(f"DROP DATABASE {quote_identifier(db_name)}")
    try:
        await conn.execute(drop)
    except DBAPIError as exc:
        if _sqlstate(exc) != _OBJECT_IN_USE:
            raise
        # A session that connected just before the door locked can miss the first eviction. With
        # nothing new to evict, a second wait would end the same way.
        if await _terminate_backends(conn, db_name=db_name) == 0:
            raise
        await conn.execute(drop)


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
    # `pg_has_role`: Postgres refuses to terminate a session of a role this one has no
    # privileges of, and a single refusal fails the whole statement.
    result = await conn.execute(
        sa.text(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
            "WHERE datname = :db_name AND pid <> pg_backend_pid() "
            "AND pg_has_role(usesysid, 'USAGE')"
        ),
        {"db_name": db_name},
    )
    return len(result.fetchall())


@dataclass
class _StepOutcome:
    """Whether one `_best_effort` step left its object gone. Mutable and yielded BY the
    context manager, because a caller that swallows an exception has no other way to learn one
    happened — and `salt_the_earth` owes its own caller a truthful answer about whether the
    citizen's database is really destroyed."""

    ok: bool = True


@asynccontextmanager
async def _best_effort(label: str, **context: str) -> AsyncIterator[_StepOutcome]:
    """Run one teardown step, swallowing nothing silently: an already-gone object is logged
    at debug (that IS the idempotent outcome), anything else at warning with its SQLSTATE
    so an operator can tell a transient lock from a real failure."""
    outcome = _StepOutcome()
    try:
        yield outcome
    except DBAPIError as exc:
        code = _sqlstate(exc)
        if code in _ALREADY_GONE:
            # ALREADY GONE IS SUCCESS, not a failure to report: the object the step exists to
            # remove is not there, which is the state the caller asked for.
            _log.debug(f"app_database_{label}_already_gone", sqlstate=code, **context)
            return
        outcome.ok = False
        _log.warning(
            f"app_database_{label}_failed",
            sqlstate=code,
            transient=code in {_OBJECT_IN_USE, _DEPENDENT_OBJECTS},
            **context,
        )
    except Exception:
        outcome.ok = False
        _log.exception(f"app_database_{label}_failed", **context)


def _sqlstate(exc: DBAPIError) -> str | None:
    code = getattr(exc.orig, "sqlstate", None)
    return code if isinstance(code, str) else None
