"""A workspace reclaimed around a chat, and the chat continued afterwards.

Two claims, each proved by driving a real turn through the engine and the real sweep:

* while a turn runs, the sweep does not tear down its container, including when the turn cannot
  renew its liveness lease;
* once a container has gone, the next message restores the workspace and continues, whatever
  gap the stored tool results have, and never throws.

The sweep does not wait for tool results to be stored, so ordering is asserted at the moment of
teardown, by a container that records what was already durable when it was told to go.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import redis.asyncio as aioredis
import sqlalchemy as sa
from pydantic import SecretStr
from pydantic_ai.messages import ModelResponse, ToolCallPart
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings
from src.db.models.app_registry import AppRegistry
from src.db.models.conversation import ChatKind
from src.services.build_sessions import locks, pass_history, reaper
from src.services.build_sessions import manager as manager_module
from src.services.build_sessions.inventory import OwnedApp
from src.services.build_sessions.pass_history import CopyAttempt
from src.services.messages.store import _INTERRUPTED_RESULT, dump_for_row
from src.services.redis import REGISTRY_STATE_ENDING, REGISTRY_STATE_READY, get_redis
from src.services.redis.keys import (
    REGISTRY_FIELD_APP_NAME,
    REGISTRY_FIELD_CREATED_AT,
    REGISTRY_FIELD_FQDN,
    REGISTRY_FIELD_PREVIEW_STAY_UNTIL,
    REGISTRY_FIELD_STATE,
    heartbeat_key,
    lock_key,
    registry_key,
)
from src.services.sandbox.base import SandboxError, SandboxGoneError, SandboxHandle
from src.services.sandbox.config import SandboxConfig
from src.services.storage import snapshot_key
from src.services.storage.errors import StorageError
from src.services.turns import engine as engine_module
from src.services.turns.engine import (
    STOPPED_AT_A_BOUNDARY,
    TurnEngine,
    publish_cooperative_stop,
    set_turn_engine_for_tests,
)
from src.services.turns.guard import _mid_reply
from tests.continuation import (
    WROTE_THE_PAGE,
    Chat,
    Workspace,
    answering,
    builds_a_page,
    new_chat,
    tool_answers,
    user_message,
    writes_then,
)
from tests.factories import MessageFactory
from tests.fakes import FakeStorage, a_sandbox_name

_SRC = Path(__file__).resolve().parents[3] / "src"
_UNREADABLE = "Your saved app could not be loaded just now"


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


@pytest.fixture(autouse=True)
def _fresh_engine():
    _mid_reply.clear()
    engine = TurnEngine()
    set_turn_engine_for_tests(engine)
    yield engine
    set_turn_engine_for_tests(None)
    _mid_reply.clear()


@pytest.fixture(autouse=True)
def attempts(monkeypatch: pytest.MonkeyPatch) -> list[CopyAttempt]:
    """The sweep's copy records, kept off the database: the real writer commits in its own
    session, which no per-test rollback reaches."""
    recorded: list[CopyAttempt] = []

    async def _spy(attempt: CopyAttempt) -> None:
        recorded.append(attempt)

    monkeypatch.setattr(pass_history, "record_durable_copy_attempt", _spy)
    return recorded


@pytest.fixture
def session_factory(db_session: AsyncSession):
    @contextlib.asynccontextmanager
    async def _session():
        yield db_session

    return lambda: _session()


@pytest.fixture
def _a_brisk_lease(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(engine_module, "LIVENESS_LEASE_RENEW_CADENCE_SECONDS", 0.01)


@pytest.fixture
def _no_backoff(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _no_wait(_seconds: float) -> None:
        return None

    monkeypatch.setattr(manager_module, "_asleep", _no_wait)


class _Reclaimable(Workspace):
    """A container the sweep can reach, and that records what was durable when it was destroyed.

    `observe` runs at the top of every teardown; `meanwhile` runs after it, for a start that
    lands while the delete is still in flight."""

    def __init__(self, *, hold_writes: bool = False) -> None:
        super().__init__(hold_writes=hold_writes)
        self.restore_fails = False
        self.observe: Callable[[], Awaitable[dict[str, bool]]] | None = None
        self.at_teardown: list[dict[str, bool]] = []
        self.meanwhile: Callable[[], Awaitable[None]] | None = None

    async def attach_existing(self, user_id: str) -> SandboxHandle:
        reg = await get_redis().hgetall(registry_key(uuid.UUID(user_id)))
        if not reg or reg.get(REGISTRY_FIELD_STATE) == REGISTRY_STATE_ENDING:
            raise SandboxGoneError("no live sandbox for user")
        handle = self.by_name.get(str(reg.get(REGISTRY_FIELD_APP_NAME, "")))
        if handle is None:
            raise SandboxGoneError("no container answers to the record")
        return handle

    async def restore_from_snapshot(self, user_id: str, app_name: str, **kwargs: Any):
        if self.restore_fails:
            raise SandboxError("the restored container did not come up")
        return await super().restore_from_snapshot(user_id, app_name, **kwargs)

    async def teardown(self, handle: SandboxHandle) -> None:
        if self.observe is not None:
            self.at_teardown.append(await self.observe())
        if self.meanwhile is not None:
            await self.meanwhile()
        await super().teardown(handle)


async def _built_chat(
    engine: TurnEngine, db: AsyncSession, session_factory: Any, *, hold_writes: bool = False
) -> tuple[Chat, _Reclaimable]:
    workspace = _Reclaimable(hold_writes=hold_writes)
    chat = await new_chat(engine, db, session_factory, ChatKind.BUILD, workspace=workspace)
    return chat, workspace


async def _the_app(chat: Chat, redis: aioredis.Redis) -> tuple[str, uuid.UUID]:
    reg = await locks.read_registry(redis, chat.user.id)
    assert reg is not None, "the turn registered no container"
    app_id = await chat.db.scalar(
        sa.select(AppRegistry.id).where(AppRegistry.project_id == chat.conversation.project_id)
    )
    assert app_id is not None
    return reg[REGISTRY_FIELD_APP_NAME], app_id


async def _sweep(chat: Chat, redis: aioredis.Redis, workspace: _Reclaimable):
    """The scheduled pass, with no in-process shield: what the worker would run."""
    app_name, app_id = await _the_app(chat, redis)
    return await reaper.sweep_all(
        redis,
        workspace,
        live_users=set(),
        app_ids_by_name={app_name: OwnedApp(app_id, chat.user.id)},
    )


async def _the_stay_runs_out(redis: aioredis.Redis, user_id: uuid.UUID) -> None:
    """A finished turn leaves its container pardoned for a while; this is that while, over."""
    await redis.hdel(registry_key(user_id), REGISTRY_FIELD_PREVIEW_STAY_UNTIL)


def _what_was_durable(chat: Chat, store: FakeStorage, app_id: uuid.UUID):
    async def _observe() -> dict[str, bool]:
        stored = await chat.stored_messages()
        kinds = {
            (part["part_kind"], part.get("tool_call_id"))
            for message in stored
            for part in message["parts"]
        }
        return {
            "call": ("tool-call", "c-write-1") in kinds,
            "answer": ("tool-return", "c-write-1") in kinds,
            "tree": snapshot_key(app_id) in store.objects,
        }

    return _observe


async def _inside_the_write(chat: Chat, workspace: _Reclaimable) -> None:
    await chat.send("add a page", builds_a_page())
    await asyncio.wait_for(workspace.inside.wait(), timeout=10)


# --- while a turn runs ---------------------------------------------------------------------------


async def test_a_sweep_during_a_turn_spares_the_container_on_the_lease_alone(
    _fresh_engine: TurnEngine,
    db_session: AsyncSession,
    session_factory,
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
) -> None:
    chat, workspace = await _built_chat(
        _fresh_engine, db_session, session_factory, hold_writes=True
    )
    await _inside_the_write(chat, workspace)
    # The lease is the only claim left standing: the lock and heartbeat are renewed on the
    # loop's 30-second cadence, so taking them away here is not undone during the test.
    await fake_redis.delete(lock_key(chat.user.id), heartbeat_key(chat.user.id))
    assert await locks.liveness_lease_is_held(fake_redis, chat.user.id)

    result = await _sweep(chat, fake_redis, workspace)

    assert result == reaper.SweepResult(reaped=0, failed=0)
    assert workspace.torn_down == []
    workspace.let_go.set()
    state = await chat.settled()
    assert state.status == "completed"
    assert tool_answers(await chat.history())["c-write-1"] == WROTE_THE_PAGE


async def test_a_sweep_during_a_turn_whose_lease_cannot_be_renewed_still_spares_it(
    _fresh_engine: TurnEngine,
    db_session: AsyncSession,
    session_factory,
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The lease write fails on every tick; the lock and heartbeat the same loop renews in a
    separate arm are what keep the container."""

    async def _store_refuses(*_a: object, **_k: object) -> bool:
        raise RedisError("the store refused the lease")

    monkeypatch.setattr(engine_module, "renew_liveness_lease", _store_refuses)
    chat, workspace = await _built_chat(
        _fresh_engine, db_session, session_factory, hold_writes=True
    )
    await _inside_the_write(chat, workspace)
    assert not await locks.liveness_lease_is_held(fake_redis, chat.user.id)

    result = await _sweep(chat, fake_redis, workspace)

    assert result == reaper.SweepResult(reaped=0, failed=0)
    assert workspace.torn_down == []
    workspace.let_go.set()
    assert (await chat.settled()).status == "completed"
    assert tool_answers(await chat.history())["c-write-1"] == WROTE_THE_PAGE


