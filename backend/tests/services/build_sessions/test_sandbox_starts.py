"""One row per sandbox start: written when a container is about to be born, closed on its first
page or on its failure, and never written for an attach.

The create-path stages and sub-steps are timed inside the real `AcaSandboxClient`, so the tests
that read them drive it over a recording control plane and a scripted supervisor. The canned
client is enough wherever the question is which row, of which kind, was written.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from typing import cast, get_args

import httpx
import pytest
import redis.asyncio as aioredis
import sqlalchemy as sa
import structlog.testing
from pydantic import SecretStr
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models.function import AgentInfo, FunctionModel
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

import src.services.turns.engine as engine_mod
from src.config import settings
from src.core.connectors import CONNECTORS
from src.db.models.app_registry import AppRegistry
from src.db.models.conversation import ChatKind
from src.db.models.sandbox_start import (
    SandboxProjectType,
    SandboxStart,
    SandboxStartKind,
    SandboxStartMiss,
    SandboxStartOutcome,
)
from src.db.models.user import User
from src.services.agent.mode_prompts import PromptContext
from src.services.build_sessions import manager as manager_module
from src.services.build_sessions.alarms import SANDBOX_START_NOT_RECORDED_EVENT
from src.services.build_sessions.appdata import resolve_app_for_project
from src.services.build_sessions.integrity import IntegrityVerdict, WorkspaceState
from src.services.build_sessions.locks import read_registry
from src.services.build_sessions.manager import BuildSession, SessionManager
from src.services.build_sessions.sandbox_starts import StartRecord
from src.services.lake.config import LakeConfig
from src.services.lake.env import connector_env_names
from src.services.orchestrator.deps import SandboxSession
from src.services.redis.keys import REGISTRY_FIELD_SERVING_SINCE
from src.services.sandbox import SandboxHandle
from src.services.sandbox.base import a_fresh_sandbox_name
from src.services.sandbox.client import _REINSTALLED_MARKER, _RESTORE_SCRIPT, AcaSandboxClient
from src.services.sandbox.config import SandboxConfig
from src.services.sandbox.stopwatch import Miss
from src.services.storage import snapshot_key
from src.services.turns.engine import TurnEngine, _TurnState, set_turn_engine_for_tests
from src.services.turns.guard import _mid_reply
from tests.api.v1.build_sessions.test_relaunch import RecordingAca, SupervisorScript
from tests.factories import AppRegistryFactory, ConversationFactory, ProjectFactory, UserFactory
from tests.fakes import FakeSandboxClient, FakeStorage, a_ready_pool_row, detached_work_done

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]


def _config() -> SandboxConfig:
    return SandboxConfig(
        subscription_id="s",
        resource_group="r",
        region="westeurope",
        managed_environment_name="aca-env",
        acr_server="acr.azurecr.io",
        acr_username="acr-user",
        acr_password=SecretStr("acr-pass"),
        image_ref="acr/img:latest",
    )


@pytest.fixture(autouse=True)
def _sandbox_configured(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(settings, "sandbox", _config())


@pytest.fixture(autouse=True)
def handed_over(monkeypatch: pytest.MonkeyPatch) -> list[object]:
    """A switch's outgoing container, recorded rather than shut down: what the routine does once
    spawned is `test_shutdown.py`'s subject, and left running it reaches the fakes mid-test."""
    spawned: list[object] = []

    def _record(owed: object, **_aimed_at: object) -> None:
        spawned.append(owed)

    monkeypatch.setattr(manager_module, "shut_it_down_in_the_background", _record)
    return spawned


@pytest.fixture
def books(db_session: AsyncSession) -> SessionFactory:
    """The rolled-back test session, handed out as the manager's own: a start's row names a user
    and an app that exist only inside this test's transaction."""

    @contextlib.asynccontextmanager
    async def _session() -> AsyncIterator[AsyncSession]:
        yield db_session

    return lambda: _session()


