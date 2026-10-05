"""The claim inside the create seam and the fill that replaces what it takes, against ledger rows
on the real test database.

The ledger commits in sessions of its own, so exclusivity is what Postgres does, not what a double
says it does, and every test starts and ends with the table empty. Azure is a control plane keyed
by container name, and each container's supervisor is scripted per host.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import redis.asyncio as aioredis
import sqlalchemy as sa
import structlog
from pydantic import SecretStr
from redis.exceptions import ConnectionError as RedisConnectionError
from redis.exceptions import RedisError
from sqlalchemy.exc import IntegrityError
from structlog.testing import capture_logs

import src.db.base as db_base
from src.core.alarms import SANDBOX_POOL_BELOW_SIZE_EVENT
from src.core.connectors import CONNECTORS
from src.db.models.sandbox_pool import SandboxPoolMember, SandboxPoolState
from src.services.build_sessions import pool_pass
from src.services.lake.env import connector_env_names
from src.services.redis import registry_key
from src.services.redis.keys import (
    REGISTRY_FIELD_APP_ID,
    REGISTRY_FIELD_APP_NAME,
    REGISTRY_FIELD_FQDN,
    REGISTRY_FIELD_SHARED_OWNER_ID,
    REGISTRY_FIELD_SHARED_PROJECT_ID,
)
from src.services.sandbox import client as client_module
from src.services.sandbox import pool
from src.services.sandbox.aca import AcaControlPlane, AcaError
from src.services.sandbox.base import (
    KIND_BUILD_SANDBOX,
    KIND_SHARED_SANDBOX,
    TAG_CONTROL_PLANE,
    TAG_KIND,
    TAG_POOL,
    SandboxError,
    SandboxHandle,
    a_fresh_sandbox_name,
    control_plane_segment,
    identity_from_tags,
)
from src.services.sandbox.client import AcaSandboxClient
from src.services.sandbox.config import SandboxConfig
from src.services.sandbox.stopwatch import Stopwatch, running_stopwatch, timed_by
from src.services.storage import snapshot_key
from tests.fakes import FakeStorage, a_git_bundle, a_ready_pool_row

IMAGE = "acr.azurecr.io/citizen-dev-sandbox:v2"
OLD_IMAGE = "acr.azurecr.io/citizen-dev-sandbox:v1"
DSN = "postgresql://bialrole_pool:POOLROLEPASSWORD@db.example:5432/bialapp_pool"
SAS = "sv=2021-08-06&sr=c&sp=rwdl&sig=POOLSASSIGNATURE"

pytestmark = pytest.mark.usefixtures("empty_sandbox_pool")


class PoolAca(AcaControlPlane):
    """Every container Azure knows, by name: its environment, its tags, and what was done to it.
    A create made for the pool, which carries the pool flag, is kept apart from a start's own.
    `__init__` is overridden so no credential or management client is built."""

    def __init__(self) -> None:
        self.envs: dict[str, dict[str, str]] = {}
        self.tags: dict[str, dict[str, str]] = {}
        self.identities: dict[str, str | None] = {}
        self.create_attempts: list[str] = []
        self.created: list[str] = []
        self.filled: list[str] = []
        self.deleted: list[str] = []
        self.refuses_to_create = False
        self.refuses_to_delete: set[str] = set()
        self.refuses_every_delete = False
        # Set, a create for the pool waits on it: how a test holds a fill in flight.
        self.fills_wait_for: asyncio.Event | None = None
        self.fill_began = asyncio.Event()
        self.fill_takes = 0.0
        self.filling_now = 0
        self.most_filling_at_once = 0
        # Run as each create or delete reaches Azure: what a test samples meanwhile.
        self.on_each_call: Callable[[], Awaitable[object]] | None = None
        # The stopwatch and log bindings each restamp, and each fill, ran under.
        self.restamped_under: list[tuple[Stopwatch, dict[str, Any]]] = []
        self.filled_under: list[tuple[Stopwatch, dict[str, Any]]] = []
        # Set, a restamp waits on it: how a test holds one in flight.
        self.restamps_wait_for: asyncio.Event | None = None

    def made_for_the_pool(self, name: str, *, token: str | None) -> None:
        self.envs[name] = {"BIAL_POOL_MEMBER": "1"}
        if token is not None:
            self.envs[name]["SUPERVISOR_TOKEN"] = token
        self.tags[name] = {
            TAG_KIND: KIND_BUILD_SANDBOX,
            TAG_CONTROL_PLANE: control_plane_segment(),
            TAG_POOL: "1",
        }

    async def create_app(
        self,
        *,
        name: str,
        env: dict[str, str],
        tags: dict[str, str],
        identity_resource_id: str | None = None,
    ) -> str:
        if self.on_each_call is not None:
            await self.on_each_call()
        self.create_attempts.append(name)
        if self.refuses_to_create:
            raise AcaError("the create was refused")
        self.envs[name] = dict(env)
        self.tags[name] = dict(tags)
        self.identities[name] = identity_resource_id
        if env.get("BIAL_POOL_MEMBER") != "1":
            self.created.append(name)
            return f"{name}.aca.example"
        self.filled.append(name)
        self.filled_under.append((running_stopwatch(), structlog.contextvars.get_contextvars()))
        self.filling_now += 1
        self.most_filling_at_once = max(self.most_filling_at_once, self.filling_now)
        self.fill_began.set()
        try:
            await asyncio.sleep(self.fill_takes)
            if self.fills_wait_for is not None:
                await asyncio.wait_for(self.fills_wait_for.wait(), timeout=5)
        finally:
            self.filling_now -= 1
        return f"{name}.aca.example"

    async def delete_app(self, *, name: str) -> None:
        if self.on_each_call is not None:
            await self.on_each_call()
        self.deleted.append(name)
        if self.refuses_every_delete or name in self.refuses_to_delete:
            raise AcaError("the delete was refused")
        self.envs.pop(name, None)

    async def get_app_env_value(self, *, name: str, key: str) -> str | None:
        return self.envs.get(name, {}).get(key)

    async def get_app_fqdn(self, *, name: str) -> str | None:
        return f"{name}.aca.example" if name in self.envs else None

    async def stamp_tags(self, *, name: str, tags: dict[str, str]) -> None:
        self.restamped_under.append((running_stopwatch(), structlog.contextvars.get_contextvars()))
        if self.restamps_wait_for is not None:
            await asyncio.wait_for(self.restamps_wait_for.wait(), timeout=5)
        self.tags.setdefault(name, {}).update(tags)

    async def aclose(self) -> None:
        return None


class Supervisors:
    """Each container's supervisor, told apart by the host a request goes to. Like the real one,
    each takes one delivery of settings and refuses the next. Each stays silent for its first
    `silent_for` health checks, as a container Azure has only just made does."""

    def __init__(self) -> None:
        self.silent_for = 0
        self.health_checks: dict[str, int] = {}
        self.health_status: dict[str, int] = {}
        self.configure_status: dict[str, int] = {}
        self.hangs_on: set[tuple[str, str]] = set()
        self.configured: list[tuple[str, str, dict[str, str]]] = []
        # Set, health checks wait on it: how a test holds a claim open.
        self.health_waits_for: asyncio.Event | None = None
        # Run as a delivery of settings lands: what another start does meanwhile.
        self.meanwhile: Callable[[], Awaitable[object]] | None = None

    async def __call__(self, request: httpx.Request) -> httpx.Response:
        host = request.url.host
        path = request.url.path.removeprefix("/_sup")
        if (host, path) in self.hangs_on:
            raise httpx.ReadTimeout("no answer", request=request)
        configured = any(seen == host for seen, _, _ in self.configured)
        if path == "/health":
            self.health_checks[host] = self.health_checks.get(host, 0) + 1
            if self.health_checks[host] <= self.silent_for:
                raise httpx.ReadTimeout("not serving yet", request=request)
            if self.health_waits_for is not None:
                await asyncio.wait_for(self.health_waits_for.wait(), timeout=5)
            return httpx.Response(
                self.health_status.get(host, 200), json={"ok": True, "configured": configured}
            )
        if path == "/configure":
            if configured:
                return httpx.Response(409, json={"detail": "already configured"})
            status = self.configure_status.get(host, 200)
            if status == 200:
                if self.meanwhile is not None:
                    await self.meanwhile()
                env = json.loads(request.content)["env"]
                self.configured.append((host, request.headers["authorization"], env))
                return httpx.Response(200, json={"ok": True})
            return httpx.Response(status, json={"detail": "refused"})
        if path == "/exec":
            return httpx.Response(200, json={"stdout": "", "stderr": "", "exit": 0})
        if path == "/files":
            return httpx.Response(200, json={"ok": True})
        return httpx.Response(404, json={"detail": path})


def _config(**overrides: Any) -> SandboxConfig:
    return SandboxConfig.model_validate(
        {
            "subscription_id": "s",
            "resource_group": "r",
            "region": "centralindia",
            "managed_environment_name": "aca-env",
            "acr_server": "acr.azurecr.io",
            "acr_username": "acr-user",
            "acr_password": SecretStr("acr-pass"),
            "image_ref": IMAGE,
            "pool_day_size": 5,
            "pool_night_size": 5,
            **overrides,
        }
    )


def _app_env(app_id: uuid.UUID) -> dict[str, str]:
    return {
        "BIAL_APP_ID": str(app_id),
        "BIAL_PORTAL_ORIGIN": "https://portal.example",
        "BIAL_BLOB_CONTAINER_URL": "https://blob.example/app",
        "BIAL_BLOB_SAS": SAS,
        "BIAL_DATABASE_URL": DSN,
    }


@pytest.fixture
async def world(fake_redis: aioredis.Redis) -> AsyncIterator[SimpleNamespace]:
    aca = PoolAca()
    supervisors = Supervisors()
    client = AcaSandboxClient(_config(), transport=httpx.MockTransport(supervisors), aca=aca)
    yield SimpleNamespace(aca=aca, supervisors=supervisors, client=client, redis=fake_redis)
    await _settled(client)
    await client.aclose()


async def _settled(client: AcaSandboxClient) -> None:
    """Wait out the work a claim left running behind its start."""
    while client._detached:
        await asyncio.gather(*list(client._detached))


async def _ready(
    world: SimpleNamespace,
    *,
    image_ref: str = IMAGE,
    since: datetime | None = None,
    token: str | None = "pool-bearer",
) -> str:
    """A container made for the pool and its ready row. Returns its name, whose host is
    `<name>.pool.example`."""
    name = a_fresh_sandbox_name()
    world.aca.made_for_the_pool(name, token=token)
    await a_ready_pool_row(name, fqdn=f"{name}.pool.example", image_ref=image_ref, since=since)
    return name


async def _ledger() -> dict[str, SandboxPoolState]:
    async with db_base.async_session_factory() as db:
        rows = await db.execute(sa.select(SandboxPoolMember.name, SandboxPoolMember.state))
    return {name: state for name, state in rows}


async def _start(
    client: AcaSandboxClient, user_id: uuid.UUID, app_id: uuid.UUID
) -> tuple[SandboxHandle, Stopwatch]:
    stopwatch = Stopwatch()
    with timed_by(stopwatch):
        handle = await client.provision_new(
            str(user_id), a_fresh_sandbox_name(), app_env=_app_env(app_id)
        )
    return handle, stopwatch


async def _recorded_name(redis: aioredis.Redis, user_id: uuid.UUID) -> str:
    return str(await redis.hget(registry_key(user_id), REGISTRY_FIELD_APP_NAME))


async def _until(condition: Callable[[], Awaitable[bool]]) -> None:
    async with asyncio.timeout(5):
        while not await condition():
            await asyncio.sleep(0.01)


# --- the claim, end to end -------------------------------------------------------------------


async def test_a_start_takes_a_ready_container_as_its_own_workspace(world) -> None:
    """The person's workspace is the pool container: the registry records it under its own name
    and address, the handle reaches it with its own bearer, and its row is gone, because the
    registry describes it from here."""
    member = await _ready(world)
    user, app_id = uuid.uuid4(), uuid.uuid4()

    handle, stopwatch = await _start(world.client, user, app_id)

    assert handle.app_name == member
    assert handle.fqdn == f"{member}.pool.example"
    assert handle.token == "pool-bearer"
    assert handle.preview_url.endswith(f"/a/{member}")
    record = await world.redis.hgetall(registry_key(user))
    assert record[REGISTRY_FIELD_APP_NAME] == member
    assert record[REGISTRY_FIELD_APP_ID] == str(app_id)
    assert record[REGISTRY_FIELD_FQDN] == f"{member}.pool.example"
    assert world.aca.created == []
    assert member not in await _ledger()
    assert (stopwatch.claimed, stopwatch.miss_reason, stopwatch.ready_count) == (True, None, 1)
    assert {"bearer_read", "configure", "registry_write"} <= set(stopwatch.laps)
    # A claimed start's create stage is the claim.
    assert stopwatch.elapsed_ms(None, "created") is not None


async def test_a_claimed_container_is_given_exactly_the_projects_own_settings(world) -> None:
    """Over the supervisor's authenticated call, with the bearer read back from the container's
    own Azure environment. The portal origin was set when the container was made, and any name
    the supervisor does not take would refuse the whole delivery."""
    member = await _ready(world)
    url_name, client_id_name = connector_env_names(next(iter(CONNECTORS)))
    env = {
        **_app_env(uuid.uuid4()),
        url_name: "https://lake.example/data/",
        client_id_name: "lake-client-id",
        "SOMETHING_ELSE": "not for the container",
    }

    await world.client.provision_new(str(uuid.uuid4()), a_fresh_sandbox_name(), app_env=env)

    [(host, auth, delivered)] = world.supervisors.configured
    assert (host, auth) == (f"{member}.pool.example", "Bearer pool-bearer")
    assert delivered == {
        "BIAL_APP_ID": env["BIAL_APP_ID"],
        "BIAL_BLOB_CONTAINER_URL": "https://blob.example/app",
        "BIAL_BLOB_SAS": SAS,
        "BIAL_DATABASE_URL": DSN,
        url_name: "https://lake.example/data/",
        client_id_name: "lake-client-id",
    }


async def test_two_starts_at_once_never_receive_the_same_ready_container(
    world, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One ready container, two people pressing at once: one takes it, the other creates as
    today and records that nothing was ready. The second asks the ledger while the first is
    still taking over its container, which is where a claim spends its seconds; two statements
    landing together are the row-lock test's."""
    member = await _ready(world)
    first, second = uuid.uuid4(), uuid.uuid4()
    first_claimed, both_asked = asyncio.Event(), asyncio.Event()
    world.supervisors.health_waits_for = both_asked
    asked = 0
    real_claim = pool.claim

    async def counted_claim(image_ref: str) -> pool.ClaimedMember | None:
        nonlocal asked
        claimed = await real_claim(image_ref)
        asked += 1
        (first_claimed if asked == 1 else both_asked).set()
        return claimed

    monkeypatch.setattr(pool, "claim", counted_claim)

    pressed_first = asyncio.create_task(_start(world.client, first, uuid.uuid4()))
    await asyncio.wait_for(first_claimed.wait(), timeout=5)
    (one, one_watch), (two, two_watch) = await asyncio.gather(
        pressed_first, _start(world.client, second, uuid.uuid4())
    )
    await _settled(world.client)

    assert (one.app_name, one_watch.claimed) == (member, True)
    assert (two_watch.claimed, two_watch.miss_reason) == (False, "no_ready")
    assert world.aca.created == [two.app_name]
    assert world.aca.deleted == []
    assert len(world.supervisors.configured) == 1
    assert await _recorded_name(world.redis, first) == member
    assert await _recorded_name(world.redis, second) == two.app_name


