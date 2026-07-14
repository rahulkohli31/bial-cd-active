"""U5 — the SessionManager lifecycle: start (provision/attach/restore + launch), the
progress channel, the single-owner end sequence, stop/force-end, and start compensation.
Driven by FakeSandboxClient (mock C1) + FakeBrain (mock C7) + fakeredis + fake storage +
the `:5432` test DB.
"""

from __future__ import annotations

import asyncio
import uuid

import pytest
import redis.asyncio as aioredis
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.v1.build_sessions.schemas import (
    BuildResult,
    BuildSessionStatus,
    EndedEvent,
    PreviewReadyEvent,
    ProgressEnvelope,
    StepEvent,
)
from src.config import settings
from src.db.models.user import User
from src.services.build_sessions.locks import lock_is_held
from src.services.build_sessions.manager import (
    BuildSession,
    BuildSessionConflictError,
    SessionManager,
    app_name_for,
)
from src.services.redis import (
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
from src.services.sandbox import SandboxError, SandboxHandle
from src.services.sandbox.config import SandboxConfig
from src.services.storage import snapshot_key
from tests.factories import ProjectFactory, UserFactory
from tests.fakes import FakeBrain, FakeSandboxClient, FakeStorage


@pytest.fixture(autouse=True)
def _sandbox_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        settings,
        "sandbox",
        SandboxConfig(
            subscription_id="s",
            resource_group="r",
            region="westeurope",
            image_ref="acr/img:latest",
            app_data_base_url="https://platform.example/v1",
        ),
    )


class BlockingBrain:
    """A brain that emits one step, then blocks until `release()` — keeps a session live
    so concurrency / stop tests aren't racing a fast completion."""

    def __init__(self) -> None:
        self._gate = asyncio.Event()

    def release(self) -> None:
        self._gate.set()

    async def __call__(self, session_id, user_id, sandbox_client, on_progress) -> BuildResult:
        await on_progress(StepEvent(seq=1, name="scaffold", label="Scaffolding", state="started"))
        await self._gate.wait()
        await on_progress(
            EndedEvent(
                seq=2,
                status=BuildSessionStatus.ENDED,
                preview_url=None,
                snapshot_committed=False,
                reason="completed",
            )
        )
        return BuildResult(
            status=BuildSessionStatus.ENDED,
            app_id=uuid.uuid4(),
            preview_url=None,
            last_seq=2,
            snapshot_committed=False,
        )


async def _mk(db: AsyncSession, email: str) -> tuple[User, uuid.UUID]:
    user = await UserFactory.create(db, email=email)
    project = await ProjectFactory.create(db, user.id)
    return user, project.id


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
    assert app_name_for(session.app_id) in client.torn_down  # teardown ran
    assert await lock_is_held(fake_redis, user.id) is False  # lock released LAST
    assert session.last_seq == 4
    assert [e.seq for e in session.envelopes] == [1, 2, 3, 4]  # gap-free


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


async def test_rehydrate_attaches_when_a_live_registry_exists(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    user, project_id = await _mk(db_session, "m3@rvaiglobal.com")
    manager = SessionManager()
    client = FakeSandboxClient()
    # Seed a live registry entry + set the client's attach handle -> start ATTACHES.
    app_name = "sbx-existing"
    client.attach_handle = SandboxHandle(
        fqdn="existing.example",
        token="tok",
        app_name=app_name,
        preview_url="https://existing.example/",
        ready=False,
    )
    await fake_redis.hset(
        registry_key(user.id),
        mapping={
            REGISTRY_FIELD_APP_NAME: app_name,
            REGISTRY_FIELD_FQDN: "existing.example",
            REGISTRY_FIELD_TOKEN_REF: "ref",
            REGISTRY_FIELD_CREATED_AT: "2026-07-14T00:00:00+00:00",
            REGISTRY_FIELD_STATE: REGISTRY_STATE_READY,
        },
    )
    # Seed a live lock + heartbeat so reconcile leaves the registry (a live session look).
    await fake_redis.set(lock_key(user.id), "held", ex=900)
    await fake_redis.set(f"bial:sandbox:heartbeat:{user.id}", "beat", ex=90)
    # The lock is held, so start would 409 — clear it first to model a same-user resume
    # where reconcile kept the registry but the lock lapsed.
    await fake_redis.delete(lock_key(user.id))

    session = await manager.start(
        db_session, user, project_id, "resume", run_build=FakeBrain(), sandbox_client=client
    )
    assert client.provisioned == []  # attached, not re-provisioned
    assert client.restored == []
    brain_done = session.task
    assert brain_done is not None
    await brain_done


async def test_rehydrate_restores_when_container_gone_but_snapshot_exists(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    user, project_id = await _mk(db_session, "m4@rvaiglobal.com")
    manager = SessionManager()
    client = FakeSandboxClient()  # attach_handle unset -> attach raises Gone
    # Resolve the app so we know its id, then seed a matching registry + a snapshot.
    from src.services.build_sessions.appdata import resolve_app_for_project

    app_id, _ = await resolve_app_for_project(db_session, user.id, project_id)
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

    session = await manager.start(
        db_session, user, project_id, "resume", run_build=FakeBrain(), sandbox_client=client
    )
    assert client.restored == [app_name_for(app_id)]  # attach gone + snapshot -> restore
    assert client.provisioned == []
    assert session.task is not None
    await session.task


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
    # A second stop returns the terminal state (idempotent).
    again = await manager.stop(session, client)
    assert again.status == BuildSessionStatus.ENDED


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
    await session.task  # the brain raises after preview_ready (seq 3)

    assert session.status == BuildSessionStatus.FAILED  # derived from the synthetic ended
    terminal = session.envelopes[-1]
    assert isinstance(terminal, EndedEvent)
    assert terminal.status == BuildSessionStatus.FAILED
    assert terminal.reason == "build_failed"
    assert terminal.seq == 4  # strictly last_seq+1 (preview_ready was seq 3), gap-free
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
            app_name="sbx-x",
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
            REGISTRY_FIELD_APP_NAME: "sbx-stale",
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
    assert "sbx-stale" in client.torn_down  # the orphan was reaped on start
    assert session.status == BuildSessionStatus.PROVISIONING  # the fresh start acquired
    assert session.task is not None
    await session.task
