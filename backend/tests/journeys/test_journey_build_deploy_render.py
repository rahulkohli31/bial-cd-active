"""Journey: build -> deploy -> render.

The builder provisions an app FOR a builder conversation C, then the SPA re-addresses
that same app at `/apps/C/*` using the conversation/build id C (never the id returned
by provision) — `provisionApp` sends `{conversationId: C}` and `BuilderPage` calls
`getAppStatus(saved.id)` / `submitApp(id, …)` with C. So the whole builder deploy UX is
contractually bound to "an app provisioned for conversation C is addressable at
`/v1/apps/C/*`" (see `tests/journeys/_CONTRACTS.md` Journey 1).

Two isolated concerns, one file:

  * `test_provisioned_app_is_addressable_at_its_conversation_id` — the SPA id-contract.
    CAPTURES THE CRITICAL BUG: FastAPI's `provision` leaves `AppRegistry.id` to the
    uuid7 PK default and stores C only as a soft, non-PK `conversation_id` column, so
    `read_status` (`db.get(AppRegistry, C)`) misses the PK and 404s. Red today, green
    after provision makes C the primary key.

  * `test_build_submit_approve_render_pipeline` — the backend pipeline, addressed by the
    provision-RETURNED appId (the uuid7 PK). provision -> submit -> admin approve ->
    the runner frame serves the compiled artifact verbatim. This PASSES today, proving
    the pipeline itself is sound and ONLY the SPA id-contract is broken.
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings
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


async def test_provisioned_app_is_addressable_at_its_conversation_id(client, db_session) -> None:
    """SPA CONTRACT (Journey 1.1/1.2): provision(C) then the app is addressable at C."""
    owner, headers = await _auth_user(db_session, email="owner@rvaiglobal.com")
    # C = a real builder conversation id (the SPA mints it client-side, kind=builder).
    conv = await ConversationFactory.create(
        db_session, owner.id, kind=ConversationKind.BUILDER, title="My builder app"
    )
    conversation_id = conv.id

    # (a) provision the app FOR conversation C.
    prov = await client.post(
        "/v1/apps/provision", json={"conversationId": str(conversation_id)}, headers=headers
    )
    assert prov.status_code == 201

    # (b) The SPA re-addresses the app at /apps/C/* — so it MUST resolve at the
    # conversation id C. GET /v1/apps/C/status must be 200 with the draft status.
    #
    # CAPTURES BUG: provision mints appId != conversationId — provision leaves `id` to
    # the uuid7 default and stores C only in the soft, non-PK `conversation_id` column,
    # so read_status's `db.get(AppRegistry, C)` misses the PK and `_owned_app_or_404`
    # fail-closes to 404. This 404s today; it must be 200 once provision makes C the PK
    # (the model docstring already states "the row's `id` IS the appId").
    status_resp = await client.get(f"/v1/apps/{conversation_id}/status", headers=headers)
    assert status_resp.status_code == 200
    body = status_resp.json()
    assert body["status"] == "draft"
    assert body["appId"] == str(conversation_id)
    assert body["appKey"].startswith("bial_")


async def test_build_submit_approve_render_pipeline(client, db_session) -> None:
    """BACKEND PIPELINE (should PASS): provision -> submit -> approve -> frame renders.

    Addressed by the provision-RETURNED appId (the uuid7 PK), which isolates the pipeline
    from the broken SPA id-contract above.
    """
    owner, owner_headers = await _auth_user(db_session, email="owner@rvaiglobal.com")
    conv = await ConversationFactory.create(
        db_session, owner.id, kind=ConversationKind.BUILDER, title="My builder app"
    )

    # (a) provision — take the returned appId (the PK the backend actually resolves on).
    prov = await client.post(
        "/v1/apps/provision", json={"conversationId": str(conv.id)}, headers=owner_headers
    )
    assert prov.status_code == 201
    app_id = prov.json()["appId"]
    # FIXED (Express parity restored): the app's PK IS the conversation id, so the returned
    # appId equals C — which is exactly what lets the SPA address the app at /apps/{C}/*.
    assert app_id == str(conv.id)

    # (c) owner submits the client-compiled build → draft moves to pending.
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