async def test_six_starts_against_five_ready_containers_claim_five_and_create_one(world) -> None:
    """The replacements each claim starts are held filling, so the sixth start finds none ready."""
    members = {await _ready(world) for _ in range(5)}
    world.aca.fills_wait_for = asyncio.Event()

    started = [await _start(world.client, uuid.uuid4(), uuid.uuid4()) for _ in range(6)]
    world.aca.fills_wait_for.set()

    assert {handle.app_name for handle, _ in started[:5]} == members
    assert [(w.claimed, w.ready_count) for _, w in started[:5]] == [
        (True, 5),
        (True, 4),
        (True, 3),
        (True, 2),
        (True, 1),
    ]
    last_handle, last = started[5]
    assert (last.claimed, last.miss_reason, last.ready_count) == (False, "no_ready", 0)
    assert world.aca.created == [last_handle.app_name]


async def test_a_size_of_zero_creates_without_asking_the_ledger(world) -> None:
    """No day counts as daytime here, so the night size of zero applies at any instant. A ready
    row left over is not claimed: a size of zero is how the pool is switched off."""
    world.client._config = _config(pool_night_size=0, pool_day_days=frozenset())
    member = await _ready(world)

    handle, stopwatch = await _start(world.client, uuid.uuid4(), uuid.uuid4())

    assert (stopwatch.claimed, stopwatch.miss_reason, stopwatch.ready_count) == (
        False,
        "size_zero",
        0,
    )
    assert world.aca.created == [handle.app_name]
    assert await _ledger() == {member: SandboxPoolState.READY}


