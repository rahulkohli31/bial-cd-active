"""Provision a project's own PostgreSQL database + login role, idempotently and exactly once.

Two phases, deliberately NOT atomic (nothing can make cluster DDL and a table row commit
together):

1. **Claim** — `INSERT ... ON CONFLICT DO NOTHING RETURNING` against the unique
   `project_databases.project_id`. Exactly one racer gets a row back and runs phase 2; the
   losers converge on `db_ready`. The claim commits immediately with `db_ready = false`, so
   the winner never holds a row lock across the external sequence.
2. **External sequence** — on the AUTOCOMMIT maintenance connection, then the terminal
   `db_ready` marker in its own LAST commit.

The order inside phase 2 is load-bearing. The wall (`REVOKE CONNECT ... FROM PUBLIC`) goes
up immediately after the database exists, and `db_ready` is only written once every step
has succeeded — so a crash anywhere in the middle reads as not-ready and the next ensure
re-runs the WHOLE sequence. That is safe because every step tolerates its duplicate-object
SQLSTATE (`42P04` duplicate_database, `42710` duplicate_object) and re-derives the same
names from the project id (`names.py`), which is what makes "run it again" the recovery.

When `APP_DB__*` is unconfigured the whole thing no-ops and returns `None`: a project
without a database is a supported deployment (the generated app simply has no persistence),
exactly the posture `provision_app_storage` takes when object storage is off. This
DIVERGES from `get_storage()`, which raises — see `engine.get_maintenance_engine`.
"""

from __future__ import annotations

import asyncio
import datetime
import secrets
import uuid
from typing import Final

import sqlalchemy as sa
import structlog
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import URL, make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from src.db.models.project_database import ProjectDatabase
from src.services.appdb.config import AppDatabaseSettings
from src.services.appdb.engine import get_maintenance_engine
from src.services.appdb.errors import AppDatabaseError, AppDatabaseUnconfiguredError
from src.services.appdb.names import (
    database_name,
    quote_identifier,
    quote_password_literal,
    role_name,
)
from src.services.appdb.secrets import decrypt_password, encrypt_password

_log = structlog.get_logger()

# 32 bytes of entropy -> a 43-char url-safe token (ADR-0006: secrets, never a UUID).
_PASSWORD_BYTES: Final = 32

# The two SQLSTATEs that mean "this step already happened" — the idempotency signal.
_DUPLICATE_DATABASE: Final = "42P04"
_DUPLICATE_OBJECT: Final = "42710"

# How long a racer that LOST the claim waits for the winner to publish `db_ready` before
# concluding the winner died mid-sequence and re-running it itself. Short: the external
# sequence is ~6 statements against a local socket. Overshooting merely re-runs an
# idempotent sequence; undershooting would never self-heal a crashed provision.
_CONVERGE_ATTEMPTS: Final = 40
_CONVERGE_DELAY_SECONDS: Final = 0.05


async def ensure_project_database(
    db: AsyncSession, project_id: uuid.UUID
) -> ProjectDatabase | None:
    """Ensure `project_id` has a ready database + role; return its registry row.

    Idempotent and safe to call from every arm that might need one (project create, build
    start, a later self-heal). Returns `None` — with no row written and nothing raised —
    when no substrate is configured.

    The caller owns nothing transactional here: this function commits its own claim and its
    own terminal marker, because both must be durable independently of whatever the caller
    is doing. A genuine substrate error PROPAGATES (fail-first); the registry is left
    non-terminal so the next ensure retries.
    """
    engine = get_maintenance_engine()
    if engine is None:
        _log.debug("app_database_substrate_unconfigured", project_id=str(project_id))
        return None

    # Fast path — the steady state. This function is called on EVERY build start / relaunch,
    # and the overwhelmingly common answer is "already provisioned". One read short-circuits
    # the `_claim` INSERT ... ON CONFLICT + COMMIT and the `_await_ready` converge sleep that
    # an already-ready row would otherwise pay on every call. A not-ready-or-absent row falls
    # through to the claim/converge path UNCHANGED, so the race semantics are untouched.
    fast = await _row_for(db, project_id)
    if fast is not None and fast.db_ready:
        return fast

    if not await _claim(db, project_id):
        # Someone else holds the claim (or holds it from a previous run). Give them the
        # bounded convergence window before concluding anything.
        converged = await _await_ready(db, project_id)
        if converged is not None:
            return converged
        # Nobody published `db_ready` inside the window: the previous attempt died
        # mid-sequence (or crashed before starting). Re-run it — that is the self-heal,
        # and it is safe because every step below tolerates its duplicate-object SQLSTATE.
        _log.warning("app_database_claim_stale_reprovisioning", project_id=str(project_id))

    row = await _row_for(db, project_id)
    if row is None:
        # The project was deleted between the claim and the read (ON DELETE CASCADE).
        return None
    if row.db_ready:
        return row

    # Plain scalars ACROSS the commit boundary that follows — never touch the ORM
    # attributes after a commit for values the DDL needs
    # (docs/solutions/design-patterns/prefer-returning-over-refresh-across-commit).
    db_name = row.db_name
    role = row.role_name
    password = decrypt_password(row.password_encrypted)

    async with engine.connect() as conn:
        await _create_role(conn, role=role, password=password)
        # BEFORE `CREATE DATABASE`, not after: since PG16 a CREATEROLE maintenance role does
        # NOT automatically get SET/INHERIT on the roles it creates, and `CREATE DATABASE
        # ... OWNER <role>` requires the ability to SET ROLE to that owner. This grant is
        # also the INHERIT membership that later authorizes `pg_terminate_backend` and the
        # drops without any `SET ROLE` gymnastics.
        await _grant_membership(conn, role=role)
        await _create_database(conn, db_name=db_name, role=role)
        await _raise_the_wall(conn, db_name=db_name, role=role)
        await _apply_timeouts(conn, db_name=db_name, role=role)
        await _stamp_provisioned_at(conn, db_name=db_name)

    ready = await _publish_ready(db, project_id)
    _log.info(
        "app_database_provisioned",
        project_id=str(project_id),
        db_name=db_name,
        role_name=role,
    )
    return ready


