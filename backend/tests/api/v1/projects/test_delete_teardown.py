"""U5 — deleting a project destroys its database, its role, and its container (ADR-0028).

The ordering under test is the one the whole delete path is built around: gather handles →
delete rows → COMMIT → `salt_the_earth` → sweep. Nothing irreversible outside PostgreSQL's
own rows happens before the commit that authorizes it, and nothing after the commit is
allowed to raise — a delete that already succeeded must not 500 because a drop failed.

The interesting scenario is the one the build-session guard does NOT cover. A relaunched
preview holds no lock (pinned by `test_a_relaunched_preview_does_not_block_the_delete_and_
is_not_torn_down`) and a deployed container has no interlock at all, so at delete time
something can still be holding live connections to the app database. `DROP DATABASE ...
WITH (FORCE)` after the sever — not the guard — is what makes that stop.

Real cluster: the `db_session` rollback cannot undo cluster DDL run on the separate
AUTOCOMMIT engine, so every database created here is destroyed either by the endpoint under
test or by the session-scoped hook in `tests/conftest.py`.
"""

from __future__ import annotations

import uuid
from typing import Any

import pytest
import sqlalchemy as sa
import structlog
from httpx import AsyncClient
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from src.config import settings
from src.db.models.app_registry import AppRegistry
from src.db.models.audit import AuditLog
from src.db.models.project import Project
from src.db.models.project_database import ProjectDatabase
from src.services.appdb import teardown as appdb_teardown
from src.services.appdb.engine import get_maintenance_engine
from src.services.appdb.provision import control_plane_dsn, ensure_project_database
from src.services.auth.session_jwt import mint_session_jwt
from src.services.storage import AppContainerStore
from tests.factories import AppRegistryFactory, ProjectFactory, UserFactory
from tests.services.appdb.helpers import scalar_on

_TTL = settings.auth.access_ttl_seconds
_DATABASE_EXISTS = "SELECT EXISTS (SELECT 1 FROM pg_database WHERE datname = :db)"
_ROLE_EXISTS = "SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :role)"


class _RecordingContainerStore(AppContainerStore):
    """Records container deletes without touching Azure (skips `__init__` on purpose)."""

    def __init__(self) -> None:  # noqa: D107
        self.deleted: list[uuid.UUID] = []

    async def delete_container(self, app_id: uuid.UUID) -> None:
        self.deleted.append(app_id)


def _override_container_store(app: Any, store: AppContainerStore) -> None:
    from src.api.v1.projects.router import container_store_dependency

    app.dependency_overrides[container_store_dependency] = lambda: store


async def _registry_row(db: AsyncSession, project_id: uuid.UUID) -> uuid.UUID | None:
    """The `project_databases` row id, re-read from the database.

    A plain `session.get()` would answer out of the identity map: the row is removed by the
    FK cascade on `projects`, which the ORM never sees, so the stale instance would still be
    sitting there looking alive.
    """
    return await db.scalar(
        sa.select(ProjectDatabase.id).where(ProjectDatabase.project_id == project_id)
    )


async def _catalog(sql: str, **params: Any) -> Any:
    engine = get_maintenance_engine()
    assert engine is not None
    async with engine.connect() as conn:
        return await conn.scalar(sa.text(sql), params)


async def _project_with_database(
    db: AsyncSession,
) -> tuple[dict[str, str], Any, Project, AppRegistry, ProjectDatabase]:
    user = await UserFactory.create(db)
    headers = {"Cookie": f"session={mint_session_jwt(user.id, user.token_version, _TTL)}"}
    project = await ProjectFactory.create(db, user.id)
    app_row = await AppRegistryFactory.create(db, user_id=user.id, project_id=project.id)
    record = await ensure_project_database(db, project.id)
    assert record is not None, "APP_DB__* must be configured in .env.test"
    return headers, user, project, app_row, record


# --- the happy path -----------------------------------------------------------------------


async def test_delete_drops_the_database_the_role_and_the_container(
    app: Any, client: AsyncClient, db_session: AsyncSession
) -> None:
    containers = _RecordingContainerStore()
    _override_container_store(app, containers)
    headers, user, project, app_row, record = await _project_with_database(db_session)

    resp = await client.request(
        "DELETE",
        f"/v1/projects/{project.id}",
        headers=headers,
        json={"remark": "No longer needed by the ground operations team"},
    )

    assert resp.status_code == 200
    # Rows first: the registry row goes with the project (ON DELETE CASCADE), which is what
    # makes "no row" mean "never provisioned" again.
    assert await db_session.get(Project, project.id) is None
    assert await db_session.get(AppRegistry, app_row.id) is None
    assert await _registry_row(db_session, project.id) is None
    # ...then the cluster objects, post-commit.
    assert await _catalog(_DATABASE_EXISTS, db=record.db_name) is False
    assert await _catalog(_ROLE_EXISTS, role=record.role_name) is False
    assert containers.deleted == [app_row.id]

    dropped = await db_session.scalar(
        sa.select(AuditLog).where(
            AuditLog.action == "db:drop", AuditLog.resource_id == str(project.id)
        )
    )
    assert dropped is not None
    assert dropped.actor_id == user.id
    # NAMES only, plus the `appId` that keeps a project-scoped row visible in the app drawer.
    assert dropped.detail == {
        "dbName": record.db_name,
        "roleName": record.role_name,
        "appId": str(app_row.id),
    }