async def test_a_shared_view_claims_and_is_restamped_with_the_viewer_as_owner(
    world, fake_storage: FakeStorage
) -> None:
    """The registry carries the shared-view stamp from the claim, and the restamp behind it gives
    the container the shared kind, the colleague as owner and the owner's app, beside the pool
    tag it was made with."""
    member = await _ready(world)
    viewer, owner, project_id, app_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    await fake_storage.put(snapshot_key(app_id), a_git_bundle())

    handle = await world.client.restore_from_snapshot(
        str(viewer),
        a_fresh_sandbox_name(),
        app_env=_app_env(app_id),
        kind="shared_sandbox",
        shared_project_id=project_id,
        shared_owner_id=owner,
    )
    await _settled(world.client)

    assert handle.app_name == member
    record = await world.redis.hgetall(registry_key(viewer))
    assert record[REGISTRY_FIELD_SHARED_PROJECT_ID] == str(project_id)
    assert record[REGISTRY_FIELD_SHARED_OWNER_ID] == str(owner)
    assert record[REGISTRY_FIELD_APP_ID] == str(app_id)
    identity = identity_from_tags(world.aca.tags[member])
    assert (identity.kind, identity.user_id, identity.app_id) == (
        KIND_SHARED_SANDBOX,
        viewer,
        app_id,
    )
    assert identity.created_at is not None
    assert world.aca.tags[member][TAG_POOL] == "1"


