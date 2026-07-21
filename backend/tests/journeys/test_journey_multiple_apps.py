"""Journey — multiple apps for ONE user, one app PER PROJECT (KD-4 fan-out).

One citizen builds two independent tools. Under one-app-per-project, each tool is its own
PROJECT, and each project holds exactly one app. Provisioning in each project fans out
cleanly: two projects mint TWO distinct apps — distinct appIds, distinct publishable
appKeys — both owned by the same user, each independently submittable. Provision is
idempotent PER PROJECT (a repeat provision in the same project returns that one app, never
a third row) — even from a different builder session, which just advances the head pointer.

Two layers of assertion here:

* The FAN-OUT + IDEMPOTENCY layer (distinct ids/keys, exactly two owned rows, repeat
  provision in a project is a no-op) is the data-plane truth the whole platform stands on.

* The ADDRESSING layer (KD-4): each app is addressed flat by its RETURNED appId (its own
  uuid7 PK), never by a conversation id. `body.appId != conversationId`, and
  `/v1/apps/{appId}/submit` → pending.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import storage_dependency, storage_or_none_dependency
from src.config import settings
from src.db.models.app_registry import AppRegistry, AppStatus
from src.db.models.conversation import ConversationKind
from src.services.auth.session_jwt import mint_session_jwt
from src.services.storage import snapshot_key
from tests.factories import ConversationFactory, ProjectFactory, UserFactory
from tests.fakes import FakeStorage

_TTL = settings.auth.access_ttl_seconds

# A valid submittable artifact: each app's build session finalized a snapshot bundle
# (APPROVAL — submit copies it; nothing is client-supplied).
_SHA = "5a" * 20
_BUNDLE = b"# v2 git bundle\n" + _SHA.encode() + b" HEAD\n\nPACK-fanout"


def _cookie(jwt: str) -> dict[str, str]:
    return {"Cookie": f"session={jwt}"}


async def _auth_user(db: AsyncSession, **overrides: object):
    """Create a user + return (user, cookie-headers)."""
    user = await UserFactory.create(db, **overrides)
    return user, _cookie(mint_session_jwt(user.id, user.token_version, _TTL))


async def test_one_user_fans_out_into_two_independent_apps(client, app, db_session) -> None:
    # One citizen, two independent tools → two PROJECTS (one app per project, KD-4).
    store = FakeStorage()
    app.dependency_overrides[storage_dependency] = lambda: store
    # Both storage seams to ONE store: routes that document a 503 take the None-tolerant
    # `storage_or_none_dependency`, `hard_delete` keeps the raising one. Binding both keeps
    # this journey blind to which seam each route it walks happens to sit on.
    app.dependency_overrides[storage_or_none_dependency] = lambda: store
    user, headers = await _auth_user(db_session, email="fanout@rvaiglobal.com")
    project_a = await ProjectFactory.create(db_session, user.id, name="Tool A")
    project_b = await ProjectFactory.create(db_session, user.id, name="Tool B")
    conv_a = await ConversationFactory.create(
        db_session, user.id, kind=ConversationKind.BUILDER, project_id=project_a.id
    )
    conv_b = await ConversationFactory.create(
        db_session, user.id, kind=ConversationKind.BUILDER, project_id=project_b.id
    )

    # --- provision an app in each project ---------------------------------------
    prov_a = await client.post(
        "/v1/apps/provision",
        json={"conversationId": str(conv_a.id), "projectId": str(project_a.id)},
        headers=headers,
    )
    prov_b = await client.post(
        "/v1/apps/provision",
        json={"conversationId": str(conv_b.id), "projectId": str(project_b.id)},
        headers=headers,
    )
    assert prov_a.status_code == 201, prov_a.text
    assert prov_b.status_code == 201, prov_b.text
    body_a, body_b = prov_a.json(), prov_b.json()

    # --- FAN-OUT: two distinct apps, two distinct publishable keys --------------
    assert body_a["appId"] != body_b["appId"]
    # Each app has its OWN id, never a conversation id (KD-4).
    assert body_a["appId"] != str(conv_a.id)
    assert body_b["appId"] != str(conv_b.id)
    assert body_a["appKey"] != body_b["appKey"]
    assert body_a["appKey"].startswith("bial_")
    assert body_b["appKey"].startswith("bial_")
    assert body_a["status"] == body_b["status"] == "draft"
    assert body_a["loginRequired"] is False and body_b["loginRequired"] is False

    # --- OWNERSHIP + FAN-OUT at the row level: exactly two apps, both this user's -
    rows = (
        (await db_session.execute(sa.select(AppRegistry).where(AppRegistry.user_id == user.id)))
        .scalars()
        .all()
    )
    assert len(rows) == 2
    assert all(row.user_id == user.id for row in rows)  # single-tenant ownership boundary
    # Each app lives in its own project, and points back at its builder conversation (head).
    assert {row.project_id for row in rows} == {project_a.id, project_b.id}
    assert {row.conversation_id for row in rows} == {conv_a.id, conv_b.id}
    assert len({row.app_key for row in rows}) == 2

    # --- IDEMPOTENCY: re-provisioning the SAME project returns the SAME app ------
    reprov_a = await client.post(
        "/v1/apps/provision",
        json={"conversationId": str(conv_a.id), "projectId": str(project_a.id)},
        headers=headers,
    )
    assert reprov_a.status_code == 201
    rebody_a = reprov_a.json()
    assert rebody_a["appId"] == body_a["appId"]  # same app row
    assert rebody_a["appKey"] == body_a["appKey"]  # key minted once, never rotated
    # ...and it did NOT spawn a third row — still exactly two apps for this user.
    still_two = (
        await db_session.execute(
            sa.select(sa.func.count())
            .select_from(AppRegistry)
            .where(AppRegistry.user_id == user.id)
        )
    ).scalar_one()
    assert still_two == 2

    # --- ADDRESSING (KD-4): each app is submittable at its OWN returned appId ----
    app_id_a, app_id_b = body_a["appId"], body_b["appId"]
    store.objects[snapshot_key(uuid.UUID(app_id_a))] = _BUNDLE
    store.objects[snapshot_key(uuid.UUID(app_id_b))] = _BUNDLE
    sub_a = await client.post(f"/v1/apps/{app_id_a}/submit", headers=headers)
    sub_b = await client.post(f"/v1/apps/{app_id_b}/submit", headers=headers)
    assert sub_a.status_code == 200, sub_a.text
    assert sub_b.status_code == 200, sub_b.text
    assert sub_a.json()["status"] == "pending" and sub_a.json()["appId"] == app_id_a
    assert sub_b.json()["status"] == "pending" and sub_b.json()["appId"] == app_id_b
    # Independent submissions: two distinct immutable copies, one per app.
    assert sub_a.json()["submissionId"] != sub_b.json()["submissionId"]

    # Submitting one app leaves the other untouched — the fan-out is independent.
    fresh_a = await db_session.get(AppRegistry, rows[0].id)
    fresh_b = await db_session.get(AppRegistry, rows[1].id)
    assert fresh_a is not None and fresh_a.status is AppStatus.PENDING
    assert fresh_b is not None and fresh_b.status is AppStatus.PENDING
