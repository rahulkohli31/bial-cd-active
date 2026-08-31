"""#43 — POST /v1/build-sessions/relaunch: restore a torn-down app from its snapshot into a
fresh, READY sandbox (cookie auth + CSRF, owner-scoping, Decision-6 no-build-slot)."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
import sqlalchemy as sa
from httpx import AsyncClient
from pydantic import SecretStr
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.v1.build_sessions.deps import (
    run_build_dependency,
    sandbox_dependency,
    sandbox_or_none_dependency,
)
from src.api.v1.build_sessions.schemas import BuildSessionStatus
from src.db.base import async_session_factory
from src.db.models.app_registry import AppRegistry
from src.db.models.conversation import ConversationKind
from src.db.models.harness_counter import HarnessCount, HarnessCounter
from src.services.build_sessions.appdata import resolve_app_for_project
from src.services.build_sessions.locks import lock_is_held
from src.services.build_sessions.manager import app_name_for
from src.services.build_sessions.outcome import write_build_outcome
from src.services.redis import (
    BUILD_COORDINATION_UNAVAILABLE_MSG,
    REGISTRY_STATE_ENDING,
    registry_key,
)
from src.services.redis.keys import REGISTRY_FIELD_STATE
from src.services.sandbox.aca import AcaControlPlane, AcaTransientError
from src.services.sandbox.base import (
    SandboxError,
    SandboxGoneError,
    SandboxHandle,
    SandboxNotReadyError,
)
from src.services.sandbox.client import AcaSandboxClient
from src.services.sandbox.config import SandboxConfig
from src.services.storage import recovery_key, snapshot_key
from tests.api.v1.build_sessions.conftest import (
    BlockingBrain,
    auth_headers,
    drain,
    seed_live_sandbox_state,
)
from tests.conftest import forget_every_harness_count
from tests.factories import ConversationFactory, ProjectFactory, UserFactory


async def _user_project(db: AsyncSession, email: str):
    user = await UserFactory.create(db, email=email)
    project = await ProjectFactory.create(db, user.id)
    return user, project


async def _seed_snapshot(db: AsyncSession, user, project, store) -> uuid.UUID:
    """Provision the app row (so `resolve_app_for_project` inside the endpoint is idempotent)
    and stage a snapshot bundle the relaunch restores."""
    app_id = await resolve_app_for_project(db, user.id, project.id)
    await db.commit()
    await store.put(snapshot_key(app_id), b"BUNDLE")
    return app_id


async def _seed_worked_on(store, app_id: uuid.UUID) -> None:
    """Mark this app as holding real work.

    The reclaim guard exempts a workspace with NOTHING in it — no commit, nothing saved, no
    recovery bundle — because a Plan-only turn on an untouched template must not block another
    project. A recovery bundle is one of the three proofs that a turn actually touched files
    (`finish_turn_sandbox` writes it on `touched=True`), so seeding it is how a test says "this
    project has been worked on" without scripting the container's git state."""
    key = recovery_key(app_id)
    await store.put(key, b"RECOVERY-BUNDLE")
    # `FakeStorage.head` reads `last_modified` off `mtimes`, and the guard keys on that
    # timestamp — a bundle with no mtime reads as "no recovery bundle".
    store.mtimes[key] = datetime.now(UTC)


async def test_relaunch_happy_returns_200_ready_preview(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, wire
) -> None:
    user, project = await _user_project(db_session, "rl1@rvaiglobal.com")
    app_id = await _seed_snapshot(db_session, user, project, fake_storage)

    resp = await client.post(
        "/v1/build-sessions/relaunch",
        json={"projectId": str(project.id)},
        headers=auth_headers(user),
    )

    assert resp.status_code == 200
    body = resp.json()
    assert body["appId"] == str(app_id)
    assert body["status"] == "ready"
    assert body["previewUrl"].startswith("https://")  # a live, framable URL
    assert body["restoredFromFailedBuild"] is False  # no failed outcome → no label
    # Decision 6: relaunch did NOT occupy the build slot — the lock is free, no live session.
    assert wire.manager._active_by_user == {}
    assert await lock_is_held(fake_redis, user.id) is False


async def test_relaunch_after_failed_build_signals_last_saved_version(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, wire
) -> None:
    # U6 (F1): the project's newest recorded outcome FAILED, so the restored snapshot is the
    # last SAVED state — the wire flag drives the "Relaunch last saved version" label.
    user, project = await _user_project(db_session, "rl6@rvaiglobal.com")
    await _seed_snapshot(db_session, user, project, fake_storage)
    conv = await ConversationFactory.create(
        db_session, user.id, project_id=project.id, kind=ConversationKind.BUILDER
    )
    await write_build_outcome(
        db_session,
        user_id=user.id,
        conversation_id=conv.id,
        session_id=uuid.uuid4(),
        status=BuildSessionStatus.FAILED,
        preview_url=None,
        snapshot_committed=True,
        reason="build_failed",
    )

    resp = await client.post(
        "/v1/build-sessions/relaunch",
        json={"projectId": str(project.id)},
        headers=auth_headers(user),
    )
    assert resp.status_code == 200
    assert resp.json()["restoredFromFailedBuild"] is True


async def test_relaunch_without_snapshot_is_404(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, wire
) -> None:
    # A never-built project has nothing to relaunch — a definite 404, not a blank preview.
    user, project = await _user_project(db_session, "rl2@rvaiglobal.com")
    resp = await client.post(
        "/v1/build-sessions/relaunch",
        json={"projectId": str(project.id)},
        headers=auth_headers(user),
    )
    assert resp.status_code == 404
    assert wire.sbx.provisioned == []  # never a blank template
    # F17: the 404 path must not mint a phantom DRAFT app row. The speculative upsert was
    # never committed; production `get_db` rolls it back on the error response — mirror that
    # rollback, then prove nothing survived it.
    await db_session.rollback()
    count = await db_session.scalar(
        sa.select(sa.func.count())
        .select_from(AppRegistry)
        .where(AppRegistry.project_id == project.id)
    )
    assert count == 0


