"""`ensure_project_database` — the real thing, against the real cluster.

Nothing here is faked: the tests create actual databases and roles through the maintenance
engine and then interrogate the PostgreSQL catalogs (and open real connections) to prove
the wall exists. A fake would only prove that the fake agrees with itself, and the whole
point of this unit is a STRUCTURAL guarantee — it has to be checked structurally.
"""

from __future__ import annotations

import asyncio
import datetime
import traceback
import uuid
from typing import Any
from urllib.parse import parse_qsl, urlsplit

import asyncpg
import pytest
import sqlalchemy as sa
import structlog
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from src.config import settings
from src.db.models.project_database import ProjectDatabase
from src.services.appdb import provision as provision_module
from src.services.appdb.engine import reset_maintenance_engine_for_tests
from src.services.appdb.errors import AppDatabaseError
from src.services.appdb.names import database_name, role_name
from src.services.appdb.provision import (
    control_plane_dsn,
    ensure_project_database,
    sandbox_dsn,
)
from src.services.appdb.secrets import decrypt_password, encrypt_password
from tests.factories import ProjectFactory, UserFactory
from tests.services.appdb.helpers import (
    control_plane_identity_dsn,
    execute_on,
    scalar_on,
    unpersisted_record,
)

_OWNER_SQL = "SELECT pg_get_userbyid(datdba) FROM pg_database WHERE datname = :db"
# `grantee = 0` is PUBLIC in an exploded ACL — the precise question is "does the
# pseudo-role PUBLIC still hold CONNECT on this database", not "is the ACL non-empty".
# A freshly created database has a NULL `datacl` meaning "the default ACL", which DOES
# grant PUBLIC CONNECT — so the COALESCE onto `acldefault` is what makes the pre-wall
# state read as open instead of silently reading as closed.
_PUBLIC_CONNECT_SQL = """
SELECT EXISTS (
    SELECT 1 FROM pg_database d,
         aclexplode(COALESCE(d.datacl, acldefault('d', d.datdba))) a
    WHERE d.datname = :db AND a.grantee = 0 AND a.privilege_type = 'CONNECT'
)
"""
_ROLE_SETTINGS_SQL = """
SELECT s.setconfig
FROM pg_db_role_setting s
JOIN pg_database d ON d.oid = s.setdatabase
JOIN pg_roles r ON r.oid = s.setrole
WHERE d.datname = :db AND r.rolname = :role
"""
_COMMENT_SQL = "SELECT shobj_description(oid, 'pg_database') FROM pg_database WHERE datname = :db"
_ROLE_FLAGS_SQL = (
    "SELECT rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, rolbypassrls "
    "FROM pg_roles WHERE rolname = :role"
)


async def _catalog(engine: AsyncEngine, sql: str, **params: Any) -> Any:
    async with engine.connect() as conn:
        return await conn.scalar(sa.text(sql), params)


async def _new_project(db: AsyncSession) -> uuid.UUID:
    user = await UserFactory.create(db, email=f"appdb-{uuid.uuid4().hex}@rvaiglobal.com")
    project = await ProjectFactory.create(db, user.id)
    return project.id


# --- happy path ---------------------------------------------------------------------


async def test_provision_creates_the_role_and_its_database(
    db_session: AsyncSession, maintenance: AsyncEngine, salted: list[uuid.UUID]
) -> None:
    project_id = await _new_project(db_session)
    salted.append(project_id)

    record = await ensure_project_database(db_session, project_id)

    assert record is not None
    assert record.db_name == database_name(project_id)
    assert record.role_name == role_name(project_id)
    # The role owns its own database — full DDL inside its box, zero cluster reach.
    assert await _catalog(maintenance, _OWNER_SQL, db=record.db_name) == record.role_name


async def test_provision_revokes_public_connect_and_grants_only_the_app_role(
    db_session: AsyncSession, maintenance: AsyncEngine, salted: list[uuid.UUID]
) -> None:
    project_id = await _new_project(db_session)
    salted.append(project_id)
    record = await ensure_project_database(db_session, project_id)
    assert record is not None

    # THE cross-app wall.
    assert await _catalog(maintenance, _PUBLIC_CONNECT_SQL, db=record.db_name) is False
    assert (
        await _catalog(
            maintenance,
            "SELECT has_database_privilege(:role, :db, 'CONNECT')",
            role=record.role_name,
            db=record.db_name,
        )
        is True
    )


