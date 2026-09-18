"""Deleting a project destroys its database, its role, and both of its containers — the per-app
Blob container and the sandbox container it was running in. The database is a database on the
shared cluster, dropped with `DROP DATABASE`; it is not one of the containers.

The ordering under test: gather handles → delete rows → COMMIT → `salt_the_earth` → sweep → reap
the sandbox. Nothing irreversible outside PostgreSQL's own rows happens before the commit that
authorizes it, and nothing after may raise — a delete that already succeeded must not 500 because
a drop failed. The build-session guard does not cover every case: a relaunched preview holds no
lock and a deployed container has no interlock, so something can still be holding live
connections at delete time — the sandbox reap is best-effort and cannot see a published
container, so `DROP DATABASE ... WITH (FORCE)` after the sever is what stops that. Every database
here is dropped by the endpoint under test or the session-scoped hook in `tests/conftest.py`."""

from __future__ import annotations

import importlib
import uuid
from collections.abc import Mapping, Sequence
from typing import Any

import pytest
import sqlalchemy as sa
import structlog
from httpx import AsyncClient
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from src.config import settings
from src.core.alarms import TEARDOWN_ARTEFACT_SURVIVED_EVENT
from src.db.models.app_registry import AppRegistry
from src.db.models.audit import AuditLog
from src.db.models.deleted_project import DeletedProject
from src.db.models.deployment import Deployment
from src.db.models.project import Project
from src.db.models.project_database import ProjectDatabase
from src.services.appdb import teardown as appdb_teardown
from src.services.appdb.engine import get_maintenance_engine
from src.services.appdb.provision import control_plane_dsn, ensure_project_database
from src.services.auth.session_jwt import mint_session_jwt
from src.services.storage import AppContainerStore
from tests.api.v1.projects.conftest import DELETE_BODY
from tests.factories import (
    AppRegistryFactory,
    ConversationFactory,
    ProjectFactory,
    UserFactory,
)
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


def _survived(captured: Sequence[Mapping[str, Any]], *, artefact: str) -> list[str]:
    """The ids the pinned survival alarm named for one artefact class.

    Asserts on the CONSTANT, never a string copy of it — `alarms.py`'s one rule is that the
    name exists in exactly one place, and a test that retypes it is the second spelling that
    rule exists to prevent."""
    return [
        str(entry.get("artefact_id"))
        for entry in captured
        if entry.get("event") == TEARDOWN_ARTEFACT_SURVIVED_EVENT
        and entry.get("artefact") == artefact
    ]


async def _teardown_record(db: AsyncSession, project_id: uuid.UUID) -> list[Any] | None:
    """What the `project:teardown-incomplete` audit row says survived, or `None` when the
    delete left nothing behind and so wrote no row at all."""
    row = await db.scalar(
        sa.select(AuditLog).where(
            AuditLog.action == "project:teardown-incomplete",
            AuditLog.resource_id == str(project_id),
        )
    )
    if row is None:
        return None
    assert row.detail is not None
    survived = row.detail["survived"]
    assert isinstance(survived, list)
    assert row.detail["count"] == len(survived)
    return survived


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
        json=DELETE_BODY,
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

    # THE ONLY TEST THAT EVER PROVISIONS A REAL DATABASE THROUGH THE DELETE PATH, so it is
    # the only one that can prove `had_database` is ever actually `True`. Every OTHER
    # tombstone test builds its project via the bare factory, where `teardown_handles()`
    # always returns `None` — a mutant hard-coding `had_database=False` on the
    # `DeletedProject(...)` construction passed every one of them.
    tombstone = await db_session.scalar(
        sa.select(DeletedProject).where(DeletedProject.project_id == project.id)
    )
    assert tombstone is not None
    assert tombstone.had_database is True
    assert tombstone.had_app is True


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
        json=DELETE_BODY,
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


# --- what the app WAS survives the cascade ------------------------------------------------
#
# The record already named the project and said who deleted it and why. It did not say what
# the thing DID, so an administrator reading "Visitor Log" three months later had a name and
# nothing else. The description is the sentence that answers it.
#
# WHY THESE TESTS LIVE HERE rather than beside the other tombstone tests in
# `test_delete_remark.py`: the claim is about ORDERING against a real cascade. The description
# is on the `projects` row `delete_project_cascade` deletes, and `projects.description_tsv` is
# a lossy `to_tsvector` of it rather than a second copy — so the value has to be read into the
# record BEFORE the cascade runs, and a record written afterwards loses it permanently. Only a
# test that runs the real cascade against real Postgres can tell a correct implementation from
# one that reads the description a line too late; a mocked cascade would pass either.


async def test_the_description_survives_the_cascade_that_destroys_its_only_copy(
    app: Any, client: AsyncClient, db_session: AsyncSession
) -> None:
    """The whole record, read back after the cascade committed.

    Every field is asserted, not just the new one: a record that gained the description by
    losing the count, the owner or the reason would be a worse record than the one before it.
    """
    containers = _RecordingContainerStore()
    _override_container_store(app, containers)
    headers, user, project, app_row, record = await _project_with_database(db_session)
    user.display_name = "Asha Rao"
    project.description = "Logs every visitor to the terminal and flags anyone without a pass."
    await ConversationFactory.create(db_session, user.id, project_id=project.id)
    await db_session.commit()

    resp = await client.request(
        "DELETE", f"/v1/projects/{project.id}", headers=headers, json=DELETE_BODY
    )

    assert resp.status_code == 200
    # The source is gone — this is what makes the read below a durability claim rather than a
    # round-trip through an object that happens to still be in memory.
    assert await db_session.get(Project, project.id) is None

    tombstone = (
        await db_session.execute(
            sa.select(DeletedProject).where(DeletedProject.project_id == project.id)
        )
    ).scalar_one()  # ONE record, not two: `scalar_one` is the double-delete assertion too.
    assert tombstone.project_description == (
        "Logs every visitor to the terminal and flags anyone without a pass."
    )
    # ...and every field that was already there.
    assert tombstone.project_name == project.name
    assert tombstone.owner_id == user.id
    assert tombstone.owner_email == user.email
    assert tombstone.deleted_by == user.id
    assert tombstone.deleted_by_name == "Asha Rao"
    assert tombstone.remark == DELETE_BODY["remark"]
    assert tombstone.chats_deleted == 1
    assert tombstone.had_app is True
    assert tombstone.had_database is True
    assert tombstone.deleted_at is not None


