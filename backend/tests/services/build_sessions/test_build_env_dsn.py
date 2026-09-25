"""`BIAL_DATABASE_URL` reaches the sandbox on every BIRTH arm.

Two layers, both real: `provision_app_database` on its own (the env-dict shape and the
sandbox-vs-control-plane DSN split), and the manager's three birth sites end-to-end
(fresh provision, restore, relaunch) asserted off the env each container was actually
born with — `FakeSandboxClient.provision_env` / `.restore_env`.

Nothing here fakes the substrate: `.env.test` configures a real `APP_DB__*`, so these
create actual databases and roles. Every test that provisions registers with `salted`
first — the `db_session` rollback cannot undo work done on the AUTOCOMMIT engine.

The birth arms are driven through `ensure_sandbox` + `stop`, which is the pair production
uses — `SessionManager.start` is deleted. A `stop` ends the session the way the Stop button
does: the step-1 snapshot is written, the container is torn down and the registry deleted, so
the NEXT `ensure_sandbox` finds no registry and takes the restore arm off that bundle.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import redis.asyncio as aioredis
import sqlalchemy as sa
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession

from src.config import settings
from src.db.models.project_database import ProjectDatabase
from src.db.models.user import User
from src.services.appdb import provision as provision_module
from src.services.appdb.engine import reset_maintenance_engine_for_tests
from src.services.appdb.names import database_name, role_name
from src.services.appdb.teardown import salt_the_earth
from src.services.build_sessions.appdb_env import provision_app_database
from src.services.build_sessions.manager import SessionManager, app_name_for
from src.services.sandbox.config import SandboxConfig
from src.services.storage import snapshot_key
from tests.factories import ProjectFactory, UserFactory
from tests.fakes import FakeSandboxClient, FakeStorage, detached_work_done

_BASE_ENV = ("BIAL_APP_ID", "BIAL_PORTAL_ORIGIN")


@pytest.fixture(autouse=True)
def _sandbox_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        settings,
        "sandbox",
        SandboxConfig(
            subscription_id="s",
            resource_group="r",
            region="westeurope",
            managed_environment_name="aca-env",
            acr_server="acr.azurecr.io",
            acr_username="acr-user",
            acr_password=SecretStr("acr-pass"),
            image_ref="acr/img:latest",
        ),
    )


@pytest.fixture
async def salted() -> AsyncIterator[list[uuid.UUID]]:
    """Destroy the databases/roles a test provisions. Register BEFORE provisioning — the
    teardown is idempotent and never raises."""
    project_ids: list[uuid.UUID] = []
    try:
        yield project_ids
    finally:
        for project_id in project_ids:
            await salt_the_earth(
                db_name=database_name(project_id), role_name=role_name(project_id)
            )
        await reset_maintenance_engine_for_tests()


async def _mk(db: AsyncSession, email: str) -> tuple[User, uuid.UUID]:
    user = await UserFactory.create(db, email=email)
    project = await ProjectFactory.create(db, user.id)
    return user, project.id


def _assert_is_a_sandbox_dsn(value: str, project_id: uuid.UUID) -> None:
    """The injected value must be the SANDBOX form, not `control_plane_dsn`."""
    # Assert on the SCHEME, not merely on presence: `postgresql+asyncpg://` is a SQLAlchemy
    # driver selector that node-postgres (and Drizzle on top of it) cannot parse at all.
    assert value.startswith("postgresql://")
    assert "+asyncpg" not in value
    assert f"/{database_name(project_id)}" in value
    assert role_name(project_id) in value


# --- the env-dict builder on its own ------------------------------------------------------


async def test_returns_exactly_the_one_database_var(
    db_session: AsyncSession, salted: list[uuid.UUID]
) -> None:
    user, project_id = await _mk(db_session, "u3env1@rvaiglobal.com")
    salted.append(project_id)

    env = await provision_app_database(db_session, project_id)

    assert set(env) == {"BIAL_DATABASE_URL"}
    _assert_is_a_sandbox_dsn(env["BIAL_DATABASE_URL"], project_id)


async def test_returns_empty_when_the_substrate_is_unconfigured(
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A deployment with no APP_DB__* is supported: the merge is a no-op and the app
    # simply has no persistence. The cached engine must be dropped BOTH sides —
    # `get_maintenance_engine` memoizes, so a stale engine would keep the feature on.
    await reset_maintenance_engine_for_tests()
    monkeypatch.setattr(settings, "app_db", None)
    user, project_id = await _mk(db_session, "u3env2@rvaiglobal.com")
    try:
        assert await provision_app_database(db_session, project_id) == {}
        row = await db_session.scalar(
            sa.select(ProjectDatabase).where(ProjectDatabase.project_id == project_id)
        )
        assert row is None  # no marker row at all — absence, not a not-ready claim
    finally:
        await reset_maintenance_engine_for_tests()


async def test_the_injected_host_honours_the_sandbox_facing_override(
    db_session: AsyncSession, salted: list[uuid.UUID], monkeypatch: pytest.MonkeyPatch
) -> None:
    # This is the database twin of `blob_base_url`: the container cannot reach the control
    # plane's own `localhost`, so the injected DSN carries the sandbox-visible host.
    assert settings.app_db is not None
    monkeypatch.setattr(
        settings,
        "app_db",
        settings.app_db.model_copy(update={"sandbox_dsn_host": "db.internal:6432"}),
    )
    user, project_id = await _mk(db_session, "u3env3@rvaiglobal.com")
    salted.append(project_id)

    env = await provision_app_database(db_session, project_id)

    assert "@db.internal:6432/" in env["BIAL_DATABASE_URL"]


# --- the manager's three birth arms -------------------------------------------------------


async def test_the_fresh_provision_arm_injects_the_dsn_alongside_the_base_env(
    db_session: AsyncSession,
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
    salted: list[uuid.UUID],
) -> None:
    user, project_id = await _mk(db_session, "u3start@rvaiglobal.com")
    salted.append(project_id)
    manager = SessionManager()
    client = FakeSandboxClient()

    session = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=client, may_write=True
    )

    assert client.provisioned == [app_name_for(session.app_id)]
    assert client.provision_env is not None
    _assert_is_a_sandbox_dsn(client.provision_env["BIAL_DATABASE_URL"], project_id)
    # Merged, not replaced — the always-present identity vars are still there.
    assert all(name in client.provision_env for name in _BASE_ENV)


async def _end_save_and_release(
    manager: SessionManager,
    db: AsyncSession,
    user,
    project_id: uuid.UUID,
    session,
    client: FakeSandboxClient,
) -> None:
    """Leave a saved bundle behind and a free slot — the citizen's own three steps, in order.

    The turn's ending frees the slot and PARDONS the container (no bundle: `finish_turn_sandbox`
    writes only the recovery copy). So the Save is what puts a bundle in the saved slot, and the
    release is what takes the pardoned container away — without it the next `ensure_sandbox`
    reattaches to a live container instead of taking the restore arm this file is about."""
    await manager.finish_turn_sandbox(session, client, touched=True)
    # The Save attaches through the registry rather than through a session — it is the
    # BETWEEN-turns click — so the fake needs a container to answer with.
    client.attach_handle = session.handle
    await manager.save_project_snapshot(db, user, project_id, sandbox_client=client)
    await manager.release_project_sandbox(db, user, project_id, sandbox_client=client)


async def test_the_restore_arm_reinjects_the_dsn(
    db_session: AsyncSession,
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
    salted: list[uuid.UUID],
) -> None:
    # Restore is one of `_resolve_sandbox`'s birth arms, so the DSN has to ride it too or a
    # resumed app silently loses its database.
    user, project_id = await _mk(db_session, "u3restore@rvaiglobal.com")
    salted.append(project_id)
    manager = SessionManager()
    client = FakeSandboxClient()
    first = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=client, may_write=True
    )
    await _end_save_and_release(manager, db_session, user, project_id, first, client)
    assert snapshot_key(first.app_id) in fake_storage.objects  # the Save wrote the bundle

    second_client = FakeSandboxClient()
    second = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=second_client, may_write=True
    )

    assert second_client.restored == [app_name_for(second.app_id)]  # restored, not re-provisioned
    assert second_client.restore_env is not None
    _assert_is_a_sandbox_dsn(second_client.restore_env["BIAL_DATABASE_URL"], project_id)


async def test_relaunch_preview_reinjects_the_dsn(
    db_session: AsyncSession,
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
    salted: list[uuid.UUID],
) -> None:
    # The relaunch merge is written SEPARATELY from the start merge on purpose
    # (`relaunch_preview` must not reuse `_restore_or_provision`), so a var added to only one
    # of the two is a silent half-fix — this is the test that catches it.
    user, project_id = await _mk(db_session, "u3relaunch@rvaiglobal.com")
    salted.append(project_id)
    manager = SessionManager()
    first_client = FakeSandboxClient()
    built = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=first_client, may_write=True
    )
    await _end_save_and_release(manager, db_session, user, project_id, built, first_client)

    client = FakeSandboxClient()
    await manager.relaunch_preview(db_session, user, project_id, client)
    await detached_work_done(manager)

    assert client.restored == [app_name_for(built.app_id)]
    assert client.restore_env is not None
    _assert_is_a_sandbox_dsn(client.restore_env["BIAL_DATABASE_URL"], project_id)
    assert all(name in client.restore_env for name in _BASE_ENV)


async def test_a_legacy_project_is_provisioned_lazily_and_exactly_once(
    db_session: AsyncSession,
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
    salted: list[uuid.UUID],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # A project that predates the feature (here: created through the factory, so nothing
    # provisioned it) gets its database on its NEXT BUILD — no migration, no backfill. And
    # the second build must not re-run the external sequence: `db_ready` short-circuits it.
    user, project_id = await _mk(db_session, "u3legacy@rvaiglobal.com")
    salted.append(project_id)
    assert (
        await db_session.scalar(
            sa.select(ProjectDatabase).where(ProjectDatabase.project_id == project_id)
        )
    ) is None

    role_creations: list[str] = []
    real_create_role = provision_module._create_role

    async def _counting_create_role(conn: AsyncConnection, *, role: str, password: str) -> None:
        role_creations.append(role)
        await real_create_role(conn, role=role, password=password)

    monkeypatch.setattr(provision_module, "_create_role", _counting_create_role)

    manager = SessionManager()
    for _turn in ("the first turn", "the second turn"):
        client = FakeSandboxClient()
        session = await manager.ensure_sandbox(
            db_session, user, project_id, sandbox_client=client, may_write=True
        )
        await _end_save_and_release(manager, db_session, user, project_id, session, client)

    assert role_creations == [role_name(project_id)]  # the external sequence ran ONCE
    row = await db_session.scalar(
        sa.select(ProjectDatabase)
        .where(ProjectDatabase.project_id == project_id)
        .execution_options(populate_existing=True)
    )
    assert row is not None and row.db_ready is True