# --- phase 1: the claim ------------------------------------------------------------


async def _claim(db: AsyncSession, project_id: uuid.UUID) -> bool:
    """Insert the claim row if absent; True iff THIS caller created it.

    The unique constraint on `project_id` is the election. A loser's INSERT briefly blocks
    on the index and then returns no row, which is the signal to converge rather than
    provision. Committed immediately so no row lock is held across the external sequence.
    """
    password = secrets.token_urlsafe(_PASSWORD_BYTES)
    stmt = (
        pg_insert(ProjectDatabase)
        .values(
            project_id=project_id,
            db_name=database_name(project_id),
            role_name=role_name(project_id),
            password_encrypted=encrypt_password(password),
        )
        .on_conflict_do_nothing(constraint="uq_project_databases_project")
        .returning(ProjectDatabase.id)
    )
    claimed = await db.scalar(stmt)
    await db.commit()
    return claimed is not None


async def _row_for(db: AsyncSession, project_id: uuid.UUID) -> ProjectDatabase | None:
    # `populate_existing` refreshes the identity-mapped instance: the session may already
    # hold a stale copy from before another statement (or another racer) moved it on.
    stmt = (
        sa.select(ProjectDatabase)
        .where(ProjectDatabase.project_id == project_id)
        .execution_options(populate_existing=True)
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def _await_ready(db: AsyncSession, project_id: uuid.UUID) -> ProjectDatabase | None:
    """Poll for the claim winner's terminal marker; `None` if the window expires.

    Re-reading inside the caller's open transaction is enough: PostgreSQL's default READ
    COMMITTED takes a fresh snapshot per STATEMENT, so each round sees the winner's commit.
    This deliberately never calls `db.rollback()` — `get_db` owns the caller's transaction,
    and discarding its uncommitted work to poll would be a spectacular side effect.
    """
    for _ in range(_CONVERGE_ATTEMPTS):
        # Check THEN sleep: the winner often published `db_ready` before this racer began
        # polling, so reading first returns on attempt one instead of after a wasted 50ms
        # sleep. Same attempt count and same window — only the leading sleep is removed.
        row = await _row_for(db, project_id)
        if row is not None and row.db_ready:
            return row
        if row is None:
            return None
        await asyncio.sleep(_CONVERGE_DELAY_SECONDS)
    return None


async def _publish_ready(db: AsyncSession, project_id: uuid.UUID) -> ProjectDatabase | None:
    """Flip the TERMINAL marker — the last thing that happens, after every external step."""
    await db.execute(
        sa.update(ProjectDatabase)
        .where(ProjectDatabase.project_id == project_id)
        .values(db_ready=True, provisioned_at=sa.func.now())
    )
    await db.commit()
    return await _row_for(db, project_id)


# --- phase 2: the external sequence -------------------------------------------------


# The role's full attribute set at birth. `NOCREATEROLE` is kill-switch COMPLETENESS, not
# hygiene: it is what stops the agent minting a second LOGIN role as a re-entry path that
# `NOLOGIN` on the primary would never cover. `NOBYPASSRLS` is inert for an owner (owners
# bypass their own tables' RLS regardless) and is kept as harmless defence in depth.
_ROLE_ATTRIBUTES: Final = "LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOBYPASSRLS"
# The converge subset. `NOSUPERUSER`/`NOBYPASSRLS` are omitted deliberately: PostgreSQL
# lets only a SUPERUSER *alter* those two attributes even when clearing them, so including
# them would make the recovery path fail for the CREATEDB+CREATEROLE maintenance role. They
# were set correctly at CREATE and only a superuser could have changed them since.
_ROLE_CONVERGE_ATTRIBUTES: Final = "LOGIN NOCREATEDB NOCREATEROLE"


async def _create_role(conn: AsyncConnection, *, role: str, password: str) -> None:
    quoted = quote_identifier(role)
    literal = quote_password_literal(password)
    try:
        await conn.execute(sa.text(f"CREATE ROLE {quoted} {_ROLE_ATTRIBUTES} PASSWORD {literal}"))
    except DBAPIError as exc:
        if _sqlstate(exc) != _DUPLICATE_OBJECT:
            raise _scrubbed_role_failure("CREATE ROLE", exc) from None
        # The role survived a partial previous run (or a sever left it NOLOGIN). Converge
        # it onto the registry's stored password and attributes rather than trusting
        # whatever state it was left in.
        try:
            await conn.execute(
                sa.text(f"ALTER ROLE {quoted} WITH {_ROLE_CONVERGE_ATTRIBUTES} PASSWORD {literal}")
            )
        except DBAPIError as converge_exc:
            raise _scrubbed_role_failure("ALTER ROLE", converge_exc) from None


def _scrubbed_role_failure(step: str, exc: DBAPIError) -> AppDatabaseError:
    """Replace a role-DDL error with one that does not carry the password.

    `CREATE ROLE ... PASSWORD '<literal>'` is DDL, so the password CANNOT be a bind
    parameter — it is part of the statement text. SQLAlchemy's `StatementError.__str__`
    appends `[SQL: <statement>]`, which makes the raised exception *itself* a credential:
    anything that logs it (`_log.exception`, the build-session error path, a 500 handler)
    writes the app role's password to disk. `.claude/rules/security.md`: never log a
    credential value.

    So the original never leaves this function. `from None` drops it from the traceback
    chain entirely rather than merely detaching `__cause__`; the SQLSTATE is the diagnostic
    that survives, and it is the one an operator actually acts on.
    """
    return AppDatabaseError(
        f"{step} failed while provisioning a project database (sqlstate={_sqlstate(exc)})"
    )


async def _grant_membership(conn: AsyncConnection, *, role: str) -> None:
    # `CURRENT_USER` instead of the maintenance role's name: the connected identity IS the
    # maintenance role, so nothing has to parse a name out of the DSN and hand it to DDL.
    # INHERIT is what lets `pg_terminate_backend` and the drops work without `SET ROLE`;
    # SET is what lets `CREATE DATABASE ... OWNER` name this role as owner (PG16+).
    await conn.execute(
        sa.text(f"GRANT {quote_identifier(role)} TO CURRENT_USER WITH INHERIT TRUE, SET TRUE")
    )


async def _create_database(conn: AsyncConnection, *, db_name: str, role: str) -> None:
    # TEMPLATE template0: a pristine database with none of `template1`'s local additions,
    # so nothing a dev installed on this cluster leaks into an app's box.
    statement = (
        f"CREATE DATABASE {quote_identifier(db_name)} "
        f"OWNER {quote_identifier(role)} TEMPLATE template0"
    )
    try:
        await conn.execute(sa.text(statement))
    except DBAPIError as exc:
        if _sqlstate(exc) != _DUPLICATE_DATABASE:
            raise


async def _raise_the_wall(conn: AsyncConnection, *, db_name: str, role: str) -> None:
    """THE cross-app wall: only this project's role may connect to this project's database.

    Structural isolation, replacing the old shared-table `WHERE app_id` predicate — an
    agent that forgets a filter can no longer reach another app's data, because it cannot
    open the connection at all.
    """
    quoted_db = quote_identifier(db_name)
    await conn.execute(sa.text(f"REVOKE CONNECT ON DATABASE {quoted_db} FROM PUBLIC"))
    await conn.execute(
        sa.text(f"GRANT CONNECT ON DATABASE {quoted_db} TO {quote_identifier(role)}")
    )


async def _apply_timeouts(conn: AsyncConnection, *, db_name: str, role: str) -> None:
    app_db = _app_db_settings()
    scope = f"ALTER ROLE {quote_identifier(role)} IN DATABASE {quote_identifier(db_name)} SET"
    # The values are PositiveInt from a validated settings model, so they are integers and
    # cannot carry SQL — but they still go through `int()` at the seam so that stays true.
    await conn.execute(
        sa.text(f"{scope} statement_timeout = '{int(app_db.statement_timeout_ms)}ms'")
    )
    await conn.execute(
        sa.text(
            f"{scope} idle_in_transaction_session_timeout = "
            f"'{int(app_db.idle_in_transaction_timeout_ms)}ms'"
        )
    )


async def _stamp_provisioned_at(conn: AsyncConnection, *, db_name: str) -> None:
    """Write the provision timestamp into the database's COMMENT.

    The ONLY creation-age source that survives orphaning: `pg_database` carries no
    timestamp and `pg_stat_file` needs a superuser Azure will not grant. The orphan
    reconciler reads it back with `shobj_description(oid, 'pg_database')`.
    """
    stamp = datetime.datetime.now(datetime.UTC).isoformat()
    if "'" in stamp or "\\" in stamp:  # pragma: no cover - isoformat cannot produce these
        raise ValueError("refusing to build DDL: generated timestamp is not literal-safe")
    await conn.execute(sa.text(f"COMMENT ON DATABASE {quote_identifier(db_name)} IS '{stamp}'"))


def _sqlstate(exc: DBAPIError) -> str | None:
    """The five-character SQLSTATE, or None. Errors are classified by CODE, never by
    message text — the text is localized and version-dependent."""
    code = getattr(exc.orig, "sqlstate", None)
    return code if isinstance(code, str) else None


# --- DSN assembly -------------------------------------------------------------------


def control_plane_dsn(record: ProjectDatabase) -> str:
    """The app database's DSN in the **`postgresql+asyncpg://`** form — for anything the
    CONTROL PLANE connects with (tests, a future size probe). Never injected into a
    sandbox: see `sandbox_dsn`."""
    return _app_url(record).render_as_string(hide_password=False)


def sandbox_dsn(record: ProjectDatabase) -> str:
    """The same database in the **plain `postgresql://`** form, host-rewritten for the
    sandbox — the value injected as `BIAL_DATABASE_URL`.

    Two divergences from `control_plane_dsn`, both real footguns:

    * **Scheme.** `postgresql+asyncpg://` is a SQLAlchemy driver selector, not a URL scheme.
      node-postgres (and Drizzle on top of it) cannot parse it and fails at connect time
      with an opaque error, so the generated app gets the bare scheme.
    * **TLS parameter name.** asyncpg reads `ssl=`; libpq/node-postgres read `sslmode=`.
      Same vocabulary of values, different key — carrying `ssl=require` through unchanged
      would silently drop TLS on the app's connection.

    The host comes from `app_db.sandbox_dsn_host` when set: the control plane's own
    `localhost` resolves to the sandbox container's OWN localhost, exactly the class of bug
    `SandboxConfig.blob_base_url` exists to solve.
    """
    url = _app_url(record)
    query = {("sslmode" if key == "ssl" else key): value for key, value in url.query.items()}
    host, port = _sandbox_host_port(url)
    return url.set(drivername="postgresql", host=host, port=port, query=query).render_as_string(
        hide_password=False
    )


def _app_url(record: ProjectDatabase) -> URL:
    """The maintenance DSN with the app's identity and database swapped in — so host, port
    and every connection parameter (`ssl=require`, …) are inherited from the one place an
    operator configures them. `render_as_string` percent-encodes the password."""
    return make_url(_app_db_settings().maintenance_dsn.get_secret_value()).set(
        drivername="postgresql+asyncpg",
        username=record.role_name,
        password=decrypt_password(record.password_encrypted),
        database=record.db_name,
    )


def _sandbox_host_port(url: URL) -> tuple[str | None, int | None]:
    override = _app_db_settings().sandbox_dsn_host
    if override is None:
        return url.host, url.port
    host, _, port = override.partition(":")
    return host, int(port) if port else url.port


def _app_db_settings() -> AppDatabaseSettings:
    from src.config import settings  # lazy: avoid an import cycle via src.config

    if settings.app_db is None:
        raise AppDatabaseUnconfiguredError(
            "per-project databases are not configured: set APP_DB__MAINTENANCE_DSN and "
            "APP_DB__ENCRYPTION_KEY"
        )
    return settings.app_db