async def test_relaunch_while_a_build_is_running_is_409(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, wire
) -> None:
    # A live build owns the one-per-user slot; relaunch 409s and carries the live session id.
    brain = BlockingBrain()
    wire.app.dependency_overrides[run_build_dependency] = lambda: brain
    user, project = await _user_project(db_session, "rl3@rvaiglobal.com")
    await _seed_snapshot(db_session, user, project, fake_storage)

    started = await client.post(
        "/v1/build-sessions",
        json={"projectId": str(project.id), "prompt": "build it"},
        headers=auth_headers(user),
    )
    assert started.status_code == 201
    sid = started.json()["sessionId"]

    conflict = await client.post(
        "/v1/build-sessions/relaunch",
        json={"projectId": str(project.id)},
        headers=auth_headers(user),
    )
    assert conflict.status_code == 409
    err = conflict.json()["error"]
    assert err["code"] == "build_session_already_active"
    assert err["sessionId"] == sid

    brain.release()
    await drain(wire.manager, sid)


async def test_relaunch_another_users_project_is_404(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, wire
) -> None:
    # Owner-scoped (ADR-0004): relaunching another user's project is a non-leaking 404.
    owner, project = await _user_project(db_session, "rl4-owner@rvaiglobal.com")
    await _seed_snapshot(db_session, owner, project, fake_storage)
    intruder = await UserFactory.create(db_session, email="rl4-intruder@rvaiglobal.com")

    resp = await client.post(
        "/v1/build-sessions/relaunch",
        json={"projectId": str(project.id)},
        headers=auth_headers(intruder),
    )
    assert resp.status_code == 404


async def test_relaunch_without_csrf_is_403(
    client: AsyncClient, db_session: AsyncSession, fake_redis, wire
) -> None:
    user, project = await _user_project(db_session, "rl5@rvaiglobal.com")
    resp = await client.post(
        "/v1/build-sessions/relaunch",
        json={"projectId": str(project.id)},
        headers=auth_headers(user, with_csrf=False),
    )
    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "csrf_failed"


# --- U3: the same 503/409 matrix, because relaunch takes the same per-user lock ---------
#
# Relaunch runs through `_holding_user_lock` exactly as a start does, so it inherited the
# identical defect: a Redis blip told the user a build was already running. It is NOT
# covered by the start-path tests — it is a separate route with its own `except` arms and
# its own 409 (`test_relaunch_while_a_build_is_running_is_409`), and the two could drift.


async def test_relaunch_is_503_not_500_when_redis_is_entirely_unreachable(
    client: AsyncClient, db_session: AsyncSession, dead_redis, fake_storage, wire
) -> None:
    # HARD shape: reconcile raises first. The snapshot the relaunch would restore is
    # untouched, and no container is created — the user retries, they do not lose work.
    user, project = await _user_project(db_session, "rl-redis-dead@rvaiglobal.com")
    await _seed_snapshot(db_session, user, project, fake_storage)

    resp = await client.post(
        "/v1/build-sessions/relaunch",
        json={"projectId": str(project.id)},
        headers=auth_headers(user),
    )
    assert resp.status_code == 503
    assert resp.status_code not in (409, 500)
    assert resp.json()["error"]["message"] == BUILD_COORDINATION_UNAVAILABLE_MSG
    assert wire.sbx.restored == []


async def test_relaunch_is_503_not_409_when_only_the_lock_acquire_fails(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, wire, monkeypatch
) -> None:
    # PARTIAL shape — the false 409. Cursing only `set` lets the reconcile succeed, so the
    # request reaches `acquire_lock`, which is the sole place the old code manufactured a
    # conflict out of an outage.
    user, project = await _user_project(db_session, "rl-redis-acq@rvaiglobal.com")
    await _seed_snapshot(db_session, user, project, fake_storage)

    async def only_the_acquire_is_down(*args: object, **kwargs: object) -> object:
        raise RedisError("redis is down")

    monkeypatch.setattr(fake_redis, "set", only_the_acquire_is_down)
    resp = await client.post(
        "/v1/build-sessions/relaunch",
        json={"projectId": str(project.id)},
        headers=auth_headers(user),
    )
    assert resp.status_code == 503
    body = resp.json()["error"]
    assert body["message"] == BUILD_COORDINATION_UNAVAILABLE_MSG
    assert body.get("code") != "build_session_already_active"


async def test_relaunch_is_503_when_redis_is_not_configured(
    client: AsyncClient, db_session: AsyncSession, fake_storage, wire
) -> None:
    # Fixture-free, mirroring the start path: `fake_redis` would make this branch
    # unreachable by construction.
    user, project = await _user_project(db_session, "rl-redis-off@rvaiglobal.com")
    await _seed_snapshot(db_session, user, project, fake_storage)
    resp = await client.post(
        "/v1/build-sessions/relaunch",
        json={"projectId": str(project.id)},
        headers=auth_headers(user),
    )
    assert resp.status_code == 503
    assert resp.json()["error"]["message"] == BUILD_COORDINATION_UNAVAILABLE_MSG


async def test_relaunch_reaps_through_anothers_dead_residue_at_the_acquire_seam(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, wire
) -> None:
    # U3/#10, relaunch side: registry+lock+heartbeat with NO in-process session is a dead
    # session's residue (single-replica: `_active_by_user` is authoritative) — the relaunch
    # reaps through it and serves the preview instead of refusing with a 409.
    user, project = await _user_project(db_session, "rl-contend@rvaiglobal.com")
    await _seed_snapshot(db_session, user, project, fake_storage)
    await seed_live_sandbox_state(fake_redis, user.id)

    resp = await client.post(
        "/v1/build-sessions/relaunch",
        json={"projectId": str(project.id)},
        headers=auth_headers(user),
    )
    assert resp.status_code == 200
    assert resp.json()["status"] == "ready"
    assert wire.sbx.restored != []  # the snapshot restore actually ran on a fresh sandbox


