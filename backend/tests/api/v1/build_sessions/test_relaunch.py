"""POST /v1/build-sessions/relaunch: restore a torn-down app from its snapshot into a
fresh, READY sandbox (cookie auth + CSRF, owner-scoping, no build slot taken)."""

from __future__ import annotations

import asyncio
import base64
import json
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import sqlalchemy as sa
from httpx import AsyncClient
from pydantic import SecretStr
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession
from structlog.testing import capture_logs

import src.services.build_sessions.manager as manager_mod
from src.api.v1.build_sessions.deps import (
    sandbox_dependency,
    sandbox_or_none_dependency,
)
from src.api.v1.build_sessions.schemas import (
    SURFACE_PRESENT_STAY_SECONDS,
)
from src.db.base import async_session_factory
from src.db.models.app_registry import AppRegistry
from src.db.models.harness_counter import HarnessCount, HarnessCounter
from src.db.models.sandbox_start import SandboxStart
from src.services.build_sessions.alarms import SERVING_PROOF_ABSENT_AT_TEARDOWN
from src.services.build_sessions.appdata import resolve_app_for_project
from src.services.build_sessions.locks import lock_is_held
from src.services.build_sessions.manager import SessionManager
from src.services.build_sessions.shutdown import OwedTeardown
from src.services.redis import (
    BUILD_COORDINATION_UNAVAILABLE_MSG,
    REGISTRY_STATE_ENDING,
    registry_key,
)
from src.services.redis.keys import (
    REGISTRY_FIELD_APP_NAME,
    REGISTRY_FIELD_PREVIEW_STAY_UNTIL,
    REGISTRY_FIELD_SERVING_SINCE,
    REGISTRY_FIELD_STATE,
    REGISTRY_FIELD_STAY_WRITER,
)
from src.services.sandbox.aca import AcaControlPlane, AcaTransientError
from src.services.sandbox.base import (
    SandboxError,
    SandboxGoneError,
    SandboxHandle,
    a_fresh_sandbox_name,
    new_alias,
)
from src.services.sandbox.client import AcaSandboxClient
from src.services.sandbox.config import SandboxConfig
from src.services.storage import snapshot_key
from tests.api.v1.build_sessions.conftest import (
    a_live_session,
    auth_headers,
    seed_live_sandbox_state,
)
from tests.conftest import forget_every_harness_count
from tests.factories import ProjectFactory, UserFactory
from tests.fakes import a_git_bundle, a_ready_pool_row, detached_work_done


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
    """Mark this app as holding real work: the reclaim guard reads a saved bundle as proof that
    something was built here."""
    key = snapshot_key(app_id)
    await store.put(key, b"SAVED-BUNDLE")
    # `FakeStorage.head` reads `last_modified` off `mtimes`, and the guard keys on that
    # timestamp — a bundle with no mtime reads as "no bundle".
    store.mtimes[key] = datetime.now(UTC)


async def test_relaunch_is_accepted_and_the_poll_reports_the_app(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, wire
) -> None:
    """The start answers 202 with the app it admitted: whether the app is up, and where, is the
    preview-state poll's to say, and saying it twice is how two readers came to disagree about
    one container. The other two fields are a constant `status`, which a tab loaded before this
    server needs to read the body at all, and the start it began, which the browser times
    itself against.

    Mutation-check: drop `status` from `RelaunchPreviewResponse` and this goes red."""
    user, project = await _user_project(db_session, "rl1@rvaiglobal.com")
    app_id = await _seed_snapshot(db_session, user, project, fake_storage)

    resp = await _relaunch(client, user, project, wire.manager)

    assert resp.status_code == 202
    start_id = await db_session.scalar(
        sa.select(SandboxStart.id).where(SandboxStart.user_id == user.id)
    )
    assert resp.json() == {
        "appId": str(app_id),
        "status": "provisioning",
        "startId": str(start_id),
    }
    polled = await client.get(
        f"/v1/build-sessions/projects/{project.id}/preview-state", headers=auth_headers(user)
    )
    assert polled.json()["state"] == "alive"
    assert polled.json()["previewUrl"].startswith("https://")
    # Relaunch does NOT occupy the build slot: the lock is free and no session is live.
    assert wire.manager._active_by_user == {}
    assert await lock_is_held(fake_redis, user.id) is False


