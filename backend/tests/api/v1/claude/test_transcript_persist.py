"""U5 — the relay persists its turns into the native message store.

The wire contract is untouched (delta frames + [DONE]); what changes is durability: the newest
user turn lands BEFORE the run, the assistant's responses land BEFORE the terminal [DONE]
(write-before-DONE), and an unresolved conversation persists nothing. The billing/model
overrides mirror `test_chat_stream.py` (package conftest).
"""

from __future__ import annotations

import base64
import uuid

import sqlalchemy as sa
from pydantic_ai.models.test import TestModel

from src.config import settings
from src.db.models.message import Message, MessageEntryKind
from src.services.auth.session_jwt import mint_session_jwt
from src.services.messages import store as store_module
from tests.factories import ConversationFactory, ProjectFactory, UserFactory

_TTL = settings.auth.access_ttl_seconds


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


async def test_turn_persists_exactly_the_new_messages(client, db_session, set_chat_model) -> None:
    """A 3-message history payload persists ONLY the newest user prompt + the response —
    never a quadratic full-history rewrite (plan U5 scenario 1)."""
    headers, _, conversation = await _auth_with_conversation(db_session)
    set_chat_model(TestModel(custom_output_text="the answer"))

    resp = await client.post(
        "/v1/claude",
        headers=headers,
        json={
            "messages": [
                {"role": "user", "content": "old question"},
                {"role": "assistant", "content": "old answer"},
                {"role": "user", "content": "the newest question"},
            ],
            "conversationId": str(conversation.id),
        },
    )
    assert resp.status_code == 200
    assert "data: [DONE]" in resp.text

    rows = await _rows(db_session, conversation.id)
    assert [row.entry_kind for row in rows] == [MessageEntryKind.TURN, MessageEntryKind.TURN]
    assert [row.seq for row in rows] == [0, 1]
    assert [row.mode for row in rows] == [conversation.mode, conversation.mode]

    user_batch = rows[0].payload
    assert len(user_batch) == 1
    [prompt_part] = user_batch[0]["parts"]
    assert prompt_part["content"] == "the newest question"
    # The pre-written request is CLEAN: the per-run composed instructions never enter a row.
    assert user_batch[0].get("instructions") is None
    # The older history the browser re-sent is nowhere in the stored payloads.
    flattened = str(rows[0].payload) + str(rows[1].payload)
    assert "old question" not in flattened
    assert "old answer" not in flattened

    response_batch = rows[1].payload
    assert response_batch[0]["kind"] == "response"
    assert any(
        part.get("content") == "the answer"
        for part in response_batch[0]["parts"]
        if isinstance(part, dict)
    )


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
        json={
            "messages": [{"role": "user", "content": "hello"}],
            "conversationId": str(conversation.id),
        },
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
        json={
            "messages": [{"role": "user", "content": "hello"}],
            "conversationId": str(conversation.id),
        },
    )
    assert resp.status_code == 409
    assert "recorded" in resp.json()["error"]["message"]


async def test_unresolved_conversation_streams_but_persists_nothing(
    client, db_session, set_chat_model
) -> None:
    """The SPA-mints-id first-turn arm (retired at U7): the turn streams, nothing persists."""
    user = await UserFactory.create(db_session)
    headers = _cookie(mint_session_jwt(user.id, user.token_version, _TTL))
    set_chat_model(TestModel(custom_output_text="hi"))
    ghost_conversation = uuid.uuid4()

    resp = await client.post(
        "/v1/claude",
        headers=headers,
        json={
            "messages": [{"role": "user", "content": "hello"}],
            "conversationId": str(ghost_conversation),
        },
    )
    assert resp.status_code == 200
    assert "data: [DONE]" in resp.text
    assert await _rows(db_session, ghost_conversation) == []


async def test_binary_prompt_persists_placeholder_not_bytes(
    client, db_session, set_chat_model
) -> None:
    """A base64 image block streams to the model as today, but the DURABLE row carries a
    factual placeholder — no base64, no unattributed-binary crash (interim until U7's
    reference-passing attachments)."""
    headers, _, conversation = await _auth_with_conversation(db_session)
    set_chat_model(TestModel(custom_output_text="nice image"))
    png = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8).decode()

    resp = await client.post(
        "/v1/claude",
        headers=headers,
        json={
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "what is this?"},
                        {
                            "type": "image",
                            "source": {"type": "base64", "media_type": "image/png", "data": png},
                        },
                    ],
                }
            ],
            "conversationId": str(conversation.id),
        },
    )
    assert resp.status_code == 200
    assert "data: [DONE]" in resp.text

    rows = await _rows(db_session, conversation.id)
    assert len(rows) == 2
    stored = str(rows[0].payload)
    assert png not in stored  # bytes never land in a row
    assert "image/png" in stored  # the placeholder names what was attached
    assert "not retained" in stored
    [prompt_part] = rows[0].payload[0]["parts"]
    assert isinstance(prompt_part["content"], list)
    assert prompt_part["content"][0] == "what is this?"
