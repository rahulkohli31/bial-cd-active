"""The build-outcome row factory, `write_build_outcome`, and its non-interference with the
legacy `build_started` marker's rows. It is a test fake: the live readers it feeds are what
these tests are really about.

`write_build_started` ITSELF IS DELETED — the build-start path it belonged to is gone. Rows of
its shape are permanent in production transcripts, though, so the reader covered here
(`write_build_outcome`'s idempotency probe) still has to step around one. It is exercised
against a faithful legacy row produced by
`tests.fakes.write_legacy_build_started`, which is byte-identical to the deleted writer.

The per-step BRAIN producer is tested with its own fixtures in
`tests/services/orchestrator/test_transcript_steps.py`. There was a third producer on the relay,
covered by `tests/api/v1/claude/test_transcript_persist.py`; both were deleted when the relay was
retired. This file covers the manager-level lifecycle rows, which need nothing but the store and
factories.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa

from src.db.models.message import Message, MessageEntryKind, MessageVisibility
from src.services.messages.store import load_history, load_rows
from tests.factories import ConversationFactory, ProjectFactory, UserFactory
from tests.fakes import BuildSessionStatus, write_build_outcome, write_legacy_build_started


async def _thread(db_session):
    user = await UserFactory.create(db_session)
    project = await ProjectFactory.create(db_session, user.id)
    conversation = await ConversationFactory.create(db_session, user.id, project_id=project.id)
    return user, project, conversation


async def test_build_started_row_is_hidden_and_replay_inert(db_session) -> None:
    """Pins the SHAPE of a legacy `build_started` row, not the behaviour of a writer.

    The production writer is deleted; what survives is a database full of rows it already wrote
    and the live readers over them — the projection's `BuildInProgressItem` arm and the outcome
    idempotency probe. Those readers are only as trustworthy as
    the row they are tested against, so this pins that row: hidden `system_event`, empty native
    payload, `meta = {kind, sessionId, startedSeq}`, invisible to both transcript reads."""
    user, _, conversation = await _thread(db_session)
    session_id = uuid.uuid4()

    written = await write_legacy_build_started(
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
    """Regression pin for the `kind='build_outcome'` predicate fix: the `build_started` row
    carries the same sessionId, and without that filter it would satisfy the idempotency
    probe and silently suppress the real outcome."""
    user, _, conversation = await _thread(db_session)
    session_id = uuid.uuid4()
    await write_legacy_build_started(
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