async def test_provision_applies_both_per_database_timeouts(
    db_session: AsyncSession, maintenance: AsyncEngine, salted: list[uuid.UUID]
) -> None:
    project_id = await _new_project(db_session)
    salted.append(project_id)
    record = await ensure_project_database(db_session, project_id)
    assert record is not None
    assert settings.app_db is not None

    setconfig = await _catalog(
        maintenance, _ROLE_SETTINGS_SQL, db=record.db_name, role=record.role_name
    )
    assert f"statement_timeout={settings.app_db.statement_timeout_ms}ms" in setconfig
    assert (
        f"idle_in_transaction_session_timeout={settings.app_db.idle_in_transaction_timeout_ms}ms"
        in setconfig
    )


async def test_provision_stamps_a_parseable_iso_timestamp_on_the_database_comment(
    db_session: AsyncSession, maintenance: AsyncEngine, salted: list[uuid.UUID]
) -> None:
    # `pg_database` carries no creation timestamp and `pg_stat_file` needs a superuser
    # Azure will not grant, so this COMMENT is the orphan reconciler's ONLY age source —
    # and it has to survive being read back by a machine, not just look right.
    project_id = await _new_project(db_session)
    salted.append(project_id)
    record = await ensure_project_database(db_session, project_id)
    assert record is not None

    stamp = await _catalog(maintenance, _COMMENT_SQL, db=record.db_name)
    parsed = datetime.datetime.fromisoformat(stamp)
    assert parsed.tzinfo is not None
    assert abs((datetime.datetime.now(datetime.UTC) - parsed).total_seconds()) < 300


