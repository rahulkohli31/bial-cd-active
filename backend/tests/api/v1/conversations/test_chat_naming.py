"""A chat is named after its first accepted message with words in it, by the rule the portal's
heading uses, in the same commit as that message — and no later send renames it."""

from __future__ import annotations

import asyncio
import contextlib
import datetime
import sys
import unicodedata
import uuid

import pytest
import sqlalchemy as sa
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models.function import AgentInfo, FunctionModel

from src.api.v1.conversations.router import _clean_title
from src.api.v1.conversations.turns import FILE_NOTE_KIND, derive_title
from src.db.models.conversation import ChatKind, Conversation
from src.db.models.message import Message, MessageEntryKind
from src.services.messages.store import SeqContentionError, append_batch
from tests.api.v1.conversations.conftest import _headers
from tests.factories import ConversationFactory, UserFactory

pytestmark = pytest.mark.usefixtures("_fresh_engine", "_override_billing")

_LONG_AGO = datetime.datetime(2020, 1, 1, tzinfo=datetime.UTC)

# The portal's `deriveTitle` tests carry the same table: the heading and the saved name agree.
NAMES: list[tuple[str, str | None]] = [
    ("  hello  ", "hello"),
    ("line one\nline two", "line one line two"),
    ("tab\there\r\n\r\nthen a line a para", "tab here then a line a para"),
    ("a\x00b\x07c\x7fd\x85e", "a b c d e"),
    (" 　wide spaces ", "wide spaces"),
    ("y" * 40, "y" * 40),
    ("y" * 41, "y" * 40 + "…"),
    ("😀" * 45, "😀" * 40 + "…"),
    ("a" * 39 + "😀😀", "a" * 39 + "😀…"),
    ("﻿kept mark ", "﻿kept mark"),
    ("", None),
    (" \n\t  ", None),
]


def _streaming_text(*chunks: str) -> FunctionModel:
    async def _stream(_messages: list[ModelMessage], _info: AgentInfo):
        for chunk in chunks:
            yield chunk

    return FunctionModel(stream_function=_stream)


async def _send(
    client,
    user,
    conv,
    text: str,
    attachment_texts: list[str] | None = None,
    attachment_ids: list[str] | None = None,
):
    return await client.post(
        f"/v1/conversations/{conv.id}/turns",
        headers=_headers(user),
        json={
            "message": {
                "text": text,
                "attachmentTexts": attachment_texts or [],
                "attachmentIds": attachment_ids or [],
            }
        },
    )


async def _settle(engine, conversation_id) -> None:
    state = engine.peek(conversation_id)
    assert state is not None and state.task is not None
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(state.task, timeout=10)


async def _accepted(client, engine, user, conv, text: str) -> None:
    resp = await _send(client, user, conv, text)
    assert resp.status_code == 202, resp.text
    await _settle(engine, conv.id)


async def _title_of(db_session, conversation_id: uuid.UUID) -> str | None:
    """A column read, so the answer is the row's and never the identity map's."""
    title: str | None = await db_session.scalar(
        sa.select(Conversation.title).where(Conversation.id == conversation_id)
    )
    return title


# --- the rule -----------------------------------------------------------------------------


@pytest.mark.parametrize(("text", "expected"), NAMES)
def test_a_name_is_the_first_forty_code_points_on_one_line(
    text: str, expected: str | None
) -> None:
    assert derive_title(text) == expected


def test_every_derived_name_is_one_a_rename_would_accept() -> None:
    """The name is written without passing through the rename route's checks, so it has to meet
    them already: not empty, inside the length limit, one line, nothing left to trim."""
    separators = [
        chr(point)
        for point in range(sys.maxunicode + 1)
        if unicodedata.category(chr(point)) in {"Cc", "Zs", "Zl", "Zp"}
    ]
    samples = [
        *(text for text, _ in NAMES),
        *(f"{sep}start{sep}end{sep}" for sep in separators),
        "x".join(separators),
        "a" * 39 + "\n" + "b" * 10,
        "".join(separators) + "y" * 500,
    ]
    for text in samples:
        name = derive_title(text)
        if name is not None:
            assert _clean_title(name) == name, repr(text)


# --- the send names the chat ----------------------------------------------------------------


@pytest.mark.parametrize("kind", [ChatKind.PLAN, ChatKind.GENERIC])
async def test_the_first_accepted_message_names_the_chat(
    client, db_session, set_chat_model, _fresh_engine, kind: ChatKind
) -> None:
    user = await UserFactory.create(db_session)
    conv = await ConversationFactory.create(db_session, user.id, kind=kind)
    set_chat_model(_streaming_text("ok"))

    await _accepted(client, _fresh_engine, user, conv, "Track runway inspections")

    # Mutation-checked: remove the naming UPDATE from `start_conversation_turn` and this is None.
    assert await _title_of(db_session, conv.id) == "Track runway inspections"