async def test_an_app_less_project_still_has_its_database_dropped(
    app: Any, client: AsyncClient, db_session: AsyncSession
) -> None:
    # The database is project-keyed and exists from project-create, so a project that never
    # reached a first build still owns one — and the build-session guard is skipped entirely
    # for it. Nothing else would ever reclaim this database.
    user = await UserFactory.create(db_session)
    headers = {"Cookie": f"session={mint_session_jwt(user.id, user.token_version, _TTL)}"}
    project = await ProjectFactory.create(db_session, user.id)
    record = await ensure_project_database(db_session, project.id)
    assert record is not None

    resp = await client.request(
        "DELETE",
        f"/v1/projects/{project.id}",
        headers=headers,
        json={"remark": "No longer needed by the ground operations team"},
    )

    assert resp.status_code == 200
    assert await _catalog(_DATABASE_EXISTS, db=record.db_name) is False
    dropped = await db_session.scalar(
        sa.select(AuditLog).where(
            AuditLog.action == "db:drop", AuditLog.resource_id == str(project.id)
        )
    )
    assert dropped is not None and dropped.detail is not None
    assert "appId" not in dropped.detail  # there is no app to file it under


# --- the guard gap: a live preview connection ---------------------------------------------


async def test_a_live_preview_connection_does_not_survive_the_delete(
    app: Any, client: AsyncClient, db_session: AsyncSession, fake_redis: Any
) -> None:
    # Exactly the state a relaunched preview leaves behind: a READY registry entry under a
    # live stay and NO lock, so `refuse_while_build_session_live` returns without refusing.
    # The container is still connected. The force-drop is the entire guarantee here.
    from datetime import UTC, datetime, timedelta

    from src.services.build_sessions import app_name_for
    from src.services.redis.keys import (
        REGISTRY_FIELD_APP_NAME,
        REGISTRY_FIELD_FQDN,
        REGISTRY_FIELD_PREVIEW_STAY_UNTIL,
        REGISTRY_FIELD_STATE,
        lock_key,
        registry_key,
    )

    headers, user, project, app_row, record = await _project_with_database(db_session)
    await fake_redis.hset(
        registry_key(user.id),
        mapping={
            REGISTRY_FIELD_APP_NAME: app_name_for(app_row.id),
            REGISTRY_FIELD_FQDN: "sbx.example.net",
            REGISTRY_FIELD_STATE: "ready",
            REGISTRY_FIELD_PREVIEW_STAY_UNTIL: (
                datetime.now(UTC) + timedelta(minutes=30)
            ).isoformat(),
        },
    )
    assert not await fake_redis.exists(lock_key(user.id))

    dsn = control_plane_dsn(record)
    preview = create_async_engine(dsn, poolclass=NullPool)
    try:
        async with preview.connect() as connection:
            assert await connection.scalar(sa.text("SELECT 1")) == 1

            resp = await client.request(
                "DELETE",
                f"/v1/projects/{project.id}",
                headers=headers,
                json={"remark": "No longer needed by the ground operations team"},
            )
            assert resp.status_code == 200
    except DBAPIError:
        pass  # the forced-out connection may fail on its way out; that is the point
    finally:
        await preview.dispose()

    assert await _catalog(_DATABASE_EXISTS, db=record.db_name) is False
    assert await _catalog(_ROLE_EXISTS, role=record.role_name) is False


# --- the guard where it DOES hold ---------------------------------------------------------


