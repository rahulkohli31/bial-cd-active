"""The SessionManager lifecycle: allocation (provision/attach/restore), the progress channel, the
single-owner end sequence, stop/force-end, and allocation compensation. Driven by
FakeSandboxClient + fakeredis + fake storage + the `:5432` test DB.

HOW A LIVE SESSION IS CONJURED HERE, because it used to be one call and is now two.
`SessionManager.start` was deleted with the build-start route, and with it went the `run_build`
task, the in-process build feed, and the `FakeBrain` that drove them. What is left is the pair
production uses: `ensure_sandbox` allocates (the identical skeleton — slot claim, reconcile,
lock, app row, env, resolve, heartbeat, adopt), `on_progress` is the same sink the feed always
went through, and `stop` / `force_end` run the same end sequence. So a test that needed "a live
session with a step buffered" pushes the step into `on_progress` itself, and a test that needed
"a build that ended" calls `stop` with the reason that ending carried — `stop`'s `reason` is a
real parameter, and `_do_finalize` branches on nothing else (`completed` pardons, everything else
tears down).
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import redis.asyncio as aioredis
import sqlalchemy as sa
from pydantic import SecretStr
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.v1.build_sessions.schemas import (
    RELAUNCH_PREVIEW_STAY_SECONDS,
    BuildSessionStatus,
    EndedEvent,
    PreviewReadyEvent,
    PreviewReconnectingEvent,
    ProgressEnvelope,
    QuotaExceededEvent,
    StepEvent,
)
from src.config import settings
from src.core.errors import AppApiError
from src.db.models.app_registry import AppRegistry, AppStatus
from src.db.models.conversation import ChatKind
from src.db.models.user import User
from src.services.build_sessions.appdata import (
    APP_SWITCHED_OFF_CODE,
    build_app_env,
    resolve_app_for_project,
)
from src.services.build_sessions.locks import (
    LockUnavailableError,
    heartbeat_is_alive,
    lock_is_held,
    read_registry,
    stay_of_execution_is_current,
    write_heartbeat,
)
from src.services.build_sessions.manager import (
    _ENDED_RETENTION_SECONDS,
    _HEAD_ATTEMPTS,
    _RESTORE_ATTEMPTS,
    BuildSession,
    BuildSessionConflictError,
    NoSnapshotToRelaunchError,
    SessionManager,
    SnapshotUnavailableError,
    app_name_for,
)
from src.services.build_sessions.outcome import write_build_outcome
from src.services.build_sessions.reaper import sweep_all
from src.services.build_sessions.snapshot import Destination, write_snapshot
from src.services.redis import (
    REGISTRY_STATE_ENDING,
    REGISTRY_STATE_READY,
    lock_key,
    registry_key,
)
from src.services.redis.keys import (
    REGISTRY_FIELD_APP_NAME,
    REGISTRY_FIELD_CREATED_AT,
    REGISTRY_FIELD_FQDN,
    REGISTRY_FIELD_PREVIEW_STAY_UNTIL,
    REGISTRY_FIELD_STATE,
    REGISTRY_FIELD_TOKEN_REF,
)
from src.services.sandbox import (
    ExecResult,
    SandboxClient,
    SandboxError,
    SandboxHandle,
    SandboxNotReadyError,
)
from src.services.sandbox.config import SandboxConfig
from src.services.storage import (
    StorageAuthError,
    StorageError,
    StorageNotFoundError,
    snapshot_key,
)
from tests.factories import ConversationFactory, ProjectFactory, UserFactory
from tests.fakes import FakeSandboxClient, FakeStorage, a_sandbox_name


@pytest.fixture(autouse=True)
def _sandbox_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        settings,
        "sandbox",
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
    )


# The end reasons `_do_finalize` branches on, spelled out because the manager's own constants
# are private. `COMPLETED` is the ONLY one that earns the pardon; every other reason takes
# the teardown arm, and `BUILD_FAILED` is additionally the only one `_terminal_status` derives
# a FAILED status from.
COMPLETED = "completed"
BUILD_FAILED = "build_failed"

A_STEP = StepEvent(seq=1, name="scaffold", label="Scaffolding", state="started")


async def _ends_with(
    manager: SessionManager, session: BuildSession, client: SandboxClient, reason: str
) -> None:
    """Run the end sequence the way a RUN'S OWN ENDING ran it: `_finalize`, with the reason the
    run carried.

    NOT `stop`, and the difference is observable rather than stylistic. `stop` goes through
    `_end`, which marks the registry `ending` before it finalizes — correct for a user-driven
    stop, and something a natural ending never did: the deleted `_run_and_finalize` called
    `_finalize` directly with the run's verdict and nothing touched the registry state. Ending a
    COMPLETED session through `stop` therefore leaves a pardoned container sitting behind an
    `ending` registry, a pair production has never produced, and the next allocation refuses to
    attach to it (`_the_live_sandbox_is_already_the_one_we_want` requires READY). Tests that go
    on to allocate again would then assert a teardown-and-restore the code does not really do.

    So: this is the completion door, `manager.stop` / `manager.force_end` are the user's doors,
    and each test uses the one whose behaviour it is about."""
    await manager._finalize(session, reason, client)


async def _mk(db: AsyncSession, email: str) -> tuple[User, uuid.UUID]:
    user = await UserFactory.create(db, email=email)
    project = await ProjectFactory.create(db, user.id)
    return user, project.id


async def _seed_live_sandbox_state(redis: aioredis.Redis, user_id: uuid.UUID) -> None:
    """A dead session's LINGERING Redis facade: registry + lock + heartbeat, all still
    inside their TTLs. The reconcile once spared this conjunction and start 409ed
    on a phantom; now start's certified-dead reconcile reaps straight through it (there is
    no in-process session, and one replica means nobody else could own it). The sweep still
    spares exactly this state — see test_reaper.py's certified-dead section."""
    await redis.hset(
        registry_key(user_id),
        mapping={
            REGISTRY_FIELD_APP_NAME: a_sandbox_name("someone-elses"),
            REGISTRY_FIELD_FQDN: "live.example",
            REGISTRY_FIELD_TOKEN_REF: "ref",
            REGISTRY_FIELD_CREATED_AT: datetime.now(UTC).isoformat(),
            REGISTRY_FIELD_STATE: REGISTRY_STATE_READY,
        },
    )
    await redis.set(lock_key(user_id), "another-processes-token", ex=900)
    await write_heartbeat(redis, user_id)


async def test_finalize_runs_the_liveness_detector_while_the_container_is_up(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    # The idle detector hooks the end sequence: its workspace collect must run at
    # finalize, BEFORE teardown — the only moment the workspace still exists to scan.
    user, project_id = await _mk(db_session, "m40@rvaiglobal.com")
    manager = SessionManager()
    client = FakeSandboxClient()
    cmds: list[list[str]] = []

    def record(cmd: list[str]) -> ExecResult:
        cmds.append(cmd)
        return ExecResult(stdout="", stderr="", exit=0)

    client.exec_handler = record
    session = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=client, may_write=True
    )
    await _ends_with(manager, session, client, COMPLETED)

    # The collect script (find over *.tsx/*.jsx/…) ran through the sandbox exec seam.
    assert any("*.tsx" in part for cmd in cmds for part in cmd)


# The one-per-user rehydrate resolution (`_resolve_sandbox`): through `ensure_sandbox` on a
# single replica the reconcile-then-acquire gate means attach/restore aren't reached (a live
# registry is either reaped as stale → provision, or 409s on a held lock), so the attach
# and restore branches are exercised directly here. Fresh-provision + reap-then-provision
# ARE reachable through the allocator and covered below.
#
# THE SLOT CONFLICT ITSELF used to be pinned here twice — a second start refused while one was
# live, and its mirror in `test_write_turn_sandbox.py`. `_claim_the_one_build_slot` had exactly
# two callers and one of them (`_start_locked`) is deleted, so the two tests collapsed into
# one: `test_write_turn_sandbox.py::test_a_second_write_attach_while_one_is_live_is_a_conflict`
# is now the whole of that claim, and it carries the same 409-with-the-live-session-id
# assertion this one did.


