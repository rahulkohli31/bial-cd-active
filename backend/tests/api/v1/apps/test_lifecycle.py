"""App lifecycle — the owner-scoped status read (APPROVAL U4, R18/R4; re-shaped by U8).

The citizen submit ROUTE this file grew up around is RETIRED (U8, ASM18): the publish
flow is now the only way into the admin queue. Its behavioural tests moved with the
body they proved — `tests/services/approvals/test_submit.py` exercises the extracted
service — and the route-is-gone guards live at
`tests/api/v1/apps/test_submit_retired.py` (the same flip this file already carries
for `POST /apps/provision` and `GET /apps/{id}/source`, removed in U6). Withdrawal,
the route that replaced the re-submit refresh, is proved at
`tests/api/v1/apps/test_withdraw.py`.

What remains here is the `status` read: owner-scoped via the session cookie, a
cross-user or absent app is the same non-leaking 404 everywhere (ADR-0004), and
pending state is seeded through the REAL writer — the submit service — so the
projection is tested against rows shaped exactly as production shapes them.

The app ROW is minted by `resolve_app_for_project` (the build session's path) — the
standalone `POST /apps/provision` endpoint was removed in U6, so `_provision_app`
below calls that service directly."""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings
from src.db.models.app_registry import AppRegistry, ApprovalRoute
from src.main import create_app
from src.services.approvals.submit import submit_app_for_review
from src.services.auth.session_jwt import mint_session_jwt
from src.services.build_sessions.appdata import resolve_app_for_project
from src.services.storage import snapshot_key
from tests.factories import ProjectFactory, UserFactory
from tests.fakes import FakeStorage

_TTL = settings.auth.access_ttl_seconds

_SHA = "ab" * 20  # 40 lowercase hex chars
# The exact artifact shape `write_snapshot` ships: a raw v2 bundle (R5).
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
    """Take the app to PENDING through the one remaining writer (U8's service) —
    the same call the publish gate makes. Commits, like the gate does."""
    store = FakeStorage()
    store.objects[snapshot_key(uuid.UUID(app_id))] = _BUNDLE
    app_row = await db_session.get(AppRegistry, uuid.UUID(app_id))
    receipt = await submit_app_for_review(
        db_session,
        store,
        user_id=user.id,
        app=app_row,
        declaration={"citizen": {}, "review": {}, "differences": [], "explanation": ""},
        route=ApprovalRoute.SELF_PUBLISH,
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


async def test_status_surfaces_the_deployed_url_and_marker(client, db_session) -> None:
    """ "Your app is live" (R5), owner side: once an admin records the deploy, the
    owner's status read carries `deployedAt` + `deployedUrl` — the SubmitControl's
    Live link. Read-only: the citizen route never writes these, it projects them."""
    user, headers = await _auth_user(db_session, email="liveowner@rvaiglobal.com")
    app_id = await _provision_app(db_session, user)

    # Before any deploy the marker is simply absent — no Live link, no timestamp.
    fresh_read = await client.get(f"/v1/apps/{app_id}/status", headers=headers)
    assert fresh_read.json()["deployedAt"] is None
    assert fresh_read.json()["deployedUrl"] is None

    # The admin's mark-deployed, simulated at the row (the endpoint itself is proven
    # in the admin governance suite — this asserts the OWNER's projection of it).
    live_url = "https://apps.bial.example.com/gate-ops"
    await db_session.execute(
        sa.update(AppRegistry)
        .where(AppRegistry.id == uuid.UUID(app_id))
        .values(deployed_at=sa.func.now(), deployed_url=live_url)
    )
    await db_session.flush()

    body = (await client.get(f"/v1/apps/{app_id}/status", headers=headers)).json()
    assert body["deployedUrl"] == live_url
    assert body["deployedAt"] is not None


async def test_deployed_url_does_not_leak_across_users(client, db_session) -> None:
    # The Live link is owner-scoped like every other field on this read (ADR-0004):
    # a stranger gets the non-leaking 404, never a peek at where the app lives.
    owner, _owner_headers = await _auth_user(db_session, email="liveowner2@rvaiglobal.com")
    app_id = await _provision_app(db_session, owner)
    await db_session.execute(
        sa.update(AppRegistry)
        .where(AppRegistry.id == uuid.UUID(app_id))
        .values(deployed_at=sa.func.now(), deployed_url="https://apps.bial.example.com/secret-ops")
    )
    await db_session.flush()

    _, stranger_headers = await _auth_user(db_session, email="livestranger@rvaiglobal.com")
    denied = await client.get(f"/v1/apps/{app_id}/status", headers=stranger_headers)
    assert denied.status_code == 404
    assert denied.json() == {"error": {"message": "App not found."}}


async def test_status_read_is_owner_scoped(client, db_session) -> None:
    owner, owner_headers = await _auth_user(db_session, email="owner@rvaiglobal.com")
    app_id = await _provision_app(db_session, owner)

    # The owner reads status fine.
    ok = await client.get(f"/v1/apps/{app_id}/status", headers=owner_headers)
    assert ok.status_code == 200
    assert ok.json()["status"] == "draft"

    # A different user gets the same non-leaking 404 every `/apps/*` route returns —
    # indistinguishable from an app that simply doesn't exist (the `200 {status:null}`
    # shim is gone).
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
    # The retired endpoints are gone from the schema entirely — a request to one is a
    # 404 from the router, never a 500 from a half-removed handler. provision/source
    # went in U6; submit went in U8 (its dedicated guard suite is
    # `test_submit_retired.py` — this line keeps the whole retirement ledger in one
    # place beside the routes that remain).
    assert "/v1/apps/provision" not in paths
    assert "/v1/apps/{app_id}/source" not in paths
    assert "/v1/apps/{app_id}/submit" not in paths
