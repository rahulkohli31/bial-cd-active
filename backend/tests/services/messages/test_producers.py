"""U5 — lifecycle producers around the native store: the hidden `build_started` marker and its
non-interference with the outcome record's idempotency and status reads.

The per-step BRAIN producer is tested with its own fixtures in
`tests/services/orchestrator/test_transcript_steps.py`; the relay producer in
`tests/api/v1/claude/test_transcript_persist.py`. This file covers the manager-level lifecycle
rows, which need nothing but the store and factories.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa

from src.api.v1.build_sessions.schemas import BuildSessionStatus
from src.db.models.message import Message, MessageEntryKind, MessageVisibility
from src.services.build_sessions.outcome import (
    newest_build_outcome_status,
    write_build_outcome,
    write_build_started,
)
from src.services.messages.store import load_history, load_rows
from tests.factories import ConversationFactory, ProjectFactory, UserFactory


async def _thread(db_session):
    user = await UserFactory.create(db_session)
    project = await ProjectFactory.create(db_session, user.id)
    conversation = await ConversationFactory.create(db_session, user.id, project_id=project.id)
    return user, project, conversation


async def test_build_started_row_is_hidden_and_replay_inert(db_session) -> None:
    user, _, conversation = await _thread(db_session)
    session_id = uuid.uuid4()

    written = await write_build_started(
        db_session,
        user_id=user.id,
        conversation_id=conversation.id,
        session_id=session_id,
        started_seq=-1,
    )
    assert written is True

    row = await db_session.scalar(
        sa.select(Message).where(Message.conversation_id == conversation.id)
    )
    assert row.entry_kind is MessageEntryKind.SYSTEM_EVENT
    assert row.visibility is MessageVisibility.HIDDEN
    assert row.meta == {"kind": "build_started", "sessionId": str(session_id), "startedSeq": -1}
    assert row.payload == []  # the record is the row; nothing replays to the model

    # Projection reads exclude it; the model-history read carries nothing from it.
    visible = await load_rows(db_session, user_id=user.id, conversation_id=conversation.id)
    assert list(visible) == []

    async def _no_refs(attachment_ids) -> dict[str, tuple[str, str]]:
        raise AssertionError("no attachments here")

    assert (
        await load_history(
            db_session, user_id=user.id, conversation_id=conversation.id, rehydrate=_no_refs
        )
        == []
    )


async def test_outcome_idempotency_ignores_the_started_marker(db_session) -> None:
    """Regression pin for the U5 predicate fix: the `build_started` row carries the same
    sessionId, and without the `kind='build_outcome'` filter it would satisfy the idempotency
    probe and silently suppress the real outcome."""
    user, _, conversation = await _thread(db_session)
    session_id = uuid.uuid4()
    await write_build_started(
        db_session,
        user_id=user.id,
        conversation_id=conversation.id,
        session_id=session_id,
        started_seq=-1,
    )

    first = await write_build_outcome(
        db_session,
        user_id=user.id,
        conversation_id=conversation.id,
        session_id=session_id,
        status=BuildSessionStatus.ENDED,
        preview_url=None,
        snapshot_committed=True,
        reason="completed",
        started_seq=-1,
    )
    assert first is True  # the marker did not masquerade as the outcome

    second = await write_build_outcome(
        db_session,
        user_id=user.id,
        conversation_id=conversation.id,
        session_id=session_id,
        status=BuildSessionStatus.ENDED,
        preview_url=None,
        snapshot_committed=True,
        reason="completed",
        started_seq=-1,
    )
    assert second is False  # the real outcome still deduplicates itself


async def test_newest_outcome_status_skips_started_markers(db_session) -> None:
    """A NEWER build's start marker must not blank the newest OUTCOME's status (the relaunch
    label would regress to 'nothing known' the moment any new build starts)."""
    user, project, conversation = await _thread(db_session)
    finished = uuid.uuid4()
    await write_build_outcome(
        db_session,
        user_id=user.id,
        conversation_id=conversation.id,
        session_id=finished,
        status=BuildSessionStatus.FAILED,
        preview_url=None,
        snapshot_committed=True,
        reason="build_failed",
        started_seq=-1,
    )
    await write_build_started(
        db_session,
        user_id=user.id,
        conversation_id=conversation.id,
        session_id=uuid.uuid4(),
        started_seq=0,
    )

    status = await newest_build_outcome_status(db_session, user_id=user.id, project_id=project.id)
    assert status is BuildSessionStatus.FAILED
