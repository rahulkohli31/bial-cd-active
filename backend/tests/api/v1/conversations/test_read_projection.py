"""U6 — GET /v1/conversations/{id} returns the display projection + the `activeTurn` seam.

The projection derivation itself is proven in `tests/services/messages/test_projection.py`;
this file proves the READ: one request rebuilds the chat (header + items), the U10 seam is
present-and-null, and the read is owner-scoped. The populated-while-running `activeTurn`
test lands with U10's turn engine (no registry exists yet to populate it).
"""

from __future__ import annotations

import uuid

from pydantic_ai.messages import ModelRequest, ModelResponse, TextPart, UserPromptPart

from src.api.v1.build_sessions.schemas import BuildSessionStatus
from src.config import settings
from src.db.models.conversation import ConversationMode
from src.db.models.message import MessageEntryKind
from src.services.auth.session_jwt import mint_session_jwt
from src.services.build_sessions.outcome import write_build_outcome
from src.services.messages.store import append_batch, append_mode_switch_marker
from tests.factories import ConversationFactory, ProjectFactory, UserFactory

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
        mode=ConversationMode.ASK,
    )
    await append_batch(
        db_session,
        user_id=user.id,
        conversation_id=conversation.id,
        messages=[ModelResponse(parts=[TextPart(content="It tracks visitors.")])],
        entry_kind=MessageEntryKind.TURN,
        mode=ConversationMode.ASK,
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
    assert body["conversation"]["mode"] == "plan"  # the server-owned sticky mode (U4 default)
    assert body["activeTurn"] is None  # the U10 seam: present, and null until the engine lands

    projection = body["projection"]
    assert [item["type"] for item in projection] == ["user_text", "assistant_text", "banner"]
    assert projection[0]["text"] == "what does my app do?"
    assert projection[1]["text"] == "It tracks visitors."
    assert projection[2]["banner"] == "completed"
    assert projection[2]["previewUrl"] == PREVIEW  # camelCase on the wire
    assert all("seq" in item and "mode" in item for item in projection)


async def test_hidden_marker_rows_never_reach_the_wire(client, db_session) -> None:
    headers, user = await _auth(db_session)
    conversation = await _seeded_conversation(db_session, user)
    await append_mode_switch_marker(
        db_session,
        user_id=user.id,
        conversation_id=conversation.id,
        old_mode=ConversationMode.ASK,
        new_mode=ConversationMode.WRITE,
    )

    resp = await client.get(f"/v1/conversations/{conversation.id}", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert [item["type"] for item in body["projection"]] == ["user_text", "assistant_text"]
    assert "mode changed" not in resp.text  # the marker prose never leaves the server


async def test_empty_conversation_projects_an_empty_list(client, db_session) -> None:
    headers, user = await _auth(db_session)
    conversation = await ConversationFactory.create(db_session, user.id)

    resp = await client.get(f"/v1/conversations/{conversation.id}", headers=headers)
    assert resp.status_code == 200
    assert resp.json()["projection"] == []


async def test_cross_user_read_is_a_404(client, db_session) -> None:
    headers, _ = await _auth(db_session)
    other = await UserFactory.create(db_session)
    theirs = await ConversationFactory.create(db_session, other.id)

    resp = await client.get(f"/v1/conversations/{theirs.id}", headers=headers)
    assert resp.status_code == 404
    assert "projection" not in resp.text