async def test_relaunch_documents_the_503_in_its_openapi_responses(client: AsyncClient) -> None:
    schema = (await client.get("/openapi.json")).json()
    responses = schema["paths"]["/v1/build-sessions/relaunch"]["post"]["responses"]
    assert "503" in responses
    assert "coordination" in responses["503"]["description"]


async def test_relaunch_is_503_when_the_sandbox_is_not_configured(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Deliberately FIXTURE-FREE on the sandbox (`.claude/rules/testing.md`): `wire` both sets
    `SANDBOX__*` and binds `sandbox_dependency`, so with it in place `SandboxNotConfiguredError`
    is unreachable BY CONSTRUCTION and this branch could never be tested. The sandbox is
    genuinely optional outside production, so a sandbox-off deployment is supported and owes the
    caller the 503 this route already documents.

    Before the fix an eager `SandboxDep` raised at dependency-solve time — before this body, and
    before the `except (..., SandboxError)` that would otherwise have caught it, since
    `SandboxNotConfiguredError` IS a `SandboxError`. The caller got an undocumented 500 carrying
    the catch-all `{"detail": ...}` envelope instead."""
    user, project = await _user_project(db_session, "relaunch-sbx-off@rvaiglobal.com")

    resp = await client.post(
        "/v1/build-sessions/relaunch",
        json={"projectId": str(project.id)},
        headers=auth_headers(user),
    )

    assert resp.status_code == 503
    body = resp.json()
    assert (
        body["error"]["message"]
        == "Sandbox unavailable. Please try again later or contact the admin"
    )
    # Pin the ENVELOPE, not just the status: `detail` would mean the catch-all handled it.
    assert "detail" not in body


# --- U1: the attach arm, pinned ON THE ACA CONTROL PLANE -------------------------------
#
# EVERY assertion in this section is a delete/create CALL COUNT, never "relaunch returned
# 200". The whole unit is "a call that used to happen no longer happens", and a 200 proves
# nothing about it: the shared `wire` fixture's `FakeSandboxClient` cannot see the delete at
# all, because `restore_from_snapshot` issues its own `_safe_teardown` from INSIDE the client
# (`services/sandbox/client.py`). So this lane drives the REAL `AcaSandboxClient` with a
# recording control plane underneath it and an `httpx.MockTransport` over the `/_sup/*`
# surface — the only composition where "no container was destroyed" is an observable fact.
# (`docs/solutions/best-practices/mocks-mask-composition-seams-integration-test-2026-07-15.md`.)


class RecordingAca(AcaControlPlane):
    """A control plane that records lifecycle calls instead of talking to Azure. Overrides
    `__init__` so it never builds a credential or a mgmt client.

    The FQDN carries the create ORDINAL (`-r1`, `-r2`, …) deliberately. `app_name_for` is
    stable per app, so a rebuilt container reuses the very same name — the name alone can
    therefore never tell a reuse from a replacement, and "the user is looking at the same
    container" would be a tautology without this."""

    def __init__(self) -> None:
        self.created: dict[str, dict[str, str]] = {}
        self.create_calls: list[str] = []
        self.delete_calls: list[str] = []
        self.fqdns: dict[str, str] = {}

    async def create_app(self, *, name: str, env: dict[str, str], tags: dict[str, str]) -> str:
        self.create_calls.append(name)
        fqdn = f"{name}-r{len(self.create_calls)}.westeurope.azurecontainerapps.io"
        self.created[name] = env
        self.fqdns[name] = fqdn
        return fqdn

    async def delete_app(self, *, name: str) -> None:
        self.delete_calls.append(name)
        self.created.pop(name, None)

    async def get_app_fqdn(self, *, name: str) -> str | None:
        return self.fqdns.get(name) if name in self.created else None

    async def get_app_env_value(self, *, name: str, key: str) -> str | None:
        # Serves the env recorded at CREATE, which is what real ACA does: environment variables
        # are set on the revision and readable back off the container-app spec. This is what
        # makes the supervisor bearer recoverable after a control-plane restart, so a fake that
        # answered None here would quietly re-create the data-loss path it exists to test.
        return self.created.get(name, {}).get(key)

    async def aclose(self) -> None:
        return None


class SupervisorScript:
    """The `/_sup/*` surface a relaunch drives, scripted per endpoint.

    `dev_start_status` + `dev_running` together reproduce the supervisor's TWO 409 arms
    (`sandbox/supervisor/app.py`): the owned-child 409 answers `running=True` (the client maps
    it to the already-running sentinel), while the UNOWNED-server 409 leaves `running=False`
    and the client raises `SandboxError` from it."""

    def __init__(self) -> None:
        self.dev_start_status = 200
        self.dev_running = True
        self.dev_ready = True
        self.paths: list[str] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/_sup")
        self.paths.append(path)
        if path == "/health":
            return httpx.Response(200, json={"ok": True})
        if path == "/files":
            return httpx.Response(200, json={"ok": True})
        if path == "/exec":
            return httpx.Response(200, json={"stdout": "", "stderr": "", "exit": 0})
        if path == "/dev/start":
            if self.dev_start_status != 200:
                return httpx.Response(self.dev_start_status, json={"detail": "already serving"})
            return httpx.Response(200, json={"pid": 4321})
        if path == "/dev/status":
            return httpx.Response(
                200,
                json={"running": self.dev_running, "ready": self.dev_ready, "port": 3000},
            )
        return httpx.Response(404, json={"detail": path})


@pytest.fixture
async def aca_wire(wire, fake_redis) -> AsyncIterator[SimpleNamespace]:
    """`wire`, with the canned `FakeSandboxClient` swapped for the real `AcaSandboxClient`
    over a recording control plane. One client instance for the whole test, which is what
    makes the in-process `token_ref` map survive between two requests — the exact
    single-process-lifetime bound R1 is scoped to."""
    aca = RecordingAca()
    sup = SupervisorScript()
    sandbox = AcaSandboxClient(
        SandboxConfig(
            subscription_id="s",
            resource_group="r",
            region="westeurope",
            managed_environment_name="aca-env",
            acr_server="acr.azurecr.io",
            acr_username="acr-user",
            acr_password=SecretStr("acr-pass"),
            image_ref="acr/img:latest",
        ),
        transport=httpx.MockTransport(sup),
        aca=aca,
    )
    wire.app.dependency_overrides[sandbox_dependency] = lambda: sandbox
    wire.app.dependency_overrides[sandbox_or_none_dependency] = lambda: sandbox
    yield SimpleNamespace(app=wire.app, manager=wire.manager, aca=aca, sup=sup, sandbox=sandbox)
    await sandbox.aclose()


async def _relaunch(client: AsyncClient, user, project) -> httpx.Response:
    return await client.post(
        "/v1/build-sessions/relaunch",
        json={"projectId": str(project.id)},
        headers=auth_headers(user),
    )


async def test_a_relaunch_onto_a_live_healthy_container_touches_no_aca_lifecycle(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, aca_wire
) -> None:
    """R1, and THE assertion of this unit. The first relaunch is genuinely cold and pays a
    create; the immediate repeat — the exact shape measured at 57.8s — must reuse what is
    already up: ZERO deletes and ZERO creates, because the ~20s ACA delete plus the ~33.5s
    ACA create are the entire cost being removed."""
    user, project = await _user_project(db_session, "rl-attach@rvaiglobal.com")
    app_id = await _seed_snapshot(db_session, user, project, fake_storage)

    cold = await _relaunch(client, user, project)
    assert cold.status_code == 200
    assert aca_wire.aca.create_calls == [app_name_for(app_id)]  # the cold path DID build one
    assert aca_wire.aca.delete_calls == []

    warm = await _relaunch(client, user, project)

    assert warm.status_code == 200
    # The whole unit, stated as call counts: the second relaunch added NEITHER lifecycle call.
    assert aca_wire.aca.delete_calls == []
    assert aca_wire.aca.create_calls == [app_name_for(app_id)]


async def test_the_warm_relaunch_attaches_to_the_pre_existing_container(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, aca_wire
) -> None:
    """The user is looking at the SAME container, not a same-named replacement.

    THE INSTRUMENT CHANGED AND THE CLAIM DID NOT. This used to read the create-ordinal out of
    the returned FQDN (`-r1` vs `-r2`), because `previewUrl` carried the container's own name.
    It no longer does — every app is served from one public hostname under a key derived from
    the app id, so the address is IDENTICAL for a reuse and for a rebuild and can no longer
    falsify anything here. Asserting it alone would be a tautology, which is worse than not
    asserting it: the test would still read as if it proved something.

    So the proof moves to the thing it was always a proxy for — whether ACA was asked to create
    a container a second time. That is a direct observation rather than an inference from a
    name, and it stays falsifiable: make relaunch rebuild instead of attach and `create_calls`
    grows.
    """
    user, project = await _user_project(db_session, "rl-attach-fqdn@rvaiglobal.com")
    app_id = await _seed_snapshot(db_session, user, project, fake_storage)

    cold = await _relaunch(client, user, project)
    creates_after_cold = list(aca_wire.aca.create_calls)
    warm = await _relaunch(client, user, project)

    assert aca_wire.aca.create_calls == creates_after_cold, (
        "the warm relaunch attached; a rebuild would have appended another create"
    )
    expected = f"https://citizenapps.bialairport.com/a/{app_name_for(app_id)}"
    assert cold.json()["previewUrl"] == expected
    assert warm.json()["previewUrl"] == expected


async def test_a_registry_naming_a_different_app_refuses_the_relaunch(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, aca_wire
) -> None:
    """The one-per-user registry can only name one container. Relaunching a DIFFERENT project
    must never ATTACH to it — that would serve project A's tree under project B's id.

    RE-CUT FOR #83 (was `…_refuses_the_attach_and_restores`). Not attaching was always right;
    reaping instead was the mistake. A's container is holding work nobody saved, and this route
    used to destroy it inside B's request without a word. The 409 names A so the client can
    offer to save it, and `release` is the way through."""
    user, project_a = await _user_project(db_session, "rl-otherapp@rvaiglobal.com")
    project_b = await ProjectFactory.create(db_session, user.id)
    app_a = await _seed_snapshot(db_session, user, project_a, fake_storage)
    await _seed_snapshot(db_session, user, project_b, fake_storage)
    await _seed_worked_on(fake_storage, app_a)  # A holds work; an empty template would not block

    assert (await _relaunch(client, user, project_a)).status_code == 200
    resp = await _relaunch(client, user, project_b)

    assert resp.status_code == 409
    body = resp.json()["error"]
    assert body["code"] == "sandbox_reclaim_blocked"
    assert body["projectId"] == str(project_a.id)  # names the project holding the slot
    assert aca_wire.aca.delete_calls == []  # A's container survives the refusal
    assert aca_wire.aca.create_calls == [app_name_for(app_a)]  # B was never built


async def test_a_registry_marked_ending_is_never_attached_to(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, aca_wire
) -> None:
    """`ending` is a container the reaper has already committed to destroying. Attaching would
    race that teardown and leave us paying the restore anyway, having skipped the cleanup."""
    user, project = await _user_project(db_session, "rl-ending@rvaiglobal.com")
    app_id = await _seed_snapshot(db_session, user, project, fake_storage)

    assert (await _relaunch(client, user, project)).status_code == 200
    await fake_redis.hset(registry_key(user.id), REGISTRY_FIELD_STATE, REGISTRY_STATE_ENDING)

    resp = await _relaunch(client, user, project)

    assert resp.status_code == 200
    name = app_name_for(app_id)
    assert aca_wire.aca.delete_calls == [name]  # the dying container was reaped, not adopted
    assert aca_wire.aca.create_calls == [name, name]  # ...and a fresh one restored


async def test_no_registry_at_all_still_takes_the_restore_arm(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, aca_wire
) -> None:
    """The canonical "user comes back tomorrow" case: a clean finalize deletes the registry, so
    there is nothing to attach to and the behaviour is exactly what it was before U1."""
    user, project = await _user_project(db_session, "rl-noreg@rvaiglobal.com")
    app_id = await _seed_snapshot(db_session, user, project, fake_storage)

    assert (await _relaunch(client, user, project)).status_code == 200
    await fake_redis.delete(registry_key(user.id))

    resp = await _relaunch(client, user, project)

    assert resp.status_code == 200
    assert aca_wire.aca.create_calls == [app_name_for(app_id), app_name_for(app_id)]


async def test_a_control_plane_restart_reattaches_instead_of_rebuilding_the_container(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, aca_wire
) -> None:
    """★ THE "ACCEPTED BOUND" THAT WAS ACTUALLY DATA LOSS, and this test used to pin it.

    Its previous form asserted that the first relaunch after a deploy tears the container down
    and restores it — "a slow 200, never an error" — because the supervisor bearer lived only
    in process memory, so an emptied `_token_refs` made `attach_existing` raise
    `SandboxGoneError`. The cost was read as latency. It is not: restore pulls the last SAVED
    bundle, so every citizen with unsaved work in an open sandbox was silently rolled back to
    their last save. That is SL-20's data loss on a deploy schedule.

    The premise was wrong. An unresolvable ref says nothing about the container — the token was
    injected into the container's own ACA env at create, so the container app spec is its
    durable home and the in-process map was only ever a cache. Recovering it is both cheaper
    and non-destructive.

    The orphan hazard the old test guarded still matters, and it is asserted here as an
    ABSENCE: nothing is torn down, because nothing is replaced."""
    user, project = await _user_project(db_session, "rl-restart@rvaiglobal.com")
    app_id = await _seed_snapshot(db_session, user, project, fake_storage)

    first = await _relaunch(client, user, project)
    assert first.status_code == 200
    aca_wire.sandbox._token_refs.clear()  # what a control-plane restart leaves behind

    resp = await _relaunch(client, user, project)

    assert resp.status_code == 200
    name = app_name_for(app_id)
    assert aca_wire.aca.delete_calls == [], "a restart must not destroy a live container"
    assert aca_wire.aca.create_calls == [name], "…nor build a replacement over the citizen's tree"
    # The ordinal-carrying FQDN is what makes "same container" a claim rather than a tautology:
    # `app_name_for` is stable, so only `-r1` vs `-r2` can tell reuse from replacement.
    assert resp.json()["previewUrl"] == first.json()["previewUrl"]


async def test_a_relaunch_with_no_snapshot_creates_no_container_at_all(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, aca_wire
) -> None:
    """The 404 contract, re-pinned on the control plane: the snapshot gate stays ABOVE both
    arms, so a never-built project allocates nothing — no attach probe, no container."""
    user, project = await _user_project(db_session, "rl-nosnap-aca@rvaiglobal.com")

    resp = await _relaunch(client, user, project)

    assert resp.status_code == 404
    assert aca_wire.aca.create_calls == []
    assert aca_wire.aca.delete_calls == []


async def test_an_unowned_server_409_after_attach_still_returns_200_and_deletes_nothing(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, aca_wire
) -> None:
    """R6 at its most likely trigger. `/dev/start` has TWO 409 arms; the UNOWNED-server one
    (`_dev_port_serving()` true while `_Dev.proc` is dead — what the agent leaves behind when
    it starts its own dev server through the open-sandbox `run_command` surface) answers
    `running=False`, so the client raises `SandboxError`. Unguarded that reaches compensation,
    which would destroy the healthy container this unit exists to preserve."""
    user, project = await _user_project(db_session, "rl-409@rvaiglobal.com")
    app_id = await _seed_snapshot(db_session, user, project, fake_storage)

    assert (await _relaunch(client, user, project)).status_code == 200
    aca_wire.sup.dev_start_status = 409  # something is already serving on the dev port...
    aca_wire.sup.dev_running = False  # ...and it is not the supervisor's child

    resp = await _relaunch(client, user, project)

    assert resp.status_code == 200
    assert aca_wire.aca.delete_calls == []  # the already-serving container survived
    assert aca_wire.aca.create_calls == [app_name_for(app_id)]


# --- #83: the release route, and the refusal it exists to resolve --------------------


async def _release(client: AsyncClient, user, project) -> httpx.Response:
    return await client.post(
        f"/v1/build-sessions/projects/{project.id}/release", headers=auth_headers(user)
    )


async def test_release_gives_up_the_container_and_unblocks_the_switch(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, aca_wire
) -> None:
    """The way through the #83 refusal, end to end over HTTP. The teardown is the same one the
    start path used to perform silently; what changed is who asked for it."""
    user, project_a = await _user_project(db_session, "rl-release@rvaiglobal.com")
    project_b = await ProjectFactory.create(db_session, user.id)
    app_a = await _seed_snapshot(db_session, user, project_a, fake_storage)
    app_b = await _seed_snapshot(db_session, user, project_b, fake_storage)
    await _seed_worked_on(fake_storage, app_a)  # A holds work; an empty template would not block

    assert (await _relaunch(client, user, project_a)).status_code == 200
    assert (await _relaunch(client, user, project_b)).status_code == 409  # A is in the way

    released = await _release(client, user, project_a)

    assert released.status_code == 200
    assert released.json()["released"] is True
    assert aca_wire.aca.delete_calls == [app_name_for(app_a)]  # gone, on the user's say-so
    assert (await _relaunch(client, user, project_b)).status_code == 200  # B can have it now
    assert app_name_for(app_b) in aca_wire.aca.create_calls


async def test_releasing_a_workspace_that_is_already_gone_is_a_success(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, aca_wire
) -> None:
    """`released: false`, not 404. The caller asked for the workspace to be gone and it is —
    reporting failure would send a client into a retry loop over an outcome it already has."""
    user, project = await _user_project(db_session, "rl-release-noop@rvaiglobal.com")
    await _seed_snapshot(db_session, user, project, fake_storage)

    resp = await _release(client, user, project)

    assert resp.status_code == 200
    assert resp.json()["released"] is False
    assert aca_wire.aca.delete_calls == []


async def test_a_teardown_that_fails_is_a_503_not_a_reported_success(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_redis,
    fake_storage,
    aca_wire,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """#83 REVIEW, BLOCKER 2. `reap_user` swallows `SandboxError` and returns False by design,
    so a container that refuses to die used to be INDISTINGUISHABLE from "there was nothing to
    release" — both `200 {"released": false}`. The route's `except SandboxError` arm could
    never fire.

    That is load-bearing rather than cosmetic: the client discards the boolean and immediately
    retries the thing that wanted the slot, so a false success sends the user straight back
    into the refusal they were just told had been cleared. `release_project_sandbox` now reaps
    with `strict=True`, which re-raises for this caller only — the sweep keeps the lenient
    default, because a background retry loop is exactly what it is for.

    Mutation-check: drop `strict=True` in `release_project_sandbox` and this goes red with a
    200/`released: false`."""
    user, project_a = await _user_project(db_session, "rl-release-fail@rvaiglobal.com")
    app_a = await _seed_snapshot(db_session, user, project_a, fake_storage)
    await _seed_worked_on(fake_storage, app_a)
    assert (await _relaunch(client, user, project_a)).status_code == 200

    # ARM stops accepting deletes — the throttle / transient-failure shape.
    # `AcaSandboxClient.teardown` maps this to `SandboxError` and KEEPS the registry.
    async def throttled(*, name: str) -> None:
        aca_wire.aca.delete_calls.append(name)
        raise AcaTransientError("arm is throttling")

    monkeypatch.setattr(aca_wire.aca, "delete_app", throttled)

    resp = await _release(client, user, project_a)

    assert resp.status_code == 503, "a teardown that failed must not report a release"
    assert resp.status_code != 200
    assert "try again" in resp.json()["error"]["message"].lower()
    # The state is KEPT, so a later sweep retries rather than orphaning a live container.
    assert await fake_redis.exists(registry_key(user.id)) == 1


async def test_release_is_owner_scoped_and_csrf_guarded(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, wire
) -> None:
    """ADR-0004 + KTD-4 on a route that DESTROYS a container: another user's project is a
    non-leaking 404, and a cookie without the CSRF header is refused outright."""
    owner, project = await _user_project(db_session, "rl-release-owner@rvaiglobal.com")
    await _seed_snapshot(db_session, owner, project, fake_storage)
    stranger = await UserFactory.create(db_session, email="rl-release-other@rvaiglobal.com")

    assert (await _release(client, stranger, project)).status_code == 404
    no_csrf = await client.post(
        f"/v1/build-sessions/projects/{project.id}/release",
        headers=auth_headers(owner, with_csrf=False),
    )
    assert no_csrf.status_code == 403


async def test_preview_state_says_gone_when_another_project_took_the_workspace(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, aca_wire
) -> None:
    """#83, second half — the probe a framed tab uses to notice it is showing a dead app.

    The registry is one-per-user, so "somebody else's container is up" IS the shape of "yours
    is gone". Answering from this project's point of view is what lets the pane stop claiming
    a preview it no longer has."""
    user, project_a = await _user_project(db_session, "rl-preview@rvaiglobal.com")
    project_b = await ProjectFactory.create(db_session, user.id)
    await _seed_snapshot(db_session, user, project_a, fake_storage)
    await _seed_snapshot(db_session, user, project_b, fake_storage)

    assert (await _relaunch(client, user, project_a)).status_code == 200
    alive = await client.get(
        f"/v1/build-sessions/projects/{project_a.id}/preview-state", headers=auth_headers(user)
    )
    assert alive.status_code == 200
    assert alive.json()["alive"] is True
    assert alive.json()["previewUrl"].startswith("https://")

    # B is not the one serving, so from B's side there is no preview — and once A releases,
    # A's own answer flips too.
    from_b = await client.get(
        f"/v1/build-sessions/projects/{project_b.id}/preview-state", headers=auth_headers(user)
    )
    assert from_b.json()["alive"] is False

    assert (await _release(client, user, project_a)).status_code == 200
    after = await client.get(
        f"/v1/build-sessions/projects/{project_a.id}/preview-state", headers=auth_headers(user)
    )
    # ASLEEP, not "gone" (C3 §8.3): the project was built, its work is on Blob, and the next
    # prompt brings it back. The exact-dict assertion this replaces could not survive the
    # response growing a state — see `test_preview_state.py` for the four states themselves.
    body = after.json()
    assert (body["state"], body["alive"], body["previewUrl"]) == ("asleep", False, None)


async def test_preview_state_of_a_never_built_project_is_not_an_error(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, wire
) -> None:
    """Nothing was ever built, so nothing can be serving it. `alive: false`, not a 404 — the
    pane asks this on a timer and an error would be noise for a perfectly ordinary state."""
    user, project = await _user_project(db_session, "rl-preview-new@rvaiglobal.com")
    resp = await client.get(
        f"/v1/build-sessions/projects/{project.id}/preview-state", headers=auth_headers(user)
    )
    assert resp.status_code == 200
    assert resp.json()["alive"] is False


async def test_preview_state_is_owner_scoped(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, wire
) -> None:
    owner, project = await _user_project(db_session, "rl-preview-own@rvaiglobal.com")
    await _seed_snapshot(db_session, owner, project, fake_storage)
    stranger = await UserFactory.create(db_session, email="rl-preview-other@rvaiglobal.com")
    resp = await client.get(
        f"/v1/build-sessions/projects/{project.id}/preview-state", headers=auth_headers(stranger)
    )
    assert resp.status_code == 404


# --- U2: what the start path records (R102, R103, R106) --------------------------------------
#
# R103 is "the difference between pressing the control and seeing the app", so the denominator
# has to hold every press including the refused ones — which is what most of the scenarios below
# are actually about. R102 is the cold arm's own clock, and the boundary test at the end is what
# stops it quietly becoming "how long the whole request took".
#
# These rows escape the test transaction on purpose: `count(...)` owns its own session and
# COMMITS, so a count survives a rolled-back transaction (the property
# `tests/services/build_sessions/test_counters.py` pins). The consequence is that a test reading
# them starts from a known-empty table rather than from a rollback that cannot reach them.


async def _counter_values(counter: HarnessCounter) -> list[int]:
    """Every value recorded under one counter name. Read as columns, not ORM rows: the session
    that read them is closed by the time the assertion runs."""
    async with async_session_factory() as db:
        rows = (
            await db.execute(
                sa.select(HarnessCount.value).where(HarnessCount.name == counter.value)
            )
        ).all()
    return [int(v) for (v,) in rows]


async def test_a_cold_relaunch_records_the_press_the_arrival_and_the_wait(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_redis,
    fake_storage,
    wire,
    empty_harness_counts,
) -> None:
    """The whole of R102/R103 on the happy path: one press, one arrival, one duration."""
    user, project = await _user_project(db_session, "rl-count-cold@rvaiglobal.com")
    await _seed_snapshot(db_session, user, project, fake_storage)

    assert (await _relaunch(client, user, project)).status_code == 200

    assert await _counter_values(HarnessCounter.APP_START_ATTEMPTED) == [1]
    assert await _counter_values(HarnessCounter.APP_START_REACHED_RUNNING) == [1]
    cold = await _counter_values(HarnessCounter.APP_COLD_START_MS)
    assert len(cold) == 1
    # Bounded by the cold budget it is measuring — a duration outside it is a clock reading
    # something else entirely, which is the failure this number cannot survive.
    assert 0 <= cold[0] < 120_000


async def test_the_attach_arm_records_the_press_and_the_arrival_but_no_duration(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_redis,
    fake_storage,
    aca_wire,
    empty_harness_counts,
) -> None:
    """★ A 15-second attach budget and a 120-second cold budget averaged together produce a
    number that describes neither, so only the restore arm writes a duration.

    Two presses: the first is genuinely cold, the second attaches to what it left up (the same
    shape `…touches_no_aca_lifecycle` pins). Both are starts from the citizen's side, so both
    land in the pair — and there is still exactly ONE duration."""
    user, project = await _user_project(db_session, "rl-count-attach@rvaiglobal.com")
    await _seed_snapshot(db_session, user, project, fake_storage)

    assert (await _relaunch(client, user, project)).status_code == 200
    assert (await _relaunch(client, user, project)).status_code == 200

    assert len(await _counter_values(HarnessCounter.APP_START_ATTEMPTED)) == 2
    assert len(await _counter_values(HarnessCounter.APP_START_REACHED_RUNNING)) == 2
    assert len(await _counter_values(HarnessCounter.APP_COLD_START_MS)) == 1


async def test_an_attach_that_fails_open_unready_is_a_press_that_never_arrived(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_redis,
    fake_storage,
    wire,
    empty_harness_counts,
) -> None:
    """★ THE MUTANT THIS EXISTS FOR. The attach arm deliberately fails open and hands back a
    framable URL with `ready=False` (the SL-20 fix). That is not a running app, and an emit that
    fired unconditionally beside the response would make R103 measure nothing at all.

    Mutation check: move the reached-running emit out from under `if ready:` and this goes red.
    """
    user, project = await _user_project(db_session, "rl-count-unready@rvaiglobal.com")
    app_id = await _seed_snapshot(db_session, user, project, fake_storage)

    # One cold relaunch to leave a container up and a registry naming THIS app…
    assert (await _relaunch(client, user, project)).status_code == 200
    # …then attach to it, with a dev server that never comes back ready.
    wire.sbx.attach_handle = SandboxHandle(
        fqdn="live.example",
        token="tok",
        app_name=app_name_for(app_id),
        preview_url="https://live.example",
        ready=True,
    )

    async def the_dev_server_never_answers(handle, *, timeout_s: float = 120.0):
        raise SandboxNotReadyError("the app root never served")

    wire.sbx.wait_ready = the_dev_server_never_answers

    assert (await _relaunch(client, user, project)).status_code == 200  # fails OPEN, not 503

    assert len(await _counter_values(HarnessCounter.APP_START_ATTEMPTED)) == 2
    assert len(await _counter_values(HarnessCounter.APP_START_REACHED_RUNNING)) == 1
    assert len(await _counter_values(HarnessCounter.APP_COLD_START_MS)) == 1


async def test_a_press_refused_by_the_one_slot_conflict_still_counts_as_a_press(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_redis,
    fake_storage,
    wire,
    empty_harness_counts,
) -> None:
    """★ ONE OF THE TWO REFUSALS THAT SIT ABOVE THE 404 GATE, and the reason the emit is at
    function entry rather than after it. A live build owns the one-per-user slot; the citizen
    pressed the control and did not see their app, which is exactly what R103 measures.

    Mutation check: move the attempted emit below the snapshot gate and this goes red."""
    brain = BlockingBrain()
    wire.app.dependency_overrides[run_build_dependency] = lambda: brain
    user, project = await _user_project(db_session, "rl-count-409@rvaiglobal.com")
    await _seed_snapshot(db_session, user, project, fake_storage)

    started = await client.post(
        "/v1/build-sessions",
        json={"projectId": str(project.id), "prompt": "build it"},
        headers=auth_headers(user),
    )
    assert started.status_code == 201

    assert (await _relaunch(client, user, project)).status_code == 409

    assert await _counter_values(HarnessCounter.APP_START_ATTEMPTED) == [1]
    assert await _counter_values(HarnessCounter.APP_START_REACHED_RUNNING) == []
    assert await _counter_values(HarnessCounter.APP_COLD_START_MS) == []

    brain.release()
    await drain(wire.manager, started.json()["sessionId"])


async def test_a_press_refused_because_reclaiming_would_destroy_work_still_counts(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_redis,
    fake_storage,
    aca_wire,
    empty_harness_counts,
) -> None:
    """★ THE OTHER REFUSAL ABOVE THE 404 GATE (#83). Project A holds the one container and it is
    holding unsaved work, so B's press is refused rather than reclaiming it — a press that could
    have started something and did not."""
    user, project_a = await _user_project(db_session, "rl-count-reclaim@rvaiglobal.com")
    project_b = await ProjectFactory.create(db_session, user.id)
    app_a = await _seed_snapshot(db_session, user, project_a, fake_storage)
    await _seed_snapshot(db_session, user, project_b, fake_storage)
    await _seed_worked_on(fake_storage, app_a)

    assert (await _relaunch(client, user, project_a)).status_code == 200
    assert (await _relaunch(client, user, project_b)).status_code == 409

    assert len(await _counter_values(HarnessCounter.APP_START_ATTEMPTED)) == 2
    assert len(await _counter_values(HarnessCounter.APP_START_REACHED_RUNNING)) == 1
    assert len(await _counter_values(HarnessCounter.APP_COLD_START_MS)) == 1


async def test_a_press_with_nothing_to_restore_still_counts_as_a_press(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_redis,
    fake_storage,
    wire,
    empty_harness_counts,
) -> None:
    """The 404 gate. A never-built project has nothing to relaunch, and the citizen still
    pressed."""
    user, project = await _user_project(db_session, "rl-count-404@rvaiglobal.com")

    assert (await _relaunch(client, user, project)).status_code == 404

    assert await _counter_values(HarnessCounter.APP_START_ATTEMPTED) == [1]
    assert await _counter_values(HarnessCounter.APP_START_REACHED_RUNNING) == []
    assert await _counter_values(HarnessCounter.APP_COLD_START_MS) == []


async def test_a_relaunch_that_dies_after_the_arm_is_chosen_records_no_arrival(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_redis,
    fake_storage,
    wire,
    empty_harness_counts,
) -> None:
    """A failure past the point where the cold clock started: the press is in the denominator,
    nothing is in the numerator, and — because the arm never reached `wait_ready` — no duration
    is written for a wait that never finished."""
    user, project = await _user_project(db_session, "rl-count-dies@rvaiglobal.com")
    await _seed_snapshot(db_session, user, project, fake_storage)

    async def the_dev_server_will_not_start(handle, *, cmd=None, cwd=None) -> int:
        raise SandboxError("supervisor refused /dev/start")

    wire.sbx.dev_start = the_dev_server_will_not_start

    assert (await _relaunch(client, user, project)).status_code != 200

    assert await _counter_values(HarnessCounter.APP_START_ATTEMPTED) == [1]
    assert await _counter_values(HarnessCounter.APP_START_REACHED_RUNNING) == []
    assert await _counter_values(HarnessCounter.APP_COLD_START_MS) == []


async def test_the_cold_clock_times_the_restore_not_the_whole_request(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_redis,
    fake_storage,
    wire,
    empty_harness_counts,
) -> None:
    """★ THE CLOCK'S BOUNDARY, and the reason both instants are named in the method's docstring.

    Everything before the restore arm is entered — the slot check, the reclaim refusal, the lock
    wait, app resolution, the snapshot gate, the commit, and THE ATTACH ATTEMPT ITSELF — is
    outside the number, because none of it is a citizen waiting for a container to come up. Here
    the attach attempt is made to take a full second before it gives up; the recorded duration
    must not contain it.

    Mutation check: move the clock's start to function entry and this goes red."""
    user, project = await _user_project(db_session, "rl-count-boundary@rvaiglobal.com")
    await _seed_snapshot(db_session, user, project, fake_storage)

    # A first cold relaunch, so the registry names this app and the attach attempt below is
    # actually MADE rather than skipped by the registry check.
    assert (await _relaunch(client, user, project)).status_code == 200
    await forget_every_harness_count()

    slow_attach_seconds = 1.0

    async def a_slow_goodbye(user_id: str):
        await asyncio.sleep(slow_attach_seconds)
        raise SandboxGoneError("took a while to be sure it is gone")

    wire.sbx.attach_existing = a_slow_goodbye

    assert (await _relaunch(client, user, project)).status_code == 200

    cold = await _counter_values(HarnessCounter.APP_COLD_START_MS)
    assert len(cold) == 1
    assert cold[0] < slow_attach_seconds * 1000 / 2, (
        f"{cold[0]}ms contains the {slow_attach_seconds}s attach attempt — the clock is timing "
        "the request, not the restore"
    )
