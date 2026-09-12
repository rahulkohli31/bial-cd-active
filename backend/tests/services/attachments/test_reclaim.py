"""`reclaim_orphaned_attachments` — the never-sent-upload sweep.

The eligibility rule is the crux: a row is orphaned iff (a) NO sent message references its
token AND (b) it is older than the 48h window. NULL `conversation_id` is legacy, never a
deletion proxy; the reference scan decides every candidate. Both halves are user-scoped, so
a token that collides across owners cannot let one user's message shield another's orphan.
"""

from __future__ import annotations

import datetime
import uuid

from pydantic_ai.messages import ModelRequest, UserPromptPart
from sqlalchemy import func, select

from src.db.models.attachment import Attachment
from src.services.attachments import (
    NEVER_SENT_RECLAIM_WINDOW,
    reclaim_orphaned_attachments,
)
from src.services.media.lanes import EXCEL_MEDIA_TYPE
from src.services.messages.store import dump_for_row
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
) -> str:
    """Persist an attachment row AND its stored blob; return the storage key."""
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
    return key


async def _file_message(db, *, user_id: uuid.UUID, attachment_id: str) -> None:
    """A sent message that references `attachment_id` via a native ref marker."""
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


async def test_a_sent_code_lane_file_is_never_reclaimed_as_an_orphan(db_session) -> None:
    """★ THE DATA-LOSS BUG THIS MARKER EXISTS TO CLOSE.

    A code-lane file never becomes `BinaryContent`, so `_externalize_binaries` never ran for it and
    the message it was sent with recorded nothing. This scan reads stored payloads to decide what
    is still referenced — so a spreadsheet in an active conversation looked exactly like a file
    nobody ever sent, and 48 hours after upload its row and its blob were deleted underneath a
    citizen still using it.

    The store now writes `ATTACHMENT_FILE_REF_KIND` for the code lane, and this scan reads both
    kinds. Mutation receipt: drop either half — the write in `dump_for_row` or the second kind in
    `_collect_ref_ids` — and this goes red with the file reclaimed.
    """
    storage = FakeStorage()
    user = await UserFactory.create(db_session)
    key = await _add_attachment(
        db_session,
        storage,
        user_id=user.id,
        attachment_id="att_sheet",
        created_at=_OLD,
        media_type=EXCEL_MEDIA_TYPE,
    )
    conv = await ConversationFactory.create(db_session, user.id)
    await MessageFactory.create(
        db_session,
        user.id,
        conv.id,
        payload=dump_for_row(
            [ModelRequest(parts=[UserPromptPart(content="what is in this?")])],
            file_attachment_ids=["att_sheet"],
        ),
    )

    result = await reclaim_orphaned_attachments(db_session, storage, user_id=user.id, now=_NOW)

    assert result.reclaimed == 0, "a live code-lane attachment was reclaimed as never-sent"
    assert key in storage.objects
    row = await db_session.scalar(
        select(Attachment).where(Attachment.attachment_id == "att_sheet")
    )
    assert row is not None


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
    # The reference scan wins over age for linked rows too.
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


# THE DECK-SIBLING SWEEP IS GONE. A .pptx upload used to own `{storage_key}` AND a
# derived `{storage_key}.pdf` from the converter, so reclamation had to remove both or leak the
# rendered PDF forever. Nothing derives anything from an attachment now - a deck is stored as
# itself and read in the sandbox - so `_blob_keys_for` returns one key per row and there is no
# second key to sweep.


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
    assert key_b in storage.objects
    row_b = await db_session.scalar(select(Attachment).where(Attachment.attachment_id == "att_b"))
    assert row_b is not None


async def test_token_collision_does_not_shield_other_users_orphan(db_session) -> None:
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

    result_a = await reclaim_orphaned_attachments(db_session, storage, user_id=user_a.id, now=_NOW)
    assert result_a.reclaimed == 1
    assert key_a not in storage.objects

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
    # The err-long posture: 48h, a large multiple of any upload-then-send interval.
    assert NEVER_SENT_RECLAIM_WINDOW == datetime.timedelta(hours=48)


def test_reference_resolver_is_shared_with_the_cascade() -> None:
    # Drift guard: the reclaimer must resolve references and blob keys through the SAME helpers
    # as the conversation cascade. Asserted on `__globals__` — the function's own resolution
    # namespace — because that is exactly what it will call.
    from src.services.conversations.delete import (
        _blob_keys_for,
        _referenced_attachment_ids,
    )

    resolution_ns = reclaim_orphaned_attachments.__globals__
    assert resolution_ns["_referenced_attachment_ids"] is _referenced_attachment_ids
    assert resolution_ns["_blob_keys_for"] is _blob_keys_for