async def test_a_served_preview_is_handed_to_the_screen_that_asked_for_it(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, wire
) -> None:
    """THE GRANT AT THE MOMENT A PREVIEW BECOMES VIEWABLE IS THE SHORT ONE, and that is the whole
    hand-over: from here the surface framing the app renews on its own poll, so what this grant
    owes is the gap until the first renewal arrives — not a reprieve of its own.

    A thirty-minute stamp here would keep a container nobody came back to alive for half an hour
    after the tab closed, which is the cost presence renewal exists to stop paying, reintroduced
    at the one moment every relaunch passes through.

    Mutation check: change the writer back to `BUILDER_ACTED` and both assertions go red."""
    user, project = await _user_project(db_session, "served@rvaiglobal.com")
    await _seed_snapshot(db_session, user, project, fake_storage)
    before = datetime.now(UTC)

    resp = await _relaunch(client, user, project, wire.manager)
    assert resp.status_code == 202

    raw = await fake_redis.hmget(
        registry_key(user.id),
        [REGISTRY_FIELD_STAY_WRITER, REGISTRY_FIELD_PREVIEW_STAY_UNTIL],
    )
    writer, stamp = raw[0], raw[1]
    assert (writer.decode() if isinstance(writer, bytes) else str(writer)) == "surface_present"
    stay = datetime.fromisoformat(stamp.decode() if isinstance(stamp, bytes) else str(stamp))
    assert stay - before <= timedelta(seconds=SURFACE_PRESENT_STAY_SECONDS + 5)


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
    """A live session owns the one-per-user slot; relaunch 409s rather than pre-empting it.

    Re-fixtured onto `a_live_session`. The slot has to be genuinely OCCUPIED for this to prove
    anything, and relaunch provably cannot occupy it itself — asserted directly by
    `test_relaunch_is_accepted_and_the_poll_reports_the_app`: `_active_by_user == {}` — so the
    occupant comes from `ensure_sandbox`, the only allocator left that claims the slot. Same
    project as the relaunch, which is what earns the BARE conflict rather than the hand-over 409
    (`_slot_conflict_for`)."""
    user, project = await _user_project(db_session, "rl3@rvaiglobal.com")
    await _seed_snapshot(db_session, user, project, fake_storage)

    await a_live_session(wire, db_session, user, project.id)

    conflict = await client.post(
        "/v1/build-sessions/relaunch",
        json={"projectId": str(project.id)},
        headers=auth_headers(user),
    )
    assert conflict.status_code == 409
    err = conflict.json()["error"]
    assert err["code"] == "build_session_already_active"
    assert set(err) == {"message", "code"}


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

    resp = await _relaunch(client, user, project, wire.manager)
    assert resp.status_code == 202
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
# fixture's `FakeSandboxClient` cannot see a delete the client issues from INSIDE itself, as a
# failed birth's clean-up does. Driving the real `AcaSandboxClient` over a recording control
# plane is the only composition where "no container was destroyed" is observable — a 200 from
# this route says nothing about it.