async def test_a_build_claim_is_restamped_with_its_owner_and_birth(world) -> None:
    member = await _ready(world)
    user, app_id = uuid.uuid4(), uuid.uuid4()
    before = datetime.now(UTC)

    await _start(world.client, user, app_id)
    await _settled(world.client)

    identity = identity_from_tags(world.aca.tags[member])
    assert (identity.kind, identity.user_id, identity.app_id) == (KIND_BUILD_SANDBOX, user, app_id)
    assert identity.created_at is not None and identity.created_at >= before


async def test_work_a_claim_leaves_behind_runs_outside_the_start(world) -> None:
    """The restamp and the replacement outlive the start, whose record is closed by then: they
    must see neither the start's stopwatch nor its log bindings, or they would time themselves
    into a closed record."""
    await _ready(world)
    stopwatch = Stopwatch()

    with structlog.contextvars.bound_contextvars(build_id="the-start"), timed_by(stopwatch):
        await world.client.provision_new(
            str(uuid.uuid4()), a_fresh_sandbox_name(), app_env=_app_env(uuid.uuid4())
        )
    laps_at_the_return = dict(stopwatch.laps)
    await _settled(world.client)

    [(restamp_timed_by, restamp_bound)] = world.aca.restamped_under
    [(fill_timed_by, fill_bound)] = world.aca.filled_under
    assert restamp_timed_by is fill_timed_by is running_stopwatch()
    assert restamp_timed_by is not stopwatch
    assert "build_id" not in restamp_bound
    assert "build_id" not in fill_bound
    assert stopwatch.laps == laps_at_the_return


# --- a claim that fails ----------------------------------------------------------------------


def _bearer_unreadable(world: SimpleNamespace, name: str) -> None:
    world.aca.envs[name].pop("SUPERVISOR_TOKEN")


def _health_refused(world: SimpleNamespace, name: str) -> None:
    world.supervisors.health_status[f"{name}.pool.example"] = 503


def _health_hangs(world: SimpleNamespace, name: str) -> None:
    world.supervisors.hangs_on.add((f"{name}.pool.example", "/health"))


def _configure_refused(world: SimpleNamespace, name: str) -> None:
    world.supervisors.configure_status[f"{name}.pool.example"] = 500


def _configure_hangs(world: SimpleNamespace, name: str) -> None:
    world.supervisors.hangs_on.add((f"{name}.pool.example", "/configure"))


_FAILURES = [
    pytest.param(_bearer_unreadable, "claim_failed", id="bearer-unreadable"),
    pytest.param(_health_refused, "unhealthy", id="health-refused"),
    pytest.param(_health_hangs, "unhealthy", id="health-hangs"),
    pytest.param(_configure_refused, "claim_failed", id="configure-500"),
    pytest.param(_configure_hangs, "claim_failed", id="configure-hangs"),
]


@pytest.mark.parametrize(("break_it", "reason"), _FAILURES)
async def test_a_failed_claim_is_deleted_and_the_start_creates_recording_why(
    world, break_it, reason: str
) -> None:
    member = await _ready(world)
    break_it(world, member)
    user = uuid.uuid4()

    handle, stopwatch = await _start(world.client, user, uuid.uuid4())
    await _settled(world.client)

    assert (stopwatch.claimed, stopwatch.miss_reason, stopwatch.ready_count) == (
        False,
        reason,
        1,
    )
    assert world.aca.created == [handle.app_name]
    assert await _recorded_name(world.redis, user) == handle.app_name
    assert world.aca.deleted == [member]
    assert await _ledger() == {}