@pytest.fixture
def manager(books: SessionFactory) -> SessionManager:
    return SessionManager(session_factory=books)


@pytest.fixture
async def aca(fake_redis: aioredis.Redis) -> AsyncIterator[SimpleNamespace]:
    """The real client over a recording control plane and a scripted supervisor."""
    control_plane = RecordingAca()
    supervisor = SupervisorScript()
    client = AcaSandboxClient(
        _config(), transport=httpx.MockTransport(supervisor), aca=control_plane
    )
    yield SimpleNamespace(client=client, supervisor=supervisor, control_plane=control_plane)
    await client.aclose()


async def _mk(db: AsyncSession, email: str) -> tuple[User, uuid.UUID]:
    user = await UserFactory.create(db, email=email)
    project = await ProjectFactory.create(db, user.id)
    return user, project.id


async def _saved(
    db: AsyncSession, user: User, project_id: uuid.UUID, store: FakeStorage
) -> uuid.UUID:
    app_id = await resolve_app_for_project(db, user.id, project_id)
    await db.commit()
    await store.put(snapshot_key(app_id), b"BUNDLE")
    return app_id


async def _rows(db: AsyncSession, user_id: uuid.UUID) -> list[SandboxStart]:
    found = await db.scalars(
        sa.select(SandboxStart)
        .where(SandboxStart.user_id == user_id)
        .order_by(SandboxStart.started_at)
        .execution_options(populate_existing=True)
    )
    return list(found.all())


# --- the relaunch door -------------------------------------------------------------------------


async def test_a_restoring_relaunch_writes_one_reopen_row_with_every_stage_filled(
    db_session: AsyncSession, fake_storage: FakeStorage, manager: SessionManager, aca
) -> None:
    """A reopen is where the time between the create and the dev server starting has to be
    explained, so every stage and every step inside it has to be there.

    Mutation checks: drop the `created` split in `_provision_container` and `create_ms` goes
    red; drop the `first_page` split in `_bring_it_up` and `first_page_ms` goes red."""
    user, project_id = await _mk(db_session, "st-reopen@rvaiglobal.com")
    app_id = await _saved(db_session, user, project_id, fake_storage)

    admitted = await manager.relaunch_preview(db_session, user, project_id, aca.client)
    await detached_work_done(manager)

    [row] = await _rows(db_session, user.id)
    assert row.id == admitted.start_id
    assert (row.kind, row.app_id, row.claimed) == (SandboxStartKind.REOPEN, app_id, False)
    assert row.project_type is SandboxProjectType.PLAIN
    # The pool's sizes default to zero, so the start created its container and says why.
    assert (row.miss_reason, row.ready_count) == (SandboxStartMiss.SIZE_ZERO, 0)
    assert row.outcome is SandboxStartOutcome.SERVED
    assert row.ended_at is not None and row.ended_at >= row.started_at
    stages = {
        "admission_ms": row.admission_ms,
        "settings_ms": row.settings_ms,
        "create_ms": row.create_ms,
        "dev_start_ms": row.dev_start_ms,
        "first_page_ms": row.first_page_ms,
    }
    assert all(ms is not None and ms >= 0 for ms in stages.values()), stages
    # The stages are contiguous, so they add up to the door-to-first-page time to within the
    # millisecond each one rounds away.
    total_ms = (row.ended_at - row.started_at) / timedelta(milliseconds=1)
    assert 0 <= total_ms - sum(ms for ms in stages.values() if ms is not None) <= len(stages)
    assert row.browser_visible_ms is None
    assert set(row.sub_steps) == {"files", "restore_exec", "registry_write", "dev_start"}
    assert row.reinstalled is False


