"""U6 — the five lock ops + the superadmin internal/reap (owner-scoping: 404 everywhere
except the one force-end 403)."""

from __future__ import annotations

import uuid

import pytest
from httpx import AsyncClient
from redis.exceptions import RedisError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps_rbac import superadmin_allowlist
from src.api.v1.build_sessions.deps import run_build_dependency
from src.db.models.audit import AuditLog
from src.services.build_sessions import BuildSession
from src.services.redis import (
    BUILD_COORDINATION_UNAVAILABLE_MSG,
    REGISTRY_STATE_READY,
    lock_key,
    registry_key,
)
from src.services.redis.keys import (
    REGISTRY_FIELD_APP_NAME,
    REGISTRY_FIELD_CREATED_AT,
    REGISTRY_FIELD_FQDN,
    REGISTRY_FIELD_STATE,
    REGISTRY_FIELD_TOKEN_REF,
)
from src.services.sandbox.base import SandboxHandle
from tests.api.v1.build_sessions.conftest import BlockingBrain, auth_headers, drain
from tests.factories import ProjectFactory, UserFactory
from tests.fakes import FakeBrain, a_sandbox_name


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
            REGISTRY_FIELD_APP_NAME: a_sandbox_name("stale"),
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


async def test_internal_reap_is_audited(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, wire
) -> None:
    # Every superadmin-gated action is audited (ADR-0005): a successful reap writes ONE
    # accountability row with the sweep count in `detail`.
    wire.app.dependency_overrides[run_build_dependency] = lambda: FakeBrain()
    admin = await UserFactory.create(db_session, email="lk6-admin@rvaiglobal.com")
    wire.app.dependency_overrides[superadmin_allowlist] = lambda: frozenset({admin.email})

    stale = uuid.uuid4()
    await fake_redis.hset(
        registry_key(stale),
        mapping={
            REGISTRY_FIELD_APP_NAME: a_sandbox_name("stale"),
            REGISTRY_FIELD_FQDN: "stale.example",
            REGISTRY_FIELD_TOKEN_REF: "ref",
            REGISTRY_FIELD_CREATED_AT: "2026-07-14T00:00:00+00:00",
            REGISTRY_FIELD_STATE: REGISTRY_STATE_READY,
        },
    )
    await fake_redis.set(lock_key(stale), "crashed", ex=900)

    ok = await client.post("/v1/build-sessions/internal/reap", headers=auth_headers(admin))
    assert ok.status_code == 200 and ok.json()["reaped"] == 1

    row = (
        await db_session.execute(select(AuditLog).where(AuditLog.action == "build_session.reap"))
    ).scalar_one()
    assert row.actor_id == admin.id
    assert row.resource_type == "build_session"
    # `failed` rides in the audit row too: "reaped 0" alone cannot distinguish a clean
    # sweep from one in which every user threw.
    assert row.detail == {"reaped": 1, "failed": 0}


async def test_acquire_409_already_active_when_another_session_holds_the_lock(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, wire
) -> None:
    # C3 §3.1 (#14): a live session B holds the one-per-user lock; acquiring on a DIFFERENT
    # owned session A → 409 `build_session_already_active` carrying B's id — NOT the
    # `build_session_lock_lost` the old acquire==renew impl always returned.
    user, sid_b, brain = await _live_session(client, db_session, wire, "lk-acq@rvaiglobal.com")
    # A second owned-but-not-active session (an earlier, ended session still held in memory).
    stale = BuildSession(
        session_id=uuid.uuid7(),
        user_id=user.id,
        project_id=uuid.uuid4(),
        app_id=uuid.uuid4(),
        prompt="p",
        lock_token="stale-tok",
        handle=SandboxHandle(
            fqdn="a.example",
            token="t",
            app_name=a_sandbox_name("a"),
            preview_url="https://a.example/",
            ready=False,
        ),
    )
    wire.manager._sessions[stale.session_id] = stale

    r = await client.post(
        f"/v1/build-sessions/{stale.session_id}/lock/acquire", headers=auth_headers(user)
    )
    assert r.status_code == 409
    err = r.json()["error"]
    assert err["code"] == "build_session_already_active"
    assert err["sessionId"] == sid_b  # carries the LIVE session, not the acquired-on one

    brain.release()
    await drain(wire.manager, sid_b)


