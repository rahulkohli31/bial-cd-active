"""Journey — multiple apps for ONE user, one app PER PROJECT.

One citizen builds two independent tools, each its own project. Minting fans out cleanly: two
projects mint TWO distinct apps (distinct appIds, distinct publishable appKeys) owned by the same
user, each independently submittable, and is idempotent PER PROJECT — a second build session in
the same project resolves that one app, never a third row.

Two layers: FAN-OUT + IDEMPOTENCY, the truth the platform stands on, and ADDRESSING — each app
is reached flat by its OWN appId (`appId != conversationId`), and submits into the queue
independently through the submit service, keyed by that same appId.

Minted by `resolve_app_for_project`, the only path now: `POST /apps/provision` had no
production caller and is gone."""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import storage_dependency, storage_or_none_dependency
from src.config import settings
from src.db.models.app_registry import AppRegistry, AppStatus
from src.db.models.conversation import ChatKind
from src.services.approvals.submit import submit_app_for_review
from src.services.auth.session_jwt import mint_session_jwt
from src.services.build_sessions.appdata import resolve_app_for_project
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
        db_session, user.id, kind=ChatKind.BUILD, project_id=project_a.id
    )
    conv_b = await ConversationFactory.create(
        db_session, user.id, kind=ChatKind.BUILD, project_id=project_b.id
    )

    # --- mint an app in each project --------------------------------------------
    app_id_a = await resolve_app_for_project(db_session, user.id, project_a.id)
    app_id_b = await resolve_app_for_project(db_session, user.id, project_b.id)
    await db_session.commit()
    row_a = await db_session.get(AppRegistry, app_id_a)
    row_b = await db_session.get(AppRegistry, app_id_b)
    assert row_a is not None and row_b is not None

    # --- FAN-OUT: two distinct apps, two distinct publishable keys --------------
    assert app_id_a != app_id_b
    assert app_id_a != conv_a.id
    assert app_id_b != conv_b.id
    assert row_a.app_key != row_b.app_key
    assert row_a.app_key.startswith("bial_")
    assert row_b.app_key.startswith("bial_")
    assert row_a.status is AppStatus.DRAFT and row_b.status is AppStatus.DRAFT
    assert row_a.login_required is False and row_b.login_required is False

    # --- OWNERSHIP + FAN-OUT at the row level: exactly two apps, both this user's -
    rows = (
        (await db_session.execute(sa.select(AppRegistry).where(AppRegistry.user_id == user.id)))
        .scalars()
        .all()
    )
    assert len(rows) == 2
    assert all(row.user_id == user.id for row in rows)  # single-tenant ownership boundary
    assert {row.project_id for row in rows} == {project_a.id, project_b.id}
    assert len({row.app_key for row in rows}) == 2

    # --- IDEMPOTENCY: re-resolving the SAME project returns the SAME app ---------
    reresolved_a = await resolve_app_for_project(db_session, user.id, project_a.id)
    await db_session.commit()
    refetched_a = await db_session.get(AppRegistry, reresolved_a)
    assert refetched_a is not None
    assert reresolved_a == app_id_a
    assert refetched_a.app_key == row_a.app_key  # key minted once, never rotated
    # ...and it did NOT spawn a third row — still exactly two apps for this user.
    still_two = (
        await db_session.execute(
            sa.select(sa.func.count())
            .select_from(AppRegistry)
            .where(AppRegistry.user_id == user.id)
        )
    ).scalar_one()
    assert still_two == 2

    # --- ADDRESSING: each app is submittable at its OWN appId, through the submit service -----
    store.objects[snapshot_key(app_id_a)] = _BUNDLE
    store.objects[snapshot_key(app_id_b)] = _BUNDLE
    declaration = {"citizen": {}, "review": {}, "differences": [], "explanation": ""}
    sub_a = await submit_app_for_review(
        db_session,
        store,
        user_id=user.id,
        app=refetched_a,
        declaration=declaration,
    )
    sub_b = await submit_app_for_review(
        db_session,
        store,
        user_id=user.id,
        app=row_b,
        declaration=declaration,
    )
    await db_session.commit()
    assert sub_a.submission_id != sub_b.submission_id

    fresh_a = await db_session.get(AppRegistry, app_id_a)
    fresh_b = await db_session.get(AppRegistry, app_id_b)
    await db_session.refresh(fresh_a)
    await db_session.refresh(fresh_b)
    assert fresh_a is not None and fresh_a.status is AppStatus.PENDING
    assert fresh_b is not None and fresh_b.status is AppStatus.PENDING
    assert fresh_a.source_submission_id == sub_a.submission_id
    assert fresh_b.source_submission_id == sub_b.submission_id
    read_a = await client.get(f"/v1/apps/{app_id_a}/status", headers=headers)
    read_b = await client.get(f"/v1/apps/{app_id_b}/status", headers=headers)
    assert read_a.json()["status"] == "pending" and read_a.json()["appId"] == str(app_id_a)
    assert read_b.json()["status"] == "pending" and read_b.json()["appId"] == str(app_id_b)