async def test_a_relaunch_that_takes_a_ready_container_records_the_claim(
    db_session: AsyncSession,
    fake_storage: FakeStorage,
    manager: SessionManager,
    aca,
    empty_sandbox_pool: None,
) -> None:
    """What tells a start the pool served from one it did not: it claimed, nothing was missed,
    how many were ready, and a create stage that is the claim itself.

    Mutation check: drop the claim's facts from `StartRecord.close` and every field here reads as
    the column default."""
    aca.client._config = _config().model_copy(update={"pool_day_size": 1, "pool_night_size": 1})
    member = a_fresh_sandbox_name()
    fqdn = aca.control_plane.made_for_the_pool(member)
    await a_ready_pool_row(member, fqdn=fqdn, image_ref="acr/img:latest")
    user, project_id = await _mk(db_session, "st-claim@rvaiglobal.com")
    await _saved(db_session, user, project_id, fake_storage)

    await manager.relaunch_preview(db_session, user, project_id, aca.client)
    await detached_work_done(manager)

    [row] = await _rows(db_session, user.id)
    assert (row.claimed, row.miss_reason, row.ready_count) == (True, None, 1)
    await asyncio.gather(*aca.client._detached)
    [the_replacement] = aca.control_plane.create_calls
    assert aca.control_plane.created[the_replacement]["BIAL_POOL_MEMBER"] == "1"
    assert row.create_ms is not None
    assert {"bearer_read", "configure", "registry_write"} <= set(row.sub_steps)
    assert row.outcome is SandboxStartOutcome.SERVED


