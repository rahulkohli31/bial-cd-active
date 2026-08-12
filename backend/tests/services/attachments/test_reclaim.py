"""`reclaim_orphaned_attachments` — the never-sent-upload sweep (R10 / U9).

The eligibility rule is the crux: a row is orphaned iff (a) NO sent message references its
token AND (b) it is older than the 48h window. NULL `conversation_id` is legacy, never a
deletion proxy; the reference scan decides every candidate. Both halves are user-scoped, so
a token that collides across owners cannot let one user's message shield another's orphan.
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import func, select

from src.db.models.attachment import Attachment
from src.services.attachments import (
    NEVER_SENT_RECLAIM_WINDOW,
    reclaim_orphaned_attachments,
)
from src.services.extract.office import PPTX_MEDIA_TYPE
from tests.factories import ConversationFactory, MessageFactory, UserFactory
from tests.fakes import FakeStorage

_NOW = datetime.datetime(2026, 7, 21, 12, 0, tzinfo=datetime.UTC)
_OLD = _NOW - datetime.timedelta(days=30)  # comfortably past the 48h window
_FRESH = _NOW - datetime.timedelta(hours=1)  # mid-composition, inside the window


async def _add_attachment(
    db,
    storage: FakeStorage,
    *,
    user_id: uuid.UUID,
    attachment_id: str,
    created_at: datetime.datetime,
    size: int = 10,
    media_type: str = "image/png",
    conversation_id: uuid.UUID | None = None,
    with_pdf_sibling: bool = False,
) -> str:
    """Persist an attachment row AND its stored blob(s); return the storage key."""
    key = f"att/{user_id}/{attachment_id}"
    db.add(
        Attachment(
            user_id=user_id,
            attachment_id=attachment_id,
            media_type=media_type,
            name="",
            size=size,
            storage_key=key,
            conversation_id=conversation_id,
            created_at=created_at,
        )
    )
    await db.flush()
    storage.objects[key] = b"x" * size
    if with_pdf_sibling:
        storage.objects[key + ".pdf"] = b"pdf"
    return key


async def _file_message(db, *, user_id: uuid.UUID, attachment_id: str) -> None:
    """A sent message that references `attachment_id` via a native ref marker (U4 shape)."""
    from pydantic_ai import BinaryContent
    from pydantic_ai.messages import ModelRequest, UserPromptPart

    from src.services.messages.store import dump_for_row

    conv = await ConversationFactory.create(db, user_id)
    await MessageFactory.create(
        db,
        user_id,
        conv.id,
        payload=dump_for_row(
            [
                ModelRequest(
                    parts=[
                        UserPromptPart(
                            content=[
                                BinaryContent(
                                    data=b"\x89PNGx",
                                    media_type="image/png",
                                    identifier=attachment_id,
                                )
                            ]
                        )
                    ]
                )
            ]
        ),
    )


async def test_legacy_unreferenced_orphan_is_reclaimed(db_session) -> None:
    storage = FakeStorage()
    user = await UserFactory.create(db_session)
    key = await _add_attachment(
        db_session, storage, user_id=user.id, attachment_id="att_orphan", created_at=_OLD, size=42
    )

    result = await reclaim_orphaned_attachments(db_session, storage, user_id=user.id, now=_NOW)
    assert result.reclaimed == 1
    assert result.freed_bytes == 42
    assert result.swept_keys == 1
    assert key not in storage.objects
    row = await db_session.scalar(
        select(Attachment).where(Attachment.attachment_id == "att_orphan")
    )
    assert row is None


async def test_legacy_referenced_row_survives(db_session) -> None:
    # conversation_id NULL, 30 days old, its token present in a sent message → NOT eligible.
    # Pins that a NULL link is not a deletion proxy: the reference scan wins.
    storage = FakeStorage()
    user = await UserFactory.create(db_session)
    key = await _add_attachment(
        db_session, storage, user_id=user.id, attachment_id="att_sent", created_at=_OLD
    )
    await _file_message(db_session, user_id=user.id, attachment_id="att_sent")

    result = await reclaim_orphaned_attachments(db_session, storage, user_id=user.id, now=_NOW)
    assert result.reclaimed == 0
    assert key in storage.objects
    row = await db_session.scalar(select(Attachment).where(Attachment.attachment_id == "att_sent"))
    assert row is not None


async def test_linked_referenced_row_survives(db_session) -> None:
    # Non-NULL conversation_id, 30 days old, referenced by a sent message → the reference scan
    # wins over age for linked rows too.
    storage = FakeStorage()
    user = await UserFactory.create(db_session)
    conv = await ConversationFactory.create(db_session, user.id)
    await _add_attachment(
        db_session,
        storage,
        user_id=user.id,
        attachment_id="att_linked_sent",
        created_at=_OLD,
        conversation_id=conv.id,
    )
    await _file_message(db_session, user_id=user.id, attachment_id="att_linked_sent")

    result = await reclaim_orphaned_attachments(db_session, storage, user_id=user.id, now=_NOW)
    assert result.reclaimed == 0
    row = await db_session.scalar(
        select(Attachment).where(Attachment.attachment_id == "att_linked_sent")
    )
    assert row is not None


async def test_fresh_unreferenced_not_eligible(db_session) -> None:
    # No referencing message but age < 48h → the mid-composition file is still needed.
    storage = FakeStorage()
    user = await UserFactory.create(db_session)
    key = await _add_attachment(
        db_session, storage, user_id=user.id, attachment_id="att_fresh", created_at=_FRESH
    )

    result = await reclaim_orphaned_attachments(db_session, storage, user_id=user.id, now=_NOW)
    assert result.reclaimed == 0
    assert key in storage.objects
    row = await db_session.scalar(
        select(Attachment).where(Attachment.attachment_id == "att_fresh")
    )
    assert row is not None


async def test_reclaim_reduces_sum_size(db_session) -> None:
    storage = FakeStorage()
    user = await UserFactory.create(db_session)
    await _add_attachment(
        db_session, storage, user_id=user.id, attachment_id="att_big", created_at=_OLD, size=1000
    )
    before = await db_session.scalar(
        select(func.coalesce(func.sum(Attachment.size), 0)).where(Attachment.user_id == user.id)
    )
    assert int(before or 0) == 1000

    result = await reclaim_orphaned_attachments(db_session, storage, user_id=user.id, now=_NOW)
    assert result.freed_bytes == 1000

    after = await db_session.scalar(
        select(func.coalesce(func.sum(Attachment.size), 0)).where(Attachment.user_id == user.id)
    )
    assert int(after or 0) == 0


async def test_deck_orphan_sweeps_both_keys(db_session) -> None:
    # A deck upload owns `{storage_key}` AND `{storage_key}.pdf`; reclamation must sweep both,
    # or it deletes the row and leaks the rendered PDF forever.
    storage = FakeStorage()
    user = await UserFactory.create(db_session)
    key = await _add_attachment(
        db_session,
        storage,
        user_id=user.id,
        attachment_id="att_deck",
        created_at=_OLD,
        media_type=PPTX_MEDIA_TYPE,
        with_pdf_sibling=True,
    )
    assert key in storage.objects and key + ".pdf" in storage.objects

    result = await reclaim_orphaned_attachments(db_session, storage, user_id=user.id, now=_NOW)
    assert result.reclaimed == 1
    assert result.swept_keys == 2
    assert key not in storage.objects
    assert key + ".pdf" not in storage.objects


async def test_cross_user_isolation(db_session) -> None:
    storage = FakeStorage()
    user_a = await UserFactory.create(db_session)
    user_b = await UserFactory.create(db_session)
    key_a = await _add_attachment(
        db_session, storage, user_id=user_a.id, attachment_id="att_a", created_at=_OLD
    )
    key_b = await _add_attachment(
        db_session, storage, user_id=user_b.id, attachment_id="att_b", created_at=_OLD
    )

    result = await reclaim_orphaned_attachments(db_session, storage, user_id=user_a.id, now=_NOW)
    assert result.reclaimed == 1
    assert key_a not in storage.objects
    # B's orphan is untouched by a sweep scoped to A.
    assert key_b in storage.objects
    row_b = await db_session.scalar(select(Attachment).where(Attachment.attachment_id == "att_b"))
    assert row_b is not None


async def test_token_collision_does_not_shield_other_users_orphan(db_session) -> None:
    # User A and user B each own an attachment with the SAME client-minted token. B's sent
    # message references that token; A's copy is never sent. Because BOTH halves of the scan
    # are user-scoped, B's message does NOT shield A's orphan (and B's own copy still survives).
    storage = FakeStorage()
    user_a = await UserFactory.create(db_session)
    user_b = await UserFactory.create(db_session)
    key_a = await _add_attachment(
        db_session, storage, user_id=user_a.id, attachment_id="att_shared", created_at=_OLD
    )
    key_b = await _add_attachment(
        db_session, storage, user_id=user_b.id, attachment_id="att_shared", created_at=_OLD
    )
    await _file_message(db_session, user_id=user_b.id, attachment_id="att_shared")

    # A's never-sent orphan IS reclaimed despite the colliding token in B's transcript.
    result_a = await reclaim_orphaned_attachments(db_session, storage, user_id=user_a.id, now=_NOW)
    assert result_a.reclaimed == 1
    assert key_a not in storage.objects

    # B's copy is referenced by B's own message → survives.
    result_b = await reclaim_orphaned_attachments(db_session, storage, user_id=user_b.id, now=_NOW)
    assert result_b.reclaimed == 0
    assert key_b in storage.objects


async def test_second_run_is_a_noop(db_session) -> None:
    storage = FakeStorage()
    user = await UserFactory.create(db_session)
    await _add_attachment(
        db_session, storage, user_id=user.id, attachment_id="att_once", created_at=_OLD
    )
    first = await reclaim_orphaned_attachments(db_session, storage, user_id=user.id, now=_NOW)
    assert first.reclaimed == 1
    second = await reclaim_orphaned_attachments(db_session, storage, user_id=user.id, now=_NOW)
    assert second.reclaimed == 0
    assert second.swept_keys == 0


def test_window_is_the_err_long_48h() -> None:
    # The err-long posture (KD-7): 48h, a large multiple of any upload-then-send interval.
    assert NEVER_SENT_RECLAIM_WINDOW == datetime.timedelta(hours=48)


def test_reference_resolver_is_shared_with_the_cascade() -> None:
    # Drift guard: the reclaimer resolves sent references + derives blob keys through the SAME
    # helpers as the conversation cascade, so a `parts` shape the cascade honours is honoured
    # here too and the two can never diverge. Assert on the function's own resolution namespace
    # (`__globals__`) — that is exactly what `reclaim_orphaned_attachments` will call.
    from src.services.conversations.delete import (
        _blob_keys_for,
        _referenced_attachment_ids,
    )

    resolution_ns = reclaim_orphaned_attachments.__globals__
    assert resolution_ns["_referenced_attachment_ids"] is _referenced_attachment_ids
    assert resolution_ns["_blob_keys_for"] is _blob_keys_for
