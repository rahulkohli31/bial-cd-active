"""U6 — the reusable CSRF dependency (KTD-4): mutating POSTs require a valid signed
double-submit token; the status GET is exempt."""

from __future__ import annotations

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.v1.build_sessions.deps import run_build_dependency
from src.config import settings
from src.services.auth.session_jwt import mint_session_jwt
from tests.api.v1.build_sessions.conftest import auth_headers, drain
from tests.factories import ProjectFactory, UserFactory
from tests.fakes import FakeBrain

_TTL = settings.auth.access_ttl_seconds


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
