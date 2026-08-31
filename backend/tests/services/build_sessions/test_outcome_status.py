"""`newest_build_outcome_status` — the U6 "last saved version" query (#43/F1): newest-wins
across a project's threads, owner-scoped, and best-effort on absent/unreadable outcomes."""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from src.api.v1.build_sessions.schemas import BuildSessionStatus
from src.db.models.conversation import ChatKind
from src.db.models.message import MessageEntryKind
from src.services.build_sessions.outcome import (
    newest_build_outcome_status,
    write_build_outcome,
)
from tests.factories import ConversationFactory, MessageFactory, ProjectFactory, UserFactory


async def _project_with_thread(db: AsyncSession, email: str):
    user = await UserFactory.create(db, email=email)
    project = await ProjectFactory.create(db, user.id)
    conv = await ConversationFactory.create(
        db, user.id, project_id=project.id, kind=ChatKind.BUILD
    )
    return user, project, conv


async def _record(db: AsyncSession, user, conv, status: BuildSessionStatus, reason: str) -> None:
    assert await write_build_outcome(
        db,
        user_id=user.id,
        conversation_id=conv.id,
        session_id=uuid.uuid4(),
        status=status,
        preview_url=None,
        snapshot_committed=True,
        reason=reason,
    )


async def test_no_outcome_recorded_reads_as_none(db_session: AsyncSession) -> None:
    user, project, _ = await _project_with_thread(db_session, "os1@rvaiglobal.com")
    got = await newest_build_outcome_status(db_session, user_id=user.id, project_id=project.id)
    assert got is None


async def test_newest_outcome_wins(db_session: AsyncSession) -> None:
    user, project, conv = await _project_with_thread(db_session, "os2@rvaiglobal.com")
    await _record(db_session, user, conv, BuildSessionStatus.ENDED, "completed")
    await _record(db_session, user, conv, BuildSessionStatus.FAILED, "build_failed")
    got = await newest_build_outcome_status(db_session, user_id=user.id, project_id=project.id)
    assert got is BuildSessionStatus.FAILED

    # A later clean build supersedes the failure — only the NEWEST outcome speaks.
    await _record(db_session, user, conv, BuildSessionStatus.ENDED, "completed")
    got = await newest_build_outcome_status(db_session, user_id=user.id, project_id=project.id)
    assert got is BuildSessionStatus.ENDED


async def test_scoped_to_the_owner_and_the_project(db_session: AsyncSession) -> None:
    # Cross-user + cross-project isolation (ADR-0004): another user's failed build, and the
    # same user's OTHER project, must both be invisible to this project's answer.
    user, project, _ = await _project_with_thread(db_session, "os3@rvaiglobal.com")
    other_user, _, other_conv = await _project_with_thread(db_session, "os3-other@rvaiglobal.com")
    await _record(db_session, other_user, other_conv, BuildSessionStatus.FAILED, "build_failed")

    other_project = await ProjectFactory.create(db_session, user.id)
    own_other_conv = await ConversationFactory.create(
        db_session, user.id, project_id=other_project.id, kind=ChatKind.BUILD
    )
    await _record(db_session, user, own_other_conv, BuildSessionStatus.FAILED, "build_failed")

    got = await newest_build_outcome_status(db_session, user_id=user.id, project_id=project.id)
    assert got is None


async def test_an_unreadable_status_reads_as_none(db_session: AsyncSession) -> None:
    # The outcome write is best-effort, so the read must be too: a mangled meta is "nothing
    # known", never a crash inside the relaunch path.
    user, project, conv = await _project_with_thread(db_session, "os4@rvaiglobal.com")
    await MessageFactory.create(
        db_session,
        user.id,
        conv.id,
        seq=0,
        entry_kind=MessageEntryKind.SYSTEM_EVENT,
        payload=[],
        meta={"kind": "build_outcome", "status": "not-a-status", "sessionId": "x"},
    )
    got = await newest_build_outcome_status(db_session, user_id=user.id, project_id=project.id)
    assert got is None
