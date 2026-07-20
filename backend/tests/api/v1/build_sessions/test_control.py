"""U6 — C3 control ops: start / stop / status (cookie auth + CSRF, owner-scoping)."""

from __future__ import annotations

import uuid

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.v1.build_sessions.deps import run_build_dependency
from src.services.build_sessions.locks import lock_is_held
from src.services.storage import StorageError
from tests.api.v1.build_sessions.conftest import BlockingBrain, auth_headers, drain
from tests.factories import ProjectFactory, UserFactory
from tests.fakes import FakeBrain


async def _user_project(db: AsyncSession, email: str):
    user = await UserFactory.create(db, email=email)
    project = await ProjectFactory.create(db, user.id)
    return user, project


async def _no_sleep(_seconds: float) -> None:
    """Collapse the R6 retry backoff so the fail-closed path is tested at full speed."""


async def test_start_happy_returns_201_provisioning(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, wire
) -> None:
    wire.app.dependency_overrides[run_build_dependency] = lambda: FakeBrain()
    user, project = await _user_project(db_session, "ctl1@rvaiglobal.com")
    resp = await client.post(
        "/v1/build-sessions",
        json={"projectId": str(project.id), "prompt": "build me an app"},
        headers=auth_headers(user),
    )
    assert resp.status_code == 201
    body = resp.json()
    assert body["status"] == "provisioning"
    assert body["previewUrl"] is None  # camelCase wire, null until ready
    assert body["projectId"] == str(project.id)
    assert uuid.UUID(body["sessionId"]) and uuid.UUID(body["appId"])
    await drain(wire.manager, body["sessionId"])


async def test_start_without_cookie_is_401(
    client: AsyncClient, db_session: AsyncSession, wire
) -> None:
    wire.app.dependency_overrides[run_build_dependency] = lambda: FakeBrain()
    resp = await client.post(
        "/v1/build-sessions", json={"projectId": str(uuid.uuid4()), "prompt": "p"}
    )
    assert resp.status_code == 401