async def test_a_relaunch_whose_watch_runs_out_keeps_the_stages_it_reached(
    db_session: AsyncSession,
    fake_storage: FakeStorage,
    manager: SessionManager,
    aca,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A reopen slow enough to outlast its watch is the one whose time from the create to the dev
    server most needs reading, so its stages are kept even though no page was seen."""
    monkeypatch.setattr(manager_module, "_COLD_READY_BUDGET_SECONDS", 0.05)
    monkeypatch.setattr(manager_module, "READINESS_POLL_S", 0)
    aca.supervisor.root_status = 404
    user, project_id = await _mk(db_session, "st-outlasted@rvaiglobal.com")
    await _saved(db_session, user, project_id, fake_storage)

    await manager.relaunch_preview(db_session, user, project_id, aca.client)
    await detached_work_done(manager)

    [row] = await _rows(db_session, user.id)
    assert (row.outcome, row.ended_at, row.first_page_ms) == (None, None, None)
    assert row.create_ms is not None and row.dev_start_ms is not None
    assert set(row.sub_steps) == {"files", "restore_exec", "registry_write", "dev_start"}


class _ReinstallingSupervisor(SupervisorScript):
    """A supervisor whose restore reinstalls, because the saved lockfile is not the image's."""

    def __call__(self, request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/exec"):
            if json.loads(request.content).get("cmd") == ["sh", "-c", _RESTORE_SCRIPT]:
                self.paths.append("/exec")
                stdout = f"{_REINSTALLED_MARKER}\nadded 3 packages in 9s\n"
                return httpx.Response(200, json={"stdout": stdout, "stderr": "", "exit": 0})
        return super().__call__(request)


async def test_a_restore_whose_lockfile_moved_records_the_reinstall(
    db_session: AsyncSession, fake_storage: FakeStorage, fake_redis, manager: SessionManager
) -> None:
    # Mutation check: read the marker as never present and this goes red.
    user, project_id = await _mk(db_session, "st-reinstall@rvaiglobal.com")
    await _saved(db_session, user, project_id, fake_storage)
    client = AcaSandboxClient(
        _config(), transport=httpx.MockTransport(_ReinstallingSupervisor()), aca=RecordingAca()
    )

    await manager.relaunch_preview(db_session, user, project_id, client)
    await detached_work_done(manager)
    await client.aclose()

    [row] = await _rows(db_session, user.id)
    assert row.reinstalled is True


async def test_a_relaunch_that_attaches_writes_no_row(
    db_session: AsyncSession, fake_storage: FakeStorage, manager: SessionManager, aca
) -> None:
    """Reopening the project already running is not a start, and counting it as one would put a
    second of attach beside a minute of restore under one kind."""
    user, project_id = await _mk(db_session, "st-attach@rvaiglobal.com")
    await _saved(db_session, user, project_id, fake_storage)

    cold = await manager.relaunch_preview(db_session, user, project_id, aca.client)
    await detached_work_done(manager)
    warm = await manager.relaunch_preview(db_session, user, project_id, aca.client)
    await detached_work_done(manager)

    assert len(aca.control_plane.create_calls) == 1, "guard the premise: the second attached"
    assert warm.start_id is None
    assert [row.id for row in await _rows(db_session, user.id)] == [cold.start_id]


async def test_a_start_that_fails_before_its_first_page_is_closed_failed(
    db_session: AsyncSession, fake_storage: FakeStorage, manager: SessionManager, aca
) -> None:
    """A fresh container whose dev server will not start is a failed start. The stages it never
    reached stay empty rather than reading as zero.

    Mutation check: drop the close from the lock's compensation and the outcome goes red."""
    user, project_id = await _mk(db_session, "st-failed@rvaiglobal.com")
    await _saved(db_session, user, project_id, fake_storage)
    aca.supervisor.dev_start_status = 500

    await manager.relaunch_preview(db_session, user, project_id, aca.client)
    await detached_work_done(manager)

    [row] = await _rows(db_session, user.id)
    assert row.outcome is SandboxStartOutcome.FAILED
    assert row.ended_at is not None
    assert row.create_ms is not None, "guard the premise: the container was created"
    assert (row.dev_start_ms, row.first_page_ms) == (None, None)


async def test_a_database_that_will_not_record_does_not_fail_the_start(
    db_session: AsyncSession, fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """Timing is observability: a start whose row cannot be written comes up exactly as it
    would have, and the miss is said once.

    Mutation check: narrow the record's `except` to `IntegrityError` and the start fails."""
    user, project_id = await _mk(db_session, "st-dbdown@rvaiglobal.com")
    await _saved(db_session, user, project_id, fake_storage)

    @contextlib.asynccontextmanager
    async def _the_database_is_down() -> AsyncIterator[AsyncSession]:
        raise OperationalError("INSERT INTO sandbox_starts", {}, ConnectionRefusedError())
        yield db_session  # pragma: no cover - unreachable; makes this a generator

    manager = SessionManager(session_factory=lambda: _the_database_is_down())
    client = FakeSandboxClient()

    with structlog.testing.capture_logs() as logs:
        admitted = await manager.relaunch_preview(db_session, user, project_id, client)
        await detached_work_done(manager)

    assert admitted.start_id is None
    reg = await read_registry(fake_redis, user.id)
    assert reg is not None and reg.get(REGISTRY_FIELD_SERVING_SINCE), "the app did not come up"
    unrecorded = [entry for entry in logs if entry["event"] == SANDBOX_START_NOT_RECORDED_EVENT]
    assert [entry["step"] for entry in unrecorded] == ["open"]


async def test_a_database_that_will_not_record_the_close_does_not_raise(
    db_session: AsyncSession, books: SessionFactory
) -> None:
    """The close runs on the start's way out, a failed start's included, so a refused write must
    not replace what the start itself raised.

    Mutation check: narrow the close's `except` to `IntegrityError` and the close raises."""
    user = await UserFactory.create(db_session, email="st-closedown@rvaiglobal.com")
    app = await AppRegistryFactory.create(db_session, user_id=user.id)

    @contextlib.asynccontextmanager
    async def _the_database_is_down() -> AsyncIterator[AsyncSession]:
        raise OperationalError("UPDATE sandbox_starts", {}, ConnectionRefusedError())
        yield db_session  # pragma: no cover - unreachable; makes this a generator

    up_then_down = iter((books, lambda: _the_database_is_down()))
    record = StartRecord()
    record.admitted(SandboxStartKind.REOPEN, user_id=user.id, books=lambda: next(up_then_down)())
    await record.open(app_id=app.id, env={"BIAL_APP_ID": str(app.id)})
    assert record.on_the_books == record.id, "guard the premise: the row was written"

    with structlog.testing.capture_logs() as logs:
        await record.close(SandboxStartOutcome.FAILED)

    unrecorded = [entry for entry in logs if entry["event"] == SANDBOX_START_NOT_RECORDED_EVENT]
    assert [(entry["step"], entry["start_id"]) for entry in unrecorded] == [
        ("close", str(record.id))
    ]
    [row] = await _rows(db_session, user.id)
    assert row.outcome is None


async def test_a_relaunch_that_takes_the_slot_from_another_project_is_a_switch(
    db_session: AsyncSession,
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
    manager: SessionManager,
    handed_over: list[object],
) -> None:
    # Mutation check: drop the switch arm of `_kind_of_start` and the second kind goes red.
    user, project_a = await _mk(db_session, "st-switch@rvaiglobal.com")
    project_b = (await ProjectFactory.create(db_session, user.id)).id
    await _saved(db_session, user, project_a, fake_storage)
    await _saved(db_session, user, project_b, fake_storage)
    client = FakeSandboxClient()

    await manager.relaunch_preview(db_session, user, project_a, client)
    await detached_work_done(manager)
    await manager.relaunch_preview(db_session, user, project_b, client)
    await detached_work_done(manager)

    assert len(handed_over) == 1, "guard the premise: the first project was handed over"
    kinds = [row.kind for row in await _rows(db_session, user.id)]
    assert kinds == [SandboxStartKind.REOPEN, SandboxStartKind.SWITCH]


# --- the turn's door ---------------------------------------------------------------------------


async def test_a_new_projects_first_turn_writes_a_new_project_row(
    db_session: AsyncSession, fake_redis, fake_storage: FakeStorage, manager: SessionManager
) -> None:
    # Mutation check: decide a turn's kind from anything but the project's app row and one of
    # this test and the next goes red.
    user, project_id = await _mk(db_session, "st-new@rvaiglobal.com")

    session = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=FakeSandboxClient(), may_write=True
    )

    [row] = await _rows(db_session, user.id)
    assert row.kind is SandboxStartKind.NEW_PROJECT
    assert row.id == session.start.on_the_books
    assert row.app_id == session.app_id
    assert row.project_type is SandboxProjectType.PLAIN


async def test_a_turn_for_a_saved_project_with_nothing_running_writes_a_chat_row(
    db_session: AsyncSession, fake_redis, fake_storage: FakeStorage, manager: SessionManager
) -> None:
    user, project_id = await _mk(db_session, "st-chat@rvaiglobal.com")
    await _saved(db_session, user, project_id, fake_storage)
    client = FakeSandboxClient()

    await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=client, may_write=True
    )

    assert client.restored, "guard the premise: the turn restored the saved app"
    assert [row.kind for row in await _rows(db_session, user.id)] == [SandboxStartKind.CHAT]


async def test_a_second_message_that_attaches_writes_no_row(
    db_session: AsyncSession, fake_redis, fake_storage: FakeStorage, manager: SessionManager
) -> None:
    """Every message calls the allocator; only the one that brought a container up is a start."""
    user, project_id = await _mk(db_session, "st-message@rvaiglobal.com")
    client = FakeSandboxClient()
    first = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=client, may_write=True
    )
    await manager.finish_turn_sandbox(first)
    client.attach_handle = first.handle

    second = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=client, may_write=True
    )

    assert second.attached, "guard the premise: the second message attached"
    assert second.start.on_the_books is None
    assert [row.id for row in await _rows(db_session, user.id)] == [first.start.on_the_books]


