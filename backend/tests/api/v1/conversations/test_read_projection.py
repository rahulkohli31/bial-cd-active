"""GET /v1/conversations/{id} returns the display projection + the `activeTurn` seam.

The projection derivation itself is proven in `tests/services/messages/test_projection.py`;
this file proves the READ: one request rebuilds the chat (header + items), the `activeTurn`
seam is present-and-null, and the read is owner-scoped. The populated-while-running `activeTurn`
test lands with the turn engine (no registry exists yet to populate it).
"""

from __future__ import annotations

import uuid

from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart
from pydantic_ai.usage import RequestUsage

from src.config import settings
from src.db.models.conversation import ChatKind
from src.db.models.message import MessageEntryKind
from src.services.auth.session_jwt import mint_session_jwt
from src.services.messages.store import append_batch
from tests.factories import ConversationFactory, ProjectFactory, UserFactory
from tests.fakes import BuildSessionStatus, write_build_outcome, write_legacy_build_started

_TTL = settings.auth.access_ttl_seconds
PREVIEW = "https://sbx-abc.westeurope.azurecontainerapps.io/"


def _cookie(jwt: str) -> dict[str, str]:
    return {"Cookie": f"session={jwt}"}


async def _auth(db_session):
    user = await UserFactory.create(db_session)
    return _cookie(mint_session_jwt(user.id, user.token_version, _TTL)), user


async def _seeded_conversation(db_session, user):
    project = await ProjectFactory.create(db_session, user.id)
    conversation = await ConversationFactory.create(db_session, user.id, project_id=project.id)
    await append_batch(
        db_session,
        user_id=user.id,
        conversation_id=conversation.id,
        messages=[ModelRequest(parts=[UserPromptPart(content="what does my app do?")])],
        entry_kind=MessageEntryKind.TURN,
        kind=ChatKind.PLAN,
    )
    await append_batch(
        db_session,
        user_id=user.id,
        conversation_id=conversation.id,
        messages=[ModelResponse(parts=[TextPart(content="It tracks visitors.")])],
        entry_kind=MessageEntryKind.TURN,
        kind=ChatKind.PLAN,
    )
    return conversation


async def test_get_returns_header_projection_and_null_active_turn(client, db_session) -> None:
    headers, user = await _auth(db_session)
    conversation = await _seeded_conversation(db_session, user)
    await write_build_outcome(
        db_session,
        user_id=user.id,
        conversation_id=conversation.id,
        session_id=uuid.uuid4(),
        status=BuildSessionStatus.ENDED,
        preview_url=PREVIEW,
        snapshot_committed=True,
        reason="completed",
    )

    resp = await client.get(f"/v1/conversations/{conversation.id}", headers=headers)
    assert resp.status_code == 200
    body = resp.json()

    assert body["conversation"]["_id"] == str(conversation.id)
    # What the chat IS, chosen at creation and never changed. There is no second
    # field beside it: `mode` came off the header with the concept.
    assert body["conversation"]["kind"] == "build"
    assert "mode" not in body["conversation"]
    assert body["activeTurn"] is None  # the seam: present, and null until the engine lands

    projection = body["projection"]
    assert [item["type"] for item in projection] == ["user_text", "assistant_text", "banner"]
    assert projection[0]["text"] == "what does my app do?"
    assert projection[1]["text"] == "It tracks visitors."
    assert projection[2]["banner"] == "completed"
    assert projection[2]["previewUrl"] == PREVIEW  # camelCase on the wire
    # `seq` still identifies the row. The per-item `kind` stamp is GONE from the wire, not
    # renamed: nothing ever rendered it, and kind classification concerns CHATS being listed —
    # which the header already carries — not individual items.
    assert all("seq" in item for item in projection)
    assert all("mode" not in item and "kind" not in item for item in projection)


async def test_reloading_a_thread_with_an_unfinished_legacy_build(client, db_session) -> None:
    """A thread holding a `build_started` row no outcome ever closed still reads back, as a
    display-only marker: no session id, and no running turn."""
    headers, user = await _auth(db_session)
    conversation = await _seeded_conversation(db_session, user)
    await write_legacy_build_started(
        db_session,
        user_id=user.id,
        conversation_id=conversation.id,
        session_id=uuid.uuid4(),
        started_seq=-1,
    )

    resp = await client.get(f"/v1/conversations/{conversation.id}", headers=headers)
    assert resp.status_code == 200
    body = resp.json()

    assert body["activeTurn"] is None
    anchor = body["projection"][-1]
    assert anchor == {"type": "build_in_progress", "seq": anchor["seq"]}


async def test_empty_conversation_projects_an_empty_list(client, db_session) -> None:
    headers, user = await _auth(db_session)
    conversation = await ConversationFactory.create(db_session, user.id)

    resp = await client.get(f"/v1/conversations/{conversation.id}", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["projection"] == []


async def test_the_read_carries_how_full_the_chat_is(client, db_session) -> None:
    """★ THE COLD READ'S HALF OF THE METER. The browser's "this chat is getting long" line is
    fed by this field, so a reopened chat that is already past the threshold says so on first
    paint instead of staying silent until the citizen has sent one more message into it.

    It is the RAW prompt count the provider reported — cache-inclusive, never a cost-weighted
    spend — and it is the same number `enforce_context_limit` refuses on. The transcript above it
    and this figure come from ONE `load_rows`, so they cannot describe different conversations.

    Mutation: drop the field from the route and the first assertion goes red; read the last
    response rather than the largest and the third does, because the platform spoke last."""
    headers, user = await _auth(db_session)
    project = await ProjectFactory.create(db_session, user.id)
    conversation = await ConversationFactory.create(db_session, user.id, project_id=project.id)

    empty = await client.get(f"/v1/conversations/{conversation.id}", headers=headers)
    # UNMEASURED IS NOT EMPTY: a chat nobody has served answers null, and the browser stays
    # silent on it. A `0` here would be the server claiming the chat is empty.
    assert empty.json()["contextTokens"] is None

    await append_batch(
        db_session,
        user_id=user.id,
        conversation_id=conversation.id,
        messages=[
            ModelRequest(parts=[UserPromptPart(content="carry on")]),
            ModelResponse(
                parts=[TextPart(content="here you go")],
                usage=RequestUsage(input_tokens=190_000, cache_read_tokens=185_000),
            ),
        ],
        entry_kind=MessageEntryKind.TURN,
        kind=ChatKind.PLAN,
    )
    # …then the PLATFORM has the last word, carrying no measurement of its own.
    await append_batch(
        db_session,
        user_id=user.id,
        conversation_id=conversation.id,
        messages=[ModelResponse(parts=[TextPart(content="Starting your build.")])],
        entry_kind=MessageEntryKind.TURN,
        kind=ChatKind.PLAN,
    )

    body = (await client.get(f"/v1/conversations/{conversation.id}", headers=headers)).json()

    assert body["contextTokens"] == 190_000
    # LIVENESS: the transcript really came back too, so the figure above is part of a working
    # read rather than the only thing this route still answers.
    assert [item["type"] for item in body["projection"]] == [
        "user_text",
        "assistant_text",
        "assistant_text",
    ]


async def test_cross_user_read_is_a_404(client, db_session) -> None:
    headers, _ = await _auth(db_session)
    other = await UserFactory.create(db_session)
    theirs = await ConversationFactory.create(db_session, other.id)

    resp = await client.get(f"/v1/conversations/{theirs.id}", headers=headers)
    assert resp.status_code == 404
    assert "projection" not in resp.text