# --- U3: the lock ops answer a Redis outage with 503, never a 500 and never a lie -------


@pytest.mark.parametrize(
    ("path", "cursed_command"),
    [
        ("lock/renew", "eval"),
        ("lock/release", "eval"),
        ("heartbeat", "set"),
    ],
)
async def test_lock_ops_503_on_a_redis_outage(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_redis,
    fake_storage,
    wire,
    path: str,
    cursed_command: str,
) -> None:
    """These three routes call the BARE primitives (`locks.py`'s REDIS-ERROR POLICY), so
    before U3 an outage on any of them was an opaque 500.

    Guarding inside the primitive is not the alternative — that policy spells out why it
    would be worse here: `release_lock_as_holder` swallowing would make this route answer
    `200 {"released": true}` about a release that never happened, and `write_heartbeat`
    swallowing would answer `200 {"alive": true}` with an expiry the portal then schedules
    its next beat against. A 503 is the only answer that is both non-500 and true.
    """
    user, sid, brain = await _live_session(client, db_session, wire, f"lk-503-{path[:5]}@x.com")

    async def the_store_is_gone(*args: object, **kwargs: object) -> object:
        raise RedisError("redis is down")

    # Undone before the drain: the live session's own finalize needs a working Redis, and a
    # cursed teardown would mask the assertion below with unrelated noise.
    curse = pytest.MonkeyPatch()
    curse.setattr(fake_redis, cursed_command, the_store_is_gone)
    try:
        resp = await client.post(f"/v1/build-sessions/{sid}/{path}", headers=auth_headers(user))
    finally:
        curse.undo()

    assert resp.status_code == 503
    body = resp.json()["error"]
    assert body["message"] == BUILD_COORDINATION_UNAVAILABLE_MSG
    # Not a 409 either: `renew_lock` returning False means the token stopped matching, and
    # an unanswered Redis is not evidence of that. Ending a healthy build on a phantom
    # "lock lost" is the failure this arm exists to prevent.
    assert body.get("code") != "build_session_lock_lost"

    brain.release()
    await drain(wire.manager, sid)


async def test_lock_ops_document_the_503_in_their_openapi_responses(client: AsyncClient) -> None:
    schema = (await client.get("/openapi.json")).json()
    for path in (
        "/v1/build-sessions/{session_id}/lock/acquire",
        "/v1/build-sessions/{session_id}/lock/renew",
        "/v1/build-sessions/{session_id}/lock/release",
        "/v1/build-sessions/{session_id}/heartbeat",
        "/v1/build-sessions/internal/reap",
    ):
        assert "503" in schema["paths"][path]["post"]["responses"], path


# --- internal/reap: a Redis outage is a 503 to the operator, never a 500 -----------------


async def test_internal_reap_is_503_on_a_redis_outage(
    client: AsyncClient, db_session: AsyncSession, fake_redis, wire
) -> None:
    """A total Redis outage during the reconciliation sweep is a retryable 503 to the operator,
    never an opaque 500: the sweep runs inside `build_coordination_or_503`, and the audit row lives
    INSIDE the seam AFTER the sweep, so a sweep that never ran writes NO accountability row and
    nothing is committed. `scan_iter` is cursed because that is the first Redis call `sweep_all`
    makes (an async generator, so it must raise on iteration)."""
    admin = await UserFactory.create(db_session, email="lk7-admin@rvaiglobal.com")
    wire.app.dependency_overrides[superadmin_allowlist] = lambda: frozenset({admin.email})

    async def scan_iter_is_gone(*_args: object, **_kwargs: object):
        raise RedisError("redis is down")
        yield  # unreachable: forces an async generator so `async for` raises on the first step

    curse = pytest.MonkeyPatch()
    curse.setattr(fake_redis, "scan_iter", scan_iter_is_gone)
    try:
        resp = await client.post("/v1/build-sessions/internal/reap", headers=auth_headers(admin))
    finally:
        curse.undo()

    assert resp.status_code == 503
    assert resp.status_code != 500
    assert resp.json()["error"]["message"] == BUILD_COORDINATION_UNAVAILABLE_MSG
    # No audit row for a sweep that never ran (the row is deliberately inside the seam).
    row = await db_session.scalar(select(AuditLog).where(AuditLog.action == "build_session.reap"))
    assert row is None


