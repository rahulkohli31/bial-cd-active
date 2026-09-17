"""Rollback: the workspace goes back to a version its owner chose, and nothing is destroyed.

THE ORDERING IS THE POINT OF THIS FILE. A rollback lands HEAD on an older commit, and
`write_recovery_copy` diverts any tree whose HEAD is not a descendant of the sha stamped on the
recovery slot — so a rollback that resets the container without moving that stamp first poisons
crash recovery permanently rather than for one turn. The store moves before the container here,
and the test below fails if that order is swapped.
"""

from __future__ import annotations

import base64
import contextlib
import uuid
from collections.abc import AsyncIterator

import pytest
import redis.asyncio as aioredis
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings
from src.db.models.user import User
from src.services.build_sessions import manager as manager_module
from src.services.build_sessions.manager import (
    BuildSessionConflictError,
    SessionManager,
    VersionNotOfferedError,
)
from src.services.build_sessions.versions import most_recent
from src.services.sandbox import ExecResult, SandboxHandle
from src.services.sandbox.base import SandboxError
from src.services.sandbox.config import SandboxConfig
from src.services.storage import (
    head_sha_from_metadata,
    quarantine_prefix,
    recovery_key,
    snapshot_key,
)
from src.services.storage.bundle import parse_bundle_head_sha
from tests.factories import ProjectFactory, UserFactory
from tests.fakes import DevServerDownUntilStarted, FakeStorage, a_git_bundle

FIRST = "1" * 40
SECOND = "2" * 40


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
    monkeypatch.setattr(manager_module, "READINESS_POLL_S", 0)
    held = SessionManager()
    yield held
    for watcher in list(held._tasks):
        with contextlib.suppress(Exception):
            await watcher


class _Workspace(DevServerDownUntilStarted):
    """A container holding one tree at a time, moved by the test and put back by a reset."""

    def __init__(self, head: str) -> None:
        super().__init__()
        self.head = head
        self.refuse_reset = False
        self.exec_handler = self._answer

    def _answer(self, cmd: list[str]) -> ExecResult:
        if cmd[0] == "sh" and "rev-parse" in cmd[-1]:
            ancestry = "0 0" if "merge-base" in cmd[-1] else ""
            return ExecResult(stdout=f"{self.head}\n@@@@3@@{ancestry}", stderr="", exit=0)
        if cmd[0] == "base64":
            return ExecResult(
                stdout=base64.b64encode(a_git_bundle(self.head)).decode(), stderr="", exit=0
            )
        return ExecResult(stdout="", stderr="", exit=0)

    async def reset_to_bundle(self, handle: SandboxHandle, bundle: bytes) -> None:
        if self.refuse_reset:
            raise SandboxError("reset to the saved version failed (exit 1)")
        await super().reset_to_bundle(handle, bundle)
        self.head = parse_bundle_head_sha(bundle)


async def _two_saved_versions(
    db: AsyncSession, manager: SessionManager, email: str
) -> tuple[User, uuid.UUID, uuid.UUID, _Workspace]:
    """Saved once at FIRST, then the tree moved and was saved again at SECOND."""
    user = await UserFactory.create(db, email=email)
    project = await ProjectFactory.create(db, user.id)
    client = _Workspace(FIRST)
    session = await manager.ensure_sandbox(
        db, user, project.id, sandbox_client=client, may_write=True
    )
    await manager.finish_turn_sandbox(session, client, touched=False)
    client.attach_handle = session.handle
    await manager.save_project_snapshot(
        db, user, project.id, sandbox_client=client, description="First working version"
    )
    client.head = SECOND
    await manager.save_project_snapshot(
        db, user, project.id, sandbox_client=client, description="Added approval"
    )
    return user, project.id, session.app_id, client


async def test_rolling_back_restores_the_tree_and_keeps_what_it_replaced(
    db_session: AsyncSession,
    manager: SessionManager,
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
) -> None:
    """★ APPEND-ONLY. The restored content becomes current, what it replaced becomes previous, and
    the tree that was in the container is parked rather than dropped.

    Mutation receipt: skip the quarantine write and the replaced tree exists nowhere.
    """
    user, project_id, app_id, client = await _two_saved_versions(
        db_session, manager, "rb-basic@rvaiglobal.com"
    )
    versions = await most_recent(db_session, user_id=user.id, app_id=app_id)
    older = versions[-1]

    outcome = await manager.rollback_to_version(
        db_session,
        user,
        project_id,
        version_id=older.id,
        sandbox_client=client,
        conversation_id=None,
    )

    # The container holds the restored tree.
    assert client.head == FIRST
    # A new row, carrying the restored version's date and words forward.
    now_offered = await most_recent(db_session, user_id=user.id, app_id=app_id)
    assert now_offered[0].id == outcome.version_id
    assert now_offered[0].description == "First working version"
    assert now_offered[0].saved_at == older.saved_at
    # ...and the version it was rolled back from is still there, as previous.
    assert now_offered[1].description == "Added approval"
    # The replaced tree is parked, not gone.
    assert any(key.startswith(quarantine_prefix(app_id)) for key in fake_storage.objects)


