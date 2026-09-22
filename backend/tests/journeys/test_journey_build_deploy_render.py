"""Journey: build -> submit -> approve (one app per project).

WHY THIS EXISTS
The filename is historical: the old runner render/serve stage was retired with the open-sandbox
pivot — a deployed app is served from the sandbox's own Caddy, not this control plane.

The submit ROUTE is retired: the submit service is the queue's one entrant, called
directly by the publish gate exactly as stage (b) below does — self-publish lineage,
declaration attached. The manual-runbook handoff is gone for this lineage too:
mark-deployed refuses a self-published app, because the citizen publishes the approved
version themselves through the deploy pipeline.

The builder provisions the project's ONE app, then addresses it flat by the RETURNED
appId — `/v1/apps/{appId}/*` — never by the builder conversation id, which the app's own
fresh UUIDv7 id retires as an identity. The acting builder conversation is recorded as
the app's head/last-builder pointer; the parent project resolves via `project_id` for
the breadcrumb (see `_CONTRACTS.md` Journey 1).

Two isolated concerns, one file: `test_provisioned_app_is_addressable_at_its_returned_id`
covers the flat id-addressing contract (provision returns the app's own id,
`/apps/{appId}/status` resolves it, the acting conversation is the head pointer);
`test_build_submit_approve_pipeline` covers the backend pipeline (provision -> submit,
forking the immutable submission copy -> admin approve pinning exactly the reviewed
submission -> the runbook lever refused).
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import storage_dependency, storage_or_none_dependency
from src.config import settings
from src.db.models.app_registry import AppRegistry, ApprovalRoute, AppStatus
from src.db.models.conversation import ChatKind
from src.services.approvals.submit import submit_app_for_review
from src.services.auth.session_jwt import mint_session_jwt
from src.services.build_sessions.appdata import resolve_app_for_project
from src.services.storage import snapshot_key, submission_key
from tests.factories import ConversationFactory, ProjectFactory, UserFactory
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
    """CONTRACT: the mint returns the app's own id and the app is addressable at
    `/apps/{appId}/*`. The row is minted by `resolve_app_for_project` (the build session's
    path) — there is no client-callable provision endpoint."""
    owner, headers = await _auth_user(db_session, email="owner@rvaiglobal.com")
    project = await ProjectFactory.create(db_session, owner.id)
    conv = await ConversationFactory.create(
        db_session, owner.id, kind=ChatKind.BUILD, project_id=project.id, title="My builder app"
    )

    app_id = str(await resolve_app_for_project(db_session, owner.id, project.id))
    await db_session.commit()
    # The app has its OWN fresh id — one app per project, NOT the conversation id.
    assert app_id != str(conv.id)

    status_resp = await client.get(f"/v1/apps/{app_id}/status", headers=headers)
    assert status_resp.status_code == 200
    body = status_resp.json()
    assert body["status"] == "draft"
    assert body["appId"] == app_id
    assert body["appKey"].startswith("bial_")

    app = await db_session.get(AppRegistry, uuid.UUID(app_id))
    assert app is not None
    assert app.project_id == conv.project_id


async def test_build_submit_approve_pipeline(client, app, db_session) -> None:
    """BACKEND PIPELINE: mint -> submit service -> approve, addressed by the minted
    appId (the app's own uuid7 PK). The submit service forks an immutable copy of the
    build-session snapshot; approve pins EXACTLY the reviewed submission; and the
    manual-runbook lever REFUSES this lineage — the citizen self-publishes the
    approved version through the deploy pipeline, no operator handoff."""
    store = FakeStorage()
    app.dependency_overrides[storage_dependency] = lambda: store
    # Both storage seams to ONE store: routes that document a 503 take the None-tolerant
    # `storage_or_none_dependency`, `hard_delete` keeps the raising one. Binding both keeps
    # this journey blind to which seam each route it walks happens to sit on.
    app.dependency_overrides[storage_or_none_dependency] = lambda: store
    owner, owner_headers = await _auth_user(db_session, email="owner@rvaiglobal.com")
    project = await ProjectFactory.create(db_session, owner.id)
    await ConversationFactory.create(
        db_session, owner.id, kind=ChatKind.BUILD, project_id=project.id, title="My builder app"
    )

    # (a) mint the project's app the way a build session does — take the appId it resolves on.
    app_id = str(await resolve_app_for_project(db_session, owner.id, project.id))
    await db_session.commit()

    # (b) the build session finalized a snapshot bundle (SESSION-API's job — seeded
    # here), and the publish flow routes the app into the queue through the ONE
    # remaining writer: draft -> pending + the immutable copy, lineage and
    # declaration attached.
    store.objects[snapshot_key(uuid.UUID(app_id))] = _BUNDLE
    app_row = await db_session.get(AppRegistry, uuid.UUID(app_id))
    receipt = await submit_app_for_review(
        db_session,
        store,
        user_id=owner.id,
        app=app_row,
        declaration={"citizen": {}, "review": {}, "differences": [], "explanation": ""},
        route=ApprovalRoute.SELF_PUBLISH,
    )
    await db_session.commit()
    assert receipt.commit_sha == _SHA
    submission_id = str(receipt.submission_id)
    # The copy is byte-identical to the snapshot — the exact tree the admin reviews.
    copied = store.objects[submission_key(uuid.UUID(app_id), receipt.submission_id)]
    assert copied == _BUNDLE and _RENDER_MARKER in copied
    # The owner sees the pending state on the flat status read.
    status_read = await client.get(f"/v1/apps/{app_id}/status", headers=owner_headers)
    assert status_read.json()["status"] == "pending"
    assert status_read.json()["submissionId"] == submission_id

    # (c) a super-admin (email allowlist: admin@bial.com) approves THE reviewed
    # submission — the guard pins exactly what was reviewed.
    _, admin_headers = await _auth_user(db_session, email="admin@bial.com")
    approved = await client.post(
        f"/v1/admin/apps/{app_id}/approve",
        json={"submissionId": submission_id},
        headers=admin_headers,
    )
    assert approved.status_code == 200
    assert approved.json() == {"appId": app_id, "status": "approved"}

    # (d) FLIPPED: the runbook handoff gets no new entrants — recording a
    # runbook deployment nobody performed, on an app whose owner publishes it
    # themselves, is refused and stamps nothing.
    deployed = await client.post(f"/v1/admin/apps/{app_id}/mark-deployed", headers=admin_headers)
    assert deployed.status_code == 409

    app_row = await db_session.get(AppRegistry, uuid.UUID(app_id))
    await db_session.refresh(app_row)
    assert app_row is not None
    assert app_row.status is AppStatus.APPROVED
    assert str(app_row.approved_submission_id) == submission_id
    assert app_row.approved_commit_sha == _SHA
    assert app_row.deployed_submission_id is None  # no runbook marker for this lineage