@pytest.mark.parametrize(("break_it", "reason"), _FAILURES)
async def test_a_failed_claim_moves_on_to_the_next_ready_container(
    world, break_it, reason: str
) -> None:
    an_hour_ago = datetime.now(UTC) - timedelta(hours=1)
    broken = await _ready(world, since=an_hour_ago)
    sound = await _ready(world)
    break_it(world, broken)

    handle, stopwatch = await _start(world.client, uuid.uuid4(), uuid.uuid4())
    await _settled(world.client)

    assert handle.app_name == sound
    assert (stopwatch.claimed, stopwatch.miss_reason) == (True, None)
    assert world.aca.created == []
    assert world.aca.deleted == [broken]
    [replacement] = world.aca.filled
    assert await _ledger() == {replacement: SandboxPoolState.READY}


async def test_a_start_lets_two_failed_claims_go_and_then_creates_its_own(world) -> None:
    """A run of broken ready containers costs a start two claims, not the whole pool: the third
    stays ready for a later start, and this one records the last reason it was given.

    Mutation check: let the claim loop run until the ledger is empty and the third is spent."""
    an_hour_ago = datetime.now(UTC) - timedelta(hours=1)
    first = await _ready(world, since=an_hour_ago)
    second = await _ready(world, since=an_hour_ago + timedelta(minutes=1))
    third = await _ready(world, since=an_hour_ago + timedelta(minutes=2))
    _configure_refused(world, first)
    _health_refused(world, second)
    _health_refused(world, third)

    handle, stopwatch = await _start(world.client, uuid.uuid4(), uuid.uuid4())
    await _settled(world.client)

    assert (stopwatch.claimed, stopwatch.miss_reason, stopwatch.ready_count) == (
        False,
        "unhealthy",
        3,
    )
    assert world.aca.created == [handle.app_name]
    assert world.aca.deleted == [first, second]
    assert (await _ledger())[third] is SandboxPoolState.READY


async def test_a_failed_claim_whose_delete_is_refused_is_left_retiring(world) -> None:
    member = await _ready(world)
    _health_refused(world, member)
    world.aca.refuses_to_delete.add(member)

    await _start(world.client, uuid.uuid4(), uuid.uuid4())
    await _settled(world.client)

    assert await _ledger() == {member: SandboxPoolState.RETIRING}


async def test_a_refused_configure_logs_none_of_what_it_carried(world) -> None:
    member = await _ready(world)
    _configure_refused(world, member)

    with capture_logs() as logged:
        await _start(world.client, uuid.uuid4(), uuid.uuid4())
        await _settled(world.client)

    assert logged, "the failed claim must be on the record"
    for secret in ("POOLROLEPASSWORD", "POOLSASSIGNATURE", "pool-bearer"):
        assert all(secret not in repr(entry) for entry in logged)


async def test_a_claim_whose_slot_was_taken_meanwhile_fails_the_start_and_spends_no_more(
    world,
) -> None:
    """The provision's guard is a read, and a claim spends seconds before its registry write:
    another start may take the person's slot meanwhile. The claim's write then lands nothing,
    its container is let go, and the start fails as a create would on a taken slot, spending no
    further ready container and creating none.

    Mutation check: fall through to another claim on a taken slot and the spare is spent."""
    member = await _ready(world, since=datetime.now(UTC) - timedelta(hours=1))
    spare = await _ready(world)
    user = uuid.uuid4()
    elsewhere = a_fresh_sandbox_name()

    async def another_start_takes_the_slot() -> None:
        await world.redis.hset(registry_key(user), mapping={REGISTRY_FIELD_APP_NAME: elsewhere})

    world.supervisors.meanwhile = another_start_takes_the_slot

    with pytest.raises(SandboxError):
        await _start(world.client, user, uuid.uuid4())
    await _settled(world.client)

    assert await _recorded_name(world.redis, user) == elsewhere
    assert world.aca.deleted == [member]
    assert world.aca.created == []
    assert await _ledger() == {spare: SandboxPoolState.READY}