async def test_a_project_with_no_description_records_an_empty_value(
    app: Any, client: AsyncClient, db_session: AsyncSession
) -> None:
    """The description is optional on `projects` (NULL = none) and the column that keeps it is
    NOT NULL, so the two have to be bridged somewhere. What is pinned here is the OUTCOME —
    empty, never null, never a refusal — not which of the three bridges produced it.

    Stated because it was measured rather than assumed. Dropping the route's coalesce does NOT
    fail this test, and neither does dropping the model's Python-side default on top of it:
    SQLAlchemy leaves a `None` out of the INSERT when the column carries a server default, so
    the row still lands as `''`. What this DOES kill is a route that writes something else —
    `str(project.description)` storing the literal `"None"` is the slip it was mutation-checked
    against, and `or project.name` is the other shape of it. The nullable-vs-NOT-NULL decision
    underneath is pinned at the DDL instead, by
    `tests/db/test_migration_0037_deleted_project_description.py`, which goes red for it.
    """
    user = await UserFactory.create(db_session)
    headers = {"Cookie": f"session={mint_session_jwt(user.id, user.token_version, _TTL)}"}
    project = await ProjectFactory.create(db_session, user.id)
    assert project.description is None  # the state under test, stated rather than assumed
    await db_session.commit()

    resp = await client.request(
        "DELETE", f"/v1/projects/{project.id}", headers=headers, json=DELETE_BODY
    )

    assert resp.status_code == 200, resp.text
    tombstone = (
        await db_session.execute(
            sa.select(DeletedProject).where(DeletedProject.project_id == project.id)
        )
    ).scalar_one()
    assert tombstone.project_description == ""  # empty, and NOT null


async def test_the_description_is_read_before_the_cascade_that_deletes_its_only_row(
    app: Any, client: AsyncClient, db_session: AsyncSession
) -> None:
    """THE ORDERING, pinned directly. An implementation that reads `project.description` after
    `delete_project_cascade` has run records an empty string, and every assertion above except
    the first would still pass.

    Read as a bare COLUMN, not through the mapped entity: the tombstone instance the route
    constructed is still in this session's identity map, so `select(DeletedProject)` can answer
    from memory. A column select goes to the database, which is where the value has to be.
    """
    user = await UserFactory.create(db_session)
    headers = {"Cookie": f"session={mint_session_jwt(user.id, user.token_version, _TTL)}"}
    described = "Tracks bay allocation for turnarounds under forty minutes."
    project = await ProjectFactory.create(db_session, user.id, description=described)
    await db_session.commit()

    resp = await client.request(
        "DELETE", f"/v1/projects/{project.id}", headers=headers, json=DELETE_BODY
    )

    assert resp.status_code == 200
    stored = await db_session.scalar(
        sa.select(DeletedProject.project_description).where(
            DeletedProject.project_id == project.id
        )
    )
    assert stored == described
    # ...and there is nowhere left it could have been re-read from. Not just "this project's
    # row is gone" — NO row in `projects` holds this text any more, so the value on the
    # tombstone can only have come from a read that happened before the cascade.
    assert (
        await db_session.scalar(
            sa.select(sa.func.count()).select_from(Project).where(Project.description == described)
        )
    ) == 0


async def test_the_double_delete_race_still_fails_closed_on_the_records_unique_index(
    app: Any, client: AsyncClient, db_session: AsyncSession
) -> None:
    """The new column does not loosen the guard that makes one deletion one record.

    A NOT NULL column added to this table is exactly the kind of change that quietly turns the
    loser of the race from a clean 404 into a 500 — the insert would now fail on a null before
    it ever reached the unique index, from a different exception class the route does not
    catch. Staged the way `test_delete_remark.py` stages it: two overlapping requests are not
    expressible against a single bound `db_session`, so the winner's row is seeded as if it had
    already committed and the client's own DELETE runs the real cascade into it.
    """
    user = await UserFactory.create(db_session)
    headers = {"Cookie": f"session={mint_session_jwt(user.id, user.token_version, _TTL)}"}
    project = await ProjectFactory.create(
        db_session, user.id, description="Bay allocation for short turnarounds."
    )
    db_session.add(
        DeletedProject(
            project_id=project.id,
            project_name=project.name,
            project_description="Recorded by the request that won the race.",
            owner_id=user.id,
            owner_email=user.email,
            deleted_by=user.id,
            deleted_by_name="Someone Else",
            remark=DELETE_BODY["remark"],
        )
    )
    await db_session.commit()

    resp = await client.request(
        "DELETE", f"/v1/projects/{project.id}", headers=headers, json=DELETE_BODY
    )

    assert resp.status_code == 404, resp.text
    assert "Internal server error" not in resp.text  # a NOT NULL slip would land here


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
                json=DELETE_BODY,
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
    # A held lock refuses before anything is gathered, so the database must be
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
        json=DELETE_BODY,
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
            json=DELETE_BODY,
        )

    assert resp.status_code == 200
    assert await db_session.get(Project, project.id) is None
    assert await _registry_row(db_session, project.id) is None
    assert any(e.get("event") == "app_database_drop_database_failed" for e in captured)
    # Still there, and findable by name — which is exactly what the reconciler needs.
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
            json=DELETE_BODY,
        )

    assert resp.status_code == 200
    assert await db_session.get(Project, project.id) is None
    assert await _registry_row(db_session, project.id) is None
    # THE ALARM, not a bespoke event name: one pinned event across every arm of this path, with
    # the artefact class as a field. Nothing collects this database automatically —
    # `appdb/reconcile.py` is operator-invoked AND report-only — so the alarm is the notice.
    assert _survived(captured, artefact="app_database") == [record.db_name]
    # ...and the record an operator reads, on the audit log rather than in the citizen's own
    # words on the tombstone.
    assert await _teardown_record(db_session, project.id) == [
        {"artefact": "app_database", "id": record.db_name}
    ]
    # The salt never reached the cluster, so the database survives.
    assert await _catalog(_DATABASE_EXISTS, db=record.db_name) is True


async def test_a_project_without_a_database_deletes_exactly_as_before(
    app: Any, client: AsyncClient, db_session: AsyncSession
) -> None:
    # No `project_databases` row means no handles, no teardown call, and no audit row —
    # deletion behaves exactly as it did before project databases existed.
    user = await UserFactory.create(db_session)
    headers = {"Cookie": f"session={mint_session_jwt(user.id, user.token_version, _TTL)}"}
    project = await ProjectFactory.create(db_session, user.id)
    await db_session.commit()

    resp = await client.request(
        "DELETE",
        f"/v1/projects/{project.id}",
        headers=headers,
        json=DELETE_BODY,
    )

    assert resp.status_code == 200
    assert await db_session.get(Project, project.id) is None
    assert (
        await db_session.scalar(
            sa.select(sa.func.count()).select_from(AuditLog).where(AuditLog.action == "db:drop")
        )
    ) == 0