async def test_a_live_build_still_refuses_the_delete_and_leaves_the_database_alone(
    app: Any, client: AsyncClient, db_session: AsyncSession, fake_redis: Any
) -> None:
    # R9 unchanged: a held lock refuses before anything is gathered, so the database must be
    # exactly as reachable afterwards as it was before. A refusal that had already severed
    # would be a silent outage on a delete the user was told did not happen.
    from src.services.build_sessions import app_name_for
    from src.services.redis.keys import (
        REGISTRY_FIELD_APP_NAME,
        REGISTRY_FIELD_FQDN,
        REGISTRY_FIELD_STATE,
        lock_key,
        registry_key,
    )

    headers, user, project, app_row, record = await _project_with_database(db_session)
    await fake_redis.set(lock_key(user.id), "holder-token")
    await fake_redis.hset(
        registry_key(user.id),
        mapping={
            REGISTRY_FIELD_APP_NAME: app_name_for(app_row.id),
            REGISTRY_FIELD_FQDN: "sbx.example.net",
            REGISTRY_FIELD_STATE: "ready",
        },
    )

    resp = await client.request(
        "DELETE",
        f"/v1/projects/{project.id}",
        headers=headers,
        json={"remark": "No longer needed by the ground operations team"},
    )

    assert resp.status_code == 409
    assert await db_session.get(Project, project.id) is not None
    assert await _catalog(_DATABASE_EXISTS, db=record.db_name) is True
    assert await scalar_on(control_plane_dsn(record), "SELECT 1") == 1
    assert (
        await db_session.scalar(
            sa.select(sa.func.count()).select_from(AuditLog).where(AuditLog.action == "db:drop")
        )
    ) == 0


# --- the error path: the drop fails --------------------------------------------------------


async def test_a_failed_drop_logs_an_orphan_and_still_returns_success(
    app: Any,
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # By the time the drop runs, the commit is durable — `get_db` only rolls back on an
    # exception escaping the endpoint, so raising here could not un-delete anything; it
    # would merely 500 a delete that succeeded. The database becomes a reclaimable orphan.
    headers, _user, project, _app_row, record = await _project_with_database(db_session)

    async def explode(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("simulated DROP DATABASE failure")

    monkeypatch.setattr(appdb_teardown, "_drop_database", explode)
    with structlog.testing.capture_logs() as captured:
        resp = await client.request(
            "DELETE",
            f"/v1/projects/{project.id}",
            headers=headers,
            json={"remark": "No longer needed by the ground operations team"},
        )

    assert resp.status_code == 200
    assert await db_session.get(Project, project.id) is None
    assert await _registry_row(db_session, project.id) is None
    assert any(e.get("event") == "app_database_drop_database_failed" for e in captured)
    # Still there, and findable by name — which is exactly what the reconciler needs (U7).
    assert await _catalog(_DATABASE_EXISTS, db=record.db_name) is True


async def test_a_salt_that_cannot_reach_the_cluster_still_returns_success(
    app: Any,
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # `_best_effort` wraps each teardown STATEMENT, but not `engine.connect()` itself. An
    # unreachable cluster raises out of the acquisition (a bare OSError before the driver
    # wraps it), and both delete callers invoke `salt_the_earth` bare, post-commit — so
    # without the guard inside `salt_the_earth` an already-committed delete would 500. This
    # pins that it does not: the delete still succeeds and the database is a logged orphan.
    headers, _user, project, _app_row, record = await _project_with_database(db_session)

    class _UnreachableEngine:
        def connect(self) -> Any:
            raise OSError("connection refused")

    # Only teardown's binding is swapped — provision (already done) and `_catalog` below use
    # the real engine, so the orphan is still observable and the session hook still cleans it.
    monkeypatch.setattr(appdb_teardown, "get_maintenance_engine", lambda: _UnreachableEngine())
    with structlog.testing.capture_logs() as captured:
        resp = await client.request(
            "DELETE",
            f"/v1/projects/{project.id}",
            headers=headers,
            json={"remark": "No longer needed by the ground operations team"},
        )

    assert resp.status_code == 200
    assert await db_session.get(Project, project.id) is None
    assert await _registry_row(db_session, project.id) is None
    assert any(e.get("event") == "app_database_salt_connect_failed" for e in captured)
    # The salt never reached the cluster, so the database survives as a reclaimable orphan.
    assert await _catalog(_DATABASE_EXISTS, db=record.db_name) is True


async def test_a_project_without_a_database_deletes_exactly_as_before(
    app: Any, client: AsyncClient, db_session: AsyncSession
) -> None:
    # The unconfigured baseline, fixture-free by construction: no `project_databases` row
    # means no handles, no teardown call, and no audit row — the pre-ADR-0028 behaviour.
    user = await UserFactory.create(db_session)
    headers = {"Cookie": f"session={mint_session_jwt(user.id, user.token_version, _TTL)}"}
    project = await ProjectFactory.create(db_session, user.id)
    await db_session.commit()

    resp = await client.request(
        "DELETE",
        f"/v1/projects/{project.id}",
        headers=headers,
        json={"remark": "No longer needed by the ground operations team"},
    )

    assert resp.status_code == 200
    assert await db_session.get(Project, project.id) is None
    assert (
        await db_session.scalar(
            sa.select(sa.func.count()).select_from(AuditLog).where(AuditLog.action == "db:drop")
        )
    ) == 0
