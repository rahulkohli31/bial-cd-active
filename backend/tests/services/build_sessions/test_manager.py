"""U5 — the SessionManager lifecycle: start (provision/attach/restore + launch), the
progress channel, the single-owner end sequence, stop/force-end, and start compensation.
Driven by FakeSandboxClient (mock C1) + FakeBrain (mock C7) + fakeredis + fake storage +
the `:5432` test DB.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta

import pytest
import redis.asyncio as aioredis
import sqlalchemy as sa
from pydantic import SecretStr
from pydantic_ai import BinaryContent
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.v1.build_sessions.schemas import (
    RELAUNCH_PREVIEW_STAY_SECONDS,
    BuildResult,
    BuildSessionStatus,
    EndedEvent,
    PreviewReadyEvent,
    PreviewReconnectingEvent,
    ProgressEnvelope,
    QuotaExceededEvent,
    StepEvent,
)
from src.config import settings
from src.db.models.app_registry import AppRegistry
from src.db.models.attachment import Attachment
from src.db.models.conversation import ChatKind
from src.db.models.user import User
from src.services.build_sessions.appdata import build_app_env, resolve_app_for_project
from src.services.build_sessions.attachments import BuildAttachmentError
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
from tests.factories import (
    ConversationFactory,
    MessageFactory,
    ProjectFactory,
    UserFactory,
)
from tests.fakes import FakeBrain, FakeSandboxClient, FakeStorage, a_sandbox_name


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


class BlockingBrain:
    """A brain that emits one step, then blocks until `release()` — keeps a session live
    so concurrency / stop tests aren't racing a fast completion. `stepped` fires AFTER the
    step is buffered, so a test can deterministically stop with a known `last_seq`.
    Emits no terminal `ended`: that frame is SESSION-API's alone (R7)."""

    def __init__(self) -> None:
        self._gate = asyncio.Event()
        self.stepped = asyncio.Event()

    def release(self) -> None:
        self._gate.set()

    async def __call__(self, session_id, user_id, sandbox_client, on_progress) -> BuildResult:
        await on_progress(StepEvent(seq=1, name="scaffold", label="Scaffolding", state="started"))
        self.stepped.set()
        await self._gate.wait()
        return BuildResult(
            status=BuildSessionStatus.ENDED,
            reason="completed",
            app_id=uuid.uuid4(),
            preview_url=None,
            last_seq=1,
            snapshot_committed=False,
        )


async def _mk(db: AsyncSession, email: str) -> tuple[User, uuid.UUID]:
    user = await UserFactory.create(db, email=email)
    project = await ProjectFactory.create(db, user.id)
    return user, project.id


