"""U7 — `POST /v1/admin/apps/reconcile-databases` and the advisory size column.

Superadmin-gated, audited, and REPORT-ONLY: the endpoint-level pin is that a seeded orphan
shows up in the response AND is still on the cluster when the request is over. The
classifier's guards are proven exhaustively service-side
(`tests/services/appdb/test_reconcile.py`); what is proven here is the API contract —
counts-only body, counts-only audit `detail`, and the 503 seam that must never degrade into
a partial report or an undocumented 500.

Real cluster, real databases. Every project provisioned here is destroyed by the
session-scoped `_salt_every_provisioned_app_database` hook in `tests/conftest.py`.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
import sqlalchemy as sa
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

import src.api.v1.admin.router as admin_router
from src.config import settings
from src.db.models.app_registry import AppRegistry
from src.db.models.audit import AuditLog
from src.db.models.project_database import ProjectDatabase
from src.services.appdb.engine import (
    get_maintenance_engine,
    reset_maintenance_engine_for_tests,
)
from src.services.appdb.provision import ensure_project_database
from src.services.appdb.reconcile import advisory_database_sizes
from src.services.auth.session_jwt import mint_session_jwt
from tests.factories import AppRegistryFactory, ProjectFactory, UserFactory

_TTL = settings.auth.access_ttl_seconds
_RECONCILE = "/v1/admin/apps/reconcile-databases"
_LIST = "/v1/admin/apps"
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


async def _app_row(db: AsyncSession, **overrides: Any) -> AppRegistry:
    owner = await UserFactory.create(db)
    project = await ProjectFactory.create(db, owner.id)
    return await AppRegistryFactory.create(
        db, user_id=owner.id, project_id=project.id, **overrides
    )


async def _with_database(db: AsyncSession) -> tuple[AppRegistry, ProjectDatabase]:
    row = await _app_row(db)
    record = await ensure_project_database(db, row.project_id)
    assert record is not None, "APP_DB__* must be configured in .env.test"
    return row, record


async def _catalog_scalar(sql: str, **params: Any) -> Any:
    engine = get_maintenance_engine()
    assert engine is not None
    async with engine.connect() as conn:
        return await conn.scalar(sa.text(sql), params)


@pytest.fixture
async def no_substrate(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[None]:
    """The supported unconfigured deployment. The cached engine is dropped on BOTH sides:
    on the way in so the accessor re-reads the patched settings, on the way out so the next
    test does not inherit an engine built from them."""
    await reset_maintenance_engine_for_tests()
    monkeypatch.setattr(settings, "app_db", None)
    try:
        yield
    finally:
        await reset_maintenance_engine_for_tests()


# --- the report ---------------------------------------------------------------------------


async def test_a_clean_sweep_reports_typed_counts(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    resp = await client.post(_RECONCILE, headers=await _admin(db_session))

    assert resp.status_code == 200
    body = resp.json()
    assert set(body) == {"databases", "roles"}
    assert set(body["databases"]) == {
        "scanned",
        "notOurs",
        "owned",
        "orphaned",
        "unknownAge",
        "oldestOrphanAgeHours",
    }
    assert set(body["roles"]) == {"scanned", "notOurs", "owned", "stranded", "paired"}
    databases = body["databases"]
    assert (
        databases["scanned"]
        == databases["notOurs"]
        + databases["owned"]
        + databases["orphaned"]
        + databases["unknownAge"]
    )
    roles = body["roles"]
    assert (
        roles["scanned"] == roles["notOurs"] + roles["owned"] + roles["stranded"] + roles["paired"]
    )


async def test_an_orphan_is_reported_and_nothing_is_deleted(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    # The safety pin, at the API boundary: a real orphaned database + role, reported, and
    # still on the cluster once the request has returned. Report-only is the mechanism.
    headers = await _admin(db_session)
    before = (await client.post(_RECONCILE, headers=headers)).json()
    _row, record = await _with_database(db_session)
    # Exactly what a failed `salt_the_earth` leaves: the registry forgets, the cluster does not.
    await db_session.execute(sa.delete(ProjectDatabase).where(ProjectDatabase.id == record.id))

    body = (await client.post(_RECONCILE, headers=headers)).json()

    # `>=`, not `==`: `.env.test` configures a real substrate, so every project any test in
    # the session creates leaves a real database on this shared cluster (the conftest hook
    # salts them only at session end) and the total is not a stable measurement. The EXACT
    # per-name verdict is pinned service-side in `tests/services/appdb/test_reconcile.py`.
    assert body["databases"]["orphaned"] >= before["databases"]["orphaned"] + 1
    # THE contract: reported, and still there. Report-only is the mechanism, not a phase.
    assert await _catalog_scalar(_DATABASE_EXISTS, db=record.db_name) is True
    assert await _catalog_scalar(_ROLE_EXISTS, role=record.role_name) is True


async def test_the_body_carries_no_database_or_role_names(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    # The analogue of the storage report's key-list pin: a database name embeds the owning
    # project's uuid, so a name list is an inventory of who has what.
    headers = await _admin(db_session)
    _row, record = await _with_database(db_session)

    resp = await client.post(_RECONCILE, headers=headers)

    serialized = resp.text
    assert record.db_name not in serialized
    assert record.role_name not in serialized
    for block in ("databases", "roles"):
        assert all(
            value is None or isinstance(value, int) for value in resp.json()[block].values()
        )


async def test_the_sweep_is_audited_with_counts_only(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    _row, record = await _with_database(db_session)

    assert (await client.post(_RECONCILE, headers=await _admin(db_session))).status_code == 200

    row = await db_session.scalar(sa.select(AuditLog).where(AuditLog.action == "db:reconcile"))
    assert row is not None
    # Cluster-wide, so it belongs to no app and no project — the `storage:reconcile` shape.
    assert row.resource_type == "database"
    assert row.resource_id is None
    assert row.detail is not None
    assert all(isinstance(value, int) for value in row.detail.values())
    serialized = json.dumps(row.detail)
    assert record.db_name not in serialized
    assert record.role_name not in serialized


# --- gating -------------------------------------------------------------------------------


async def test_citizen_is_forbidden(client: AsyncClient, db_session: AsyncSession) -> None:
    resp = await client.post(_RECONCILE, headers=await _citizen(db_session))

    assert resp.status_code == 403
    assert (
        await db_session.scalar(sa.select(AuditLog).where(AuditLog.action == "db:reconcile"))
        is None
    )


async def test_unauthenticated_is_401(client: AsyncClient) -> None:
    assert (await client.post(_RECONCILE)).status_code == 401


# --- the 503 seam -------------------------------------------------------------------------


async def test_an_unconfigured_substrate_is_503_not_500(
    client: AsyncClient, db_session: AsyncSession, no_substrate: None
) -> None:
    # The eager-`Depends` trap (commit 6be7a9c): the maintenance engine is resolved INSIDE
    # the body, so its absence reaches the route's own error seam and answers with the
    # documented, retryable 503 instead of an undocumented 500 in the wrong envelope.
    resp = await client.post(_RECONCILE, headers=await _admin(db_session))

    assert resp.status_code == 503
    assert resp.status_code != 500
    assert "try again" in resp.json()["error"]["message"].lower()


async def test_an_unreachable_cluster_is_503_never_a_partial_report(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A half-enumerated cluster is indistinguishable from a clean one, and "clean" is the
    # answer that gets an orphan forgotten — so the failure surfaces rather than degrading
    # into a zeroed report.
    async def explode(*args: Any, **kwargs: Any) -> None:
        raise OSError("connection refused")

    monkeypatch.setattr(admin_router, "reconcile_orphaned_app_databases", explode)

    resp = await client.post(_RECONCILE, headers=await _admin(db_session))

    assert resp.status_code == 503
    assert "try again" in resp.json()["error"]["message"].lower()
    assert (
        await db_session.scalar(sa.select(AuditLog).where(AuditLog.action == "db:reconcile"))
        is None
    )


def test_openapi_documents_the_route() -> None:
    from src.main import create_app

    responses = set(create_app().openapi()["paths"][_RECONCILE]["post"]["responses"])
    assert {"200", "503", "401", "403", "500"} <= responses


# --- the advisory size column on the listing ----------------------------------------------


async def test_the_listing_shows_a_size_only_for_apps_that_have_a_database(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    with_db, _record = await _with_database(db_session)
    without_db = await _app_row(db_session)

    body = (await client.get(_LIST, headers=await _admin(db_session))).json()

    rows = {row["appId"]: row for row in body["apps"]}
    assert rows[str(with_db.id)]["databaseBytes"] > 0
    # Null, never 0: "no number to show" is a different claim from "an empty database".
    assert rows[str(without_db.id)]["databaseBytes"] is None


async def test_a_not_ready_claim_shows_no_size(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    # `db_ready = false` means the external sequence never finished, so the name in the row
    # may not name anything. Absence is the honest answer.
    row, record = await _with_database(db_session)
    await db_session.execute(
        sa.update(ProjectDatabase).where(ProjectDatabase.id == record.id).values(db_ready=False)
    )

    body = (await client.get(_LIST, headers=await _admin(db_session))).json()

    rows = {item["appId"]: item for item in body["apps"]}
    assert rows[str(row.id)]["databaseBytes"] is None


async def test_the_listing_still_renders_when_the_cluster_is_unreachable(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # The size is DECORATION. An admin queue that 500s because a decoration failed is a far
    # worse outcome than one that renders with the column blank — the opposite call from
    # `reconcile-databases`, which 503s on the same condition because the report IS its product.
    row, _record = await _with_database(db_session)

    async def explode(*args: Any, **kwargs: Any) -> None:
        raise OSError("connection refused")

    monkeypatch.setattr(admin_router, "advisory_database_sizes", explode)

    resp = await client.get(_LIST, headers=await _admin(db_session))

    assert resp.status_code == 200
    rows = {item["appId"]: item for item in resp.json()["apps"]}
    assert rows[str(row.id)]["databaseBytes"] is None


async def test_the_listing_renders_with_no_substrate_at_all(
    client: AsyncClient, db_session: AsyncSession, no_substrate: None
) -> None:
    # The fixture-free absent baseline (`.claude/rules/testing.md`): per-project databases
    # are a genuinely optional integration, so the store-off deployment needs its own test.
    row = await _app_row(db_session)

    resp = await client.get(_LIST, headers=await _admin(db_session))

    assert resp.status_code == 200
    rows = {item["appId"]: item for item in resp.json()["apps"]}
    assert rows[str(row.id)]["databaseBytes"] is None


async def test_patch_app_still_projects_without_a_size(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    # `_project` is shared with `patch_app`, which re-reads one strict tuple and has no size
    # to hand over. The defaulted keyword is what keeps that path working.
    row, _record = await _with_database(db_session)

    resp = await client.patch(
        f"/v1/admin/apps/{row.id}",
        headers=await _admin(db_session),
        json={"loginRequired": True},
    )

    assert resp.status_code == 200
    assert resp.json()["databaseBytes"] is None


async def test_the_size_probe_is_one_query_not_one_per_app(
    client: AsyncClient, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> None:
    # N+1 against the maintenance cluster on the busiest admin page would be a self-inflicted
    # outage. The probe takes every name at once and is called exactly once per listing.
    calls: list[int] = []

    async def counting(engine: Any, db_names: Any) -> dict[str, int]:
        calls.append(len(list(db_names)))
        return await advisory_database_sizes(engine, db_names)

    for _ in range(3):
        await _with_database(db_session)
    monkeypatch.setattr(admin_router, "advisory_database_sizes", counting)

    assert (await client.get(_LIST, headers=await _admin(db_session))).status_code == 200

    assert len(calls) == 1
    assert calls[0] == 3


def test_the_reconcile_route_does_not_shadow_an_app_route() -> None:
    # `reconcile-databases` sits beside `reconcile-storage` on a router whose other POSTs are
    # `/{app_id}/...`. That is safe only while no bare `POST /admin/apps/{app_id}` exists —
    # adding one later would swallow both sweeps, so the absence is pinned here.
    from src.main import create_app

    paths = create_app().openapi()["paths"]
    assert "post" not in paths.get("/v1/admin/apps/{app_id}", {})
    assert "post" in paths[_RECONCILE]
