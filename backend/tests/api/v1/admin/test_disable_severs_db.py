"""U5 — the admin database levers: `disable` severs, `enable` restores, the reveal
endpoint hands over the DSN, and `hard_delete` salts the earth after committing (ADR-0028).

Real cluster, real connections, no fakes on the database side. That is not thoroughness
theatre: after the shared `data_records` plane is retired, `disable`'s sever is the ONLY
data kill for a deployed app — there is no `X-App-Key` 403 left to fall back on — so the
thing under test is whether a container that is already connected, or is about to
reconnect, actually loses its data. A test that asserted "the endpoint returned 200" would
prove nothing about that.

The reconnecting-pool test is the load-bearing one. `pg_terminate_backend` alone is not a
kill — a connection pool's entire job is to route around a dropped socket — so the test
that matters is the one where the pool tries to heal itself and is refused. That is also
why `sever` locks the door (`NOLOGIN` + `REVOKE CONNECT`) BEFORE terminating.

Every project provisioned here is destroyed by the session-scoped
`_salt_every_provisioned_app_database` hook in `tests/conftest.py`, which records ids at
`provision._claim` — the `db_session` rollback cannot undo cluster DDL done on the separate
AUTOCOMMIT engine.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from typing import Any

import asyncpg
import pytest
import sqlalchemy as sa
import structlog
from httpx import AsyncClient
from sqlalchemy import event
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from src.api.deps import storage_dependency, storage_or_none_dependency
from src.config import settings
from src.db.models.app_registry import AppRegistry, AppStatus
from src.db.models.audit import AuditLog
from src.db.models.project_database import ProjectDatabase
from src.services.appdb import teardown as appdb_teardown
from src.services.appdb.engine import get_maintenance_engine
from src.services.appdb.provision import control_plane_dsn, ensure_project_database
from src.services.appdb.secrets import decrypt_password
from src.services.appdb.teardown import sever
from src.services.auth.session_jwt import mint_session_jwt
from src.services.storage import snapshot_key
from tests.factories import AppRegistryFactory, ProjectFactory, UserFactory
from tests.fakes import FakeStorage
from tests.services.appdb.helpers import scalar_on

_TTL = settings.auth.access_ttl_seconds
_SHA = "1f" * 20
_DATABASE_EXISTS = "SELECT EXISTS (SELECT 1 FROM pg_database WHERE datname = :db)"
_ROLE_EXISTS = "SELECT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = :role)"


def _cookie(jwt: str) -> dict[str, str]:
    return {"Cookie": f"session={jwt}"}


async def _admin(db: AsyncSession) -> dict[str, str]:
    # The .env.test allowlist contains admin@bial.com → super-admin.
    user = await UserFactory.create(db, email="admin@bial.com")
    return _cookie(mint_session_jwt(user.id, user.token_version, _TTL))


async def _citizen(db: AsyncSession) -> dict[str, str]:
    user = await UserFactory.create(db, email="nobody@rvaiglobal.com")
    return _cookie(mint_session_jwt(user.id, user.token_version, _TTL))


def _approved(**extra: Any) -> dict[str, Any]:
    sid = uuid.uuid4()
    return {
        "status": AppStatus.APPROVED,
        "source_submission_id": sid,
        "source_commit_sha": _SHA,
        "submitted_at": datetime.now(UTC),
        "approved_submission_id": sid,
        "approved_commit_sha": _SHA,
        **extra,
    }


async def _app_row(db: AsyncSession, **overrides: Any) -> AppRegistry:
    owner = await UserFactory.create(db)
    project = await ProjectFactory.create(db, owner.id)
    return await AppRegistryFactory.create(
        db, user_id=owner.id, project_id=project.id, **overrides
    )


async def _with_database(
    db: AsyncSession, **overrides: Any
) -> tuple[AppRegistry, ProjectDatabase]:
    """An app whose project owns a REAL provisioned database."""
    row = await _app_row(db, **overrides)
    record = await ensure_project_database(db, row.project_id)
    assert record is not None, "APP_DB__* must be configured in .env.test"
    return row, record


async def _audit_rows(db: AsyncSession, action: str) -> list[AuditLog]:
    return list(
        (await db.execute(sa.select(AuditLog).where(AuditLog.action == action))).scalars().all()
    )


async def _catalog(sql: str, **params: Any) -> Any:
    engine = get_maintenance_engine()
    assert engine is not None
    async with engine.connect() as conn:
        return await conn.scalar(sa.text(sql), params)


def _wire_storage(app: Any) -> FakeStorage:
    store = FakeStorage()
    app.dependency_overrides[storage_dependency] = lambda: store
    app.dependency_overrides[storage_or_none_dependency] = lambda: store
    return store


# --- disable: the kill switch ------------------------------------------------------------


async def test_disable_severs_a_live_session_and_refuses_the_next_connection(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    row, record = await _with_database(db_session, **_approved())
    headers = await _admin(db_session)
    dsn = control_plane_dsn(record)

    live = create_async_engine(dsn, poolclass=NullPool)
    try:
        async with live.connect() as connection:
            assert await connection.scalar(sa.text("SELECT 1")) == 1

            resp = await client.post(f"/v1/admin/apps/{row.id}/disable", headers=headers)
            assert resp.status_code == 200
            assert resp.json()["status"] == "disabled"

            # The in-flight session — the deployed container mid-request — is terminated.
            with pytest.raises(DBAPIError):
                await connection.scalar(sa.text("SELECT 1"))
    finally:
        await live.dispose()

    # ...and a brand-new connection attempt is refused outright.
    with pytest.raises(asyncpg.InvalidAuthorizationSpecificationError):
        await scalar_on(dsn, "SELECT 1")


async def test_a_reconnecting_pool_finds_the_door_already_locked(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    # The generated app talks to its database through node-postgres'
    # `new Pool({ max: 4, idleTimeoutMillis: 30_000 })`, which parks idle connections and
    # transparently opens a replacement the moment one dies. Modelled here with a real
    # 4-slot pool plus pre-ping (the pool's own liveness check): terminating backends is NOT
    # a kill on its own, because a pool's whole job is to route around a dropped socket. The
    # kill is the revoke, and this is what proves the pool cannot self-heal past it.
    #
    # (The narrower race the sever's ORDER exists for — a reconnect landing between the
    # terminate and the revoke — is unreachable by construction rather than merely unlikely,
    # and is not what this test times.)
    row, record = await _with_database(db_session, **_approved())
    headers = await _admin(db_session)
    dsn = control_plane_dsn(record)

    pool = create_async_engine(dsn, pool_size=4, max_overflow=0, pool_pre_ping=True)
    try:
        async with pool.connect() as connection:
            assert await connection.scalar(sa.text("SELECT 1")) == 1
        # The connection is now parked in the pool — the app between two requests.

        assert (
            await client.post(f"/v1/admin/apps/{row.id}/disable", headers=headers)
        ).status_code == 200

        # Refused, and refused for the RIGHT reason: an auth rejection, not a stale-socket
        # error that a second attempt would sail past.
        with pytest.raises(asyncpg.InvalidAuthorizationSpecificationError):
            async with pool.connect() as connection:
                await connection.scalar(sa.text("SELECT 1"))
    finally:
        await pool.dispose()


async def test_sever_locks_the_door_before_it_terminates_anyone(db_session: AsyncSession) -> None:
    # The reconnect test above proves the OUTCOME; this pins the ORDER that produces it.
    # `NOLOGIN` + `REVOKE CONNECT` must be issued BEFORE `pg_terminate_backend`, because
    # terminating first hands a reconnecting pool a fresh, fully-privileged connection in the
    # window between the two statements. That window is invisible to a behavioural test — a
    # terminate-then-revoke implementation passes the reconnect test above — so the invariant
    # the whole design rests on is asserted directly against the statement stream.
    _row, record = await _with_database(db_session)
    engine = get_maintenance_engine()
    assert engine is not None

    issued: list[str] = []

    def _record_statement(conn, cursor, statement, parameters, context, executemany):
        issued.append(" ".join(statement.split()))

    event.listen(engine.sync_engine, "before_cursor_execute", _record_statement)
    try:
        await sever(db_name=record.db_name, role_name=record.role_name)
    finally:
        event.remove(engine.sync_engine, "before_cursor_execute", _record_statement)

    steps = [
        statement
        for statement in issued
        if "NOLOGIN" in statement
        or "REVOKE CONNECT" in statement
        or "pg_terminate_backend" in statement
    ]
    assert len(steps) == 3, f"expected exactly the three sever statements, got {steps}"
    assert "NOLOGIN" in steps[0], f"the role must be locked FIRST, not {steps[0]}"
    assert "REVOKE CONNECT" in steps[1], f"CONNECT must be revoked SECOND, not {steps[1]}"
    assert "pg_terminate_backend" in steps[2], f"backends must be killed LAST, not {steps[2]}"


async def test_disable_audits_the_names_and_never_the_credential(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    row, record = await _with_database(db_session, **_approved())
    headers = await _admin(db_session)
    password = decrypt_password(record.password_encrypted)

    await client.post(f"/v1/admin/apps/{row.id}/disable", headers=headers)

    revoked = await _audit_rows(db_session, "db:revoke")
    assert len(revoked) == 1
    detail = revoked[0].detail
    assert detail is not None
    assert detail["dbName"] == record.db_name
    assert detail["roleName"] == record.role_name
    # Project-scoped row, and `appId` is what keeps it visible in the app's audit drawer.
    assert revoked[0].resource_type == "project"
    assert revoked[0].resource_id == str(row.project_id)
    assert detail["appId"] == str(row.id)
    assert password not in json.dumps(detail)

    # The drawer really does surface it (`read_audit` matches `detail["appId"]`).
    events = (await client.get(f"/v1/admin/apps/{row.id}/audit", headers=headers)).json()
    assert "db:revoke" in {event["action"] for event in events["events"]}


async def test_disable_for_an_app_with_no_database_is_a_clean_no_op(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    # The unconfigured era: an app whose project was created before per-project databases
    # existed has no registry row at all. Absence is a supported state, not a failure.
    row = await _app_row(db_session, **_approved())

    resp = await client.post(f"/v1/admin/apps/{row.id}/disable", headers=await _admin(db_session))

    assert resp.status_code == 200
    assert resp.json()["status"] == "disabled"
    assert await _audit_rows(db_session, "db:revoke") == []


# --- enable: the restore -----------------------------------------------------------------


async def test_enable_restores_connections(client: AsyncClient, db_session: AsyncSession) -> None:
    row, record = await _with_database(db_session, **_approved())
    headers = await _admin(db_session)
    dsn = control_plane_dsn(record)
    assert (
        await client.post(f"/v1/admin/apps/{row.id}/disable", headers=headers)
    ).status_code == 200

    resp = await client.post(f"/v1/admin/apps/{row.id}/enable", headers=headers)

    assert resp.status_code == 200
    assert resp.json()["status"] == "approved"
    assert await scalar_on(dsn, "SELECT 1") == 1
    restored = await _audit_rows(db_session, "db:restore")
    assert len(restored) == 1
    assert restored[0].detail == {
        "appId": str(row.id),
        "dbName": record.db_name,
        "roleName": record.role_name,
    }


async def test_a_refused_enable_leaves_the_database_severed(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    # `→approved` legally accepts PENDING, so the Python status pre-check is the only thing
    # standing between `enable` and an approve-gate bypass. If the restore ran outside that
    # guard, an unvetted app's owner could re-open a database an admin had just sealed.
    row, record = await _with_database(db_session, status=AppStatus.PENDING)
    dsn = control_plane_dsn(record)
    await sever(db_name=record.db_name, role_name=record.role_name)

    resp = await client.post(f"/v1/admin/apps/{row.id}/enable", headers=await _admin(db_session))

    assert resp.status_code == 409
    assert await _audit_rows(db_session, "db:restore") == []
    with pytest.raises(asyncpg.InvalidAuthorizationSpecificationError):
        await scalar_on(dsn, "SELECT 1")


# --- the reveal: POST /v1/admin/apps/{app_id}/database-credential ------------------------


async def test_reveal_hands_a_superadmin_a_working_dsn(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    row, record = await _with_database(db_session, **_approved())

    resp = await client.post(
        f"/v1/admin/apps/{row.id}/database-credential", headers=await _admin(db_session)
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["dbName"] == record.db_name
    assert body["roleName"] == record.role_name
    # The `BIAL_DATABASE_URL` form the runbook pastes: node-postgres cannot parse
    # SQLAlchemy's `postgresql+asyncpg://` driver selector.
    assert body["dsn"].startswith("postgresql://")
    assert "+asyncpg" not in body["dsn"]
    # It is not merely well-formed — it connects.
    assert await scalar_on(control_plane_dsn(record), "SELECT 1") == 1


async def test_the_reveal_audit_row_carries_no_secret(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    row, record = await _with_database(db_session, **_approved())
    password = decrypt_password(record.password_encrypted)

    resp = await client.post(
        f"/v1/admin/apps/{row.id}/database-credential", headers=await _admin(db_session)
    )

    revealed = await _audit_rows(db_session, "db:reveal")
    assert len(revealed) == 1
    detail = revealed[0].detail
    assert detail is not None
    assert detail["roleName"] == record.role_name
    assert detail["host"] == resp.json()["host"] and detail["host"]
    # WHO and WHERE, never WITH WHAT — the DSN differs from this row by exactly the password.
    serialized = json.dumps(detail)
    assert password not in serialized
    assert resp.json()["dsn"] not in serialized


async def test_reveal_is_superadmin_only(client: AsyncClient, db_session: AsyncSession) -> None:
    row, _record = await _with_database(db_session, **_approved())

    resp = await client.post(
        f"/v1/admin/apps/{row.id}/database-credential", headers=await _citizen(db_session)
    )

    assert resp.status_code == 403
    assert await _audit_rows(db_session, "db:reveal") == []


async def test_reveal_for_a_project_without_a_database_is_a_409(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    # A state, not a bug: no row means never provisioned. A 500 here would send an operator
    # hunting a platform fault that does not exist.
    row = await _app_row(db_session, **_approved())

    resp = await client.post(
        f"/v1/admin/apps/{row.id}/database-credential", headers=await _admin(db_session)
    )

    assert resp.status_code == 409


async def test_reveal_refuses_a_database_that_never_finished_provisioning(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    # `db_ready = false` means the external sequence died partway, so the cross-app wall may
    # never have gone up. Handing that DSN to an operator would publish a database that is
    # not yet isolated.
    row, record = await _with_database(db_session, **_approved())
    await db_session.execute(
        sa.update(ProjectDatabase).where(ProjectDatabase.id == record.id).values(db_ready=False)
    )

    resp = await client.post(
        f"/v1/admin/apps/{row.id}/database-credential", headers=await _admin(db_session)
    )

    assert resp.status_code == 409


# --- hard delete: the admin danger-op ----------------------------------------------------


async def test_hard_delete_salts_the_earth_after_committing(
    app: Any, client: AsyncClient, db_session: AsyncSession
) -> None:
    store = _wire_storage(app)
    row, record = await _with_database(db_session, **_approved())
    store.objects[snapshot_key(row.id)] = b"bundle"
    headers = await _admin(db_session)

    resp = await client.delete(f"/v1/admin/apps/{row.id}", headers=headers)

    assert resp.json() == {"ok": True}
    assert await db_session.get(AppRegistry, row.id) is None
    # The PROJECT survives an app hard-delete, so its registry row is deleted explicitly —
    # absence is what makes the next build re-provision instead of injecting a dead DSN.
    assert await db_session.get(ProjectDatabase, record.id) is None
    assert await _catalog(_DATABASE_EXISTS, db=record.db_name) is False
    assert await _catalog(_ROLE_EXISTS, role=record.role_name) is False
    dropped = await _audit_rows(db_session, "db:drop")
    assert len(dropped) == 1
    assert dropped[0].detail == {
        "appId": str(row.id),
        "dbName": record.db_name,
        "roleName": record.role_name,
    }


async def test_a_failing_drop_never_un_deletes_the_registry(
    app: Any,
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Post-commit best-effort: by the time the drop runs the commit is durable, so raising
    # would 500 a delete that in fact succeeded. The database becomes a logged orphan for
    # the reconciler (U7) instead.
    _wire_storage(app)
    row, record = await _with_database(db_session, **_approved())

    async def explode(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("simulated DROP DATABASE failure")

    monkeypatch.setattr(appdb_teardown, "_drop_database", explode)
    with structlog.testing.capture_logs() as captured:
        resp = await client.delete(f"/v1/admin/apps/{row.id}", headers=await _admin(db_session))

    assert resp.json() == {"ok": True}
    assert await db_session.get(AppRegistry, row.id) is None
    assert any(e.get("event") == "app_database_drop_database_failed" for e in captured)
    assert await _catalog(_DATABASE_EXISTS, db=record.db_name) is True  # the orphan