async def _seed_live_sandbox_state(redis: aioredis.Redis, user_id: uuid.UUID) -> None:
    """A dead session's LINGERING Redis facade: registry + lock + heartbeat, all still
    inside their TTLs. Before #10/R3 the reconcile spared this conjunction and start 409ed
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


async def test_happy_start_provisions_launches_and_ends(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    user, project_id = await _mk(db_session, "m1@rvaiglobal.com")
    manager = SessionManager()
    client = FakeSandboxClient()

    session = await manager.start(
        db_session,
        user,
        project_id,
        "build me a CRUD app",
        run_build=FakeBrain(),
        sandbox_client=client,
    )
    assert session.status.value == "provisioning"
    assert client.provisioned == [app_name_for(session.app_id)]  # fresh project -> provision

    assert session.task is not None
    await session.task  # let the background build run to completion

    assert session.status == BuildSessionStatus.ENDED
    assert session.preview_url == "https://preview.example/"
    assert session.snapshot_committed is True  # C4 snapshot ran in _finalize
    assert snapshot_key(session.app_id) in fake_storage.objects
    # #13/R2 — the completed build's container is PARDONED, not executed: it stays up under
    # the idle lease (registry kept, stay granted) so the user sees what they just built.
    assert app_name_for(session.app_id) not in client.torn_down
    assert await read_registry(fake_redis, user.id) is not None  # the sweep can still find it
    assert await stay_of_execution_is_current(fake_redis, user.id) is True
    assert await lock_is_held(fake_redis, user.id) is False  # the build slot is free
    assert session.last_seq == 3
    assert [e.seq for e in session.envelopes] == [1, 2, 3]  # gap-free


async def test_finalize_runs_the_liveness_detector_while_the_container_is_up(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    # The #46 detector (plan U1) hooks the end sequence: its workspace collect must run at
    # finalize, BEFORE teardown — the only moment the workspace still exists to scan.
    user, project_id = await _mk(db_session, "m40@rvaiglobal.com")
    manager = SessionManager()
    client = FakeSandboxClient()
    cmds: list[list[str]] = []

    def record(cmd: list[str]) -> ExecResult:
        cmds.append(cmd)
        return ExecResult(stdout="", stderr="", exit=0)

    client.exec_handler = record
    session = await manager.start(
        db_session, user, project_id, "p", run_build=FakeBrain(), sandbox_client=client
    )
    assert session.task is not None
    await session.task

    # The collect script (find over *.tsx/*.jsx/…) ran through the sandbox exec seam.
    assert any("*.tsx" in part for cmd in cmds for part in cmd)


async def test_second_start_while_live_is_409_with_existing_session_id(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    user, project_id = await _mk(db_session, "m2@rvaiglobal.com")
    manager = SessionManager()
    client = FakeSandboxClient()
    brain = BlockingBrain()

    first = await manager.start(
        db_session, user, project_id, "p", run_build=brain, sandbox_client=client
    )
    # A second start for the same user while the first is live -> 409 carrying the id.
    with pytest.raises(BuildSessionConflictError) as exc:
        await manager.start(
            db_session, user, project_id, "p2", run_build=FakeBrain(), sandbox_client=client
        )
    assert exc.value.session_id == first.session_id
    assert client.provisioned == [app_name_for(first.app_id)]  # no second sandbox

    brain.release()
    assert first.task is not None
    await first.task


# The one-per-user rehydrate resolution (`_resolve_sandbox`): via `start()` on a single
# replica the reconcile-then-acquire gate means attach/restore aren't reached (a live
# registry is either reaped as stale → provision, or 409s on a held lock), so the attach
# and restore branches are exercised directly here. Fresh-provision + reap-then-provision
# ARE reachable via start and covered above / below.


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
    brain = BlockingBrain()

    session = await manager.start(
        db_session, user, project_id, "p", run_build=brain, sandbox_client=client
    )
    await brain.stepped.wait()  # the step (seq 1) is buffered before we stop
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
    """U3, tested AT THE SITE THAT DECIDES IT (`.claude/rules/testing.md`).

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
            await manager.start(
                db_session,
                user,
                project_id,
                "p",
                run_build=FakeBrain(),
                sandbox_client=client,
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
    """#10/R3, tested AT THE SITE THAT DECIDES IT: `_holding_user_lock`'s reconcile passes
    `certified_dead=True`, so the walkthrough's back-to-back 409 is gone — a dead session's
    lingering registry+lock+heartbeat is reaped on the way in and the start SUCCEEDS. The
    ghost's container is torn down (never orphaned) before the new one is provisioned; a
    GENUINELY live build still 409s via `_active_by_user`
    (test_second_start_while_live_is_409_with_existing_session_id)."""
    user, project_id = await _mk(db_session, "m-lockheld@rvaiglobal.com")
    manager = SessionManager()
    await _seed_live_sandbox_state(fake_redis, user.id)
    client = FakeSandboxClient()

    session = await manager.start(
        db_session, user, project_id, "p", run_build=FakeBrain(), sandbox_client=client
    )
    assert a_sandbox_name("someone-elses") in client.torn_down  # the ghost was executed first
    assert session.task is not None
    await session.task
    assert session.status == BuildSessionStatus.ENDED


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
        await manager.start(
            db_session, user, project_id, "p", run_build=FakeBrain(), sandbox_client=client
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
        await manager.start(
            db_session,
            user,
            project_id,
            "p",
            run_build=FakeBrain(),
            sandbox_client=FailingProvision(),
        )
    # No leaked lock, no session registered — the immediate next start succeeds.
    assert await lock_is_held(fake_redis, user.id) is False
    assert manager.active_session_for(user.id) is None

    good = FakeSandboxClient()
    session = await manager.start(
        db_session, user, project_id, "retry", run_build=FakeBrain(), sandbox_client=good
    )
    assert session.status == BuildSessionStatus.PROVISIONING
    assert session.task is not None
    await session.task


async def test_a_failed_starting_marker_write_leaks_no_lock(
    db_session: AsyncSession,
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★ U13s placement rule, and it is a placement rule rather than a style note.

    `write_starting_marker` runs with the per-user lock ALREADY HELD. Called from above
    `_holding_user_lock`s try — where it was — a Redis blip on that one `SET` unwinds straight
    out, past the compensation arm that releases the lock, and leaves it in place for its full
    900-second TTL. Every start, relaunch and turn that user attempts for the next fifteen
    minutes is refused with "already building" while nothing is building. Inside the try, the
    same blip is compensated: the lock goes, and the next start succeeds immediately.
    """
    user, project_id = await _mk(db_session, "marker@rvaiglobal.com")
    manager = SessionManager()

    async def _boom(*args: object, **kwargs: object) -> object:
        raise RedisError("redis is down")

    monkeypatch.setattr("src.services.build_sessions.manager.write_starting_marker", _boom)
    with pytest.raises(RedisError):
        await manager.start(
            db_session,
            user,
            project_id,
            "p",
            run_build=FakeBrain(),
            sandbox_client=FakeSandboxClient(),
        )

    assert await lock_is_held(fake_redis, user.id) is False
    assert manager.active_session_for(user.id) is None


async def test_abnormal_completion_synthesizes_failed_ended(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    user, project_id = await _mk(db_session, "m7@rvaiglobal.com")
    manager = SessionManager()
    client = FakeSandboxClient()

    session = await manager.start(
        db_session,
        user,
        project_id,
        "p",
        run_build=FakeBrain(raise_before_ended=True),
        sandbox_client=client,
    )
    assert session.task is not None
    await session.task  # the brain raises after preview_ready (seq 2)

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
    """F8/U5 — a `preview_reconnecting` envelope is buffered, bumps `last_seq`, and fans out like
    any other, but does NOT change the lifecycle status (the C3 enum is frozen at five, with no
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

    session = await manager.start(
        db_session, user, project_id, "p", run_build=FakeBrain(), sandbox_client=client
    )
    assert a_sandbox_name("stale") in client.torn_down  # the orphan was reaped on start
    assert session.status == BuildSessionStatus.PROVISIONING  # the fresh start acquired
    assert session.task is not None
    await session.task


async def test_concurrent_same_user_starts_never_double_allocate(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    # FIX-1 (critical): two concurrent starts for one user must NOT both provision — the
    # per-user start lock serializes them so the second sees the first's held lock → 409.
    user, project_id = await _mk(db_session, "m9@rvaiglobal.com")
    manager = SessionManager()
    client = FakeSandboxClient()
    b1, b2 = BlockingBrain(), BlockingBrain()
    results = await asyncio.gather(
        manager.start(db_session, user, project_id, "p1", run_build=b1, sandbox_client=client),
        manager.start(db_session, user, project_id, "p2", run_build=b2, sandbox_client=client),
        return_exceptions=True,
    )
    sessions = [r for r in results if isinstance(r, BuildSession)]
    conflicts = [r for r in results if isinstance(r, BuildSessionConflictError)]
    assert len(sessions) == 1  # exactly one start won
    assert len(conflicts) == 1  # the other 409'd
    assert len(client.provisioned) == 1  # only ONE sandbox — no double-allocation
    b1.release()
    b2.release()
    if sessions[0].task is not None:
        await sessions[0].task


# --- FIX-3/6: the four best-effort error branches of `_do_finalize` -----------------
# Each injects a failure at one step and asserts the sequence STILL reaches the terminal
# `ended` synthesis (never leaves the SSE feed hung), popping `_active_by_user` regardless.


async def _live_session_stepped(
    manager: SessionManager, db_session: AsyncSession, email: str, client: FakeSandboxClient
) -> tuple[User, BuildSession, BlockingBrain]:
    user, project_id = await _mk(db_session, email)
    brain = BlockingBrain()
    session = await manager.start(
        db_session, user, project_id, "p", run_build=brain, sandbox_client=client
    )
    await brain.stepped.wait()  # step (seq 1) buffered before we stop
    return user, session, brain


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
    user, session, _ = await _live_session_stepped(
        manager, db_session, "m11@rvaiglobal.com", client
    )

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
    user, session, _ = await _live_session_stepped(
        manager, db_session, "m12@rvaiglobal.com", client
    )
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
    _, session, _ = await _live_session_stepped(manager, db_session, "m13@rvaiglobal.com", client)

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
    user, session, _ = await _live_session_stepped(
        manager, db_session, "m14@rvaiglobal.com", client
    )

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
    # A COMPLETED end PARDONS the container (#13): registry kept under the lease. The next
    # start must RESTORE the C4 snapshot the finalize just wrote — provisioning fresh would
    # wipe the user's work onto a blank template.
    #
    # It must ALSO not destroy the pardoned container on the way. `start` used to pass no
    # `spare_app`, so `_the_live_sandbox_is_already_the_one_we_want` answered False
    # unconditionally and reconcile-on-start reaped every incumbent — including, as here, one
    # already serving this very app. That is the same destroy-and-rebuild 1.6.5 removed from
    # the two turn paths and never removed from this one. Here the reap is invisible because
    # `attach_handle` is unset, so the attach arm raises `SandboxGoneError` and the restore
    # happens either way; on a REACHABLE container it cost the user everything since their
    # last Save (see the sibling below).
    user, project_id = await _mk(db_session, "m15@rvaiglobal.com")
    manager = SessionManager()
    client = FakeSandboxClient()
    first = await manager.start(
        db_session, user, project_id, "p", run_build=FakeBrain(), sandbox_client=client
    )
    assert first.task is not None
    await first.task  # clean end: snapshot written, container pardoned, lock released
    assert first.snapshot_committed is True
    assert await fake_redis.hgetall(registry_key(user.id)) != {}  # pardoned: registry stays

    second = await manager.start(
        db_session, user, project_id, "refine it", run_build=FakeBrain(), sandbox_client=client
    )
    assert second.app_id == first.app_id  # same project -> same app
    assert client.torn_down == []  # the pardoned container was SPARED, not reaped
    assert client.restored == [app_name_for(second.app_id)]  # RESTORED, not re-provisioned
    assert client.provisioned == [app_name_for(first.app_id)]  # only the very first start
    assert second.task is not None
    await second.task


async def test_a_start_on_the_same_project_reuses_the_pardoned_container(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """The sibling above with a REACHABLE container, which is where the old behaviour was
    destructive rather than merely wasteful.

    Drop `spare_app` from `_start_locked` and this goes red: `torn_down` gains the first
    container and `restored` gains an entry — the build restarts from the last SAVED bundle,
    silently discarding everything the user had not saved. Mirrors
    `test_write_turn_sandbox.py::test_a_second_message_attaches_instead_of_rebuilding_the_container`,
    which pins the identical rule on the Write-turn path."""
    user, project_id = await _mk(db_session, "m15b@rvaiglobal.com")
    manager = SessionManager()
    client = FakeSandboxClient()
    first = await manager.start(
        db_session, user, project_id, "p", run_build=FakeBrain(), sandbox_client=client
    )
    assert first.task is not None
    await first.task
    client.attach_handle = first.handle  # the pardoned container answers

    second = await manager.start(
        db_session, user, project_id, "refine it", run_build=FakeBrain(), sandbox_client=client
    )
    assert second.app_id == first.app_id
    assert client.torn_down == []  # nothing destroyed
    assert client.restored == []  # nothing rebuilt from the snapshot
    assert client.provisioned == [app_name_for(first.app_id)]  # only the very first start
    assert second.task is not None
    await second.task


async def test_restore_falls_back_to_fresh_when_snapshot_vanishes_mid_restore(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    # A snapshot that disappears between the head-check and the pull must fall back to a
    # fresh provision, never fail the start.
    user, project_id = await _mk(db_session, "m16@rvaiglobal.com")
    manager = SessionManager()

    class VanishingSnapshot(FakeSandboxClient):
        async def restore_from_snapshot(self, user_id, app_name, *, app_env, source_key=None):
            raise StorageNotFoundError("snapshot vanished", provider="fake", key="k")

    client = VanishingSnapshot()
    app_id = await resolve_app_for_project(db_session, user.id, project_id)
    await db_session.commit()
    await fake_storage.put(snapshot_key(app_id), b"BUNDLE")  # head-check sees it...

    env = build_app_env(app_id)
    handle = (await manager._resolve_sandbox(client, user.id, app_id, env)).handle
    assert client.provisioned == [app_name_for(app_id)]  # ...the pull 404s -> fresh
    assert handle.app_name == app_name_for(app_id)


# --- R6: never provision a blank template over the user's work ----------------------
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
            await manager.start(
                db_session,
                user,
                project_id,
                "refine it",
                run_build=FakeBrain(),
                sandbox_client=client,
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

        async def restore_from_snapshot(self, user_id, app_name, *, app_env, source_key=None):
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

        async def restore_from_snapshot(self, user_id, app_name, *, app_env, source_key=None):
            self.attempts += 1
            raise SandboxError("npm install failed under set -e")

    client = DoomedRestore()
    app_id, _ = await _seed_app_with_bundle(db_session, user, project_id, fake_storage)

    with pytest.raises(SnapshotUnavailableError) as caught:
        await manager.start(
            db_session, user, project_id, "refine it", run_build=FakeBrain(), sandbox_client=client
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

        async def restore_from_snapshot(self, user_id, app_name, *, app_env, source_key=None):
            self.attempts += 1
            raise StorageAuthError("the bundle pull was denied", provider="fake", key="k")

    client = AuthDeniedPull()
    app_id, _ = await _seed_app_with_bundle(db_session, user, project_id, fake_storage)

    with pytest.raises(SnapshotUnavailableError) as caught:  # a 503, NOT a 500
        await manager.start(
            db_session, user, project_id, "refine it", run_build=FakeBrain(), sandbox_client=client
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
    # 503 EVERY build start on such a deployment. With no store there is no bundle, so a fresh
    # provision destroys nothing: it is a CONFIRMED absent, not an unknown.
    from src.services.storage import accessor as storage_accessor

    assert storage_accessor._backend_singleton is None  # the fixture-free baseline this rests on
    user, project_id = await _mk(db_session, "m31@rvaiglobal.com")
    manager = SessionManager()
    client = FakeSandboxClient()

    session = await manager.start(
        db_session,
        user,
        project_id,
        "build me a CRUD app",
        run_build=FakeBrain(),
        sandbox_client=client,
    )

    assert client.provisioned == [app_name_for(session.app_id)]  # started, and started fresh
    assert no_sleep == []  # never retried what is a permanent config fact, not a blip
    assert session.task is not None
    await session.task  # the finalize snapshot is a no-op here; the build still ends cleanly


# --- the ended-session retention window --------------------------------------------


async def test_ended_session_kept_inside_retention_window_evicted_after(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    user, project_id = await _mk(db_session, "m17@rvaiglobal.com")
    manager = SessionManager()
    client = FakeSandboxClient()
    session = await manager.start(
        db_session, user, project_id, "p", run_build=FakeBrain(), sandbox_client=client
    )
    assert session.task is not None
    await session.task
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
    # The opportunistic sweep at the top of start() is the guaranteed-recurring seam — an
    # expired ended session must be gone once the next start (any user) runs.
    user, project_id = await _mk(db_session, "m18@rvaiglobal.com")
    manager = SessionManager()
    client = FakeSandboxClient()
    first = await manager.start(
        db_session, user, project_id, "p", run_build=FakeBrain(), sandbox_client=client
    )
    assert first.task is not None
    await first.task
    first.ended_at = datetime.now(UTC) - timedelta(seconds=_ENDED_RETENTION_SECONDS + 1)

    second = await manager.start(
        db_session, user, project_id, "again", run_build=FakeBrain(), sandbox_client=client
    )
    assert manager.get(first.session_id) is None  # swept on entry
    assert manager.get(second.session_id) is second
    assert second.task is not None
    await second.task


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
    # the gate because it runs on EVERY end path — a completed build no longer tears down
    # (#13), so a teardown gate would never be entered.
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
    first = await manager.start(
        db_session, user, project_id, "p", run_build=FakeBrain(), sandbox_client=client
    )
    await entered.wait()  # finalize is mid-snapshot: terminal committed, not done
    assert first.terminal_committed is True
    assert first.finalize_task is not None and not first.finalize_task.done()

    starter = asyncio.create_task(
        manager.start(
            db_session, user, project_id, "refine", run_build=FakeBrain(), sandbox_client=client
        )
    )
    for _ in range(20):  # the second start WAITS on the finalize instead of 409ing
        await asyncio.sleep(0)
    assert not starter.done()

    gate.set()  # finalize completes -> the waiting start proceeds FRESH
    second = await starter
    assert second.session_id != first.session_id
    assert client.restored == [app_name_for(second.app_id)]  # picked up the C4 snapshot
    assert first.task is not None
    await first.task
    assert second.task is not None
    await second.task


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
    user, session, _ = await _live_session_stepped(
        manager, db_session, "m20@rvaiglobal.com", client
    )

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
    user, session, _ = await _live_session_stepped(
        manager, db_session, "m21@rvaiglobal.com", client
    )

    ended = await manager.force_end(session, client)  # no raise -> no 500 path
    assert ended.status == BuildSessionStatus.ENDED
    assert ended.terminal_committed is True  # force-end COMPLETED — never left half-done
    assert session.force_ended is True
    assert session.snapshot_committed is False  # kill switch skips the snapshot by design
    assert snapshot_key(session.app_id) not in fake_storage.objects
    assert app_name_for(session.app_id) in client.torn_down
    assert await lock_is_held(fake_redis, user.id) is False
    assert manager.active_session_for(user.id) is None


class _CompletesOnCue:
    """A brain that steps, then completes the instant `cue` is set — and announces the
    completion (`completing`) in the same event-loop step in which it commits its terminal.
    That is what makes the TOCTOU window below deterministic rather than a hopeful sleep."""

    def __init__(self) -> None:
        self.stepped = asyncio.Event()
        self.cue = asyncio.Event()
        self.completing = asyncio.Event()

    async def __call__(self, session_id, user_id, sandbox_client, on_progress) -> BuildResult:
        await on_progress(StepEvent(seq=1, name="scaffold", label="Scaffolding", state="started"))
        self.stepped.set()
        await self.cue.wait()
        # Wake `_end` (parked in mark-registry-ending), THEN return — so `_finalize` commits the
        # terminal in this same step and `_end` resumes with a stale `terminal_committed=False`.
        self.completing.set()
        return BuildResult(
            status=BuildSessionStatus.ENDED,
            reason="completed",
            app_id=uuid.uuid4(),
            preview_url=None,
            last_seq=1,
            snapshot_committed=False,
        )


async def test_force_end_landing_inside_mark_ending_never_steals_a_completed_snapshot(
    db_session: AsyncSession,
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The TOCTOU `_end` guards against: the build COMPLETES while `_end` is suspended inside
    # `mark_registry_ending`. `_end`'s entry check said "not terminal" and is now stale, so a
    # blind `force_ended = True` would land BEHIND the terminal commit — finalize would then skip
    # the snapshot of a finished build whose terminal already says `completed`. The user's work
    # would be gone with nothing anywhere admitting it. The loser of this race writes NOTHING.
    manager = SessionManager()
    client = FakeSandboxClient()
    user, project_id = await _mk(db_session, "m32@rvaiglobal.com")
    brain = _CompletesOnCue()
    session = await manager.start(
        db_session, user, project_id, "p", run_build=brain, sandbox_client=client
    )
    await brain.stepped.wait()

    async def complete_inside_the_await(*_a: object, **_k: object) -> None:
        brain.cue.set()
        await brain.completing.wait()

    monkeypatch.setattr(
        "src.services.build_sessions.manager.mark_registry_ending", complete_inside_the_await
    )

    ended = await manager.force_end(session, client)

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
    # The COMPLETION owned the end sequence, so its pardon stands (#13): the container the
    # late kill switch failed to claim stays up under the lease, lock released.
    assert app_name_for(session.app_id) not in client.torn_down
    assert await stay_of_execution_is_current(fake_redis, user.id) is True
    assert await lock_is_held(fake_redis, user.id) is False


async def test_stop_racing_completion_finalizes_exactly_once(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    # FIX-2: a stop racing the task's own normal completion+finalize must not tear the end
    # sequence in half (the shielded _do_finalize runs to completion exactly once).
    user, project_id = await _mk(db_session, "m10@rvaiglobal.com")
    manager = SessionManager()
    client = FakeSandboxClient()
    session = await manager.start(
        db_session, user, project_id, "p", run_build=FakeBrain(), sandbox_client=client
    )
    assert session.task is not None
    await asyncio.gather(manager.stop(session, client), session.task, return_exceptions=True)
    # Fully finalized, no leak, ONE end sequence — whichever racer won it. A completion win
    # pardons the container (#13: zero teardowns, lease granted); a stop win tears it down
    # exactly once. Either way the lock is released and exactly one terminal is emitted.
    assert session.terminal_committed is True
    assert await lock_is_held(fake_redis, user.id) is False
    terminal = session.envelopes[-1]
    assert isinstance(terminal, EndedEvent)
    expected_teardowns = 0 if terminal.reason == "completed" else 1
    assert client.torn_down.count(app_name_for(session.app_id)) == expected_teardowns
    assert manager.active_session_for(user.id) is None


# --- U3: per-app Blob env injection on the birth arms only (C9 §6, KTD-3) ------------
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

    session = await manager.start(
        db_session, user, project_id, "p", run_build=FakeBrain(), sandbox_client=client
    )
    assert session.task is not None
    await session.task

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
    # The corrected KTD-3 assertion: attach reuses the live container's SAS — provision_app_storage
    # is NEVER called on the attach arm (no container/SAS work), and attach_existing takes no env.
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
        await manager.start(
            db_session, user, project_id, "p", run_build=FakeBrain(), sandbox_client=client
        )
    # Compensation ran: lock released, no session registered, and we never reached provision
    # (storage failed first, so no sandbox handle exists to tear down).
    assert await lock_is_held(fake_redis, user.id) is False
    assert manager.active_session_for(user.id) is None
    assert client.provisioned == []


# --- R7: the single authoritative terminal `ended` ----------------------------
#
# The unit's whole point, stated as an invariant: NO end path may emit two `ended` frames or a
# false `snapshot_committed`. BRAIN emits none at all (see tests/services/orchestrator/); the
# manager emits exactly one, from `_do_finalize`, AFTER the C4 snapshot — the only moment the
# flag can be told truthfully. These tests enumerate every end path there is:
#   completed · quota_exceeded · escalated · stop · idle_teardown · force_end · run_build raised


def _endeds(session: BuildSession) -> list[EndedEvent]:
    return [e for e in session.envelopes if isinstance(e, EndedEvent)]


class _OrderRecordingSandboxClient(FakeSandboxClient):
    """Records teardown into a shared order log so the C4 ordering invariant
    (snapshot → teardown-or-pardon → release → terminal) is asserted, not assumed —
    a completed build's log shows NO teardown at all (#13, the pardon)."""

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
    # R7's headline: the terminal frame reports snapshot_committed=TRUE on a build whose
    # snapshot really committed. It can only do so because it is emitted after the commit —
    # the old BRAIN-emitted frame necessarily preceded it and always said false.
    manager = SessionManager()
    order = _spy_order(manager, monkeypatch)
    client = _OrderRecordingSandboxClient(order)
    user, project_id = await _mk(db_session, "r7a@rvaiglobal.com")

    session = await manager.start(
        db_session, user, project_id, "p", run_build=FakeBrain(), sandbox_client=client
    )
    assert session.task is not None
    await session.task

    ended = _endeds(session)
    assert len(ended) == 1  # exactly ONE terminal
    assert ended[0].snapshot_committed is True  # …and it is TRUE (the lie R7 kills)
    assert ended[0].status == BuildSessionStatus.ENDED
    assert ended[0].reason == "completed"
    assert ended[0].preview_url == "https://preview.example/"  # carried off the verdict
    assert ended[0] is session.envelopes[-1]  # always last
    # The snapshot really is committed, and the frame really is emitted after it. No
    # teardown in between: the completed build's container is pardoned (#13), so the frame's
    # preview_url points at a container that is actually still serving.
    assert snapshot_key(session.app_id) in fake_storage.objects
    assert order == ["snapshot", "ended"]
    assert app_name_for(session.app_id) not in client.torn_down
    # seq continues BRAIN's stream at last_seq + 1 — gap-free across the handoff.
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

    session = await manager.start(
        db_session, user, project_id, "p", run_build=FakeBrain(), sandbox_client=client
    )
    assert session.task is not None
    await session.task

    ended = _endeds(session)
    assert len(ended) == 1
    assert ended[0].snapshot_committed is False  # never claims a snapshot that did not happen
    assert ended[0].reason == "completed"
    assert session.snapshot_committed is False
    assert snapshot_key(session.app_id) not in fake_storage.objects
    # A failed snapshot must not disturb the ordering invariant — and it must not cost the
    # user the live preview either: the BUILD completed, so the pardon (#13) still applies.
    # Durability and visibility are separate questions with separate answers.
    assert order == ["snapshot", "ended"]
    assert app_name_for(session.app_id) not in client.torn_down
    assert await stay_of_execution_is_current(fake_redis, user.id) is True
    assert await lock_is_held(fake_redis, user.id) is False  # …and the lock still released


async def test_quota_run_emits_the_quota_envelope_then_exactly_one_ended(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    # The quota path composed end-to-end: BRAIN keeps its informational `quota_exceeded`
    # envelope and hands the END home on the verdict. The portal must see quota_exceeded
    # followed by ONE terminal — and a graceful ENDED, never FAILED.
    class QuotaBrain:
        async def __call__(self, session_id, user_id, sandbox_client, on_progress) -> BuildResult:
            await on_progress(
                QuotaExceededEvent(seq=1, limit=50, used=50, resets_at="2026-07-17T00:00:00Z")
            )
            return BuildResult(
                status=BuildSessionStatus.ENDED,
                reason="quota_exceeded",
                app_id=uuid.uuid4(),
                preview_url=None,
                last_seq=1,
                snapshot_committed=False,
            )

    manager = SessionManager()
    client = FakeSandboxClient()
    user, project_id = await _mk(db_session, "r7c@rvaiglobal.com")

    session = await manager.start(
        db_session, user, project_id, "p", run_build=QuotaBrain(), sandbox_client=client
    )
    assert session.task is not None
    await session.task

    assert [e.type for e in session.envelopes] == ["quota_exceeded", "ended"]
    ended = _endeds(session)
    assert len(ended) == 1
    assert ended[0].status == BuildSessionStatus.ENDED  # graceful, NOT failed
    assert ended[0].reason == "quota_exceeded"
    assert ended[0].snapshot_committed is True  # a quota end still saves the work
    assert ended[0].seq == 2  # last_seq + 1


async def test_escalated_verdict_ends_failed_even_though_its_reason_is_not_build_failed(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    # The trap this unit had to dodge: `status` CANNOT be re-derived from `reason` on BRAIN's
    # paths. An `escalated` end is FAILED, yet "escalated" != _BUILD_FAILED — deriving it would
    # silently downgrade every escalated build to a graceful ENDED. So the verdict's own
    # `status` wins whenever there is a verdict.
    manager = SessionManager()
    client = FakeSandboxClient()
    user, project_id = await _mk(db_session, "r7d@rvaiglobal.com")
    brain = FakeBrain(status=BuildSessionStatus.FAILED, reason="escalated")

    session = await manager.start(
        db_session, user, project_id, "p", run_build=brain, sandbox_client=client
    )
    assert session.task is not None
    await session.task

    ended = _endeds(session)
    assert len(ended) == 1
    assert ended[0].status == BuildSessionStatus.FAILED  # NOT downgraded to ENDED
    assert ended[0].reason == "escalated"
    assert ended[0].snapshot_committed is True  # a failed build's work is still saved
    assert session.status == BuildSessionStatus.FAILED


@pytest.mark.parametrize("reason", ["stopped_by_user", "idle_teardown"])
async def test_stop_paths_emit_exactly_one_ended(
    db_session: AsyncSession,
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
    monkeypatch: pytest.MonkeyPatch,
    reason: str,
) -> None:
    # The manager-originated ends (a user stop, and the idle reap that stops with its own
    # reason). BRAIN is cancelled and emits nothing, so the terminal here is entirely the
    # manager's — one frame, post-snapshot, carrying the caller's reason.
    manager = SessionManager()
    order = _spy_order(manager, monkeypatch)
    client = _OrderRecordingSandboxClient(order)
    user, session, _ = await _live_session_stepped(
        manager, db_session, f"r7-{reason}@rvaiglobal.com", client
    )

    await manager.stop(session, client, reason=reason)

    ended = _endeds(session)
    assert len(ended) == 1  # no double emission
    assert ended[0].reason == reason
    assert ended[0].status == BuildSessionStatus.ENDED  # a stop is graceful
    assert ended[0].snapshot_committed is True  # the user's work IS saved on a stop
    assert order == ["snapshot", "teardown", "ended"]
    assert ended[0].seq == 2  # BlockingBrain's step (seq 1) + 1
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
    user, session, _ = await _live_session_stepped(
        manager, db_session, "r7f@rvaiglobal.com", client
    )

    await manager.force_end(session, client)

    ended = _endeds(session)
    assert len(ended) == 1
    assert ended[0].reason == "force_ended"
    assert ended[0].snapshot_committed is False  # skipped, and said so
    assert order == ["teardown", "ended"]  # the snapshot never even ran
    assert snapshot_key(session.app_id) not in fake_storage.objects


async def test_a_raised_run_build_still_emits_exactly_one_failed_ended(
    db_session: AsyncSession,
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # BRAIN breaking its never-raise invariant leaves NO verdict. The manager must still
    # terminate the feed itself — deriving `build_failed`/FAILED — or the SSE feed hangs
    # forever. The snapshot still runs first, so the frame's flag is still earned.
    manager = SessionManager()
    order = _spy_order(manager, monkeypatch)
    client = _OrderRecordingSandboxClient(order)
    user, project_id = await _mk(db_session, "r7g@rvaiglobal.com")

    session = await manager.start(
        db_session,
        user,
        project_id,
        "p",
        run_build=FakeBrain(raise_before_ended=True),
        sandbox_client=client,
    )
    assert session.task is not None
    await session.task

    ended = _endeds(session)
    assert len(ended) == 1
    assert ended[0].status == BuildSessionStatus.FAILED
    assert ended[0].reason == "build_failed"
    assert ended[0].snapshot_committed is True  # crashed, but the work was still saved
    assert order == ["snapshot", "teardown", "ended"]
    assert ended[0].seq == 3  # the 2 envelopes it emitted before dying, + 1


async def test_stop_racing_a_natural_completion_still_emits_exactly_one_ended(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    # The double-emission danger zone: a stop landing while the task's own completion is
    # already finalizing. The single-owner `finalize_task` guard means _do_finalize — and so
    # the terminal emit — happens exactly once, no matter who calls or how many times.
    manager = SessionManager()
    client = FakeSandboxClient()
    user, project_id = await _mk(db_session, "r7h@rvaiglobal.com")
    session = await manager.start(
        db_session, user, project_id, "p", run_build=FakeBrain(), sandbox_client=client
    )
    assert session.task is not None

    # Race the natural completion against a stop AND a redundant second stop.
    await asyncio.gather(
        session.task,
        manager.stop(session, client),
        manager.stop(session, client),
    )

    assert len(_endeds(session)) == 1
    assert session.terminal_emitted is True
    assert session.status in (BuildSessionStatus.ENDED, BuildSessionStatus.FAILED)
    assert [e.seq for e in session.envelopes] == [1, 2, 3]  # still gap-free


# --- R3: the attachment resolution the manager performs at start ------------


async def test_start_carries_resolved_attachments_onto_the_session(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """R3 — `conversationId` at start → the thread's attachments land on the session, which is
    where `_live_session_spec` reads them to build BRAIN's multimodal prompt."""
    user, project_id = await _mk(db_session, "m-att1@rvaiglobal.com")
    conv = await ConversationFactory.create(
        db_session, user.id, project_id=project_id, kind=ChatKind.BUILD
    )
    from pydantic_ai.messages import ModelRequest, UserPromptPart

    from src.services.messages.store import dump_for_row
    from src.services.storage import attachment_key

    png = bytes([0x89, 0x50, 0x4E, 0x47]) + b" body"
    key = attachment_key(user.id, uuid.uuid7())
    await fake_storage.put(key, png, content_type="image/png")
    db_session.add(
        Attachment(
            user_id=user.id,
            attachment_id="a-img",
            media_type="image/png",
            name="chart.png",
            size=len(png),
            storage_key=key,
        )
    )
    await db_session.flush()
    await MessageFactory.create(
        db_session,
        user.id,
        conv.id,
        seq=0,
        payload=dump_for_row(
            [
                ModelRequest(
                    parts=[
                        UserPromptPart(
                            content=[
                                "use this",
                                BinaryContent(
                                    data=png, media_type="image/png", identifier="a-img"
                                ),
                            ]
                        )
                    ]
                )
            ]
        ),
    )
    manager = SessionManager()
    client = FakeSandboxClient()

    session = await manager.start(
        db_session,
        user,
        project_id,
        "build me a dashboard",
        conversation_id=conv.id,
        run_build=FakeBrain(),
        sandbox_client=client,
    )

    assert len(session.attachments) == 1
    binary = session.attachments[0]
    assert isinstance(binary, BinaryContent) and binary.data == png
    assert session.task is not None
    await session.task


async def test_start_without_conversation_resolves_no_attachments(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    user, project_id = await _mk(db_session, "m-att2@rvaiglobal.com")
    manager = SessionManager()

    session = await manager.start(
        db_session,
        user,
        project_id,
        "build me a CRUD app",
        run_build=FakeBrain(),
        sandbox_client=FakeSandboxClient(),
    )

    assert session.attachments == []
    assert session.task is not None
    await session.task


async def test_unusable_attachment_aborts_start_before_any_sandbox(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """Fail-first, cheaply: the resolution runs BEFORE the lock and the container, so a rejected
    attachment leaves nothing to compensate — no sandbox, no held lock, no registered session."""
    user, project_id = await _mk(db_session, "m-att3@rvaiglobal.com")
    conv = await ConversationFactory.create(
        db_session, user.id, project_id=project_id, kind=ChatKind.BUILD
    )
    from pydantic_ai.messages import ModelRequest, UserPromptPart

    from src.services.messages.store import dump_for_row

    await MessageFactory.create(
        db_session,
        user.id,
        conv.id,
        seq=0,
        payload=dump_for_row(
            [
                ModelRequest(
                    parts=[
                        UserPromptPart(
                            content=[
                                BinaryContent(
                                    data=b"\x89PNGx", media_type="image/png", identifier="gone"
                                )
                            ]
                        )
                    ]
                )
            ]
        ),
    )
    manager = SessionManager()
    client = FakeSandboxClient()

    with pytest.raises(BuildAttachmentError, match="no longer available"):
        await manager.start(
            db_session,
            user,
            project_id,
            "build it",
            conversation_id=conv.id,
            run_build=FakeBrain(),
            sandbox_client=client,
        )

    assert client.provisioned == []
    assert client.torn_down == []
    assert not await lock_is_held(fake_redis, user.id)
    assert manager.active_session_for(user.id) is None


# --- #43: relaunch a torn-down preview from its snapshot (Decision 6) ----------------
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
    # The happy path (F4): restore the snapshot, DRIVE the dev server (dev_start + wait_ready),
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

    # A real build for the same user now starts cleanly (no BuildSessionConflictError).
    session = await manager.start(
        db_session,
        user,
        project_id,
        "refine it",
        run_build=FakeBrain(),
        sandbox_client=FakeSandboxClient(),
    )
    assert session.task is not None
    await session.task


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
        async def restore_from_snapshot(self, user_id, app_name, *, app_env, source_key=None):
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
    """U2 regression pin. `release_lock_as_holder` on the `_holding_user_lock` CLEAN-EXIT
    path (`manager.py:398`) sits INSIDE the protected region, and its raise is the mechanism
    that triggers compensation — see the docstring at `manager.py:385-388`: "if it fails,
    compensation still tears the container down rather than leaving a live preview behind a
    lock nobody can release."

    So a guard inside the primitive that returned `False` instead of raising would leave a
    live container orphaned, silently. That guard was briefly added and reverted; this test
    is what makes re-adding it impossible to do quietly."""
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
    """U2 regression pin, the heartbeat half. `write_heartbeat` at `manager.py:554` is
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
    # CHANGED, deliberately. This used to assert the container was torn down. The comment it
    # cites feared "500ing with a live container behind a HELD LOCK" — and the lock half is
    # still guaranteed below, because compensation releases regardless. What is no longer
    # true is the container half: by the time the heartbeat is seeded, `wait_ready` has
    # returned and the container is up, registered and under a stay — the same state a
    # successful relaunch leaves. Destroying a working preview to tidy a hash costs the user
    # their app; leaving it means their retry ATTACHES to it in seconds instead of paying a
    # full restore. The error still surfaces either way.
    assert client.torn_down == []
    assert await lock_is_held(fake_redis, user.id) is False  # the lock IS still given back
    assert manager._active_by_user == {}


async def test_relaunch_while_a_build_is_live_is_409(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    # A live build owns the one-per-user slot; relaunch never pre-empts it. It 409s
    # (BuildSessionConflictError), carrying the live session's id.
    user, project_id = await _mk(db_session, "r6@rvaiglobal.com")
    manager = SessionManager()
    brain = BlockingBrain()
    await _seed_app_with_bundle(db_session, user, project_id, fake_storage)

    session = await manager.start(
        db_session,
        user,
        project_id,
        "build it",
        run_build=brain,
        sandbox_client=FakeSandboxClient(),
    )
    await brain.stepped.wait()  # the build is now live and holds the slot
    try:
        with pytest.raises(BuildSessionConflictError) as caught:
            await manager.relaunch_preview(db_session, user, project_id, FakeSandboxClient())
        assert caught.value.session_id == session.session_id
    finally:
        brain.release()
        assert session.task is not None
        await session.task


async def test_relaunch_404_leaves_no_committed_app_row_and_provisions_no_storage(
    db_session: AsyncSession,
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # F17: the snapshot gate runs BEFORE the commit and the storage provision, so a never-built
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
    # F11: relaunch blocks for minutes (restore + wait_ready), so a dropped request cancels the
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
    # U6 (F1): `_do_finalize` snapshots pass and fail alike, so after a FAILED build the newest
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
    # live 30-minute lease), then start a real build on project B for the SAME user. The
    # build needs the one-per-user sandbox slot, so reconcile-on-start must reap THROUGH
    # the unexpired stay. Sparing it would leave A's container running while B registers
    # its own over that hash — the container orphaned, invisible to the registry-only sweep
    # forever after.
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

    # A blocking brain keeps the build LIVE, so the registry can be read while it is still
    # the build's — a completed build's finalize deletes the hash outright.
    brain = BlockingBrain()
    session = await manager.start(
        db_session,
        user,
        project_b,
        "build me something else",
        run_build=brain,
        sandbox_client=client,
    )
    await brain.stepped.wait()

    # (a) the PREVIEW's container was actually torn down — reaped, never orphaned.
    assert preview_app_name in client.torn_down
    # (b) ...and the registry now names the NEW BUILD's app. Project B is a different app,
    #     so this is a real assertion and not a tautology about a shared app_name.
    build_app_name = app_name_for(session.app_id)
    assert build_app_name != preview_app_name
    reg_after = await read_registry(fake_redis, user.id)
    assert reg_after is not None
    assert reg_after[REGISTRY_FIELD_APP_NAME] == build_app_name
    # The build inherited NO lease from the preview it displaced (see the C2 client's
    # `_write_registry`): its container is reapable the moment its own liveness lapses.
    assert REGISTRY_FIELD_PREVIEW_STAY_UNTIL not in reg_after
    assert await stay_of_execution_is_current(fake_redis, user.id) is False

    brain.release()
    await manager.stop(session, client)


# --- U1: the attach arm's own seams (the ACA call counts live in test_relaunch.py) -----
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
    """Put a healthy, READY container for `app_id` in front of the manager: the C5 registry
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
    # R1 at the manager seam: same app, registry READY → attach, and the dev server is still
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
    assert client.restored == [name]  # ...and a fresh one restored, exactly as before U1


async def test_a_post_attach_readiness_failure_spares_the_attached_container(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """THE COMPENSATION HAZARD, in its general form. `_compensate_lock_and_container` tears
    down `scope.handle` on ANY body failure — including a `CancelledError` from a dropped
    request — and before U1 that was safe because relaunch only ever assigned a container it
    had just created. The moment it assigns a PRE-EXISTING one, every post-attach failure
    becomes destructive on a container this request did not create. Without `_LockScope.
    attached`, U1 destroys the healthy container it exists to preserve."""
    user, project_id = await _mk(db_session, "r15@rvaiglobal.com")
    manager = SessionManager()

    class DevNeverReadies(_RelaunchRecorder):
        async def wait_ready(self, handle, *, timeout_s=120.0):
            raise SandboxNotReadyError("dev server not ready within 120s")

    client = DevNeverReadies()
    app_id, _ = await _seed_app_with_bundle(db_session, user, project_id, fake_storage)
    await _the_container_is_already_up(client, fake_redis, user.id, app_id)

    # No longer raises: the attach arm fails open (R6/SL-20). The hazard this test names is
    # unchanged and is asserted below — a post-attach failure must never destroy a container
    # this request did not create.
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
    # R6. On the attach arm `dev_start` is an optimization against a container that is very
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
    # URL that 404s — the exact "successful build, blank page" failure this plan is fixing
    # elsewhere. Widen the guard to both arms and this goes red.
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
    """A container gets its env exactly ONCE, at birth — ACA sets vars on the revision, not on
    a running process — so the attach arm builds no env at all, matching `_resolve_sandbox`'s
    documented attach semantics.

    This is a real consequence and it is pinned rather than assumed: relaunching used to
    re-mint the session SAS every time, and U1 makes relaunching cheap, so rotation cadence on
    this path goes to zero. Deliberate; see the plan's Risks table."""
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
    """U3/R3 on the relaunch path. `wait_ready` returning means the dev server ANSWERS, not that
    this route has been built — Turbopack compiles on first request. Relaunch hands its
    `previewUrl` straight back to a browser that frames it immediately, so if the platform does
    not pay that compile the citizen does, staring at a blank white card for 5-7s directly after
    clicking a button labelled Relaunch."""
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
    """★ R6. The warm request is an optimization bolted onto a path that already worked. A route
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
    """★ SL-20 — THE MOST EXPENSIVE LESSON ON THIS BRANCH, AND THIS TEST USED TO ASSERT THE BUG.

    Its previous form required a readiness timeout to mark the registry `ending`, so a container
    whose dev server would not come up "stopped winning the attach arm". Run against real Azure,
    that is silent data loss. `attach_existing` refuses an `ending` sandbox BEFORE it probes, so
    the very next press took the RESTORE arm — and restore calls `_safe_teardown` on the live
    container before pulling the last SAVED bundle. Two clicks, and every unsaved edit was gone
    with nothing on screen to say so. The 503 the old path raised is the copy that invited the
    second click.

    The error was reading a readiness timeout as a statement about the CONTAINER. It is a
    statement about the generated APP: since U6, `ready` means a request was actually served, so
    any root route slower than the supervisor's read timeout reports un-ready forever — a heavy
    dashboard query is enough. The container is healthy and holds the citizen's work.

    So it KEEPS its READY state and keeps winning the attach arm, which is exactly right: the
    next press should attach to the container holding their work, not restore over it. The wedge
    the `ending` mark was reaching for is closed by the lease we decline to grant before the wait
    (asserted by its sibling below) — that lapses on its own and covers every exit, not just this
    one."""
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
        "save. That is SL-20, and it is P0."
    )


async def test_a_second_press_after_a_slow_app_attaches_instead_of_eating_the_workspace(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """★ SL-20, END TO END — the regression this branch actually shipped and then reproduced
    against real Azure. The scenario is two clicks by a confused citizen, and the ONLY thing
    standing between them and losing their unsaved work is that the second press takes the
    ATTACH arm rather than the RESTORE arm.

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
    """★ THE TRAP'S SIBLINGS. `wait_ready` does not only raise `SandboxNotReadyError` — a
    persistently non-200 `/dev/status`, a malformed body and an unreachable supervisor all
    surface as a bare `SandboxError`, and a dropped request arrives as `CancelledError`. The
    mark-ending arm names exactly one of those, so every other exit skipped it — and while the
    stay of execution was granted BEFORE the wait, each of those exits still bought the doomed
    container another full 30-minute reprieve. Retry, refresh, repeat: the same wedge the
    readiness arm closes, reached through its siblings.

    The fix is the ABSENCE of a grant, so this asserts an absence: the pre-existing lease is
    left exactly as it was found. Nothing new is condemned — the container keeps its READY
    state, because a supervisor blip must not commit the reaper to destroying a container that
    may hold unsaved work (that is why the `ending` arm stays narrow).

    Mutation check: drop the `if not attached:` guard on the pre-wait grant and the stamp below
    moves, which is the renewal itself.
    """
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
    """★ U1 skipped the reconcile to spare the container — but reconcile also `reap_lock`ed,
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