@pytest.fixture
def _no_turn_left_behind():
    _mid_reply.clear()
    yield
    set_turn_engine_for_tests(None)
    _mid_reply.clear()


async def test_a_turns_workspace_frame_carries_the_start_it_began(
    db_session: AsyncSession,
    fake_redis,
    fake_storage: FakeStorage,
    books: SessionFactory,
    _no_turn_left_behind,
) -> None:
    """The browser times a turn's start against this id, so it must be the row's.

    The app root answers without a page, so the watcher never closes the row while the turn
    still holds the session it writes through."""
    engine = TurnEngine()
    set_turn_engine_for_tests(engine)
    user = await UserFactory.create(db_session, email="st-frame@rvaiglobal.com")
    project = await ProjectFactory.create(db_session, user.id)
    conv = await ConversationFactory.create(
        db_session, user.id, project_id=project.id, kind=ChatKind.PLAN
    )
    client = FakeSandboxClient()
    client.root_status = 404

    async def _answer(_messages: list[ModelMessage], _info: AgentInfo):
        yield "It lists the visitors."

    async def _noop() -> None:
        return None

    await engine.start_turn(
        conversation=conv,
        user_id=user.id,
        prompt="what does the home page do?",
        history=[],
        prompt_context=PromptContext(
            user_name="Ada", project_name="Visitors", project_description=None
        ),
        app_id=None,
        project_id=project.id,
        model=FunctionModel(stream_function=_answer),
        # The engine names the real sessionmaker's type; it only ever calls the factory.
        session_factory=cast("async_sessionmaker[AsyncSession]", books),
        persist_user_turn=_noop,
        manager=SessionManager(session_factory=books),
        sandbox_client=client,
    )
    state = engine.peek(conv.id)
    assert state is not None and state.task is not None
    await asyncio.wait_for(state.task, timeout=10)

    [row] = await _rows(db_session, user.id)
    ready = [
        frame
        for frame in state.ring
        if getattr(frame, "type", None) == "workspace" and getattr(frame, "state", None) == "ready"
    ]
    assert [getattr(frame, "start_id", None) for frame in ready] == [row.id]
    assert row.kind is SandboxStartKind.NEW_PROJECT


