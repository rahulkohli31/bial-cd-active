"""#43 — POST /v1/build-sessions/relaunch: restore a torn-down app from its snapshot into a
fresh, READY sandbox (cookie auth + CSRF, owner-scoping, Decision-6 no-build-slot)."""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator
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
from src.db.models.app_registry import AppRegistry
from src.db.models.conversation import ConversationKind
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
from src.services.sandbox.aca import AcaControlPlane
from src.services.sandbox.client import AcaSandboxClient
from src.services.sandbox.config import SandboxConfig
from src.services.storage import snapshot_key
from tests.api.v1.build_sessions.conftest import (
    BlockingBrain,
    auth_headers,
    drain,
    seed_live_sandbox_state,
)
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

    async def create_app(self, *, name: str, env: dict[str, str]) -> str:
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


async def test_the_warm_relaunch_hands_back_the_pre_existing_containers_own_fqdn(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, aca_wire
) -> None:
    """The user is looking at the SAME container, not a same-named replacement. `app_name_for`
    is stable per app, so the name proves nothing — the create-ordinal in `RecordingAca`'s
    FQDN is what makes this falsifiable (a rebuild would answer `-r2`)."""
    user, project = await _user_project(db_session, "rl-attach-fqdn@rvaiglobal.com")
    app_id = await _seed_snapshot(db_session, user, project, fake_storage)

    cold = await _relaunch(client, user, project)
    warm = await _relaunch(client, user, project)

    born = f"https://{app_name_for(app_id)}-r1.westeurope.azurecontainerapps.io/"
    assert cold.json()["previewUrl"] == born
    assert warm.json()["previewUrl"] == born  # the birth container, not a rebuilt one


async def test_a_registry_naming_a_different_app_refuses_the_attach_and_restores(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, aca_wire
) -> None:
    """The one-per-user registry can only name one container. Relaunching a DIFFERENT project
    must reap it and restore — attaching would serve project A's tree under project B's id."""
    user, project_a = await _user_project(db_session, "rl-otherapp@rvaiglobal.com")
    project_b = await ProjectFactory.create(db_session, user.id)
    app_a = await _seed_snapshot(db_session, user, project_a, fake_storage)
    app_b = await _seed_snapshot(db_session, user, project_b, fake_storage)

    assert (await _relaunch(client, user, project_a)).status_code == 200
    resp = await _relaunch(client, user, project_b)

    assert resp.status_code == 200
    assert aca_wire.aca.delete_calls == [app_name_for(app_a)]  # A reaped exactly once...
    assert aca_wire.aca.create_calls == [app_name_for(app_a), app_name_for(app_b)]  # ...B built


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


async def test_a_control_plane_restart_falls_through_to_restore_without_orphaning(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, aca_wire
) -> None:
    """THE ACCEPTED BOUND ON R1, pinned so nobody "fixes" it into a 500. The supervisor bearer
    lives in-process by design (`tests/services/sandbox/test_aca.py` pins that it never reaches
    Redis), so the first relaunch after a deploy cannot resolve the token_ref, `attach_existing`
    raises `SandboxGoneError`, and the restore arm runs — a slow 200, never an error.

    The second assertion is the one that matters more: U1 skips the reconcile on this path
    (the registry still names our app in READY), so the ONLY thing left to reap the old
    container is `restore_from_snapshot`'s own defensive teardown. If that ever stopped
    firing, a live container would be orphaned under an overwritten registry entry, invisible
    to the sweep forever."""
    user, project = await _user_project(db_session, "rl-restart@rvaiglobal.com")
    app_id = await _seed_snapshot(db_session, user, project, fake_storage)

    assert (await _relaunch(client, user, project)).status_code == 200
    aca_wire.sandbox._token_refs.clear()  # what a control-plane restart leaves behind

    resp = await _relaunch(client, user, project)

    assert resp.status_code == 200
    name = app_name_for(app_id)
    assert aca_wire.aca.delete_calls == [name]  # the old container reaped, NOT orphaned
    assert aca_wire.aca.create_calls == [name, name]


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