async def test_a_claim_whose_registry_write_fails_fails_the_start_and_lets_it_go(
    world, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A registry that does not answer would fail a create's write the same way, so the start
    fails rather than spending another ready container or creating one."""
    member = await _ready(world)

    async def refusing(user_uuid: uuid.UUID, **fields: Any) -> None:
        raise RedisConnectionError("the coordination store went away")

    monkeypatch.setattr(world.client, "_write_registry", refusing)

    with pytest.raises(RedisError):
        await _start(world.client, uuid.uuid4(), uuid.uuid4())
    await _settled(world.client)

    assert world.aca.deleted == [member]
    assert world.aca.created == []
    assert await _ledger() == {}


# --- the ledger statements -------------------------------------------------------------------


async def test_a_claim_prefers_the_current_image_and_falls_back_to_an_older_one(world) -> None:
    """An older-image container can still serve a start until its replacement is ready, so it is
    claimed only when no current one is ready, however long it has waited."""
    an_hour_ago = datetime.now(UTC) - timedelta(hours=1)
    old = await _ready(world, image_ref=OLD_IMAGE, since=an_hour_ago)
    current = await _ready(world)

    first = await pool.claim(IMAGE)
    second = await pool.claim(IMAGE)

    assert first is not None and second is not None
    assert (first.name, second.name) == (current, old)
    assert await pool.claim(IMAGE) is None


async def test_a_claim_skips_a_row_another_claim_holds_rather_than_waiting_for_it(world) -> None:
    """The row lock another claim holds until it commits is skipped, not waited on: a start is
    never queued behind someone else's claim, and never handed the row it holds."""
    held = await _ready(world, since=datetime.now(UTC) - timedelta(hours=1))
    free = await _ready(world)
    async with db_base.async_session_factory() as other_claim:
        await other_claim.execute(
            sa.select(SandboxPoolMember.id).where(SandboxPoolMember.name == held).with_for_update()
        )

        mine = await asyncio.wait_for(pool.claim(IMAGE), timeout=5)
        nothing_left = await asyncio.wait_for(pool.claim(IMAGE), timeout=5)

        await other_claim.rollback()
    assert mine is not None and mine.name == free
    assert nothing_left is None
    assert await _ledger() == {held: SandboxPoolState.READY, free: SandboxPoolState.CLAIMED}


@pytest.mark.parametrize(
    "name", ["pub-" + "a" * 28, "sbx-" + "A" * 28, "sbx-" + "a" * 27, "shr-" + "a" * 28]
)
async def test_the_ledger_holds_only_names_this_platform_mints(name: str) -> None:
    """Every path that deletes a container refuses a name of any other shape, so a pool
    container under one could never be cleaned up."""
    with pytest.raises(IntegrityError):
        await a_ready_pool_row(name, fqdn="x.example", image_ref=IMAGE)


async def test_the_ready_count_counts_only_ready_rows(world) -> None:
    await _ready(world)
    await _ready(world)
    claimed = await pool.claim(IMAGE)
    assert claimed is not None

    assert await pool.ready_count() == 1


async def test_retiring_touches_only_a_claimed_row(world) -> None:
    """A retire is how a failed claim's row leaves the pool, so it must never take a ready one."""
    first = await _ready(world, since=datetime.now(UTC) - timedelta(hours=1))
    second = await _ready(world)
    claimed = await pool.claim(IMAGE)
    assert claimed is not None and claimed.name == first
    async with db_base.async_session_factory() as db:
        second_id = await db.scalar(
            sa.select(SandboxPoolMember.id).where(SandboxPoolMember.name == second)
        )
    assert second_id is not None

    await pool.retire(claimed.id, was=SandboxPoolState.CLAIMED)
    await pool.retire(second_id, was=SandboxPoolState.CLAIMED)

    assert await _ledger() == {
        first: SandboxPoolState.RETIRING,
        second: SandboxPoolState.READY,
    }


# --- making ready containers -----------------------------------------------------------------


async def test_a_fill_makes_a_container_that_holds_nothing_of_any_project(world) -> None:
    """Made from its own name with the platform's settings and nothing else: no project's
    settings, no data identity, and no owner, app or birth on its tags until a claim."""
    assert await world.client.fill_one(5) == "filled"

    [name] = world.aca.filled
    env = world.aca.envs[name]
    assert set(env) == {
        "SUPERVISOR_TOKEN",
        "BIAL_BASE_PATH",
        "BIAL_APPS_HOSTNAME",
        "BIAL_PORTAL_ORIGIN",
        "BIAL_POOL_MEMBER",
    }
    assert env["BIAL_BASE_PATH"] == f"/a/{name}"
    assert env["BIAL_APPS_HOSTNAME"] == "citizenapps.bialairport.com"
    assert env["BIAL_PORTAL_ORIGIN"] == "http://localhost:5173"
    assert env["BIAL_POOL_MEMBER"] == "1"
    assert len(env["SUPERVISOR_TOKEN"]) >= 43
    assert world.aca.tags[name] == {
        TAG_KIND: KIND_BUILD_SANDBOX,
        TAG_CONTROL_PLANE: control_plane_segment(),
        TAG_POOL: "1",
    }
    assert world.aca.identities[name] is None
    async with db_base.async_session_factory() as db:
        row = await db.scalar(sa.select(SandboxPoolMember).where(SandboxPoolMember.name == name))
    assert row is not None
    assert (row.state, row.fqdn, row.image_ref) == (
        SandboxPoolState.READY,
        f"{name}.aca.example",
        IMAGE,
    )


async def test_each_fill_gets_a_bearer_of_its_own(world) -> None:
    await world.client.fill_one(5)
    await world.client.fill_one(5)

    first, second = world.aca.filled
    assert world.aca.envs[first]["SUPERVISOR_TOKEN"] != world.aca.envs[second]["SUPERVISOR_TOKEN"]


async def test_a_fill_is_on_the_ledger_before_azure_is_asked(world) -> None:
    """So the refill and the worker's pass each count the other's create in flight."""
    world.aca.fills_wait_for = asyncio.Event()
    filling = asyncio.create_task(world.client.fill_one(5))
    await asyncio.wait_for(world.aca.fill_began.wait(), timeout=5)

    [name] = world.aca.filled
    assert await _ledger() == {name: SandboxPoolState.FILLING}

    world.aca.fills_wait_for.set()
    assert await filling == "filled"
    assert await _ledger() == {name: SandboxPoolState.READY}


async def test_a_create_azure_refuses_leaves_no_row_and_nothing_standing(world) -> None:
    world.aca.refuses_to_create = True

    assert await world.client.fill_one(5) == "refused"

    [attempted] = world.aca.create_attempts
    assert world.aca.deleted == [attempted]
    assert await _ledger() == {}


async def test_a_refused_create_whose_clean_up_is_refused_too_is_left_retiring(world) -> None:
    """Something may stand under that name, so its row stays for a pass to retry the delete."""
    world.aca.refuses_to_create = True
    world.aca.refuses_every_delete = True

    assert await world.client.fill_one(5) == "refused"

    [attempted] = world.aca.create_attempts
    assert await _ledger() == {attempted: SandboxPoolState.RETIRING}


async def test_a_fill_whose_row_a_pass_let_go_deletes_what_it_made(world) -> None:
    """A pass that judged the create overdue has given up its row, so nothing would hold the
    container the create goes on to make."""
    world.aca.fills_wait_for = asyncio.Event()
    filling = asyncio.create_task(world.client.fill_one(5))
    await asyncio.wait_for(world.aca.fill_began.wait(), timeout=5)
    [name] = world.aca.filled
    async with db_base.async_session_factory() as db:
        member_id = await db.scalar(
            sa.select(SandboxPoolMember.id).where(SandboxPoolMember.name == name)
        )
    assert member_id is not None
    assert await pool.retire(member_id, was=SandboxPoolState.FILLING) is True

    world.aca.fills_wait_for.set()

    assert await filling == "refused"
    assert world.aca.deleted == [name]
    assert await _ledger() == {name: SandboxPoolState.RETIRING}


async def test_one_claim_makes_one_replacement_that_a_pass_meanwhile_counts(world) -> None:
    """The replacement is on the ledger while Azure makes it, so the worker's pass in that
    minute finds the pool at its size and makes nothing of its own."""
    world.client._config = _config(pool_day_size=1, pool_night_size=1)
    member = await _ready(world)
    world.aca.fills_wait_for = asyncio.Event()

    handle, _ = await _start(world.client, uuid.uuid4(), uuid.uuid4())
    await asyncio.wait_for(world.aca.fill_began.wait(), timeout=5)
    meanwhile = await pool_pass.keep_the_pool(world.client, at=datetime.now(UTC))
    world.aca.fills_wait_for.set()
    await _settled(world.client)

    assert handle.app_name == member
    assert meanwhile.filled == 0
    [replacement] = world.aca.filled
    assert await _ledger() == {replacement: SandboxPoolState.READY}


async def test_a_burst_of_claims_makes_its_replacements_two_at_a_time(world) -> None:
    """Six claims at a size of five: five replacements, never more than two made at once."""
    for _ in range(6):
        await _ready(world)
    world.aca.fill_takes = 0.05

    started = await asyncio.gather(
        *(_start(world.client, uuid.uuid4(), uuid.uuid4()) for _ in range(6))
    )
    await _settled(world.client)

    assert all(stopwatch.claimed for _, stopwatch in started)
    assert len(world.aca.filled) == 5
    assert world.aca.most_filling_at_once == 2


async def test_a_lost_replacement_does_not_fail_the_start_that_claimed(
    world, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The ledger going away after a claim costs the pool one replacement, which the worker's
    next pass makes up, and nothing else."""
    member = await _ready(world)

    async def unreachable(name: str, image_ref: str, *, up_to: int) -> uuid.UUID:
        raise OSError("the database went away")

    monkeypatch.setattr(pool, "add_filling", unreachable)

    with capture_logs() as logged:
        handle, stopwatch = await _start(world.client, uuid.uuid4(), uuid.uuid4())
        await _settled(world.client)

    assert (handle.app_name, stopwatch.claimed) == (member, True)
    assert world.aca.filled == []
    assert [e["event"] for e in logged if e["event"] == "sandbox_pool_refill_failed"] == [
        "sandbox_pool_refill_failed"
    ]


async def test_a_fill_is_ready_only_once_its_supervisor_answers(
    world, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ Azure reports a create done up to a minute and a half before the supervisor inside it
    serves. A claim in that gap would find a sound container silent and let it go, so the fill
    holds its row filling until the container answers.

    Mutation check: mark the row ready as the create returns and it is ready while silent."""
    world.supervisors.silent_for = 3
    while_it_was_silent: list[dict[str, SandboxPoolState]] = []

    async def asked_again(seconds: float) -> None:
        while_it_was_silent.append(await _ledger())

    monkeypatch.setattr(client_module, "_asleep", asked_again)

    assert await world.client.fill_one(5) == "filled"

    [name] = world.aca.filled
    assert world.supervisors.health_checks[f"{name}.aca.example"] == 4
    assert while_it_was_silent == [{name: SandboxPoolState.FILLING}] * 3
    assert await _ledger() == {name: SandboxPoolState.READY}


async def test_a_container_that_never_answers_is_let_go_and_the_pass_counts_it_refused(
    world, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Never claimable, so it is deleted like a create Azure refused, and the pass stops filling
    and raises the alarm as it would for one.

    Mutation check: mark a silent container ready and the pass counts it filled."""
    world.supervisors.silent_for = 1_000
    monkeypatch.setattr(client_module, "FIRST_ANSWER_CEILING", timedelta(0))
    world.client._config = _config(pool_day_size=1, pool_night_size=1)

    with capture_logs() as logged:
        outcome = await pool_pass.keep_the_pool(world.client, at=datetime.now(UTC))

    assert (outcome.filled, outcome.refused) == (0, True)
    [name] = world.aca.filled
    assert world.aca.deleted == [name]
    assert await _ledger() == {}
    assert [e["event"] for e in logged if e["event"] == SANDBOX_POOL_BELOW_SIZE_EVENT] == [
        SANDBOX_POOL_BELOW_SIZE_EVENT
    ]


async def test_a_claims_replacement_is_on_the_ledger_while_its_restamp_is_in_flight(
    world,
) -> None:
    """★ The restamp spends seconds on ARM, and a replacement queued behind it would be invisible
    to a pass in those seconds, which would make one of its own.

    Mutation check: refill only once the restamp is done and no row appears while it hangs."""
    member = await _ready(world)
    world.aca.restamps_wait_for = asyncio.Event()

    await _start(world.client, uuid.uuid4(), uuid.uuid4())

    async def a_replacement_is_on_the_ledger() -> bool:
        return set(await _ledger()) - {member} != set()

    await _until(a_replacement_is_on_the_ledger)
    world.aca.restamps_wait_for.set()
    await _settled(world.client)


async def test_a_fill_waiting_for_the_bound_is_already_on_the_ledger(world) -> None:
    """So a pass, or another refill, counts a create still queued behind the two in flight.

    Mutation check: write the row once the bound is held and the queued fill is not counted."""
    world.aca.fills_wait_for = asyncio.Event()
    fills = [asyncio.create_task(world.client.fill_one(5)) for _ in range(3)]

    async def two_in_flight_and_three_rows() -> bool:
        return world.aca.filling_now == 2 and len(await _ledger()) == 3

    await _until(two_in_flight_and_three_rows)
    assert set((await _ledger()).values()) == {SandboxPoolState.FILLING}
    world.aca.fills_wait_for.set()

    assert await asyncio.gather(*fills) == ["filled", "filled", "filled"]


async def test_a_rush_of_claims_and_a_pass_among_their_refills_make_the_pool_whole_once(
    world,
) -> None:
    """★ Five starts take all five ready containers at once, and their refills queue behind the
    bound while the worker's pass runs. Each fill is written only while the pool is short, so
    between them the refills and the pass make five, and nothing is made only to be retired.

    Mutation check: write a fill's row whatever the pool holds and eight are made."""
    for _ in range(5):
        await _ready(world)
    world.aca.fills_wait_for = asyncio.Event()

    started = await asyncio.gather(
        *(_start(world.client, uuid.uuid4(), uuid.uuid4()) for _ in range(5))
    )

    async def two_refills_in_flight() -> bool:
        return world.aca.filling_now == 2

    await _until(two_refills_in_flight)
    meanwhile = asyncio.create_task(pool_pass.keep_the_pool(world.client, at=datetime.now(UTC)))
    await asyncio.sleep(0.05)
    world.aca.fills_wait_for.set()
    await meanwhile
    await _settled(world.client)

    assert all(stopwatch.claimed for _, stopwatch in started)
    assert len(world.aca.filled) == 5
    assert world.aca.deleted == []
    assert list((await _ledger()).values()) == [SandboxPoolState.READY] * 5


async def test_a_claim_after_the_size_drops_makes_no_replacement(world) -> None:
    """At the evening's smaller size the containers still ready are enough, so the claim's refill
    makes nothing for the next pass to retire.

    Mutation check: refill whatever the pool holds and one container is made."""
    for _ in range(3):
        await _ready(world)
    world.client._config = _config(pool_day_size=1, pool_night_size=1)

    _, stopwatch = await _start(world.client, uuid.uuid4(), uuid.uuid4())
    await _settled(world.client)

    assert stopwatch.claimed is True
    assert world.aca.filled == []
    assert list((await _ledger()).values()) == [SandboxPoolState.READY] * 2


async def test_a_fill_cut_short_is_retired_for_the_next_pass_to_delete(world) -> None:
    """★ A backend or worker stopping mid-create cancels the fill, and Azure may finish the
    container anyway. Its row goes retiring at once, so the next pass deletes the container
    instead of the row being counted, and holding back every fill, until its deadline.

    Mutation check: retire nothing on a cancellation and the row stays filling."""
    world.aca.fills_wait_for = asyncio.Event()
    filling = asyncio.create_task(world.client.fill_one(5))
    await asyncio.wait_for(world.aca.fill_began.wait(), timeout=5)
    [name] = world.aca.filled

    filling.cancel()
    with pytest.raises(asyncio.CancelledError):
        await filling

    assert await _ledger() == {name: SandboxPoolState.RETIRING}
    world.client._config = _config(pool_day_size=0, pool_night_size=0)
    after = await pool_pass.keep_the_pool(world.client, at=datetime.now(UTC))
    assert after.deleted == 1
    assert world.aca.deleted == [name]
    assert await _ledger() == {}


async def test_a_retire_takes_only_a_row_still_in_the_state_it_was_judged_in(world) -> None:
    """Every retire is a compare-and-set, which is what keeps one from deleting a container a
    start claimed after the pass looked."""
    member = await _ready(world)
    claimed = await pool.claim(IMAGE)
    assert claimed is not None and claimed.name == member

    assert await pool.retire(claimed.id, was=SandboxPoolState.READY) is False
    assert await _ledger() == {member: SandboxPoolState.CLAIMED}
    assert await pool.retire(claimed.id, was=SandboxPoolState.CLAIMED) is True
    assert await _ledger() == {member: SandboxPoolState.RETIRING}


def test_a_row_is_overdue_only_after_the_longest_create_could_have_run() -> None:
    """Every create attempt waits at most five minutes for Azure, and a create is tried four
    times; a pool member then has five minutes to answer. A row younger than that may still have
    its create, or its first answer, in flight."""
    assert pool_pass.ROW_DEADLINE > timedelta(minutes=25)
    assert pool_pass.ROW_DEADLINE < timedelta(minutes=30)
