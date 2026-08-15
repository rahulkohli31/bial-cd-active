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
    # RECORD the arguments, don't just bind them. `owned_project_or_404` authorizes an id and
    # then DISCARDS its return value; the manager is handed `project_id` again by convention
    # alone, so nothing otherwise ties the id that was authorized to the id that is acted on.
    # Since `save_project_snapshot` IS the write, an authorize-A/act-on-B drift would silently
    # overwrite a different project's saved bundle. Same recording precedent as
    # `test_deploy_routes.py`'s `FakeService.start`, which this file's docstring already cites.
    seen: list[tuple[uuid.UUID, uuid.UUID]] = []

    async def _fake_save(db, user, project_id, *, sandbox_client) -> SaveOutcome:
        seen.append((user.id, project_id))
        return SaveOutcome(app_id=app_id, head_sha="a" * 40)

    wire.manager.save_project_snapshot = _fake_save

    resp = await client.post(_save_url(project.id), headers=auth_headers(user))

    assert resp.status_code == 200
    body = resp.json()
    assert body["appId"] == str(app_id)
    assert body["headSha"] == "a" * 40
    assert seen == [(user.id, project.id)]


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
    # The negative half of the happy path's `seen` assertion: the guard has to short-circuit
    # BEFORE the write, not merely produce a 404 on the way out. Without this, a route that
    # saved first and only then checked ownership would still answer 404 and pass.
    seen: list[uuid.UUID] = []

    async def _fake_save(db, user, project_id, *, sandbox_client) -> SaveOutcome:
        seen.append(project_id)
        return SaveOutcome(app_id=uuid.uuid4(), head_sha="a" * 40)

    wire.manager.save_project_snapshot = _fake_save

    resp = await client.post(_save_url(project.id), headers=auth_headers(intruder))

    assert resp.status_code == 404
    assert seen == []


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
    # A FIXED sentinel, never `now()`: `router.py` forwards the stored autosave timestamp, and
    # the mutant worth killing is `recovery_at=datetime.now(UTC)` — the exact substitution
    # `manager.py`'s own docstring forbids ("it is the value the caller shows the user — 'your
    # work from 14:47' — so it must be the write time, never `now`"). Binding this from
    # `now()` would make that mutant a microsecond-margin race instead of a clean kill.
    recovery_at = datetime(2026, 5, 4, 14, 47, tzinfo=UTC)
    seen: list[tuple[uuid.UUID, uuid.UUID]] = []

    async def _fake_state(db, user, project_id, *, sandbox_client) -> SaveState:
        seen.append((user.id, project_id))
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
    # VALUE, not presence — dropping the field is already caught by `SaveStateResponse`'s
    # `None` default, so only a wrong non-null value survives, which is the documented failure.
    # `fromisoformat` reads both the `+00:00` and `Z` spellings, so this stays
    # serialization-agnostic.
    assert datetime.fromisoformat(body["recoveryAt"]) == recovery_at
    assert seen == [(user.id, project.id)]


async def test_save_state_passes_an_unknown_dirty_through_as_null(
    client: AsyncClient, db_session: AsyncSession, wire
) -> None:
    """`dirty=None` is UNKNOWN, and it is a DISTINCT answer from False — `SaveState`'s own
    docstring: "no live container (nothing to compare), or a store we could not read. A UI
    that renders unknown as clean tells the user their work is safe when nobody checked."

    Not a hypothetical arm: `project_save_state` returns `dirty=None` on three paths with a
    sandbox configured — no app yet, `NoLiveSandboxError` from `_attach_for_read` (the
    ordinary "container isn't running" case), and `_container_state` returning `None`.

    Note the asymmetry that makes this worth a test of its own: the DANGEROUS direction is
    already caught, because hardcoding `dirty=False` fails the happy path above. It is the
    documented `None`-to-`False` collapse (`dirty=bool(state.dirty)`) that survives — and the
    sandbox is deliberately left at the `wire` default here, because the no-sandbox test
    below asserts the manager is never called and so cannot cover a manager-produced `None`."""
    user, project = await _user_project(db_session, "save8b@rvaiglobal.com")
    app_id = uuid.uuid4()

    async def _fake_state(db, user, project_id, *, sandbox_client) -> SaveState:
        return SaveState(app_id=app_id, dirty=None, container_head=None, saved_head=None)

    wire.manager.project_save_state = _fake_state

    resp = await client.get(_save_state_url(project.id), headers=auth_headers(user))

    assert resp.status_code == 200
    body = resp.json()
    assert body["appId"] == str(app_id)
    assert body["dirty"] is None


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
    # The negative half of the happy path's `seen` assertion — the guard must short-circuit
    # before the manager, not just produce a 404 on the way out.
    seen: list[uuid.UUID] = []

    async def _fake_state(db, user, project_id, *, sandbox_client) -> SaveState:
        seen.append(project_id)
        return SaveState(app_id=None, dirty=None, container_head=None, saved_head=None)

    wire.manager.project_save_state = _fake_state

    resp = await client.get(_save_state_url(project.id), headers=auth_headers(intruder))

    assert resp.status_code == 404
    assert seen == []


async def test_save_state_checks_ownership_before_the_missing_sandbox(
    client: AsyncClient, db_session: AsyncSession, wire
) -> None:
    """The two routes order these guards DIFFERENTLY on purpose, and nothing else stops a
    refactor from quietly normalizing them: `save_state` authorizes first and only then
    short-circuits on a missing sandbox, while `save_project` (below) refuses 503 first.

    NOT a security boundary, and it should not be read as one — `SaveStateResponse()` is
    constant and project-independent, so a 200-all-null for an unowned project is
    indistinguishable from one for your own never-built project. Neither ordering is an
    existence oracle. This pins the deliberate asymmetry, nothing more."""
    owner, project = await _user_project(db_session, "save11b-owner@rvaiglobal.com")
    intruder = await UserFactory.create(db_session, email="save11b-intruder@rvaiglobal.com")
    wire.app.dependency_overrides[sandbox_or_none_dependency] = lambda: None

    resp = await client.get(_save_state_url(project.id), headers=auth_headers(intruder))

    # 404 from the ownership guard — NOT the 200-all-null the sandbox short-circuit returns.
    assert resp.status_code == 404


async def test_save_refuses_the_missing_sandbox_before_checking_ownership(
    client: AsyncClient, db_session: AsyncSession, wire
) -> None:
    """The mirror of the test above, pinning the other half of the asymmetry: `save_project`
    raises 503 on a missing sandbox BEFORE `owned_project_or_404` runs, so an intruder hits
    the 503 rather than the 404. Same caveat — `_SANDBOX_UNAVAILABLE_MSG` is constant and
    project-independent, so this leaks nothing about whether the project exists."""
    owner, project = await _user_project(db_session, "save12-owner@rvaiglobal.com")
    intruder = await UserFactory.create(db_session, email="save12-intruder@rvaiglobal.com")
    wire.app.dependency_overrides[sandbox_or_none_dependency] = lambda: None

    resp = await client.post(_save_url(project.id), headers=auth_headers(intruder))

    assert resp.status_code == 503
