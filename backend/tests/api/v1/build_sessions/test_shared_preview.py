"""POST /v1/build-sessions/projects/{projectId}/shared-launch and .../shared-refresh (#198
slice 3) — a colleague's own door into a project shared with them: cookie auth + CSRF,
SHARED-only access (never the owner's own door — that's `relaunch_preview`), no build slot
taken."""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator
from types import SimpleNamespace

import httpx
import pytest
import sqlalchemy as sa
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

import src.db.base as db_base
from src.api.v1.build_sessions.deps import sandbox_dependency, sandbox_or_none_dependency
from src.config import settings
from src.db.models.pending_teardown import PendingTeardown, PendingTeardownKind
from src.db.models.sandbox_start import SandboxStart, SandboxStartKind, SandboxStartOutcome
from src.services.build_sessions import shutdown as shutdown_module
from src.services.build_sessions.appdata import resolve_app_for_project
from src.services.projects.shares import create_share
from src.services.redis import registry_key
from src.services.redis.keys import REGISTRY_FIELD_APP_NAME
from src.services.sandbox.client import AcaSandboxClient
from src.services.storage import snapshot_key
from tests.api.v1.build_sessions.conftest import auth_headers
from tests.api.v1.build_sessions.test_relaunch import RecordingAca, SupervisorScript
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
    assert len(wire.sbx.restored) == 1
    # Never occupies the build slot, exactly like relaunch_preview.
    assert wire.manager._active_by_user == {}


async def test_a_launch_writes_a_shared_view_start_and_answers_with_its_id(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, wire
) -> None:
    """The colleague is who started it, the owner's app is what started, and the browser times
    the view against the id this answers with."""
    owner, project, app_id, recipient = await _shared_project(
        db_session, fake_storage, owner_email="owner-st@example.com", recipient_email="r-st@x.com"
    )

    resp = await client.post(
        f"/v1/build-sessions/projects/{project.id}/shared-launch",
        headers=auth_headers(recipient),
    )

    assert resp.status_code == 200, resp.text
    [row] = (await db_session.scalars(sa.select(SandboxStart))).all()
    assert (row.kind, row.user_id, row.app_id) == (
        SandboxStartKind.SHARED_VIEW,
        recipient.id,
        app_id,
    )
    assert row.outcome is SandboxStartOutcome.SERVED
    assert resp.json()["startId"] == str(row.id)


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
    # Restored TWICE, each into a container of its own, not attached.
    assert len(set(wire.sbx.restored)) == 2


async def test_a_restarted_shared_view_is_given_the_owners_settings_again_and_starts(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, wire
) -> None:
    """A colleague's view taken from the pool comes back without its settings when Azure
    restarts it, and refuses to start the app. Launching it again hands them back, built as the
    view's own birth builds them, from the owner's app.

    Mutation check: drop the hand-back from the shared launch's attach arm and the second launch
    starts nothing."""
    sandbox = wire.sbx
    owner, project, app_id, recipient = await _shared_project(
        db_session, fake_storage, owner_email="owner-rc@example.com", recipient_email="r-rc@x.com"
    )
    launch = f"/v1/build-sessions/projects/{project.id}/shared-launch"
    assert (await client.post(launch, headers=auth_headers(recipient))).status_code == 200
    [name] = sandbox.restored
    sandbox.attach_handle = sandbox.by_name[name]
    sandbox.unconfigured.add(name)

    relaunched = await client.post(launch, headers=auth_headers(recipient))

    assert relaunched.status_code == 200, relaunched.text
    assert sandbox.restored == [name]
    [(configured, env)] = sandbox.configured_with
    assert configured == name
    assert env["BIAL_APP_ID"] == str(app_id)
    assert sandbox.restore_env is not None
    assert set(env) == set(sandbox.restore_env)
    assert sandbox.started == [name, name]


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


