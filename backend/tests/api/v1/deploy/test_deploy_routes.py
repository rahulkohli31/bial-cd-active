"""The two deploy routes.

The 202 is the load-bearing assertion. A deploy runs for minutes and the edge gateway times
out at twenty seconds, so a route that waited for the result would 504 on deploys that are
in fact going fine — and the citizen would retry, and the second claim would 409, and the
platform would look broken while doing exactly the right thing.

The 503 test is the other one worth having: FastAPI resolves every `Depends` BEFORE the
route body runs, so a provider that RAISED when publishing is unconfigured would escape the
body's own error handling and surface as a 500 with the wrong envelope. Asserting the status
AND the envelope shape is what pins that.
"""

from __future__ import annotations

import contextlib
import uuid
from types import SimpleNamespace

import pytest
from fastapi import FastAPI

from src.api.v1.build_sessions.deps import (
    sandbox_dependency,
    sandbox_or_none_dependency,
    session_manager_dependency,
)
from src.api.v1.deploy.deps import deploy_service_or_none
from src.services.build_sessions.manager import SessionManager
from src.services.deploy.service import DeployNotPossibleError, StartedDeploy
from tests.api.v1.build_sessions.conftest import auth_headers
from tests.factories import AppRegistryFactory, ProjectFactory, UserFactory
from tests.fakes import FakeSandboxClient

_DEPLOY = "/v1/projects/{pid}/deploy"
_STATUS = "/v1/projects/{pid}/deployment"


class FakeService:
    """Records what the route asked for; can refuse like the real claim does."""

    def __init__(self, *, refuse: DeployNotPossibleError | None = None) -> None:
        self.started: list[dict[str, object]] = []
        self._refuse = refuse

    async def start(self, db, *, user_id, app_id, project_id, conversation_id) -> StartedDeploy:
        if self._refuse is not None:
            raise self._refuse
        self.started.append(
            {"user_id": user_id, "app_id": app_id, "conversation_id": conversation_id}
        )
        return StartedDeploy(deployment_id=uuid.uuid4(), app_id=app_id)


class CleanSaveState:
    """A save-state view with nothing outstanding."""

    dirty = False


@pytest.fixture
def wire(app: FastAPI, db_session, monkeypatch):
    @contextlib.asynccontextmanager
    async def _session():
        yield db_session

    manager = SessionManager(session_factory=lambda: _session())
    monkeypatch.setattr(
        SessionManager,
        "project_save_state",
        lambda self, db, user, project_id, *, sandbox_client: _clean(),
    )
    sbx = FakeSandboxClient()
    service = FakeService()
    app.dependency_overrides[session_manager_dependency] = lambda: manager
    app.dependency_overrides[sandbox_dependency] = lambda: sbx
    app.dependency_overrides[sandbox_or_none_dependency] = lambda: sbx
    app.dependency_overrides[deploy_service_or_none] = lambda: service
    return SimpleNamespace(app=app, service=service, manager=manager)


async def _clean() -> CleanSaveState:
    return CleanSaveState()


async def _owner_with_app(db):
    user = await UserFactory.create(db)
    app_row = await AppRegistryFactory.create(db, user_id=user.id)
    return user, app_row


# --- starting a deploy -------------------------------------------------------------


async def test_a_deploy_returns_202_immediately(wire, client, db_session) -> None:
    """Never 200-after-waiting: the work takes minutes and the edge gives it twenty seconds."""
    user, app_row = await _owner_with_app(db_session)

    resp = await client.post(
        _DEPLOY.format(pid=app_row.project_id), headers=auth_headers(user), json={}
    )

    assert resp.status_code == 202
    body = resp.json()
    assert body["appId"] == str(app_row.id)
    assert body["deploymentId"]
    assert body["status"] == "running"


async def test_the_deploy_is_scoped_to_the_owner(wire, client, db_session) -> None:
    _owner, app_row = await _owner_with_app(db_session)
    stranger = await UserFactory.create(db_session, email="stranger@rvaiglobal.com")

    resp = await client.post(
        _DEPLOY.format(pid=app_row.project_id), headers=auth_headers(stranger), json={}
    )

    # A non-leaking 404 — never a 403 that confirms the project exists.
    assert resp.status_code == 404
    assert wire.service.started == []