async def test_a_start_is_timed_only_by_the_turn_that_began_it(
    db_session: AsyncSession,
    fake_redis,
    fake_storage: FakeStorage,
    books: SessionFactory,
    aca,
    _no_turn_left_behind,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A new project's first reply often lands while the first page is still compiling. The row
    keeps the stages that turn reached, and the next turn, which sees the app serve after the
    person typed again, writes nothing into it: that wait is theirs, not the start's.

    Mutation checks: drop the close from `finish_turn_sandbox` and the first turn's stages are
    never written; carry one record across turns and reopen it after the close, and the second
    turn's dev start and page land in the row."""
    engine = TurnEngine()
    set_turn_engine_for_tests(engine)
    user = await UserFactory.create(db_session, email="st-two-turns@rvaiglobal.com")
    project = await ProjectFactory.create(db_session, user.id)
    conv = await ConversationFactory.create(
        db_session, user.id, project_id=project.id, kind=ChatKind.PLAN
    )
    manager = SessionManager(session_factory=books)
    serving = asyncio.Event()

    async def _intact(*_args: object) -> IntegrityVerdict:
        # The scripted supervisor has no repository to show the integrity gate.
        return IntegrityVerdict(state=WorkspaceState.INTACT, reason="scripted")

    monkeypatch.setattr(manager_module, "workspace_integrity", _intact)

    async def _answer(_messages: list[ModelMessage], _info: AgentInfo):
        if aca.supervisor.root_status is None:
            await asyncio.wait_for(_framed(), timeout=5)
        yield "Which visitors should it list?"

    async def _framed() -> None:
        while not any(getattr(f, "type", None) == "preview" for f in _ring()):
            await asyncio.sleep(0.01)
        serving.set()

    def _ring() -> list[object]:
        state = engine.peek(conv.id)
        return list(state.ring) if state is not None else []

    async def _noop() -> None:
        return None

    async def _a_turn() -> _TurnState:
        await engine.start_turn(
            conversation=conv,
            user_id=user.id,
            prompt="build a visitor log",
            history=[],
            prompt_context=PromptContext(
                user_name="Ada", project_name="Visitors", project_description=None
            ),
            app_id=None,
            project_id=project.id,
            model=FunctionModel(stream_function=_answer),
            session_factory=cast("async_sessionmaker[AsyncSession]", books),
            persist_user_turn=_noop,
            manager=manager,
            sandbox_client=aca.client,
        )
        state = engine.peek(conv.id)
        assert state is not None and state.task is not None
        await asyncio.wait_for(state.task, timeout=10)
        return state

    aca.supervisor.root_status = 404  # still compiling: the root answers with no page
    await _a_turn()
    [after_the_first] = await _rows(db_session, user.id)
    first_turns = (after_the_first.create_ms, after_the_first.dev_start_ms)
    assert after_the_first.dev_start_ms is not None, "the first turn's stages were not kept"

    await asyncio.sleep(0.3)  # the person reads the reply and types again
    aca.supervisor.root_status = None
    second = await _a_turn()

    assert second.write_session is not None and second.write_session.attached
    assert serving.is_set(), "guard the premise: the second turn saw the app serve"
    [row] = await _rows(db_session, user.id)
    assert (row.create_ms, row.dev_start_ms) == first_turns
    assert (row.first_page_ms, row.outcome, row.ended_at) == (None, None, None)


async def test_the_turns_watcher_closes_the_start_on_the_first_page_it_sees(
    db_session: AsyncSession,
    fake_redis,
    books: SessionFactory,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Mutation check: drop the close from `_watch_preview` and this times out.
    monkeypatch.setattr(engine_mod, "READINESS_POLL_S", 0)
    user = await UserFactory.create(db_session, email="st-watch@rvaiglobal.com")
    app = await AppRegistryFactory.create(db_session, user_id=user.id)
    record = StartRecord()
    record.admitted(SandboxStartKind.CHAT, user_id=user.id, books=books)
    await record.open(app_id=app.id, env={})
    record.split("dev_started")
    closed = asyncio.Event()
    close = record.close

    async def _close_and_say_so(outcome: SandboxStartOutcome) -> None:
        await close(outcome)
        closed.set()

    monkeypatch.setattr(record, "close", _close_and_say_so)
    state = _a_turn_holding(user.id, app, record)

    watcher = asyncio.create_task(TurnEngine()._watch_preview(state))
    await asyncio.wait_for(closed.wait(), timeout=5)
    watcher.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await watcher

    [row] = await _rows(db_session, user.id)
    assert row.outcome is SandboxStartOutcome.SERVED
    assert row.first_page_ms is not None


def _a_turn_holding(user_id: uuid.UUID, app: AppRegistry, record: StartRecord) -> _TurnState:
    state = _TurnState(
        turn_id=uuid.uuid4(), conversation_id=uuid.uuid4(), user_id=user_id, kind=ChatKind.BUILD
    )
    fqdn = "sbx-watch.westeurope.azurecontainerapps.io"
    handle = SandboxHandle(
        fqdn=fqdn,
        token="tok-watch",  # noqa: S106 - a fake, never a real bearer
        app_name="sbx-watch",
        preview_url=f"https://{fqdn}/",
        ready=True,
    )
    client = FakeSandboxClient()
    state.sandbox = SandboxSession(sandbox_client=client, handle=handle, app_id=app.id)
    state.write_session = BuildSession(
        session_id=uuid.uuid4(),
        user_id=user_id,
        project_id=app.project_id,
        app_id=app.id,
        lock_token="token",  # noqa: S106 - a fake lock token
        handle=handle,
        start=record,
    )
    return state


# --- what a row says about its start -----------------------------------------------------------


def test_every_reason_a_start_gives_for_creating_is_one_its_row_can_hold() -> None:
    """The stopwatch carries a reason as a `Literal` and the row as a native enum, so a reason
    only one side knows fails the close that would record it."""
    assert set(get_args(Miss)) == {miss.value for miss in SandboxStartMiss}


async def test_a_connector_start_is_recorded_as_never_asking_the_pool(
    db_session: AsyncSession, books: SessionFactory
) -> None:
    """The newest reason reaches the database's own type, not only the model's enum."""
    user = await UserFactory.create(db_session, email="st-connector-miss@rvaiglobal.com")
    app = await AppRegistryFactory.create(db_session, user_id=user.id)
    record = StartRecord()
    record.admitted(SandboxStartKind.REOPEN, user_id=user.id, books=books)
    await record.open(app_id=app.id, env={"BIAL_APP_ID": "x"})
    record.missed("connector", ready_count=None)

    await record.close(SandboxStartOutcome.SERVED)

    [row] = await _rows(db_session, user.id)
    assert (row.claimed, row.miss_reason, row.ready_count) == (
        False,
        SandboxStartMiss.CONNECTOR,
        None,
    )


@pytest.mark.parametrize(
    ("granted", "project_type"),
    [(True, SandboxProjectType.CONNECTOR), (False, SandboxProjectType.PLAIN)],
)
async def test_a_start_whose_environment_carries_the_data_identity_is_a_connector_project(
    db_session: AsyncSession,
    books: SessionFactory,
    monkeypatch: pytest.MonkeyPatch,
    granted: bool,
    project_type: SandboxProjectType,
) -> None:
    """The project type is the fact the create reads the identity from, never a second decision.

    Mutation check: write every row as plain and the connector case goes red."""
    monkeypatch.setattr(
        settings,
        "connector_lake",
        LakeConfig(
            url="https://alake.blob.core.windows.net/c/reports/",
            identity_client_id="52b74947-0621-46e2-a523-a6b466f47c33",
            identity_resource_id="/subscriptions/s/resourcegroups/rg/providers/x/y/an-identity",
        ),
    )
    url_name, client_id_name = connector_env_names(next(iter(CONNECTORS)))
    env = {"BIAL_APP_ID": "x"}
    if granted:
        env |= {url_name: "https://alake.blob.core.windows.net/c/reports/", client_id_name: "id"}
    user = await UserFactory.create(db_session, email=f"st-type-{granted}@rvaiglobal.com")
    app = await AppRegistryFactory.create(db_session, user_id=user.id)
    record = StartRecord()
    record.admitted(SandboxStartKind.REOPEN, user_id=user.id, books=books)

    await record.open(app_id=app.id, env=env)

    [row] = await _rows(db_session, user.id)
    assert row.project_type is project_type


# --- the row's lifetime -----------------------------------------------------------------------


async def test_rows_go_with_their_app_and_with_their_user(db_session: AsyncSession) -> None:
    """A row names who started which app, so it never outlives either."""
    owner = await UserFactory.create(db_session, email="st-owner@rvaiglobal.com")
    other = await UserFactory.create(db_session, email="st-other@rvaiglobal.com")
    owners_app = await AppRegistryFactory.create(db_session, user_id=owner.id)
    others_app = await AppRegistryFactory.create(db_session, user_id=other.id)
    for user, app in ((owner, owners_app), (other, others_app)):
        db_session.add(
            SandboxStart(
                user_id=user.id,
                app_id=app.id,
                kind=SandboxStartKind.REOPEN,
                project_type=SandboxProjectType.PLAIN,
                started_at=datetime.now(UTC),
            )
        )
    await db_session.flush()

    await db_session.execute(sa.delete(AppRegistry).where(AppRegistry.id == owners_app.id))
    assert await _rows(db_session, owner.id) == []
    assert len(await _rows(db_session, other.id)) == 1, "another user's row went with it"

    await db_session.execute(sa.delete(User).where(User.id == other.id))
    assert await _rows(db_session, other.id) == []
