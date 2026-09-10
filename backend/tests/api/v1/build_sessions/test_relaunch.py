"""POST /v1/build-sessions/relaunch: restore a torn-down app from its snapshot into a
fresh, READY sandbox (cookie auth + CSRF, owner-scoping, no build slot taken)."""

from __future__ import annotations

import asyncio
import contextlib
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

import src.services.build_sessions.manager as manager_mod
from src.api.v1.build_sessions.deps import (
    sandbox_dependency,
    sandbox_or_none_dependency,
)
from src.api.v1.build_sessions.schemas import BuildSessionStatus
from src.db.base import async_session_factory
from src.db.models.app_registry import AppRegistry
from src.db.models.conversation import ChatKind
from src.db.models.harness_counter import HarnessCount, HarnessCounter
from src.services.build_sessions.appdata import resolve_app_for_project
from src.services.build_sessions.locks import lock_is_held
from src.services.build_sessions.manager import SessionManager, app_name_for
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
    a_live_session,
    auth_headers,
    seed_live_sandbox_state,
)
from tests.conftest import forget_every_harness_count
from tests.factories import ConversationFactory, ProjectFactory, UserFactory


async def _user_project(db: AsyncSession, email: str):
    user = await UserFactory.create(db, email=email)
    project = await ProjectFactory.create(db, user.id)
    return user, project


async def _seed_snapshot(db: AsyncSession, user, project, store) -> uuid.UUID:
    app_id = await resolve_app_for_project(db, user.id, project.id)
    await db.commit()
    await store.put(snapshot_key(app_id), b"BUNDLE")
    return app_id


async def _seed_worked_on(store, app_id: uuid.UUID) -> None:
    """Mark this app as holding real work: the reclaim guard reads a recovery bundle as proof
    that a turn touched files."""
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
    assert body["previewUrl"].startswith("https://")
    assert body["restoredFromFailedBuild"] is False
    # Relaunch does NOT occupy the build slot: the lock is free and no session is live.
    assert wire.manager._active_by_user == {}
    assert await lock_is_held(fake_redis, user.id) is False


