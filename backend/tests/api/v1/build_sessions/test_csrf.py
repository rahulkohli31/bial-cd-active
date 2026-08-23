"""U6 — the reusable CSRF dependency (KTD-4): mutating POSTs require a valid signed
double-submit token; the status GET is exempt."""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps_rbac import superadmin_allowlist
from src.api.v1.build_sessions.deps import run_build_dependency
from src.config import settings
from src.services.auth.session_jwt import mint_session_jwt
from tests.api.v1.build_sessions.conftest import auth_headers, drain
from tests.factories import ProjectFactory, UserFactory
from tests.fakes import FakeBrain

_TTL = settings.auth.access_ttl_seconds

# Every mutating POST beyond `start` (which the two focused tests below already cover). The
# signed double-submit CSRF dependency short-circuits with a 403 BEFORE the route body — so a
# bogus path id is fine; the CSRF check fails first. `internal/reap` also carries the
# superadmin gate, so the caller is allowlisted to prove CSRF (not RBAC) is the failing check.
_MUTATING_POSTS = [
    "/v1/build-sessions/{sid}/stop",
    "/v1/build-sessions/{sid}/lock/acquire",
    "/v1/build-sessions/{sid}/lock/renew",
    "/v1/build-sessions/{sid}/lock/release",
    "/v1/build-sessions/{sid}/lock/force-end",
    "/v1/build-sessions/{sid}/heartbeat",
    "/v1/build-sessions/internal/reap",
    "/v1/build-sessions/projects/{project_id}/save",
    # U13 — the app's own client-error report. This table is HAND-MAINTAINED, not
    # auto-discovered from the route tree, so a new mutating POST is only covered here
    # because the change that added it added this row too.
    "/v1/build-sessions/projects/{project_id}/client-error",
    # U4 — the idle-tab workspace check. A POST rather than a GET because it costs a container
    # exec and can raise an operational alarm, which is exactly the kind of thing CSRF is for.
    "/v1/build-sessions/projects/{project_id}/workspace-check",
]


def _slug(path_tmpl: str) -> str:
    return (
        path_tmpl.strip("/")
        .replace("/", "-")
        .replace("{sid}", "sid")
        .replace("{project_id}", "project_id")
        .replace("{app_id}", "app_id")
    )


async def _user_project(db: AsyncSession, email: str):
    user = await UserFactory.create(db, email=email)
    project = await ProjectFactory.create(db, user.id)
    return user, project


async def test_valid_csrf_passes(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, wire
) -> None:
    wire.app.dependency_overrides[run_build_dependency] = lambda: FakeBrain()
    user, project = await _user_project(db_session, "csrf1@rvaiglobal.com")
    resp = await client.post(
        "/v1/build-sessions",
        json={"projectId": str(project.id), "prompt": "p"},
        headers=auth_headers(user),
    )
    assert resp.status_code == 201
    await drain(wire.manager, resp.json()["sessionId"])


async def test_missing_csrf_header_is_403(
    client: AsyncClient, db_session: AsyncSession, wire
) -> None:
    wire.app.dependency_overrides[run_build_dependency] = lambda: FakeBrain()
    user, project = await _user_project(db_session, "csrf2@rvaiglobal.com")
    resp = await client.post(
        "/v1/build-sessions",
        json={"projectId": str(project.id), "prompt": "p"},
        headers=auth_headers(user, with_csrf=False),
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "csrf_failed"


async def test_mismatched_csrf_token_is_403(
    client: AsyncClient, db_session: AsyncSession, wire
) -> None:
    wire.app.dependency_overrides[run_build_dependency] = lambda: FakeBrain()
    user, project = await _user_project(db_session, "csrf3@rvaiglobal.com")
    jwt = mint_session_jwt(user.id, user.token_version, _TTL)
    # Cookie CSRF and header CSRF disagree -> double-submit fails.
    headers = {"Cookie": f"session={jwt}; csrf=aaa.bbb", "X-CSRF-Token": "ccc.ddd"}
    resp = await client.post(
        "/v1/build-sessions",
        json={"projectId": str(project.id), "prompt": "p"},
        headers=headers,
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "csrf_failed"


@pytest.mark.parametrize("path_tmpl", _MUTATING_POSTS)
async def test_missing_csrf_header_is_403_on_every_mutating_post(
    client: AsyncClient, db_session: AsyncSession, wire, path_tmpl: str
) -> None:
    user = await UserFactory.create(
        db_session, email=f"csrf-miss-{_slug(path_tmpl)}@rvaiglobal.com"
    )
    wire.app.dependency_overrides[superadmin_allowlist] = lambda: frozenset({user.email})
    path = path_tmpl.format(sid=uuid.uuid4(), project_id=uuid.uuid4(), app_id=uuid.uuid4())
    resp = await client.post(path, headers=auth_headers(user, with_csrf=False))
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "csrf_failed"


@pytest.mark.parametrize("path_tmpl", _MUTATING_POSTS)
async def test_mismatched_csrf_token_is_403_on_every_mutating_post(
    client: AsyncClient, db_session: AsyncSession, wire, path_tmpl: str
) -> None:
    user = await UserFactory.create(
        db_session, email=f"csrf-mism-{_slug(path_tmpl)}@rvaiglobal.com"
    )
    wire.app.dependency_overrides[superadmin_allowlist] = lambda: frozenset({user.email})
    jwt = mint_session_jwt(user.id, user.token_version, _TTL)
    # Cookie CSRF and header CSRF disagree -> double-submit fails.
    headers = {"Cookie": f"session={jwt}; csrf=aaa.bbb", "X-CSRF-Token": "ccc.ddd"}
    path = path_tmpl.format(sid=uuid.uuid4(), project_id=uuid.uuid4(), app_id=uuid.uuid4())
    resp = await client.post(path, headers=headers)
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "csrf_failed"


async def test_status_get_needs_no_csrf(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, wire
) -> None:
    wire.app.dependency_overrides[run_build_dependency] = lambda: FakeBrain()
    user, project = await _user_project(db_session, "csrf4@rvaiglobal.com")
    r = await client.post(
        "/v1/build-sessions",
        json={"projectId": str(project.id), "prompt": "p"},
        headers=auth_headers(user),
    )
    sid = r.json()["sessionId"]
    await drain(wire.manager, sid)
    # A cookie-only GET (no X-CSRF-Token) is accepted.
    jwt = mint_session_jwt(user.id, user.token_version, _TTL)
    s = await client.get(f"/v1/build-sessions/{sid}", headers={"Cookie": f"session={jwt}"})
    assert s.status_code == 200