async def test_internal_reap_is_503_not_500_when_redis_is_not_configured(
    client: AsyncClient, db_session: AsyncSession, wire
) -> None:
    """FIX 1 regression, deliberately FIXTURE-FREE (`.claude/rules/testing.md`): no `fake_redis`
    bound, so `get_redis()` raises `RedisNotConfiguredError` INSIDE the seam and the trailing
    `_coordination_is_gone()` answers 503. Before FIX 1 the eager `RedisDep` raised that at
    dependency-solve time → an undocumented 500. No sweep, so no audit row."""
    admin = await UserFactory.create(db_session, email="lk7b-admin@rvaiglobal.com")
    wire.app.dependency_overrides[superadmin_allowlist] = lambda: frozenset({admin.email})

    resp = await client.post("/v1/build-sessions/internal/reap", headers=auth_headers(admin))
    assert resp.status_code == 503
    assert resp.status_code != 500
    assert resp.json()["error"]["message"] == BUILD_COORDINATION_UNAVAILABLE_MSG
    row = await db_session.scalar(select(AuditLog).where(AuditLog.action == "build_session.reap"))
    assert row is None


# --- FIX 1 regression: the lock ops answer a Redis-off deployment with 503, not a 500 -----
#
# Deliberately FIXTURE-FREE (no `fake_redis`): `fake_redis` binds the client singleton, so with it
# in place `RedisNotConfiguredError` is unreachable BY CONSTRUCTION and this branch could never be
# tested (`.claude/rules/testing.md`). Before FIX 1 these routes took `redis: RedisDep`, resolved
# by FastAPI BEFORE the body ran, so a store-off deployment raised at dependency-solve time → an
# undocumented 500 `{"detail": ...}`. Now Redis is acquired lazily INSIDE the seam, so the answer
# is the documented 503 `{"error": {...}}`.


def _inject_owned_session(manager, user_id: uuid.UUID) -> uuid.UUID:
    """Register a valid OWNED (but not `_active_by_user`) session directly in the manager, so a
    lock op passes the 404 ownership check and reaches the Redis seam WITHOUT a real build — which
    could not start here, because starting one needs Redis. Mirrors the direct-injection the
    already-active 409 test above uses."""
    session = BuildSession(
        session_id=uuid.uuid7(),
        user_id=user_id,
        project_id=uuid.uuid4(),
        app_id=uuid.uuid4(),
        prompt="p",
        lock_token="tok",
        handle=SandboxHandle(
            fqdn="a.example",
            token="t",
            app_name=a_sandbox_name("a"),
            preview_url="https://a.example/",
            ready=False,
        ),
    )
    manager._sessions[session.session_id] = session
    return session.session_id


@pytest.mark.parametrize("path", ["lock/acquire", "lock/renew", "lock/release", "heartbeat"])
async def test_lock_op_is_503_not_500_when_redis_is_not_configured(
    client: AsyncClient, db_session: AsyncSession, wire, path: str
) -> None:
    user = await UserFactory.create(db_session, email=f"lk-off-{path.replace('/', '-')}@x.com")
    sid = _inject_owned_session(wire.manager, user.id)

    resp = await client.post(f"/v1/build-sessions/{sid}/{path}", headers=auth_headers(user))
    assert resp.status_code == 503
    assert resp.status_code != 500
    body = resp.json()
    assert body["error"]["message"] == BUILD_COORDINATION_UNAVAILABLE_MSG
    assert "detail" not in body  # the 503 `{"error": {...}}` envelope, never a 500 `{"detail": …}`


@pytest.mark.parametrize("path", ["lock/acquire", "lock/renew", "lock/release", "heartbeat"])
async def test_lock_op_404s_a_bogus_session_before_touching_redis(
    client: AsyncClient, db_session: AsyncSession, wire, path: str
) -> None:
    # The ownership check runs BEFORE Redis: a bogus/unowned session id is a 404 even with no Redis
    # configured (which would otherwise 503), proving `_owned_or_404` still fires first after FIX 1
    # moved Redis acquisition into the body.
    user = await UserFactory.create(db_session, email=f"lk-404-{path.replace('/', '-')}@x.com")
    resp = await client.post(
        f"/v1/build-sessions/{uuid.uuid4()}/{path}", headers=auth_headers(user)
    )
    assert resp.status_code == 404