class RecordingAca(AcaControlPlane):
    """Records lifecycle calls instead of talking to Azure; `__init__` is overridden so it
    never builds a credential or a mgmt client.

    The FQDN carries the create ORDINAL (`-r1`, `-r2`, …), so every create can be told apart
    by its address as well as its name."""

    def __init__(self) -> None:
        self.created: dict[str, dict[str, str]] = {}
        self.create_calls: list[str] = []
        self.delete_calls: list[str] = []
        self.fqdns: dict[str, str] = {}
        # Which connector identity, if any, each container was born with. `None` is the answer
        # for every container on a deployment with no lake — which is every test but the gate's.
        self.identities: dict[str, str | None] = {}
        self.stamped: dict[str, dict[str, str]] = {}

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

    async def stamp_tags(self, *, name: str, tags: dict[str, str]) -> None:
        self.stamped.setdefault(name, {}).update(tags)

    def made_for_the_pool(self, name: str) -> str:
        """A container a pool fill made, as Azure knows it. Returns its address."""
        self.created[name] = {
            "SUPERVISOR_TOKEN": "pool-bearer",
            "BIAL_POOL_MEMBER": "1",
            "BIAL_BASE_PATH": f"/a/{new_alias()}",
        }
        self.fqdns[name] = f"{name}.pool.westeurope.azurecontainerapps.io"
        return self.fqdns[name]

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
        # Every delivery of a pool container's settings: the host it went to, and the names.
        self.configured: list[tuple[str, dict[str, str]]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/_sup")
        self.paths.append(path)
        if path == "/health":
            return httpx.Response(200, json={"ok": True})
        if path == "/configure":
            self.configured.append((request.url.host, json.loads(request.content)["env"]))
            return httpx.Response(200, json={"ok": True})
        if path == "/files":
            return httpx.Response(200, json={"ok": True})
        if path == "/exec":
            # A BUNDLE READ MUST ANSWER WITH A PARSEABLE BUNDLE. Every door that destroys a
            # container writes its tree back first, and a container that answers the read with
            # nothing is one whose write-back raises — so a supervisor double that stayed silent
            # here would make every release in this file spare the container it means to give up.
            cmd = json.loads(request.content).get("cmd") or []
            if cmd[:1] == ["base64"]:
                stdout = base64.b64encode(a_git_bundle()).decode()
                return httpx.Response(200, json={"stdout": stdout, "stderr": "", "exit": 0})
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


@pytest.fixture(autouse=True)
def handed_over(monkeypatch: pytest.MonkeyPatch) -> list[OwedTeardown]:
    """Every container a start in this file hands to the shutdown routine, recorded not run.

    Left to run, the routine reaches into this file's own control-plane double at an arbitrary
    await point and deletes the outgoing container mid-assertion, so `delete_calls` would depend
    on scheduling. What it does once spawned is `test_shutdown.py`'s subject."""
    spawned: list[OwedTeardown] = []

    def _record(owed: OwedTeardown, **_aimed_at: object) -> None:
        spawned.append(owed)

    monkeypatch.setattr(manager_mod, "shut_it_down_in_the_background", _record)
    return spawned


async def _relaunch(client: AsyncClient, user, project, manager: SessionManager) -> httpx.Response:
    """One press, with everything the start detached waited out before the caller asserts."""
    resp = await client.post(
        "/v1/build-sessions/relaunch",
        json={"projectId": str(project.id)},
        headers=auth_headers(user),
    )
    await detached_work_done(manager)
    return resp


async def test_a_relaunch_onto_a_live_healthy_container_touches_no_aca_lifecycle(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, aca_wire
) -> None:
    """The first relaunch is genuinely cold and pays a create; the immediate repeat — the
    exact shape measured at 57.8s — must reuse what is already up: ZERO deletes and ZERO
    creates, because the ~20s ACA delete plus the ~33.5s ACA create are the entire cost
    being removed."""
    user, project = await _user_project(db_session, "rl-attach@rvaiglobal.com")
    await _seed_snapshot(db_session, user, project, fake_storage)

    cold = await _relaunch(client, user, project, aca_wire.manager)
    assert cold.status_code == 202
    assert len(aca_wire.aca.create_calls) == 1
    assert aca_wire.aca.delete_calls == []
    cold_creates = list(aca_wire.aca.create_calls)

    warm = await _relaunch(client, user, project, aca_wire.manager)

    assert warm.status_code == 202
    assert aca_wire.aca.delete_calls == []
    assert aca_wire.aca.create_calls == cold_creates


async def test_a_relaunch_that_attaches_answers_with_no_start(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, aca_wire
) -> None:
    # Mutation check: answer with the start's id whether or not its row was written and the
    # second `startId` goes red.
    user, project = await _user_project(db_session, "rl-attach-start@rvaiglobal.com")
    await _seed_snapshot(db_session, user, project, fake_storage)

    cold = await _relaunch(client, user, project, aca_wire.manager)
    warm = await _relaunch(client, user, project, aca_wire.manager)

    assert cold.json()["startId"] is not None
    assert warm.json()["startId"] is None


async def test_the_warm_relaunch_attaches_to_the_pre_existing_container(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, aca_wire
) -> None:
    """Every app is served from one public hostname under a key derived from the app id, so the
    preview URL is IDENTICAL for a reuse and for a rebuild: the create-call count is the only
    falsifiable proof here, and asserting the URL alone would be a tautology."""
    user, project = await _user_project(db_session, "rl-attach-fqdn@rvaiglobal.com")
    await _seed_snapshot(db_session, user, project, fake_storage)

    assert (await _relaunch(client, user, project, aca_wire.manager)).status_code == 202
    creates_after_cold = list(aca_wire.aca.create_calls)
    assert (await _relaunch(client, user, project, aca_wire.manager)).status_code == 202

    assert aca_wire.aca.create_calls == creates_after_cold, (
        "the warm relaunch attached; a rebuild would have appended another create"
    )


async def test_a_registry_naming_a_different_app_is_a_switch_not_a_refusal(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_redis,
    fake_storage,
    aca_wire,
    handed_over: list[object],
) -> None:
    """Pressing start on a second project used to answer 409 `sandbox_reclaim_blocked` naming
    the first. It is admitted and starts, and the outgoing container leaves by the hand-over.

    THE DELETE COUNT IS THE ASSERTION WITH TEETH. Two parties aimed at one container is the
    failure this whole design is built around, so the start must issue no ARM delete at all —
    the routine it hands to owns that, after the tree is written back."""
    user, project_a = await _user_project(db_session, "rl-otherapp@rvaiglobal.com")
    project_b = await ProjectFactory.create(db_session, user.id)
    app_a = await _seed_snapshot(db_session, user, project_a, fake_storage)
    app_b = await _seed_snapshot(db_session, user, project_b, fake_storage)
    await _seed_worked_on(fake_storage, app_a)

    assert (await _relaunch(client, user, project_a, aca_wire.manager)).status_code == 202
    resp = await _relaunch(client, user, project_b, aca_wire.manager)

    assert resp.status_code == 202, resp.text
    assert resp.json()["appId"] == str(app_b)
    assert aca_wire.aca.delete_calls == []
    assert len(set(aca_wire.aca.create_calls)) == 2
    assert len(handed_over) == 1


async def test_a_registry_marked_ending_is_never_attached_to(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_redis,
    fake_storage,
    aca_wire,
    handed_over: list[OwedTeardown],
) -> None:
    """★ A container already marked ending holds the slot of a session nothing is running. The
    start owes its delete and goes on at once under a name of its own, and the debt carries no
    write-back: whatever that container held, the start restores the saved copy instead.

    Mutation check: owe the holder with a write-back and the row says so; delete it inline and
    the start records an ARM delete it waited on."""
    user, project = await _user_project(db_session, "rl-ending@rvaiglobal.com")
    await _seed_snapshot(db_session, user, project, fake_storage)

    assert (await _relaunch(client, user, project, aca_wire.manager)).status_code == 202
    await fake_redis.hset(registry_key(user.id), REGISTRY_FIELD_STATE, REGISTRY_STATE_ENDING)

    resp = await _relaunch(client, user, project, aca_wire.manager)

    assert resp.status_code == 202
    held, replacement = aca_wire.aca.create_calls
    assert replacement != held
    assert aca_wire.aca.delete_calls == [], "the start waited on an ARM delete"
    assert [(owed.app_name, owed.write_back) for owed in handed_over] == [(held, False)]
    reg = await fake_redis.hgetall(registry_key(user.id))
    assert reg[REGISTRY_FIELD_APP_NAME] == replacement


async def test_a_container_from_the_pool_is_the_persons_one_workspace(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_redis,
    fake_storage,
    aca_wire,
    handed_over: list[OwedTeardown],
    empty_sandbox_pool: None,
) -> None:
    """Once claimed, a pool container is a workspace like any other: the next press attaches to
    it, and switching to another project writes it back and hands it to the shutdown routine,
    never back to the pool. The switch's start takes the replacement the first claim made.

    Mutation check: claim nothing in `_provision_container` and the first start records a create
    under a name of its own instead of the pool container."""
    aca_wire.sandbox._config = aca_wire.sandbox._config.model_copy(
        update={"pool_day_size": 1, "pool_night_size": 1}
    )
    member = a_fresh_sandbox_name()
    fqdn = aca_wire.aca.made_for_the_pool(member)
    await a_ready_pool_row(member, fqdn=fqdn, image_ref="acr/img:latest")
    user, project_a = await _user_project(db_session, "rl-pool@rvaiglobal.com")
    project_b = await ProjectFactory.create(db_session, user.id)
    app_a = await _seed_snapshot(db_session, user, project_a, fake_storage)
    await _seed_snapshot(db_session, user, project_b, fake_storage)
    await _seed_worked_on(fake_storage, app_a)

    assert (await _relaunch(client, user, project_a, aca_wire.manager)).status_code == 202
    assert await fake_redis.hget(registry_key(user.id), REGISTRY_FIELD_APP_NAME) == member
    await asyncio.gather(*aca_wire.sandbox._detached)
    assert (await _relaunch(client, user, project_a, aca_wire.manager)).status_code == 202
    [replacement] = aca_wire.aca.create_calls
    assert aca_wire.aca.created[replacement]["BIAL_POOL_MEMBER"] == "1"
    assert [host for host, _ in aca_wire.sup.configured] == [fqdn]

    assert (await _relaunch(client, user, project_b, aca_wire.manager)).status_code == 202

    assert [(owed.app_name, owed.write_back) for owed in handed_over] == [(member, True)]
    assert aca_wire.aca.delete_calls == []
    assert await fake_redis.hget(registry_key(user.id), REGISTRY_FIELD_APP_NAME) == replacement
    await asyncio.gather(*aca_wire.sandbox._detached)


async def test_a_restarted_pool_container_is_given_up_and_its_project_restored_again(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_redis,
    fake_storage,
    wire,
    handed_over: list[OwedTeardown],
) -> None:
    """★ Azure restarting a claimed pool container brings it back with no project settings and
    none of its files, so starting the app in it would show the bare template. The start treats
    it as gone: the saved copy goes into a new container, the restarted one is never started,
    and it is owed its delete with nothing written back.

    Mutation check: attach to it whatever it reports and no second restore happens."""
    sandbox = wire.sbx
    user, project = await _user_project(db_session, "rl-restarted@rvaiglobal.com")
    await _seed_snapshot(db_session, user, project, fake_storage)
    assert (await _relaunch(client, user, project, wire.manager)).status_code == 202
    [restarted] = sandbox.restored
    sandbox.attach_handle = sandbox.by_name[restarted]
    sandbox.unconfigured.add(restarted)

    assert (await _relaunch(client, user, project, wire.manager)).status_code == 202

    assert sandbox.restored[0] == restarted
    [reborn] = sandbox.restored[1:]
    assert sandbox.started == [restarted, reborn]
    assert [(owed.app_name, owed.write_back) for owed in handed_over] == [(restarted, False)]
    assert await fake_redis.hget(registry_key(user.id), REGISTRY_FIELD_APP_NAME) == reborn


async def test_a_restarted_container_is_not_reported_alive_while_its_rebirth_waits(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_redis,
    fake_storage,
    wire,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★ The restore runs behind the press, and until it hands the restarted container over, the
    record still names it. A poll in that window framed it as alive, and the browser framed a
    502, so its serving proof goes the moment the start decides on a rebirth.

    Mutation check: decide on the rebirth without retracting the proof and the poll reads alive."""
    sandbox = wire.sbx
    user, project = await _user_project(db_session, "rl-restarted-poll@rvaiglobal.com")
    await _seed_snapshot(db_session, user, project, fake_storage)
    assert (await _relaunch(client, user, project, wire.manager)).status_code == 202
    [restarted] = sandbox.restored
    sandbox.attach_handle = sandbox.by_name[restarted]
    sandbox.unconfigured.add(restarted)
    url = f"/v1/build-sessions/projects/{project.id}/preview-state"
    before = (await client.get(url, headers=auth_headers(user))).json()
    assert before["state"] == "alive", "guard the premise: the restarted container had served"

    at_the_rebirth, go_on = asyncio.Event(), asyncio.Event()
    clear_the_way = wire.manager._clear_the_way_for_a_birth

    async def held_at_the_rebirth(*args: Any) -> None:
        at_the_rebirth.set()
        await go_on.wait()
        await clear_the_way(*args)

    monkeypatch.setattr(wire.manager, "_clear_the_way_for_a_birth", held_at_the_rebirth)
    pressed = await client.post(
        "/v1/build-sessions/relaunch",
        json={"projectId": str(project.id)},
        headers=auth_headers(user),
    )
    assert pressed.status_code == 202
    await asyncio.wait_for(at_the_rebirth.wait(), timeout=5)

    meanwhile = (await client.get(url, headers=auth_headers(user))).json()
    go_on.set()
    await detached_work_done(wire.manager)

    assert meanwhile["state"] == "starting"
    assert meanwhile.get("previewUrl") is None


@pytest.mark.parametrize("had_served", [True, False], ids=["served", "never-served"])
async def test_a_restarted_container_raises_the_absent_proof_alarm_only_if_it_never_served(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_redis,
    fake_storage,
    wire,
    handed_over: list[OwedTeardown],
    had_served: bool,
) -> None:
    """The start retracts a restarted container's proof before handing it over, and the alarm
    counts containers that reached teardown never having served.

    Mutation check: sound the alarm at every hand-over and the one that served is counted.
    Mutation check: note every restarted container as served and the silent one is not."""
    sandbox = wire.sbx
    user, project = await _user_project(db_session, f"rl-restarted-{had_served}@rvaiglobal.com")
    await _seed_snapshot(db_session, user, project, fake_storage)
    assert (await _relaunch(client, user, project, wire.manager)).status_code == 202
    [restarted] = sandbox.restored
    sandbox.attach_handle = sandbox.by_name[restarted]
    sandbox.unconfigured.add(restarted)
    assert await fake_redis.hget(registry_key(user.id), REGISTRY_FIELD_SERVING_SINCE), (
        "guard the premise: the first start watched its app serve"
    )
    if not had_served:
        await fake_redis.hset(registry_key(user.id), REGISTRY_FIELD_SERVING_SINCE, "")

    with capture_logs() as logged:
        assert (await _relaunch(client, user, project, wire.manager)).status_code == 202

    assert [(owed.app_name, owed.write_back) for owed in handed_over] == [(restarted, False)]
    fired = [e for e in logged if e["event"] == SERVING_PROOF_ABSENT_AT_TEARDOWN]
    assert [(e["app_name"], e["reason"]) for e in fired] == (
        [] if had_served else [(restarted, "replaced")]
    )


async def test_no_registry_at_all_still_takes_the_restore_arm(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, aca_wire
) -> None:
    user, project = await _user_project(db_session, "rl-noreg@rvaiglobal.com")
    await _seed_snapshot(db_session, user, project, fake_storage)

    assert (await _relaunch(client, user, project, aca_wire.manager)).status_code == 202
    await fake_redis.delete(registry_key(user.id))

    resp = await _relaunch(client, user, project, aca_wire.manager)

    assert resp.status_code == 202
    assert len(set(aca_wire.aca.create_calls)) == 2


async def test_a_control_plane_restart_reattaches_instead_of_rebuilding_the_container(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, aca_wire
) -> None:
    user, project = await _user_project(db_session, "rl-restart@rvaiglobal.com")
    await _seed_snapshot(db_session, user, project, fake_storage)

    assert (await _relaunch(client, user, project, aca_wire.manager)).status_code == 202
    aca_wire.sandbox._token_refs.clear()  # what a control-plane restart leaves behind

    resp = await _relaunch(client, user, project, aca_wire.manager)

    assert resp.status_code == 202
    assert aca_wire.aca.delete_calls == [], "a restart must not destroy a live container"
    assert len(aca_wire.aca.create_calls) == 1, "…nor build a replacement over the citizen's tree"


async def test_a_relaunch_with_no_snapshot_creates_no_container_at_all(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, aca_wire
) -> None:
    user, project = await _user_project(db_session, "rl-nosnap-aca@rvaiglobal.com")

    resp = await _relaunch(client, user, project, aca_wire.manager)

    assert resp.status_code == 404
    assert aca_wire.aca.create_calls == []
    assert aca_wire.aca.delete_calls == []


async def test_an_unowned_server_409_after_attach_is_still_admitted_and_deletes_nothing(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, aca_wire
) -> None:
    user, project = await _user_project(db_session, "rl-409@rvaiglobal.com")
    await _seed_snapshot(db_session, user, project, fake_storage)

    assert (await _relaunch(client, user, project, aca_wire.manager)).status_code == 202
    aca_wire.sup.dev_start_status = 409  # something is already serving on the dev port...
    aca_wire.sup.dev_running = False  # ...and it is not the supervisor's child

    resp = await _relaunch(client, user, project, aca_wire.manager)

    assert resp.status_code == 202
    assert aca_wire.aca.delete_calls == []
    assert len(aca_wire.aca.create_calls) == 1


async def test_a_cold_relaunch_whose_root_shows_no_page_deletes_nothing_from_aca(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_redis,
    fake_storage,
    aca_wire,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★ THE REGRESSION, PINNED WHERE DESTRUCTION IS ACTUALLY OBSERVABLE. A 202 from this route
    says nothing about whether a container was destroyed — `restore_from_snapshot` issues its own
    teardown from INSIDE the client, so only the recording control plane can see it. This is the
    composition in which "the container survived" is a falsifiable claim.

    The shape: a cold relaunch restores the citizen's tree, the dev server comes up, and the app
    root answers 404 because the agent has not written `app/page.tsx` yet. The container is up and
    holds the work. Answering that with a raise escapes the lock scope before `scope.spare()`, so
    compensation tears down the container that has just been built.

    Mutation-check: raise from `_retract_a_proof_it_cannot_back` when the root shows no page, and
    this goes red with a delete recorded against the app it just created."""
    monkeypatch.setattr(manager_mod, "_COLD_READY_BUDGET_SECONDS", 0.0)
    monkeypatch.setattr(manager_mod, "READINESS_POLL_S", 0)
    user, project = await _user_project(db_session, "rl-no-page@rvaiglobal.com")
    await _seed_snapshot(db_session, user, project, fake_storage)
    # `ready` stays TRUE alongside it, and that pairing is the whole point: the supervisor's
    # readiness is fail-open by its own design, so a 404 root is a READY dev server with nothing
    # to show. Script them apart and the two questions collapse into one.
    aca_wire.sup.root_status = 404

    resp = await _relaunch(client, user, project, aca_wire.manager)

    assert resp.status_code == 202
    assert aca_wire.aca.delete_calls == [], "the restored container was destroyed over a 404"
    assert len(aca_wire.aca.create_calls) == 1, "guard the premise: it was cold"
    polled = await client.get(
        f"/v1/build-sessions/projects/{project.id}/preview-state", headers=auth_headers(user)
    )
    assert polled.json()["state"] == "starting", "a 404 root was reported as a running app"


# --- the release route, and the refusal it exists to resolve -------------------------


async def _release(client: AsyncClient, user, project) -> httpx.Response:
    return await client.post(
        f"/v1/build-sessions/projects/{project.id}/release", headers=auth_headers(user)
    )


async def test_release_gives_up_the_container_on_the_spot(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage, aca_wire
) -> None:
    """Release is no longer the way out of a refusal — nothing refuses — but it is still the one
    route that destroys a container because the citizen said so, and it still has to be
    immediate: the delete happens inside the request, not on a later sweep."""
    user, project_a = await _user_project(db_session, "rl-release@rvaiglobal.com")
    project_b = await ProjectFactory.create(db_session, user.id)
    app_a = await _seed_snapshot(db_session, user, project_a, fake_storage)
    await _seed_snapshot(db_session, user, project_b, fake_storage)
    await _seed_worked_on(fake_storage, app_a)

    assert (await _relaunch(client, user, project_a, aca_wire.manager)).status_code == 202

    released = await _release(client, user, project_a)

    assert released.status_code == 200
    assert released.json()["released"] is True
    assert aca_wire.aca.delete_calls == aca_wire.aca.create_calls
    assert (await _relaunch(client, user, project_b, aca_wire.manager)).status_code == 202
    assert len(set(aca_wire.aca.create_calls)) == 2


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
    assert (await _relaunch(client, user, project_a, aca_wire.manager)).status_code == 202

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

    assert (await _relaunch(client, user, project_a, aca_wire.manager)).status_code == 202
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

    assert (await _relaunch(client, user, project, wire.manager)).status_code == 202

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
    """The attach arm restored nothing, so only the restore arm writes a duration: folding the
    attach arm's near-instant readings in would make the number describe neither."""
    user, project = await _user_project(db_session, "rl-count-attach@rvaiglobal.com")
    await _seed_snapshot(db_session, user, project, fake_storage)

    assert (await _relaunch(client, user, project, aca_wire.manager)).status_code == 202
    assert (await _relaunch(client, user, project, aca_wire.manager)).status_code == 202

    assert len(await _counter_values(HarnessCounter.APP_START_ATTEMPTED)) == 2
    assert len(await _counter_values(HarnessCounter.APP_START_REACHED_RUNNING)) == 2
    assert len(await _counter_values(HarnessCounter.APP_COLD_START_MS)) == 1


async def test_an_attach_that_shows_no_page_is_a_press_that_never_arrived(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_redis,
    fake_storage,
    wire,
    empty_harness_counts,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Mutation check: count the arrival whatever the watch answered and this goes red.
    monkeypatch.setattr(manager_mod, "_COLD_READY_BUDGET_SECONDS", 0.0)
    user, project = await _user_project(db_session, "rl-count-unready@rvaiglobal.com")
    await _seed_snapshot(db_session, user, project, fake_storage)

    # A cold relaunch first, so the registry names THIS app and the second press below actually
    # takes the attach arm rather than building again.
    assert (await _relaunch(client, user, project, wire.manager)).status_code == 202
    wire.sbx.attach_handle = SandboxHandle(
        fqdn="live.example",
        token="tok",
        app_name=wire.sbx.restored[-1],
        preview_url="https://live.example",
        ready=True,
    )

    wire.sbx.root_status = 404

    assert (await _relaunch(client, user, project, wire.manager)).status_code == 202

    assert len(await _counter_values(HarnessCounter.APP_START_ATTEMPTED)) == 2
    assert len(await _counter_values(HarnessCounter.APP_START_REACHED_RUNNING)) == 1
    assert len(await _counter_values(HarnessCounter.APP_COLD_START_MS)) == 1


async def test_a_page_the_store_would_not_record_is_still_a_press_that_arrived(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_redis,
    fake_storage,
    wire,
    empty_harness_counts,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The proof is bookkeeping behind an app that is already up. A store that will not take it
    leaves the stamp to the reconciler's sweep; the page was still seen.

    Mutation check: drop the `except RedisError` around the recorder in the watch and this goes
    red."""
    monkeypatch.setattr(manager_mod, "_COLD_READY_BUDGET_SECONDS", 0.0)
    user, project = await _user_project(db_session, "rl-count-store-down@rvaiglobal.com")
    await _seed_snapshot(db_session, user, project, fake_storage)

    async def the_store_will_not_answer(*args: object, **kwargs: object) -> None:
        raise RedisError("redis is down")

    monkeypatch.setattr(manager_mod, "record_the_first_serve", the_store_will_not_answer)

    assert (await _relaunch(client, user, project, wire.manager)).status_code == 202

    assert len(await _counter_values(HarnessCounter.APP_START_REACHED_RUNNING)) == 1


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

    assert (await _relaunch(client, user, project, wire.manager)).status_code == 409

    assert await _counter_values(HarnessCounter.APP_START_ATTEMPTED) == [1]
    assert await _counter_values(HarnessCounter.APP_START_REACHED_RUNNING) == []
    assert await _counter_values(HarnessCounter.APP_COLD_START_MS) == []


async def test_a_press_that_switches_projects_counts_as_a_press_that_arrived(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_redis,
    fake_storage,
    aca_wire,
    empty_harness_counts,
    handed_over: list[object],
) -> None:
    """The switch changed what this ratio measures, so it is pinned here rather than left to
    drift: the second press used to be refused and counted as an attempt that reached nothing.
    It now starts a container, so both presses arrive — and the denominator is still one row per
    press, which is what makes the ratio readable at all."""
    user, project_a = await _user_project(db_session, "rl-count-reclaim@rvaiglobal.com")
    project_b = await ProjectFactory.create(db_session, user.id)
    app_a = await _seed_snapshot(db_session, user, project_a, fake_storage)
    await _seed_snapshot(db_session, user, project_b, fake_storage)
    await _seed_worked_on(fake_storage, app_a)

    assert (await _relaunch(client, user, project_a, aca_wire.manager)).status_code == 202
    assert (await _relaunch(client, user, project_b, aca_wire.manager)).status_code == 202

    assert len(await _counter_values(HarnessCounter.APP_START_ATTEMPTED)) == 2
    assert len(await _counter_values(HarnessCounter.APP_START_REACHED_RUNNING)) == 2
    assert len(await _counter_values(HarnessCounter.APP_COLD_START_MS)) == 2


async def test_a_press_with_nothing_to_restore_still_counts_as_a_press(
    client: AsyncClient,
    db_session: AsyncSession,
    fake_redis,
    fake_storage,
    wire,
    empty_harness_counts,
) -> None:
    user, project = await _user_project(db_session, "rl-count-404@rvaiglobal.com")

    assert (await _relaunch(client, user, project, wire.manager)).status_code == 404

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

    assert (await _relaunch(client, user, project, wire.manager)).status_code == 202

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
    assert (await _relaunch(client, user, project, wire.manager)).status_code == 202
    await forget_every_harness_count()

    slow_attach_seconds = 1.0

    async def a_slow_goodbye(user_id: str):
        await asyncio.sleep(slow_attach_seconds)
        raise SandboxGoneError("took a while to be sure it is gone")

    wire.sbx.attach_existing = a_slow_goodbye

    assert (await _relaunch(client, user, project, wire.manager)).status_code == 202

    cold = await _counter_values(HarnessCounter.APP_COLD_START_MS)
    assert len(cold) == 1
    assert cold[0] < slow_attach_seconds * 1000 / 2, (
        f"{cold[0]}ms contains the {slow_attach_seconds}s attach attempt — the clock is timing "
        "the request, not the restore"
    )
