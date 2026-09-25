"""Discard: the app goes back to the version its owner saved, inside the container it runs in.

What the discard replaces is parked rather than deleted, the saved copy is left exactly as its
owner wrote it, and every conversation of the project that spoke since the save gets a note its
next reply reads."""

from __future__ import annotations

import base64
import contextlib
import uuid
from collections.abc import AsyncIterator, Sequence
from dataclasses import replace
from datetime import datetime, timedelta
from typing import Any

import pytest
import redis.asyncio as aioredis
import sqlalchemy as sa
from pydantic import SecretStr
from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart
from sqlalchemy.ext.asyncio import AsyncSession
from structlog.testing import capture_logs

from src.config import settings
from src.db.models.conversation import Conversation
from src.db.models.message import Message, MessageEntryKind
from src.db.models.user import User
from src.services.build_sessions import manager as manager_module
from src.services.build_sessions.alarms import SANDBOX_DEV_STARTED_EVENT
from src.services.build_sessions.manager import (
    BuildSessionConflictError,
    SessionManager,
    app_name_for,
)
from src.services.build_sessions.snapshot import NothingSavedToGoBackToError
from src.services.messages.projection import UserTextItem, WorkspaceDiscardedItem, project_rows
from src.services.messages.store import append_batch, load_history, load_rows
from src.services.sandbox import ExecResult, SandboxHandle
from src.services.sandbox.config import SandboxConfig
from src.services.storage import (
    StorageError,
    head_sha_from_metadata,
    quarantine_prefix,
    snapshot_key,
)
from src.services.storage.bundle import parse_bundle_head_sha
from tests.factories import ConversationFactory, ProjectFactory, UserFactory
from tests.fakes import DevServerDownUntilStarted, FakeStorage, a_git_bundle

SAVED = "5" * 40
LATER = "7" * 40


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


