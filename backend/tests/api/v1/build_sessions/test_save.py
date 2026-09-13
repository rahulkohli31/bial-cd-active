"""Router-level tests for the two Save endpoints: `POST /projects/{project_id}/save` and
`GET .../save-state`.

NOT THE SOLE HTTP COVERAGE: `test_control.py`'s build-ordering test already drives
`POST .../save` mid-build through the REAL manager and asserts the 409, making
`test_save_while_a_build_is_running_is_409` below the WEAKER sibling (it only proves the
router's `BuildSessionConflictError` → 409 mapping) — kept so nobody drops it as redundant.

`save_project_snapshot`'s mechanics have deep coverage in
`tests/services/build_sessions/test_write_turn_sandbox.py`; this file covers only the
router-level gap — scoping, CSRF, status-code mapping — so both manager methods are
monkeypatched on `wire` rather than re-driving real sandbox mechanics."""

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
    # RECORD the arguments, don't just bind them: `owned_project_or_404` authorizes an id and
    # DISCARDS the return value, so the manager is handed `project_id` again by convention
    # alone — nothing else ties the id authorized to the id acted on, and since
    # `save_project_snapshot` IS the write, an authorize-A/act-on-B drift would silently
    # overwrite a different project's saved bundle.
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


async def test_save_returns_a_null_head_sha_when_the_state_read_failed(
    client: AsyncClient, db_session: AsyncSession, wire
) -> None:
    """`head_sha=None` is a REAL return, not a defensive default: `save_project_snapshot`
    builds `SaveOutcome(head_sha=saved.head if saved else None)`, and `container_state`
    answers `None` whenever the post-save `git` exec raises or exits non-zero. The save
    itself succeeded — only the read-back of where it landed did not.

    Without this, tightening `SaveResponse.head_sha` to a bare `str`, or writing
    `head_sha=outcome.head_sha or ""`, passes every other test here and then 500s in
    production at response validation the first time that git read fails."""
    user, project = await _user_project(db_session, "save1b@rvaiglobal.com")
    app_id = uuid.uuid4()

    async def _fake_save(db, user, project_id, *, sandbox_client) -> SaveOutcome:
        return SaveOutcome(app_id=app_id, head_sha=None)

    wire.manager.save_project_snapshot = _fake_save

    resp = await client.post(_save_url(project.id), headers=auth_headers(user))

    assert resp.status_code == 200
    body = resp.json()
    assert body["appId"] == str(app_id)
    assert body["headSha"] is None


async def test_save_with_no_live_workspace_is_409(
    client: AsyncClient, db_session: AsyncSession, wire
) -> None:
    user, project = await _user_project(db_session, "save2@rvaiglobal.com")
    # The same `seen` machinery as the happy path, extended to the error path — recorded
    # BEFORE the raise, since an append placed after it would be unreachable and prove nothing.
    seen: list[tuple[uuid.UUID, uuid.UUID]] = []

    async def _fake_save(db, user, project_id, *, sandbox_client) -> SaveOutcome:
        seen.append((user.id, project_id))
        raise NoLiveSandboxError(project_id)

    wire.manager.save_project_snapshot = _fake_save

    resp = await client.post(_save_url(project.id), headers=auth_headers(user))

    assert resp.status_code == 409
    assert "nothing to save" in resp.json()["error"]["message"].lower()
    assert seen == [(user.id, project.id)]


async def test_save_while_a_build_is_running_is_409(
    client: AsyncClient, db_session: AsyncSession, wire
) -> None:
    user, project = await _user_project(db_session, "save3@rvaiglobal.com")
    seen: list[tuple[uuid.UUID, uuid.UUID]] = []

    async def _fake_save(db, user, project_id, *, sandbox_client) -> SaveOutcome:
        seen.append((user.id, project_id))
        raise BuildSessionConflictError(uuid.uuid4())

    wire.manager.save_project_snapshot = _fake_save

    resp = await client.post(_save_url(project.id), headers=auth_headers(user))

    assert resp.status_code == 409
    assert "still being built" in resp.json()["error"]["message"].lower()
    assert seen == [(user.id, project.id)]


async def test_save_with_no_sandbox_configured_is_503(
    client: AsyncClient, db_session: AsyncSession, wire
) -> None:
    user, project = await _user_project(db_session, "save4@rvaiglobal.com")
    wire.app.dependency_overrides[sandbox_or_none_dependency] = lambda: None

    resp = await client.post(_save_url(project.id), headers=auth_headers(user))

    assert resp.status_code == 503
    # The ENVELOPE too, matching `test_control.py`'s own sandbox-503 sibling: the SPA reads
    # `error.message` verbatim, so a 503 raised as a bare `HTTPException` would come back as
    # `{"detail": ...}` and render as an empty error while a status-only assertion stayed green.
    body = resp.json()
    assert (
        body["error"]["message"]
        == "Sandbox unavailable. Please try again later or contact the admin"
    )
    assert "detail" not in body