# --- the sandbox container goes with the project -------------------------------------------
#
# THE MOST DESTRUCTIVE STEP ON THIS PATH, and the reason every test below pins the identity
# check as hard as it pins the teardown. The sandbox registry key is per-USER
# (`registry_key(user_id)`), so an unconditional clear destroys whatever container that
# citizen happens to be running — for ANY project. `refuse_while_build_session_live` cannot
# cover it: it is app-scoped and, by its own docblock, does not cover a relaunched preview,
# which holds no lock by design. The check in front of `reap_user` is the whole guard.
#
# EVERY ARM IS FAKES. Nothing here reaches ARM; `FakeSandboxClient.teardown` records a name.


def _wire_sandbox(app: Any, sandbox: Any) -> None:
    from src.api.v1.build_sessions.deps import sandbox_or_none_dependency

    app.dependency_overrides[sandbox_or_none_dependency] = lambda: sandbox


def _wire_manager(app: Any) -> Any:
    """A FRESH `SessionManager` per test, bound to the route.

    Not the process singleton: the object under test here is `_start_locks[user_id]`, and a
    test that has to reason about which lock object the route picked up is a test that can go
    green for the wrong reason."""
    from src.api.v1.build_sessions.deps import session_manager_dependency
    from src.services.build_sessions import SessionManager

    manager = SessionManager()
    app.dependency_overrides[session_manager_dependency] = lambda: manager
    return manager


def _the_router_module() -> Any:
    """The projects ROUTER MODULE, for `monkeypatch.setattr`.

    `from src.api.v1.projects import router` hands back the `APIRouter` object — the package
    `__init__` re-exports it under that exact name, shadowing the submodule — so patching an
    attribute on it raises `AttributeError` on a module that never saw the change. Same idiom
    as `test_projects_crud.py`."""
    import importlib

    return importlib.import_module("src.api.v1.projects.router")


async def _project_with_app(db: AsyncSession) -> tuple[dict[str, str], Any, Project, AppRegistry]:
    """A project + its app row, with NO per-project database — the light setup.

    `_project_with_database` above provisions a real cluster database on the AUTOCOMMIT
    engine; the sandbox arms have no opinion about that and should not pay for it. The one
    test that asserts the reap did not displace the database drop uses the heavy helper."""
    user = await UserFactory.create(db)
    headers = {"Cookie": f"session={mint_session_jwt(user.id, user.token_version, _TTL)}"}
    project = await ProjectFactory.create(db, user.id)
    app_row = await AppRegistryFactory.create(db, user_id=user.id, project_id=project.id)
    await db.commit()
    return headers, user, project, app_row


async def _registry_names(
    fake_redis: Any, user_id: uuid.UUID, app_id: uuid.UUID, state: str
) -> None:
    """Put the registry into the state a live container leaves behind: this user's one hash,
    naming this app's container."""
    from src.services.build_sessions import app_name_for
    from src.services.redis.keys import (
        REGISTRY_FIELD_APP_NAME,
        REGISTRY_FIELD_FQDN,
        REGISTRY_FIELD_STATE,
        registry_key,
    )

    await fake_redis.hset(
        registry_key(user_id),
        mapping={
            REGISTRY_FIELD_APP_NAME: app_name_for(app_id),
            REGISTRY_FIELD_FQDN: "sbx.example.net",
            REGISTRY_FIELD_STATE: state,
        },
    )


def _registry(user_id: uuid.UUID) -> str:
    from src.services.redis.keys import registry_key

    return registry_key(user_id)


def _named(app_id: uuid.UUID) -> str:
    from src.services.build_sessions import app_name_for

    return app_name_for(app_id)


async def _delete(client: AsyncClient, project_id: uuid.UUID, headers: dict[str, str]) -> Any:
    """The delete, BOUNDED. The bound is an assertion, not a convenience: the whole point of
    `_SANDBOX_REAP_LOCK_WAIT_SECONDS` is that an already-committed delete never parks behind a
    provision, and an unbounded `await lock.acquire()` would hang this call forever rather than
    fail it."""
    import asyncio

    return await asyncio.wait_for(
        client.request("DELETE", f"/v1/projects/{project_id}", headers=headers, json=DELETE_BODY),
        timeout=10,
    )


async def test_the_sandbox_serving_the_deleted_project_is_torn_down(
    app: Any, client: AsyncClient, db_session: AsyncSession, fake_redis: Any
) -> None:
    # The whole point of the reap: a container serving a project that no longer exists bills at
    # $2.60/day until the citizen next builds. Registry named this app; ARM delete called;
    # registry entry cleared.
    from tests.fakes import FakeSandboxClient

    headers, user, project, app_row = await _project_with_app(db_session)
    sandbox = FakeSandboxClient()
    _wire_sandbox(app, sandbox)
    _wire_manager(app)
    await _registry_names(fake_redis, user.id, app_row.id, "ready")

    resp = await _delete(client, project.id, headers)

    assert resp.status_code == 200
    assert await db_session.get(Project, project.id) is None
    assert sandbox.torn_down == [_named(app_row.id)]
    assert await fake_redis.exists(_registry(user.id)) == 0


async def test_a_sandbox_serving_a_different_project_is_left_alone(
    app: Any, client: AsyncClient, db_session: AsyncSession, fake_redis: Any
) -> None:
    # THE GUARD WHOSE ABSENCE DESTROYS LIVE WORK. The registry key is per-USER, so the same
    # citizen's container for project B is what an unconditional clear would delete — and the
    # route's own build-session guard proceeds here exactly as it should, because nothing is
    # building.
    #
    # Mutation check: delete the `reg is None or reg.get(...) != app_name_for(app_id)` arm in
    # `_reap_the_project_sandbox_or_shrug` and this goes red on BOTH assertions — B's container
    # torn down and B's registry entry cleared.
    from tests.fakes import FakeSandboxClient

    headers, user, project_a, app_a = await _project_with_app(db_session)
    project_b = await ProjectFactory.create(db_session, user.id)
    app_b = await AppRegistryFactory.create(db_session, user_id=user.id, project_id=project_b.id)
    await db_session.commit()
    sandbox = FakeSandboxClient()
    _wire_sandbox(app, sandbox)
    _wire_manager(app)
    # B is the one that is up; A is the one being deleted.
    await _registry_names(fake_redis, user.id, app_b.id, "ready")

    resp = await _delete(client, project_a.id, headers)

    assert resp.status_code == 200
    assert await db_session.get(Project, project_a.id) is None
    assert sandbox.torn_down == []
    from src.services.redis.keys import REGISTRY_FIELD_APP_NAME

    survivor = await fake_redis.hgetall(_registry(user.id))
    assert survivor[REGISTRY_FIELD_APP_NAME] == _named(app_b.id)
    assert await db_session.get(Project, project_b.id) is not None
    assert app_a.id != app_b.id  # the two names really are different