@pytest.fixture
async def manager(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[SessionManager]:
    """A manager whose first-page watchers are drained before the test's loop closes."""
    monkeypatch.setattr(manager_module, "READINESS_POLL_S", 0)
    held = SessionManager()
    yield held
    for watcher in list(held._tasks):
        with contextlib.suppress(Exception):
            await watcher


class _Workspace(DevServerDownUntilStarted):
    """A container holding one tree at a time: moved by the test, bundled as it stands, and put
    back by a discard's reset."""

    def __init__(self, head: str) -> None:
        super().__init__()
        self.head = head
        self.exec_handler = self._answer

    def _answer(self, cmd: list[str]) -> ExecResult:
        if cmd[0] == "sh" and "rev-parse" in cmd[-1]:
            ancestry = "0 0" if "merge-base" in cmd[-1] else ""
            return ExecResult(stdout=f"{self.head}\n@@@@3@@{ancestry}", stderr="", exit=0)
        if cmd[0] == "base64":
            bundle = base64.b64encode(a_git_bundle(self.head)).decode()
            return ExecResult(stdout=bundle, stderr="", exit=0)
        return ExecResult(stdout="", stderr="", exit=0)

    async def reset_to_bundle(self, handle: SandboxHandle, bundle: bytes) -> None:
        await super().reset_to_bundle(handle, bundle)
        self.head = parse_bundle_head_sha(bundle)


async def _a_saved_app_with_later_work(
    db: AsyncSession, manager: SessionManager, email: str
) -> tuple[User, uuid.UUID, uuid.UUID, _Workspace]:
    """Saved at SAVED, then a turn moved the tree to LATER and ended without saving it."""
    user = await UserFactory.create(db, email=email)
    project = await ProjectFactory.create(db, user.id)
    client = _Workspace(SAVED)
    session = await manager.ensure_sandbox(
        db, user, project.id, sandbox_client=client, may_write=True
    )
    await manager.finish_turn_sandbox(session, client, touched=False)
    client.attach_handle = session.handle
    await manager.save_project_snapshot(db, user, project.id, sandbox_client=client)
    client.head = LATER
    assert session.handle is not None
    return user, project.id, session.app_id, client


async def _a_turn(
    db: AsyncSession, user: User, conversation: Conversation, *, at: datetime | None = None
) -> None:
    await append_batch(
        db,
        user_id=user.id,
        conversation_id=conversation.id,
        messages=[
            ModelRequest(parts=[UserPromptPart(content="add a date filter")]),
            ModelResponse(parts=[TextPart(content="Added it.")]),
        ],
        entry_kind=MessageEntryKind.TURN,
        kind=conversation.kind,
    )
    if at is not None:
        await db.execute(
            sa.update(Message)
            .where(Message.conversation_id == conversation.id)
            .values(created_at=at)
        )


async def _no_refs(_ids: Sequence[str]) -> dict[str, tuple[str, str]]:
    return {}


def _parked(store: FakeStorage, app_id: uuid.UUID) -> list[bytes]:
    return [
        data for key, data in store.objects.items() if key.startswith(quarantine_prefix(app_id))
    ]


# --- the tree ------------------------------------------------------------------------------


async def test_a_discard_puts_the_saved_version_back_in_the_running_container(
    db_session: AsyncSession,
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
    manager: SessionManager,
) -> None:
    """★ The same container, reset to the saved tree, with its app started and Save settled."""
    user, project_id, app_id, client = await _a_saved_app_with_later_work(
        db_session, manager, "discard1@rvaiglobal.com"
    )

    outcome = await manager.discard_unsaved_changes(
        db_session, user, project_id, sandbox_client=client, conversation_id=None
    )

    assert client.reset_to == [a_git_bundle(SAVED)]
    assert outcome.state.dirty is False
    assert outcome.state.container_head == SAVED
    assert client.provisioned == [app_name_for(app_id)]
    assert client.restored == []
    assert client.dev_started == [app_name_for(app_id)]


async def test_an_idle_check_during_the_discards_start_leaves_the_app_to_the_discard(
    db_session: AsyncSession,
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
    manager: SessionManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A tab's idle check restarts a stopped app once the start lock is free. Asked while the
    Discard is starting the app it put back, it has to find that lock still held, or it starts a
    second dev server beside the Discard's in a container already short of memory.

    Mutation check: start the app after the `async with` in `discard_unsaved_changes` and
    `dev_started` names the app twice."""
    user, project_id, app_id, client = await _a_saved_app_with_later_work(
        db_session, manager, "discard-idle-race@rvaiglobal.com"
    )
    start = client.dev_start
    asked = False

    async def _the_idle_check_asks_mid_start(handle: SandboxHandle, **kw: Any) -> int:
        nonlocal asked
        if not asked:
            asked = True
            await manager.project_workspace_check(
                db_session, user, project_id, sandbox_client=client
            )
        return await start(handle, **kw)

    monkeypatch.setattr(client, "dev_start", _the_idle_check_asks_mid_start)

    await manager.discard_unsaved_changes(
        db_session, user, project_id, sandbox_client=client, conversation_id=None
    )

    assert asked, "guard the premise: the idle check ran inside the Discard's start"
    assert client.dev_started == [app_name_for(app_id)]


@pytest.mark.parametrize("found_serving", [True, False])
async def test_the_start_line_says_whether_the_discard_found_the_app_running(
    db_session: AsyncSession,
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
    manager: SessionManager,
    found_serving: bool,
) -> None:
    """`already_running` is what the attach read from the container, not a constant.

    Mutation check: hard-code either value in `_boot_the_tree_we_put_back`; one case goes red."""
    user, project_id, _, client = await _a_saved_app_with_later_work(
        db_session, manager, f"discard-start-{str(found_serving).lower()}@rvaiglobal.com"
    )
    assert client.attach_handle is not None
    client.attach_handle = replace(client.attach_handle, ready=found_serving)

    with capture_logs() as logs:
        await manager.discard_unsaved_changes(
            db_session, user, project_id, sandbox_client=client, conversation_id=None
        )

    started = [e for e in logs if e.get("event") == SANDBOX_DEV_STARTED_EVENT]
    assert [(e["arm"], e["already_running"]) for e in started] == [("discard", found_serving)]


async def test_the_discarded_work_is_parked_and_a_restart_cannot_bring_it_back(
    db_session: AsyncSession,
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
    manager: SessionManager,
) -> None:
    """The one durable slot still holds what its owner saved, so the only tree a restart can
    bring back is the discarded-TO one.
    Mutation check: write the live tree to `snapshot_key` here and a restart restores the work
    the citizen just asked to throw away."""
    user, project_id, app_id, client = await _a_saved_app_with_later_work(
        db_session, manager, "discard2@rvaiglobal.com"
    )

    await manager.discard_unsaved_changes(
        db_session, user, project_id, sandbox_client=client, conversation_id=None
    )

    assert _parked(fake_storage, app_id) == [a_git_bundle(LATER)]
    assert head_sha_from_metadata(fake_storage.meta[snapshot_key(app_id)]) == SAVED


async def test_the_next_turn_after_a_discard_finds_its_workspace_intact(
    db_session: AsyncSession,
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
    manager: SessionManager,
) -> None:
    user, project_id, _, client = await _a_saved_app_with_later_work(
        db_session, manager, "discard3@rvaiglobal.com"
    )
    await manager.discard_unsaved_changes(
        db_session, user, project_id, sandbox_client=client, conversation_id=None
    )

    session = await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=client, may_write=True
    )

    assert session.restored is False
    assert client.restored == []


@pytest.mark.parametrize("may_write", [True, False], ids=["build reply", "plan reply"])
async def test_a_discard_waits_for_the_reply_holding_the_workspace(
    db_session: AsyncSession,
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
    manager: SessionManager,
    may_write: bool,
) -> None:
    """A Plan reply reads the files too, so it must not have them reset under it.

    Mutation check: refuse only a writing session, as Save does, and the plan reply goes red."""
    user, project_id, _, client = await _a_saved_app_with_later_work(
        db_session, manager, f"discard4-{may_write}@rvaiglobal.com"
    )
    await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=client, may_write=may_write
    )
    stored = dict(fake_storage.objects)

    with pytest.raises(BuildSessionConflictError):
        await manager.discard_unsaved_changes(
            db_session, user, project_id, sandbox_client=client, conversation_id=None
        )

    assert client.reset_to == []
    assert fake_storage.objects == stored


async def test_with_nothing_saved_a_discard_changes_nothing(
    db_session: AsyncSession,
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
    manager: SessionManager,
) -> None:
    user = await UserFactory.create(db_session, email="discard5@rvaiglobal.com")
    project = await ProjectFactory.create(db_session, user.id)
    client = _Workspace(LATER)
    session = await manager.ensure_sandbox(
        db_session, user, project.id, sandbox_client=client, may_write=True
    )
    await manager.finish_turn_sandbox(session, client, touched=False)
    client.attach_handle = session.handle

    with pytest.raises(NothingSavedToGoBackToError):
        await manager.discard_unsaved_changes(
            db_session, user, project.id, sandbox_client=client, conversation_id=None
        )

    assert client.reset_to == []
    assert _parked(fake_storage, session.app_id) == []


async def test_a_store_that_fails_before_the_reset_leaves_the_work_in_the_container(
    db_session: AsyncSession,
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
    manager: SessionManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mutation check: reset the container before parking the tree and the work is gone."""
    user, project_id, app_id, client = await _a_saved_app_with_later_work(
        db_session, manager, "discard6@rvaiglobal.com"
    )
    real_put = fake_storage.put

    async def refuse_the_park(key, data, **kwargs):
        if key.startswith(quarantine_prefix(app_id)):
            raise StorageError("blob is having a day")
        return await real_put(key, data, **kwargs)

    monkeypatch.setattr(fake_storage, "put", refuse_the_park)

    with pytest.raises(StorageError):
        await manager.discard_unsaved_changes(
            db_session, user, project_id, sandbox_client=client, conversation_id=None
        )

    assert client.reset_to == []
    assert client.head == LATER


# --- the conversations ---------------------------------------------------------------------


async def test_the_conversation_a_discard_came_from_tells_its_next_reply(
    db_session: AsyncSession,
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
    manager: SessionManager,
) -> None:
    """★ The model reads the note as the newest message; the user reads a line, never a bubble."""
    user, project_id, app_id, client = await _a_saved_app_with_later_work(
        db_session, manager, "discard7@rvaiglobal.com"
    )
    conversation = await ConversationFactory.create(db_session, user.id, project_id=project_id)
    await _a_turn(db_session, user, conversation)

    outcome = await manager.discard_unsaved_changes(
        db_session, user, project_id, sandbox_client=client, conversation_id=conversation.id
    )

    history = await load_history(
        db_session, user_id=user.id, conversation_id=conversation.id, rehydrate=_no_refs
    )
    note = history[-1]
    assert isinstance(note, ModelRequest)
    [part] = note.parts
    assert isinstance(part, UserPromptPart)
    assert "discarded every unsaved change" in str(part.content)

    items = project_rows(
        await load_rows(db_session, user_id=user.id, conversation_id=conversation.id)
    )
    assert items[-1] == WorkspaceDiscardedItem(
        seq=outcome.notes[conversation.id], saved_at=fake_storage.mtimes[snapshot_key(app_id)]
    )
    assert [item.text for item in items if isinstance(item, UserTextItem)] == ["add a date filter"]


async def test_every_conversation_that_spoke_since_the_save_is_told_and_no_other(
    db_session: AsyncSession,
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
    manager: SessionManager,
) -> None:
    """Mutation check: drop the since-the-save filter, or the project filter, and a conversation
    that never saw the discarded work is told about it."""
    user, project_id, app_id, client = await _a_saved_app_with_later_work(
        db_session, manager, "discard8@rvaiglobal.com"
    )
    saved_at = fake_storage.mtimes[snapshot_key(app_id)]
    origin = await ConversationFactory.create(db_session, user.id, project_id=project_id)
    spoke_since = await ConversationFactory.create(db_session, user.id, project_id=project_id)
    await _a_turn(db_session, user, spoke_since, at=saved_at + timedelta(minutes=5))
    spoke_before = await ConversationFactory.create(db_session, user.id, project_id=project_id)
    await _a_turn(db_session, user, spoke_before, at=saved_at - timedelta(days=1))
    elsewhere = await ConversationFactory.create(db_session, user.id)
    await _a_turn(db_session, user, elsewhere, at=saved_at + timedelta(minutes=5))

    outcome = await manager.discard_unsaved_changes(
        db_session, user, project_id, sandbox_client=client, conversation_id=origin.id
    )

    assert set(outcome.notes) == {origin.id, spoke_since.id}


async def test_a_conversation_that_is_not_this_users_is_never_written_to(
    db_session: AsyncSession,
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
    manager: SessionManager,
) -> None:
    """Filed under this very project, so the owner predicate alone keeps it out.

    Mutation check: drop `Conversation.user_id` from the note query and the stranger is told."""
    user, project_id, _, client = await _a_saved_app_with_later_work(
        db_session, manager, "discard9@rvaiglobal.com"
    )
    stranger = await UserFactory.create(db_session, email="discard9-stranger@rvaiglobal.com")
    theirs = await ConversationFactory.create(db_session, stranger.id, project_id=project_id)
    await _a_turn(db_session, stranger, theirs)

    outcome = await manager.discard_unsaved_changes(
        db_session, user, project_id, sandbox_client=client, conversation_id=theirs.id
    )

    assert outcome.notes == {}
    written = await db_session.scalar(
        sa.select(sa.func.count()).select_from(Message).where(Message.conversation_id == theirs.id)
    )
    assert written == 1
