"""#43 — POST /v1/build-sessions/relaunch: restore a torn-down app from its snapshot into a
fresh, READY sandbox (cookie auth + CSRF, owner-scoping, Decision-6 no-build-slot)."""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.v1.build_sessions.deps import run_build_dependency
from src.api.v1.build_sessions.schemas import BuildSessionStatus
from src.db.models.app_registry import AppRegistry
from src.db.models.conversation import ConversationKind
from src.services.build_sessions.appdata import resolve_app_for_project
from src.services.build_sessions.locks import lock_is_held
from src.services.build_sessions.outcome import write_build_outcome
from src.services.storage import snapshot_key
from tests.api.v1.build_sessions.conftest import BlockingBrain, auth_headers, drain
from tests.factories import ConversationFactory, ProjectFactory, UserFactory


async def _user_project(db: AsyncSession, email: str):
    user = await UserFactory.create(db, email=email)
    project = await ProjectFactory.create(db, user.id)
    return user, project


async def _seed_snapshot(db: AsyncSession, user, project, store) -> uuid.UUID:
    """Provision the app row (so `resolve_app_for_project` inside the endpoint is idempotent)
    and stage a snapshot bundle the relaunch restores."""
    app_id, _ = await resolve_app_for_project(db, user.id, project.id)
    await db.commit()
    await store.put(snapshot_key(app_id), b"BUNDLE")
    return app_id


async def test_relaunch_happy_returns_200_ready_preview(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, wire
) -> None:
    user, project = await _user_project(db_session, "rl1@rvaiglobal.com")
    app_id = await _seed_snapshot(db_session, user, project, fake_storage)

    resp = await client.post(
        "/v1/build-sessions/relaunch",
        json={"projectId": str(project.id)},
        headers=auth_headers(user),
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["appId"] == str(app_id)
    assert body["status"] == "ready"
    assert body["previewUrl"].startswith("https://")  # a live, framable URL
    assert body["restoredFromFailedBuild"] is False  # no failed outcome → no label
    # Decision 6: relaunch did NOT occupy the build slot — the lock is free, no live session.
    assert wire.manager._active_by_user == {}
    assert await lock_is_held(fake_redis, user.id) is False


async def test_relaunch_after_failed_build_signals_last_saved_version(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, wire
) -> None:
    # U6 (F1): the project's newest recorded outcome FAILED, so the restored snapshot is the
    # last SAVED state — the wire flag drives the "Relaunch last saved version" label.
    user, project = await _user_project(db_session, "rl6@rvaiglobal.com")
    await _seed_snapshot(db_session, user, project, fake_storage)
    conv = await ConversationFactory.create(
        db_session, user.id, project_id=project.id, kind=ConversationKind.BUILDER
    )
    await write_build_outcome(
        db_session,
        user_id=user.id,
        conversation_id=conv.id,
        session_id=uuid.uuid4(),
        status=BuildSessionStatus.FAILED,
        preview_url=None,
        snapshot_committed=True,
        reason="build_failed",
    )

    resp = await client.post(
        "/v1/build-sessions/relaunch",
        json={"projectId": str(project.id)},
        headers=auth_headers(user),
    )
    assert resp.status_code == 200
    assert resp.json()["restoredFromFailedBuild"] is True


async def test_relaunch_without_snapshot_is_404(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, wire
) -> None:
    # A never-built project has nothing to relaunch — a definite 404, not a blank preview.
    user, project = await _user_project(db_session, "rl2@rvaiglobal.com")
    resp = await client.post(
        "/v1/build-sessions/relaunch",
        json={"projectId": str(project.id)},
        headers=auth_headers(user),
    )
    assert resp.status_code == 404
    assert wire.sbx.provisioned == []  # never a blank template
    # F17: the 404 path must not mint a phantom DRAFT app row. The speculative upsert was
    # never committed; production `get_db` rolls it back on the error response — mirror that
    # rollback, then prove nothing survived it.
    await db_session.rollback()
    count = await db_session.scalar(
        sa.select(sa.func.count())
        .select_from(AppRegistry)
        .where(AppRegistry.project_id == project.id)
    )
    assert count == 0


async def test_relaunch_while_a_build_is_running_is_409(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, wire
) -> None:
    # A live build owns the one-per-user slot; relaunch 409s and carries the live session id.
    brain = BlockingBrain()
    wire.app.dependency_overrides[run_build_dependency] = lambda: brain
    user, project = await _user_project(db_session, "rl3@rvaiglobal.com")
    await _seed_snapshot(db_session, user, project, fake_storage)

    started = await client.post(
        "/v1/build-sessions",
        json={"projectId": str(project.id), "prompt": "build it"},
        headers=auth_headers(user),
    )
    assert started.status_code == 201
    sid = started.json()["sessionId"]

    conflict = await client.post(
        "/v1/build-sessions/relaunch",
        json={"projectId": str(project.id)},
        headers=auth_headers(user),
    )
    assert conflict.status_code == 409
    err = conflict.json()["error"]
    assert err["code"] == "build_session_already_active"
    assert err["sessionId"] == sid

    brain.release()
    await drain(wire.manager, sid)


async def test_relaunch_another_users_project_is_404(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, wire
) -> None:
    # Owner-scoped (ADR-0004): relaunching another user's project is a non-leaking 404.
    owner, project = await _user_project(db_session, "rl4-owner@rvaiglobal.com")
    await _seed_snapshot(db_session, owner, project, fake_storage)
    intruder = await UserFactory.create(db_session, email="rl4-intruder@rvaiglobal.com")

    resp = await client.post(
        "/v1/build-sessions/relaunch",
        json={"projectId": str(project.id)},
        headers=auth_headers(intruder),
    )
    assert resp.status_code == 404


async def test_relaunch_without_csrf_is_403(
    client: AsyncClient, db_session: AsyncSession, fake_redis, wire
) -> None:
    user, project = await _user_project(db_session, "rl5@rvaiglobal.com")
    resp = await client.post(
        "/v1/build-sessions/relaunch",
        json={"projectId": str(project.id)},
        headers=auth_headers(user, with_csrf=False),
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "csrf_failed"