# --- after the container has gone ------------------------------------------------------------


async def test_a_turn_stopped_at_its_boundary_then_reclaimed_had_stored_its_answer_and_its_tree(
    _fresh_engine: TurnEngine,
    db_session: AsyncSession,
    session_factory,
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
    attempts: list[CopyAttempt],
    _a_brisk_lease: None,
) -> None:
    chat, workspace = await _built_chat(
        _fresh_engine, db_session, session_factory, hold_writes=True
    )
    await _inside_the_write(chat, workspace)
    await publish_cooperative_stop(chat.conversation.id)
    state = _fresh_engine.peek(chat.conversation.id)
    assert state is not None
    for _ in range(2000):
        if state.cooperative_stop_requested:
            break
        await asyncio.sleep(0.001)
    workspace.let_go.set()
    assert (await chat.settled()).end_reason == STOPPED_AT_A_BOUNDARY
    _, app_id = await _the_app(chat, fake_redis)
    workspace.observe = _what_was_durable(chat, fake_storage, app_id)
    await _the_stay_runs_out(fake_redis, chat.user.id)

    result = await _sweep(chat, fake_redis, workspace)

    assert result.reaped == 1
    assert workspace.at_teardown == [{"call": True, "answer": True, "tree": True}]
    assert attempts == [CopyAttempt.COPIED]
    seen = await chat.continues()
    assert len(workspace.restored) == 1
    assert tool_answers(seen) == {"c-write-1": WROTE_THE_PAGE}


