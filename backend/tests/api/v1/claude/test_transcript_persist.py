"""U5/U7 — the relay persists its turns into the native message store, stateless-body edition.

The wire contract is untouched (delta frames + [DONE]); durability is: the newest user turn
lands BEFORE the run, the assistant's responses land BEFORE the terminal [DONE]
(write-before-DONE). Under U7 the browser sends only the new message, so attachments arrive
as OWNED REFERENCES and persist as reference markers — the U5 placeholder arm is gone.
"""

from __future__ import annotations

import base64

import sqlalchemy as sa
from pydantic_ai.models.test import TestModel

from src.config import settings
from src.db.models.message import Message, MessageEntryKind
from src.services.auth.session_jwt import mint_session_jwt
from src.services.messages import store as store_module
from src.services.messages.store import ATTACHMENT_REF_KIND
from tests.factories import ConversationFactory, ProjectFactory, UserFactory

_TTL = settings.auth.access_ttl_seconds
_PNG = bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A]) + b"fake-png-body"


def _cookie(jwt: str) -> dict[str, str]:
    return {"Cookie": f"session={jwt}"}


async def _auth_with_conversation(db_session):
    user = await UserFactory.create(db_session)
    project = await ProjectFactory.create(db_session, user.id)
    conversation = await ConversationFactory.create(db_session, user.id, project_id=project.id)
    return _cookie(mint_session_jwt(user.id, user.token_version, _TTL)), user, conversation


async def _rows(db_session, conversation_id) -> list[Message]:
    return list(
        (
            await db_session.execute(
                sa.select(Message)
                .where(Message.conversation_id == conversation_id)
                .order_by(Message.seq)
            )
        ).scalars()
    )


async def test_turn_persists_user_prompt_then_responses(
    client, db_session, set_chat_model
) -> None:
    """One stateless turn → exactly two rows: the clean user request (no instructions), then
    the assistant's response batch."""
    headers, _, conversation = await _auth_with_conversation(db_session)
    set_chat_model(TestModel(custom_output_text="the answer"))

    resp = await client.post(
        "/v1/claude",
        headers=headers,
        json={"conversationId": str(conversation.id), "message": {"text": "the question"}},
    )
    assert resp.status_code == 200
    assert "data: [DONE]" in resp.text

    rows = await _rows(db_session, conversation.id)
    assert [row.entry_kind for row in rows] == [MessageEntryKind.TURN, MessageEntryKind.TURN]
    assert [row.seq for row in rows] == [0, 1]
    assert [row.kind for row in rows] == [conversation.kind, conversation.kind]

    user_batch = rows[0].payload
    assert len(user_batch) == 1
    [prompt_part] = user_batch[0]["parts"]
    assert prompt_part["content"] == "the question"
    # The pre-written request is CLEAN: the per-run composed instructions never enter a row.
    assert user_batch[0].get("instructions") is None

    response_batch = rows[1].payload
    assert response_batch[0]["kind"] == "response"
    assert any(
        part.get("content") == "the answer"
        for part in response_batch[0]["parts"]
        if isinstance(part, dict)
    )


async def test_second_turn_appends_exactly_two_rows(client, db_session, set_chat_model) -> None:
    """No quadratic rewrite: turn N+1 appends its own two rows and leaves turn N's untouched
    (the server loads history from the DB; the browser sent only the new message)."""
    headers, _, conversation = await _auth_with_conversation(db_session)
    set_chat_model(TestModel(custom_output_text="reply"))

    for text in ("first question", "second question"):
        resp = await client.post(
            "/v1/claude",
            headers=headers,
            json={"conversationId": str(conversation.id), "message": {"text": text}},
        )
        assert resp.status_code == 200
        _ = resp.text

    rows = await _rows(db_session, conversation.id)
    assert [row.seq for row in rows] == [0, 1, 2, 3]
    assert "first question" in str(rows[0].payload)
    assert "second question" in str(rows[2].payload)


