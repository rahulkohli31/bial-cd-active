"""The retention policy's own half: what it condemns, what it spares, and what it takes with it.

TIME IS INJECTED, NEVER FROZEN. Every function under test takes its cutoff as an argument, which
is what lets a test place a conversation on either side of a boundary without touching the clock —
and what stops the pass and a test drifting into two opinions about "a week ago".

THE SET-BASED CLAIM IS ASSERTED, not assumed: two owners are condemned by one call, and the
surviving conversations of both are still there afterwards.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
import sqlalchemy as sa

from src.db.models.attachment import Attachment
from src.db.models.conversation import ChatKind, Conversation
from src.db.models.message import Message
from src.services.conversations.retention import (
    condemned_conversations,
    gather_and_delete,
    idle_before,
)
from tests.factories import ConversationFactory, MessageFactory, ProjectFactory, UserFactory

_WINDOW = dt.timedelta(days=7)
_NOW = dt.datetime(2026, 9, 22, 12, 0, tzinfo=dt.UTC)
_CUTOFF = idle_before(_NOW, window=_WINDOW)


async def _chat_last_touched(
    db, user_id, *, days_ago: float, messages: int = 0, **overrides
) -> Conversation:
    """A conversation whose `updated_at` sits `days_ago` behind the test's own now.

    THE BACKDATE IS LAST, AFTER ANY MESSAGES, and it has to be: appending one fires the trigger,
    which stamps `now()` and would take the conversation straight back out of the condemned set.
    Writing the column directly is also why a boundary test is honest — the timestamp under test
    is the one it chose, not the one the clock happens to be at.
    """
    conversation = await ConversationFactory.create(db, user_id, **overrides)
    for seq in range(messages):
        await MessageFactory.create(db, user_id, conversation.id, seq=seq)
    await db.execute(
        sa.update(Conversation)
        .where(Conversation.id == conversation.id)
        .values(updated_at=_NOW - dt.timedelta(days=days_ago))
    )
    await db.flush()
    return conversation


async def _an_attachment(db, user_id, conversation_id, *, key: str) -> Attachment:
    attachment = Attachment(
        user_id=user_id,
        conversation_id=conversation_id,
        attachment_id=f"att-{uuid.uuid4().hex[:12]}",
        name="roster.pdf",
        media_type="application/pdf",
        size=512,
        storage_key=key,
    )
    db.add(attachment)
    await db.flush()
    return attachment


# --- what is condemned ------------------------------------------------------------------


async def test_a_conversation_idle_past_the_window_is_condemned(db_session) -> None:
    user = await UserFactory.create(db_session)
    doomed = await _chat_last_touched(db_session, user.id, days_ago=8)

    ids, outstanding = await condemned_conversations(db_session, cutoff=_CUTOFF, limit=10)

    assert doomed.id in ids
    assert outstanding == 0


async def test_a_chat_created_nine_days_ago_and_used_this_morning_survives(db_session) -> None:
    """★ AE7. The signal is LAST TOUCHED, not created — a long-running conversation somebody is
    still using is the case this predicate exists to spare, and the one a naive column gets
    wrong."""
    user = await UserFactory.create(db_session)
    fresh = await _chat_last_touched(db_session, user.id, days_ago=0.2)
    stale = await _chat_last_touched(db_session, user.id, days_ago=8)

    ids, _ = await condemned_conversations(db_session, cutoff=_CUTOFF, limit=10)

    assert stale.id in ids
    assert fresh.id not in ids


async def test_a_conversation_with_no_messages_is_judged_by_its_creation_time(
    db_session,
) -> None:
    """`updated_at` starts life equal to `created_at`, and the trigger never fires for a chat
    that received nothing — so an empty chat is condemned on its age, which is the fallback the
    policy asks for rather than a gap in it."""
    user = await UserFactory.create(db_session)
    empty = await _chat_last_touched(db_session, user.id, days_ago=30)

    ids, _ = await condemned_conversations(db_session, cutoff=_CUTOFF, limit=10)

    assert empty.id in ids
    assert (
        await db_session.scalar(
            sa.select(sa.func.count())
            .select_from(Message)
            .where(Message.conversation_id == empty.id)
        )
    ) == 0


@pytest.mark.parametrize("kind", list(ChatKind))
async def test_every_kind_is_condemned_alike(db_session, kind: ChatKind) -> None:
    """The policy is about the conversation, not about what it was for."""
    user = await UserFactory.create(db_session)
    project_id = (
        None if kind is ChatKind.GENERIC else (await ProjectFactory.create(db_session, user.id)).id
    )
    doomed = await _chat_last_touched(
        db_session, user.id, days_ago=9, kind=kind, project_id=project_id
    )

    ids, _ = await condemned_conversations(db_session, cutoff=_CUTOFF, limit=10)

    assert doomed.id in ids


async def test_the_cap_takes_the_oldest_and_reports_what_is_left(db_session) -> None:
    """★ The first enabled run's candidate set is the whole historical backlog. The ceiling is
    what lets that run commit at all; the outstanding count is what tells an operator the backlog
    is draining rather than the pass being stuck."""
    user = await UserFactory.create(db_session)
    oldest = await _chat_last_touched(db_session, user.id, days_ago=40)
    middle = await _chat_last_touched(db_session, user.id, days_ago=20)
    newest = await _chat_last_touched(db_session, user.id, days_ago=9)

    ids, outstanding = await condemned_conversations(db_session, cutoff=_CUTOFF, limit=2)

    assert list(ids) == [oldest.id, middle.id]
    assert outstanding == 1
    # Liveness: the third is genuinely a candidate, so the cap is what withheld it.
    later, _ = await condemned_conversations(db_session, cutoff=_CUTOFF, limit=10)
    assert newest.id in later


# --- what the delete takes with it --------------------------------------------------------


async def test_a_condemned_conversation_takes_its_messages_and_attachment_rows(
    db_session,
) -> None:
    user = await UserFactory.create(db_session)
    doomed = await _chat_last_touched(db_session, user.id, days_ago=9, messages=1)
    await _an_attachment(db_session, user.id, doomed.id, key="att/one")

    sweep = await gather_and_delete(db_session, conversation_ids=(doomed.id,), cutoff=_CUTOFF)
    await db_session.flush()

    assert sweep.removed == 1
    assert sweep.blob_keys == ("att/one",)
    assert await db_session.get(Conversation, doomed.id) is None
    assert (
        await db_session.scalar(
            sa.select(sa.func.count())
            .select_from(Message)
            .where(Message.conversation_id == doomed.id)
        )
    ) == 0
    assert (
        await db_session.scalar(
            sa.select(sa.func.count())
            .select_from(Attachment)
            .where(Attachment.conversation_id == doomed.id)
        )
    ) == 0


async def test_a_file_uploaded_to_a_doomed_chat_and_never_sent_goes_too(db_session) -> None:
    """★ THE GAP THE INTERACTIVE PATH STRUCTURALLY CANNOT SEE. It discovers attachments by
    scanning what the stored messages reference, so a file uploaded and never sent is invisible
    to it. The foreign key the upload door stamps is what makes it visible here, and there is no
    separate never-sent sweep because that key is the whole mechanism.

    Mutation receipt: change the attachment read to go through the message payloads and this goes
    red while the sent-attachment test above stays green."""
    user = await UserFactory.create(db_session)
    doomed = await _chat_last_touched(db_session, user.id, days_ago=9)
    await _an_attachment(db_session, user.id, doomed.id, key="att/never-sent")

    sweep = await gather_and_delete(db_session, conversation_ids=(doomed.id,), cutoff=_CUTOFF)

    assert sweep.blob_keys == ("att/never-sent",)


async def test_two_owners_are_swept_in_one_pass_and_their_live_chats_survive(
    db_session,
) -> None:
    """★ SET-BASED, AND THE SPARING IS THE OTHER HALF. A per-owner loop would reach the same
    answer; this asserts that one call does, and that neither owner loses a conversation they are
    still using."""
    one = await UserFactory.create(db_session)
    two = await UserFactory.create(db_session)
    doomed = [
        await _chat_last_touched(db_session, one.id, days_ago=9),
        await _chat_last_touched(db_session, two.id, days_ago=11),
    ]
    spared = [
        await _chat_last_touched(db_session, one.id, days_ago=1),
        await _chat_last_touched(db_session, two.id, days_ago=2),
    ]

    ids, _ = await condemned_conversations(db_session, cutoff=_CUTOFF, limit=100)
    sweep = await gather_and_delete(db_session, conversation_ids=ids, cutoff=_CUTOFF)
    await db_session.flush()
    db_session.expunge_all()

    assert {c.id for c in doomed} <= set(sweep.conversation_ids)
    for survivor in spared:
        assert await db_session.get(Conversation, survivor.id) is not None


async def test_a_conversation_touched_between_selection_and_deletion_survives(
    db_session,
) -> None:
    """★ The race the re-check exists for. Selection and deletion are separated by the attachment
    read, and a citizen who sends a message in that window must keep their conversation.

    Mutation receipt: drop the `updated_at` predicate from the delete and this goes red while
    every other test in this file stays green."""
    user = await UserFactory.create(db_session)
    doomed = await _chat_last_touched(db_session, user.id, days_ago=9)

    ids, _ = await condemned_conversations(db_session, cutoff=_CUTOFF, limit=10)
    assert doomed.id in ids

    # The citizen sends. The trigger is what does this in production; the test writes the same
    # value directly, because what the delete reads is the column rather than the trigger.
    await db_session.execute(
        sa.update(Conversation).where(Conversation.id == doomed.id).values(updated_at=_NOW)
    )
    await db_session.flush()

    sweep = await gather_and_delete(db_session, conversation_ids=ids, cutoff=_CUTOFF)
    await db_session.flush()
    db_session.expunge_all()

    assert sweep.removed == 0
    assert await db_session.get(Conversation, doomed.id) is not None


async def test_nothing_condemned_deletes_nothing(db_session) -> None:
    sweep = await gather_and_delete(db_session, conversation_ids=(), cutoff=_CUTOFF)
    assert sweep.removed == 0
    assert sweep.blob_keys == ()