async def test_a_second_message_does_not_rename_it(
    client, db_session, set_chat_model, _fresh_engine
) -> None:
    user = await UserFactory.create(db_session)
    conv = await ConversationFactory.create(db_session, user.id, kind=ChatKind.PLAN)
    set_chat_model(_streaming_text("ok"))

    await _accepted(client, _fresh_engine, user, conv, "first words")
    await _accepted(client, _fresh_engine, user, conv, "second words")

    assert await _title_of(db_session, conv.id) == "first words"


async def test_a_name_given_before_the_first_message_is_kept(
    client, db_session, set_chat_model, _fresh_engine
) -> None:
    user = await UserFactory.create(db_session)
    conv = await ConversationFactory.create(db_session, user.id, kind=ChatKind.PLAN)
    set_chat_model(_streaming_text("ok"))
    renamed = await client.patch(
        f"/v1/conversations/{conv.id}", headers=_headers(user), json={"title": "Gate rota"}
    )
    assert renamed.status_code == 200, renamed.text

    await _accepted(client, _fresh_engine, user, conv, "Plan the weekend gate rota")

    assert await _title_of(db_session, conv.id) == "Gate rota"


async def test_a_refused_send_leaves_the_chat_unnamed_and_the_retry_names_it(
    client, db_session, set_chat_model, _fresh_engine, building
) -> None:
    """Every refusal lands above the first write, so the refused words never name the chat and
    the chat does not move in its list. The citizen's retry is then the first message that
    lands, and it names the chat."""
    user = await UserFactory.create(db_session)
    conv = await ConversationFactory.create(
        db_session, user.id, kind=ChatKind.PLAN, updated_at=_LONG_AGO
    )
    set_chat_model(_streaming_text("ok"))

    with building(user.id):
        refused = await _send(client, user, conv, "words that never landed")
    assert refused.status_code == 409, refused.text

    assert await _title_of(db_session, conv.id) is None
    assert (
        await db_session.scalar(
            sa.select(Conversation.updated_at).where(Conversation.id == conv.id)
        )
        == _LONG_AGO
    )

    await _accepted(client, _fresh_engine, user, conv, "words that landed")
    assert await _title_of(db_session, conv.id) == "words that landed"


@pytest.mark.route_rollback
async def test_a_send_refused_at_the_append_leaves_the_chat_unnamed(
    client, db_session, set_chat_model, monkeypatch
) -> None:
    """The one refusal below the naming UPDATE: the append itself giving up. The name has to
    roll back with the message it was written for."""
    user = await UserFactory.create(db_session)
    conv = await ConversationFactory.create(db_session, user.id, kind=ChatKind.PLAN)
    await db_session.commit()
    # A plain local: the rollback below expires every instance attribute.
    conversation_id = conv.id
    set_chat_model(_streaming_text("ok"))

    async def _contended(*_args, **_kwargs):
        raise SeqContentionError("planted")

    monkeypatch.setattr("src.api.v1.conversations.turns.append_batch", _contended)

    resp = await _send(client, user, conv, "words that never landed")
    assert resp.status_code == 409, resp.text

    # Stands in for `get_db`, which rolls the request back in production but not under the
    # suite's shared session. Mutation-checked: commit right after the naming UPDATE and the
    # name survives this rollback.
    await db_session.rollback()
    assert await _title_of(db_session, conversation_id) is None


@pytest.mark.route_rollback
async def test_a_send_refused_after_its_file_note_leaves_the_chat_unnamed_and_the_note_standing(
    client, db_session, set_chat_model, monkeypatch, shared_storage
) -> None:
    """The file note commits on its own, ahead of the naming UPDATE, so a message refused at its
    append must still take the name with it. The note stays, and it is still true."""
    user = await UserFactory.create(db_session)
    conv = await ConversationFactory.create(db_session, user.id, kind=ChatKind.PLAN)
    await db_session.commit()
    conversation_id = conv.id
    uploaded = await client.post(
        "/v1/attachments",
        headers=_headers(user),
        json={
            "conversationId": str(conversation_id),
            "attachmentId": "att_csv",
            "name": "visitors.csv",
            "mediaType": "text/csv",
            "base64": "YSxiCjEsMgo=",
        },
    )
    assert uploaded.status_code == 201, uploaded.text
    set_chat_model(_streaming_text("ok"))

    async def _message_contended(*args, **kwargs):
        if kwargs["entry_kind"] is MessageEntryKind.TURN:
            raise SeqContentionError("planted")
        return await append_batch(*args, **kwargs)

    monkeypatch.setattr("src.api.v1.conversations.turns.append_batch", _message_contended)

    resp = await _send(client, user, conv, "words that never landed", attachment_ids=["att_csv"])
    assert resp.status_code == 409, resp.text

    await db_session.rollback()
    assert await _title_of(db_session, conversation_id) is None
    notes = (
        await db_session.scalars(
            sa.select(Message.payload).where(
                Message.conversation_id == conversation_id,
                Message.meta["kind"].astext == FILE_NOTE_KIND,
            )
        )
    ).all()
    assert len(notes) == 1
    assert "visitors.csv" in notes[0][0]["parts"][0]["content"]


