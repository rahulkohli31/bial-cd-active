"""App lifecycle — the owner-scoped `status` read.

Pending state is seeded through the real writer, `services.approvals.submit`, so the
projection is tested against rows shaped exactly as production shapes them. The app
row is minted by `resolve_app_for_project`, which `_provision_app` below calls
directly."""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings
from src.db.models.app_registry import AppRegistry
from src.main import create_app
from src.services.approvals.submit import submit_app_for_review
from src.services.auth.session_jwt import mint_session_jwt
from src.services.build_sessions.appdata import resolve_app_for_project
from src.services.storage import snapshot_key
from tests.factories import ProjectFactory, UserFactory
from tests.fakes import FakeStorage

_TTL = settings.auth.access_ttl_seconds

_SHA = "ab" * 20  # 40 lowercase hex chars
# The exact artifact shape `write_snapshot` ships: a raw v2 bundle.
_BUNDLE = b"# v2 git bundle\n" + _SHA.encode() + b" HEAD\n\nPACK-fake-bytes"


def _cookie(jwt: str) -> dict[str, str]:
    return {"Cookie": f"session={jwt}"}


async def _auth_user(db: AsyncSession, **overrides: object):
    user = await UserFactory.create(db, **overrides)
    return user, _cookie(mint_session_jwt(user.id, user.token_version, _TTL))


async def _provision_app(db_session, user) -> str:
    """Mint the user's app inside a fresh project (project-first); return the appId.
    Commits, because the endpoints under test read through their own session."""
    project = await ProjectFactory.create(db_session, user.id)
    app_id = await resolve_app_for_project(db_session, user.id, project.id)
    await db_session.commit()
    return str(app_id)


async def _submit_via_service(db_session, user, app_id: str):
    """Take the app to PENDING through the approvals submit service — the same call
    the publish gate makes. Commits, like the gate does."""
    store = FakeStorage()
    store.objects[snapshot_key(uuid.UUID(app_id))] = _BUNDLE
    app_row = await db_session.get(AppRegistry, uuid.UUID(app_id))
    receipt = await submit_app_for_review(
        db_session,
        store,
        user_id=user.id,
        app=app_row,
        declaration={"citizen": {}, "review": {}, "differences": [], "explanation": ""},
    )
    await db_session.commit()
    return receipt


async def test_status_surfaces_submission_metadata(client, db_session) -> None:
    user, headers = await _auth_user(db_session)
    app_id = await _provision_app(db_session, user)
    receipt = await _submit_via_service(db_session, user, app_id)

    resp = await client.get(f"/v1/apps/{app_id}/status", headers=headers)
    body = resp.json()
    assert body["status"] == "pending"
    assert body["submissionId"] == str(receipt.submission_id)
    assert body["commitSha"] == _SHA
    assert body["submittedAt"] is not None


async def test_the_owner_status_read_carries_no_hand_recorded_deployment(
    client, db_session
) -> None:
    user, headers = await _auth_user(db_session, email="liveowner@rvaiglobal.com")
    app_id = await _provision_app(db_session, user)

    body = (await client.get(f"/v1/apps/{app_id}/status", headers=headers)).json()

    assert body["appId"] == app_id
    assert not {"deployedAt", "deployedUrl"} & set(body)


async def test_status_read_is_owner_scoped(client, db_session) -> None:
    owner, owner_headers = await _auth_user(db_session, email="owner@rvaiglobal.com")
    app_id = await _provision_app(db_session, owner)

    ok = await client.get(f"/v1/apps/{app_id}/status", headers=owner_headers)
    assert ok.status_code == 200
    assert ok.json()["status"] == "draft"

    _, other_headers = await _auth_user(db_session, email="other@rvaiglobal.com")
    denied = await client.get(f"/v1/apps/{app_id}/status", headers=other_headers)
    assert denied.status_code == 404
    assert denied.json() == {"error": {"message": "App not found."}}


async def test_status_unknown_app_is_404(client, db_session) -> None:
    _, headers = await _auth_user(db_session)
    resp = await client.get(f"/v1/apps/{uuid.uuid4()}/status", headers=headers)
    assert resp.status_code == 404
    assert resp.json() == {"error": {"message": "App not found."}}


async def test_lifecycle_requires_authentication(client) -> None:
    resp = await client.get(f"/v1/apps/{uuid.uuid4()}/status")
    assert resp.status_code == 401


def test_lifecycle_routes_document_error_codes_in_openapi() -> None:
    paths = create_app().openapi()["paths"]
    # `.500` is inherited from the v1-router default; the rest are declared per route.
    assert {"401", "404", "500"} <= set(paths["/v1/apps/{app_id}/status"]["get"]["responses"])
    assert "/v1/apps/provision" not in paths
    assert "/v1/apps/{app_id}/source" not in paths
    assert "/v1/apps/{app_id}/submit" not in paths
