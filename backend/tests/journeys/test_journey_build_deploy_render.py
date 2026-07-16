"""Journey: build -> submit -> approve -> mark-deployed (one app per project, KD-4).

(The old runner render/serve stage was retired with the open-sandbox pivot — a deployed
app is served from the sandbox's own Caddy, not this control plane — so this pipeline ends
at the approval gate and its manual-runbook handoff. The filename keeps its historical name.)

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

  * `test_build_submit_approve_pipeline` — the backend pipeline: provision -> submit
    (forks the immutable submission copy, APPROVAL R1) -> admin approve pinning exactly
    the reviewed submission (D5) -> mark-deployed recording the runbook handoff (R17).
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import storage_dependency
from src.config import settings
from src.db.models.app_registry import AppRegistry, AppStatus
from src.db.models.conversation import ConversationKind
from src.services.auth.session_jwt import mint_session_jwt
from src.services.storage import snapshot_key, submission_key
from tests.factories import ConversationFactory, UserFactory
from tests.fakes import FakeStorage

_TTL = settings.auth.access_ttl_seconds

# A unique marker embedded in the bundle's pack bytes so the immutable submission copy is
# verifiably the exact artifact submitted (byte-identical, not merely a placeholder).
_RENDER_MARKER = b"DEPLOY_RENDER_PROOF_7f3a"
_SHA = "3c" * 20
_BUNDLE = b"# v2 git bundle\n" + _SHA.encode() + b" HEAD\n\nPACK" + _RENDER_MARKER


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


async def test_build_submit_approve_pipeline(client, app, db_session) -> None:
    """BACKEND PIPELINE: provision -> submit -> approve -> mark-deployed, addressed by the
    provision-RETURNED appId (the app's own uuid7 PK). Submit forks an immutable copy of
    the build-session snapshot; approve pins EXACTLY the reviewed submission; the deployed
    marker closes the loop to the manual go-live runbook (ADR-0015)."""
    store = FakeStorage()
    app.dependency_overrides[storage_dependency] = lambda: store
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

    # (b) the build session finalized a snapshot bundle (SESSION-API's job — seeded
    # here), and the owner submits: draft -> pending + the immutable copy (R1).
    store.objects[snapshot_key(uuid.UUID(app_id))] = _BUNDLE
    submitted = await client.post(f"/v1/apps/{app_id}/submit", headers=owner_headers)
    assert submitted.status_code == 200
    body = submitted.json()
    assert body["appId"] == app_id
    assert body["status"] == "pending"
    assert body["commitSha"] == _SHA
    submission_id = body["submissionId"]
    # The copy is byte-identical to the snapshot — the exact tree the admin reviews.
    copied = store.objects[submission_key(uuid.UUID(app_id), uuid.UUID(submission_id))]
    assert copied == _BUNDLE and _RENDER_MARKER in copied

    # (c) a super-admin (email allowlist: admin@bial.com) approves THE reviewed
    # submission — the D5 guard pins exactly what was reviewed.
    _, admin_headers = await _auth_user(db_session, email="admin@bial.com")
    approved = await client.post(
        f"/v1/admin/apps/{app_id}/approve",
        json={"submissionId": submission_id},
        headers=admin_headers,
    )
    assert approved.status_code == 200
    assert approved.json() == {"appId": app_id, "status": "approved"}

    # (d) the runbook operator ships it and records the handoff (R17).
    deployed = await client.post(f"/v1/admin/apps/{app_id}/mark-deployed", headers=admin_headers)
    assert deployed.status_code == 200
    assert deployed.json()["deployedSubmissionId"] == submission_id

    app_row = await db_session.get(AppRegistry, uuid.UUID(app_id))
    assert app_row is not None
    assert app_row.status is AppStatus.APPROVED
    assert str(app_row.approved_submission_id) == submission_id
    assert app_row.approved_commit_sha == _SHA
    assert app_row.deployed_submission_id == app_row.approved_submission_id
