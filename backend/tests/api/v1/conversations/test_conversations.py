"""GET/PATCH /v1/conversations — user-scoped list, header get, patch.
Keeps the Express-era wire shape (`_id`, `{error:{message}}`); the message read/append
surface died with U4's destructive reset (the projection read arrives in U6)."""

from __future__ import annotations

import datetime
import uuid

from sqlalchemy import select

from src.config import settings
from src.db.models.conversation import ChatKind, Conversation
from src.services.auth.session_jwt import mint_session_jwt
from tests.factories import ConversationFactory, UserFactory

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
    assert convs[0]["kind"] == "build"  # the factory's default, and every migrated chat's
    assert convs[0]["title"] == "mine"
    assert convs[0]["createdAt"].endswith("Z")


async def test_list_kind_filter(client, db_session) -> None:
    headers, user = await _auth(db_session)
    builder = await ConversationFactory.create(db_session, user.id, kind=ChatKind.BUILD)
    await ConversationFactory.create(db_session, user.id, kind=ChatKind.PLAN)

    resp = await client.get("/v1/conversations?kind=build", headers=headers)
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


async def test_get_returns_header_with_the_chats_kind(client, db_session) -> None:
    headers, user = await _auth(db_session)
    conv = await ConversationFactory.create(db_session, user.id, kind=ChatKind.PLAN)

    resp = await client.get(f"/v1/conversations/{conv.id}", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["conversation"]["_id"] == str(conv.id)
    # What the chat IS (R16's server half), and nothing beside it.
    assert body["conversation"]["kind"] == "plan"
    assert "mode" not in body["conversation"]
    # The legacy message read died with the reset — the projection arrives in U6.
    assert "messages" not in body


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


async def test_patch_code_is_retired_400(client, db_session) -> None:
    """The `code` column died in 0024 — a client still sending a snapshot gets a 400 naming
    the retirement (never a silent ignore that looks like a saved snapshot)."""
    headers, user = await _auth(db_session)
    conv = await ConversationFactory.create(db_session, user.id, kind=ChatKind.BUILD)
    resp = await client.patch(
        f"/v1/conversations/{conv.id}",
        headers=headers,
        json={"code": {"source": "x", "entry": "y"}},
    )
    assert resp.status_code == 400
    assert resp.json() == {
        "error": {"message": "code snapshots are no longer stored on conversations."}
    }


async def test_patch_malformed_json_body_400_leaves_row_unchanged(client, db_session) -> None:
    # A truncated body used to coerce to `{}` and return `200 {ok:true}` — a save that looks
    # successful while the builder's auto-saved code/title is silently discarded.
    headers, user = await _auth(db_session)
    conv = await ConversationFactory.create(db_session, user.id, title="original")
    resp = await client.patch(
        f"/v1/conversations/{conv.id}",
        headers={**headers, "Content-Type": "application/json"},
        content=b'{"title": "Renamed"',
    )
    assert resp.status_code == 400
    assert resp.json() == {"error": {"message": "Invalid JSON body."}}
    stored = await db_session.scalar(select(Conversation).where(Conversation.id == conv.id))
    assert stored.title == "original"


async def test_patch_non_object_body_400(client, db_session) -> None:
    headers, user = await _auth(db_session)
    conv = await ConversationFactory.create(db_session, user.id)
    for body in (["title"], "title"):
        resp = await client.patch(f"/v1/conversations/{conv.id}", headers=headers, json=body)
        assert resp.status_code == 400, body
        assert resp.json() == {"error": {"message": "Request body must be a JSON object."}}, body


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


async def test_requires_auth(client) -> None:
    assert (await client.get("/v1/conversations")).status_code == 401


def test_conversations_openapi_documents_models_and_codes() -> None:
    from src.main import create_app

    schema = create_app().openapi()
    paths = schema["paths"]
    get = paths["/v1/conversations/{conversation_id}"]["get"]["responses"]
    assert {"400", "404", "401", "500"} <= set(get)
    # The legacy append endpoint is GONE (U4's destructive reset).
    assert "/v1/conversations/{conversation_id}/messages" not in paths
    # The documented-only HeaderOut preserves the Mongo `_id` wire key + camelCase
    # timestamps, and title/context stay optional (omitted-when-unset shape).
    header = schema["components"]["schemas"]["HeaderOut"]["properties"]
    assert "_id" in header
    assert "createdAt" in header
    # `mode` is gone from the required set, not merely absent from a response body: the schema
    # is the contract the portal's hand-written mirror is read against.
    assert set(schema["components"]["schemas"]["HeaderOut"]["required"]) == {
        "_id",
        "projectId",
        "kind",
        "createdAt",
        "updatedAt",
    }
    assert "mode" not in header


# --- documented-only wire-shape characterization ------------------------------
# HeaderOut is documented-only (the route returns a hand-built JSONResponse), so the
# openapi test above proves the *schema*, not the *wire body*. These runtime assertions
# are the only guard that `_header_dict` keeps its exact key set: title/context
# ABSENT when unset, PRESENT when set. A regression emitting them as `null` (e.g. someone
# wiring `response_model` enforcement) would pass the schema test but fail here.

_BASE_HEADER_KEYS = {"_id", "projectId", "kind", "createdAt", "updatedAt"}


async def test_list_omits_unset_optional_header_keys(client, db_session) -> None:
    headers, user = await _auth(db_session)
    await ConversationFactory.create(db_session, user.id)  # no title/context/code

    resp = await client.get("/v1/conversations", headers=headers)
    assert resp.status_code == 200
    assert set(resp.json()["conversations"][0]) == _BASE_HEADER_KEYS


async def test_list_includes_set_optional_header_keys(client, db_session) -> None:
    headers, user = await _auth(db_session)
    await ConversationFactory.create(
        db_session,
        user.id,
        title="T",
        context={"theme": "dark"},
    )

    resp = await client.get("/v1/conversations", headers=headers)
    header = resp.json()["conversations"][0]
    assert set(header) == _BASE_HEADER_KEYS | {"title", "context"}
    assert header["title"] == "T"
    assert header["context"] == {"theme": "dark"}


async def test_get_omits_unset_optional_header_keys(client, db_session) -> None:
    headers, user = await _auth(db_session)
    conv = await ConversationFactory.create(db_session, user.id)  # no title/context/code

    resp = await client.get(f"/v1/conversations/{conv.id}", headers=headers)
    assert resp.status_code == 200
    assert set(resp.json()["conversation"]) == _BASE_HEADER_KEYS