async def test_start_without_csrf_is_403(
    client: AsyncClient, db_session: AsyncSession, fake_redis, wire
) -> None:
    wire.app.dependency_overrides[run_build_dependency] = lambda: FakeBrain()
    user, project = await _user_project(db_session, "ctl2@rvaiglobal.com")
    resp = await client.post(
        "/v1/build-sessions",
        json={"projectId": str(project.id), "prompt": "p"},
        headers=auth_headers(user, with_csrf=False),
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "csrf_failed"


async def test_start_without_configured_brain_is_503(
    client: AsyncClient, db_session: AsyncSession, wire
) -> None:
    wire.app.dependency_overrides[run_build_dependency] = lambda: None
    user, project = await _user_project(db_session, "ctl3@rvaiglobal.com")
    resp = await client.post(
        "/v1/build-sessions",
        json={"projectId": str(project.id), "prompt": "p"},
        headers=auth_headers(user),
    )
    assert resp.status_code == 503  # None brain -> 503 BEFORE any Redis write


async def test_second_start_while_live_is_409_carrying_session_id(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, wire
) -> None:
    brain = BlockingBrain()
    wire.app.dependency_overrides[run_build_dependency] = lambda: brain
    user, project = await _user_project(db_session, "ctl4@rvaiglobal.com")
    r1 = await client.post(
        "/v1/build-sessions",
        json={"projectId": str(project.id), "prompt": "p"},
        headers=auth_headers(user),
    )
    assert r1.status_code == 201
    sid = r1.json()["sessionId"]
    r2 = await client.post(
        "/v1/build-sessions",
        json={"projectId": str(project.id), "prompt": "p2"},
        headers=auth_headers(user),
    )
    assert r2.status_code == 409
    err = r2.json()["error"]
    assert err["code"] == "build_session_already_active"
    assert err["sessionId"] == sid  # carries the existing session
    brain.release()
    await drain(wire.manager, sid)


async def test_status_after_completion_carries_preview_and_last_seq(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, wire
) -> None:
    wire.app.dependency_overrides[run_build_dependency] = lambda: FakeBrain()
    user, project = await _user_project(db_session, "ctl5@rvaiglobal.com")
    r = await client.post(
        "/v1/build-sessions",
        json={"projectId": str(project.id), "prompt": "p"},
        headers=auth_headers(user),
    )
    sid = r.json()["sessionId"]
    await drain(wire.manager, sid)  # let the fast brain run to the terminal ended
    s = await client.get(f"/v1/build-sessions/{sid}", headers=auth_headers(user))
    assert s.status_code == 200
    body = s.json()
    assert body["status"] == "ended"
    assert body["previewUrl"] == "https://preview.example/"
    assert body["lastSeq"] == 4


async def test_status_of_another_users_session_is_404(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, wire
) -> None:
    wire.app.dependency_overrides[run_build_dependency] = lambda: FakeBrain()
    owner, project = await _user_project(db_session, "ctl6a@rvaiglobal.com")
    intruder = await UserFactory.create(db_session, email="ctl6b@rvaiglobal.com")
    r = await client.post(
        "/v1/build-sessions",
        json={"projectId": str(project.id), "prompt": "p"},
        headers=auth_headers(owner),
    )
    sid = r.json()["sessionId"]
    await drain(wire.manager, sid)
    s = await client.get(f"/v1/build-sessions/{sid}", headers=auth_headers(intruder))
    assert s.status_code == 404  # non-leaking (ADR-0004)


async def test_stop_is_idempotent(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, wire
) -> None:
    brain = BlockingBrain()
    wire.app.dependency_overrides[run_build_dependency] = lambda: brain
    user, project = await _user_project(db_session, "ctl7@rvaiglobal.com")
    r = await client.post(
        "/v1/build-sessions",
        json={"projectId": str(project.id), "prompt": "p"},
        headers=auth_headers(user),
    )
    sid = r.json()["sessionId"]
    s1 = await client.post(f"/v1/build-sessions/{sid}/stop", json={}, headers=auth_headers(user))
    assert s1.status_code == 200 and s1.json()["status"] == "ended"
    s2 = await client.post(f"/v1/build-sessions/{sid}/stop", json={}, headers=auth_headers(user))
    assert s2.status_code == 200 and s2.json()["status"] == "ended"  # idempotent
    await drain(wire.manager, sid)


# --- R6: an unrestorable snapshot fails the start closed, in the user's words ---------


async def test_start_503s_with_the_exact_approved_copy_when_the_snapshot_is_unreachable(
    client: AsyncClient, db_session: AsyncSession, fake_redis, wire, monkeypatch
) -> None:
    # A head-check that never answers must abort the start with the USER-APPROVED wording,
    # verbatim, on a 503. The copy is pinned character-for-character (no trailing period):
    # the portal renders `error.message` as-is, so this string IS the user-facing text and a
    # well-meaning reword would silently change the product.
    from src.services.storage import accessor as storage_accessor
    from tests.fakes import FakeStorage

    class DeadStorage(FakeStorage):
        async def head(self, key):
            raise StorageError("blob is down", provider="fake", key=key)

    storage_accessor._backend_singleton = DeadStorage()
    monkeypatch.setattr("src.services.build_sessions.manager._asleep", _no_sleep)
    try:
        wire.app.dependency_overrides[run_build_dependency] = lambda: FakeBrain()
        user, project = await _user_project(db_session, "ctl8@rvaiglobal.com")
        resp = await client.post(
            "/v1/build-sessions",
            json={"projectId": str(project.id), "prompt": "refine it"},
            headers=auth_headers(user),
        )
        assert resp.status_code == 503
        assert (
            resp.json()["error"]["message"]
            == "Sandbox unavailable. Please try again later or contact the admin"
        )
        assert wire.sbx.provisioned == []  # no blank template left behind
        assert await lock_is_held(fake_redis, user.id) is False  # lock released
    finally:
        storage_accessor._backend_singleton = None
