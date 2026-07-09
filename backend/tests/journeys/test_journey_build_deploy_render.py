"""Journey: build -> deploy -> render (one app per project, KD-4).

The builder provisions the project's ONE app, then addresses it flat by the RETURNED
appId — `/v1/apps/{appId}/*` — never by the builder conversation id. The app has its own
fresh UUIDv7 id (KD-4 retired the old `appId == conversationId` identity); the acting
builder conversation is recorded as the app's head/last-builder pointer, and the parent
project is resolved via `project_id` for the breadcrumb. The rebuilt frontends address by
the returned id (see `_CONTRACTS.md` Journey 1).

Two isolated concerns, one file:

  * `test_provisioned_app_is_addressable_at_its_returned_id` — the flat id-addressing
    contract: provision returns the app's own id, `/apps/{appId}/status` resolves it, and
    the acting conversation is the head pointer.

  * `test_build_submit_approve_render_pipeline` — the backend pipeline: provision -> submit
    -> admin approve -> the runner frame serves the compiled artifact verbatim, all
    addressed by the returned appId.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings
from src.db.models.app_registry import AppRegistry
from src.db.models.conversation import ConversationKind
from src.services.auth.session_jwt import mint_session_jwt
from tests.factories import ConversationFactory, UserFactory

_TTL = settings.auth.access_ttl_seconds

# A unique marker embedded in the client-compiled artifact — its presence in the served
# frame HTML proves the artifact was rendered (not merely stored).
_RENDER_MARKER = "DEPLOY_RENDER_PROOF_7f3a"
_COMPILED = f"var PreviewApp=()=>React.createElement('div',null,'{_RENDER_MARKER}');"
_VALID_SUBMIT = {
    "source": "export default function PreviewApp(){ return <div>ship it</div>; }",
    "entry": "PreviewApp",
    "compiled": _COMPILED,
}


def _cookie(jwt: str) -> dict[str, str]:
    return {"Cookie": f"session={jwt}"}


async def _auth_user(db: AsyncSession, **overrides: object):
    user = await UserFactory.create(db, **overrides)
    return user, _cookie(mint_session_jwt(user.id, user.token_version, _TTL))


async def test_provisioned_app_is_addressable_at_its_returned_id(client, db_session) -> None:
    """CONTRACT (KD-4): provision returns the app's own id; the app is addressable at
    `/apps/{returnedAppId}/*`, and the acting builder conversation is the head pointer."""
    owner, headers = await _auth_user(db_session, email="owner@rvaiglobal.com")
    conv = await ConversationFactory.create(
        db_session, owner.id, kind=ConversationKind.BUILDER, title="My builder app"
    )

    # provision the project's app FROM the builder conversation.
    prov = await client.post(
        "/v1/apps/provision",
        json={"conversationId": str(conv.id), "projectId": str(conv.project_id)},
        headers=headers,
    )
    assert prov.status_code == 201
    app_id = prov.json()["appId"]
    # The app has its OWN fresh id — one app per project, NOT the conversation id (KD-4).
    assert app_id != str(conv.id)

    # It is addressable flat by that returned id — GET /v1/apps/{appId}/status is 200 draft.
    status_resp = await client.get(f"/v1/apps/{app_id}/status", headers=headers)
    assert status_resp.status_code == 200
    body = status_resp.json()
    assert body["status"] == "draft"
    assert body["appId"] == app_id
    assert body["appKey"].startswith("bial_")

    # The acting builder conversation is recorded as the head/last-builder pointer, and the
    # app lives in the conversation's project.
    app = await db_session.get(AppRegistry, uuid.UUID(app_id))
    assert app is not None
    assert app.conversation_id == conv.id
    assert app.project_id == conv.project_id


async def test_build_submit_approve_render_pipeline(client, db_session) -> None:
    """BACKEND PIPELINE: provision -> submit -> approve -> frame renders, addressed by the
    provision-RETURNED appId (the app's own uuid7 PK)."""
    owner, owner_headers = await _auth_user(db_session, email="owner@rvaiglobal.com")
    conv = await ConversationFactory.create(
        db_session, owner.id, kind=ConversationKind.BUILDER, title="My builder app"
    )

    # (a) provision — take the returned appId (the id the backend resolves on).
    prov = await client.post(
        "/v1/apps/provision",
        json={"conversationId": str(conv.id), "projectId": str(conv.project_id)},
        headers=owner_headers,
    )
    assert prov.status_code == 201
    app_id = prov.json()["appId"]

    # (b) owner submits the client-compiled build → draft moves to pending.
    submitted = await client.post(
        f"/v1/apps/{app_id}/submit", json=_VALID_SUBMIT, headers=owner_headers
    )
    assert submitted.status_code == 200
    assert submitted.json() == {"appId": app_id, "status": "pending"}

    # A super-admin (email allowlist: admin@bial.com) approves → copies the client
    # artifact into approved_snapshot, no server compile.
    _, admin_headers = await _auth_user(db_session, email="admin@bial.com")
    approved = await client.post(f"/v1/admin/apps/{app_id}/approve", headers=admin_headers)
    assert approved.status_code == 200
    assert approved.json() == {"appId": app_id, "status": "approved"}

    # The runner frame (status-gated, no auth) serves the compiled artifact verbatim —
    # the marker's presence proves the deployed app is rendered.
    frame = await client.get(f"/apps/{app_id}/frame")
    assert frame.status_code == 200
    assert _RENDER_MARKER in frame.text
    assert _COMPILED in frame.text

    # The same-origin shell also serves for the approved app (config carries the appId).
    shell = await client.get(f"/apps/{app_id}")
    assert shell.status_code == 200
    assert app_id in shell.text