async def test_write_failure_never_emits_done(
    client, db_session, set_chat_model, monkeypatch
) -> None:
    """WRITE-BEFORE-DONE: when the post-run append fails, the stream closes truncated —
    deltas may flow, but no [DONE] frame reports a success the DB didn't record."""
    headers, _, conversation = await _auth_with_conversation(db_session)
    set_chat_model(TestModel(custom_output_text="doomed reply"))

    real_append = store_module.append_batch
    calls = {"n": 0}

    async def flaky_append(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] >= 2:  # the pre-run user-turn write succeeds; the response write fails
            raise RuntimeError("disk on fire")
        return await real_append(*args, **kwargs)

    monkeypatch.setattr("src.api.v1.claude.router.append_batch", flaky_append)

    resp = await client.post(
        "/v1/claude",
        headers=headers,
        json={"conversationId": str(conversation.id), "message": {"text": "hello"}},
    )
    assert resp.status_code == 200  # the stream had already committed
    assert calls["n"] == 2
    assert "data: [DONE]" not in resp.text  # truncated, not falsely successful

    rows = await _rows(db_session, conversation.id)
    assert len(rows) == 1  # the user turn survived; no response row lies about the reply


async def test_seq_contention_on_the_user_turn_is_a_409(
    client, db_session, set_chat_model, monkeypatch
) -> None:
    headers, _, conversation = await _auth_with_conversation(db_session)
    set_chat_model(TestModel(custom_output_text="never streams"))

    async def contended_append(*args, **kwargs):
        raise store_module.SeqContentionError("slot taken, twice")

    monkeypatch.setattr("src.api.v1.claude.router.append_batch", contended_append)

    resp = await client.post(
        "/v1/claude",
        headers=headers,
        json={"conversationId": str(conversation.id), "message": {"text": "hello"}},
    )
    assert resp.status_code == 409
    assert "recorded" in resp.json()["error"]["message"]


async def test_attachment_ref_persists_marker_not_bytes(
    client, db_session, set_chat_model, fake_storage
) -> None:
    """U7: the browser sends an OWNED reference; the durable row carries the reference marker
    (never base64), and the fence text blocks persist as their own string items."""
    from src.db.models.attachment import Attachment
    from src.services.storage.keys import owner_prefix

    headers, user, conversation = await _auth_with_conversation(db_session)
    set_chat_model(TestModel(custom_output_text="nice image"))

    attachment_id = "att-relay-1"
    key = f"{owner_prefix(user.id)}{attachment_id}"
    db_session.add(
        Attachment(
            user_id=user.id,
            attachment_id=attachment_id,
            media_type="image/png",
            name="diagram.png",
            size=len(_PNG),
            storage_key=key,
        )
    )
    await db_session.flush()
    fake_storage.objects[key] = _PNG

    fence = '<attachment name="data.csv" type="text">\ncol1,col2\n</attachment>'
    resp = await client.post(
        "/v1/claude",
        headers=headers,
        json={
            "conversationId": str(conversation.id),
            "message": {
                "text": "what is this?",
                "attachmentTexts": [fence],
                "attachmentIds": [attachment_id],
            },
        },
    )
    assert resp.status_code == 200
    assert "data: [DONE]" in resp.text

    rows = await _rows(db_session, conversation.id)
    assert len(rows) == 2
    stored = str(rows[0].payload)
    assert base64.b64encode(_PNG).decode("ascii") not in stored  # bytes never land in a row
    assert ATTACHMENT_REF_KIND in stored and attachment_id in stored
    [prompt_part] = rows[0].payload[0]["parts"]
    content = prompt_part["content"]
    assert isinstance(content, list)
    assert content[-1] == "what is this?"  # typed prose LAST (files-before-text ordering)
    assert fence in content  # the fence rides as its own string item


async def test_unknown_attachment_ref_is_a_400_and_persists_nothing(
    client, db_session, set_chat_model, fake_storage
) -> None:
    headers, _, conversation = await _auth_with_conversation(db_session)
    set_chat_model(TestModel(custom_output_text="never streams"))

    resp = await client.post(
        "/v1/claude",
        headers=headers,
        json={
            "conversationId": str(conversation.id),
            "message": {"text": "look", "attachmentIds": ["att-ghost"]},
        },
    )
    assert resp.status_code == 400
    assert "no longer available" in resp.json()["error"]["message"]
    assert await _rows(db_session, conversation.id) == []  # nothing persisted for a dead turn