async def test_the_start_lock_is_held_across_the_check_and_the_reap(
    app: Any, client: AsyncClient, db_session: AsyncSession, fake_redis: Any
) -> None:
    # The direct form of the invariant: while the ARM delete is in flight, nobody else can be
    # inside `start`. Asserted from INSIDE `teardown`, which is the latest moment in the reap
    # and therefore the one an interleaving start would land in.
    #
    # Mutation check: drop the `async with`/`wait_for(lock.acquire())` and this goes red —
    # `locked()` reads False at teardown time.
    from tests.fakes import FakeSandboxClient

    headers, user, project, app_row = await _project_with_app(db_session)
    manager = _wire_manager(app)
    held_during_teardown: list[bool] = []

    class _LockProbingSandbox(FakeSandboxClient):
        async def teardown(self, handle: Any) -> None:
            held_during_teardown.append(manager._start_lock_for(user.id).locked())
            await super().teardown(handle)

    sandbox = _LockProbingSandbox()
    _wire_sandbox(app, sandbox)
    await _registry_names(fake_redis, user.id, app_row.id, "ready")

    resp = await _delete(client, project.id, headers)

    assert resp.status_code == 200
    assert sandbox.torn_down == [_named(app_row.id)]
    assert held_during_teardown == [True]
    # ...and it is given back, or the citizen's next build waits on a delete that is over.
    assert not manager._start_lock_for(user.id).locked()


async def test_a_racing_start_for_another_project_is_not_destroyed_by_the_reap(
    app: Any, client: AsyncClient, db_session: AsyncSession, fake_redis: Any
) -> None:
    # THE CONSEQUENCE OF DROPPING THE LOCK, spelled out. The identity check and `reap_user` are
    # a TOCTOU pair without it: `reap_user` does its OWN fresh `read_registry` and tears down
    # whatever it finds THEN, so a start for a different project that lands in the gap has its
    # brand-new container's record deleted underneath it — the container survives with nothing
    # able to find it again, which is the twelve-day-orphan shape the reaper's own docstring
    # describes.
    #
    # Mutation check: drop the lock and this goes red — B's registry entry is gone, cleared by
    # A's reap.
    import asyncio

    from tests.fakes import FakeSandboxClient

    headers, user, project_a, app_a = await _project_with_app(db_session)
    project_b = await ProjectFactory.create(db_session, user.id)
    app_b = await AppRegistryFactory.create(db_session, user_id=user.id, project_id=project_b.id)
    await db_session.commit()
    manager = _wire_manager(app)
    teardown_reached = asyncio.Event()
    let_teardown_finish = asyncio.Event()

    class _BlockingSandbox(FakeSandboxClient):
        async def teardown(self, handle: Any) -> None:
            teardown_reached.set()
            await let_teardown_finish.wait()
            await super().teardown(handle)

    sandbox = _BlockingSandbox()
    _wire_sandbox(app, sandbox)
    await _registry_names(fake_redis, user.id, app_a.id, "ready")

    async def a_start_for_project_b() -> None:
        """What `ensure_sandbox` does, reduced to the two steps that matter: take the per-user
        start lock, then write the registry record for the container it just provisioned. (The
        deleted `_start_locked` did the same two, under the same lock.)"""
        await teardown_reached.wait()
        async with manager._start_lock_for(user.id):
            await _registry_names(fake_redis, user.id, app_b.id, "ready")

    racing_start = asyncio.create_task(a_start_for_project_b())
    delete = asyncio.create_task(_delete(client, project_a.id, headers))
    try:
        # BOUNDED, and not for speed: a mutant that removes the reap outright never reaches
        # `teardown`, and a bare `await` on this event would HANG the run instead of failing
        # it. A hang is not a red — it is a test that cannot report.
        await asyncio.wait_for(teardown_reached.wait(), timeout=10)
    except TimeoutError:
        let_teardown_finish.set()
        racing_start.cancel()
        await asyncio.gather(delete, racing_start, return_exceptions=True)
        raise AssertionError(
            "the reap never reached teardown; nothing was reaped at all"
        ) from None
    for _ in range(20):
        await asyncio.sleep(0)  # give the racing start every chance to get in
    let_teardown_finish.set()
    resp = await delete
    await racing_start

    assert resp.status_code == 200
    assert await db_session.get(Project, project_a.id) is None
    # A's container went; B's record — written by the start that had to WAIT — is intact.
    assert sandbox.torn_down == [_named(app_a.id)]
    from src.services.redis.keys import REGISTRY_FIELD_APP_NAME

    survivor = await fake_redis.hgetall(_registry(user.id))
    assert survivor.get(REGISTRY_FIELD_APP_NAME) == _named(app_b.id)


async def test_a_lock_held_by_another_projects_start_does_not_invent_a_survivor(
    app: Any, client: AsyncClient, db_session: AsyncSession, fake_redis: Any
) -> None:
    """★ THE REAP ASKS WHETHER THERE WAS ANYTHING TO REAP BEFORE SAYING SOMETHING SURVIVED.

    The identity check sits on the far side of the per-user start lock, so a wait that times
    out used to report a survivor purely from having failed to get in — and the commonest way
    in is a citizen provisioning a workspace for ANOTHER project, whose provision holds that
    lock for 30-60 seconds. That filed a permanent `project:teardown-incomplete` row naming a
    container of THIS project's that was never running, and sent an operator after it. The
    registry says plainly that what is up belongs to project B; the record must agree.

    Mutation check: delete the `_registry_names_this_projects_container(...) is False` arm from
    the timeout branch and both the alarm and the audit row come back."""
    from tests.fakes import FakeSandboxClient

    headers, user, project_a, app_a = await _project_with_app(db_session)
    project_b = await ProjectFactory.create(db_session, user.id)
    app_b = await AppRegistryFactory.create(db_session, user_id=user.id, project_id=project_b.id)
    await db_session.commit()
    sandbox = FakeSandboxClient()
    _wire_sandbox(app, sandbox)
    manager = _wire_manager(app)
    # B's container is the one that is up, and B's start is the one holding the lock.
    await _registry_names(fake_redis, user.id, app_b.id, "ready")

    lock = manager._start_lock_for(user.id)  # noqa: SLF001 — the route's own lock, by design
    await lock.acquire()
    try:
        with structlog.testing.capture_logs() as captured:
            resp = await _delete(client, project_a.id, headers)
    finally:
        lock.release()

    assert resp.status_code == 200
    assert await db_session.get(Project, project_a.id) is None
    # LIVENESS FIRST: the skip was DECIDED here, so the two absences below mean the arm ran
    # and answered "nothing of ours", not that the reap never happened at all.
    assert any(
        entry.get("event") == "project_delete_sandbox_reap_skipped_not_ours" for entry in captured
    )
    assert _survived(captured, artefact="sandbox_container") == []
    assert await _teardown_record(db_session, project_a.id) is None
    # And B is untouched — the lock was never taken, so nothing could have been.
    assert sandbox.torn_down == []


