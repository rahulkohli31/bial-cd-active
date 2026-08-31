"""U7 — server-authoritative history (R9): the browser sends ONLY the new message.

The proofs that matter: the wire request to the MODEL carries the full prior transcript the
server assembled from the DB (a capturing FunctionModel reads it); stored attachment
references rehydrate to real bytes at send; one reply at a time per conversation (typed 409,
guard clears on completion); regenerate replays without duplicating the user turn; ephemeral
summarize turns read history but persist nothing.
"""

from __future__ import annotations

import asyncio

import sqlalchemy as sa
from pydantic_ai.messages import (
    BinaryContent,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel

from src.api.v1.claude.prompts import SUMMARIZE_BRIEF_PROMPT
from src.config import settings
from src.db.models.message import Message, MessageEntryKind
from src.services.auth.session_jwt import mint_session_jwt
from src.services.messages.store import append_batch
from tests.factories import ConversationFactory, ProjectFactory, UserFactory

_TTL = settings.auth.access_ttl_seconds
_PNG = bytes([0x89, 0x50, 0x4E, 0x47, 0x0D, 0x0A, 0x1A, 0x0A]) + b"fake-png-body"


def _cookie(jwt: str) -> dict[str, str]:
    return {"Cookie": f"session={jwt}"}


async def _thread(db_session):
    user = await UserFactory.create(db_session)
    project = await ProjectFactory.create(db_session, user.id)
    conversation = await ConversationFactory.create(db_session, user.id, project_id=project.id)
    return _cookie(mint_session_jwt(user.id, user.token_version, _TTL)), user, conversation


class _Capture:
    """What the model actually received: the wire messages + the composed instructions."""

    def __init__(self) -> None:
        self.messages: list[ModelMessage] = []
        self.instructions = ""


def _capturing_model():
    """A FunctionModel that records the WIRE messages of each run (what the server actually
    assembled for the provider) and streams one word."""
    captured = _Capture()

    async def _stream(messages: list[ModelMessage], info: AgentInfo):
        captured.messages = list(messages)
        captured.instructions = info.instructions or ""
        yield "reply"

    return FunctionModel(stream_function=_stream), captured


async def _seed_turns(db_session, user, conversation, exchanges: list[tuple[str, str]]) -> None:
    """Persist prior turns through the REAL store (the same rows the relay writes)."""
    for question, answer in exchanges:
        await append_batch(
            db_session,
            user_id=user.id,
            conversation_id=conversation.id,
            messages=[ModelRequest(parts=[UserPromptPart(content=question)])],
            entry_kind=MessageEntryKind.TURN,
            kind=conversation.kind,
        )
        await append_batch(
            db_session,
            user_id=user.id,
            conversation_id=conversation.id,
            messages=[ModelResponse(parts=[TextPart(content=answer)])],
            entry_kind=MessageEntryKind.TURN,
            kind=conversation.kind,
        )


async def _row_count(db_session, conversation_id) -> int:
    return (
        await db_session.scalar(
            sa.select(sa.func.count())
            .select_from(Message)
            .where(Message.conversation_id == conversation_id)
        )
    ) or 0


# --- Plan B criterion 4: the server assembles history from the DB -------------


async def test_turn_four_carries_the_full_prior_transcript_from_the_db(
    client, db_session, set_chat_model
) -> None:
    headers, user, conversation = await _thread(db_session)
    await _seed_turns(
        db_session,
        user,
        conversation,
        [("first question", "first answer"), ("second question", "second answer")],
    )
    model, captured = _capturing_model()
    set_chat_model(model)

    resp = await client.post(
        "/v1/claude",
        headers=headers,
        json={"conversationId": str(conversation.id), "message": {"text": "third question"}},
    )
    assert resp.status_code == 200
    assert "data: [DONE]" in resp.text

    # The browser sent ONLY "third question"; everything else came from the DB.
    flat = str(captured.messages)
    for expected in ("first question", "first answer", "second question", "second answer"):
        assert expected in flat
    assert "third question" in flat


async def test_attachment_reference_rehydrates_to_bytes_at_send(
    client, db_session, set_chat_model, fake_storage
) -> None:
    """A PRIOR turn stored an attachment as a reference marker; the next send must put the
    real bytes back on the wire (Foundry has no Files API)."""
    from src.db.models.attachment import Attachment
    from src.services.storage.keys import owner_prefix

    headers, user, conversation = await _thread(db_session)
    attachment_id = "att-history-1"
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
    await append_batch(
        db_session,
        user_id=user.id,
        conversation_id=conversation.id,
        messages=[
            ModelRequest(
                parts=[
                    UserPromptPart(
                        content=[
                            BinaryContent(
                                data=_PNG, media_type="image/png", identifier=attachment_id
                            ),
                            "what is in this image?",
                        ]
                    )
                ]
            )
        ],
        entry_kind=MessageEntryKind.TURN,
        kind=conversation.kind,
    )

    model, captured = _capturing_model()
    set_chat_model(model)
    resp = await client.post(
        "/v1/claude",
        headers=headers,
        json={"conversationId": str(conversation.id), "message": {"text": "and now?"}},
    )
    assert resp.status_code == 200

    messages = captured.messages
    binaries = [
        part_item
        for message in messages
        for part in getattr(message, "parts", [])
        if isinstance(part, UserPromptPart) and isinstance(part.content, list)
        for part_item in part.content
        if isinstance(part_item, BinaryContent)
    ]
    assert binaries, "the stored reference must reach the wire as BinaryContent"
    assert binaries[0].data == _PNG  # the actual bytes, not a marker


# --- one reply at a time ------------------------------------------------------


async def test_second_post_while_a_turn_is_in_flight_is_409_then_clears(
    client, db_session, set_chat_model
) -> None:
    """The per-conversation turn guard, both halves.

    (Driven via the guard registry rather than two truly concurrent HTTP requests: the test
    session fixture is ONE rolled-back session, and two interleaved turns on it deadlock the
    fixture, not the product — in production each drain has its own session. The claim is a
    synchronous check+add on one event loop, so 'while in flight' IS 'while the id is in the
    registry'.)"""
    from src.services.turns.guard import _mid_reply

    headers, _, conversation = await _thread(db_session)
    set_chat_model(TestModel(custom_output_text="fine"))

    # A claimed conversation refuses a second turn, typed — and persists NOTHING for it.
    _mid_reply.add(conversation.id)
    try:
        second = await client.post(
            "/v1/claude",
            headers=headers,
            json={"conversationId": str(conversation.id), "message": {"text": "two"}},
        )
        assert second.status_code == 409
        assert "already being generated" in second.json()["error"]["message"]
        assert await _row_count(db_session, conversation.id) == 0
    finally:
        _mid_reply.discard(conversation.id)

    # A full turn claims and RELEASES: the drain's finally clears the guard on completion,
    # so the next turn is admitted.
    first = await client.post(
        "/v1/claude",
        headers=headers,
        json={"conversationId": str(conversation.id), "message": {"text": "one"}},
    )
    assert first.status_code == 200
    _ = first.text  # drain to completion
    for _ in range(200):
        if conversation.id not in _mid_reply:
            break
        await asyncio.sleep(0.01)
    assert conversation.id not in _mid_reply  # the guard cleared with the drain

    third = await client.post(
        "/v1/claude",
        headers=headers,
        json={"conversationId": str(conversation.id), "message": {"text": "three"}},
    )
    assert third.status_code == 200


# --- regenerate ---------------------------------------------------------------


async def test_regenerate_replays_without_duplicating_the_user_turn(
    client, db_session, set_chat_model
) -> None:
    headers, user, conversation = await _thread(db_session)
    await _seed_turns(db_session, user, conversation, [("the question", "a poor answer")])
    before = await _row_count(db_session, conversation.id)

    model, captured = _capturing_model()
    set_chat_model(model)
    resp = await client.post(
        "/v1/claude",
        headers=headers,
        json={
            "conversationId": str(conversation.id),
            "message": {"text": ""},
            "regenerate": True,
        },
    )
    assert resp.status_code == 200
    assert "data: [DONE]" in resp.text

    rows = list(
        (
            await db_session.execute(
                sa.select(Message)
                .where(Message.conversation_id == conversation.id)
                .order_by(Message.seq)
            )
        ).scalars()
    )
    # Exactly ONE new row: the regenerated response. No second copy of the user turn.
    assert len(rows) == before + 1
    assert rows[-1].payload[0]["kind"] == "response"
    assert str(captured.messages).count("the question") == 1


async def test_regenerate_on_an_empty_conversation_is_400(
    client, db_session, set_chat_model
) -> None:
    headers, _, conversation = await _thread(db_session)
    set_chat_model(TestModel(custom_output_text="never"))
    resp = await client.post(
        "/v1/claude",
        headers=headers,
        json={
            "conversationId": str(conversation.id),
            "message": {"text": ""},
            "regenerate": True,
        },
    )
    assert resp.status_code == 400
    assert "nothing to regenerate" in resp.json()["error"]["message"].lower()


# --- ephemeral summarize ------------------------------------------------------


async def test_ephemeral_summarize_reads_history_and_persists_nothing(
    client, db_session, set_chat_model
) -> None:
    headers, user, conversation = await _thread(db_session)
    await _seed_turns(
        db_session, user, conversation, [("plan a visitor app", "let us define the fields")]
    )
    before = await _row_count(db_session, conversation.id)

    model, captured = _capturing_model()
    set_chat_model(model)
    resp = await client.post(
        "/v1/claude",
        headers=headers,
        json={
            "conversationId": str(conversation.id),
            "message": {"text": "Extract the app requirements into a builder prompt."},
            "ephemeral": "summarize_brief",
        },
    )
    assert resp.status_code == 200
    assert "data: [DONE]" in resp.text

    instructions = captured.instructions
    assert isinstance(instructions, str)
    assert instructions.startswith(SUMMARIZE_BRIEF_PROMPT)  # the summarize prompt won
    # The transcript did NOT ride the wire from the browser — the server supplied it.
    assert "plan a visitor app" in str(captured.messages)
    # And NOTHING persisted: the one-off turn leaves no rows behind.
    assert await _row_count(db_session, conversation.id) == before