async def test_a_turn_cut_mid_tool_then_reclaimed_restores_and_continues_with_the_call_closed(
    _fresh_engine: TurnEngine,
    db_session: AsyncSession,
    session_factory,
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
) -> None:
    chat, workspace = await _built_chat(_fresh_engine, db_session, session_factory)
    second_request = asyncio.Event()
    turn_id = await chat.send("add a page", writes_then(second_request))
    await asyncio.wait_for(second_request.wait(), timeout=10)
    await _fresh_engine.stop_turn(chat.conversation.id, turn_id)
    assert (await chat.settled()).status == "stopped"
    _, app_id = await _the_app(chat, fake_redis)
    workspace.observe = _what_was_durable(chat, fake_storage, app_id)
    await _the_stay_runs_out(fake_redis, chat.user.id)

    result = await _sweep(chat, fake_redis, workspace)

    assert result.reaped == 1
    # The call was durable and its answer never will be: the cut, not the reclaim, lost it.
    assert workspace.at_teardown == [{"call": True, "answer": False, "tree": True}]
    seen = await chat.continues()
    assert len(workspace.restored) == 1
    assert tool_answers(seen) == {"c-write-1": _INTERRUPTED_RESULT}


async def test_a_write_back_that_fails_spares_the_container_and_the_next_sweep_takes_it(
    _fresh_engine: TurnEngine,
    db_session: AsyncSession,
    session_factory,
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
    attempts: list[CopyAttempt],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    chat, workspace = await _built_chat(_fresh_engine, db_session, session_factory)
    await chat.send("add a page", builds_a_page())
    assert (await chat.settled()).status == "completed"
    _, app_id = await _the_app(chat, fake_redis)
    workspace.observe = _what_was_durable(chat, fake_storage, app_id)
    await _the_stay_runs_out(fake_redis, chat.user.id)
    real_put = fake_storage.put

    async def _the_store_says_no(*_a: object, **_k: object) -> None:
        raise StorageError("blob unreachable", provider="fake", key=snapshot_key(app_id))

    monkeypatch.setattr(fake_storage, "put", _the_store_says_no)

    spared = await _sweep(chat, fake_redis, workspace)

    assert spared.reaped == 0
    assert workspace.torn_down == []
    assert await locks.read_registry(fake_redis, chat.user.id) is not None
    monkeypatch.setattr(fake_storage, "put", real_put)

    taken = await _sweep(chat, fake_redis, workspace)

    assert taken.reaped == 1
    assert workspace.at_teardown == [{"call": True, "answer": True, "tree": True}]
    assert attempts == [CopyAttempt.FAILED, CopyAttempt.COPIED]
    await chat.continues()


@pytest.mark.usefixtures("_no_backoff")
async def test_a_tree_that_cannot_be_restored_ends_the_turn_with_news_and_a_later_send_restores(
    _fresh_engine: TurnEngine,
    db_session: AsyncSession,
    session_factory,
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
) -> None:
    chat, workspace = await _built_chat(_fresh_engine, db_session, session_factory)
    await chat.send("add a page", builds_a_page())
    assert (await chat.settled()).status == "completed"
    await _the_stay_runs_out(fake_redis, chat.user.id)
    assert (await _sweep(chat, fake_redis, workspace)).reaped == 1
    before = await chat.stored_messages()
    workspace.restore_fails = True
    model, seen = answering()
    await chat.send("anything new?", model)
    refused = await chat.settled()

    assert refused.status == "failed"
    assert refused.error_message is not None and _UNREADABLE in refused.error_message
    assert seen == []
    stored = await chat.stored_messages()
    assert stored[: len(before)] == before
    workspace.restore_fails = False
    await chat.continues()
    assert len(workspace.restored) == 1


_NEWER = a_sandbox_name("newer")


async def _a_newer_container_takes_the_slot(
    redis: aioredis.Redis, workspace: _Reclaimable, user_id: uuid.UUID
) -> None:
    """What a start leaves once it has registered its own container in this user's slot."""
    workspace.by_name[_NEWER] = reaper.handle_named(_NEWER, fqdn=f"{_NEWER}.example.io")
    await redis.hset(
        registry_key(user_id),
        mapping={
            REGISTRY_FIELD_APP_NAME: _NEWER,
            REGISTRY_FIELD_FQDN: f"{_NEWER}.example.io",
            REGISTRY_FIELD_STATE: REGISTRY_STATE_READY,
            REGISTRY_FIELD_CREATED_AT: datetime.now(UTC).isoformat(),
        },
    )
    await redis.set(lock_key(user_id), "the-newer-token", ex=900)
    await locks.renew_liveness_lease(redis, user_id)


async def test_a_late_teardown_of_the_old_container_leaves_a_newer_one_registered(
    _fresh_engine: TurnEngine,
    db_session: AsyncSession,
    session_factory,
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
) -> None:
    chat, workspace = await _built_chat(_fresh_engine, db_session, session_factory)
    await chat.send("add a page", builds_a_page())
    assert (await chat.settled()).status == "completed"
    old_name, _ = await _the_app(chat, fake_redis)
    await _the_stay_runs_out(fake_redis, chat.user.id)
    user_id = chat.user.id
    workspace.meanwhile = lambda: _a_newer_container_takes_the_slot(fake_redis, workspace, user_id)

    assert (await _sweep(chat, fake_redis, workspace)).reaped == 1

    assert workspace.torn_down == [old_name]
    reg = await locks.read_registry(fake_redis, user_id)
    assert reg is not None and reg[REGISTRY_FIELD_APP_NAME] == _NEWER
    assert await fake_redis.get(lock_key(user_id)) == "the-newer-token"
    assert await locks.liveness_lease_is_held(fake_redis, user_id)


async def test_a_newer_container_registered_during_the_write_back_is_never_marked_ending(
    _fresh_engine: TurnEngine,
    db_session: AsyncSession,
    session_factory,
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A start can take the slot while the sweep is still writing the old tree back. The old
    container is still torn down, by its own name, and the newer one stays attachable.

    Mutation check: mark the record ending by user id alone and the newer container can no longer
    be attached."""
    chat, workspace = await _built_chat(_fresh_engine, db_session, session_factory)
    await chat.send("add a page", builds_a_page())
    assert (await chat.settled()).status == "completed"
    old_name, _ = await _the_app(chat, fake_redis)
    await _the_stay_runs_out(fake_redis, chat.user.id)
    user_id = chat.user.id
    real_put = fake_storage.put

    async def _a_start_lands_mid_write_back(*args: Any, **kwargs: Any) -> object:
        await _a_newer_container_takes_the_slot(fake_redis, workspace, user_id)
        return await real_put(*args, **kwargs)

    monkeypatch.setattr(fake_storage, "put", _a_start_lands_mid_write_back)

    assert (await _sweep(chat, fake_redis, workspace)).reaped == 1

    assert workspace.torn_down == [old_name]
    reg = await locks.read_registry(fake_redis, user_id)
    assert reg is not None and reg[REGISTRY_FIELD_STATE] == REGISTRY_STATE_READY
    assert (await workspace.attach_existing(str(user_id))).app_name == _NEWER


async def test_reopening_after_a_reclaim_repairs_an_older_turns_orphaned_call(
    _fresh_engine: TurnEngine,
    db_session: AsyncSession,
    session_factory,
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
) -> None:
    chat, workspace = await _built_chat(_fresh_engine, db_session, session_factory)
    await MessageFactory.create(
        db_session,
        chat.user.id,
        chat.conversation.id,
        seq=0,
        payload=dump_for_row(
            [
                user_message("start a page"),
                ModelResponse(
                    parts=[
                        ToolCallPart(
                            tool_name="write_file", args='{"path": "a"}', tool_call_id="c-old"
                        )
                    ]
                ),
            ]
        ),
    )
    await chat.send("add a page", builds_a_page())
    assert (await chat.settled()).status == "completed"
    await _the_stay_runs_out(fake_redis, chat.user.id)
    assert (await _sweep(chat, fake_redis, workspace)).reaped == 1

    seen = await chat.continues()

    assert len(workspace.restored) == 1
    answers = tool_answers(seen)
    assert answers["c-old"] == _INTERRUPTED_RESULT
    assert answers["c-write-1"] == WROTE_THE_PAGE


def test_stored_history_reaches_a_model_through_one_loader() -> None:
    """The repair seam holds only while it is the one door: a second place turning stored
    payloads into model messages would skip it. Read off the source tree, so a new caller fails
    here before it ships."""
    callers: dict[str, set[str]] = {
        "load_history": set(),
        "repair_dangling_tool_calls": set(),
        "validate_python": set(),
    }
    for path in _SRC.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            name = (
                node.func.attr
                if isinstance(node.func, ast.Attribute)
                else node.func.id
                if isinstance(node.func, ast.Name)
                else None
            )
            if name not in callers:
                continue
            if name == "validate_python" and not (
                isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name)
                and node.func.value.id == "ModelMessagesTypeAdapter"
            ):
                continue
            callers[name].add(path.relative_to(_SRC).as_posix())

    assert callers == {
        "load_history": {"api/v1/conversations/turns.py"},
        "repair_dangling_tool_calls": {"services/messages/store.py"},
        "validate_python": {"services/messages/store.py"},
    }
