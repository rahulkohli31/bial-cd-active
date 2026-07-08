"""POST /v1/conversations/{id}/messages (append) + DELETE (delete-with-cleanup) — U9.
Byte-stable with Express (409 write-IDOR, idempotent message insert, attachment sweep)."""

from __future__ import annotations

import uuid

from sqlalchemy import select

from src.config import settings
from src.db.models.attachment import Attachment
from src.db.models.conversation import Conversation
from src.db.models.message import Message
from src.services.auth.session_jwt import mint_session_jwt
from tests.factories import ConversationFactory, MessageFactory, UserFactory

_TTL = settings.auth.access_ttl_seconds


def _cookie(jwt: str) -> dict[str, str]:
    return {"Cookie": f"session={jwt}"}


async def _auth(db_session):
    user = await UserFactory.create(db_session)
    return _cookie(mint_session_jwt(user.id, user.token_version, _TTL)), user


def _message(seq: int = 0, parts=None) -> dict:
    return {
        "_id": str(uuid.uuid4()),
        "role": "user",
        "seq": seq,
        "parts": parts or [{"type": "text", "text": "hi"}],
    }


def _header(kind: str = "planning") -> dict:
    return {"kind": kind, "title": "My chat"}


# --- append -------------------------------------------------------------------


async def test_append_creates_header_and_message(client, db_session) -> None:
    headers, user = await _auth(db_session)
    cid = str(uuid.uuid4())
    msg = _message()
    resp = await client.post(
        f"/v1/conversations/{cid}/messages",
        headers=headers,
        json={"message": msg, "header": _header()},
    )
    assert resp.status_code == 201
    assert resp.json() == {"ok": True, "message": {"_id": msg["_id"], "seq": 0}}

    conv = await db_session.scalar(select(Conversation).where(Conversation.id == uuid.UUID(cid)))
    assert conv is not None
    assert conv.user_id == user.id
    assert conv.title == "My chat"
    stored = await db_session.scalar(select(Message).where(Message.id == uuid.UUID(msg["_id"])))
    assert stored is not None
    assert stored.parts == [{"type": "text", "text": "hi"}]


async def test_append_upserts_header_once(client, db_session) -> None:
    headers, user = await _auth(db_session)
    cid = str(uuid.uuid4())
    await client.post(
        f"/v1/conversations/{cid}/messages",
        headers=headers,
        json={"message": _message(seq=0), "header": _header()},
    )
    await client.post(
        f"/v1/conversations/{cid}/messages",
        headers=headers,
        json={
            "message": _message(seq=1, parts=[{"type": "text", "text": "again"}]),
            "header": _header(),
        },
    )
    convs = (
        (await db_session.execute(select(Conversation).where(Conversation.id == uuid.UUID(cid))))
        .scalars()
        .all()
    )
    assert len(convs) == 1  # header upserted, not duplicated
    msgs = (
        (
            await db_session.execute(
                select(Message)
                .where(Message.conversation_id == uuid.UUID(cid))
                .order_by(Message.seq)
            )
        )
        .scalars()
        .all()
    )
    assert [m.seq for m in msgs] == [0, 1]


async def test_append_cross_user_409(client, db_session) -> None:
    headers_a, _ = await _auth(db_session)
    cid = str(uuid.uuid4())
    await client.post(
        f"/v1/conversations/{cid}/messages",
        headers=headers_a,
        json={"message": _message(), "header": _header()},
    )
    # User B tries to append under the same conversation id → 409.
    headers_b, _ = await _auth(db_session)
    resp = await client.post(
        f"/v1/conversations/{cid}/messages",
        headers=headers_b,
        json={"message": _message(), "header": _header()},
    )
    assert resp.status_code == 409
    assert resp.json() == {"error": {"message": "Conversation id already in use."}}


async def test_append_idempotent_message(client, db_session) -> None:
    headers, _ = await _auth(db_session)
    cid = str(uuid.uuid4())
    msg = _message()
    body = {"message": msg, "header": _header()}
    r1 = await client.post(f"/v1/conversations/{cid}/messages", headers=headers, json=body)
    r2 = await client.post(f"/v1/conversations/{cid}/messages", headers=headers, json=body)
    assert r1.status_code == 201 and r2.status_code == 201
    msgs = (
        (await db_session.execute(select(Message).where(Message.id == uuid.UUID(msg["_id"]))))
        .scalars()
        .all()
    )
    assert len(msgs) == 1  # duplicate _id is idempotent, not a second row