async def test_line_breaks_and_control_characters_never_reach_the_name(
    client, db_session, set_chat_model, _fresh_engine
) -> None:
    user = await UserFactory.create(db_session)
    conv = await ConversationFactory.create(db_session, user.id, kind=ChatKind.PLAN)
    set_chat_model(_streaming_text("ok"))

    await _accepted(client, _fresh_engine, user, conv, "Inspect\nrunway 09\t\tbefore dawn\x07")

    assert await _title_of(db_session, conv.id) == "Inspect runway 09 before dawn"


async def test_a_long_first_message_is_cut_at_forty_with_an_ellipsis(
    client, db_session, set_chat_model, _fresh_engine
) -> None:
    user = await UserFactory.create(db_session)
    conv = await ConversationFactory.create(db_session, user.id, kind=ChatKind.PLAN)
    set_chat_model(_streaming_text("ok"))

    await _accepted(
        client,
        _fresh_engine,
        user,
        conv,
        "Build a tracker for runway inspection rounds and defects",
    )

    assert await _title_of(db_session, conv.id) == "Build a tracker for runway inspection ro…"


async def test_attachments_alone_leave_it_unnamed_until_a_message_has_words(
    client, db_session, set_chat_model, _fresh_engine
) -> None:
    """An attachment's name or contents is not what the citizen called the chat. The first
    message that does carry words names it, which is why the guard is the missing name rather
    than the message count."""
    user = await UserFactory.create(db_session)
    conv = await ConversationFactory.create(db_session, user.id, kind=ChatKind.PLAN)
    set_chat_model(_streaming_text("ok"))

    resp = await _send(
        client,
        user,
        conv,
        "",
        attachment_texts=['<attachment name="notes.txt" type="text/plain">gate 4</attachment>'],
    )
    assert resp.status_code == 202, resp.text
    await _settle(_fresh_engine, conv.id)
    assert await _title_of(db_session, conv.id) is None

    await _accepted(client, _fresh_engine, user, conv, "What do these notes say?")
    assert await _title_of(db_session, conv.id) == "What do these notes say?"


async def test_the_chat_list_shows_the_name(
    client, db_session, set_chat_model, _fresh_engine
) -> None:
    user = await UserFactory.create(db_session)
    conv = await ConversationFactory.create(db_session, user.id, kind=ChatKind.PLAN)
    set_chat_model(_streaming_text("ok"))

    await _accepted(client, _fresh_engine, user, conv, "Baggage belt fault log")

    listed = await client.get(
        "/v1/conversations", headers=_headers(user), params={"projectId": str(conv.project_id)}
    )
    assert listed.status_code == 200, listed.text
    [header] = listed.json()["conversations"]
    assert header["_id"] == str(conv.id)
    assert header["title"] == "Baggage belt fault log"


async def test_naming_moves_the_chat_only_as_far_as_its_message_does(
    client, db_session, set_chat_model, _fresh_engine
) -> None:
    """A name is not activity: the chat's place in the newest-first list is the time of its
    newest message, exactly as it would be without the name."""
    user = await UserFactory.create(db_session)
    conv = await ConversationFactory.create(
        db_session, user.id, kind=ChatKind.PLAN, updated_at=_LONG_AGO
    )
    set_chat_model(_streaming_text("ok"))

    await _accepted(client, _fresh_engine, user, conv, "Shift handover notes")

    newest_message = await db_session.scalar(
        sa.select(sa.func.max(Message.created_at)).where(Message.conversation_id == conv.id)
    )
    updated_at = await db_session.scalar(
        sa.select(Conversation.updated_at).where(Conversation.id == conv.id)
    )
    assert updated_at == newest_message
    assert updated_at > _LONG_AGO