async def test_markers_reflect_reality_with_db_ready_committed_last(
    db_session: AsyncSession, salted: list[uuid.UUID]
) -> None:
    project_id = await _new_project(db_session)
    salted.append(project_id)
    record = await ensure_project_database(db_session, project_id)

    assert record is not None
    assert record.db_ready is True
    assert record.provisioned_at is not None
    # Exactly one registry row, and the stored names are what was actually created.
    rows = (
        (
            await db_session.execute(
                sa.select(ProjectDatabase).where(ProjectDatabase.project_id == project_id)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 1


async def test_provision_is_idempotent_and_reuses_the_same_row(
    db_session: AsyncSession, salted: list[uuid.UUID]
) -> None:
    project_id = await _new_project(db_session)
    salted.append(project_id)

    first = await ensure_project_database(db_session, project_id)
    second = await ensure_project_database(db_session, project_id)

    assert first is not None and second is not None
    assert first.id == second.id
    assert first.password_encrypted == second.password_encrypted


async def test_provisioning_never_logs_the_password_or_a_dsn(
    db_session: AsyncSession, salted: list[uuid.UUID]
) -> None:
    # The audit-shaped record carries role/database NAMES only. A DSN or a password in a
    # log line is the same leak as one in an audit `detail`.
    project_id = await _new_project(db_session)
    salted.append(project_id)

    with structlog.testing.capture_logs() as captured:
        record = await ensure_project_database(db_session, project_id)

    assert record is not None
    password = decrypt_password(record.password_encrypted)
    rendered = repr(captured) + repr(record)
    assert password not in rendered
    assert "postgresql" not in rendered
    # ...but the handles an operator needs ARE there.
    assert any(event.get("event") == "app_database_provisioned" for event in captured)


async def test_a_failing_role_ddl_never_carries_the_password_in_its_error(
    db_session: AsyncSession, salted: list[uuid.UUID], monkeypatch: pytest.MonkeyPatch
) -> None:
    # `CREATE ROLE ... PASSWORD '<literal>'` is DDL, so the password is part of the
    # STATEMENT TEXT rather than a bind parameter — and SQLAlchemy appends `[SQL: ...]` to
    # every DBAPIError. The raw exception is therefore itself a credential, and this path
    # propagates all the way out of a build start, where it WOULD be logged.
    project_id = await _new_project(db_session)
    salted.append(project_id)

    # Force a non-duplicate failure by corrupting the attribute clause, so the real
    # `CREATE ROLE` runs and the server rejects it with the password still in the text.
    monkeypatch.setattr(provision_module, "_ROLE_ATTRIBUTES", "LOGIN NOSUCHATTRIBUTE")

    with pytest.raises(AppDatabaseError) as failure:
        await ensure_project_database(db_session, project_id)

    row = (
        await db_session.execute(
            sa.select(ProjectDatabase).where(ProjectDatabase.project_id == project_id)
        )
    ).scalar_one()
    password = decrypt_password(row.password_encrypted)

    rendered = f"{failure.value}{failure.value!r}{traceback.format_exception(failure.value)}"
    assert password not in rendered
    assert "PASSWORD" not in rendered
    # The diagnostic an operator actually acts on survives.
    assert "sqlstate=" in str(failure.value)
    # And the claim stays non-terminal, so the next ensure retries.
    assert row.db_ready is False


# --- edge: nothing configured -------------------------------------------------------


async def test_unconfigured_substrate_is_a_clean_no_op(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A project with no database is a SUPPORTED deployment — the app simply has no
    # persistence — so this must not raise and must not claim a database that isn't there.
    await reset_maintenance_engine_for_tests()
    monkeypatch.setattr(settings, "app_db", None)
    try:
        project_id = await _new_project(db_session)

        assert await ensure_project_database(db_session, project_id) is None

        claimed = await db_session.scalar(
            sa.select(sa.func.count())
            .select_from(ProjectDatabase)
            .where(ProjectDatabase.project_id == project_id)
        )
        assert claimed == 0
    finally:
        await reset_maintenance_engine_for_tests()


# --- edge: two racers ----------------------------------------------------------------


async def test_concurrent_provision_runs_the_external_sequence_exactly_once(
    test_engine: AsyncEngine,
    committed_project: uuid.UUID,
    salted: list[uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    salted.append(committed_project)
    calls = 0
    original = provision_module._create_role

    async def counting(*args: Any, **kwargs: Any) -> None:
        nonlocal calls
        calls += 1
        await original(*args, **kwargs)

    monkeypatch.setattr(provision_module, "_create_role", counting)

    async def racer() -> ProjectDatabase | None:
        # Independent sessions: one AsyncSession cannot be shared across concurrent tasks,
        # and the claim has to serialize ACROSS transactions to be worth anything.
        async with AsyncSession(test_engine, expire_on_commit=False) as session:
            return await ensure_project_database(session, committed_project)

    first, second = await asyncio.gather(racer(), racer())

    # The unique constraint elected one racer; the loser converged on the marker.
    assert calls == 1
    assert first is not None and second is not None
    assert first.db_ready is True
    assert second.db_ready is True
    assert first.id == second.id


# --- error: a crash in the middle of the external sequence ---------------------------


async def test_a_crash_before_the_wall_leaves_a_non_terminal_marker_and_self_heals(
    db_session: AsyncSession,
    maintenance: AsyncEngine,
    salted: list[uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_id = await _new_project(db_session)
    salted.append(project_id)

    class _MidSequenceCrashError(RuntimeError):
        pass

    async def explode(*args: Any, **kwargs: Any) -> None:
        raise _MidSequenceCrashError("simulated crash after CREATE DATABASE, before the wall")

    monkeypatch.setattr(provision_module, "_raise_the_wall", explode)
    with pytest.raises(_MidSequenceCrashError):
        await ensure_project_database(db_session, project_id)

    # The database exists but PUBLIC still holds CONNECT — the wall is DOWN — so the
    # marker must NOT claim readiness. This is the whole reason `db_ready` is written last.
    db_name = database_name(project_id)
    assert await _catalog(maintenance, _OWNER_SQL, db=db_name) == role_name(project_id)
    assert await _catalog(maintenance, _PUBLIC_CONNECT_SQL, db=db_name) is True
    stranded = (
        await db_session.execute(
            sa.select(ProjectDatabase).where(ProjectDatabase.project_id == project_id)
        )
    ).scalar_one()
    assert stranded.db_ready is False
    assert stranded.provisioned_at is None

    monkeypatch.undo()
    record = await ensure_project_database(db_session, project_id)

    assert record is not None
    assert record.db_ready is True
    # ...and the wall is now genuinely up: an identity that is not the app role (nor a
    # member of it) is refused at connect time, not at query time.
    assert await _catalog(maintenance, _PUBLIC_CONNECT_SQL, db=db_name) is False
    with pytest.raises(asyncpg.InsufficientPrivilegeError) as refused:
        await scalar_on(control_plane_identity_dsn(db_name), "SELECT 1")
    assert "permission denied for database" in str(refused.value)


# --- integration: the structural guarantees -------------------------------------------


async def test_role_a_cannot_connect_to_project_b_database(
    db_session: AsyncSession, salted: list[uuid.UUID]
) -> None:
    # THE test of this unit. It is the successor to the retired `WHERE app_id` predicate:
    # isolation is now a grant the platform makes, not a filter the generated app has to
    # remember to write, so one app's runtime cannot reach another's data even if its code
    # asks for it by name.
    project_a = await _new_project(db_session)
    project_b = await _new_project(db_session)
    salted.extend([project_a, project_b])

    record_a = await ensure_project_database(db_session, project_a)
    record_b = await ensure_project_database(db_session, project_b)
    assert record_a is not None and record_b is not None

    # A reaches its own database.
    assert await scalar_on(control_plane_dsn(record_a), "SELECT 1") == 1

    # A pointed at B's database is refused BEFORE a single query runs: asyncpg raises at
    # CONNECT time, which is why this is structural rather than a predicate anyone can
    # forget to write.
    crossing = control_plane_dsn(record_a).replace(record_a.db_name, record_b.db_name)
    with pytest.raises(asyncpg.InsufficientPrivilegeError) as refused:
        await scalar_on(crossing, "SELECT 1")
    assert "permission denied for database" in str(refused.value)


async def test_a_session_as_the_app_role_carries_the_configured_statement_timeout(
    db_session: AsyncSession, salted: list[uuid.UUID]
) -> None:
    project_id = await _new_project(db_session)
    salted.append(project_id)
    record = await ensure_project_database(db_session, project_id)
    assert record is not None
    assert settings.app_db is not None

    # `SHOW` renders the value in whatever unit PostgreSQL prefers ('30s'), so compare on
    # `pg_settings.setting`, which is always the raw millisecond count.
    applied = await scalar_on(
        control_plane_dsn(record),
        "SELECT setting FROM pg_settings WHERE name = 'statement_timeout'",
    )
    assert int(applied) == settings.app_db.statement_timeout_ms


async def test_the_app_role_owns_its_box_but_has_no_cluster_reach(
    db_session: AsyncSession, maintenance: AsyncEngine, salted: list[uuid.UUID]
) -> None:
    project_id = await _new_project(db_session)
    salted.append(project_id)
    record = await ensure_project_database(db_session, project_id)
    assert record is not None
    dsn = control_plane_dsn(record)

    # Inside its own database it is the owner: Drizzle-authored DDL just works.
    await execute_on(dsn, "CREATE TABLE widgets (id integer primary key)")
    assert await scalar_on(dsn, "SELECT to_regclass('widgets') IS NOT NULL") is True

    # Outside it, nothing. NOCREATEROLE especially is kill-switch completeness, not
    # hygiene: it is what stops a second LOGIN role being minted as a re-entry path that
    # NOLOGIN on the primary would never cover.
    async with maintenance.connect() as conn:
        flags = (await conn.execute(sa.text(_ROLE_FLAGS_SQL), {"role": record.role_name})).one()
    assert flags.rolcanlogin is True
    assert flags.rolsuper is False
    assert flags.rolcreatedb is False
    assert flags.rolcreaterole is False
    assert flags.rolbypassrls is False

    with pytest.raises(DBAPIError):
        await execute_on(dsn, "CREATE DATABASE should_not_exist_bial")
    with pytest.raises(DBAPIError):
        await execute_on(dsn, "CREATE ROLE should_not_exist_bial LOGIN")


# --- DSN assembly ---------------------------------------------------------------------


def _record_for_dsn() -> ProjectDatabase:
    project_id = uuid.uuid4()
    return unpersisted_record(
        project_id=project_id,
        db_name=database_name(project_id),
        role_name=role_name(project_id),
        password_encrypted=encrypt_password("tok3n-with_url-safe-chars"),
    )


def test_control_plane_dsn_keeps_the_asyncpg_driver_and_embeds_the_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert settings.app_db is not None
    record = _record_for_dsn()
    dsn = control_plane_dsn(record)
    assert dsn.startswith("postgresql+asyncpg://")
    assert f"{record.role_name}:tok3n-with_url-safe-chars@" in dsn
    assert dsn.endswith(f"/{record.db_name}")


def test_sandbox_dsn_drops_the_driver_prefix_and_renames_the_tls_parameter(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # node-postgres (and Drizzle on it) cannot parse `postgresql+asyncpg://` — that is a
    # SQLAlchemy driver selector, not a URL scheme — and reads `sslmode=`, not asyncpg's
    # `ssl=`. Carrying either through unchanged is a silent failure in the generated app.
    assert settings.app_db is not None
    monkeypatch.setattr(
        settings.app_db,
        "maintenance_dsn",
        type(settings.app_db.maintenance_dsn)(
            "postgresql+asyncpg://maint:pw@db.internal:5432/postgres?ssl=require"
        ),
    )
    dsn = sandbox_dsn(_record_for_dsn())
    assert dsn.startswith("postgresql://")
    assert "+asyncpg" not in dsn
    query = dict(parse_qsl(urlsplit(dsn).query))
    assert query == {"sslmode": "require"}


def test_sandbox_dsn_honours_the_sandbox_visible_host_override(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The control plane's own `localhost` is the sandbox container's OWN localhost.
    assert settings.app_db is not None
    monkeypatch.setattr(settings.app_db, "sandbox_dsn_host", "host.docker.internal:6543")
    dsn = sandbox_dsn(_record_for_dsn())
    assert "@host.docker.internal:6543/" in dsn
    assert "localhost" not in dsn