class _ADeleteThatHangs(RecordingAca):
    """ARM taking its time: every delete is asked for, then waits on `gate`."""

    def __init__(self) -> None:
        super().__init__()
        self.asked: list[str] = []
        self.gate = asyncio.Event()

    async def delete_app(self, *, name: str) -> None:
        self.asked.append(name)
        await self.gate.wait()
        await super().delete_app(name=name)


@pytest.fixture
async def slow_arm(
    wire: SimpleNamespace, db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
) -> AsyncIterator[SimpleNamespace]:
    """`wire` with the real client over a control plane whose deletes hang until let go, and the
    shutdown routine's own sessions bound to the test's, where its debts are written."""

    @contextlib.asynccontextmanager
    async def _session() -> AsyncIterator[AsyncSession]:
        yield db_session

    monkeypatch.setattr(db_base, "async_session_factory", lambda: _session())
    aca = _ADeleteThatHangs()
    configured = settings.sandbox
    assert configured is not None  # `wire` binds it
    sandbox = AcaSandboxClient(
        configured, transport=httpx.MockTransport(SupervisorScript()), aca=aca
    )
    wire.app.dependency_overrides[sandbox_dependency] = lambda: sandbox
    wire.app.dependency_overrides[sandbox_or_none_dependency] = lambda: sandbox
    in_flight_before = set(shutdown_module._IN_FLIGHT)
    yield SimpleNamespace(aca=aca, in_flight_before=in_flight_before)
    aca.gate.set()
    await asyncio.gather(
        *(set(shutdown_module._IN_FLIGHT) - in_flight_before), return_exceptions=True
    )
    await sandbox.aclose()


async def _owed_by(db: AsyncSession, user_id: uuid.UUID) -> list[PendingTeardown]:
    rows = await db.execute(sa.select(PendingTeardown).where(PendingTeardown.user_id == user_id))
    return list(rows.scalars().all())


async def test_a_refresh_answers_before_the_old_view_is_deleted_and_owes_it_unread(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_redis,
    fake_storage,
    slow_arm: SimpleNamespace,
) -> None:
    """★ Refresh puts a new container under a new name in front of the colleague and answers
    without waiting for the old one's delete. The old view is owed as a shared view, never
    written back over the owner's saved copy, and its late delete takes only it: by then the
    record names the new view, and it survives.

    Mutation check: reap the old view inline at the lock and the refresh waits on the hanging
    delete; mint the new view under the old view's name and the late delete takes the new one."""
    owner, project, app_id, recipient = await _shared_project(
        db_session, fake_storage, owner_email="owner-bg@example.com", recipient_email="r-bg@x.com"
    )
    launched = await client.post(
        f"/v1/build-sessions/projects/{project.id}/shared-launch",
        headers=auth_headers(recipient),
    )
    assert launched.status_code == 200, launched.text
    [old] = slow_arm.aca.create_calls

    refreshed = await asyncio.wait_for(
        client.post(
            f"/v1/build-sessions/projects/{project.id}/shared-refresh",
            headers=auth_headers(recipient),
        ),
        timeout=10,
    )

    assert refreshed.status_code == 200, refreshed.text
    assert len(slow_arm.aca.create_calls) == 2
    new = slow_arm.aca.create_calls[1]
    assert new != old
    assert await fake_redis.hget(registry_key(recipient.id), REGISTRY_FIELD_APP_NAME) == new
    assert [
        (row.app_name, row.app_id, row.kind, row.write_back)
        for row in await _owed_by(db_session, recipient.id)
    ] == [(old, app_id, PendingTeardownKind.SHARED, False)]

    slow_arm.aca.gate.set()
    await asyncio.gather(*(set(shutdown_module._IN_FLIGHT) - slow_arm.in_flight_before))

    assert slow_arm.aca.asked == [old]
    assert new in slow_arm.aca.created
    assert await fake_redis.hget(registry_key(recipient.id), REGISTRY_FIELD_APP_NAME) == new
    assert await _owed_by(db_session, recipient.id) == []
    assert await fake_storage.get(snapshot_key(app_id)) == b"BUNDLE"