async def test_a_project_with_no_app_is_refused_not_provisioned(wire, client, db_session) -> None:
    """The build path's resolver UPSERTS a draft app; deploy must not, or a Deploy on an
    empty project would quietly mint one and then fail on the missing snapshot."""
    user = await UserFactory.create(db_session)
    project = await ProjectFactory.create(db_session, user.id)

    resp = await client.post(_DEPLOY.format(pid=project.id), headers=auth_headers(user), json={})

    assert resp.status_code == 409
    assert "nothing to deploy" in resp.json()["error"]["message"].lower()


async def test_a_deploy_already_in_flight_is_a_409(app, client, db_session, monkeypatch) -> None:
    user, app_row = await _owner_with_app(db_session)

    @contextlib.asynccontextmanager
    async def _session():
        yield db_session

    monkeypatch.setattr(
        SessionManager,
        "project_save_state",
        lambda self, db, user, project_id, *, sandbox_client: _clean(),
    )
    app.dependency_overrides[session_manager_dependency] = lambda: SessionManager(
        session_factory=lambda: _session()
    )
    app.dependency_overrides[sandbox_or_none_dependency] = lambda: FakeSandboxClient()
    app.dependency_overrides[deploy_service_or_none] = lambda: FakeService(
        refuse=DeployNotPossibleError("already deploying", code="deploy_in_flight")
    )

    resp = await client.post(
        _DEPLOY.format(pid=app_row.project_id), headers=auth_headers(user), json={}
    )

    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "deploy_in_flight"


async def test_unsaved_work_is_refused_unless_save_first_is_asked_for(
    app, client, db_session, monkeypatch
) -> None:
    """A deploy ships the last SAVED version. Publishing while the workspace is ahead of it
    would ship something the citizen never chose, with no way to notice."""
    user, app_row = await _owner_with_app(db_session)

    class Dirty:
        dirty = True

    async def _dirty() -> Dirty:
        return Dirty()

    @contextlib.asynccontextmanager
    async def _session():
        yield db_session

    monkeypatch.setattr(
        SessionManager,
        "project_save_state",
        lambda self, db, user, project_id, *, sandbox_client: _dirty(),
    )
    app.dependency_overrides[session_manager_dependency] = lambda: SessionManager(
        session_factory=lambda: _session()
    )
    app.dependency_overrides[sandbox_or_none_dependency] = lambda: FakeSandboxClient()
    service = FakeService()
    app.dependency_overrides[deploy_service_or_none] = lambda: service

    resp = await client.post(
        _DEPLOY.format(pid=app_row.project_id), headers=auth_headers(user), json={}
    )

    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "unsaved_changes"
    assert service.started == []


async def test_csrf_is_required(wire, client, db_session) -> None:
    user, app_row = await _owner_with_app(db_session)

    resp = await client.post(
        _DEPLOY.format(pid=app_row.project_id),
        headers=auth_headers(user, with_csrf=False),
        json={},
    )

    assert resp.status_code == 403
    assert wire.service.started == []


async def test_publishing_unconfigured_is_a_503_with_the_right_envelope(
    app, client, db_session
) -> None:
    """The provider yields None rather than raising. A raising one would resolve BEFORE the
    route body and escape its error handling, producing a 500 with `{"detail": ...}` instead
    of the `{"error": {...}}` shape every other route on this surface returns."""
    user, app_row = await _owner_with_app(db_session)
    app.dependency_overrides[deploy_service_or_none] = lambda: None

    resp = await client.post(
        _DEPLOY.format(pid=app_row.project_id), headers=auth_headers(user), json={}
    )

    assert resp.status_code == 503
    assert "error" in resp.json()
    assert "message" in resp.json()["error"]


# --- reading the status ------------------------------------------------------------


async def test_a_never_deployed_app_reads_as_empty_not_missing(wire, client, db_session) -> None:
    """ "Never deployed" is a normal state a client renders as a Deploy button, not a 404."""
    user, app_row = await _owner_with_app(db_session)

    resp = await client.get(_STATUS.format(pid=app_row.project_id), headers=auth_headers(user))

    assert resp.status_code == 200
    body = resp.json()
    assert body["appId"] == str(app_row.id)
    assert body["deploymentId"] is None
    assert body["status"] is None


async def test_the_status_is_owner_scoped(wire, client, db_session) -> None:
    _owner, app_row = await _owner_with_app(db_session)
    stranger = await UserFactory.create(db_session, email="nosy@rvaiglobal.com")

    resp = await client.get(_STATUS.format(pid=app_row.project_id), headers=auth_headers(stranger))

    assert resp.status_code == 404
