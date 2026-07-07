"""Journey — multiple apps for ONE user (the multi-app fan-out premise).

A single citizen runs the builder for two independent conversations (A and B) and
provisions an app from each. The platform premise is that this fans out cleanly:
two conversations mint TWO distinct apps — distinct appIds, distinct publishable
appKeys — both owned by the same user, each independently submittable, and each
conversation's provision is idempotent (re-provision returns the SAME app, never a
third row).

Two layers of assertion here:

* The FAN-OUT + IDEMPOTENCY layer (distinct ids/keys, exactly two owned rows,
  same-conversation re-provision is a no-op) is the data-plane truth the whole
  platform stands on. It holds today and must keep holding after the fix.

* The SPA-FACING CONTRACT layer (Journey 1, `_CONTRACTS.md`) is where a known bug
  bites: the builder re-addresses a provisioned app at `/v1/apps/{conversationId}/*`
  (`BuilderPage.jsx` calls `getAppStatus(id)` / `submitApp(id, …)` with the build
  conversation id — never the id echoed back by provision). So the intended
  contract is `body.appId == conversationId` AND `/v1/apps/{conversationId}/submit`
  → pending. FastAPI's `provision` instead leaves `id` to the UUIDv7 default and
  stores the conversation only as a soft `conversation_id`, so `appId != C` and
  addressing either app by its conversation id 404s. Those assertions are marked
  `# CAPTURES BUG:` — red today, green once provision makes C the primary key.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings
from src.db.models.app_registry import AppRegistry, AppStatus
from src.db.models.conversation import ConversationKind
from src.services.auth.session_jwt import mint_session_jwt
from tests.factories import ConversationFactory, UserFactory

_TTL = settings.auth.access_ttl_seconds

# A valid submit payload: a non-empty source + a non-empty client-compiled artifact
# (the server validates shape/presence only — it never runs Babel).
_VALID_SUBMIT = {
    "source": "export default function PreviewApp(){ return <div>hi</div>; }",
    "entry": "PreviewApp",
    "compiled": "var PreviewApp = () => React.createElement('div', null, 'hi');",
}


def _cookie(jwt: str) -> dict[str, str]:
    return {"Cookie": f"session={jwt}"}


async def _auth_user(db: AsyncSession, **overrides: object):
    """Create a user + return (user, cookie-headers)."""
    user = await UserFactory.create(db, **overrides)
    return user, _cookie(mint_session_jwt(user.id, user.token_version, _TTL))


async def test_one_user_fans_out_into_two_independent_apps(client, db_session) -> None:
    # One citizen, two independent builder conversations (client-minted ids, as the SPA does).
    user, headers = await _auth_user(db_session, email="fanout@rvaiglobal.com")
    conv_a = await ConversationFactory.create(db_session, user.id, kind=ConversationKind.BUILDER)
    conv_b = await ConversationFactory.create(db_session, user.id, kind=ConversationKind.BUILDER)
    assert conv_a.id != conv_b.id

    # --- provision an app from each conversation --------------------------------
    prov_a = await client.post(
        "/v1/apps/provision", json={"conversationId": str(conv_a.id)}, headers=headers
    )
    prov_b = await client.post(
        "/v1/apps/provision", json={"conversationId": str(conv_b.id)}, headers=headers
    )
    assert prov_a.status_code == 201, prov_a.text
    assert prov_b.status_code == 201, prov_b.text
    body_a, body_b = prov_a.json(), prov_b.json()

    # --- FAN-OUT: two distinct apps, two distinct publishable keys (holds today) --
    # Distinct appIds — two conversations must not collapse onto one app.
    assert body_a["appId"] != body_b["appId"]
    # Distinct appKeys — each app carries its OWN publishable `bial_` scoping key,
    # minted once at provision (never a raw UUID, ADR-0006).
    assert body_a["appKey"] != body_b["appKey"]
    assert body_a["appKey"].startswith("bial_")
    assert body_b["appKey"].startswith("bial_")
    # Both freshly minted → both start as drafts, login not required.
    assert body_a["status"] == body_b["status"] == "draft"
    assert body_a["loginRequired"] is False and body_b["loginRequired"] is False

    # --- OWNERSHIP + FAN-OUT at the row level: exactly two apps, both this user's -
    rows = (
        (
            await db_session.execute(
                sa.select(AppRegistry).where(AppRegistry.user_id == user.id)
            )
        )
        .scalars()
        .all()
    )
    assert len(rows) == 2
    assert all(row.user_id == user.id for row in rows)  # single-tenant ownership boundary
    # Each row links back to its own builder conversation (the soft `conversation_id`).
    assert {row.conversation_id for row in rows} == {conv_a.id, conv_b.id}
    # The two keys are genuinely distinct at rest too.
    assert len({row.app_key for row in rows}) == 2

    # --- IDEMPOTENCY: re-provisioning the SAME conversation returns the SAME app --
    reprov_a = await client.post(
        "/v1/apps/provision", json={"conversationId": str(conv_a.id)}, headers=headers
    )
    assert reprov_a.status_code == 201
    rebody_a = reprov_a.json()
    assert rebody_a["appId"] == body_a["appId"]  # same app row
    assert rebody_a["appKey"] == body_a["appKey"]  # key minted once, never rotated
    # ...and it did NOT spawn a third row — still exactly two apps for this user.
    still_two = (
        await db_session.execute(
            sa.select(sa.func.count()).select_from(AppRegistry).where(
                AppRegistry.user_id == user.id
            )
        )
    ).scalar_one()
    assert still_two == 2

    # --- SPA-FACING CONTRACT (Journey 1): the builder addresses each app by its ---
    # --- conversation id, so provision must echo that id back AND submit-by-C ----
    # --- must succeed. Both are red today; green once provision keys on C. -------
    #
    # CAPTURES BUG: provision leaves `id` to the UUIDv7 default, so `appId != C`.
    # The SPA re-addresses at `/apps/C/*` with the conversation id, not this value.
    assert body_a["appId"] == str(conv_a.id)
    assert body_b["appId"] == str(conv_b.id)

    # CAPTURES BUG: each app is independently submittable at its OWN conversation
    # address — `db.get(AppRegistry, C)` misses the divergent UUIDv7 PK → 404 today.
    sub_a = await client.post(
        f"/v1/apps/{conv_a.id}/submit", json=_VALID_SUBMIT, headers=headers
    )
    sub_b = await client.post(
        f"/v1/apps/{conv_b.id}/submit", json=_VALID_SUBMIT, headers=headers
    )
    assert sub_a.status_code == 200, sub_a.text
    assert sub_b.status_code == 200, sub_b.text
    assert sub_a.json() == {"appId": str(conv_a.id), "status": "pending"}
    assert sub_b.json() == {"appId": str(conv_b.id), "status": "pending"}

    # Submitting one app leaves the other untouched — the fan-out is independent.
    fresh_a = await db_session.get(AppRegistry, conv_a.id)
    fresh_b = await db_session.get(AppRegistry, conv_b.id)
    assert fresh_a is not None and fresh_a.status is AppStatus.PENDING
    assert fresh_b is not None and fresh_b.status is AppStatus.PENDING