async def test_save_of_an_unknown_project_is_404(
    client: AsyncClient, db_session: AsyncSession, wire
) -> None:
    user = await UserFactory.create(db_session, email="save5@rvaiglobal.com")
    resp = await client.post(_save_url(uuid.uuid4()), headers=auth_headers(user))
    assert resp.status_code == 404


async def test_save_of_another_users_project_is_404(
    client: AsyncClient, db_session: AsyncSession, wire
) -> None:
    """Owner-scoped: saving another user's project is a non-leaking 404, not a
    403 that would confirm the project exists."""
    owner, project = await _user_project(db_session, "save6-owner@rvaiglobal.com")
    intruder = await UserFactory.create(db_session, email="save6-intruder@rvaiglobal.com")
    # The negative half of the happy path's `seen` assertion: the guard must short-circuit
    # BEFORE the write — without this, a route that saved first then checked ownership would
    # still answer 404 and pass.
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
    # A FIXED sentinel, never `now()`: the mutant worth killing is `recovery_at=datetime.now(UTC)`
    # — the exact substitution `manager.py`'s docstring forbids ("it is the value the caller
    # shows the user... so it must be the write time, never `now`"). Binding this from `now()`
    # would make that mutant a microsecond-margin race instead of a clean kill.
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
    # VALUE, not presence — a dropped field is already caught by the `None` default, so only a
    # wrong non-null value survives. `fromisoformat` reads both `+00:00` and `Z`, staying
    # serialization-agnostic.
    assert datetime.fromisoformat(body["recoveryAt"]) == recovery_at
    assert seen == [(user.id, project.id)]


async def test_save_state_passes_an_unknown_dirty_through_as_null(
    client: AsyncClient, db_session: AsyncSession, wire
) -> None:
    """`dirty=None` is UNKNOWN, distinct from False — per `SaveState`'s docstring, rendering
    unknown as clean tells the user their work is safe when nobody checked. Not hypothetical:
    `project_save_state` returns `dirty=None` on three separate paths with a sandbox configured.

    The DANGEROUS direction is already caught elsewhere (hardcoding `dirty=False` fails the
    happy path above), so this pins the documented `None`-to-`False` collapse surviving. The
    sandbox stays at the `wire` default here on purpose: the no-sandbox test below asserts the
    manager is never called, so it can't cover a manager-produced `None`."""
    user, project = await _user_project(db_session, "save8b@rvaiglobal.com")
    app_id = uuid.uuid4()
    seen: list[tuple[uuid.UUID, uuid.UUID]] = []

    async def _fake_state(db, user, project_id, *, sandbox_client) -> SaveState:
        seen.append((user.id, project_id))
        return SaveState(app_id=app_id, dirty=None, container_head=None, saved_head=None)

    wire.manager.project_save_state = _fake_state

    resp = await client.get(_save_state_url(project.id), headers=auth_headers(user))

    assert resp.status_code == 200
    # EVERY field, not just `dirty` — a route that hardcoded `container_head=""` would
    # otherwise survive here, and this is one of only two tests that reach the manager.
    assert resp.json() == {
        "appId": str(app_id),
        "dirty": None,
        "containerHead": None,
        "savedHead": None,
        "recoveryAt": None,
    }
    assert seen == [(user.id, project.id)]


async def test_save_state_renders_a_never_built_project_as_a_null_app_id(
    client: AsyncClient, db_session: AsyncSession, wire
) -> None:
    """`project_save_state` really does return `app_id=None` — that is its no-app-yet path,
    which every project takes until its first build. The route converts with
    `str(state.app_id) if state.app_id else None`, and nothing pinned the `else` half: the
    only other test whose fake returns `app_id=None` is the no-sandbox one below, which by
    design asserts the manager is never called, so the conversion never runs there.

    Drop the guard to a bare `str(state.app_id)` and every other test in this file still
    passes while every never-built project starts reporting the string `"None"`."""
    user, project = await _user_project(db_session, "save8c@rvaiglobal.com")

    async def _fake_state(db, user, project_id, *, sandbox_client) -> SaveState:
        return SaveState(app_id=None, dirty=None, container_head=None, saved_head=None)

    wire.manager.project_save_state = _fake_state

    resp = await client.get(_save_state_url(project.id), headers=auth_headers(user))

    assert resp.status_code == 200
    assert resp.json()["appId"] is None


async def test_save_state_with_no_sandbox_configured_degrades_to_all_null(
    client: AsyncClient, db_session: AsyncSession, wire
) -> None:
    """No sandbox is a 200, not an error — `SaveStateResponse()`'s all-null shape is the
    honest "cannot compare" answer, matching `preview-state`'s own never-built case.

    Asserts the manager is never even called, not just that the response happens to be
    all-null — `project_save_state` on a project with no app ALSO returns an all-null
    `SaveState` by a different path (`existing_app_id` returns `None`), so a same-shaped
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
