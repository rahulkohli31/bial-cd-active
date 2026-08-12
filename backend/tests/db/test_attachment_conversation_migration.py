"""The attachment→conversation link + its `ON DELETE SET NULL` FK, against the REAL
migrated schema (0021_attachment_conv_link).

The test DB carries the `attachments.conversation_id` column + FK from `alembic upgrade
head`, so these exercise the actual PostgreSQL DDL — the nullable column and, crucially,
the `SET NULL` behaviour that distinguishes this FK from the `CASCADE` used by
`conversations.project_id` — inside the rolled-back per-test transaction (ORM-only, no
DDL here, so no `pg_attribute` slot burn).

The upgrade/downgrade round-trip itself is verified out-of-band via `alembic upgrade head`
/ `downgrade` (U9 Verification) and guarded against a second head by
`tests/test_alembic_single_head.py`; here we prove the shape the migration produced.
"""

from __future__ import annotations

from sqlalchemy import select

from src.db.models.attachment import Attachment
from tests.factories import ConversationFactory, UserFactory


async def test_attachment_conversation_id_defaults_null(db_session) -> None:
    # An upload with no conversation link (existing clients / legacy) stores NULL — the
    # nullable path that keeps the current contract working.
    user = await UserFactory.create(db_session)
    att = Attachment(
        user_id=user.id,
        attachment_id="att_null",
        media_type="image/png",
        name="",
        size=10,
        storage_key=f"att/{user.id}/null",
    )
    db_session.add(att)
    await db_session.flush()
    await db_session.refresh(att)
    assert att.conversation_id is None

    fetched = await db_session.scalar(select(Attachment).where(Attachment.id == att.id))
    assert fetched is not None
    assert fetched.conversation_id is None


async def test_attachment_conversation_link_persists(db_session) -> None:
    user = await UserFactory.create(db_session)
    conv = await ConversationFactory.create(db_session, user.id)
    att = Attachment(
        user_id=user.id,
        attachment_id="att_linked",
        media_type="image/png",
        name="",
        size=10,
        storage_key=f"att/{user.id}/linked",
        conversation_id=conv.id,
    )
    db_session.add(att)
    await db_session.flush()

    fetched = await db_session.scalar(select(Attachment).where(Attachment.id == att.id))
    assert fetched is not None
    assert fetched.conversation_id == conv.id


async def test_delete_conversation_sets_attachment_link_null(db_session) -> None:
    # `ON DELETE SET NULL`, NOT CASCADE: deleting a conversation NULLs the dangling link but
    # leaves the attachment ROW (and its blob) intact — so no blob is stranded ownerless. This
    # is the FK that makes CASCADE's permanent-orphan failure impossible at the DB level.
    user = await UserFactory.create(db_session)
    conv = await ConversationFactory.create(db_session, user.id)
    att = Attachment(
        user_id=user.id,
        attachment_id="att_backstop",
        media_type="image/png",
        name="",
        size=10,
        storage_key=f"att/{user.id}/backstop",
        conversation_id=conv.id,
    )
    db_session.add(att)
    await db_session.flush()

    await db_session.delete(conv)
    await db_session.flush()

    # The DB applied SET NULL; refresh the (still-present) row to see it.
    await db_session.refresh(att)
    assert att.conversation_id is None
    surviving = await db_session.scalar(select(Attachment).where(Attachment.id == att.id))
    assert surviving is not None
    assert surviving.storage_key == f"att/{user.id}/backstop"
