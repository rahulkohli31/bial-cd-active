"""U6 — C3 control ops: start / stop / status (cookie auth + CSRF, owner-scoping)."""

from __future__ import annotations

import uuid

from httpx import AsyncClient
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.v1.build_sessions.deps import run_build_dependency
from src.services.build_sessions.locks import lock_is_held
from src.services.redis import BUILD_COORDINATION_UNAVAILABLE_MSG
from src.services.storage import StorageError
from tests.api.v1.build_sessions.conftest import (
    BlockingBrain,
    auth_headers,
    drain,
    seed_live_sandbox_state,
)
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
    assert body["lastSeq"] == 3


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


# --- U3: a Redis outage on the start path is a 503, never a 500 and never a false 409 ---
#
# One defect, two shapes, and testing either one alone leaves half of it standing:
#
#   HARD    — Redis answers nothing. `reconcile_user` runs BEFORE the acquire and calls the
#             deliberately-unguarded primitives, so it raises first and the old code let a
#             raw `RedisError` reach the catch-all handler: an opaque 500.
#   PARTIAL — Redis answers the reconcile and fails the acquire. `acquire_lock` swallowed
#             that into `None`, `_holding_user_lock` read `None` as contention, and the user
#             was told "A build session is already active" about a session that never
#             existed. That one is the worse bug, because it looks like a correct answer.


async def test_start_is_503_not_500_when_redis_is_entirely_unreachable(
    client: AsyncClient, db_session: AsyncSession, dead_redis, fake_storage, wire
) -> None:
    """The HARD shape (`Covers AE1`). Every command raises, so the failure surfaces out of
    reconcile — and must land on the retryable 503 with the approved copy, which the portal
    renders verbatim (`useBuildSession.ts:130`)."""
    wire.app.dependency_overrides[run_build_dependency] = lambda: FakeBrain()
    user, project = await _user_project(db_session, "ctl-redis-dead@rvaiglobal.com")
    resp = await client.post(
        "/v1/build-sessions",
        json={"projectId": str(project.id), "prompt": "build me an app"},
        headers=auth_headers(user),
    )
    assert resp.status_code == 503
    assert resp.status_code not in (409, 500)
    assert resp.json()["error"]["message"] == BUILD_COORDINATION_UNAVAILABLE_MSG
    assert wire.sbx.provisioned == []  # fail-closed: no container allocated on the way out


async def test_start_is_503_not_409_when_only_the_lock_acquire_fails(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, wire, monkeypatch
) -> None:
    """The PARTIAL shape — the false 409 itself.

    Only `set` is cursed. `reconcile_user` reads with hgetall/get/exists and sails through,
    so the request gets all the way to `acquire_lock` before anything fails; that is the one
    window where the old code produced a conflict out of an outage. Mutation check: revert
    `acquire_lock` to `return None` and this goes red with a 409.
    """
    wire.app.dependency_overrides[run_build_dependency] = lambda: FakeBrain()
    user, project = await _user_project(db_session, "ctl-redis-acq@rvaiglobal.com")

    async def only_the_acquire_is_down(*args: object, **kwargs: object) -> object:
        raise RedisError("redis is down")

    monkeypatch.setattr(fake_redis, "set", only_the_acquire_is_down)
    resp = await client.post(
        "/v1/build-sessions",
        json={"projectId": str(project.id), "prompt": "p"},
        headers=auth_headers(user),
    )
    assert resp.status_code == 503
    body = resp.json()["error"]
    assert body["message"] == BUILD_COORDINATION_UNAVAILABLE_MSG
    # The whole point: no conflict vocabulary anywhere in the response.
    assert body.get("code") != "build_session_already_active"
    assert "sessionId" not in body


