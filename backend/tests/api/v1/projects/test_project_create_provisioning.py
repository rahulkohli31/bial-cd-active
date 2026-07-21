"""U3 — creating a project provisions its own database (ADR-0028, R3/R4).

Real cluster, no fakes: `POST /v1/projects` runs the whole external sequence, so these
assert against `project_databases` AND the PostgreSQL catalogs. Every test that provisions
registers its project with `salted` first — the `db_session` rollback cannot undo cluster
DDL done on the separate AUTOCOMMIT engine.

The load-bearing contract here is that provisioning is a POST-COMMIT, BEST-EFFORT side
effect: it must never strand the project, never 500 the endpoint, and never resurrect an
`appId` into the 201 body.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import sqlalchemy as sa
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings
from src.db.models.project import Project
from src.db.models.project_database import ProjectDatabase
from src.services.appdb import provision as provision_module
from src.services.appdb.engine import (
    get_maintenance_engine,
    reset_maintenance_engine_for_tests,
)
from src.services.appdb.names import database_name, role_name
from src.services.appdb.provision import ensure_project_database
from src.services.appdb.teardown import salt_the_earth
from src.services.auth.session_jwt import mint_session_jwt
from tests.factories import UserFactory

_DB_EXISTS = "SELECT EXISTS (SELECT 1 FROM pg_database WHERE datname = :db)"
_TTL = settings.auth.access_ttl_seconds


@pytest.fixture
async def salted() -> AsyncIterator[list[uuid.UUID]]:
    """Destroy the databases/roles a test provisions. Register BEFORE provisioning — the
    teardown is idempotent and never raises, so registering an id that was never provisioned
    costs nothing while a failure mid-provision still gets cleaned up."""
    project_ids: list[uuid.UUID] = []
    try:
        yield project_ids
    finally:
        for project_id in project_ids:
            await salt_the_earth(
                db_name=database_name(project_id), role_name=role_name(project_id)
            )
        await reset_maintenance_engine_for_tests()


async def _auth(db: AsyncSession, email: str) -> dict[str, str]:
    user = await UserFactory.create(db, email=email)
    return {"Cookie": f"session={mint_session_jwt(user.id, user.token_version, _TTL)}"}


async def _marker(db: AsyncSession, project_id: uuid.UUID) -> ProjectDatabase | None:
    stmt = (
        sa.select(ProjectDatabase)
        .where(ProjectDatabase.project_id == project_id)
        .execution_options(populate_existing=True)
    )
    return (await db.execute(stmt)).scalar_one_or_none()


async def _database_exists(db_name: str) -> bool:
    engine = get_maintenance_engine()
    assert engine is not None
    async with engine.connect() as conn:
        return bool(await conn.scalar(sa.text(_DB_EXISTS), {"db": db_name}))


# --- happy path -------------------------------------------------------------------------


async def test_create_project_provisions_a_ready_database(
    client: AsyncClient, db_session: AsyncSession, salted: list[uuid.UUID]
) -> None:
    headers = await _auth(db_session, "u3-create@rvaiglobal.com")

    resp = await client.post("/v1/projects", headers=headers, json={"name": "Gate Ops"})

    assert resp.status_code == 201
    created = resp.json()
    project_id = uuid.UUID(created["id"])
    salted.append(project_id)

    marker = await _marker(db_session, project_id)
    assert marker is not None
    assert marker.db_ready is True  # the TERMINAL marker, written last
    assert marker.provisioned_at is not None
    assert marker.db_name == database_name(project_id)
    assert await _database_exists(marker.db_name) is True


async def test_create_project_still_reports_no_app(
    client: AsyncClient, db_session: AsyncSession, salted: list[uuid.UUID]
) -> None:
    # The fresh-project contract is inviolable (D0): the DATABASE is project-keyed and exists
    # from create, while the app row stays lazily minted at first build. Provisioning must not
    # smuggle an app into the 201 body.
    headers = await _auth(db_session, "u3-noapp@rvaiglobal.com")

    created = (
        await client.post("/v1/projects", headers=headers, json={"name": "No App Yet"})
    ).json()
    salted.append(uuid.UUID(created["id"]))

    assert created["appId"] is None
    assert created["appStatus"] is None


# --- edge: the substrate is not configured at all ----------------------------------------


async def test_create_project_without_a_substrate_is_a_plain_201(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A deployment with no APP_DB__* is supported: projects simply have no database. The
    # cached engine must be dropped BOTH sides — `get_maintenance_engine` memoizes, so a
    # previous test's engine would keep the feature switched on regardless of settings.
    await reset_maintenance_engine_for_tests()
    monkeypatch.setattr(settings, "app_db", None)
    headers = await _auth(db_session, "u3-unconfigured@rvaiglobal.com")
    try:
        resp = await client.post("/v1/projects", headers=headers, json={"name": "Substrate Off"})

        assert resp.status_code == 201
        project_id = uuid.UUID(resp.json()["id"])
        assert await db_session.get(Project, project_id) is not None
        # No row at all — "never provisioned" reads as ABSENCE, not as a not-ready marker.
        assert await _marker(db_session, project_id) is None
    finally:
        await reset_maintenance_engine_for_tests()


# --- error: provisioning blows up mid-sequence -------------------------------------------


async def test_a_failed_provision_leaves_a_live_project_and_a_non_terminal_marker(
    client: AsyncClient,
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    salted: list[uuid.UUID],
) -> None:
    # Fail the LAST external step, so the role and database really were created and only the
    # terminal marker is missing — the nastiest shape, because everything looks done.
    calls: list[int] = []

    async def _boom(conn: object, *, db_name: str) -> None:
        calls.append(1)
        raise RuntimeError("simulated substrate failure")

    monkeypatch.setattr(provision_module, "_stamp_provisioned_at", _boom)
    headers = await _auth(db_session, "u3-boom@rvaiglobal.com")

    resp = await client.post("/v1/projects", headers=headers, json={"name": "Doomed Provision"})

    # The create SUCCEEDS: a substrate hiccup is degraded state, never a failed create.
    assert resp.status_code == 201
    project_id = uuid.UUID(resp.json()["id"])
    salted.append(project_id)
    assert calls == [1]
    assert await db_session.get(Project, project_id) is not None

    marker = await _marker(db_session, project_id)
    assert marker is not None
    assert marker.db_ready is False  # nothing claims readiness
    assert marker.provisioned_at is None

    # ...and the retry converges: the whole sequence is idempotent, so the next ensure
    # (which every build start performs) re-runs it and publishes the terminal marker.
    monkeypatch.undo()
    healed = await ensure_project_database(db_session, project_id)
    assert healed is not None
    assert healed.db_ready is True
    assert await _database_exists(healed.db_name) is True


async def test_a_provision_that_never_claims_still_returns_201(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The other failure shape: it blows up before the claim, so there is no marker row at all
    # and no cluster object to clean up. Still a 201, still a real project.
    async def _boom(db: object, project_id: uuid.UUID) -> bool:
        raise RuntimeError("claim exploded")

    monkeypatch.setattr(provision_module, "_claim", _boom)
    headers = await _auth(db_session, "u3-noclaim@rvaiglobal.com")

    resp = await client.post("/v1/projects", headers=headers, json={"name": "No Claim"})

    assert resp.status_code == 201
    project_id = uuid.UUID(resp.json()["id"])
    assert await db_session.get(Project, project_id) is not None
    assert await _marker(db_session, project_id) is None