async def test_relaunch_after_failed_build_signals_last_saved_version(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, wire
) -> None:
    user, project = await _user_project(db_session, "rl6@rvaiglobal.com")
    await _seed_snapshot(db_session, user, project, fake_storage)
    conv = await ConversationFactory.create(
        db_session, user.id, project_id=project.id, kind=ChatKind.BUILD
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
    user, project = await _user_project(db_session, "rl2@rvaiglobal.com")
    resp = await client.post(
        "/v1/build-sessions/relaunch",
        json={"projectId": str(project.id)},
        headers=auth_headers(user),
    )
    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "no_saved_build"
    assert wire.sbx.provisioned == []
    # The speculative app-row upsert was never committed and production's `get_db` rolls it
    # back on the error response, so the test mirrors that rollback before counting —
    # without it the count would read a row no request ever kept.
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
    """A live session owns the one-per-user slot; relaunch 409s and carries its session id.

    Re-fixtured onto `a_live_session`. The slot has to be genuinely OCCUPIED for this to prove
    anything, and relaunch provably cannot occupy it itself — asserted directly by
    `test_relaunch_happy_returns_200_ready_preview`: `_active_by_user == {}` — so the occupant
    comes from `ensure_sandbox`, the only allocator left that claims the slot. Same project as
    the relaunch, which is what earns the BARE conflict rather than the hand-over 409
    (`_slot_conflict_for`)."""
    user, project = await _user_project(db_session, "rl3@rvaiglobal.com")
    await _seed_snapshot(db_session, user, project, fake_storage)

    session = await a_live_session(wire, db_session, user, project.id)

    conflict = await client.post(
        "/v1/build-sessions/relaunch",
        json={"projectId": str(project.id)},
        headers=auth_headers(user),
    )
    assert conflict.status_code == 409
    err = conflict.json()["error"]
    assert err["code"] == "build_session_already_active"
    assert err["sessionId"] == str(session.session_id)


async def test_relaunch_another_users_project_is_404(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, wire
) -> None:
    owner, project = await _user_project(db_session, "rl4-owner@rvaiglobal.com")
    await _seed_snapshot(db_session, owner, project, fake_storage)
    intruder = await UserFactory.create(db_session, email="rl4-intruder@rvaiglobal.com")

    resp = await client.post(
        "/v1/build-sessions/relaunch",
        json={"projectId": str(project.id)},
        headers=auth_headers(intruder),
    )
    assert resp.status_code == 404
    # AND IT IS NOT `no_saved_build`: the client's "open the chat anyway" arm keys on that code,
    # so an owner-scoping 404 that carried one would open a chat on a stranger's project id.
    assert resp.json()["error"].get("code") is None


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


# --- the 503/409 matrix, repeated here on purpose ---------------------------------------
#
# Not covered by the start-path tests: relaunch is a separate route with its own `except`
# arms and its own 409, and the two can drift apart without either going red.


async def test_relaunch_is_503_not_500_when_redis_is_entirely_unreachable(
    client: AsyncClient, db_session: AsyncSession, dead_redis, fake_storage, wire
) -> None:
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
    # Only `set` is cursed, so the reconcile still succeeds and the request actually reaches
    # `acquire_lock` — curse more of the client and it never gets that far.
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
    # NO `fake_redis` fixture, deliberately: binding one makes this branch unreachable.
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
    assert wire.sbx.restored != []


async def test_relaunch_documents_the_503_in_its_openapi_responses(client: AsyncClient) -> None:
    schema = (await client.get("/openapi.json")).json()
    responses = schema["paths"]["/v1/build-sessions/relaunch"]["post"]["responses"]
    assert "503" in responses
    assert "coordination" in responses["503"]["description"]


async def test_relaunch_is_503_when_the_sandbox_is_not_configured(
    client: AsyncClient, db_session: AsyncSession
) -> None:
    """Deliberately FIXTURE-FREE on the sandbox: `wire` both sets `SANDBOX__*` and binds
    `sandbox_dependency`, so with it bound `SandboxNotConfiguredError` is unreachable by
    construction and this branch could never be tested."""
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


# --- the attach arm, pinned ON THE ACA CONTROL PLANE -----------------------------------
#
# Every assertion below is a delete/create CALL COUNT, and it has to be: the shared `wire`
# fixture's `FakeSandboxClient` cannot see the delete at all, because `restore_from_snapshot`
# issues its own `_safe_teardown` from INSIDE the client. Driving the real `AcaSandboxClient`
# over a recording control plane is the only composition where "no container was destroyed"
# is observable — a 200 from this route says nothing about it.


class RecordingAca(AcaControlPlane):
    """Records lifecycle calls instead of talking to Azure; `__init__` is overridden so it
    never builds a credential or a mgmt client.

    The FQDN carries the create ORDINAL (`-r1`, `-r2`, …) deliberately: `app_name_for` is
    stable per app, so a rebuilt container reuses the very same name and the name alone can
    never tell a reuse from a replacement."""

    def __init__(self) -> None:
        self.created: dict[str, dict[str, str]] = {}
        self.create_calls: list[str] = []
        self.delete_calls: list[str] = []
        self.fqdns: dict[str, str] = {}
        # Which connector identity, if any, each container was born with. `None` is the answer
        # for every container on a deployment with no lake — which is every test but the gate's.
        self.identities: dict[str, str | None] = {}

    async def create_app(
        self,
        *,
        name: str,
        env: dict[str, str],
        tags: dict[str, str],
        identity_resource_id: str | None = None,
    ) -> str:
        self.create_calls.append(name)
        self.identities[name] = identity_resource_id
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
        # Answers from the env recorded at CREATE: a fake that answered `None` here would
        # quietly re-create the very data-loss path this lane exists to test.
        return self.created.get(name, {}).get(key)

    async def aclose(self) -> None:
        return None


class SupervisorScript:
    """The `/_sup/*` surface a relaunch drives, scripted per endpoint.

    `dev_start_status` + `dev_running` together script the supervisor's TWO 409 arms: the
    owned-child 409 answers `running=True` and the client maps it to the already-running
    sentinel, while the UNOWNED-server 409 leaves `running=False` and the client raises
    `SandboxError` from it."""

    def __init__(self) -> None:
        self.dev_start_status = 200
        self.dev_running = True
        self.dev_ready = True
        # WHAT THE APP ROOT ANSWERED, and it is absent from the body by default rather than
        # present-and-null: that is the wire shape of a supervisor image built before the field
        # existed, which the control plane grandfathers as PROVEN. Leaving it out is therefore
        # the reading every test in this file was written under, and it keeps the rollout arm
        # exercised on this lane instead of only in `test_base.py`. A test scripting the
        # page-less reading sets it to 404 and says so.
        self.root_status: int | None = None
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
            body: dict[str, object] = {
                "running": self.dev_running,
                "ready": self.dev_ready,
                "port": 3000,
            }
            if self.root_status is not None:
                body["root_status"] = self.root_status
            return httpx.Response(200, json=body)
        return httpx.Response(404, json={"detail": path})


@pytest.fixture
async def aca_wire(wire, fake_redis) -> AsyncIterator[SimpleNamespace]:
    """`wire`, with the canned `FakeSandboxClient` swapped for the real `AcaSandboxClient`
    over a recording control plane. ONE client instance for the whole test: a fresh one per
    request would lose the in-process `token_ref` map between the two relaunches."""
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
    """The first relaunch is genuinely cold and pays a create; the immediate repeat — the
    exact shape measured at 57.8s — must reuse what is already up: ZERO deletes and ZERO
    creates, because the ~20s ACA delete plus the ~33.5s ACA create are the entire cost
    being removed."""
    user, project = await _user_project(db_session, "rl-attach@rvaiglobal.com")
    app_id = await _seed_snapshot(db_session, user, project, fake_storage)

    cold = await _relaunch(client, user, project)
    assert cold.status_code == 200
    assert aca_wire.aca.create_calls == [app_name_for(app_id)]
    assert aca_wire.aca.delete_calls == []

    warm = await _relaunch(client, user, project)

    assert warm.status_code == 200
    assert aca_wire.aca.delete_calls == []
    assert aca_wire.aca.create_calls == [app_name_for(app_id)]


async def test_the_warm_relaunch_attaches_to_the_pre_existing_container(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, aca_wire
) -> None:
    """Every app is served from one public hostname under a key derived from the app id, so
    `previewUrl` is IDENTICAL for a reuse and for a rebuild: the create-call count is the only
    falsifiable proof here, and asserting the URL alone would be a tautology."""
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
    user, project_a = await _user_project(db_session, "rl-otherapp@rvaiglobal.com")
    project_b = await ProjectFactory.create(db_session, user.id)
    app_a = await _seed_snapshot(db_session, user, project_a, fake_storage)
    await _seed_snapshot(db_session, user, project_b, fake_storage)
    await _seed_worked_on(fake_storage, app_a)

    assert (await _relaunch(client, user, project_a)).status_code == 200
    resp = await _relaunch(client, user, project_b)

    assert resp.status_code == 409
    body = resp.json()["error"]
    assert body["code"] == "sandbox_reclaim_blocked"
    assert body["projectId"] == str(project_a.id)
    # `agentWorking` is DERIVED here rather than scripted: A's container is pardoned between
    # turns, so a field hardcoded true would fail exactly here.
    assert body["agentWorking"] is False
    assert aca_wire.aca.delete_calls == []
    assert aca_wire.aca.create_calls == [app_name_for(app_a)]


async def test_a_registry_marked_ending_is_never_attached_to(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, aca_wire
) -> None:
    user, project = await _user_project(db_session, "rl-ending@rvaiglobal.com")
    app_id = await _seed_snapshot(db_session, user, project, fake_storage)

    assert (await _relaunch(client, user, project)).status_code == 200
    await fake_redis.hset(registry_key(user.id), REGISTRY_FIELD_STATE, REGISTRY_STATE_ENDING)

    resp = await _relaunch(client, user, project)

    assert resp.status_code == 200
    name = app_name_for(app_id)
    assert aca_wire.aca.delete_calls == [name]
    assert aca_wire.aca.create_calls == [name, name]


async def test_no_registry_at_all_still_takes_the_restore_arm(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, aca_wire
) -> None:
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
    assert resp.json()["previewUrl"] == first.json()["previewUrl"]


async def test_a_relaunch_with_no_snapshot_creates_no_container_at_all(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, aca_wire
) -> None:
    user, project = await _user_project(db_session, "rl-nosnap-aca@rvaiglobal.com")

    resp = await _relaunch(client, user, project)

    assert resp.status_code == 404
    assert aca_wire.aca.create_calls == []
    assert aca_wire.aca.delete_calls == []


async def test_an_unowned_server_409_after_attach_still_returns_200_and_deletes_nothing(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, aca_wire
) -> None:
    user, project = await _user_project(db_session, "rl-409@rvaiglobal.com")
    app_id = await _seed_snapshot(db_session, user, project, fake_storage)

    assert (await _relaunch(client, user, project)).status_code == 200
    aca_wire.sup.dev_start_status = 409  # something is already serving on the dev port...
    aca_wire.sup.dev_running = False  # ...and it is not the supervisor's child

    resp = await _relaunch(client, user, project)

    assert resp.status_code == 200
    assert aca_wire.aca.delete_calls == []
    assert aca_wire.aca.create_calls == [app_name_for(app_id)]


async def _drain_the_watchers(manager: SessionManager) -> None:
    """Run the detached first-serve continuation to completion.

    Duplicated from `test_preview_state.py` rather than shared through the package conftest, and
    deliberately: this lane's assertions are ACA call counts, so what the continuation must not
    do here — issue a delete of its own — is a claim about this file's fixtures and belongs
    beside them."""
    for task in list(manager._tasks):
        with contextlib.suppress(Exception):
            await task


async def test_a_cold_relaunch_whose_root_shows_no_page_deletes_nothing_from_aca(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_redis,
    fake_storage,
    aca_wire,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★ THE REGRESSION, PINNED WHERE DESTRUCTION IS ACTUALLY OBSERVABLE. A 200 from this route
    says nothing about whether a container was destroyed — `restore_from_snapshot` issues its own
    teardown from INSIDE the client, so only the recording control plane can see it. This is the
    composition in which "the container survived" is a falsifiable claim.

    The shape: a cold relaunch restores the citizen's tree, the dev server comes up, and the app
    root answers 404 because the agent has not written `app/page.tsx` yet. The container is up and
    holds the work. The first version of the page check answered that by raising
    `SandboxNotReadyError` into the readiness handler, which re-raises on the cold arm — the raise
    escaped the lock scope before `scope.spare()`, compensation tore down the container that had
    just been built, and the citizen got a 503 over their own workspace.

    Mutation-check: raise `SandboxNotReadyError` from the page-less arm instead of retracting, and
    this goes red on the status code with a delete recorded against the app it just created."""
    monkeypatch.setattr(manager_mod, "_COLD_READY_BUDGET_SECONDS", 0.0)
    monkeypatch.setattr(manager_mod, "READINESS_POLL_S", 0)
    user, project = await _user_project(db_session, "rl-no-page@rvaiglobal.com")
    app_id = await _seed_snapshot(db_session, user, project, fake_storage)
    # `ready` stays TRUE alongside it, and that pairing is the whole point: the supervisor's
    # readiness is fail-open by its own design, so a 404 root is a READY dev server with nothing
    # to show. Script them apart and the two questions collapse into one.
    aca_wire.sup.root_status = 404

    resp = await _relaunch(client, user, project)

    assert resp.status_code == 200, "the citizen got an error over a container that is up"
    assert resp.json()["ready"] is False, "a 404 root was reported as a running app"
    assert resp.json()["previewUrl"], "the URL is framable the moment a page exists"
    assert aca_wire.aca.delete_calls == [], "the restored container was destroyed over a 404"
    assert aca_wire.aca.create_calls == [app_name_for(app_id)], "guard the premise: it was cold"

    await _drain_the_watchers(aca_wire.manager)
    assert aca_wire.aca.delete_calls == [], "the continuation is an observer, never an executioner"


# --- the release route, and the refusal it exists to resolve -------------------------


async def _release(client: AsyncClient, user, project) -> httpx.Response:
    return await client.post(
        f"/v1/build-sessions/projects/{project.id}/release", headers=auth_headers(user)
    )


async def test_release_gives_up_the_container_and_unblocks_the_switch(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, aca_wire
) -> None:
    user, project_a = await _user_project(db_session, "rl-release@rvaiglobal.com")
    project_b = await ProjectFactory.create(db_session, user.id)
    app_a = await _seed_snapshot(db_session, user, project_a, fake_storage)
    app_b = await _seed_snapshot(db_session, user, project_b, fake_storage)
    await _seed_worked_on(fake_storage, app_a)

    assert (await _relaunch(client, user, project_a)).status_code == 200
    assert (await _relaunch(client, user, project_b)).status_code == 409

    released = await _release(client, user, project_a)

    assert released.status_code == 200
    assert released.json()["released"] is True
    assert aca_wire.aca.delete_calls == [app_name_for(app_a)]
    assert (await _relaunch(client, user, project_b)).status_code == 200
    assert app_name_for(app_b) in aca_wire.aca.create_calls


async def test_releasing_a_workspace_that_is_already_gone_is_a_success(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, aca_wire
) -> None:
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
    """`reap_user` swallows `SandboxError` and returns False for its background callers, so
    `release_project_sandbox` reaps with `strict=True` to re-raise for this one — without it a
    container that refuses to die is indistinguishable from one that was never there."""
    # Mutation-check: drop `strict=True` in `release_project_sandbox` and this goes red with a
    # 200/`released: false`.
    user, project_a = await _user_project(db_session, "rl-release-fail@rvaiglobal.com")
    app_a = await _seed_snapshot(db_session, user, project_a, fake_storage)
    await _seed_worked_on(fake_storage, app_a)
    assert (await _relaunch(client, user, project_a)).status_code == 200

    async def throttled(*, name: str) -> None:
        aca_wire.aca.delete_calls.append(name)
        raise AcaTransientError("arm is throttling")

    monkeypatch.setattr(aca_wire.aca, "delete_app", throttled)

    resp = await _release(client, user, project_a)

    assert resp.status_code == 503, "a teardown that failed must not report a release"
    assert resp.status_code != 200
    assert "try again" in resp.json()["error"]["message"].lower()
    # `AcaSandboxClient.teardown` KEEPS the registry on this failure, so a later sweep retries
    # rather than orphaning a live container.
    assert await fake_redis.exists(registry_key(user.id)) == 1


async def test_release_is_owner_scoped_and_csrf_guarded(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, wire
) -> None:
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

    from_b = await client.get(
        f"/v1/build-sessions/projects/{project_b.id}/preview-state", headers=auth_headers(user)
    )
    assert from_b.json()["alive"] is False

    assert (await _release(client, user, project_a)).status_code == 200
    after = await client.get(
        f"/v1/build-sessions/projects/{project_a.id}/preview-state", headers=auth_headers(user)
    )
    body = after.json()
    assert (body["state"], body["alive"], body["previewUrl"]) == ("asleep", False, None)


async def test_preview_state_of_a_never_built_project_is_not_an_error(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, wire
) -> None:
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


# --- what the start path records ------------------------------------------------------------
#
# These rows escape the test transaction on purpose: `count(...)` owns its own session and
# COMMITS, so a count survives a rolled-back transaction. The consequence for every test below
# is that it has to start from a known-empty table — a rollback cannot reach these rows.


async def _counter_values(counter: HarnessCounter) -> list[int]:
    """Read as columns, not ORM rows: the session that read them is closed by the time the
    assertion runs."""
    async with async_session_factory() as db:
        rows = (
            await db.execute(
                sa.select(HarnessCount.value).where(HarnessCount.name == counter.value)
            )
        ).all()
    return [int(v) for (v,) in rows]


async def _counter_app_ids(counter: HarnessCounter) -> list[uuid.UUID | None]:
    async with async_session_factory() as db:
        rows = (
            await db.execute(
                sa.select(HarnessCount.app_id).where(HarnessCount.name == counter.value)
            )
        ).all()
    return [app_id for (app_id,) in rows]


async def test_a_cold_relaunch_records_the_press_the_arrival_and_the_wait(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_redis,
    fake_storage,
    wire,
    empty_harness_counts,
) -> None:
    # Mutation check: pass `app_id=app_id` to the attempted emit and this goes red.
    user, project = await _user_project(db_session, "rl-count-cold@rvaiglobal.com")
    app_id = await _seed_snapshot(db_session, user, project, fake_storage)

    assert (await _relaunch(client, user, project)).status_code == 200

    assert await _counter_values(HarnessCounter.APP_START_ATTEMPTED) == [1]
    assert await _counter_values(HarnessCounter.APP_START_REACHED_RUNNING) == [1]
    cold = await _counter_values(HarnessCounter.APP_COLD_START_MS)
    assert len(cold) == 1
    # A sanity bound only: the fake sandbox answers instantly, so this cannot fail for the
    # boundary it names — the clock's real boundary is pinned by the slow-attach test below.
    assert 0 <= cold[0] < 120_000

    assert await _counter_app_ids(HarnessCounter.APP_START_ATTEMPTED) == [None]
    assert await _counter_app_ids(HarnessCounter.APP_START_REACHED_RUNNING) == [app_id]
    assert await _counter_app_ids(HarnessCounter.APP_COLD_START_MS) == [app_id]


async def test_the_attach_arm_records_the_press_and_the_arrival_but_no_duration(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_redis,
    fake_storage,
    aca_wire,
    empty_harness_counts,
) -> None:
    """A 15-second attach budget and a 120-second cold budget averaged together produce a
    number that describes neither, so only the restore arm writes a duration."""
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
    # Mutation check: move the reached-running emit out from under `if ready:` and this goes red.
    user, project = await _user_project(db_session, "rl-count-unready@rvaiglobal.com")
    app_id = await _seed_snapshot(db_session, user, project, fake_storage)

    # A cold relaunch first, so the registry names THIS app and the second press below actually
    # takes the attach arm rather than building again.
    assert (await _relaunch(client, user, project)).status_code == 200
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

    assert (await _relaunch(client, user, project)).status_code == 200

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
    pressed the control and did not see their app, which is exactly the press this counter has
    to catch.

    Mutation check: move the attempted emit below the snapshot gate and this goes red.

    The occupant is an `ensure_sandbox` session (re-fixtured off the deleted start route). It
    emits none of the three counters this asserts on — those live in `relaunch_preview` alone —
    so the single `[1]` below is still the single press this test is about."""
    user, project = await _user_project(db_session, "rl-count-409@rvaiglobal.com")
    await _seed_snapshot(db_session, user, project, fake_storage)

    await a_live_session(wire, db_session, user, project.id)

    assert (await _relaunch(client, user, project)).status_code == 409

    assert await _counter_values(HarnessCounter.APP_START_ATTEMPTED) == [1]
    assert await _counter_values(HarnessCounter.APP_START_REACHED_RUNNING) == []
    assert await _counter_values(HarnessCounter.APP_COLD_START_MS) == []


async def test_a_press_refused_because_reclaiming_would_destroy_work_still_counts(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_redis,
    fake_storage,
    aca_wire,
    empty_harness_counts,
) -> None:
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
    # Mutation check: move the clock's start to function entry and this goes red.
    user, project = await _user_project(db_session, "rl-count-boundary@rvaiglobal.com")
    await _seed_snapshot(db_session, user, project, fake_storage)

    # A cold relaunch first, so the registry names this app and the slow attach below is
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