async def test_a_lock_held_over_an_empty_registry_still_reports(
    app: Any, client: AsyncClient, db_session: AsyncSession, fake_redis: Any
) -> None:
    """★ AN EMPTY REGISTRY IS NOT EVIDENCE — outside the lock it is the shape of a provision.

    `SandboxClient._write_registry` hydrates the hash for a JUST-CREATED container, at the END
    of a 30-60 second provision, under this same per-user lock. So "the lock is held AND the
    registry is empty" is exactly what a start in flight looks like from out here — quite
    possibly a start for the very project being deleted, whose container will come up holding
    that project's database credential and serving its tree.

    Reading that as "nothing of ours survives" would silence the record for the one case it
    exists to catch, and nothing automatic collects it outside production. Only a registry that
    names ANOTHER project's container rules ours out.

    Mutation check: collapse `NOTHING_REGISTERED` back into the suppressing arm and this goes
    red on both the alarm and the audit row."""
    from tests.fakes import FakeSandboxClient

    headers, user, project, app_row = await _project_with_app(db_session)
    sandbox = FakeSandboxClient()
    _wire_sandbox(app, sandbox)
    manager = _wire_manager(app)
    # NOTHING registered — the provision under the lock has not written its entry yet.
    assert await fake_redis.exists(_registry(user.id)) == 0

    lock = manager._start_lock_for(user.id)  # noqa: SLF001 — the route's own lock, by design
    await lock.acquire()
    try:
        with structlog.testing.capture_logs() as captured:
            resp = await _delete(client, project.id, headers)
    finally:
        lock.release()

    assert resp.status_code == 200
    assert _survived(captured, artefact="sandbox_container") == [_named(app_row.id)]
    assert await _teardown_record(db_session, project.id) == [
        {"artefact": "sandbox_container", "id": _named(app_row.id)}
    ]