async def test_the_recovery_slot_is_armed_before_the_container_is_reset(
    db_session: AsyncSession,
    manager: SessionManager,
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
) -> None:
    """★ THE TRAP THIS WHOLE ORDERING EXISTS FOR. `write_recovery_copy` diverts any tree whose
    HEAD is not a descendant of the sha stamped on the recovery slot. A rollback lands HEAD on an
    OLDER commit, so if the container were reset while the slot still named the newer tree, every
    later turn would write to `divert_key`, fire `recovery_write_did_not_land`, and the slot would
    read as poisoned — crash recovery broken permanently, not for one turn.

    Asserted through a reset that FAILS: the store has already moved, so the slot names the
    restored tree and the container still holds work descending from a head the store knows.

    Mutation receipt: reset before writing the recovery slot and the slot still names the tree
    that was replaced.
    """
    user, project_id, app_id, client = await _two_saved_versions(
        db_session, manager, "rb-recovery@rvaiglobal.com"
    )
    older = (await most_recent(db_session, user_id=user.id, app_id=app_id))[-1]
    client.refuse_reset = True

    with pytest.raises(SandboxError):
        await manager.rollback_to_version(
            db_session,
            user,
            project_id,
            version_id=older.id,
            sandbox_client=client,
            conversation_id=None,
        )

    assert head_sha_from_metadata(fake_storage.meta.get(recovery_key(app_id))) == FIRST
    # The container was never reset, so it still holds the newer tree — intact, not half-done.
    assert client.head == SECOND


async def test_the_restored_tree_becomes_what_the_app_is_saved_at(
    db_session: AsyncSession,
    manager: SessionManager,
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
) -> None:
    """Otherwise the workspace reads as dirty the moment the rollback finishes — against a tree
    the citizen never edited, which is a warning about work that does not exist."""
    user, project_id, app_id, client = await _two_saved_versions(
        db_session, manager, "rb-saved@rvaiglobal.com"
    )
    older = (await most_recent(db_session, user_id=user.id, app_id=app_id))[-1]

    await manager.rollback_to_version(
        db_session,
        user,
        project_id,
        version_id=older.id,
        sandbox_client=client,
        conversation_id=None,
    )

    assert head_sha_from_metadata(fake_storage.meta.get(snapshot_key(app_id))) == FIRST


async def test_rolling_back_twice_returns_to_where_you_were(
    db_session: AsyncSession,
    manager: SessionManager,
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
) -> None:
    """★ THE ROUND TRIP, which is what append-only buys. Current C, previous B → roll back to B →
    current holds B and previous holds C → roll back again and C is current once more. Nothing was
    destroyed at any point, so nothing had to be."""
    user, project_id, app_id, client = await _two_saved_versions(
        db_session, manager, "rb-roundtrip@rvaiglobal.com"
    )
    first_pass = (await most_recent(db_session, user_id=user.id, app_id=app_id))[-1]

    await manager.rollback_to_version(
        db_session,
        user,
        project_id,
        version_id=first_pass.id,
        sandbox_client=client,
        conversation_id=None,
    )
    assert client.head == FIRST

    back_again = (await most_recent(db_session, user_id=user.id, app_id=app_id))[-1]
    await manager.rollback_to_version(
        db_session,
        user,
        project_id,
        version_id=back_again.id,
        sandbox_client=client,
        conversation_id=None,
    )

    assert client.head == SECOND


async def test_a_version_that_is_not_this_apps_is_refused(
    db_session: AsyncSession,
    manager: SessionManager,
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
) -> None:
    """Pressed a row that is no longer offered — another tab saved, or the agent did. Answered
    rather than quietly rolling back to whatever is nearest: the citizen chose a row."""
    user, project_id, _app_id, client = await _two_saved_versions(
        db_session, manager, "rb-stranger@rvaiglobal.com"
    )

    with pytest.raises(VersionNotOfferedError):
        await manager.rollback_to_version(
            db_session,
            user,
            project_id,
            version_id=uuid.uuid7(),
            sandbox_client=client,
            conversation_id=None,
        )


async def test_a_rollback_is_refused_while_a_conversation_holds_the_workspace(
    db_session: AsyncSession,
    manager: SessionManager,
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
) -> None:
    """★ DISCARD'S GATE, NOT SAVE'S. Save refuses only a WRITING session, so the button is not
    dead mid-chat. A rollback replaces every file, so it refuses while ANY session holds the
    container — a Plan reply reading files must not have them reset under it.

    Mutation receipt: use the writing-session gate and a read-only turn no longer blocks it.
    """
    user, project_id, app_id, client = await _two_saved_versions(
        db_session, manager, "rb-held@rvaiglobal.com"
    )
    older = (await most_recent(db_session, user_id=user.id, app_id=app_id))[-1]
    # A read-only turn: attached, not writing. Save would proceed; a rollback must not.
    await manager.ensure_sandbox(
        db_session, user, project_id, sandbox_client=client, may_write=False
    )

    with pytest.raises(BuildSessionConflictError):
        await manager.rollback_to_version(
            db_session,
            user,
            project_id,
            version_id=older.id,
            sandbox_client=client,
            conversation_id=None,
        )
