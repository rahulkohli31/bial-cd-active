"""App lifecycle — provision / submit / status (U1, R18/R4).

Owner-scoped via the session cookie; appKey is a secure token; submit is atomic and
audited; the status enum is enforced. Cross-user reads fail closed (404)."""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings
from src.db.models.app_registry import AppRegistry, AppStatus
from src.db.models.audit import AuditLog
from src.main import create_app
from src.services.auth.session_jwt import mint_session_jwt
from tests.factories import ProjectFactory, UserFactory

_TTL = settings.auth.access_ttl_seconds


def _cookie(jwt: str) -> dict[str, str]:
    return {"Cookie": f"session={jwt}"}


async def _auth_user(db: AsyncSession, **overrides: object):
    user = await UserFactory.create(db, **overrides)
    return user, _cookie(mint_session_jwt(user.id, user.token_version, _TTL))


_VALID_SUBMIT = {
    "source": "export default function PreviewApp(){ return <div>hi</div>; }",
    "entry": "PreviewApp",
    "compiled": "var PreviewApp = () => React.createElement('div', null, 'hi');",
}


async def test_provision_mints_appkey_and_draft(client, db_session) -> None:
    _, headers = await _auth_user(db_session)
    resp = await client.post(
        "/v1/apps/provision", json={"conversationId": str(uuid.uuid4())}, headers=headers
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "draft"
    assert body["appKey"].startswith("bial_")
    assert body["loginRequired"] is False
    # A secure token, never a raw UUID (ADR-0006).
    assert uuid.UUID(body["appId"])  # appId is a UUID
    assert "bial_" not in body["appId"]


async def test_provision_is_idempotent_per_conversation(client, db_session) -> None:
    # No project_id → both provisions land in the caller's Default project, which holds one
    # app (KD-4), so the second reuses the first's row + key (idempotent per project now).
    _, headers = await _auth_user(db_session)
    conv = str(uuid.uuid4())
    first = await client.post("/v1/apps/provision", json={"conversationId": conv}, headers=headers)
    second = await client.post(
        "/v1/apps/provision", json={"conversationId": conv}, headers=headers
    )
    assert first.status_code == 201 and second.status_code == 201
    # Same app row + same minted key (minted once on insert).
    assert first.json()["appId"] == second.json()["appId"]
    assert first.json()["appKey"] == second.json()["appKey"]


async def test_provision_reuses_the_single_project_app(client, db_session) -> None:
    # One app per project (KD-4): a second provision in the SAME project — even from a
    # DIFFERENT builder session — reuses that one app (no second row), and the app's
    # head/last-builder pointer advances to the new conversation.
    user, headers = await _auth_user(db_session)
    project = await ProjectFactory.create(db_session, user.id)
    conv_a, conv_b = str(uuid.uuid4()), str(uuid.uuid4())
    first = await client.post(
        "/v1/apps/provision",
        json={"conversationId": conv_a, "projectId": str(project.id)},
        headers=headers,
    )
    second = await client.post(
        "/v1/apps/provision",
        json={"conversationId": conv_b, "projectId": str(project.id)},
        headers=headers,
    )
    assert first.status_code == 201 and second.status_code == 201
    assert first.json()["appId"] == second.json()["appId"]  # reused, not a new row

    apps = (
        (
            await db_session.execute(
                sa.select(AppRegistry).where(AppRegistry.project_id == project.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(apps) == 1  # uq_app_registry_project holds
    assert apps[0].project_id == project.id
    assert str(apps[0].conversation_id) == conv_b  # head advanced to the latest session


async def test_provision_cross_user_project_is_404(client, db_session) -> None:
    _, headers = await _auth_user(db_session)
    other = await UserFactory.create(db_session)
    stranger_project = await ProjectFactory.create(db_session, other.id)
    resp = await client.post(
        "/v1/apps/provision",
        json={"conversationId": str(uuid.uuid4()), "projectId": str(stranger_project.id)},
        headers=headers,
    )
    assert resp.status_code == 404
    # Nothing was written into the stranger's project.
    apps = (
        (
            await db_session.execute(
                sa.select(AppRegistry).where(AppRegistry.project_id == stranger_project.id)
            )
        )
        .scalars()
        .all()
    )
    assert apps == []


async def test_submit_moves_draft_to_pending_with_snapshot(client, db_session) -> None:
    _, headers = await _auth_user(db_session)
    prov = await client.post(
        "/v1/apps/provision", json={"conversationId": str(uuid.uuid4())}, headers=headers
    )
    app_id = prov.json()["appId"]

    resp = await client.post(f"/v1/apps/{app_id}/submit", json=_VALID_SUBMIT, headers=headers)
    assert resp.status_code == 200
    assert resp.json() == {"appId": app_id, "status": "pending"}

    app = await db_session.get(AppRegistry, uuid.UUID(app_id))
    assert app is not None
    assert app.status is AppStatus.PENDING
    # Snapshot stored with the source→src rename + entry default + client artifact.
    assert app.source_snapshot is not None
    assert app.source_snapshot["src"] == _VALID_SUBMIT["source"]
    assert app.source_snapshot["entry"] == "PreviewApp"
    assert app.source_snapshot["compiled"] == _VALID_SUBMIT["compiled"]


async def test_submit_writes_an_audit_row(client, db_session) -> None:
    user, headers = await _auth_user(db_session)
    prov = await client.post(
        "/v1/apps/provision", json={"conversationId": str(uuid.uuid4())}, headers=headers
    )
    app_id = prov.json()["appId"]
    await client.post(f"/v1/apps/{app_id}/submit", json=_VALID_SUBMIT, headers=headers)

    row = (
        await db_session.execute(
            sa.select(AuditLog).where(
                AuditLog.resource_type == "app", AuditLog.resource_id == app_id
            )
        )
    ).scalar_one()
    assert row.action == "submit"
    assert row.actor_id == user.id


async def test_submit_without_source_is_rejected(client, db_session) -> None:
    _, headers = await _auth_user(db_session)
    prov = await client.post(
        "/v1/apps/provision", json={"conversationId": str(uuid.uuid4())}, headers=headers
    )
    app_id = prov.json()["appId"]
    resp = await client.post(
        f"/v1/apps/{app_id}/submit",
        json={"source": "   ", "compiled": _VALID_SUBMIT["compiled"]},
        headers=headers,
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["message"] == "Nothing to submit — generate an app first."


async def test_submit_without_compiled_artifact_is_rejected(client, db_session) -> None:
    _, headers = await _auth_user(db_session)
    prov = await client.post(
        "/v1/apps/provision", json={"conversationId": str(uuid.uuid4())}, headers=headers
    )
    app_id = prov.json()["appId"]
    resp = await client.post(
        f"/v1/apps/{app_id}/submit",
        json={"source": _VALID_SUBMIT["source"], "compiled": ""},
        headers=headers,
    )
    assert resp.status_code == 400
    assert "error" in resp.json()


async def test_status_read_is_owner_scoped(client, db_session) -> None:
    _, owner_headers = await _auth_user(db_session, email="owner@rvaiglobal.com")
    prov = await client.post(
        "/v1/apps/provision", json={"conversationId": str(uuid.uuid4())}, headers=owner_headers
    )
    app_id = prov.json()["appId"]

    # The owner reads status fine.
    ok = await client.get(f"/v1/apps/{app_id}/status", headers=owner_headers)
    assert ok.status_code == 200
    assert ok.json()["status"] == "draft"

    # A different user sees "not provisioned" — a non-leaking 200 {status:null} (Express
    # parity; indistinguishable from an app that simply doesn't exist for them).
    _, other_headers = await _auth_user(db_session, email="other@rvaiglobal.com")
    denied = await client.get(f"/v1/apps/{app_id}/status", headers=other_headers)
    assert denied.status_code == 200
    assert denied.json()["status"] is None


async def test_submit_unknown_app_is_404(client, db_session) -> None:
    _, headers = await _auth_user(db_session)
    resp = await client.post(
        f"/v1/apps/{uuid.uuid4()}/submit", json=_VALID_SUBMIT, headers=headers
    )
    assert resp.status_code == 404


async def test_lifecycle_requires_authentication(client) -> None:
    resp = await client.post("/v1/apps/provision", json={"conversationId": str(uuid.uuid4())})
    assert resp.status_code == 401


def test_lifecycle_routes_document_error_codes_in_openapi() -> None:
    paths = create_app().openapi()["paths"]
    # `.500` is inherited from the v1-router default; the rest are declared per route.
    assert {"401", "409", "500"} <= set(paths["/v1/apps/provision"]["post"]["responses"])
    submit = set(paths["/v1/apps/{app_id}/submit"]["post"]["responses"])
    assert {"400", "401", "404", "409", "500"} <= submit
    assert {"401", "500"} <= set(paths["/v1/apps/{app_id}/status"]["get"]["responses"])
