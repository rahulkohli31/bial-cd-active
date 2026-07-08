"""GET/PATCH /v1/conversations — user-scoped list, get-with-messages, patch (U8).
Byte-stable with the Express `/api/conversations` contract (`_id`, `{error:{message}}`).
"""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import select

from src.config import settings
from src.db.models.conversation import Conversation, ConversationKind
from src.db.models.message import MessageRole
from src.services.auth.session_jwt import mint_session_jwt
from tests.factories import ConversationFactory, MessageFactory, UserFactory

_TTL = settings.auth.access_ttl_seconds
_UTC = datetime.UTC


def _cookie(jwt: str) -> dict[str, str]:
    return {"Cookie": f"session={jwt}"}


async def _auth(db_session):
    user = await UserFactory.create(db_session)
    return _cookie(mint_session_jwt(user.id, user.token_version, _TTL)), user


# --- list ---------------------------------------------------------------------


async def test_list_scoped_to_caller(client, db_session) -> None:
    headers, user = await _auth(db_session)
    other = await UserFactory.create(db_session)
    mine = await ConversationFactory.create(db_session, user.id, title="mine")
    await ConversationFactory.create(db_session, other.id, title="theirs")

    resp = await client.get("/v1/conversations", headers=headers)
    assert resp.status_code == 200
    convs = resp.json()["conversations"]
    assert [c["_id"] for c in convs] == [str(mine.id)]
    assert convs[0]["kind"] == "planning"
    assert convs[0]["title"] == "mine"
    assert convs[0]["createdAt"].endswith("Z")


async def test_list_kind_filter(client, db_session) -> None:
    headers, user = await _auth(db_session)
    builder = await ConversationFactory.create(db_session, user.id, kind=ConversationKind.BUILDER)
    await ConversationFactory.create(db_session, user.id, kind=ConversationKind.PLANNING)

    resp = await client.get("/v1/conversations?kind=builder", headers=headers)
    assert resp.status_code == 200
    convs = resp.json()["conversations"]
    assert [c["_id"] for c in convs] == [str(builder.id)]


async def test_list_unknown_kind_400(client, db_session) -> None:
    headers, _ = await _auth(db_session)
    resp = await client.get("/v1/conversations?kind=bogus", headers=headers)
    assert resp.status_code == 400
    assert resp.json() == {"error": {"message": "Unknown kind."}}


async def test_list_newest_first(client, db_session) -> None:
    headers, user = await _auth(db_session)
    older = await ConversationFactory.create(
        db_session, user.id, updated_at=datetime.datetime(2026, 7, 5, 10, 0, tzinfo=_UTC)
    )
    newer = await ConversationFactory.create(
        db_session, user.id, updated_at=datetime.datetime(2026, 7, 6, 10, 0, tzinfo=_UTC)
    )
    resp = await client.get("/v1/conversations", headers=headers)
    assert [c["_id"] for c in resp.json()["conversations"]] == [str(newer.id), str(older.id)]


# --- get with messages --------------------------------------------------------


