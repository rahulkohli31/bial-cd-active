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
from src.services.build_sessions.appdata import build_app_env, resolve_app_for_project
from src.services.build_sessions.locks import lock_is_held
from src.services.build_sessions.manager import (
    _ENDED_RETENTION_SECONDS,
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
from src.services.storage import StorageError, StorageNotFoundError, snapshot_key
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
            managed_environment_name="aca-env",
            image_ref="acr/img:latest",
            app_data_base_url="https://platform.example/v1",
        ),
    )


class BlockingBrain:
    """A brain that emits one step, then blocks until `release()` — keeps a session live
    so concurrency / stop tests aren't racing a fast completion. `stepped` fires AFTER the
    step is buffered, so a test can deterministically stop with a known `last_seq`."""

    def __init__(self) -> None:
        self._gate = asyncio.Event()
        self.stepped = asyncio.Event()

    def release(self) -> None:
        self._gate.set()

    async def __call__(self, session_id, user_id, sandbox_client, on_progress) -> BuildResult:
        await on_progress(StepEvent(seq=1, name="scaffold", label="Scaffolding", state="started"))
        self.stepped.set()
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
    app_id, app_key = await resolve_app_for_project(db_session, user.id, project_id)
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
    env = build_app_env(app_id, app_key)
    handle = await manager._resolve_sandbox(client, user.id, app_id, env)
    assert client.provisioned == [] and client.restored == []  # attached, no re-provision
    assert handle.app_name == app_name_for(app_id)


async def test_resolve_sandbox_restores_when_gone_but_snapshot_exists(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    user, project_id = await _mk(db_session, "m4@rvaiglobal.com")
    manager = SessionManager()
    client = FakeSandboxClient()  # attach_handle unset -> attach raises Gone
    app_id, app_key = await resolve_app_for_project(db_session, user.id, project_id)
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
    env = build_app_env(app_id, app_key)
    handle = await manager._resolve_sandbox(client, user.id, app_id, env)
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
    # A CLEAN end deletes the registry; the next start must RESTORE the C4 snapshot the
    # finalize just wrote — provisioning fresh would wipe the user's work onto a blank
    # template. (No registry + NO snapshot -> provision_new is the happy-path test above.)
    user, project_id = await _mk(db_session, "m15@rvaiglobal.com")
    manager = SessionManager()
    client = FakeSandboxClient()
    first = await manager.start(
        db_session, user, project_id, "p", run_build=FakeBrain(), sandbox_client=client
    )
    assert first.task is not None
    await first.task  # clean end: snapshot written, registry deleted, lock released
    assert first.snapshot_committed is True
    assert await fake_redis.hgetall(registry_key(user.id)) == {}  # no registry left behind

    second = await manager.start(
        db_session, user, project_id, "refine it", run_build=FakeBrain(), sandbox_client=client
    )
    assert second.app_id == first.app_id  # same project -> same app
    assert client.restored == [app_name_for(second.app_id)]  # RESTORED, not re-provisioned
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
        async def restore_from_snapshot(self, user_id, app_name, *, app_env):
            raise StorageNotFoundError("snapshot vanished", provider="fake", key="k")

    client = VanishingSnapshot()
    app_id, app_key = await resolve_app_for_project(db_session, user.id, project_id)
    await db_session.commit()
    await fake_storage.put(snapshot_key(app_id), b"BUNDLE")  # head-check sees it...

    env = build_app_env(app_id, app_key)
    handle = await manager._resolve_sandbox(client, user.id, app_id, env)
    assert client.provisioned == [app_name_for(app_id)]  # ...the pull 404s -> fresh
    assert handle.app_name == app_name_for(app_id)


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
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    user, project_id = await _mk(db_session, "m19@rvaiglobal.com")
    manager = SessionManager()

    class SlowTeardown(FakeSandboxClient):
        """Blocks _do_finalize inside teardown so the session sits terminal_committed but
        still finalizing (the exact window a fast refine lands in)."""

        def __init__(self) -> None:
            super().__init__()
            self.entered = asyncio.Event()
            self.gate = asyncio.Event()

        async def teardown(self, handle: SandboxHandle) -> None:
            self.entered.set()
            await self.gate.wait()
            await super().teardown(handle)

    client = SlowTeardown()
    first = await manager.start(
        db_session, user, project_id, "p", run_build=FakeBrain(), sandbox_client=client
    )
    await client.entered.wait()  # finalize is mid-teardown: terminal committed, not done
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

    client.gate.set()  # finalize completes -> the waiting start proceeds FRESH
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
    # Fully finalized, no leak, teardown/release ran exactly once.
    assert session.terminal_committed is True
    assert await lock_is_held(fake_redis, user.id) is False
    assert client.torn_down.count(app_name_for(session.app_id)) == 1
    assert manager.active_session_for(user.id) is None
    assert isinstance(session.envelopes[-1], EndedEvent)


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


class _EnvCapturingClient(FakeSandboxClient):
    """Captures the app_env handed to provision_new / restore_from_snapshot."""

    def __init__(self) -> None:
        super().__init__()
        self.provision_env: dict[str, str] | None = None
        self.restore_env: dict[str, str] | None = None

    async def provision_new(
        self, user_id: str, app_name: str, *, app_env: dict[str, str]
    ) -> SandboxHandle:
        self.provision_env = dict(app_env)
        return await super().provision_new(user_id, app_name, app_env=app_env)

    async def restore_from_snapshot(
        self, user_id: str, app_name: str, *, app_env: dict[str, str]
    ) -> SandboxHandle:
        self.restore_env = dict(app_env)
        return await super().restore_from_snapshot(user_id, app_name, app_env=app_env)


async def test_provision_injects_both_blob_vars_alongside_the_c9_four(
    db_session: AsyncSession,
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[uuid.UUID] = []
    _patch_provision(monkeypatch, calls)
    user, project_id = await _mk(db_session, "m22@rvaiglobal.com")
    manager = SessionManager()
    client = _EnvCapturingClient()

    session = await manager.start(
        db_session, user, project_id, "p", run_build=FakeBrain(), sandbox_client=client
    )
    assert session.task is not None
    await session.task

    assert calls == [session.app_id]  # storage provisioned once, for this app
    assert client.provision_env is not None
    assert client.provision_env["BIAL_BLOB_CONTAINER_URL"] == _BLOB_VARS["BIAL_BLOB_CONTAINER_URL"]
    assert client.provision_env["BIAL_BLOB_SAS"] == _BLOB_VARS["BIAL_BLOB_SAS"]
    # Merged, not replaced — the four C9 vars are still present (six total).
    assert client.provision_env["BIAL_APP_ID"] == str(session.app_id)
    assert "BIAL_DATA_BASE_URL" in client.provision_env


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
    client = _EnvCapturingClient()
    app_id, app_key = await resolve_app_for_project(db_session, user.id, project_id)
    await db_session.commit()
    await fake_storage.put(snapshot_key(app_id), b"BUNDLE")  # no registry + snapshot -> restore

    handle = await manager._resolve_sandbox(
        client, user.id, app_id, build_app_env(app_id, app_key)
    )
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
    app_id, app_key = await resolve_app_for_project(db_session, user.id, project_id)
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

    handle = await manager._resolve_sandbox(
        client, user.id, app_id, build_app_env(app_id, app_key)
    )
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