async def test_append_invalid_kind_400(client, db_session) -> None:
    headers, _ = await _auth(db_session)
    cid = str(uuid.uuid4())
    resp = await client.post(
        f"/v1/conversations/{cid}/messages",
        headers=headers,
        json={"message": _message(), "header": {"kind": "bogus"}},
    )
    assert resp.status_code == 400
    assert resp.json() == {
        "error": {"message": "header.kind must be planning, assistant, or builder."}
    }


async def test_append_invalid_message_and_parts(client, db_session) -> None:
    headers, _ = await _auth(db_session)
    cid = str(uuid.uuid4())
    cases = [
        (
            {"role": "user", "seq": 0, "parts": [{"type": "text", "text": "x"}]},
            "message._id is invalid",
        ),
        (
            {
                "_id": str(uuid.uuid4()),
                "role": "bot",
                "seq": 0,
                "parts": [{"type": "text", "text": "x"}],
            },
            "message.role must be user or assistant",
        ),
        (
            {
                "_id": str(uuid.uuid4()),
                "role": "user",
                "seq": "n",
                "parts": [{"type": "text", "text": "x"}],
            },
            "message.seq must be a number",
        ),
        (
            {"_id": str(uuid.uuid4()), "role": "user", "seq": 0, "parts": []},
            "message.parts must be a non-empty array",
        ),
        (
            {"_id": str(uuid.uuid4()), "role": "user", "seq": 0, "parts": [{"type": "weird"}]},
            "unsupported part type: weird",
        ),
        (
            {
                "_id": str(uuid.uuid4()),
                "role": "user",
                "seq": 0,
                "parts": [
                    {"type": "file", "attachmentId": "att_1", "kind": "bad", "mediaType": "x"}
                ],
            },
            "a file part has an invalid kind",
        ),
        (
            {
                "_id": str(uuid.uuid4()),
                "role": "user",
                "seq": 0,
                "parts": [
                    {"type": "file", "attachmentId": "att_1", "kind": "deck", "mediaType": "x"}
                ],
            },
            "a deck file part has an invalid pdfFileId",
        ),
    ]
    for message, expected in cases:
        resp = await client.post(
            f"/v1/conversations/{cid}/messages",
            headers=headers,
            json={"message": message, "header": _header()},
        )
        assert resp.status_code == 400, message
        assert resp.json() == {"error": {"message": expected}}, message


# --- delete with cleanup ------------------------------------------------------


async def test_delete_sweeps_attachments(client, db_session, fake_storage) -> None:
    headers, user = await _auth(db_session)
    conv = await ConversationFactory.create(db_session, user.id)
    key = f"att/{user.id}/obj1"
    db_session.add(
        Attachment(
            user_id=user.id,
            attachment_id="att_swept",
            media_type="image/png",
            name="",
            size=10,
            storage_key=key,
        )
    )
    fake_storage.objects[key] = b"bytes"
    await MessageFactory.create(
        db_session,
        user.id,
        conv.id,
        seq=0,
        parts=[
            {
                "type": "file",
                "attachmentId": "att_swept",
                "kind": "image",
                "mediaType": "image/png",
                "size": 10,
            }
        ],
    )

    resp = await client.delete(f"/v1/conversations/{conv.id}", headers=headers)
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    # Conversation + its messages gone (cascade), attachment row + object swept.
    assert await db_session.scalar(select(Conversation).where(Conversation.id == conv.id)) is None
    assert (
        await db_session.execute(select(Message).where(Message.conversation_id == conv.id))
    ).scalars().all() == []
    assert (
        await db_session.scalar(select(Attachment).where(Attachment.attachment_id == "att_swept"))
        is None
    )
    assert fake_storage.objects == {}


async def test_delete_cross_user_404(client, db_session) -> None:
    headers, _ = await _auth(db_session)
    other = await UserFactory.create(db_session)
    theirs = await ConversationFactory.create(db_session, other.id)
    resp = await client.delete(f"/v1/conversations/{theirs.id}", headers=headers)
    assert resp.status_code == 404
    # Untouched.
    assert (
        await db_session.scalar(select(Conversation).where(Conversation.id == theirs.id))
        is not None
    )


async def test_delete_invalid_id_400(client, db_session) -> None:
    headers, _ = await _auth(db_session)
    resp = await client.delete("/v1/conversations/bad!id", headers=headers)
    assert resp.status_code == 400
    assert resp.json() == {"error": {"message": "Invalid conversation id."}}


async def test_append_requires_auth(client) -> None:
    resp = await client.post(f"/v1/conversations/{uuid.uuid4()}/messages", json={})
    assert resp.status_code == 401
