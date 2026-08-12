"""Router-level tests for the two Save endpoints (issue #77): #82 shipped
`POST /projects/{project_id}/save` and `GET .../save-state` with zero HTTP-level
tests — no cross-user 404, no same-user happy path, and the mutating POST was
absent from `test_csrf.py`'s `_MUTATING_POSTS` table.

`save_project_snapshot`'s own mechanics (git state, the dirty ladder, session-conflict
detection) already have deep coverage in `tests/services/build_sessions/test_write_turn_sandbox.py`
— this file is specifically the router-level gap: does the HTTP layer wire scoping, CSRF,
and status-code mapping correctly. Following `test_deploy_routes.py`'s own precedent for
testing these two exact manager methods, both are monkeypatched on the `wire` fixture's
manager instance rather than re-driving real sandbox mechanics through `FakeSandboxClient`."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.v1.build_sessions.deps import sandbox_or_none_dependency
from src.services.build_sessions.manager import (
    BuildSessionConflictError,
    NoLiveSandboxError,
    SaveOutcome,
    SaveState,
)
from tests.api.v1.build_sessions.conftest import auth_headers
from tests.factories import ProjectFactory, UserFactory


async def _user_project(db: AsyncSession, email: str):
    user = await UserFactory.create(db, email=email)
    project = await ProjectFactory.create(db, user.id)
    return user, project


def _save_url(project_id: uuid.UUID) -> str:
    return f"/v1/build-sessions/projects/{project_id}/save"


def _save_state_url(project_id: uuid.UUID) -> str:
    return f"/v1/build-sessions/projects/{project_id}/save-state"


# --- POST .../save ------------------------------------------------------------------


async def test_save_happy_path_returns_the_app_id_and_head_sha(
    client: AsyncClient, db_session: AsyncSession, wire
) -> None:
    user, project = await _user_project(db_session, "save1@rvaiglobal.com")
    app_id = uuid.uuid4()

    async def _fake_save(db, user, project_id, *, sandbox_client) -> SaveOutcome:
        return SaveOutcome(app_id=app_id, head_sha="a" * 40)

    wire.manager.save_project_snapshot = _fake_save

    resp = await client.post(_save_url(project.id), headers=auth_headers(user))

    assert resp.status_code == 200
    body = resp.json()
    assert body["appId"] == str(app_id)
    assert body["headSha"] == "a" * 40


async def test_save_with_no_live_workspace_is_409(
    client: AsyncClient, db_session: AsyncSession, wire
) -> None:
    user, project = await _user_project(db_session, "save2@rvaiglobal.com")

    async def _fake_save(db, user, project_id, *, sandbox_client) -> SaveOutcome:
        raise NoLiveSandboxError(project_id)

    wire.manager.save_project_snapshot = _fake_save

    resp = await client.post(_save_url(project.id), headers=auth_headers(user))

    assert resp.status_code == 409
    assert "nothing to save" in resp.json()["error"]["message"].lower()


async def test_save_while_a_build_is_running_is_409(
    client: AsyncClient, db_session: AsyncSession, wire
) -> None:
    user, project = await _user_project(db_session, "save3@rvaiglobal.com")

    async def _fake_save(db, user, project_id, *, sandbox_client) -> SaveOutcome:
        raise BuildSessionConflictError(uuid.uuid4())

    wire.manager.save_project_snapshot = _fake_save

    resp = await client.post(_save_url(project.id), headers=auth_headers(user))

    assert resp.status_code == 409
    assert "still being built" in resp.json()["error"]["message"].lower()


async def test_save_with_no_sandbox_configured_is_503(
    client: AsyncClient, db_session: AsyncSession, wire
) -> None:
    user, project = await _user_project(db_session, "save4@rvaiglobal.com")
    wire.app.dependency_overrides[sandbox_or_none_dependency] = lambda: None

    resp = await client.post(_save_url(project.id), headers=auth_headers(user))

    assert resp.status_code == 503


async def test_save_of_an_unknown_project_is_404(
    client: AsyncClient, db_session: AsyncSession, wire
) -> None:
    user = await UserFactory.create(db_session, email="save5@rvaiglobal.com")
    resp = await client.post(_save_url(uuid.uuid4()), headers=auth_headers(user))
    assert resp.status_code == 404


async def test_save_of_another_users_project_is_404(
    client: AsyncClient, db_session: AsyncSession, wire
) -> None:
    """Owner-scoped (ADR-0004): saving another user's project is a non-leaking 404, not a
    403 that would confirm the project exists."""
    owner, project = await _user_project(db_session, "save6-owner@rvaiglobal.com")
    intruder = await UserFactory.create(db_session, email="save6-intruder@rvaiglobal.com")

    resp = await client.post(_save_url(project.id), headers=auth_headers(intruder))

    assert resp.status_code == 404


async def test_save_without_csrf_is_403(
    client: AsyncClient, db_session: AsyncSession, wire
) -> None:
    user, project = await _user_project(db_session, "save7@rvaiglobal.com")
    resp = await client.post(_save_url(project.id), headers=auth_headers(user, with_csrf=False))
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "csrf_failed"


# --- GET .../save-state --------------------------------------------------------------


async def test_save_state_happy_path_returns_every_field(
    client: AsyncClient, db_session: AsyncSession, wire
) -> None:
    user, project = await _user_project(db_session, "save8@rvaiglobal.com")
    app_id = uuid.uuid4()
    recovery_at = datetime.now(UTC)

    async def _fake_state(db, user, project_id, *, sandbox_client) -> SaveState:
        return SaveState(
            app_id=app_id,
            dirty=True,
            container_head="b" * 40,
            saved_head="c" * 40,
            recovery_at=recovery_at,
        )

    wire.manager.project_save_state = _fake_state

    resp = await client.get(_save_state_url(project.id), headers=auth_headers(user))

    assert resp.status_code == 200
    body = resp.json()
    assert body["appId"] == str(app_id)
    assert body["dirty"] is True
    assert body["containerHead"] == "b" * 40
    assert body["savedHead"] == "c" * 40
    assert body["recoveryAt"] is not None


async def test_save_state_with_no_sandbox_configured_degrades_to_all_null(
    client: AsyncClient, db_session: AsyncSession, wire
) -> None:
    """No sandbox is a 200, not an error — `SaveStateResponse()`'s all-null shape is the
    honest "cannot compare" answer, matching `preview-state`'s own never-built case.

    Asserts the manager is never even called, not just that the response happens to be
    all-null — `project_save_state` on a project with no app ALSO returns an all-null
    `SaveState` by a different path (`_existing_app_id` returns `None`), so a same-shaped
    response alone would not prove the route's own `if sandbox is None` short-circuit is
    what produced it."""
    user, project = await _user_project(db_session, "save9@rvaiglobal.com")
    wire.app.dependency_overrides[sandbox_or_none_dependency] = lambda: None
    called = False

    async def _spy(db, user, project_id, *, sandbox_client) -> SaveState:
        nonlocal called
        called = True
        return SaveState(app_id=None, dirty=None, container_head=None, saved_head=None)

    wire.manager.project_save_state = _spy

    resp = await client.get(_save_state_url(project.id), headers=auth_headers(user))

    assert resp.status_code == 200
    assert resp.json() == {
        "appId": None,
        "dirty": None,
        "containerHead": None,
        "savedHead": None,
        "recoveryAt": None,
    }
    assert called is False


async def test_save_state_of_an_unknown_project_is_404(
    client: AsyncClient, db_session: AsyncSession, wire
) -> None:
    user = await UserFactory.create(db_session, email="save10@rvaiglobal.com")
    resp = await client.get(_save_state_url(uuid.uuid4()), headers=auth_headers(user))
    assert resp.status_code == 404


async def test_save_state_of_another_users_project_is_404(
    client: AsyncClient, db_session: AsyncSession, wire
) -> None:
    owner, project = await _user_project(db_session, "save11-owner@rvaiglobal.com")
    intruder = await UserFactory.create(db_session, email="save11-intruder@rvaiglobal.com")

    resp = await client.get(_save_state_url(project.id), headers=auth_headers(intruder))

    assert resp.status_code == 404