async def test_start_is_503_when_redis_is_not_configured(
    client: AsyncClient, db_session: AsyncSession, fake_storage, wire
) -> None:
    """Deliberately FIXTURE-FREE (`.claude/rules/testing.md`): `fake_redis` binds the client
    singleton, so with it in place `RedisNotConfiguredError` is unreachable BY CONSTRUCTION
    and the branch could never be tested. Redis is genuinely optional outside production, so
    this is a supported deployment and it owes the caller a real status.

    `build_coordination_or_503`'s not-configured tier says PROCEED — correct for a gate like
    submit (nothing can hold a lock), wrong here, where coordination IS the operation. The
    route refuses instead of falling through into an unbound session.
    """
    wire.app.dependency_overrides[run_build_dependency] = lambda: FakeBrain()
    user, project = await _user_project(db_session, "ctl-redis-off@rvaiglobal.com")
    resp = await client.post(
        "/v1/build-sessions",
        json={"projectId": str(project.id), "prompt": "p"},
        headers=auth_headers(user),
    )
    assert resp.status_code == 503
    assert resp.json()["error"]["message"] == BUILD_COORDINATION_UNAVAILABLE_MSG
    assert wire.sbx.provisioned == []


async def test_start_reaps_through_anothers_dead_residue_at_the_acquire_seam(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, wire
) -> None:
    """U3/#10 — the walkthrough's back-to-back-builds 409, fixed at this seam.

    Registry + lock + heartbeat with NO in-process session is a dead session's residue
    (single-replica deploy contract: `_active_by_user` is authoritative), so the start
    reaps THROUGH it and succeeds — never a user-visible 409, never a 503. Genuine
    contention keeps its 409 at the in-process guard, proven by
    `test_second_start_while_live_is_409_carrying_session_id`; a residue whose teardown
    fails keeps the fail-closed 409 (see `test_reaper.py`'s certified-dead suite).
    """
    wire.app.dependency_overrides[run_build_dependency] = lambda: FakeBrain()
    user, project = await _user_project(db_session, "ctl-contend@rvaiglobal.com")
    await seed_live_sandbox_state(fake_redis, user.id)

    resp = await client.post(
        "/v1/build-sessions",
        json={"projectId": str(project.id), "prompt": "p"},
        headers=auth_headers(user),
    )
    assert resp.status_code == 201  # reaped through, never a user-visible 409
    assert wire.sbx.provisioned != []  # a fresh sandbox provisioned for the new build
    await drain(wire.manager, resp.json()["sessionId"])


async def test_start_documents_the_503_in_its_openapi_responses(client: AsyncClient) -> None:
    schema = (await client.get("/openapi.json")).json()
    responses = schema["paths"]["/v1/build-sessions"]["post"]["responses"]
    assert "503" in responses
    assert "coordination" in responses["503"]["description"]


async def test_start_is_503_when_the_sandbox_is_not_configured(
    client: AsyncClient, db_session: AsyncSession, app
) -> None:
    """Sibling of the relaunch case, and fixture-free on the sandbox for the same reason. The
    brain is bound so the `run_build is None` refusal above cannot mask the sandbox one — this
    route's documented 503 names BOTH ("Build engine not configured, or the sandbox or build
    coordination is temporarily unavailable"), so each arm needs its own proof."""
    app.dependency_overrides[run_build_dependency] = lambda: FakeBrain()
    user, project = await _user_project(db_session, "ctl-sbx-off@rvaiglobal.com")

    resp = await client.post(
        "/v1/build-sessions",
        json={"projectId": str(project.id), "prompt": "p"},
        headers=auth_headers(user),
    )

    assert resp.status_code == 503
    body = resp.json()
    assert (
        body["error"]["message"]
        == "Sandbox unavailable. Please try again later or contact the admin"
    )
    assert "detail" not in body


# --- stop-and-switch, over HTTP -------------------------------------------------------


async def _stop_active(client: AsyncClient, user, project, *, csrf: bool = True):
    return await client.post(
        f"/v1/build-sessions/projects/{project.id}/stop-active-build",
        headers=auth_headers(user, with_csrf=csrf),
    )