async def test_a_reap_that_raises_over_another_projects_container_invents_nothing(
    app: Any, client: AsyncClient, db_session: AsyncSession, fake_redis: Any, monkeypatch: Any
) -> None:
    """The broad arm answers the same question as the timeout arm, and must answer it the same
    way: a Redis blip on the way in is not evidence that this project had a container.

    Mutation check: delete the `SOMEONE_ELSES` skip from the `except Exception` arm and this
    goes red — B's name appears in a record about A."""
    from tests.fakes import FakeSandboxClient

    headers, user, project_a, app_a = await _project_with_app(db_session)
    project_b = await ProjectFactory.create(db_session, user.id)
    app_b = await AppRegistryFactory.create(db_session, user_id=user.id, project_id=project_b.id)
    await db_session.commit()
    sandbox = FakeSandboxClient()
    _wire_sandbox(app, sandbox)
    _wire_manager(app)
    await _registry_names(fake_redis, user.id, app_b.id, "ready")

    projects_router = _the_router_module()
    real = projects_router.reap_user

    async def _explodes(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("redis went away mid-reap")

    monkeypatch.setattr(projects_router, "reap_user", _explodes)

    with structlog.testing.capture_logs() as captured:
        resp = await _delete(client, project_a.id, headers)

    assert resp.status_code == 200
    assert await db_session.get(Project, project_a.id) is None
    # LIVENESS: the arm really ran — the skip line is what it logged.
    assert any(
        entry.get("event") == "project_delete_sandbox_reap_skipped_not_ours" for entry in captured
    )
    assert _survived(captured, artefact="sandbox_container") == []
    assert await _teardown_record(db_session, project_a.id) is None
    assert real is not None  # the real function is still importable; only the binding moved


async def test_a_lock_held_while_our_own_container_is_up_still_reports_it(
    app: Any, client: AsyncClient, db_session: AsyncSession, fake_redis: Any
) -> None:
    """The other direction, and the reason the check is not simply "stay quiet on a timeout".

    When the registry names THIS project's container and the reap cannot get the lock, the
    container really is standing, really is billing, and outside production nothing automatic
    comes for it — `may_destroy_on_this_control_plane` gates the scheduled reap on production.
    That is a genuine leak and it must still be alarmed and recorded."""
    from tests.fakes import FakeSandboxClient

    headers, user, project, app_row = await _project_with_app(db_session)
    sandbox = FakeSandboxClient()
    _wire_sandbox(app, sandbox)
    manager = _wire_manager(app)
    await _registry_names(fake_redis, user.id, app_row.id, "ready")

    lock = manager._start_lock_for(user.id)  # noqa: SLF001 — the route's own lock, by design
    await lock.acquire()
    try:
        with structlog.testing.capture_logs() as captured:
            resp = await _delete(client, project.id, headers)
    finally:
        lock.release()

    assert resp.status_code == 200
    assert _survived(captured, artefact="sandbox_container") == [_named(app_row.id)]
    assert await _teardown_record(db_session, project.id) == [
        {"artefact": "sandbox_container", "id": _named(app_row.id)}
    ]


async def test_an_unconfigured_sandbox_still_deletes_the_project(
    app: Any, client: AsyncClient, db_session: AsyncSession, fake_redis: Any
) -> None:
    # `.env.test` carries no `SANDBOX__*`, and neither does any dev machine — so this is the
    # DEFAULT posture, not a corner. `OptionalSandbox` hands the route `None`; the reap is
    # skipped with a line, and the delete is a plain 200.
    #
    # Mutation check: swap `OptionalSandbox` for `SandboxDep` on `delete_project` and this goes
    # red at dependency-solve time with `SandboxNotConfiguredError` — before the route body,
    # where no `except` of the route's can reach it — FastAPI solves dependencies eagerly, so a
    # required dependency bypasses every in-body error seam. That mistake has shipped here once
    # already.
    headers, user, project, app_row = await _project_with_app(db_session)
    _wire_sandbox(app, None)
    _wire_manager(app)
    await _registry_names(fake_redis, user.id, app_row.id, "ready")

    with structlog.testing.capture_logs() as captured:
        resp = await _delete(client, project.id, headers)

    assert resp.status_code == 200
    assert "detail" not in resp.json()  # not the catch-all envelope
    assert await db_session.get(Project, project.id) is None
    assert any(
        e.get("event") == "project_delete_sandbox_reap_skipped_unconfigured" for e in captured
    )
    # Nothing was reaped, so the record is exactly as it was.
    assert await fake_redis.exists(_registry(user.id)) == 1


async def test_a_busy_start_lock_leaves_the_container_standing_and_says_so(
    app: Any,
    client: AsyncClient,
    db_session: AsyncSession,
    fake_redis: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The lock is held across an ENTIRE provision (ACA create, image pull, bundle restore,
    # `wait_ready`), so an unbounded acquire parks a delete that has already committed behind
    # minutes of somebody else's build — and a browser giving up first cancels the request
    # mid-wait, losing the reap altogether. Bounded, then skipped, then logged.
    #
    # Mutation check: replace the `wait_for` with a bare `await lock.acquire()` and this goes
    # red — `_delete`'s own 10 s bound fires, because nothing ever releases that lock.
    import asyncio

    projects_router = _the_router_module()
    from tests.fakes import FakeSandboxClient

    # BOUNDED IS THE SHIPPED PROPERTY; the shrink below is only so the test does not sit out
    # the real wait. Asserted here so a future edit cannot quietly make it a minute.
    assert 0 < projects_router._SANDBOX_REAP_LOCK_WAIT_SECONDS <= 5
    monkeypatch.setattr(projects_router, "_SANDBOX_REAP_LOCK_WAIT_SECONDS", 0.05)

    headers, user, project, app_row = await _project_with_app(db_session)
    sandbox = FakeSandboxClient()
    _wire_sandbox(app, sandbox)
    manager = _wire_manager(app)
    await _registry_names(fake_redis, user.id, app_row.id, "ready")
    # Somebody else's provision owns the lock for the whole request.
    await manager._start_lock_for(user.id).acquire()
    try:
        with structlog.testing.capture_logs() as captured:
            resp = await asyncio.wait_for(
                client.request(
                    "DELETE",
                    f"/v1/projects/{project.id}",
                    headers=headers,
                    json=DELETE_BODY,
                ),
                timeout=5,
            )
    finally:
        manager._start_lock_for(user.id).release()

    assert resp.status_code == 200
    assert await db_session.get(Project, project.id) is None
    assert sandbox.torn_down == []
    # SKIPPING IS STILL RIGHT; PRETENDING SOMETHING WILL COLLECT IT WAS NOT. The container is
    # still up, so the arm alarms and the delete files the record. The scheduled reap only
    # destroys in production (`may_destroy_on_this_control_plane`), which is why this stopped
    # being "left to the scheduled sweep".
    assert _survived(captured, artefact="sandbox_container") == [_named(app_row.id)]
    assert await _teardown_record(db_session, project.id) == [
        {"artefact": "sandbox_container", "id": _named(app_row.id)}
    ]
    # The registry entry is KEPT, so a production sweep (or an operator) can still find it.
    assert await fake_redis.exists(_registry(user.id)) == 1


async def test_a_registry_entry_left_ending_is_still_torn_down(
    app: Any, client: AsyncClient, db_session: AsyncSession, fake_redis: Any
) -> None:
    # WHY THE CHECK IS NAME-EQUALITY AND NOT `_registry_serves_and_is_ready`. That helper also
    # demands `state == "ready"`, and `ending` is what an earlier FAILED teardown leaves behind
    # — an entry naming THIS project's own container, on a container that is still standing and
    # still billing. Sparing it is the exact leak the reap exists to close.
    #
    # Mutation check: swap the name comparison for `_registry_serves_and_is_ready(reg, name)`
    # and this goes red — nothing is torn down and the `ending` record survives forever.
    from tests.fakes import FakeSandboxClient

    headers, user, project, app_row = await _project_with_app(db_session)
    sandbox = FakeSandboxClient()
    _wire_sandbox(app, sandbox)
    _wire_manager(app)
    await _registry_names(fake_redis, user.id, app_row.id, "ending")

    resp = await _delete(client, project.id, headers)

    assert resp.status_code == 200
    assert sandbox.torn_down == [_named(app_row.id)]
    assert await fake_redis.exists(_registry(user.id)) == 0


async def test_a_raising_redis_during_the_reap_is_logged_and_the_delete_still_succeeds(
    app: Any,
    client: AsyncClient,
    db_session: AsyncSession,
    fake_redis: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # `reap_user` guards only `SandboxError` around the teardown — its Redis calls are bare by
    # module policy — so a blip propagates straight out of the post-commit section, which
    # `delete_project`'s own docstring forbids: a delete that already succeeded must not 500
    # because a drop failed. The explicit `except Exception` is the mechanism.
    #
    # Mutation check: delete that `except Exception` and this goes red with a 500 (and the rows
    # still gone, which is the whole problem).
    from redis.exceptions import RedisError

    projects_router = _the_router_module()
    from tests.fakes import FakeSandboxClient

    headers, user, project, app_row = await _project_with_app(db_session)
    sandbox = FakeSandboxClient()
    _wire_sandbox(app, sandbox)
    manager = _wire_manager(app)
    await _registry_names(fake_redis, user.id, app_row.id, "ready")

    async def _blip(*args: Any, **kwargs: Any) -> bool:
        raise RedisError("registry write blip mid-reap")

    monkeypatch.setattr(projects_router, "reap_user", _blip)
    with structlog.testing.capture_logs() as captured:
        resp = await _delete(client, project.id, headers)

    assert resp.status_code == 200
    assert "detail" not in resp.json()
    assert await db_session.get(Project, project.id) is None
    assert _survived(captured, artefact="sandbox_container") == [_named(app_row.id)]
    # ...and on the record, because a container nobody will collect is exactly what the record
    # is for.
    assert await _teardown_record(db_session, project.id) == [
        {"artefact": "sandbox_container", "id": _named(app_row.id)}
    ]
    # The lock is handed back even on the raising path, or the citizen's next build hangs.
    assert not manager._start_lock_for(user.id).locked()


async def test_an_arm_delete_that_raises_keeps_the_registry_entry_for_a_later_sweep(
    app: Any, client: AsyncClient, db_session: AsyncSession, fake_redis: Any
) -> None:
    # `reap_user`'s failure arm, reached through this route. The deletion becomes a debt: the
    # owed-row ledger takes it where it can, and where it CANNOT — no app owns the container, or
    # the record cannot say which instance it is — the registry stays exactly where it was so a
    # later sweep retries through it. That is this case: the project row is gone by the time the
    # teardown raises, so nothing can take the debt and sparing is the only honest answer.
    # `strict=False` is what keeps the raise inside the reaper rather than at a delete that has
    # already committed.
    from src.services.sandbox import SandboxError
    from tests.fakes import FakeSandboxClient

    headers, user, project, app_row = await _project_with_app(db_session)
    sandbox = FakeSandboxClient()
    sandbox.teardown_error = SandboxError("ARM said no")
    _wire_sandbox(app, sandbox)
    _wire_manager(app)
    await _registry_names(fake_redis, user.id, app_row.id, "ready")

    with structlog.testing.capture_logs() as captured:
        resp = await _delete(client, project.id, headers)

    assert resp.status_code == 200
    assert await db_session.get(Project, project.id) is None
    assert sandbox.torn_down == []  # it raised instead
    from src.services.redis.keys import REGISTRY_FIELD_APP_NAME

    survivor = await fake_redis.hgetall(_registry(user.id))
    assert survivor[REGISTRY_FIELD_APP_NAME] == _named(app_row.id)
    assert any(
        e.get("event") == "reaper teardown failed; the deletion is now a debt this platform owes"
        for e in captured
    )


async def test_an_empty_registry_reaps_nothing_and_raises_nothing(
    app: Any, client: AsyncClient, db_session: AsyncSession, fake_redis: Any
) -> None:
    # The ordinary case — the citizen has not built in a while and nothing is up. No ARM call,
    # no error, and no 500 from a `None` registry read.
    from tests.fakes import FakeSandboxClient

    headers, user, project, _app_row = await _project_with_app(db_session)
    sandbox = FakeSandboxClient()
    _wire_sandbox(app, sandbox)
    _wire_manager(app)
    assert await fake_redis.exists(_registry(user.id)) == 0

    with structlog.testing.capture_logs() as captured:
        resp = await _delete(client, project.id, headers)

    assert resp.status_code == 200
    assert await db_session.get(Project, project.id) is None
    assert sandbox.torn_down == []
    assert any(e.get("event") == "project_delete_sandbox_reap_skipped_not_ours" for e in captured)
    # NOTHING SURVIVED, so no alarm and no record: an empty registry means there was never a
    # container of this project's to leave behind, and a record that cried leak on every
    # ordinary delete is a record nobody would read.
    assert _survived(captured, artefact="sandbox_container") == []
    assert await _teardown_record(db_session, project.id) is None


async def test_the_per_user_lock_and_the_liveness_lease_go_with_a_successful_teardown(
    app: Any, client: AsyncClient, db_session: AsyncSession, fake_redis: Any
) -> None:
    # THE ARM THE PRE-EXISTING TESTS NEVER HAD TO CHECK. `LOCK_TTL_SECONDS = 900`, so a reap
    # that cleared only the registry would leave the citizen unable to start ANY sandbox for
    # fifteen minutes after deleting a project — manufacturing the very slot-taken state the
    # rest of this batch is fixing.
    #
    # THE LOCK IS SEEDED FROM INSIDE `teardown`, not before the request, and that is forced:
    # a lock present at guard time makes `refuse_while_build_session_live` answer 409 (the
    # registry names this app), so the delete would never reach the reap at all. A lock that
    # drifts into existence mid-reap is exactly the population `reap_lock` exists for — it
    # compare-and-deletes the OBSERVED value rather than a token it never held.
    from src.services.redis.keys import lease_key, lock_key
    from tests.fakes import FakeSandboxClient

    headers, user, project, app_row = await _project_with_app(db_session)

    class _LockDriftingSandbox(FakeSandboxClient):
        async def teardown(self, handle: Any) -> None:
            await fake_redis.set(lock_key(user.id), "a-token-nobody-here-holds")
            await super().teardown(handle)

    sandbox = _LockDriftingSandbox()
    _wire_sandbox(app, sandbox)
    _wire_manager(app)
    await _registry_names(fake_redis, user.id, app_row.id, "ready")
    await fake_redis.set(lease_key(user.id), "9999999999", ex=900)

    resp = await _delete(client, project.id, headers)

    assert resp.status_code == 200
    assert sandbox.torn_down == [_named(app_row.id)]
    assert await fake_redis.exists(_registry(user.id)) == 0
    assert await fake_redis.exists(lock_key(user.id)) == 0
    assert await fake_redis.exists(lease_key(user.id)) == 0


async def test_the_reap_displaces_nothing_that_the_delete_already_did(
    app: Any,
    client: AsyncClient,
    db_session: AsyncSession,
    fake_redis: Any,
    fake_storage: Any,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # ONE ASSERTION EACH for the four steps that were already there, so the new last step
    # cannot quietly displace one of them: the tombstone, the blob sweep, the per-app database
    # drop, and the published-container teardown. This is the only sandbox test that pays for a
    # real cluster database, and it pays for it precisely to keep the drop under assertion.
    projects_router = _the_router_module()
    from src.services.storage import snapshot_key
    from tests.fakes import FakeSandboxClient

    containers = _RecordingContainerStore()
    _override_container_store(app, containers)
    headers, user, project, app_row, record = await _project_with_database(db_session)
    fake_storage.objects[snapshot_key(app_row.id)] = b"# v2 git bundle"
    published: list[uuid.UUID] = []

    async def _record_published(app_ids: Any) -> list[uuid.UUID]:
        # Returns SURVIVORS, matching the helper — an empty list is "all of them went".
        published.extend(app_ids)
        return []

    monkeypatch.setattr(projects_router, "sweep_published_apps", _record_published)
    sandbox = FakeSandboxClient()
    _wire_sandbox(app, sandbox)
    _wire_manager(app)
    await _registry_names(fake_redis, user.id, app_row.id, "ready")

    resp = await _delete(client, project.id, headers)

    assert resp.status_code == 200
    # 1. the tombstone
    assert (
        await db_session.scalar(
            sa.select(DeletedProject).where(DeletedProject.project_id == project.id)
        )
    ) is not None
    # 2. the blob sweep
    assert snapshot_key(app_row.id) not in fake_storage.objects
    # 3. the per-app database
    assert await _catalog(_DATABASE_EXISTS, db=record.db_name) is False
    # 4. the published container app
    assert published == [app_row.id]
    # ...and 5, the new one.
    assert sandbox.torn_down == [_named(app_row.id)]


# --- the built IMAGE goes with the project too ---------------------------------------------
#
# The last artefact on the path, and the one that still holds the citizen's source: a published
# app's image carries its compiled tree. The dialog promises "destroyed permanently", so a
# repository left standing under a name no row points at any more makes that copy false.
#
# THE SCOPE OF THE REGISTRY CALL IS PINNED IN `tests/services/deploy/test_registry_delete.py`,
# on the request. These are the route's own questions: is it called, with the project's app,
# and what happens to the delete when the registry says no.


async def test_the_delete_takes_the_apps_registry_repository_with_it(
    app: Any, client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    projects_router = _the_router_module()
    headers, user, project, app_row = await _project_with_app(db_session)
    # An app that was actually DEPLOYED — the only kind that can have an image in the registry,
    # since `names.image_tag` composes the push tag from the deployment id.
    db_session.add(Deployment(app_id=app_row.id, user_id=user.id))
    await db_session.commit()
    seen: list[tuple[list[uuid.UUID], Any]] = []

    async def _record_repositories(app_ids: Any, *, config: Any) -> list[str]:
        seen.append((list(app_ids), config))
        return []

    monkeypatch.setattr(projects_router, "sweep_app_repositories", _record_repositories)

    resp = await _delete(client, project.id, headers)

    assert resp.status_code == 200
    # The project's OWN app, from the pre-commit id list — after the cascade there is nothing
    # left in the database that names the repository.
    assert seen == [([app_row.id], None)]
    # Nothing survived, so nothing is recorded: the record has to stay empty on an ordinary
    # delete or it is noise an operator learns to ignore.
    assert await _teardown_record(db_session, project.id) is None


async def test_the_delete_does_not_ask_the_registry_about_an_app_never_deployed(
    app: Any, client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ AN APP THAT WAS NEVER DEPLOYED HAS NO REPOSITORY, so it is not in the sweep.

    Asking anyway is not free. A registry that refuses the delete credential answers 401/403
    for whatever it is handed, and every id in the sweep comes back a SURVIVOR — so on a
    misconfigured deployment every project delete would file a `project:teardown-incomplete`
    row naming a repository that never existed, and send an operator after it. The sibling
    sweeps' own docstrings put it plainly: naming something that was in fact deleted is worse
    than naming nothing.

    Mutation check: hand `cleanup.app_container_ids` to the sweep again and this goes red."""
    projects_router = _the_router_module()
    headers, _user, project, _app_row = await _project_with_app(db_session)  # no Deployment row
    seen: list[list[uuid.UUID]] = []

    async def _record_repositories(app_ids: Any, *, config: Any) -> list[str]:
        seen.append(list(app_ids))
        return []

    monkeypatch.setattr(projects_router, "sweep_app_repositories", _record_repositories)

    resp = await _delete(client, project.id, headers)

    assert resp.status_code == 200
    # STILL CALLED — one code path with nothing in it, rather than a branch that can drift.
    assert seen == [[]]
    assert await db_session.get(Project, project.id) is None


async def test_publishing_switched_off_still_deletes_the_project_and_records_nothing(
    app: Any, client: AsyncClient, db_session: AsyncSession
) -> None:
    # NO MONKEYPATCH: `.env.test` carries no `DEPLOY__*`, so `settings.deploy is None` and the
    # real function runs its one skip arm. This is the fixture-off posture the rules require a
    # test for — publishing is genuinely optional outside production, and a skip that reported
    # a leak would put a note on every delete in dev and test.
    from src.config import settings

    assert settings.deploy is None
    headers, _user, project, _app_row = await _project_with_app(db_session)

    resp = await _delete(client, project.id, headers)

    assert resp.status_code == 200
    assert await db_session.get(Project, project.id) is None
    assert await _teardown_record(db_session, project.id) is None


async def test_a_registry_that_refuses_the_delete_leaves_it_successful_and_on_the_record(
    app: Any, client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # THE ARM THIS SHIPS AGAINST: a credential without `content/delete`. The citizen's project
    # is already gone — the rows committed several steps ago — so the delete stands, and the
    # surviving image goes on the record for a developer to chase the permission with.
    #
    # Mutation check: have `_record_what_survived` return early unconditionally and this goes
    # red on the record while the 200 still passes, which is exactly the failure being guarded.
    projects_router = _the_router_module()
    headers, _user, project, app_row = await _project_with_app(db_session)
    survivor = f"citizen-apps/{app_row.id}"

    async def _refused(app_ids: Any, *, config: Any) -> list[str]:
        return [survivor]

    monkeypatch.setattr(projects_router, "sweep_app_repositories", _refused)

    resp = await _delete(client, project.id, headers)

    assert resp.status_code == 200
    assert "detail" not in resp.json()  # not the catch-all envelope
    assert await db_session.get(Project, project.id) is None
    assert await _teardown_record(db_session, project.id) == [
        {"artefact": "registry_repository", "id": survivor}
    ]


async def test_the_citizens_own_reason_is_left_exactly_as_they_wrote_it(
    app: Any, client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The operator's note goes in the AUDIT LOG, never appended into `remark`. That column
    # is the citizen's own words and nothing else — an administrator reads it to learn why
    # somebody deleted something, and an appended note would need a delimiter convention and a
    # parser to get back out.
    projects_router = _the_router_module()
    headers, _user, project, _app_row = await _project_with_app(db_session)

    async def _refused(app_ids: Any, *, config: Any) -> list[str]:
        return ["citizen-apps/kept"]

    monkeypatch.setattr(projects_router, "sweep_app_repositories", _refused)

    assert (await _delete(client, project.id, headers)).status_code == 200

    tombstone = await db_session.scalar(
        sa.select(DeletedProject).where(DeletedProject.project_id == project.id)
    )
    assert tombstone is not None
    assert tombstone.remark == DELETE_BODY["remark"]


async def test_a_record_that_cannot_be_written_still_leaves_the_delete_successful(
    app: Any, client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The record is written in its OWN transaction, long after the delete committed. A failure
    # there must not turn a delete that genuinely succeeded into a 500 — the leak is already on
    # the log, which is the notice; this row is only the record.
    projects_router = _the_router_module()
    headers, _user, project, _app_row = await _project_with_app(db_session)

    async def _refused(app_ids: Any, *, config: Any) -> list[str]:
        return ["citizen-apps/kept"]

    # PATCHED WHERE THE RECORD ACTUALLY WRITES, not on the router. `record_what_survived` moved
    # into `services/audit/teardown.py` when the admin hard-delete became its second caller, so
    # it resolves `append_audit` through that module's own binding; patching the router's would
    # silently miss and this test would pin nothing.
    record_module = importlib.import_module("src.services.audit.teardown")
    real_append = record_module.append_audit

    async def _explode_on_the_record(*args: Any, **kwargs: Any) -> Any:
        # ONLY the record's own write. The two pre-commit rows this path already writes must
        # still land, or the test would be pinning a 500 from the wrong failure entirely.
        if kwargs.get("action") == "project:teardown-incomplete":
            raise RuntimeError("audit insert boom")
        return await real_append(*args, **kwargs)

    monkeypatch.setattr(projects_router, "sweep_app_repositories", _refused)
    monkeypatch.setattr(record_module, "append_audit", _explode_on_the_record)

    with structlog.testing.capture_logs() as captured:
        resp = await _delete(client, project.id, headers)

    assert resp.status_code == 200
    assert await db_session.get(Project, project.id) is None
    assert any(e.get("event") == "project_teardown_record_failed" for e in captured)
