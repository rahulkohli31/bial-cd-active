"""The explicit mode switch (U13/R6): atomic with the hidden marker row, refused
mid-reply, idempotent on same-mode, owner-scoped — and the DOWNGRADE marker carries the
capability clarification while the upgrade stays minimal (U4's direction-aware rule)."""

from __future__ import annotations

import uuid

import sqlalchemy as sa

from src.db.models.conversation import Conversation, ConversationMode
from src.db.models.message import Message, MessageEntryKind, MessageVisibility
from src.services.turns.guard import _mid_reply
from tests.api.v1.conversations.test_turn_stream import _headers
from tests.factories import ConversationFactory, UserFactory


async def _rows(db_session, conversation_id):
    return list(
        await db_session.scalars(
            sa.select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.seq)
        )
    )


async def test_switch_writes_the_hidden_marker_and_flips(client, db_session) -> None:
    user = await UserFactory.create(db_session)
    conv = await ConversationFactory.create(db_session, user.id, mode=ConversationMode.ASK)
    headers = _headers(user)

    resp = await client.post(
        f"/v1/conversations/{conv.id}/mode", headers=headers, json={"mode": "write"}
    )
    assert resp.status_code == 200 and resp.json() == {"mode": "write"}

    reloaded = await db_session.get(Conversation, conv.id)
    assert reloaded is not None and reloaded.mode is ConversationMode.WRITE
    rows = await _rows(db_session, conv.id)
    assert len(rows) == 1
    marker = rows[0]
    assert marker.entry_kind is MessageEntryKind.MODE_SWITCH
    assert marker.visibility is MessageVisibility.HIDDEN
    # Upgrade marker stays MINIMAL (no capability prose).
    text = str(marker.payload)
    assert "mode changed: ask" in text and "write" in text
    assert "not available" not in text


async def test_downgrade_out_of_write_carries_the_clarification(client, db_session) -> None:
    user = await UserFactory.create(db_session)
    conv = await ConversationFactory.create(db_session, user.id, mode=ConversationMode.WRITE)
    headers = _headers(user)

    resp = await client.post(
        f"/v1/conversations/{conv.id}/mode", headers=headers, json={"mode": "ask"}
    )
    assert resp.status_code == 200
    rows = await _rows(db_session, conv.id)
    text = str(rows[0].payload)
    # The direction-aware clarification: post-Write history contradicts the current
    # toolset, and ONLY this marker says so (static segments stay R13-clean).
    assert "not available" in text and "Ask" in text


async def test_same_mode_is_an_idempotent_noop(client, db_session) -> None:
    user = await UserFactory.create(db_session)
    conv = await ConversationFactory.create(db_session, user.id, mode=ConversationMode.PLAN)
    resp = await client.post(
        f"/v1/conversations/{conv.id}/mode", headers=_headers(user), json={"mode": "plan"}
    )
    assert resp.status_code == 200 and resp.json() == {"mode": "plan"}
    assert await _rows(db_session, conv.id) == []  # no marker spam


async def test_switch_refused_mid_reply(client, db_session) -> None:
    user = await UserFactory.create(db_session)
    conv = await ConversationFactory.create(db_session, user.id, mode=ConversationMode.ASK)
    _mid_reply.add(conv.id)
    try:
        resp = await client.post(
            f"/v1/conversations/{conv.id}/mode", headers=_headers(user), json={"mode": "plan"}
        )
        assert resp.status_code == 409
    finally:
        _mid_reply.discard(conv.id)
    reloaded = await db_session.get(Conversation, conv.id)
    assert reloaded is not None and reloaded.mode is ConversationMode.ASK


async def test_ownership_and_validation(client, db_session) -> None:
    user = await UserFactory.create(db_session)
    conv = await ConversationFactory.create(db_session, user.id, mode=ConversationMode.ASK)
    other = await UserFactory.create(db_session, email="mode-other@rvaiglobal.com")
    assert (
        await client.post(
            f"/v1/conversations/{conv.id}/mode", headers=_headers(other), json={"mode": "plan"}
        )
    ).status_code == 404
    assert (
        await client.post(
            f"/v1/conversations/{uuid.uuid4()}/mode",
            headers=_headers(user),
            json={"mode": "plan"},
        )
    ).status_code == 404
    assert (
        await client.post(
            f"/v1/conversations/{conv.id}/mode", headers=_headers(user), json={"mode": "yolo"}
        )
    ).status_code == 422
    # CSRF is required (mutating POST).
    assert (
        await client.post(
            f"/v1/conversations/{conv.id}/mode",
            headers=_headers(user, with_csrf=False),
            json={"mode": "plan"},
        )
    ).status_code == 403
