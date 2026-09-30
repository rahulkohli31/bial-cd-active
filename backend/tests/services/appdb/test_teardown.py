"""`sever`, `restore_login`, and `salt_the_earth` — against real databases and roles.

The kill-switch half is only worth anything if it severs a LIVE client, so these tests
hold an open connection as the app role across the sever and prove both halves: the
existing session dies AND a reconnect is refused. Locking the door before kicking anyone
out is the whole reason the order in `sever` is what it is.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from typing import Any

import asyncpg
import pytest
import sqlalchemy as sa
import structlog
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from src.config import settings
from src.services.appdb import teardown as appdb_teardown
from src.services.appdb.engine import reset_maintenance_engine_for_tests
from src.services.appdb.names import database_name, quote_identifier, role_name
from src.services.appdb.provision import control_plane_dsn, ensure_project_database
from src.services.appdb.teardown import restore_login, salt_the_earth, sever
from tests.factories import ProjectFactory, UserFactory
from tests.services.appdb.helpers import control_plane_identity_dsn, execute_on, scalar_on

pytestmark = pytest.mark.app_db

_DATABASE_EXISTS_SQL = "SELECT EXISTS (SELECT 1 FROM pg_database WHERE datname = :db)"
_ROLE_EXISTS_SQL = "SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :role)"
_CAN_LOGIN_SQL = "SELECT rolcanlogin FROM pg_roles WHERE rolname = :role"
_VACUUM_IN_PROGRESS_SQL = (
    "SELECT EXISTS (SELECT 1 FROM pg_stat_progress_vacuum WHERE datname = :db)"
)


async def _catalog(engine: AsyncEngine, sql: str, **params: Any) -> Any:
    async with engine.connect() as conn:
        return await conn.scalar(sa.text(sql), params)


async def _provisioned(db: AsyncSession, salted: list[uuid.UUID]) -> tuple[str, str, str]:
    """A freshly provisioned project; returns (db_name, role_name, app dsn)."""
    user = await UserFactory.create(db, email=f"appdb-{uuid.uuid4().hex}@rvaiglobal.com")
    project = await ProjectFactory.create(db, user.id)
    salted.append(project.id)
    record = await ensure_project_database(db, project.id)
    assert record is not None
    return record.db_name, record.role_name, control_plane_dsn(record)


# --- sever ---------------------------------------------------------------------------


async def test_sever_kills_a_live_session_and_refuses_the_reconnect(
    db_session: AsyncSession, maintenance: AsyncEngine, salted: list[uuid.UUID]
) -> None:
    db_name, role, dsn = await _provisioned(db_session, salted)

    # A live client, mid-flight — the deployed app or a relaunched preview, neither of
    # which holds a build lock, so nothing else in the system can stop them.
    live = create_async_engine(dsn, poolclass=NullPool)
    try:
        async with live.connect() as connection:
            assert await connection.scalar(sa.text("SELECT 1")) == 1

            assert await sever(db_name=db_name, role_name=role) is True

            # The in-flight session is gone...
            with pytest.raises(DBAPIError):
                await connection.scalar(sa.text("SELECT 1"))
    finally:
        await live.dispose()

    # ...and the reconnect a pool would immediately attempt finds the door locked. Order
    # matters: had the terminate run FIRST, this reconnect would have succeeded.
    with pytest.raises(asyncpg.InvalidAuthorizationSpecificationError):
        await scalar_on(dsn, "SELECT 1")
    assert await _catalog(maintenance, _CAN_LOGIN_SQL, role=role) is False


async def test_sever_evicts_the_app_and_leaves_a_session_it_may_not_close(
    db_session: AsyncSession, maintenance: AsyncEngine, salted: list[uuid.UUID]
) -> None:
    # Postgres refuses to terminate a session of a role the maintenance role has no privileges
    # of, and one refusal fails the whole terminate. The everyday case is an autovacuum worker,
    # which cannot be summoned on demand; the control plane's own session stands in for it.
    db_name, role, dsn = await _provisioned(db_session, salted)
    outsider_role = make_url(settings.DATABASE_URL.get_secret_value()).username or ""
    async with maintenance.connect() as conn:
        await conn.execute(
            sa.text(
                f"GRANT CONNECT ON DATABASE {quote_identifier(db_name)} "
                f"TO {quote_identifier(outsider_role)}"
            )
        )
    outsider = create_async_engine(control_plane_identity_dsn(db_name), poolclass=NullPool)
    live = create_async_engine(dsn, poolclass=NullPool)
    try:
        async with outsider.connect() as other:
            assert await other.scalar(sa.text("SELECT 1")) == 1
            async with live.connect() as app_session:
                assert await app_session.scalar(sa.text("SELECT 1")) == 1

                assert await sever(db_name=db_name, role_name=role) is True

                with pytest.raises(DBAPIError):
                    await app_session.scalar(sa.text("SELECT 1"))
            assert await other.scalar(sa.text("SELECT 1")) == 1
    finally:
        await live.dispose()
        await outsider.dispose()


async def test_restore_login_reopens_the_door(
    db_session: AsyncSession, maintenance: AsyncEngine, salted: list[uuid.UUID]
) -> None:
    db_name, role, dsn = await _provisioned(db_session, salted)
    await sever(db_name=db_name, role_name=role)

    assert await restore_login(db_name=db_name, role_name=role) is True

    assert await _catalog(maintenance, _CAN_LOGIN_SQL, role=role) is True
    assert await scalar_on(dsn, "SELECT 1") == 1


async def test_sever_raises_when_the_role_does_not_exist(
    db_session: AsyncSession, salted: list[uuid.UUID]
) -> None:
    # `sever` is the kill-switch primitive: it must NOT report success on a database it
    # never touched, because an operator reads a clean return as "the app is locked out".
    del db_session, salted
    orphan = uuid.uuid4()
    with pytest.raises(DBAPIError):
        await sever(db_name=database_name(orphan), role_name=role_name(orphan))


async def test_sever_no_ops_when_the_substrate_is_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await reset_maintenance_engine_for_tests()
    monkeypatch.setattr(settings, "app_db", None)
    try:
        assert await sever(db_name="bialapp_x", role_name="bialrole_x") is False
        assert await restore_login(db_name="bialapp_x", role_name="bialrole_x") is False
    finally:
        await reset_maintenance_engine_for_tests()


# --- salt_the_earth -------------------------------------------------------------------


async def test_salt_the_earth_drops_the_database_and_the_role(
    db_session: AsyncSession, maintenance: AsyncEngine, salted: list[uuid.UUID]
) -> None:
    db_name, role, _dsn = await _provisioned(db_session, salted)
    assert await _catalog(maintenance, _DATABASE_EXISTS_SQL, db=db_name) is True

    await salt_the_earth(db_name=db_name, role_name=role)

    assert await _catalog(maintenance, _DATABASE_EXISTS_SQL, db=db_name) is False
    assert await _catalog(maintenance, _ROLE_EXISTS_SQL, role=role) is False


async def test_salt_the_earth_forces_out_a_live_connection(
    db_session: AsyncSession, maintenance: AsyncEngine, salted: list[uuid.UUID]
) -> None:
    # The teardown's own eviction is the guarantee, not the delete-time build-session guard: a
    # relaunched preview holds no lock and the deployed container has no interlock.
    db_name, role, dsn = await _provisioned(db_session, salted)
    live = create_async_engine(dsn, poolclass=NullPool)
    try:
        async with live.connect() as connection:
            assert await connection.scalar(sa.text("SELECT 1")) == 1
            await salt_the_earth(db_name=db_name, role_name=role)
    except DBAPIError:
        pass  # the forced-out connection may fail on the way out; that is the point
    finally:
        await live.dispose()

    assert await _catalog(maintenance, _DATABASE_EXISTS_SQL, db=db_name) is False


async def test_a_session_that_slips_in_after_the_sever_is_evicted_before_the_drop(
    db_session: AsyncSession,
    maintenance: AsyncEngine,
    salted: list[uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    db_name, role, dsn = await _provisioned(db_session, salted)
    real_terminate = appdb_teardown._terminate_backends
    calls = 0

    async def misses_the_first_time(conn: Any, *, db_name: str) -> int:
        nonlocal calls
        calls += 1
        return 0 if calls == 1 else await real_terminate(conn, db_name=db_name)

    monkeypatch.setattr(appdb_teardown, "_terminate_backends", misses_the_first_time)
    live = create_async_engine(dsn, poolclass=NullPool)
    try:
        async with live.connect() as connection:
            assert await connection.scalar(sa.text("SELECT 1")) == 1
            assert await salt_the_earth(db_name=db_name, role_name=role) is True
    except DBAPIError:
        pass  # the evicted connection may fail on the way out
    finally:
        await live.dispose()

    assert await _catalog(maintenance, _DATABASE_EXISTS_SQL, db=db_name) is False


@pytest.mark.autovacuum
async def test_salt_the_earth_drops_a_database_autovacuum_is_working_in(
    db_session: AsyncSession, maintenance: AsyncEngine, salted: list[uuid.UUID]
) -> None:
    db_name, role, dsn = await _provisioned(db_session, salted)
    # Enough dead rows that the worker is still vacuuming when the teardown runs.
    await execute_on(dsn, "CREATE TABLE dead_rows (id int)")
    await execute_on(dsn, "INSERT INTO dead_rows SELECT generate_series(1, 2000000)")
    await execute_on(dsn, "DELETE FROM dead_rows")

    deadline = time.monotonic() + 150
    while not await _catalog(maintenance, _VACUUM_IN_PROGRESS_SQL, db=db_name):
        assert time.monotonic() < deadline, "autovacuum never reached the database"
        await asyncio.sleep(0.05)

    assert await salt_the_earth(db_name=db_name, role_name=role) is True
    assert await _catalog(maintenance, _DATABASE_EXISTS_SQL, db=db_name) is False


async def test_re_running_salt_the_earth_is_a_silent_no_op(
    db_session: AsyncSession, salted: list[uuid.UUID]
) -> None:
    db_name, role, _dsn = await _provisioned(db_session, salted)
    await salt_the_earth(db_name=db_name, role_name=role)

    # Idempotent: "already gone" is classified by SQLSTATE (3D000 / 42704) as the expected
    # outcome, so a retry must not log a warning an operator would chase.
    with structlog.testing.capture_logs() as captured:
        await salt_the_earth(db_name=db_name, role_name=role)
    assert [event for event in captured if event.get("log_level") == "warning"] == []


async def test_a_failing_drop_is_logged_and_never_raised(
    db_session: AsyncSession,
    maintenance: AsyncEngine,
    salted: list[uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Post-commit best-effort contract: the registry row is already gone by the time this
    # runs, so raising here would explode an endpoint that has already committed. The
    # failure becomes an orphan for the reconciler, and it is LOGGED — never swallowed.
    db_name, role, _dsn = await _provisioned(db_session, salted)

    async def explode(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("simulated DROP DATABASE failure")

    monkeypatch.setattr(appdb_teardown, "_drop_database", explode)
    with structlog.testing.capture_logs() as captured:
        await salt_the_earth(db_name=db_name, role_name=role)

    assert any(event.get("event") == "app_database_drop_database_failed" for event in captured)
    # ...and the database really is still there, so the reconciler has something to find.
    assert await _catalog(maintenance, _DATABASE_EXISTS_SQL, db=db_name) is True


async def test_salt_the_earth_no_ops_when_the_substrate_is_unconfigured(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    await reset_maintenance_engine_for_tests()
    monkeypatch.setattr(settings, "app_db", None)
    try:
        await salt_the_earth(db_name="bialapp_x", role_name="bialrole_x")
    finally:
        await reset_maintenance_engine_for_tests()