async def test_get_returns_ordered_messages(client, db_session) -> None:
    headers, user = await _auth(db_session)
    conv = await ConversationFactory.create(db_session, user.id)
    # Insert out of seq order — the response must sort by seq ascending.
    await MessageFactory.create(db_session, user.id, conv.id, seq=2, role=MessageRole.ASSISTANT)
    await MessageFactory.create(db_session, user.id, conv.id, seq=0, role=MessageRole.USER)
    await MessageFactory.create(db_session, user.id, conv.id, seq=1, role=MessageRole.ASSISTANT)

    resp = await client.get(f"/v1/conversations/{conv.id}", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["conversation"]["_id"] == str(conv.id)
    assert [m["seq"] for m in body["messages"]] == [0, 1, 2]
    assert body["messages"][0]["parts"] == [{"type": "text", "text": "hi"}]
    assert body["messages"][0]["role"] == "user"


async def test_get_cross_user_404(client, db_session) -> None:
    headers, _ = await _auth(db_session)
    other = await UserFactory.create(db_session)
    theirs = await ConversationFactory.create(db_session, other.id)
    resp = await client.get(f"/v1/conversations/{theirs.id}", headers=headers)
    assert resp.status_code == 404
    assert resp.json() == {"error": {"message": "Conversation not found."}}


async def test_get_invalid_id_400(client, db_session) -> None:
    headers, _ = await _auth(db_session)
    resp = await client.get("/v1/conversations/bad!id", headers=headers)
    assert resp.status_code == 400
    assert resp.json() == {"error": {"message": "Invalid conversation id."}}


async def test_get_missing_404(client, db_session) -> None:
    headers, _ = await _auth(db_session)
    resp = await client.get(f"/v1/conversations/{uuid.uuid4()}", headers=headers)
    assert resp.status_code == 404


# --- patch --------------------------------------------------------------------


async def test_patch_title_and_context(client, db_session) -> None:
    headers, user = await _auth(db_session)
    conv = await ConversationFactory.create(db_session, user.id)
    resp = await client.patch(
        f"/v1/conversations/{conv.id}",
        headers=headers,
        json={"title": "Renamed", "context": {"theme": "dark"}},
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    stored = await db_session.scalar(select(Conversation).where(Conversation.id == conv.id))
    assert stored.title == "Renamed"
    assert stored.context == {"theme": "dark"}


async def test_patch_code_snapshot_wraps_in_current(client, db_session) -> None:
    headers, user = await _auth(db_session)
    conv = await ConversationFactory.create(db_session, user.id, kind=ConversationKind.BUILDER)
    snapshot = {"source": "export default () => <div/>", "entry": "App"}
    resp = await client.patch(
        f"/v1/conversations/{conv.id}", headers=headers, json={"code": snapshot}
    )
    assert resp.status_code == 200
    stored = await db_session.scalar(select(Conversation).where(Conversation.id == conv.id))
    assert stored.code == {"current": snapshot}


async def test_patch_code_validation(client, db_session) -> None:
    headers, user = await _auth(db_session)
    conv = await ConversationFactory.create(db_session, user.id, kind=ConversationKind.BUILDER)
    cases = [
        ("notanobject", "code must be an object"),
        ({}, "code.source is required"),
        ({"source": "x"}, "code.entry is required"),
        ({"source": "x", "entry": ""}, "code.entry is required"),
    ]
    for bad_code, message in cases:
        resp = await client.patch(
            f"/v1/conversations/{conv.id}", headers=headers, json={"code": bad_code}
        )
        assert resp.status_code == 400, bad_code
        assert resp.json() == {"error": {"message": message}}, bad_code


async def test_patch_cross_user_404(client, db_session) -> None:
    headers, _ = await _auth(db_session)
    other = await UserFactory.create(db_session)
    theirs = await ConversationFactory.create(db_session, other.id, title="theirs")
    resp = await client.patch(
        f"/v1/conversations/{theirs.id}", headers=headers, json={"title": "hacked"}
    )
    assert resp.status_code == 404
    # The victim's row is untouched.
    stored = await db_session.scalar(select(Conversation).where(Conversation.id == theirs.id))
    assert stored.title == "theirs"


async def test_patch_code_validated_before_ownership(client, db_session) -> None:
    # A malformed code snapshot is a 400 even against another user's id (Express order).
    headers, _ = await _auth(db_session)
    other = await UserFactory.create(db_session)
    theirs = await ConversationFactory.create(db_session, other.id)
    resp = await client.patch(f"/v1/conversations/{theirs.id}", headers=headers, json={"code": {}})
    assert resp.status_code == 400
    assert resp.json() == {"error": {"message": "code.source is required"}}


async def test_requires_auth(client) -> None:
    assert (await client.get("/v1/conversations")).status_code == 401
