"""Message JSONB parts round-trip (U8) — the durable-record canary.

The `/api/claude` chat path is a stateless relay; the SPA appends each turn's neutral
`parts[]` via the conversations API and the server stores it opaquely in JSONB `parts`.
This asserts a mixed-part `parts` value (text / image / office / deck) survives write → read
byte-for-byte, and that `schema_version` defaults to 1 (the `parts` version stamp).
Runs against the real migrated schema in the rolled-back per-test transaction.
"""

from __future__ import annotations

from sqlalchemy import select

from src.db.models.message import Message, MessageRole
from tests.factories import ConversationFactory, MessageFactory, UserFactory

# A realistic mixed-part parts array spanning every part kind the SPA persists.
_PARTS = [
    {"type": "text", "text": "look at these"},
    {
        "type": "file",
        "attachmentId": "att_1",
        "kind": "image",
        "mediaType": "image/png",
        "size": 12,
    },
    {
        "type": "file",
        "attachmentId": "att_2",
        "kind": "office",
        "mediaType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "format": "word",
        "text": "# Heading\n\n| a | b |",
        "truncated": False,
    },
    {
        "type": "file",
        "attachmentId": "att_3",
        "kind": "deck",
        "mediaType": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "pdfFileId": "file_abc123",
        "pageCount": 7,
    },
]


async def test_parts_round_trips_unchanged(db_session) -> None:
    user = await UserFactory.create(db_session)
    conv = await ConversationFactory.create(db_session, user.id)
    msg = await MessageFactory.create(
        db_session, user.id, conv.id, seq=1, role=MessageRole.USER, parts=_PARTS
    )

    # Detach so the query loads a fresh instance straight from PG's JSONB column (a true
    # round-trip, not the identity-map object). Capture the id first — expunge expires it.
    msg_id = msg.id
    db_session.expunge_all()
    reloaded = await db_session.scalar(select(Message).where(Message.id == msg_id))
    assert reloaded is not None
    assert reloaded.parts == _PARTS
    assert reloaded.schema_version == 1  # default stamp


async def test_schema_version_override_persists(db_session) -> None:
    user = await UserFactory.create(db_session)
    conv = await ConversationFactory.create(db_session, user.id)
    msg = await MessageFactory.create(
        db_session, user.id, conv.id, schema_version=2, parts=[{"type": "text", "text": "v2"}]
    )
    msg_id = msg.id
    db_session.expunge_all()
    reloaded = await db_session.scalar(select(Message).where(Message.id == msg_id))
    assert reloaded is not None
    assert reloaded.schema_version == 2