async def test_resolve_sandbox_attaches_when_registry_is_live(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    user, project_id = await _mk(db_session, "m3@rvaiglobal.com")
    manager = SessionManager()
    client = FakeSandboxClient()
    app_id = await resolve_app_for_project(db_session, user.id, project_id)
    await db_session.commit()
    client.attach_handle = SandboxHandle(
        fqdn="existing.example",
        token="tok",
        app_name=app_name_for(app_id),
        preview_url="https://existing.example/",
        ready=False,
    )
    await fake_redis.hset(
        registry_key(user.id),
        mapping={
            REGISTRY_FIELD_APP_NAME: app_name_for(app_id),
            REGISTRY_FIELD_FQDN: "existing.example",
            REGISTRY_FIELD_TOKEN_REF: "ref",
            REGISTRY_FIELD_CREATED_AT: "2026-07-14T00:00:00+00:00",
            REGISTRY_FIELD_STATE: REGISTRY_STATE_READY,
        },
    )
    env = build_app_env(app_id)
    handle = (await manager._resolve_sandbox(client, user.id, app_id, env)).handle
    assert client.provisioned == [] and client.restored == []  # attached, no re-provision
    assert handle.app_name == app_name_for(app_id)


async def test_resolve_sandbox_restores_when_gone_but_snapshot_exists(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    user, project_id = await _mk(db_session, "m4@rvaiglobal.com")
    manager = SessionManager()
    client = FakeSandboxClient()  # attach_handle unset -> attach raises Gone
    app_id = await resolve_app_for_project(db_session, user.id, project_id)
    await db_session.commit()
    await fake_storage.put(snapshot_key(app_id), b"BUNDLE")
    await fake_redis.hset(
        registry_key(user.id),
        mapping={
            REGISTRY_FIELD_APP_NAME: app_name_for(app_id),
            REGISTRY_FIELD_FQDN: "gone.example",
            REGISTRY_FIELD_TOKEN_REF: "ref",
            REGISTRY_FIELD_CREATED_AT: "2026-07-14T00:00:00+00:00",
            REGISTRY_FIELD_STATE: REGISTRY_STATE_READY,
        },
    )
    env = build_app_env(app_id)
    handle = (await manager._resolve_sandbox(client, user.id, app_id, env)).handle
    assert client.restored == [app_name_for(app_id)]  # attach gone + snapshot -> restore
    assert client.provisioned == []
    assert handle.app_name == app_name_for(app_id)


async def test_graceful_stop_snapshots_tears_down_and_is_idempotent(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    user, project_id = await _mk(db_session, "m5@rvaiglobal.com")
    manager = SessionManager()
    client = FakeSandboxClient()

    session = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=client, may_write=True
    )
    await manager.on_progress(session, A_STEP)  # the step (seq 1) is buffered before we stop
    ended = await manager.stop(session, client)
    assert ended.status == BuildSessionStatus.ENDED
    assert ended.terminal_committed is True
    assert app_name_for(session.app_id) in client.torn_down
    assert await lock_is_held(fake_redis, user.id) is False
    # The synthetic terminal seq is strictly last_seq+1 (gap-free, no double-terminal).
    terminal = session.envelopes[-1]
    assert isinstance(terminal, EndedEvent)
    assert terminal.reason == "stopped_by_user"
    assert terminal.seq == 2  # step was seq 1
    assert [e.seq for e in session.envelopes] == [1, 2]  # gap-free
    # A second stop returns the terminal state (idempotent).
    again = await manager.stop(session, client)
    assert again.status == BuildSessionStatus.ENDED


async def test_start_raises_lock_unavailable_not_conflict_when_the_acquire_hits_redis(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """Tested AT THE SITE THAT DECIDES IT.

    `_holding_user_lock` is where "the lock said no" became "a build session is already
    active". A partial outage — reconcile answers fine, the acquire does not — used to
    reach `BuildSessionConflictError` and render as a 409 naming a session that never
    existed. It must now raise `LockUnavailableError` instead, and the fail-closed
    guarantee has to survive: no lock written, no container, no registered session.
    """
    user, project_id = await _mk(db_session, "m-lockerr@rvaiglobal.com")
    manager = SessionManager()
    client = FakeSandboxClient()

    async def only_the_acquire_is_down(*args: object, **kwargs: object) -> object:
        raise RedisError("redis is down")

    # ONLY `set` — reconcile reads with hgetall/get/exists and sails through, so this is
    # precisely the PARTIAL shape. A blanket outage would raise out of reconcile first and
    # never reach the seam under test.
    monkeypatch_set = pytest.MonkeyPatch()
    monkeypatch_set.setattr(fake_redis, "set", only_the_acquire_is_down)
    try:
        with pytest.raises(LockUnavailableError):
            await manager.ensure_sandbox(
                db_session, user, project_id, sandbox_client=client, may_write=True
            )
    finally:
        monkeypatch_set.undo()

    assert not isinstance(LockUnavailableError("x"), BuildSessionConflictError)
    assert await lock_is_held(fake_redis, user.id) is False  # fail-closed: nothing granted
    assert client.provisioned == []  # and nothing allocated to compensate for
    assert manager.active_session_for(user.id) is None


async def test_start_reaps_through_a_dead_sessions_lingering_lock(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """Tested AT THE SITE THAT DECIDES IT: `_holding_user_lock`'s reconcile passes
    `certified_dead=True`, so the walkthrough's back-to-back 409 is gone — a dead session's
    lingering registry+lock+heartbeat is reaped on the way in and the start SUCCEEDS. The
    ghost's container is torn down (never orphaned) before the new one is provisioned; a
    GENUINELY live session still 409s via `_active_by_user`
    (`test_write_turn_sandbox.py::test_a_second_write_attach_while_one_is_live_is_a_conflict`)."""
    user, project_id = await _mk(db_session, "m-lockheld@rvaiglobal.com")
    manager = SessionManager()
    await _seed_live_sandbox_state(fake_redis, user.id)
    client = FakeSandboxClient()

    session = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=client, may_write=True
    )
    assert a_sandbox_name("someone-elses") in client.torn_down  # the ghost was executed first
    # ...and the allocation SUCCEEDED over it rather than 409ing on the phantom.
    assert client.provisioned == [app_name_for(session.app_id)]
    assert manager.active_session_for(user.id) is session


async def test_start_keeps_the_409_when_the_ghosts_teardown_fails(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """The fail-closed remainder of the old genuinely-held-lock 409: when the certified
    reconcile CANNOT reap the ghost (teardown error — the container may still be live),
    `reap_user` keeps lock+registry for a later sweep, the acquire fails, and the start
    still surfaces a 409 rather than double-allocating over a maybe-live container or
    mapping the contention to a 503."""
    user, project_id = await _mk(db_session, "m-ghost-stuck@rvaiglobal.com")
    manager = SessionManager()
    await _seed_live_sandbox_state(fake_redis, user.id)
    client = FakeSandboxClient()
    client.teardown_error = SandboxError("ACA delete wedged")

    with pytest.raises(BuildSessionConflictError):
        await manager.ensure_sandbox(
            db_session, user, project_id, sandbox_client=client, may_write=True
        )
    assert await lock_is_held(fake_redis, user.id) is True  # kept for the sweep's retry
    assert await read_registry(fake_redis, user.id) is not None


async def test_start_compensates_a_provision_failure_no_leaked_lock(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    user, project_id = await _mk(db_session, "m6@rvaiglobal.com")
    manager = SessionManager()

    class FailingProvision(FakeSandboxClient):
        async def provision_new(self, user_id, app_name, *, app_env):
            raise SandboxError("provision blew up")

    with pytest.raises(SandboxError):
        await manager.ensure_sandbox(
            db_session, user, project_id, sandbox_client=FailingProvision(), may_write=True
        )
    # No leaked lock, no session registered — the immediate next allocation succeeds.
    assert await lock_is_held(fake_redis, user.id) is False
    assert manager.active_session_for(user.id) is None

    good = FakeSandboxClient()
    session = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=good, may_write=True
    )
    assert session.status == BuildSessionStatus.PROVISIONING
    assert good.provisioned == [app_name_for(session.app_id)]


async def test_a_failed_starting_marker_write_leaks_no_lock(
    db_session: AsyncSession,
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A placement rule rather than a style note.

    `write_starting_marker` runs with the per-user lock ALREADY HELD. Called from above
    `_holding_user_lock`s try — where it was — a Redis blip on that one `SET` unwinds straight
    out, past the compensation arm that releases the lock, and leaves it in place for its full
    900-second TTL. Every start, relaunch and turn that user attempts for the next fifteen
    minutes is refused with "already building" while nothing is building. Inside the try, the
    same blip is compensated: the lock goes, and the next start succeeds immediately."""
    user, project_id = await _mk(db_session, "marker@rvaiglobal.com")
    manager = SessionManager()

    async def _boom(*args: object, **kwargs: object) -> object:
        raise RedisError("redis is down")

    monkeypatch.setattr("src.services.build_sessions.manager.write_starting_marker", _boom)
    with pytest.raises(RedisError):
        await manager.ensure_sandbox(
            db_session, user, project_id, sandbox_client=FakeSandboxClient(), may_write=True
        )

    assert await lock_is_held(fake_redis, user.id) is False
    assert manager.active_session_for(user.id) is None


async def test_abnormal_completion_synthesizes_failed_ended(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """An ending that carries `build_failed` is the one reason `_terminal_status` derives a
    FAILED status from, and the terminal frame is synthesized entirely by the manager.

    The abnormal completion USED TO BE a `run_build` task raising — the agent breaking its
    never-raise invariant — and `_run_and_finalize` caught it and finalized with this reason.
    That task is deleted; the reason, the derivation and the synthesis are not, and this drives
    them through the same `_finalize` call the task's own ending made."""
    user, project_id = await _mk(db_session, "m7@rvaiglobal.com")
    manager = SessionManager()
    client = FakeSandboxClient()

    session = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=client, may_write=True
    )
    # Two envelopes first, so the synthetic terminal's seq is a claim about arithmetic rather
    # than about an empty buffer: it must be `last_seq + 1`, not a hardcoded 1.
    await manager.on_progress(session, A_STEP)
    await manager.on_progress(session, PreviewReadyEvent(seq=2, preview_url="https://p/"))
    await _ends_with(manager, session, client, BUILD_FAILED)

    assert session.status == BuildSessionStatus.FAILED  # derived from the synthetic ended
    terminal = session.envelopes[-1]
    assert isinstance(terminal, EndedEvent)
    assert terminal.status == BuildSessionStatus.FAILED
    assert terminal.reason == "build_failed"
    assert terminal.seq == 3  # strictly last_seq+1 (preview_ready was seq 2), gap-free
    assert app_name_for(session.app_id) in client.torn_down  # container reclaimed
    assert await lock_is_held(fake_redis, user.id) is False  # lock reclaimed


async def test_on_progress_buffers_derives_status_and_fans_out(fake_redis: aioredis.Redis) -> None:
    manager = SessionManager()
    session = BuildSession(
        session_id=uuid.uuid7(),
        user_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        app_id=uuid.uuid4(),
        prompt="p",
        lock_token="tok",
        handle=SandboxHandle(
            fqdn="x.example",
            token="t",
            app_name=a_sandbox_name("x"),
            preview_url="https://x.example/",
            ready=False,
        ),
    )
    q1: asyncio.Queue[ProgressEnvelope] = asyncio.Queue()
    q2: asyncio.Queue[ProgressEnvelope] = asyncio.Queue()
    session.subscribers.update({q1, q2})

    await manager.on_progress(session, StepEvent(seq=1, name="s", label="l", state="started"))
    assert session.status.value == "building"  # provisioning -> building
    await manager.on_progress(session, PreviewReadyEvent(seq=2, preview_url="https://p/"))
    assert session.status == BuildSessionStatus.READY
    assert session.preview_url == "https://p/"
    assert session.last_seq == 2
    assert [e.seq for e in session.envelopes] == [1, 2]
    # Fanned out to BOTH subscribers, in order.
    assert q1.get_nowait().seq == 1
    assert q1.get_nowait().seq == 2
    assert q2.get_nowait().seq == 1


async def test_on_progress_reconnecting_buffers_and_fans_out_without_changing_status(
    fake_redis: aioredis.Redis,
) -> None:
    """A `preview_reconnecting` envelope is buffered, bumps `last_seq`, and fans out like
    any other, but does NOT change the lifecycle status (the status enum is frozen at five, with no
    reconnecting member): a framed session stays `ready`, and the portal reads the envelope for a
    distinct reconnecting visual."""
    manager = SessionManager()
    session = BuildSession(
        session_id=uuid.uuid7(),
        user_id=uuid.uuid4(),
        project_id=uuid.uuid4(),
        app_id=uuid.uuid4(),
        prompt="p",
        lock_token="tok",
        handle=SandboxHandle(
            fqdn="x.example",
            token="t",
            app_name=a_sandbox_name("x"),
            preview_url="https://x.example/",
            ready=False,
        ),
    )
    q: asyncio.Queue[ProgressEnvelope] = asyncio.Queue()
    session.subscribers.add(q)

    await manager.on_progress(session, PreviewReadyEvent(seq=1, preview_url="https://p/"))
    assert session.status == BuildSessionStatus.READY
    # The dev process crashes — reconnecting is buffered + fanned out, status LEFT unchanged.
    await manager.on_progress(session, PreviewReconnectingEvent(seq=2))
    assert session.status == BuildSessionStatus.READY  # NOT a 6th status; still ready
    assert session.last_seq == 2
    assert [e.seq for e in session.envelopes] == [1, 2]
    assert q.get_nowait().seq == 1
    assert q.get_nowait().seq == 2
    # A following preview_ready re-frames — the gap-free stream continues.
    await manager.on_progress(session, PreviewReadyEvent(seq=3, preview_url="https://p/"))
    assert session.status == BuildSessionStatus.READY
    assert session.last_seq == 3


async def test_reconcile_on_start_unblocks_a_crashed_user(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    user, project_id = await _mk(db_session, "m8@rvaiglobal.com")
    manager = SessionManager()
    client = FakeSandboxClient()
    # A crashed session's leftovers: a registry + a still-live lock, no heartbeat, no
    # in-proc session. reconcile-on-start must reap it so the fresh start acquires.
    await fake_redis.hset(
        registry_key(user.id),
        mapping={
            REGISTRY_FIELD_APP_NAME: a_sandbox_name("stale"),
            REGISTRY_FIELD_FQDN: "stale.example",
            REGISTRY_FIELD_TOKEN_REF: "ref",
            REGISTRY_FIELD_CREATED_AT: "2026-07-14T00:00:00+00:00",
            REGISTRY_FIELD_STATE: REGISTRY_STATE_READY,
        },
    )
    await fake_redis.set(lock_key(user.id), "crashed-token", ex=900)

    session = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=client, may_write=True
    )
    assert a_sandbox_name("stale") in client.torn_down  # the orphan was reaped on the way in
    assert session.status == BuildSessionStatus.PROVISIONING  # the fresh allocation acquired
    assert client.provisioned == [app_name_for(session.app_id)]


async def test_concurrent_same_user_starts_never_double_allocate(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    # FIX-1 (critical): two concurrent allocations for one user must NOT both provision — the
    # per-user start lock serializes them so the second sees the first's held lock → 409. The
    # SEQUENTIAL version of this refusal lives in `test_write_turn_sandbox.py`; what is only
    # observable here is the race, where a missing `_start_lock_for` lets both through.
    user, project_id = await _mk(db_session, "m9@rvaiglobal.com")
    manager = SessionManager()
    client = FakeSandboxClient()
    results = await asyncio.gather(
        manager.ensure_sandbox(
            db_session, user, project_id, sandbox_client=client, may_write=True
        ),
        manager.ensure_sandbox(
            db_session, user, project_id, sandbox_client=client, may_write=True
        ),
        return_exceptions=True,
    )
    sessions = [r for r in results if isinstance(r, BuildSession)]
    conflicts = [r for r in results if isinstance(r, BuildSessionConflictError)]
    assert len(sessions) == 1  # exactly one allocation won
    assert len(conflicts) == 1  # the other 409'd
    assert len(client.provisioned) == 1  # only ONE sandbox — no double-allocation


# --- FIX-3/6: the four best-effort error branches of `_do_finalize` -----------------
# Each injects a failure at one step and asserts the sequence STILL reaches the terminal
# `ended` synthesis (never leaves the SSE feed hung), popping `_active_by_user` regardless.


async def _live_session_stepped(
    manager: SessionManager, db_session: AsyncSession, email: str, client: FakeSandboxClient
) -> tuple[User, BuildSession]:
    """A live session with one envelope already buffered, ready to be stopped.

    THE BUFFERED SEQ IS LOAD-BEARING: the synthetic terminal is `last_seq + 1`, and a session
    with an empty buffer could not tell a gap-free terminal from a hardcoded 1."""
    user, project_id = await _mk(db_session, email)
    session = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=client, may_write=True
    )
    await manager.on_progress(session, A_STEP)  # step (seq 1) buffered before we stop
    return user, session


async def test_finalize_survives_a_snapshot_write_failure(
    db_session: AsyncSession,
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def boom_snapshot(*_a: object, **_k: object) -> None:
        raise StorageError("snapshot push failed")

    monkeypatch.setattr("src.services.build_sessions.manager.write_snapshot", boom_snapshot)
    manager = SessionManager()
    client = FakeSandboxClient()
    user, session = await _live_session_stepped(manager, db_session, "m11@rvaiglobal.com", client)

    ended = await manager.stop(session, client)
    # Snapshot raised, but teardown + release + terminal synthesis still ran.
    assert session.snapshot_committed is False
    assert ended.status == BuildSessionStatus.ENDED
    assert isinstance(session.envelopes[-1], EndedEvent)
    assert app_name_for(session.app_id) in client.torn_down
    assert await lock_is_held(fake_redis, user.id) is False  # lock still released
    assert manager.active_session_for(user.id) is None
    assert session.finalize_task is not None and session.finalize_task.done()


async def test_finalize_teardown_failure_keeps_lock_and_registry_but_still_synthesizes(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    manager = SessionManager()
    client = FakeSandboxClient()
    client.teardown_error = SandboxError("teardown boom")  # the container may still be live
    user, session = await _live_session_stepped(manager, db_session, "m12@rvaiglobal.com", client)
    # Seed a registry row so we can assert it is KEPT for the reaper (the fake provisions
    # without writing one).
    await fake_redis.hset(
        registry_key(user.id),
        mapping={
            REGISTRY_FIELD_APP_NAME: app_name_for(session.app_id),
            REGISTRY_FIELD_FQDN: "x.example",
            REGISTRY_FIELD_TOKEN_REF: "ref",
            REGISTRY_FIELD_CREATED_AT: "2026-07-14T00:00:00+00:00",
            REGISTRY_FIELD_STATE: REGISTRY_STATE_READY,
        },
    )

    ended = await manager.stop(session, client)
    # Teardown failed -> KEEP the lock + registry so the next reaper sweep retries (never
    # orphan a possibly-live container the registry-only scan could no longer see)...
    assert await lock_is_held(fake_redis, user.id) is True
    assert await fake_redis.hgetall(registry_key(user.id)) != {}
    assert app_name_for(session.app_id) not in client.torn_down  # teardown raised, no record
    # ...but STILL pop the in-proc session + synthesize the terminal so the SSE feed closes.
    assert manager.active_session_for(user.id) is None
    assert isinstance(session.envelopes[-1], EndedEvent)
    assert ended.status == BuildSessionStatus.ENDED
    assert session.finalize_task is not None and session.finalize_task.done()


async def test_finalize_survives_a_lock_release_failure(
    db_session: AsyncSession,
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def boom_release(*_a: object, **_k: object) -> bool:
        raise RuntimeError("redis release blip")

    monkeypatch.setattr("src.services.build_sessions.manager.release_lock_as_holder", boom_release)
    manager = SessionManager()
    client = FakeSandboxClient()
    _, session = await _live_session_stepped(manager, db_session, "m13@rvaiglobal.com", client)

    ended = await manager.stop(session, client)
    # The release raised, but teardown + registry-delete + terminal synthesis still ran.
    assert app_name_for(session.app_id) in client.torn_down  # teardown ran (release comes after)
    assert isinstance(session.envelopes[-1], EndedEvent)
    assert ended.status == BuildSessionStatus.ENDED
    assert manager.active_session_for(session.user_id) is None
    assert session.finalize_task is not None and session.finalize_task.done()


async def test_finalize_survives_a_registry_delete_failure(
    db_session: AsyncSession,
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def boom_delete(*_a: object, **_k: object) -> None:
        raise RuntimeError("redis delete blip")

    monkeypatch.setattr("src.services.build_sessions.manager.delete_registry", boom_delete)
    manager = SessionManager()
    client = FakeSandboxClient()
    user, session = await _live_session_stepped(manager, db_session, "m14@rvaiglobal.com", client)

    ended = await manager.stop(session, client)
    # The registry delete raised, but teardown + release + terminal synthesis still ran.
    assert app_name_for(session.app_id) in client.torn_down
    assert await lock_is_held(fake_redis, user.id) is False  # release still ran (before delete)
    assert isinstance(session.envelopes[-1], EndedEvent)
    assert ended.status == BuildSessionStatus.ENDED
    assert manager.active_session_for(user.id) is None
    assert session.finalize_task is not None and session.finalize_task.done()


# --- restore-on-absent-registry: the graceful stop→start loop must not discard work ----


async def test_clean_end_then_start_restores_from_snapshot_not_fresh(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    # A COMPLETED end PARDONS the container: registry kept under the lease. The next
    # allocation must RESTORE the snapshot the finalize just wrote — provisioning fresh
    # would wipe the user's work onto a blank template.
    #
    # It must ALSO not destroy the pardoned container on the way. The allocator used to pass no
    # `spare_app`, so `_the_live_sandbox_is_already_the_one_we_want` answered False
    # unconditionally and reconcile-on-start reaped every incumbent — including, as here, one
    # already serving this very app. That is the same destroy-and-rebuild bug removed from
    # the two turn paths and never removed from this one. Here the reap is invisible because
    # `attach_handle` is unset, so the attach arm raises `SandboxGoneError` and the restore
    # happens either way; on a REACHABLE container it cost the user everything since their
    # last Save (see the sibling below).
    #
    # THE END SEQUENCE IS THE POINT OF ENTRY, not `finish_turn_sandbox`: a `stop` carrying
    # `completed` is the only door left that runs `_do_finalize`'s step-1 snapshot AND its
    # pardon, which is the pair this loop depends on.
    user, project_id = await _mk(db_session, "m15@rvaiglobal.com")
    manager = SessionManager()
    client = FakeSandboxClient()
    first = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=client, may_write=True
    )
    # clean end: snapshot written, container pardoned, lock released
    await _ends_with(manager, first, client, COMPLETED)
    assert first.snapshot_committed is True
    assert await fake_redis.hgetall(registry_key(user.id)) != {}  # pardoned: registry stays

    second = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=client, may_write=True
    )
    assert second.app_id == first.app_id  # same project -> same app
    assert client.torn_down == []  # the pardoned container was SPARED, not reaped
    assert client.restored == [app_name_for(second.app_id)]  # RESTORED, not re-provisioned
    assert client.provisioned == [app_name_for(first.app_id)]  # only the very first allocation


async def test_a_start_on_the_same_project_reuses_the_pardoned_container(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """The sibling above with a REACHABLE container, which is where the old behaviour was
    destructive rather than merely wasteful.

    Drop `spare_app` from `ensure_sandbox` — it is the method that carries it now — and this
    goes red: `torn_down` gains the first container and `restored` gains an entry, so the next
    turn restarts from the last SAVED bundle, silently discarding everything the user had not
    saved. Its twin
    `test_write_turn_sandbox.py::test_a_second_message_attaches_instead_of_rebuilding_the_container`
    pins the identical rule, and the two are NOT duplicates: that one pardons through
    `finish_turn_sandbox`, this one through the END SEQUENCE's pardon in `_do_finalize`. Two
    different pardon sites leave a container up, and the spare has to spare both."""
    user, project_id = await _mk(db_session, "m15b@rvaiglobal.com")
    manager = SessionManager()
    client = FakeSandboxClient()
    first = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=client, may_write=True
    )
    await _ends_with(manager, first, client, COMPLETED)
    client.attach_handle = first.handle  # the pardoned container answers

    second = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=client, may_write=True
    )
    assert second.app_id == first.app_id
    assert client.torn_down == []  # nothing destroyed
    assert client.restored == []  # nothing rebuilt from the snapshot
    assert client.provisioned == [app_name_for(first.app_id)]  # only the very first allocation


async def test_restore_falls_back_to_fresh_when_snapshot_vanishes_mid_restore(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    # A snapshot that disappears between the head-check and the pull must fall back to a
    # fresh provision, never fail the start.
    user, project_id = await _mk(db_session, "m16@rvaiglobal.com")
    manager = SessionManager()

    class VanishingSnapshot(FakeSandboxClient):
        async def restore_from_snapshot(
            self,
            user_id,
            app_name,
            *,
            app_env,
            source_key=None,
            kind="build_sandbox",
            shared_project_id=None,
            shared_owner_id=None,
        ):
            raise StorageNotFoundError("snapshot vanished", provider="fake", key="k")

    client = VanishingSnapshot()
    app_id = await resolve_app_for_project(db_session, user.id, project_id)
    await db_session.commit()
    await fake_storage.put(snapshot_key(app_id), b"BUNDLE")  # head-check sees it...

    env = build_app_env(app_id)
    handle = (await manager._resolve_sandbox(client, user.id, app_id, env)).handle
    assert client.provisioned == [app_name_for(app_id)]  # ...the pull 404s -> fresh
    assert handle.app_name == app_name_for(app_id)


# --- never provision a blank template over the user's work --------------------------
#
# The whole point of this block: "fresh provision" is only ever correct when the store
# POSITIVELY says the bundle is gone. Every ambiguous or failing answer must abort the
# start, because a fresh template is not a degraded start — finalize's step-1 snapshot
# writes it OVER the user's good bundle and destroys it for good.


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    """Run the bounded-retry backoff instantly and record the schedule."""
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    monkeypatch.setattr("src.services.build_sessions.manager._asleep", fake_sleep)
    return slept


class HeadScript(FakeStorage):
    """A storage whose `head` raises the first `failures` times for the SAVED bundle, then
    behaves normally.

    Key-scoped on purpose. The restore path now heads two keys — the recovery bundle first, to
    decide which tree is newest, then the saved one — and each gets its own retry budget. A
    fake that blipped on any key would let the recovery probe absorb failures scripted for the
    snapshot check, and these tests would silently stop testing the thing they name."""

    def __init__(self, failures: int) -> None:
        super().__init__()
        self.remaining = failures
        self.head_calls = 0

    async def head(self, key):
        if not key.startswith("snapshots/"):
            return await super().head(key)
        self.head_calls += 1
        if self.remaining > 0:
            self.remaining -= 1
            raise StorageError("blob head blipped", provider="fake", key=key)
        return await super().head(key)


async def _seed_app_with_bundle(
    db: AsyncSession, user: User, project_id: uuid.UUID, store: FakeStorage
) -> tuple[uuid.UUID, dict[str, str]]:
    app_id = await resolve_app_for_project(db, user.id, project_id)
    await db.commit()
    await store.put(snapshot_key(app_id), b"BUNDLE")
    return app_id, build_app_env(app_id)


async def test_head_check_retries_a_transient_blip_then_restores(
    db_session: AsyncSession, fake_redis: aioredis.Redis, no_sleep: list[float]
) -> None:
    # Two blips then a clean answer: the retry absorbs it and the start proceeds to a
    # RESTORE. Bound the fake to the accessor singleton so the real get_storage() seam runs.
    from src.services.storage import accessor as storage_accessor

    store = HeadScript(failures=_HEAD_ATTEMPTS - 1)
    storage_accessor._backend_singleton = store
    try:
        user, project_id = await _mk(db_session, "m26@rvaiglobal.com")
        manager = SessionManager()
        client = FakeSandboxClient()
        app_id, env = await _seed_app_with_bundle(db_session, user, project_id, store)

        handle = (await manager._resolve_sandbox(client, user.id, app_id, env)).handle

        assert store.head_calls == _HEAD_ATTEMPTS  # blipped, blipped, answered
        assert len(no_sleep) == _HEAD_ATTEMPTS - 1  # backed off between attempts
        assert client.restored == [app_name_for(app_id)]
        assert client.provisioned == []  # never guessed "absent"
        assert handle.app_name == app_name_for(app_id)
    finally:
        storage_accessor._backend_singleton = None


async def test_persistent_head_failure_fails_the_start_closed_and_releases_the_lock(
    db_session: AsyncSession, fake_redis: aioredis.Redis, no_sleep: list[float]
) -> None:
    # An unanswerable head-check must abort the START (not just _resolve_sandbox), leaving
    # NO sandbox running, NO snapshot touched, and the per-user lock released by the
    # compensation block — asserted, not assumed (the git-bundle teardown invariant).
    from src.services.storage import accessor as storage_accessor

    store = HeadScript(failures=999)
    storage_accessor._backend_singleton = store
    try:
        user, project_id = await _mk(db_session, "m27@rvaiglobal.com")
        manager = SessionManager()
        client = FakeSandboxClient()
        app_id, _ = await _seed_app_with_bundle(db_session, user, project_id, store)

        with pytest.raises(SnapshotUnavailableError) as caught:
            await manager.ensure_sandbox(
                db_session, user, project_id, sandbox_client=client, may_write=True
            )

        assert caught.value.app_id == app_id
        assert store.head_calls == _HEAD_ATTEMPTS  # bounded, not infinite
        assert client.provisioned == []  # THE invariant: no blank template
        assert client.restored == []
        assert snapshot_key(app_id) in store.objects  # the user's work is untouched
        assert await lock_is_held(fake_redis, user.id) is False  # compensation released it
        assert manager._active_by_user == {}  # no half-built session left registered
    finally:
        storage_accessor._backend_singleton = None


async def test_restore_retries_a_transient_sandbox_error_then_succeeds(
    db_session: AsyncSession,
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
    no_sleep: list[float],
) -> None:
    # An npm/registry blip inside the `set -e` restore script is exactly the case the old
    # fresh-provision fallback existed to survive. The bounded retry survives it WITHOUT
    # ever reaching for a blank template.
    user, project_id = await _mk(db_session, "m28@rvaiglobal.com")
    manager = SessionManager()

    class FlakyRestore(FakeSandboxClient):
        def __init__(self) -> None:
            super().__init__()
            self.attempts = 0

        async def restore_from_snapshot(
            self,
            user_id,
            app_name,
            *,
            app_env,
            source_key=None,
            kind="build_sandbox",
            shared_project_id=None,
            shared_owner_id=None,
        ):
            self.attempts += 1
            if self.attempts == 1:
                raise SandboxError("npm install failed under set -e")
            return await super().restore_from_snapshot(user_id, app_name, app_env=app_env)

    client = FlakyRestore()
    app_id, env = await _seed_app_with_bundle(db_session, user, project_id, fake_storage)

    handle = (await manager._resolve_sandbox(client, user.id, app_id, env)).handle

    assert client.attempts == 2
    assert client.restored == [app_name_for(app_id)]
    assert client.provisioned == []  # the fallback is gone for good
    assert handle.app_name == app_name_for(app_id)


async def test_persistent_restore_failure_fails_closed_and_never_provisions_fresh(
    db_session: AsyncSession,
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
    no_sleep: list[float],
) -> None:
    # The nastiest arm: the bundle EXISTS and the restore keeps failing. The old code
    # provisioned a blank template here, which finalize would then snapshot OVER the user's
    # good bundle — silent, permanent data loss. Assert the fresh-provision arm is now
    # UNREACHABLE from this path and the bundle survives byte-for-byte.
    user, project_id = await _mk(db_session, "m29@rvaiglobal.com")
    manager = SessionManager()

    class DoomedRestore(FakeSandboxClient):
        def __init__(self) -> None:
            super().__init__()
            self.attempts = 0

        async def restore_from_snapshot(
            self,
            user_id,
            app_name,
            *,
            app_env,
            source_key=None,
            kind="build_sandbox",
            shared_project_id=None,
            shared_owner_id=None,
        ):
            self.attempts += 1
            raise SandboxError("npm install failed under set -e")

    client = DoomedRestore()
    app_id, _ = await _seed_app_with_bundle(db_session, user, project_id, fake_storage)

    with pytest.raises(SnapshotUnavailableError) as caught:
        await manager.ensure_sandbox(
            db_session, user, project_id, sandbox_client=client, may_write=True
        )

    assert caught.value.app_id == app_id
    assert client.attempts == _RESTORE_ATTEMPTS  # bounded
    assert client.provisioned == []  # THE invariant: no template over the user's work
    assert fake_storage.objects[snapshot_key(app_id)] == b"BUNDLE"  # not overwritten
    assert await lock_is_held(fake_redis, user.id) is False
    assert manager._active_by_user == {}


async def test_restore_retries_a_transient_storage_error_then_fails_closed(
    db_session: AsyncSession,
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
    no_sleep: list[float],
) -> None:
    # The pull is fallible too: `restore_from_snapshot` opens with `get_storage().get(...)`, so a
    # StorageAuthError (expired SAS delegation) surfaces from the same call as an npm blip and
    # deserves the same treatment — retried, then 503. Uncaught it would be a bare 500 with ZERO
    # retries, which is neither the docstring's promise nor survivable for a transient blip.
    user, project_id = await _mk(db_session, "m30@rvaiglobal.com")
    manager = SessionManager()

    class AuthDeniedPull(FakeSandboxClient):
        def __init__(self) -> None:
            super().__init__()
            self.attempts = 0

        async def restore_from_snapshot(
            self,
            user_id,
            app_name,
            *,
            app_env,
            source_key=None,
            kind="build_sandbox",
            shared_project_id=None,
            shared_owner_id=None,
        ):
            self.attempts += 1
            raise StorageAuthError("the bundle pull was denied", provider="fake", key="k")

    client = AuthDeniedPull()
    app_id, _ = await _seed_app_with_bundle(db_session, user, project_id, fake_storage)

    with pytest.raises(SnapshotUnavailableError) as caught:  # a 503, NOT a 500
        await manager.ensure_sandbox(
            db_session, user, project_id, sandbox_client=client, may_write=True
        )

    assert caught.value.app_id == app_id
    assert client.attempts == _RESTORE_ATTEMPTS  # retried, then exhausted — not one-and-done
    assert client.provisioned == []  # the invariant holds on this arm too
    assert fake_storage.objects[snapshot_key(app_id)] == b"BUNDLE"  # work untouched
    assert await lock_is_held(fake_redis, user.id) is False


async def test_start_with_object_storage_unconfigured_provisions_fresh_instead_of_503(
    db_session: AsyncSession, fake_redis: aioredis.Redis, no_sleep: list[float]
) -> None:
    # NO `fake_storage` fixture, deliberately: this is the storage-OFF deployment `src.config`
    # explicitly supports outside production (`provision_app_storage` returns {} for the same
    # reason). `get_storage()` raises there — and folding that into the fail-closed arm would
    # 503 EVERY allocation on such a deployment. With no store there is no bundle, so a fresh
    # provision destroys nothing: it is a CONFIRMED absent, not an unknown.
    from src.services.storage import accessor as storage_accessor

    assert storage_accessor._backend_singleton is None  # the fixture-free baseline this rests on
    user, project_id = await _mk(db_session, "m31@rvaiglobal.com")
    manager = SessionManager()
    client = FakeSandboxClient()

    session = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=client, may_write=True
    )

    assert client.provisioned == [app_name_for(session.app_id)]  # started, and started fresh
    assert no_sleep == []  # never retried what is a permanent config fact, not a blip
    # ...and the END still runs cleanly with no store: the finalize snapshot is a no-op here.
    await _ends_with(manager, session, client, COMPLETED)
    assert session.status == BuildSessionStatus.ENDED


# --- the ended-session retention window --------------------------------------------


async def test_ended_session_kept_inside_retention_window_evicted_after(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    user, project_id = await _mk(db_session, "m17@rvaiglobal.com")
    manager = SessionManager()
    client = FakeSandboxClient()
    session = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=client, may_write=True
    )
    await manager.on_progress(session, A_STEP)  # something in the replay buffer to retain
    await _ends_with(manager, session, client, COMPLETED)
    assert session.ended_at is not None  # the retention clock started at finalize

    # INSIDE the window: kept, with the full envelope buffer intact — a late SSE reconnect
    # can still replay the story + [DONE] (the replay itself is covered in test_sse.py).
    assert manager.evict_ended_sessions() == 0
    assert manager.get(session.session_id) is session
    assert isinstance(session.envelopes[-1], EndedEvent)

    # PAST the window: dropped from _sessions (and _active_by_user, defensively).
    past = datetime.now(UTC) + timedelta(seconds=_ENDED_RETENTION_SECONDS + 1)
    assert manager.evict_ended_sessions(now=past) == 1
    assert manager.get(session.session_id) is None
    assert manager.active_session_for(user.id) is None


async def test_next_start_sweeps_an_expired_ended_session(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    # The opportunistic sweep at the top of `ensure_sandbox` is the guaranteed-recurring seam
    # — an expired ended session must be gone once the next allocation (any user) runs. It is
    # also the ONLY recurring seam left now that `start` is deleted: nothing evicts on a timer,
    # so a workspace that only ever chats would otherwise accumulate ended sessions forever.
    user, project_id = await _mk(db_session, "m18@rvaiglobal.com")
    manager = SessionManager()
    client = FakeSandboxClient()
    first = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=client, may_write=True
    )
    await _ends_with(manager, first, client, COMPLETED)
    first.ended_at = datetime.now(UTC) - timedelta(seconds=_ENDED_RETENTION_SECONDS + 1)

    second = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=client, may_write=True
    )
    assert manager.get(first.session_id) is None  # swept on entry
    assert manager.get(second.session_id) is second


# --- start-after-terminal-finalize: a refine on the heels of completion is not a 409 --


async def test_start_awaits_a_still_finalizing_terminal_session_then_starts_fresh(
    db_session: AsyncSession,
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    user, project_id = await _mk(db_session, "m19@rvaiglobal.com")
    manager = SessionManager()

    # Block _do_finalize inside its step-1 SNAPSHOT so the session sits terminal_committed
    # but still finalizing (the exact window a fast refine lands in). The snapshot step is
    # the gate because it runs on EVERY end path — a completed end no longer tears down
    # (the pardon), so a teardown gate would never be entered.
    #
    # THE ENDING IS DETACHED, not awaited, and that is the whole fixture. `_finalize` awaits
    # the shielded end sequence, so calling it inline would hand back an already-finished
    # session and there would be no window at all. The ending that used to open this window ran
    # in the deleted `run_build` task; a detached call to the same door is the same shape — a
    # caller elsewhere holding the end sequence open while this one arrives.
    entered = asyncio.Event()
    gate = asyncio.Event()

    async def gated_snapshot(
        sandbox_client: SandboxClient,
        handle: SandboxHandle,
        app_id: uuid.UUID,
        *,
        destination: Destination | None = None,
    ) -> str:
        entered.set()
        await gate.wait()
        # Forward the caller's destination rather than recomputing one: the stub must not
        # quietly redirect a write the code under test aimed somewhere specific.
        return await write_snapshot(sandbox_client, handle, app_id, destination=destination)

    monkeypatch.setattr("src.services.build_sessions.manager.write_snapshot", gated_snapshot)

    client = FakeSandboxClient()
    first = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=client, may_write=True
    )
    ending = asyncio.create_task(_ends_with(manager, first, client, COMPLETED))
    await entered.wait()  # finalize is mid-snapshot: terminal committed, not done
    assert first.terminal_committed is True
    assert first.finalize_task is not None and not first.finalize_task.done()

    starter = asyncio.create_task(
        manager.ensure_sandbox(db_session, user, project_id, sandbox_client=client, may_write=True)
    )
    for _ in range(20):  # the second allocation WAITS on the finalize instead of 409ing
        await asyncio.sleep(0)
    assert not starter.done()

    gate.set()  # finalize completes -> the waiting allocation proceeds FRESH
    second = await starter
    assert second.session_id != first.session_id
    assert client.restored == [app_name_for(second.app_id)]  # picked up the snapshot
    await ending


# --- best-effort mark_registry_ending in _end (the kill switch must never 500) --------


async def test_stop_survives_a_mark_registry_ending_failure(
    db_session: AsyncSession,
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def boom_mark(*_a: object, **_k: object) -> None:
        raise RuntimeError("redis blip on mark-ending")

    monkeypatch.setattr("src.services.build_sessions.manager.mark_registry_ending", boom_mark)
    manager = SessionManager()
    client = FakeSandboxClient()
    user, session = await _live_session_stepped(manager, db_session, "m20@rvaiglobal.com", client)

    ended = await manager.stop(session, client)  # no raise -> no 500 path
    assert ended.status == BuildSessionStatus.ENDED
    assert ended.terminal_committed is True
    # The graceful stop still ran the FULL end sequence: snapshot, teardown, release.
    assert session.snapshot_committed is True
    assert snapshot_key(session.app_id) in fake_storage.objects
    assert app_name_for(session.app_id) in client.torn_down
    assert await lock_is_held(fake_redis, user.id) is False
    assert manager.active_session_for(user.id) is None


async def test_force_end_survives_a_mark_registry_ending_failure_without_poisoning(
    db_session: AsyncSession,
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The Redis blip runs BEFORE the flags are mutated, so a failing mark can never leave
    # `force_ended=True` poisoned on a still-running session (where a later natural
    # completion would silently skip its snapshot): the kill switch always proceeds to
    # cancel + finalize in the same call.
    async def boom_mark(*_a: object, **_k: object) -> None:
        raise RuntimeError("redis blip on mark-ending")

    monkeypatch.setattr("src.services.build_sessions.manager.mark_registry_ending", boom_mark)
    manager = SessionManager()
    client = FakeSandboxClient()
    user, session = await _live_session_stepped(manager, db_session, "m21@rvaiglobal.com", client)

    ended = await manager.force_end(session, client)  # no raise -> no 500 path
    assert ended.status == BuildSessionStatus.ENDED
    assert ended.terminal_committed is True  # force-end COMPLETED — never left half-done
    assert session.force_ended is True
    assert session.snapshot_committed is False  # kill switch skips the snapshot by design
    assert snapshot_key(session.app_id) not in fake_storage.objects
    assert app_name_for(session.app_id) in client.torn_down
    assert await lock_is_held(fake_redis, user.id) is False
    assert manager.active_session_for(user.id) is None


async def test_force_end_landing_inside_mark_ending_never_steals_a_completed_snapshot(
    db_session: AsyncSession,
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The TOCTOU `_end` guards against: the work COMPLETES while `_end` is suspended inside
    # `mark_registry_ending`. `_end`'s entry check said "not terminal" and is now stale, so a
    # blind `force_ended = True` would land BEHIND the terminal commit — finalize would then skip
    # the snapshot of a finished session whose terminal already says `completed`. The user's work
    # would be gone with nothing anywhere admitting it. The loser of this race writes NOTHING.
    #
    # THE RACER IS `_finalize` ITSELF, called directly. The completion that used to win this race
    # was the `run_build` task returning its verdict, and `_run_and_finalize` handed that straight
    # to `_finalize`; the task is deleted, `_finalize` is not, so the race is staged at the exact
    # door the completion went through. What makes the window deterministic is that `_finalize`
    # commits `terminal_committed` SYNCHRONOUSLY before it creates the shielded task — so by the
    # time this returns, `_end` is guaranteed to resume with a stale entry check.
    manager = SessionManager()
    client = FakeSandboxClient()
    user, project_id = await _mk(db_session, "m32@rvaiglobal.com")
    session = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=client, may_write=True
    )
    await manager.on_progress(session, A_STEP)
    completion: list[asyncio.Task[None]] = []

    async def complete_inside_the_await(*_a: object, **_k: object) -> None:
        completion.append(asyncio.ensure_future(_ends_with(manager, session, client, COMPLETED)))
        while not session.terminal_committed:
            await asyncio.sleep(0)

    monkeypatch.setattr(
        "src.services.build_sessions.manager.mark_registry_ending", complete_inside_the_await
    )

    ended = await manager.force_end(session, client)
    await completion[0]

    assert session.force_ended is False  # the late kill-switch flag was NOT written
    assert session.snapshot_committed is True  # …so the completed build's work was saved
    assert snapshot_key(session.app_id) in fake_storage.objects
    # And the terminal is truthful about it — exactly one frame, still the completion's.
    terminals = _endeds(session)
    assert len(terminals) == 1
    assert terminals[0].reason == "completed"
    assert terminals[0].snapshot_committed is True
    assert ended.status == BuildSessionStatus.ENDED
    # The end sequence still ran to completion before force_end returned (it awaited it).
    assert session.finalize_task is not None and session.finalize_task.done()
    # The COMPLETION owned the end sequence, so its pardon stands: the container the
    # late kill switch failed to claim stays up under the lease, lock released.
    assert app_name_for(session.app_id) not in client.torn_down
    assert await stay_of_execution_is_current(fake_redis, user.id) is True
    assert await lock_is_held(fake_redis, user.id) is False


async def test_stop_racing_completion_finalizes_exactly_once(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    # FIX-2: two endings racing each other must not tear the end sequence in half (the shielded
    # _do_finalize runs to completion exactly once). One of them carries `completed` and one the
    # ordinary user stop, because the two take OPPOSITE arms of `_do_finalize` — pardon versus
    # teardown — so a torn sequence would show up as both, or neither, rather than as one.
    user, project_id = await _mk(db_session, "m10@rvaiglobal.com")
    manager = SessionManager()
    client = FakeSandboxClient()
    session = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=client, may_write=True
    )
    await manager.on_progress(session, A_STEP)
    await asyncio.gather(
        _ends_with(manager, session, client, COMPLETED),
        manager.stop(session, client),
        return_exceptions=True,
    )
    # Fully finalized, no leak, ONE end sequence — whichever racer won it. A completion win
    # pardons the container (zero teardowns, lease granted); a stop win tears it down
    # exactly once. Either way the lock is released and exactly one terminal is emitted.
    assert session.terminal_committed is True
    assert await lock_is_held(fake_redis, user.id) is False
    terminal = session.envelopes[-1]
    assert isinstance(terminal, EndedEvent)
    expected_teardowns = 0 if terminal.reason == "completed" else 1
    assert client.torn_down.count(app_name_for(session.app_id)) == expected_teardowns
    assert manager.active_session_for(user.id) is None


# --- per-app Blob env injection on the birth arms only ------------------------------
# In the test env object storage is unconfigured, so the real provision_app_storage returns {}
# (harmless no-op — see the untouched tests above). These tests patch it to inject the two
# BIAL_BLOB_* vars and assert the WIRING: provision + restore get them, attach does not.

_BLOB_VARS = {
    "BIAL_BLOB_CONTAINER_URL": "http://azurite:10000/devstoreaccount1/app-x",
    "BIAL_BLOB_SAS": "sv=x&sig=SIG",
}


def _patch_provision(monkeypatch: pytest.MonkeyPatch, calls: list[uuid.UUID]) -> None:
    async def _fake(app_id: uuid.UUID) -> dict[str, str]:
        calls.append(app_id)
        return dict(_BLOB_VARS)

    monkeypatch.setattr("src.services.build_sessions.manager.provision_app_storage", _fake)


# The birth-arm env capture now lives on FakeSandboxClient itself (`provision_env` /
# `restore_env`), so every suite can assert what a container was actually born with.


async def test_provision_injects_both_blob_vars_alongside_the_base_env(
    db_session: AsyncSession,
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[uuid.UUID] = []
    _patch_provision(monkeypatch, calls)
    user, project_id = await _mk(db_session, "m22@rvaiglobal.com")
    manager = SessionManager()
    client = FakeSandboxClient()

    session = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=client, may_write=True
    )

    assert calls == [session.app_id]  # storage provisioned once, for this app
    assert client.provision_env is not None
    assert client.provision_env["BIAL_BLOB_CONTAINER_URL"] == _BLOB_VARS["BIAL_BLOB_CONTAINER_URL"]
    assert client.provision_env["BIAL_BLOB_SAS"] == _BLOB_VARS["BIAL_BLOB_SAS"]
    # Merged, not replaced — the always-present identity vars are still present.
    assert client.provision_env["BIAL_APP_ID"] == str(session.app_id)
    assert "BIAL_PORTAL_ORIGIN" in client.provision_env


async def test_restore_injects_both_blob_vars(
    db_session: AsyncSession,
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[uuid.UUID] = []
    _patch_provision(monkeypatch, calls)
    user, project_id = await _mk(db_session, "m23@rvaiglobal.com")
    manager = SessionManager()
    client = FakeSandboxClient()
    app_id = await resolve_app_for_project(db_session, user.id, project_id)
    await db_session.commit()
    await fake_storage.put(snapshot_key(app_id), b"BUNDLE")  # no registry + snapshot -> restore

    handle = (
        await manager._resolve_sandbox(client, user.id, app_id, build_app_env(app_id))
    ).handle
    assert client.restored == [app_name_for(app_id)]
    assert handle.app_name == app_name_for(app_id)
    assert calls == [app_id]
    assert client.restore_env is not None
    assert client.restore_env["BIAL_BLOB_SAS"] == _BLOB_VARS["BIAL_BLOB_SAS"]
    assert client.restore_env["BIAL_APP_ID"] == str(app_id)


async def test_attach_does_no_storage_work_and_forwards_no_env(
    db_session: AsyncSession,
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Attach reuses the live container's SAS: provision_app_storage is NEVER called on the
    # attach arm (no container/SAS work), and attach_existing takes no env.
    calls: list[uuid.UUID] = []
    _patch_provision(monkeypatch, calls)
    user, project_id = await _mk(db_session, "m24@rvaiglobal.com")
    manager = SessionManager()
    client = FakeSandboxClient()
    app_id = await resolve_app_for_project(db_session, user.id, project_id)
    await db_session.commit()
    client.attach_handle = SandboxHandle(
        fqdn="existing.example",
        token="tok",
        app_name=app_name_for(app_id),
        preview_url="https://existing.example/",
        ready=False,
    )
    await fake_redis.hset(
        registry_key(user.id),
        mapping={
            REGISTRY_FIELD_APP_NAME: app_name_for(app_id),
            REGISTRY_FIELD_FQDN: "existing.example",
            REGISTRY_FIELD_TOKEN_REF: "ref",
            REGISTRY_FIELD_CREATED_AT: "2026-07-14T00:00:00+00:00",
            REGISTRY_FIELD_STATE: REGISTRY_STATE_READY,
        },
    )

    handle = (
        await manager._resolve_sandbox(client, user.id, app_id, build_app_env(app_id))
    ).handle
    assert client.provisioned == [] and client.restored == []  # attached
    assert handle.app_name == app_name_for(app_id)
    assert calls == []  # storage untouched on attach


async def test_birth_path_storage_failure_compensates_no_leaked_lock(
    db_session: AsyncSession,
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def boom(_app_id: uuid.UUID) -> dict[str, str]:
        raise StorageError("blob provision failed")

    monkeypatch.setattr("src.services.build_sessions.manager.provision_app_storage", boom)
    user, project_id = await _mk(db_session, "m25@rvaiglobal.com")
    manager = SessionManager()
    client = FakeSandboxClient()

    with pytest.raises(StorageError):
        await manager.ensure_sandbox(
            db_session, user, project_id, sandbox_client=client, may_write=True
        )
    # Compensation ran: lock released, no session registered, and we never reached provision
    # (storage failed first, so no sandbox handle exists to tear down).
    assert await lock_is_held(fake_redis, user.id) is False
    assert manager.active_session_for(user.id) is None
    assert client.provisioned == []


# --- the single authoritative terminal `ended` --------------------------------
#
# The unit's whole point, stated as an invariant: NO end path may emit two `ended` frames or a
# false `snapshot_committed`. The manager emits exactly one, from `_do_finalize`, AFTER the
# snapshot — the only moment the flag can be told truthfully. These tests enumerate every end
# REASON the surviving door can carry:
#   completed · quota_exceeded · stopped_by_user · idle_teardown · force_ended · build_failed
#
# ONE REASON LOST ITS DOOR. `escalated` was the case that proved a verdict's own `status` must
# win over deriving one from the reason string (an escalated end is FAILED, yet "escalated" is
# not `build_failed`), and the only thing that ever carried a verdict was the deleted
# `run_build` task handing a `BuildResult` to `_finalize`. `_do_finalize` still takes that
# `result` argument and still prefers it, but nothing live passes one, so the rule is no longer
# reachable from any door a test can knock on — see the note where the escalated test used to
# be. Every reason listed above reaches `_do_finalize` through `stop` / `force_end`.


def _endeds(session: BuildSession) -> list[EndedEvent]:
    return [e for e in session.envelopes if isinstance(e, EndedEvent)]


class _OrderRecordingSandboxClient(FakeSandboxClient):
    """Records teardown into a shared order log so the ordering invariant
    (snapshot → teardown-or-pardon → release → terminal) is asserted, not assumed —
    a completed build's log shows NO teardown at all (the pardon)."""

    def __init__(self, order: list[str]) -> None:
        super().__init__()
        self._order = order

    async def teardown(self, handle: SandboxHandle) -> None:
        self._order.append("teardown")
        await super().teardown(handle)


def _spy_order(
    manager: SessionManager, monkeypatch: pytest.MonkeyPatch, *, snapshot_raises: bool = False
) -> list[str]:
    """Observe (never fake) the end sequence: log when the snapshot runs and when the terminal
    `ended` is emitted, so their ORDER — not just their outcome — is provable."""
    order: list[str] = []

    async def spy_snapshot(
        sandbox_client: SandboxClient,
        handle: SandboxHandle,
        app_id: uuid.UUID,
        *,
        destination: Destination | None = None,
    ) -> str:
        order.append("snapshot")
        if snapshot_raises:
            raise StorageError("snapshot push failed")
        return await write_snapshot(sandbox_client, handle, app_id, destination=destination)

    monkeypatch.setattr("src.services.build_sessions.manager.write_snapshot", spy_snapshot)

    real_progress = manager.on_progress

    async def spy_progress(session: BuildSession, env: ProgressEnvelope) -> None:
        if isinstance(env, EndedEvent):
            order.append("ended")
        await real_progress(session, env)

    monkeypatch.setattr(manager, "on_progress", spy_progress)
    return order


async def test_completed_build_emits_one_ended_after_the_snapshot_with_the_true_flag(
    db_session: AsyncSession,
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The headline: the terminal frame reports snapshot_committed=TRUE on a build whose
    # snapshot really committed, which it can only do because it is emitted after the commit.
    manager = SessionManager()
    order = _spy_order(manager, monkeypatch)
    client = _OrderRecordingSandboxClient(order)
    user, project_id = await _mk(db_session, "r7a@rvaiglobal.com")

    session = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=client, may_write=True
    )
    await manager.on_progress(session, A_STEP)
    await manager.on_progress(
        session, PreviewReadyEvent(seq=2, preview_url="https://preview.example/")
    )
    await _ends_with(manager, session, client, COMPLETED)

    ended = _endeds(session)
    assert len(ended) == 1  # exactly ONE terminal
    assert ended[0].snapshot_committed is True  # …and it is TRUE (the lie this kills)
    assert ended[0].status == BuildSessionStatus.ENDED
    assert ended[0].reason == "completed"
    assert ended[0].preview_url == "https://preview.example/"  # the last preview_ready seen
    assert ended[0] is session.envelopes[-1]  # always last
    # The snapshot really is committed, and the frame really is emitted after it. No
    # teardown in between: the completed build's container is pardoned, so the frame's
    # preview_url points at a container that is actually still serving.
    assert snapshot_key(session.app_id) in fake_storage.objects
    assert order == ["snapshot", "ended"]
    assert app_name_for(session.app_id) not in client.torn_down
    # seq continues the agent's stream at last_seq + 1 — gap-free across the handoff.
    assert ended[0].seq == 3
    assert [e.seq for e in session.envelopes] == [1, 2, 3]
    assert session.last_seq == 3


async def test_snapshot_failure_emits_one_ended_that_admits_the_work_was_not_saved(
    db_session: AsyncSession,
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The mirror of the happy path, and the reason the flag must be computed and not assumed:
    # the build completed, but its snapshot did NOT. `snapshot_committed=false` is exactly how
    # the frame reports that; `reason` still says `completed` because the BUILD did complete —
    # the two fields answer different questions ("did it build?" vs "was it saved?").
    manager = SessionManager()
    order = _spy_order(manager, monkeypatch, snapshot_raises=True)
    client = _OrderRecordingSandboxClient(order)
    user, project_id = await _mk(db_session, "r7b@rvaiglobal.com")

    session = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=client, may_write=True
    )
    await _ends_with(manager, session, client, COMPLETED)

    ended = _endeds(session)
    assert len(ended) == 1
    assert ended[0].snapshot_committed is False  # never claims a snapshot that did not happen
    assert ended[0].reason == "completed"
    assert session.snapshot_committed is False
    assert snapshot_key(session.app_id) not in fake_storage.objects
    # A failed snapshot must not disturb the ordering invariant — and it must not cost the
    # user the live preview either: the WORK completed, so the pardon still applies.
    # Durability and visibility are separate questions with separate answers.
    assert order == ["snapshot", "ended"]
    assert app_name_for(session.app_id) not in client.torn_down
    assert await stay_of_execution_is_current(fake_redis, user.id) is True
    assert await lock_is_held(fake_redis, user.id) is False  # …and the lock still released


async def test_quota_run_emits_the_quota_envelope_then_exactly_one_ended(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    # The quota path composed end-to-end: the informational `quota_exceeded` envelope goes out
    # on the feed, then the end is asked for with that reason. The portal must see
    # quota_exceeded followed by ONE terminal — and a graceful ENDED, never FAILED, which is a
    # claim about `_terminal_status`: only `build_failed` derives FAILED, so a reason that reads
    # like bad news must still end gracefully.
    manager = SessionManager()
    client = FakeSandboxClient()
    user, project_id = await _mk(db_session, "r7c@rvaiglobal.com")

    session = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=client, may_write=True
    )
    await manager.on_progress(
        session, QuotaExceededEvent(seq=1, limit=50, used=50, resets_at="2026-07-17T00:00:00Z")
    )
    await _ends_with(manager, session, client, "quota_exceeded")

    assert [e.type for e in session.envelopes] == ["quota_exceeded", "ended"]
    ended = _endeds(session)
    assert len(ended) == 1
    assert ended[0].status == BuildSessionStatus.ENDED  # graceful, NOT failed
    assert ended[0].reason == "quota_exceeded"
    assert ended[0].snapshot_committed is True  # a quota end still saves the work
    assert ended[0].seq == 2  # last_seq + 1


# HERE STOOD `test_escalated_verdict_ends_failed_even_though_its_reason_is_not_build_failed`,
# and it is worth knowing why it is not here any more rather than assuming it was redundant.
# It pinned the trap this unit had to dodge: `status` cannot be re-derived from `reason` when
# there is a verdict, because an `escalated` end is FAILED while "escalated" is not
# `build_failed`, so deriving it downgrades every escalated run to a graceful ENDED. The only
# producer of a verdict was the `run_build` task, which handed a `BuildResult` to `_finalize`;
# it is deleted, and `_do_finalize`'s `result` parameter now has no live caller at all. Every
# reachable ending derives its status from the reason, which is exactly what the surrounding
# tests assert. If a verdict-carrying caller is ever reintroduced, this test comes back with it.


@pytest.mark.parametrize("reason", ["stopped_by_user", "idle_teardown"])
async def test_stop_paths_emit_exactly_one_ended(
    db_session: AsyncSession,
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
    monkeypatch: pytest.MonkeyPatch,
    reason: str,
) -> None:
    # The manager-originated ends (a user stop, and the idle reap that stops with its own
    # reason). Nothing else emits on this path, so the terminal here is entirely the
    # manager's — one frame, post-snapshot, carrying the caller's reason.
    manager = SessionManager()
    order = _spy_order(manager, monkeypatch)
    client = _OrderRecordingSandboxClient(order)
    user, session = await _live_session_stepped(
        manager, db_session, f"r7-{reason}@rvaiglobal.com", client
    )

    await manager.stop(session, client, reason=reason)

    ended = _endeds(session)
    assert len(ended) == 1  # no double emission
    assert ended[0].reason == reason
    assert ended[0].status == BuildSessionStatus.ENDED  # a stop is graceful
    assert ended[0].snapshot_committed is True  # the user's work IS saved on a stop
    assert order == ["snapshot", "teardown", "ended"]
    assert ended[0].seq == 2  # the buffered step (seq 1) + 1
    assert [e.seq for e in session.envelopes] == [1, 2]


async def test_force_end_emits_one_ended_reporting_the_deliberately_skipped_snapshot(
    db_session: AsyncSession,
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The kill switch skips the snapshot BY DESIGN — so the honest frame says
    # snapshot_committed=false. Same field, same truthfulness rule, opposite cause.
    manager = SessionManager()
    order = _spy_order(manager, monkeypatch)
    client = _OrderRecordingSandboxClient(order)
    user, session = await _live_session_stepped(manager, db_session, "r7f@rvaiglobal.com", client)

    await manager.force_end(session, client)

    ended = _endeds(session)
    assert len(ended) == 1
    assert ended[0].reason == "force_ended"
    assert ended[0].snapshot_committed is False  # skipped, and said so
    assert order == ["teardown", "ended"]  # the snapshot never even ran
    assert snapshot_key(session.app_id) not in fake_storage.objects


# HERE STOOD `test_a_raised_run_build_still_emits_exactly_one_failed_ended`. Its premise was
# BRAIN breaking its never-raise invariant, which `_run_and_finalize` caught and turned into a
# derived `build_failed` ending; both the task and that except arm are deleted. The `build_failed`
# reason itself is very much alive — `_terminal_status` still derives FAILED from it and nothing
# else — and `test_abnormal_completion_synthesizes_failed_ended` above drives it through `stop`,
# asserting the same one-terminal, FAILED, gap-free-seq claims this did.


async def test_stop_racing_a_natural_completion_still_emits_exactly_one_ended(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    # The double-emission danger zone: a stop landing while another ending is already
    # finalizing. The single-owner `finalize_task` guard means _do_finalize — and so the
    # terminal emit — happens exactly once, no matter who calls or how many times.
    manager = SessionManager()
    client = FakeSandboxClient()
    user, project_id = await _mk(db_session, "r7h@rvaiglobal.com")
    session = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=client, may_write=True
    )
    await manager.on_progress(session, A_STEP)
    await manager.on_progress(session, PreviewReadyEvent(seq=2, preview_url="https://p/"))

    # Race a completion against a stop AND a redundant second stop.
    await asyncio.gather(
        _ends_with(manager, session, client, COMPLETED),
        manager.stop(session, client),
        manager.stop(session, client),
    )

    assert len(_endeds(session)) == 1
    assert session.terminal_emitted is True
    assert session.status in (BuildSessionStatus.ENDED, BuildSessionStatus.FAILED)
    assert [e.seq for e in session.envelopes] == [1, 2, 3]  # still gap-free


# --- the attachment resolution the manager performed at start ------------
#
# THREE TESTS STOOD HERE and all three are gone with their subject. They pinned
# `resolve_build_attachments` — the thread scan that turned `bial-attachment-ref` markers into
# `BinaryContent` and hung the result on `BuildSession.attachments` for the build prompt. The
# module, the function and the dataclass field are all deleted: a turn's attachments reach the
# sandbox now, not a build's prompt, so there is nothing on this path left to resolve. The
# module-level suite that covered the scan itself
# (`tests/services/build_sessions/test_attachments.py`) went with it.


# --- relaunch a torn-down preview from its snapshot ---------------------------------
#
# Relaunch reuses the restore + lock machinery but NEVER occupies the build slot: it
# registers a READY handle in Redis, releases the per-user lock, and returns synchronously.
# It must restore-or-404 (no blank-template fallback), and never enter `_active_by_user`.


class _RelaunchRecorder(FakeSandboxClient):
    """Records dev_start + wait_ready so a test can prove relaunch DROVE the dev server up,
    not merely restored the bundle (the fresh URL 404s without that step)."""

    def __init__(self) -> None:
        super().__init__()
        self.dev_started: list[str] = []
        self.waited: list[str] = []

    async def dev_start(self, handle, *, cmd=None, cwd=None):
        self.dev_started.append(handle.app_name)
        return await super().dev_start(handle, cmd=cmd, cwd=cwd)

    async def wait_ready(self, handle, *, timeout_s=120.0):
        self.waited.append(handle.app_name)
        return await super().wait_ready(handle, timeout_s=timeout_s)


async def test_relaunch_restores_launches_ready_and_releases_the_lock(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    # The happy path: restore the snapshot, DRIVE the dev server (dev_start + wait_ready),
    # return a live preview URL — then release the lock and never register a live session.
    user, project_id = await _mk(db_session, "r1@rvaiglobal.com")
    manager = SessionManager()
    client = _RelaunchRecorder()
    app_id, _ = await _seed_app_with_bundle(db_session, user, project_id, fake_storage)

    relaunched = await manager.relaunch_preview(db_session, user, project_id, client)

    assert relaunched.app_id == app_id
    name = app_name_for(app_id)
    assert client.restored == [name]
    assert client.dev_started == [name]  # NOT just restored — the dev server was started
    assert client.waited == [name]  # ...and awaited ready (else the URL 404s)
    assert relaunched.preview_url == f"https://{name}.westeurope.azurecontainerapps.io/"
    assert relaunched.restored_from_failed_build is False  # no outcome recorded → no label
    assert client.provisioned == []  # never a blank template
    assert await lock_is_held(fake_redis, user.id) is False  # lock released — slot not held
    assert manager._active_by_user == {}  # never registered as a live session (Decision 6)


async def test_relaunch_does_not_occupy_the_build_slot(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    # The Decision-6 blocker guard: a relaunch must not 409-lock the user's next build. After
    # a relaunch, a normal start for the same user succeeds instead of conflicting.
    user, project_id = await _mk(db_session, "r2@rvaiglobal.com")
    manager = SessionManager()
    await _seed_app_with_bundle(db_session, user, project_id, fake_storage)

    await manager.relaunch_preview(db_session, user, project_id, _RelaunchRecorder())
    assert manager._active_by_user == {}
    assert await lock_is_held(fake_redis, user.id) is False

    # A real turn for the same user now allocates cleanly (no BuildSessionConflictError).
    client = FakeSandboxClient()
    session = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=client, may_write=True
    )
    assert manager.active_session_for(user.id) is session


async def test_relaunch_with_no_snapshot_is_a_dead_end_404(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    # A never-built project has no snapshot: relaunch is a dead end (router 404), NOT a blank
    # provision — an empty template is not a preview of the user's app. The lock is released.
    user, project_id = await _mk(db_session, "r3@rvaiglobal.com")
    manager = SessionManager()
    client = FakeSandboxClient()

    with pytest.raises(NoSnapshotToRelaunchError):
        await manager.relaunch_preview(db_session, user, project_id, client)

    assert client.provisioned == []  # THE invariant: no blank template
    assert client.restored == []
    assert await lock_is_held(fake_redis, user.id) is False
    assert manager._active_by_user == {}


async def test_relaunch_restore_failure_releases_the_lock_and_leaves_no_orphan(
    db_session: AsyncSession,
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
    no_sleep: list[float],
) -> None:
    # A restore that fails every attempt (bundle present, npm blows up) surfaces a clean
    # SnapshotUnavailableError — never a silent success — with the lock released and no
    # session registered. The snapshot is left byte-for-byte intact (never provisioned over).
    user, project_id = await _mk(db_session, "r4@rvaiglobal.com")
    manager = SessionManager()

    class DoomedRestore(FakeSandboxClient):
        async def restore_from_snapshot(
            self,
            user_id,
            app_name,
            *,
            app_env,
            source_key=None,
            kind="build_sandbox",
            shared_project_id=None,
            shared_owner_id=None,
        ):
            raise SandboxError("npm install failed under set -e")

    client = DoomedRestore()
    app_id, _ = await _seed_app_with_bundle(db_session, user, project_id, fake_storage)

    with pytest.raises(SnapshotUnavailableError) as caught:
        await manager.relaunch_preview(db_session, user, project_id, client)

    assert caught.value.app_id == app_id
    assert client.provisioned == []  # no blank template
    assert fake_storage.objects[snapshot_key(app_id)] == b"BUNDLE"  # untouched
    assert await lock_is_held(fake_redis, user.id) is False
    assert manager._active_by_user == {}


async def test_relaunch_tears_down_the_container_if_the_dev_server_never_readies(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    # Restore succeeds but the dev server never comes ready: the freshly-restored container is
    # torn down (no orphan) and the lock released, so the user isn't billed a stuck container.
    user, project_id = await _mk(db_session, "r5@rvaiglobal.com")
    manager = SessionManager()

    class DevNeverReady(FakeSandboxClient):
        async def wait_ready(self, handle, *, timeout_s=120.0):
            raise SandboxNotReadyError("dev server not ready within 120s")

    client = DevNeverReady()
    app_id, _ = await _seed_app_with_bundle(db_session, user, project_id, fake_storage)

    with pytest.raises(SandboxNotReadyError):
        await manager.relaunch_preview(db_session, user, project_id, client)

    assert client.restored == [app_name_for(app_id)]  # a container WAS created...
    assert client.torn_down == [app_name_for(app_id)]  # ...and torn down on the failure
    assert await lock_is_held(fake_redis, user.id) is False
    assert manager._active_by_user == {}


async def test_relaunch_spares_the_container_when_the_lock_release_hits_a_redis_error(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """Regression pin. `release_lock_as_holder` on the `_holding_user_lock` CLEAN-EXIT path
    sits INSIDE the protected region, and its raise is the mechanism that triggers
    compensation — see the comment on that release: "if it fails, compensation still tears the
    container down rather than leaving a live preview behind a lock nobody can release."

    So a guard inside the primitive that returned `False` instead of raising would leave a live
    container orphaned, silently. That guard was briefly added and reverted; this test is what
    makes re-adding it impossible to do quietly."""
    user, project_id = await _mk(db_session, "r-rel@rvaiglobal.com")
    manager = SessionManager()
    client = FakeSandboxClient()
    app_id, _ = await _seed_app_with_bundle(db_session, user, project_id, fake_storage)

    async def the_lua_script_is_down(*args: object, **kwargs: object) -> object:
        raise RedisError("redis is down")

    # Only the compare-and-delete release runs a Lua script on this path (reconcile finds no
    # registry, so `reap_lock` short-circuits on its GET before reaching one).
    monkeypatch_eval = pytest.MonkeyPatch()
    monkeypatch_eval.setattr(fake_redis, "eval", the_lua_script_is_down)
    try:
        with pytest.raises(RedisError):
            await manager.relaunch_preview(db_session, user, project_id, client)
    finally:
        monkeypatch_eval.undo()

    assert client.restored == [app_name_for(app_id)]  # a container WAS created...
    # ...and SURVIVES. See the heartbeat test below for the full reasoning. Note the lock is
    # unreleasable on this path whichever way the container goes (the Lua script is down), so
    # the user is 409'd until the lock's TTL either way — the only thing the old teardown
    # bought them was losing a working container as well.
    assert client.torn_down == []
    assert manager._active_by_user == {}


async def test_relaunch_spares_the_container_when_the_heartbeat_seed_hits_a_redis_error(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """Regression pin, the heartbeat half. `write_heartbeat` inside `relaunch_preview` is
    seeded INSIDE the protected region precisely so that, per the comment there, "if it
    fails, the compensation still tears the container down + releases the lock instead of
    500ing with a live container behind a held lock". A swallow in the primitive would
    return normally and strand the container."""
    user, project_id = await _mk(db_session, "r-hb@rvaiglobal.com")
    manager = SessionManager()
    client = FakeSandboxClient()
    app_id, _ = await _seed_app_with_bundle(db_session, user, project_id, fake_storage)

    # Patched at the manager's own import site rather than on the client: `acquire_lock`
    # writes through the same `redis.set`, so cursing that instead would fail closed into a
    # 409 and never reach the seed. That the real primitive genuinely raises is pinned
    # separately by `test_every_primitive_but_acquire_still_surfaces_redis_errors`; this
    # test owns the OTHER half of the decision — what the manager does when it does.
    async def the_heartbeat_is_cursed(*args: object, **kwargs: object) -> datetime:
        raise RedisError("redis is down")

    monkeypatch_hb = pytest.MonkeyPatch()
    monkeypatch_hb.setattr(
        "src.services.build_sessions.manager.write_heartbeat", the_heartbeat_is_cursed
    )
    try:
        with pytest.raises(RedisError):
            await manager.relaunch_preview(db_session, user, project_id, client)
    finally:
        monkeypatch_hb.undo()

    assert client.restored == [app_name_for(app_id)]
    # NOT TORN DOWN, DELIBERATELY: by the time the heartbeat is seeded, `wait_ready` has
    # returned and the container is up, registered and under a stay — the same state a
    # successful relaunch leaves. The lock is still released regardless (compensation runs
    # either way), so a live container is never left stranded behind a held lock. Destroying
    # a working preview to tidy a hash costs the user their app; leaving it means their retry
    # ATTACHES to it in seconds instead of paying a full restore. The error still surfaces
    # either way.
    assert client.torn_down == []
    assert await lock_is_held(fake_redis, user.id) is False  # the lock IS still given back
    assert manager._active_by_user == {}


async def test_relaunch_while_a_build_is_live_is_409(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    # A live session owns the one-per-user slot; relaunch never pre-empts it. It 409s
    # (BuildSessionConflictError), carrying the live session's id.
    user, project_id = await _mk(db_session, "r6@rvaiglobal.com")
    manager = SessionManager()
    client = FakeSandboxClient()
    await _seed_app_with_bundle(db_session, user, project_id, fake_storage)

    # Allocated and NOT ended — the session holds the slot for as long as the test wants it,
    # which is what the blocking agent used to buy.
    session = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=client, may_write=True
    )

    with pytest.raises(BuildSessionConflictError) as caught:
        await manager.relaunch_preview(db_session, user, project_id, FakeSandboxClient())
    assert caught.value.session_id == session.session_id


async def test_relaunch_404_leaves_no_committed_app_row_and_provisions_no_storage(
    db_session: AsyncSession,
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The snapshot gate runs BEFORE the commit and the storage provision, so a never-built
    # project's 404 neither persists the speculative DRAFT app row nor provisions blob storage.
    user, project_id = await _mk(db_session, "r7@rvaiglobal.com")
    manager = SessionManager()
    provisioned: list[uuid.UUID] = []

    async def _record_provision(app_id: uuid.UUID) -> dict[str, str]:
        provisioned.append(app_id)
        return {}

    monkeypatch.setattr(
        "src.services.build_sessions.manager.provision_app_storage", _record_provision
    )

    with pytest.raises(NoSnapshotToRelaunchError):
        await manager.relaunch_preview(db_session, user, project_id, FakeSandboxClient())

    assert provisioned == []  # storage untouched for an app that was never built
    # The upsert ran but was never committed; production `get_db` rolls it back on the error
    # response. Mirror that rollback here, then prove NOTHING survived it — with the old
    # commit-before-check ordering the phantom DRAFT row would still be here.
    await db_session.rollback()
    count = await db_session.scalar(
        sa.select(sa.func.count())
        .select_from(AppRegistry)
        .where(AppRegistry.project_id == project_id)
    )
    assert count == 0


async def test_relaunch_cancelled_mid_flight_still_tears_down_and_releases_the_lock(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    # Relaunch blocks for minutes (restore + wait_ready), so a dropped request cancels the
    # handler mid-flight. Compensation must run anyway — the fresh container torn down and the
    # lock released, in a task shielded from the cancellation (the `_finalize` pattern) — or a
    # closed tab leaks a billed container and 409-locks the user's next build until the TTL.
    user, project_id = await _mk(db_session, "r8@rvaiglobal.com")
    manager = SessionManager()
    app_id, _ = await _seed_app_with_bundle(db_session, user, project_id, fake_storage)

    hung = asyncio.Event()

    class HangsAtReady(FakeSandboxClient):
        async def wait_ready(self, handle, *, timeout_s=120.0):
            hung.set()
            await asyncio.Event().wait()  # parks forever — only a cancel gets out
            raise AssertionError("unreachable")

    client = HangsAtReady()
    task = asyncio.create_task(manager.relaunch_preview(db_session, user, project_id, client))
    await hung.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    name = app_name_for(app_id)
    assert client.restored == [name]  # a container WAS created before the cancel...
    assert client.torn_down == [name]  # ...and compensation tore it down anyway
    assert await lock_is_held(fake_redis, user.id) is False  # lock released — no wedged slot
    assert manager._active_by_user == {}


async def test_relaunch_after_a_failed_build_flags_last_saved_version(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    # `_do_finalize` snapshots pass and fail alike, so after a FAILED build the newest
    # snapshot is the last SAVED state, not that build's intent — the flag drives the portal's
    # "Relaunch last saved version" label. A later CLEAN outcome clears it again.
    user, project_id = await _mk(db_session, "r9@rvaiglobal.com")
    manager = SessionManager()
    await _seed_app_with_bundle(db_session, user, project_id, fake_storage)
    conv = await ConversationFactory.create(
        db_session, user.id, project_id=project_id, kind=ChatKind.BUILD
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

    relaunched = await manager.relaunch_preview(db_session, user, project_id, _RelaunchRecorder())
    assert relaunched.restored_from_failed_build is True

    await write_build_outcome(
        db_session,
        user_id=user.id,
        conversation_id=conv.id,
        session_id=uuid.uuid4(),
        status=BuildSessionStatus.ENDED,
        preview_url=None,
        snapshot_committed=True,
        reason="completed",
    )
    again = await manager.relaunch_preview(db_session, user, project_id, _RelaunchRecorder())
    assert again.restored_from_failed_build is False  # only the NEWEST outcome speaks


async def test_relaunch_grants_a_stay_of_execution(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    # A relaunched preview releases the lock and nothing renews its heartbeat, so the
    # registry's stay is the ONLY thing that owns its container's lifetime. Without it the
    # background sweep reaps a preview the user is still reading; with it the lease is
    # explicit and bounded. (The shared `FakeSandboxClient` hydrates the registry hash the
    # stay is stamped onto, exactly as the real client does — without that the grant's
    # existence guard skips and every assertion below would be vacuously "absent".)
    user, project_id = await _mk(db_session, "r9@rvaiglobal.com")
    manager = SessionManager()
    await _seed_app_with_bundle(db_session, user, project_id, fake_storage)

    await manager.relaunch_preview(db_session, user, project_id, _RelaunchRecorder())

    reg = await read_registry(fake_redis, user.id)
    assert reg is not None
    deadline = datetime.fromisoformat(reg[REGISTRY_FIELD_PREVIEW_STAY_UNTIL])
    assert deadline > datetime.now(UTC)  # a FUTURE deadline, not a stamped-and-lapsed field
    assert deadline <= datetime.now(UTC) + timedelta(seconds=RELAUNCH_PREVIEW_STAY_SECONDS)
    assert await stay_of_execution_is_current(fake_redis, user.id) is True


class _SweepingDuringProvision(_RelaunchRecorder):
    """Runs the BACKGROUND SWEEP at the exact mid-relaunch instant — after the container
    exists (and its registry hash with it) but before the dev server is up and ready. Also
    records the coordination state it observed there, so the test can prove the sweep was
    genuinely looking at a reapable-shaped user rather than passing on a technicality."""

    def __init__(self, redis: aioredis.Redis, user_id: uuid.UUID) -> None:
        super().__init__()
        self._redis = redis
        self._user_id = user_id
        self.reaped_mid_provision: int | None = None
        self.state_at_sweep: dict[str, bool] = {}

    async def dev_start(self, handle, *, cmd=None, cwd=None):
        self.state_at_sweep = {
            "registry": await read_registry(self._redis, self._user_id) is not None,
            "lock": await lock_is_held(self._redis, self._user_id),
            "heartbeat": await heartbeat_is_alive(self._redis, self._user_id),
        }
        self.reaped_mid_provision = (await sweep_all(self._redis, self)).reaped
        return await super().dev_start(handle, cmd=cmd, cwd=cwd)


async def test_a_sweep_during_the_relaunch_provision_window_does_not_reap_it(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    # THE PROVISION WINDOW. The registry hash is written at container-CREATE, deep inside
    # `_restore_or_bust` — minutes before `dev_start` + `wait_ready` finish. Granting the
    # stay only at the end leaves that whole window naked, and the state during it is
    # precisely the state `reconcile_user` calls reapable:
    #
    #   registry PRESENT · lock HELD · heartbeat ABSENT · stay ABSENT
    #
    # because the guard is an AND (`lock_is_held AND heartbeat_is_alive`), so lock-held-
    # without-a-beat falls straight through. `live_users` does not save it either: a
    # relaunch never enters `_active_by_user` by design (Decision 6). So a sweep landing
    # here tore down the container the relaunch was still building — and the request still
    # returned 200, handing the user a preview URL pointing at nothing.
    #
    # Seeding the heartbeat earlier is NOT the fix: HEARTBEAT_TTL_SECONDS is 90 s and
    # `wait_ready` waits up to 120 s, so the beat can lapse mid-wait. The lease has to
    # start when the registry does.
    user, project_id = await _mk(db_session, "r11@rvaiglobal.com")
    manager = SessionManager()
    app_id, _ = await _seed_app_with_bundle(db_session, user, project_id, fake_storage)

    client = _SweepingDuringProvision(fake_redis, user.id)
    relaunched = await manager.relaunch_preview(db_session, user, project_id, client)

    # The sweep saw the naked-window shape — registry visible, lock held, NO heartbeat —
    # i.e. it reached the stay check rather than bailing out earlier for some other reason.
    assert client.state_at_sweep == {"registry": True, "lock": True, "heartbeat": False}
    assert client.reaped_mid_provision == 0  # ...and spared it anyway
    assert client.torn_down == []  # the half-built container survived
    name = app_name_for(app_id)
    assert relaunched.preview_url == f"https://{name}.westeurope.azurecontainerapps.io/"
    # The preview is live AND leased at the end of the call — not merely un-reaped by luck.
    assert await stay_of_execution_is_current(fake_redis, user.id) is True


async def test_the_next_real_start_reaps_a_relaunched_preview_through_its_stay(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    # THE CRUX, pinned AT THE CALL SITE THAT DECIDES IT. `reaper.reconcile_user` takes
    # `honor_stay` as a keyword with a default, and every other test calls the helper
    # directly — which pins the DEFAULT, not the argument `manager` actually passes. Forcing
    # `honor_stay=True` at the manager's call site therefore left the whole suite green
    # while re-opening the exact orphan-the-container regression the asymmetry exists to
    # prevent. (Patching `reaper.reconcile_user` does not even reach it: `manager` imports
    # the function BY VALUE.)
    #
    # So drive the real thing end to end: relaunch a preview of project A (which grants a
    # live 30-minute lease), then allocate for project B for the SAME user. The new session
    # needs the one-per-user sandbox slot, so reconcile-on-start must reap THROUGH the
    # unexpired stay. Sparing it would leave A's container running while B registers its own
    # over that hash — the container orphaned, invisible to the registry-only sweep forever
    # after.
    user, project_a = await _mk(db_session, "r12@rvaiglobal.com")
    project_b = (await ProjectFactory.create(db_session, user.id)).id
    manager = SessionManager()
    preview_app_id, _ = await _seed_app_with_bundle(db_session, user, project_a, fake_storage)
    client = _RelaunchRecorder()

    await manager.relaunch_preview(db_session, user, project_a, client)

    preview_app_name = app_name_for(preview_app_id)
    reg = await read_registry(fake_redis, user.id)
    assert reg is not None
    assert reg[REGISTRY_FIELD_APP_NAME] == preview_app_name
    # A genuinely CURRENT lease — the sweep would spare this container right now.
    assert datetime.fromisoformat(reg[REGISTRY_FIELD_PREVIEW_STAY_UNTIL]) > datetime.now(UTC)
    assert await stay_of_execution_is_current(fake_redis, user.id) is True
    assert (await sweep_all(fake_redis, FakeSandboxClient())).reaped == 0  # ...proven, not assumed

    # The session is left LIVE (never ended), so the registry can be read while it is still
    # B's — an ended session's finalize deletes the hash outright.
    session = await manager.ensure_sandbox(
        db_session, user, project_b, sandbox_client=client, may_write=True
    )

    # (a) the PREVIEW's container was actually torn down — reaped, never orphaned.
    assert preview_app_name in client.torn_down
    # (b) ...and the registry now names the NEW SESSION's app. Project B is a different app,
    #     so this is a real assertion and not a tautology about a shared app_name.
    build_app_name = app_name_for(session.app_id)
    assert build_app_name != preview_app_name
    reg_after = await read_registry(fake_redis, user.id)
    assert reg_after is not None
    assert reg_after[REGISTRY_FIELD_APP_NAME] == build_app_name
    # The new session inherited NO lease from the preview it displaced (see the fake client's
    # `_write_registry`): its container is reapable the moment its own liveness lapses.
    assert REGISTRY_FIELD_PREVIEW_STAY_UNTIL not in reg_after
    assert await stay_of_execution_is_current(fake_redis, user.id) is False

    await manager.stop(session, client)


# --- the attach arm's own seams (the ACA call counts live in test_relaunch.py) ---------
#
# `test_relaunch.py` owns "no container was created or destroyed", which is only observable
# under the real client. What is only observable HERE is what the manager does around the
# attach: whether it skips the birth env, whether a post-attach failure destroys a container
# it did not create, and whether `dev_start` fails open on one arm and closed on the other.


async def _the_container_is_already_up(
    client: FakeSandboxClient,
    redis: aioredis.Redis,
    user_id: uuid.UUID,
    app_id: uuid.UUID,
    *,
    state: str = REGISTRY_STATE_READY,
) -> SandboxHandle:
    """Put a healthy, READY container for `app_id` in front of the manager: the registry
    hash the real client writes at container-create, plus a handle `attach_existing` can hand
    back. `state` is a parameter because `ending` is the interesting negative case."""
    app_name = app_name_for(app_id)
    fqdn = f"{app_name}.westeurope.azurecontainerapps.io"
    handle = SandboxHandle(
        fqdn=fqdn,
        token=f"tok-{app_name}",
        app_name=app_name,
        preview_url=f"https://{fqdn}/",
        ready=True,
    )
    await redis.hset(
        registry_key(user_id),
        mapping={
            REGISTRY_FIELD_APP_NAME: app_name,
            REGISTRY_FIELD_FQDN: fqdn,
            REGISTRY_FIELD_TOKEN_REF: f"ref-{app_name}",
            REGISTRY_FIELD_CREATED_AT: datetime.now(UTC).isoformat(),
            REGISTRY_FIELD_STATE: state,
        },
    )
    client.attach_handle = handle
    return handle


async def test_relaunch_attaches_the_live_container_instead_of_rebuilding_it(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    # At the manager seam: same app, registry READY → attach, and the dev server is still
    # DRIVEN (a container can be up with a dead dev server — the attach is not a promise that
    # anything is serving, `wait_ready` is).
    user, project_id = await _mk(db_session, "r13@rvaiglobal.com")
    manager = SessionManager()
    client = _RelaunchRecorder()
    app_id, _ = await _seed_app_with_bundle(db_session, user, project_id, fake_storage)
    live = await _the_container_is_already_up(client, fake_redis, user.id, app_id)

    relaunched = await manager.relaunch_preview(db_session, user, project_id, client)

    assert client.restored == []  # nothing rebuilt...
    assert client.provisioned == []
    assert client.torn_down == []  # ...and nothing demolished to get there
    assert relaunched.preview_url == live.preview_url
    assert client.dev_started == [live.app_name]  # the dev server was still driven
    assert client.waited == [live.app_name]
    assert await lock_is_held(fake_redis, user.id) is False  # Decision 6 unchanged
    assert manager._active_by_user == {}


async def test_relaunch_never_attaches_to_a_container_that_is_already_ending(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    # `ending` means the reaper has already committed to destroying it. Attaching would race
    # that teardown AND skip the cleanup, so we would pay the restore anyway — with an orphan.
    user, project_id = await _mk(db_session, "r14@rvaiglobal.com")
    manager = SessionManager()
    client = _RelaunchRecorder()
    app_id, _ = await _seed_app_with_bundle(db_session, user, project_id, fake_storage)
    await _the_container_is_already_up(
        client, fake_redis, user.id, app_id, state=REGISTRY_STATE_ENDING
    )

    await manager.relaunch_preview(db_session, user, project_id, client)

    name = app_name_for(app_id)
    assert client.torn_down == [name]  # the dying container was reaped...
    assert client.restored == [name]  # ...and a fresh one restored


async def test_a_post_attach_readiness_failure_spares_the_attached_container(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """THE COMPENSATION HAZARD, in its general form. `_compensate_lock_and_container` tears
    down `scope.handle` on ANY body failure — including a `CancelledError` from a dropped
    request — and that was safe while relaunch only ever assigned a container it had just
    created. The moment it assigns a PRE-EXISTING one, every post-attach failure becomes
    destructive on a container this request did not create. Without `_LockScope.attached`,
    relaunch destroys the healthy container it exists to preserve."""
    user, project_id = await _mk(db_session, "r15@rvaiglobal.com")
    manager = SessionManager()

    class DevNeverReadies(_RelaunchRecorder):
        async def wait_ready(self, handle, *, timeout_s=120.0):
            raise SandboxNotReadyError("dev server not ready within 120s")

    client = DevNeverReadies()
    app_id, _ = await _seed_app_with_bundle(db_session, user, project_id, fake_storage)
    await _the_container_is_already_up(client, fake_redis, user.id, app_id)

    # No longer raises: the attach arm fails open.
    relaunched = await manager.relaunch_preview(db_session, user, project_id, client)

    assert relaunched.ready is False, "an app that never served must not be reported as ready"
    assert relaunched.preview_url, "…but the URL still ships — the pane owns the labelled wait"
    assert client.torn_down == []  # the container we attached to is STILL RUNNING
    assert await lock_is_held(fake_redis, user.id) is False  # the lock IS ours to give back
    assert manager._active_by_user == {}


async def test_a_restored_container_that_never_readies_is_still_torn_down(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    # The other half of the same decision, kept honest: `attached` must not become a blanket
    # amnesty. A container this request DID create and could not bring up is still ours to
    # clean up — the same assertion `test_relaunch_tears_down_the_container_if_the_dev_server_
    # never_readies` makes, restated here as the mutation guard on the new flag.
    user, project_id = await _mk(db_session, "r16@rvaiglobal.com")
    manager = SessionManager()

    class DevNeverReadies(_RelaunchRecorder):
        async def wait_ready(self, handle, *, timeout_s=120.0):
            raise SandboxNotReadyError("dev server not ready within 120s")

    client = DevNeverReadies()  # no registry seeded → nothing to attach to
    app_id, _ = await _seed_app_with_bundle(db_session, user, project_id, fake_storage)

    with pytest.raises(SandboxNotReadyError):
        await manager.relaunch_preview(db_session, user, project_id, client)

    assert client.torn_down == [app_name_for(app_id)]


async def test_dev_start_refused_on_an_attached_container_is_logged_and_ignored(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    # On the attach arm `dev_start` is an optimization against a container that is very
    # probably already serving, and its 409-unowned-server arm raises `SandboxError` — which
    # unguarded would reach compensation and destroy that container. Fail open, then let
    # `wait_ready` be the actual gate.
    user, project_id = await _mk(db_session, "r17@rvaiglobal.com")
    manager = SessionManager()

    class DevStartRefuses(_RelaunchRecorder):
        async def dev_start(self, handle, *, cmd=None, cwd=None):
            self.dev_started.append(handle.app_name)
            raise SandboxError("dev/start reported 409 but the server is not running")

    client = DevStartRefuses()
    app_id, _ = await _seed_app_with_bundle(db_session, user, project_id, fake_storage)
    live = await _the_container_is_already_up(client, fake_redis, user.id, app_id)

    relaunched = await manager.relaunch_preview(db_session, user, project_id, client)

    assert relaunched.preview_url == live.preview_url  # the preview still framed
    assert client.dev_started == [live.app_name]  # it was tried...
    assert client.waited == [live.app_name]  # ...and readiness still decided the answer
    assert client.torn_down == []  # nothing destroyed for the sin of already serving


async def test_dev_start_failing_on_the_restore_arm_still_fails_the_relaunch(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    # The fail-open above is scoped to ATTACH, deliberately. A freshly restored container has
    # nothing serving on it, so swallowing `dev_start` there would return 200 with a preview
    # URL that 404s — the exact "successful build, blank page" failure. Widen the guard to
    # both arms and this goes red.
    user, project_id = await _mk(db_session, "r18@rvaiglobal.com")
    manager = SessionManager()

    class DevStartRefuses(_RelaunchRecorder):
        async def dev_start(self, handle, *, cmd=None, cwd=None):
            self.dev_started.append(handle.app_name)
            raise SandboxError("dev/start reported 409 but the server is not running")

    client = DevStartRefuses()  # no registry seeded → the restore arm
    app_id, _ = await _seed_app_with_bundle(db_session, user, project_id, fake_storage)

    with pytest.raises(SandboxError):
        await manager.relaunch_preview(db_session, user, project_id, client)

    assert client.restored == [app_name_for(app_id)]
    assert client.torn_down == [app_name_for(app_id)]  # ours to create, ours to clean up


async def test_an_attached_relaunch_mints_no_fresh_blob_sas(
    db_session: AsyncSession,
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The attach arm builds no env at all, which is `_resolve_sandbox`'s attach semantics.

    Pinned rather than assumed, because it has a consequence: relaunching used to re-mint the
    session SAS every time, and relaunching is now cheap, so rotation cadence on this path goes
    to zero. Deliberate."""
    user, project_id = await _mk(db_session, "r19@rvaiglobal.com")
    manager = SessionManager()
    client = _RelaunchRecorder()
    app_id, _ = await _seed_app_with_bundle(db_session, user, project_id, fake_storage)
    await _the_container_is_already_up(client, fake_redis, user.id, app_id)

    minted: list[uuid.UUID] = []

    async def _record_mint(app: uuid.UUID) -> dict[str, str]:
        minted.append(app)
        return {"BIAL_BLOB_SAS": "fresh"}

    monkeypatch.setattr("src.services.build_sessions.manager.provision_app_storage", _record_mint)

    await manager.relaunch_preview(db_session, user, project_id, client)

    assert minted == []  # no SAS minted...
    assert client.restore_env is None  # ...because no birth env was built at all
    assert client.provision_env is None


async def test_a_relaunch_warms_the_route_before_it_hands_back_a_preview_url(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """`wait_ready` returning does not mean THIS route has been built. Relaunch hands its
    `previewUrl` straight back to a browser that frames it immediately, so the platform pays
    the first compile here rather than leaving the citizen to stare at it."""
    user, project_id = await _mk(db_session, "r22@rvaiglobal.com")
    manager = SessionManager()
    client = _RelaunchRecorder()
    app_id, _ = await _seed_app_with_bundle(db_session, user, project_id, fake_storage)
    live = await _the_container_is_already_up(client, fake_redis, user.id, app_id)

    relaunched = await manager.relaunch_preview(db_session, user, project_id, client)

    assert client.warmed == [live.preview_url], "warmed the app root exactly once"
    assert relaunched.preview_url == live.preview_url


async def test_a_relaunch_survives_a_warm_request_that_cannot_be_served(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """★ The warm request is an optimization bolted onto a path that already worked. A route
    that 500s — or one the helper could not reach at all — must still produce a 200 with a usable
    preview URL, and must never leave a healthy attached container torn down behind it."""
    user, project_id = await _mk(db_session, "r23@rvaiglobal.com")
    manager = SessionManager()
    client = _RelaunchRecorder()
    client.warm_status = None  # the helper's "I could not reach it" answer
    app_id, _ = await _seed_app_with_bundle(db_session, user, project_id, fake_storage)
    live = await _the_container_is_already_up(client, fake_redis, user.id, app_id)

    relaunched = await manager.relaunch_preview(db_session, user, project_id, client)

    assert client.warmed, "guard the premise: the failing warm request was actually attempted"
    assert relaunched.preview_url == live.preview_url
    assert client.torn_down == [], "a warm request may never cost the container"


async def test_a_container_that_never_readies_is_never_condemned_for_it(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """A readiness timeout is a statement about the generated APP, not the CONTAINER: any root
    route slower than the supervisor's read timeout reports un-ready forever, while the container
    stays healthy and still holds the citizen's work.

    So it KEEPS its READY state and keeps winning the attach arm. Marking the registry `ending`
    instead sends the next press down the RESTORE arm, which tears the live container down before
    pulling the last SAVED bundle — every unsaved edit gone. The wedge that mark reached for is
    closed by the lease we decline to grant before the wait, asserted by its sibling below."""
    user, project_id = await _mk(db_session, "r24@rvaiglobal.com")
    manager = SessionManager()

    class DevNeverReadies(_RelaunchRecorder):
        async def wait_ready(self, handle, *, timeout_s=120.0):
            raise SandboxNotReadyError("dev server not ready within 120s")

    client = DevNeverReadies()
    app_id, _ = await _seed_app_with_bundle(db_session, user, project_id, fake_storage)
    await _the_container_is_already_up(client, fake_redis, user.id, app_id)

    relaunched = await manager.relaunch_preview(db_session, user, project_id, client)

    assert relaunched.ready is False, "the pane must be told the app is not serving yet"
    assert client.torn_down == [], "still not ours to destroy — that part was always right"
    registry = await read_registry(fake_redis, user.id)
    assert registry is not None and registry[REGISTRY_FIELD_STATE] == REGISTRY_STATE_READY, (
        "a slow app must never condemn a live container: `ending` sends the next press down the "
        "restore arm, which tears this container down and rolls the citizen back to their last "
        "save. Losing a citizen's unsaved work is the worst thing this system can do."
    )


async def test_a_second_press_after_a_slow_app_attaches_instead_of_eating_the_workspace(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """★ END TO END — the regression this branch shipped and then reproduced against real
    Azure. The scenario is two clicks by a confused citizen, and the ONLY thing standing between
    them and losing their unsaved work is that the second press takes the ATTACH arm.

    Restore is not a gentler fallback: `restore_from_snapshot` tears the live container down
    before pulling the last SAVED bundle, so it is a rollback to the last save with no notice on
    screen. Anything that pushes a still-healthy container onto that path is a data-loss bug,
    which is why this asserts on `restored`/`torn_down` and not merely on the registry state."""
    user, project_id = await _mk(db_session, "r24b@rvaiglobal.com")
    manager = SessionManager()

    class DevNeverReadies(_RelaunchRecorder):
        async def wait_ready(self, handle, *, timeout_s=120.0):
            raise SandboxNotReadyError("dev server not ready within 120s")

    client = DevNeverReadies()
    app_id, _ = await _seed_app_with_bundle(db_session, user, project_id, fake_storage)
    live = await _the_container_is_already_up(client, fake_redis, user.id, app_id)

    first = await manager.relaunch_preview(db_session, user, project_id, client)
    second = await manager.relaunch_preview(db_session, user, project_id, client)

    assert first.ready is False and second.ready is False
    # The same live container both times — never a fresh one built over the citizen's tree.
    assert first.preview_url == live.preview_url
    assert second.preview_url == live.preview_url
    assert client.restored == [], "a slow root route must never trigger a snapshot rollback"
    assert client.torn_down == [], "the container holding the unsaved work is still running"
    assert client.provisioned == [], "and no replacement was built for it"


async def test_a_wait_that_dies_any_other_way_still_does_not_renew_the_reprieve(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """★ THE TRAP'S SIBLINGS. `wait_ready` raises more than `SandboxNotReadyError`: a
    persistently non-200 `/dev/status`, a malformed body and an unreachable supervisor all
    surface as a bare `SandboxError`, and a dropped request arrives as `CancelledError`. The
    mark-ending arm names exactly one, so every other exit skipped it and still bought the
    doomed container another full 30-minute reprieve. The fix is the ABSENCE of a grant, so
    this asserts one: the pre-existing lease is left as found, and the container keeps its
    READY state because a supervisor blip must not commit the reaper to destroying work.
    Mutation check: drop the `if not attached:` guard on the pre-wait grant and the stamp moves."""
    user, project_id = await _mk(db_session, "r26@rvaiglobal.com")
    manager = SessionManager()

    class TheSupervisorIsUnreachable(_RelaunchRecorder):
        async def wait_ready(self, handle, *, timeout_s=120.0):
            raise SandboxError("supervisor dev/status request failed")

    client = TheSupervisorIsUnreachable()
    app_id, _ = await _seed_app_with_bundle(db_session, user, project_id, fake_storage)
    await _the_container_is_already_up(client, fake_redis, user.id, app_id)
    # The reprieve it is already living under, from whoever put it there (a previous relaunch,
    # or the pardon a completed build granted it). Nearly spent, which is the interesting case:
    # a renewal here is what turns a doomed container into an immortal one.
    nearly_spent = (datetime.now(UTC) + timedelta(seconds=30)).isoformat()
    await fake_redis.hset(registry_key(user.id), REGISTRY_FIELD_PREVIEW_STAY_UNTIL, nearly_spent)

    with pytest.raises(SandboxError):
        await manager.relaunch_preview(db_session, user, project_id, client)

    reg = await read_registry(fake_redis, user.id)
    assert reg is not None
    assert reg[REGISTRY_FIELD_PREVIEW_STAY_UNTIL] == nearly_spent, (
        "a failed attach must not renew the container's lease — that is the trap"
    )
    assert reg[REGISTRY_FIELD_STATE] == REGISTRY_STATE_READY, (
        "…and it must not condemn it either: `ending` is the reaper's committed-to-destroy "
        "marker, and a supervisor blip is not proof the workspace is expendable"
    )
    assert client.torn_down == []


async def test_a_successful_attach_still_earns_the_container_a_fresh_reprieve(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """The other half of the same decision. Moving the attach arm's grant AFTER `wait_ready`
    must not leave an attached preview unleased: it holds no lock and renews no heartbeat, so
    the stay is still the only thing standing between the container and the sweep — it is now
    simply EARNED by answering rather than spent on the hope that it will."""
    user, project_id = await _mk(db_session, "r27@rvaiglobal.com")
    manager = SessionManager()
    client = _RelaunchRecorder()
    app_id, _ = await _seed_app_with_bundle(db_session, user, project_id, fake_storage)
    await _the_container_is_already_up(client, fake_redis, user.id, app_id)
    lapsed = (datetime.now(UTC) - timedelta(seconds=1)).isoformat()
    await fake_redis.hset(registry_key(user.id), REGISTRY_FIELD_PREVIEW_STAY_UNTIL, lapsed)

    await manager.relaunch_preview(db_session, user, project_id, client)

    assert client.restored == []  # guard the premise: this really was the attach arm
    assert await stay_of_execution_is_current(fake_redis, user.id) is True


async def test_an_attached_relaunch_never_claims_it_restored_the_last_saved_version(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """★ A relaunch that ATTACHED restored nothing, so it must claim nothing. The flag drives
    the portal's "last saved version" banner, and the container the attach arm hands back has
    been running since before this request — its workspace may hold edits newer than any
    snapshot. Telling that user they are looking at their last SAVED version is the one thing
    the banner must never say, and the newest recorded outcome being FAILED says nothing at all
    about a live tree."""
    user, project_id = await _mk(db_session, "r28@rvaiglobal.com")
    manager = SessionManager()
    client = _RelaunchRecorder()
    app_id, _ = await _seed_app_with_bundle(db_session, user, project_id, fake_storage)
    await _the_container_is_already_up(client, fake_redis, user.id, app_id)
    conv = await ConversationFactory.create(
        db_session, user.id, project_id=project_id, kind=ChatKind.BUILD
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

    relaunched = await manager.relaunch_preview(db_session, user, project_id, client)

    assert client.restored == []  # guard the premise: the attach arm ran
    assert relaunched.restored_from_failed_build is False


async def test_a_residual_lock_does_not_409_the_recovery_button(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """★ Skipping the reconcile spared the container — but reconcile also `reap_lock`ed,
    and that was the only thing clearing a dead process's residual lock on this path. After a
    control-plane restart the lock outlives its owner, so relaunch answered 409 (naming no
    session at all) until the sweep caught up minutes later. Spare the CONTAINER, not the
    lock: the certified-dead facts say any lock still here is residue."""
    user, project_id = await _mk(db_session, "r25@rvaiglobal.com")
    manager = SessionManager()
    client = _RelaunchRecorder()
    app_id, _ = await _seed_app_with_bundle(db_session, user, project_id, fake_storage)
    live = await _the_container_is_already_up(client, fake_redis, user.id, app_id)
    # A dead process's leftovers: its lock token survived, its process did not.
    await fake_redis.set(lock_key(user.id), "a-token-nobody-holds-any-more")

    relaunched = await manager.relaunch_preview(db_session, user, project_id, client)

    assert relaunched.preview_url == live.preview_url
    assert client.restored == [] and client.torn_down == []  # still the fast attach arm


# --- the switched-off app: both doors refuse, and Save does not ---------------------------
#
# THE REFUSAL ITSELF IS ASSERTED IN `test_appdata.py`, at `resolve_app_for_project` — the
# site that makes the decision. What these pin is the CALL GRAPH the decision relies on:
# that both doors into a container really do come through that one function, so there is no
# third door quietly holding a copy of the check. Plus the one thing that must NOT be
# refused.


async def _a_switched_off_app(
    db: AsyncSession, user: User, project_id: uuid.UUID, store: FakeStorage
) -> uuid.UUID:
    """A project whose app an administrator has switched off, with a saved bundle behind it
    (so `relaunch_preview` reaches the resolve rather than stopping at the snapshot gate)."""
    app_id, _ = await _seed_app_with_bundle(db, user, project_id, store)
    await db.execute(
        sa.update(AppRegistry).where(AppRegistry.id == app_id).values(status=AppStatus.DISABLED)
    )
    await db.commit()
    return app_id


async def test_a_turn_of_any_kind_is_refused_while_the_app_is_switched_off(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """`ensure_sandbox` is the door EVERY turn kind uses — Ask, Plan and Build alike, since
    `services/turns/engine.py` routes all of them through it — so this one refusal is the
    whole of "sending a turn is refused while switched off"."""
    user, project_id = await _mk(db_session, "off-turn@rvaiglobal.com")
    await _a_switched_off_app(db_session, user, project_id, fake_storage)
    manager = SessionManager()
    client = FakeSandboxClient()

    with pytest.raises(AppApiError) as exc:
        await manager.ensure_sandbox(
            db_session, user, project_id, sandbox_client=client, may_write=True
        )

    assert exc.value.status_code == 409
    assert exc.value.code == APP_SWITCHED_OFF_CODE
    # FAILS CLOSED: nothing allocated, nothing left holding the user's one slot.
    assert client.provisioned == [] and client.restored == []
    assert await lock_is_held(fake_redis, user.id) is False
    assert manager.active_session_for(user.id) is None


async def test_starting_the_sandbox_is_refused_while_the_app_is_switched_off(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """`relaunch_preview` is the EXPLICIT START CONTROL — the button the citizen presses to
    bring their workspace up — and the second of the two doors. It has a saved bundle, so the
    refusal below is the status gate and not the snapshot gate answering for it."""
    user, project_id = await _mk(db_session, "off-relaunch@rvaiglobal.com")
    await _a_switched_off_app(db_session, user, project_id, fake_storage)
    manager = SessionManager()
    client = _RelaunchRecorder()

    with pytest.raises(AppApiError) as exc:
        await manager.relaunch_preview(db_session, user, project_id, client)

    assert exc.value.status_code == 409
    assert client.restored == [] and client.provisioned == []
    assert await lock_is_held(fake_redis, user.id) is False


async def test_save_still_succeeds_while_the_app_is_switched_off(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """★ SAVE IS NOT GATED, AND THIS TEST IS HERE TO STOP SOMEONE "FINISHING THE JOB".

    If you are reading this because the switched-off enforcement looks incomplete: it is not.
    Save is deliberately outside it. `save_project_snapshot` is the only thing that writes a
    citizen's work to durable storage and containers are ephemeral — the reaper destroys idle
    ones — so refusing a save in the one window where it matters (an administrator flips the
    switch while the owner holds unsaved work in a live container) does not contain anything.
    It PERMANENTLY DESTROYS that work. That is the same harm
    `_refuse_if_reclaim_would_destroy_work` exists to prevent.

    The accepted trade is that a disabled app's saved bundle may advance by one commit, and
    nothing consumes it: publish still refuses, approval pins a submission rather than the
    saved head, and the app is off the live roster and out of the catalog.

    Structurally this holds because Save reads its app id through `_existing_app_id`, never
    through `resolve_app_for_project` — so wiring the gate into Save would take a deliberate
    edit. Making that edit turns this test red.
    """
    user, project_id = await _mk(db_session, "off-save@rvaiglobal.com")
    app_id = await _a_switched_off_app(db_session, user, project_id, fake_storage)
    manager = SessionManager()
    client = FakeSandboxClient()
    # A live container to save FROM — the state the harm above describes: the switch was
    # flipped while the owner's workspace was still up with unsaved work in it.
    client.attach_handle = SandboxHandle(
        fqdn="live.example",
        token="tok",
        app_name=app_name_for(app_id),
        preview_url="https://live.example/",
        ready=True,
    )
    await fake_redis.hset(
        registry_key(user.id),
        mapping={
            REGISTRY_FIELD_APP_NAME: app_name_for(app_id),
            REGISTRY_FIELD_FQDN: "live.example",
            REGISTRY_FIELD_TOKEN_REF: "ref",
            REGISTRY_FIELD_CREATED_AT: "2026-09-07T00:00:00+00:00",
            REGISTRY_FIELD_STATE: REGISTRY_STATE_READY,
        },
    )

    outcome = await manager.save_project_snapshot(
        db_session, user, project_id, sandbox_client=client
    )

    assert outcome.app_id == app_id
    # THE WORK REACHED DURABLE STORAGE. Not "no exception was raised" — a refusal that
    # returned quietly would pass that, and the citizen's work would still be gone.
    assert snapshot_key(app_id) in fake_storage.objects