async def test_stop_active_build_settles_a_live_build_so_release_can_proceed(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, wire
) -> None:
    """THE ORDERING, end to end over HTTP: while the agent works, save and release BOTH refuse;
    after the stop returns, the release goes through.

    That is the whole reason this route exists. The switch dialog used to offer "Save and switch"
    to a user whose project was mid-build, and the server declined both halves — so the user
    got a choice, then an error, whichever button they pressed."""
    brain = BlockingBrain()
    wire.app.dependency_overrides[run_build_dependency] = lambda: brain
    user, project = await _user_project(db_session, "ctl-stop1@rvaiglobal.com")
    started = await client.post(
        "/v1/build-sessions",
        json={"projectId": str(project.id), "prompt": "build it"},
        headers=auth_headers(user),
    )
    assert started.status_code == 201
    sid = started.json()["sessionId"]

    # Mid-build, both onward steps refuse — this is what makes the stop necessary rather than
    # a nicety, and what keeps the ORDER an invariant instead of a client convention.
    save = await client.post(
        f"/v1/build-sessions/projects/{project.id}/save", headers=auth_headers(user)
    )
    assert save.status_code == 409
    assert "still being built" in save.json()["error"]["message"]
    release = await client.post(
        f"/v1/build-sessions/projects/{project.id}/release", headers=auth_headers(user)
    )
    assert release.status_code == 409

    # The gate stays SHUT. The stop has to be what ends this run — releasing the brain first
    # would let the build finish on its own and every assertion below would pass without the
    # route having done anything. Cancellation lands inside the brain's `wait()`, which is the
    # shape a real agent mid-write takes.
    stopped = await _stop_active(client, user, project)

    assert stopped.status_code == 200
    assert stopped.json()["stopped"] is True
    # Settled BY THE TIME IT ANSWERED — the release no longer conflicts.
    after = await client.post(
        f"/v1/build-sessions/projects/{project.id}/release", headers=auth_headers(user)
    )
    assert after.status_code != 409
    brain.release()  # nothing is waiting on it now; keeps teardown clean
    await drain(wire.manager, sid)


async def test_stopping_a_settled_project_is_200_false_not_an_error(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, wire
) -> None:
    """`stopped: false` is the answer, not a 409. The caller wants "settled" and it already is
    — and the common path is exactly this, because the build usually finishes while the user
    is still reading the dialog."""
    user, project = await _user_project(db_session, "ctl-stop2@rvaiglobal.com")
    resp = await _stop_active(client, user, project)
    assert resp.status_code == 200
    assert resp.json()["stopped"] is False


async def test_stop_active_build_is_owner_scoped_and_csrf_guarded(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, wire
) -> None:
    """ADR-0004 + KTD-4 on a route that KILLS WORK IN PROGRESS. Another user's project is a
    non-leaking 404, and a cookie without the CSRF header is refused — a forged cross-site POST
    here would destroy an unfinished build."""
    owner, project = await _user_project(db_session, "ctl-stop3@rvaiglobal.com")
    stranger = await UserFactory.create(db_session, email="ctl-stop4@rvaiglobal.com")

    assert (await _stop_active(client, stranger, project)).status_code == 404
    assert (await _stop_active(client, owner, project, csrf=False)).status_code == 403


async def test_stop_active_build_answers_without_redis(
    client: AsyncClient, db_session: AsyncSession, fake_storage, wire
) -> None:
    """This route ANSWERS with no Redis, where `release` and `save` must refuse — and that
    asymmetry is the reason it carries no `build_coordination_or_503` seam.

    Those two ask the registry what is live, so an absent coordination subsystem leaves them
    deciding nothing. This one asks "is this process running work for this user?", which lives
    in `_active_by_user` and is answerable regardless. Wrapping it in the seam produced a
    trailing `_coordination_is_gone()` that could never execute — the dead-arm shape this PR's
    review caught elsewhere — and would have refused on the one path that matters: a live
    in-process build during a Redis outage is exactly when a user still needs to stop it.

    Deliberately takes no `fake_redis` fixture: with the singleton unset `get_redis()` raises
    `RedisNotConfiguredError`, which is what a deployment with no Redis configured does."""
    user, project = await _user_project(db_session, "ctl-stop-noredis@rvaiglobal.com")
    resp = await _stop_active(client, user, project)
    assert resp.status_code == 200, resp.text
    assert resp.json() == {"stopped": False}  # nothing running, and it could still say so
