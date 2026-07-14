"""U6 — the five lock ops + the superadmin internal/reap (owner-scoping: 404 everywhere
except the one force-end 403)."""

from __future__ import annotations

import uuid

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps_rbac import superadmin_allowlist
from src.api.v1.build_sessions.deps import run_build_dependency
from src.services.redis import REGISTRY_STATE_READY, lock_key, registry_key
from src.services.redis.keys import (
    REGISTRY_FIELD_APP_NAME,
    REGISTRY_FIELD_CREATED_AT,
    REGISTRY_FIELD_FQDN,
    REGISTRY_FIELD_STATE,
    REGISTRY_FIELD_TOKEN_REF,
)
from tests.api.v1.build_sessions.conftest import BlockingBrain, auth_headers, drain
from tests.factories import ProjectFactory, UserFactory
from tests.fakes import FakeBrain


async def _live_session(client, db, wire, email):
    """Start a session kept live by a BlockingBrain; returns (user, session_id, brain)."""
    brain = BlockingBrain()
    wire.app.dependency_overrides[run_build_dependency] = lambda: brain
    user = await UserFactory.create(db, email=email)
    project = await ProjectFactory.create(db, user.id)
    r = await client.post(
        "/v1/build-sessions",
        json={"projectId": str(project.id), "prompt": "p"},
        headers=auth_headers(user),
    )
    assert r.status_code == 201
    return user, r.json()["sessionId"], brain


async def test_acquire_renew_release_happy(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, wire
) -> None:
    user, sid, brain = await _live_session(client, db_session, wire, "lk1@rvaiglobal.com")
    acq = await client.post(f"/v1/build-sessions/{sid}/lock/acquire", headers=auth_headers(user))
    assert acq.status_code == 200
    body = acq.json()
    assert body["held"] is True
    assert body["ownerUserId"] == str(user.id)
    assert body["ttlSeconds"] == 900
    assert "expiresAt" in body

    ren = await client.post(f"/v1/build-sessions/{sid}/lock/renew", headers=auth_headers(user))
    assert ren.status_code == 200 and ren.json()["held"] is True

    rel = await client.post(f"/v1/build-sessions/{sid}/lock/release", headers=auth_headers(user))
    assert rel.status_code == 200 and rel.json()["released"] is True

    brain.release()
    await drain(wire.manager, sid)


async def test_renew_a_lost_lock_is_409(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, wire
) -> None:
    user, sid, brain = await _live_session(client, db_session, wire, "lk2@rvaiglobal.com")
    # Drop the lock out from under the session, then renew -> lock lost.
    await fake_redis.delete(lock_key(user.id))
    ren = await client.post(f"/v1/build-sessions/{sid}/lock/renew", headers=auth_headers(user))
    assert ren.status_code == 409
    assert ren.json()["error"]["code"] == "build_session_lock_lost"
    brain.release()
    await drain(wire.manager, sid)


async def test_heartbeat_happy_and_cross_user_404(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, wire
) -> None:
    user, sid, brain = await _live_session(client, db_session, wire, "lk3@rvaiglobal.com")
    hb = await client.post(f"/v1/build-sessions/{sid}/heartbeat", headers=auth_headers(user))
    assert hb.status_code == 200
    body = hb.json()
    assert body["alive"] is True
    assert body["cadenceSeconds"] == 30
    assert "heartbeatExpiresAt" in body

    intruder = await UserFactory.create(db_session, email="lk3b@rvaiglobal.com")
    other = await client.post(
        f"/v1/build-sessions/{sid}/heartbeat", headers=auth_headers(intruder)
    )
    assert other.status_code == 404  # another user's session -> 404

    brain.release()
    await drain(wire.manager, sid)


async def test_force_end_owner_200_nonowner_403_unknown_404(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, wire
) -> None:
    user, sid, brain = await _live_session(client, db_session, wire, "lk4@rvaiglobal.com")
    intruder = await UserFactory.create(db_session, email="lk4b@rvaiglobal.com")

    # Non-owner on an EXISTING session -> 403 (the one owner-asserted route).
    forbidden = await client.post(
        f"/v1/build-sessions/{sid}/lock/force-end", headers=auth_headers(intruder)
    )
    assert forbidden.status_code == 403
    assert forbidden.json()["error"]["code"] == "build_session_forbidden"

    # Unknown session -> 404.
    unknown = await client.post(
        f"/v1/build-sessions/{uuid.uuid4()}/lock/force-end", headers=auth_headers(user)
    )
    assert unknown.status_code == 404

    # Owner -> 200 ended.
    ok = await client.post(f"/v1/build-sessions/{sid}/lock/force-end", headers=auth_headers(user))
    assert ok.status_code == 200 and ok.json()["status"] == "ended"
    await drain(wire.manager, sid)


async def test_internal_reap_superadmin_only_and_idempotent(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, wire
) -> None:
    wire.app.dependency_overrides[run_build_dependency] = lambda: FakeBrain()
    citizen = await UserFactory.create(db_session, email="lk5-citizen@rvaiglobal.com")
    admin = await UserFactory.create(db_session, email="lk5-admin@rvaiglobal.com")
    wire.app.dependency_overrides[superadmin_allowlist] = lambda: frozenset({admin.email})

    # A citizen is denied by the superadmin gate.
    denied = await client.post("/v1/build-sessions/internal/reap", headers=auth_headers(citizen))
    assert denied.status_code == 403

    # Seed a stale sandbox (registry + lock, no heartbeat) so the sweep reaps exactly one.
    stale = uuid.uuid4()
    await fake_redis.hset(
        registry_key(stale),
        mapping={
            REGISTRY_FIELD_APP_NAME: "sbx-stale",
            REGISTRY_FIELD_FQDN: "stale.example",
            REGISTRY_FIELD_TOKEN_REF: "ref",
            REGISTRY_FIELD_CREATED_AT: "2026-07-14T00:00:00+00:00",
            REGISTRY_FIELD_STATE: REGISTRY_STATE_READY,
        },
    )
    await fake_redis.set(lock_key(stale), "crashed", ex=900)

    ok = await client.post("/v1/build-sessions/internal/reap", headers=auth_headers(admin))
    assert ok.status_code == 200
    assert ok.json()["reaped"] == 1
    # A second immediate sweep is a clean no-op (idempotent / timer-safe).
    again = await client.post("/v1/build-sessions/internal/reap", headers=auth_headers(admin))
    assert again.status_code == 200 and again.json()["reaped"] == 0
