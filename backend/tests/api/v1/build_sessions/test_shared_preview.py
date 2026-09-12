"""POST /v1/build-sessions/projects/{projectId}/shared-launch and .../shared-refresh (#198
slice 3) — a colleague's own door into a project shared with them: cookie auth + CSRF,
SHARED-only access (never the owner's own door — that's `relaunch_preview`), no build slot
taken."""

from __future__ import annotations

import uuid

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.services.build_sessions.appdata import resolve_app_for_project
from src.services.build_sessions.manager import shr_name_for
from src.services.projects.shares import create_share
from src.services.storage import snapshot_key
from tests.api.v1.build_sessions.conftest import auth_headers
from tests.factories import ProjectFactory, UserFactory


async def _shared_project(db: AsyncSession, store, *, owner_email: str, recipient_email: str):
    owner = await UserFactory.create(db, email=owner_email)
    project = await ProjectFactory.create(db, owner.id, description="A shared project")
    app_id = await resolve_app_for_project(db, owner.id, project.id)
    await db.commit()
    await store.put(snapshot_key(app_id), b"BUNDLE")
    recipient = await UserFactory.create(db, email=recipient_email)
    await create_share(db, project=project, actor_id=owner.id, colleague_id=recipient.id)
    await db.commit()
    return owner, project, app_id, recipient


async def test_launch_requires_auth_401(client: AsyncClient) -> None:
    resp = await client.post(
        f"/v1/build-sessions/projects/{uuid.uuid4()}/shared-launch", headers={}
    )
    assert resp.status_code == 401


async def test_launch_404s_for_the_owner_of_the_project(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, wire
) -> None:
    """Requirements 19 + the router's own gate: the owner's door into their own project is
    `relaunch_preview`, never this one — reaching it as the owner reads identically to a
    stranger who was never shared with."""
    owner, project, app_id, recipient = await _shared_project(
        db_session, fake_storage, owner_email="owner1@example.com", recipient_email="r1@x.com"
    )

    resp = await client.post(
        f"/v1/build-sessions/projects/{project.id}/shared-launch", headers=auth_headers(owner)
    )
    assert resp.status_code == 404


async def test_launch_404s_for_a_stranger_never_shared_with(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, wire
) -> None:
    owner, project, app_id, recipient = await _shared_project(
        db_session, fake_storage, owner_email="owner2@example.com", recipient_email="r2@x.com"
    )
    stranger = await UserFactory.create(db_session, email="stranger2@example.com")

    resp = await client.post(
        f"/v1/build-sessions/projects/{project.id}/shared-launch",
        headers=auth_headers(stranger),
    )
    assert resp.status_code == 404


async def test_launch_happy_path_returns_200_ready_preview(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, wire
) -> None:
    owner, project, app_id, recipient = await _shared_project(
        db_session, fake_storage, owner_email="owner3@example.com", recipient_email="r3@x.com"
    )

    resp = await client.post(
        f"/v1/build-sessions/projects/{project.id}/shared-launch",
        headers=auth_headers(recipient),
    )

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["appId"] == str(app_id)  # the OWNER's app id
    assert body["ready"] is True
    assert body["previewUrl"].startswith("https://")
    shared_name = shr_name_for(app_id, recipient.id)
    assert shared_name in wire.sbx.restored
    # Never occupies the build slot, exactly like relaunch_preview.
    assert wire.manager._active_by_user == {}


async def test_launch_with_nothing_saved_is_404(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, wire
) -> None:
    owner = await UserFactory.create(db_session, email="owner4@example.com")
    project = await ProjectFactory.create(db_session, owner.id, description="Never saved")
    await resolve_app_for_project(db_session, owner.id, project.id)
    await db_session.commit()
    recipient = await UserFactory.create(db_session, email="r4@example.com")
    # Bypass `create_share`'s own snapshot gate to reach the router's OWN defensive check —
    # a share existing here is already unreachable in practice; this pins that IF it happened,
    # the launch still answers 404 rather than 500.
    from src.db.models.project_share import ProjectShare

    db_session.add(ProjectShare(project_id=project.id, shared_with_user_id=recipient.id))
    await db_session.commit()

    resp = await client.post(
        f"/v1/build-sessions/projects/{project.id}/shared-launch",
        headers=auth_headers(recipient),
    )
    assert resp.status_code == 404


async def test_refresh_restores_again_even_when_already_live(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, wire
) -> None:
    owner, project, app_id, recipient = await _shared_project(
        db_session, fake_storage, owner_email="owner5@example.com", recipient_email="r5@x.com"
    )
    launched = await client.post(
        f"/v1/build-sessions/projects/{project.id}/shared-launch",
        headers=auth_headers(recipient),
    )
    assert launched.status_code == 200

    refreshed = await client.post(
        f"/v1/build-sessions/projects/{project.id}/shared-refresh",
        headers=auth_headers(recipient),
    )

    assert refreshed.status_code == 200
    shared_name = shr_name_for(app_id, recipient.id)
    assert wire.sbx.restored == [shared_name, shared_name]  # restored TWICE, not attached


async def test_launch_without_csrf_is_403(
    client: AsyncClient, db_session: AsyncSession, fake_storage, wire
) -> None:
    owner, project, app_id, recipient = await _shared_project(
        db_session, fake_storage, owner_email="owner6@example.com", recipient_email="r6@x.com"
    )
    resp = await client.post(
        f"/v1/build-sessions/projects/{project.id}/shared-launch",
        headers=auth_headers(recipient, with_csrf=False),
    )
    assert resp.status_code == 403
