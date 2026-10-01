"""GET/PATCH /v1/conversations — user-scoped list, header get, patch.
Keeps the Express-era wire shape (`_id`, `{error:{message}}`); there is no message
read/append endpoint on this router."""

from __future__ import annotations

import datetime
import uuid

import pytest
import sqlalchemy as sa
from sqlalchemy import select

from src.config import settings
from src.db.models.conversation import ChatKind, Conversation
from src.services.auth.session_jwt import mint_session_jwt
from tests.factories import ConversationFactory, ProjectFactory, UserFactory

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
    assert convs[0]["kind"] == "build"  # the factory's default
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
    # What the chat IS, and nothing beside it.
    assert body["conversation"]["kind"] == "plan"
    assert "mode" not in body["conversation"]
    # This endpoint returns the header only; message content is not included.
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
    """The `code` column no longer exists on conversations — a client still sending a snapshot
    gets a 400 naming the retirement (never a silent ignore that looks like a saved snapshot)."""
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
    assert await _title_of(db_session, theirs.id) == "theirs"
    # …and the same request from the owner lands, so the 404 above is the scoping speaking.
    owner_headers = _cookie(mint_session_jwt(other.id, other.token_version, _TTL))
    owned = await client.patch(
        f"/v1/conversations/{theirs.id}", headers=owner_headers, json={"title": "renamed"}
    )
    assert owned.status_code == 200
    assert await _title_of(db_session, theirs.id) == "renamed"


# --- rename -------------------------------------------------------------------
# A rename is not activity: the list orders by `updated_at`, so a rename that moved it would
# reorder the history list and could make an old chat the "Last chat".

_TWO_DAYS_AGO = datetime.datetime.now(_UTC) - datetime.timedelta(days=2)
_ONE_DAY_AGO = datetime.datetime.now(_UTC) - datetime.timedelta(days=1)


async def _title_of(db_session, conversation_id: uuid.UUID) -> str | None:
    return await db_session.scalar(
        sa.select(Conversation.title).where(Conversation.id == conversation_id)
    )


async def _updated_at_of(db_session, conversation_id: uuid.UUID) -> datetime.datetime:
    return await db_session.scalar(
        sa.select(Conversation.updated_at).where(Conversation.id == conversation_id)
    )


async def _rename(client, headers, conversation_id: uuid.UUID, title: object):
    return await client.patch(
        f"/v1/conversations/{conversation_id}", headers=headers, json={"title": title}
    )


async def test_a_rename_is_stored_and_listed(client, db_session) -> None:
    headers, user = await _auth(db_session)
    conv = await ConversationFactory.create(db_session, user.id, title="Untitled")

    resp = await _rename(client, headers, conv.id, "Visitor counts by gate")

    assert resp.status_code == 200
    assert resp.json() == {"ok": True}
    listed = (await client.get("/v1/conversations", headers=headers)).json()["conversations"]
    assert [c["title"] for c in listed] == ["Visitor counts by gate"]


async def test_a_rename_is_trimmed_before_it_is_stored(client, db_session) -> None:
    headers, user = await _auth(db_session)
    conv = await ConversationFactory.create(db_session, user.id, title="Untitled")

    resp = await _rename(client, headers, conv.id, "   Gate report  ")

    assert resp.status_code == 200
    assert await _title_of(db_session, conv.id) == "Gate report"


async def test_a_name_of_exactly_the_limit_is_kept_whole(client, db_session) -> None:
    headers, user = await _auth(db_session)
    conv = await ConversationFactory.create(db_session, user.id, title="Untitled")
    name = "a" * 120

    resp = await _rename(client, headers, conv.id, name)

    assert resp.status_code == 200
    assert await _title_of(db_session, conv.id) == name


@pytest.mark.parametrize(
    ("title", "message", "code"),
    [
        ("", "Give the chat a name.", "title_required"),
        ("    ", "Give the chat a name.", "title_required"),
        ("\t \n", "Give the chat a name.", "title_required"),
        ("a" * 121, "Keep the name under 120 characters.", "title_too_long"),
        ("  " + "a" * 121 + "  ", "Keep the name under 120 characters.", "title_too_long"),
        (
            "first line\nsecond line",
            "The name cannot contain line breaks or control characters.",
            "title_invalid",
        ),
        (
            "carriage\rreturn",
            "The name cannot contain line breaks or control characters.",
            "title_invalid",
        ),
        (
            "a\ttab",
            "The name cannot contain line breaks or control characters.",
            "title_invalid",
        ),
        (
            "nul\x00byte",
            "The name cannot contain line breaks or control characters.",
            "title_invalid",
        ),
        (
            "line separator",
            "The name cannot contain line breaks or control characters.",
            "title_invalid",
        ),
        (None, "title must be a string", "title_invalid"),
        (42, "title must be a string", "title_invalid"),
        (["a list"], "title must be a string", "title_invalid"),
    ],
)
async def test_an_unusable_name_is_refused_and_changes_nothing(
    client, db_session, title: object, message: str, code: str
) -> None:
    headers, user = await _auth(db_session)
    conv = await ConversationFactory.create(
        db_session, user.id, title="Kept", updated_at=_TWO_DAYS_AGO
    )

    resp = await _rename(client, headers, conv.id, title)

    assert resp.status_code == 400
    assert resp.json() == {"error": {"message": message, "code": code}}
    assert await _title_of(db_session, conv.id) == "Kept"
    assert await _updated_at_of(db_session, conv.id) == _TWO_DAYS_AGO


async def test_a_rename_keeps_the_chats_updated_time(client, db_session) -> None:
    """Seeded two days old so a bump to the transaction's `now()` is a visible difference. Goes
    red if the title write lets the ORM's `onupdate` fire."""
    headers, user = await _auth(db_session)
    conv = await ConversationFactory.create(
        db_session, user.id, title="Old name", updated_at=_TWO_DAYS_AGO
    )

    assert (await _rename(client, headers, conv.id, "New name")).status_code == 200

    assert await _title_of(db_session, conv.id) == "New name"
    assert await _updated_at_of(db_session, conv.id) == _TWO_DAYS_AGO


async def test_renaming_an_older_chat_leaves_the_list_order_alone(client, db_session) -> None:
    headers, user = await _auth(db_session)
    older = await ConversationFactory.create(
        db_session, user.id, title="Older", updated_at=_TWO_DAYS_AGO
    )
    newer = await ConversationFactory.create(
        db_session, user.id, title="Newer", updated_at=_ONE_DAY_AGO
    )

    assert (await _rename(client, headers, older.id, "Older, renamed")).status_code == 200

    listed = (await client.get("/v1/conversations", headers=headers)).json()["conversations"]
    assert [c["_id"] for c in listed] == [str(newer.id), str(older.id)]
    assert listed[1]["title"] == "Older, renamed"


async def test_a_context_write_still_moves_the_chats_updated_time(client, db_session) -> None:
    """The other writer keeps its bump: a context PATCH is activity."""
    headers, user = await _auth(db_session)
    conv = await ConversationFactory.create(db_session, user.id, updated_at=_TWO_DAYS_AGO)

    resp = await client.patch(
        f"/v1/conversations/{conv.id}", headers=headers, json={"context": {"step": 2}}
    )

    assert resp.status_code == 200
    assert await _updated_at_of(db_session, conv.id) > _TWO_DAYS_AGO


async def test_a_rename_carried_with_a_context_write_is_cleaned_and_moves_the_time(
    client, db_session
) -> None:
    headers, user = await _auth(db_session)
    conv = await ConversationFactory.create(db_session, user.id, updated_at=_TWO_DAYS_AGO)

    resp = await client.patch(
        f"/v1/conversations/{conv.id}",
        headers=headers,
        json={"title": "  Both at once ", "context": {"step": 3}},
    )

    assert resp.status_code == 200
    assert await _title_of(db_session, conv.id) == "Both at once"
    assert await _updated_at_of(db_session, conv.id) > _TWO_DAYS_AGO


async def test_a_bad_name_carried_with_a_context_write_refuses_both(client, db_session) -> None:
    headers, user = await _auth(db_session)
    conv = await ConversationFactory.create(
        db_session, user.id, title="Kept", context={"step": 1}, updated_at=_TWO_DAYS_AGO
    )

    resp = await client.patch(
        f"/v1/conversations/{conv.id}", headers=headers, json={"title": "", "context": {"step": 9}}
    )

    assert resp.status_code == 400
    assert await _title_of(db_session, conv.id) == "Kept"
    stored_context = await db_session.scalar(
        sa.select(Conversation.context).where(Conversation.id == conv.id)
    )
    assert stored_context == {"step": 1}
    assert await _updated_at_of(db_session, conv.id) == _TWO_DAYS_AGO


async def test_a_generic_chat_renames_like_a_project_chat(client, db_session) -> None:
    headers, user = await _auth(db_session)
    conv = await ConversationFactory.create(
        db_session, user.id, kind=ChatKind.GENERIC, title="Ask", updated_at=_TWO_DAYS_AGO
    )

    assert (await _rename(client, headers, conv.id, "  Leave policy ")).status_code == 200
    assert (await _rename(client, headers, conv.id, " ")).status_code == 400

    assert await _title_of(db_session, conv.id) == "Leave policy"
    assert await _updated_at_of(db_session, conv.id) == _TWO_DAYS_AGO


async def test_a_project_scoped_list_leaves_out_the_same_users_generic_chat(
    client, db_session
) -> None:
    headers, user = await _auth(db_session)
    project = await ProjectFactory.create(db_session, user.id)
    plan = await ConversationFactory.create(
        db_session, user.id, project_id=project.id, kind=ChatKind.PLAN
    )
    build = await ConversationFactory.create(
        db_session, user.id, project_id=project.id, kind=ChatKind.BUILD
    )
    generic = await ConversationFactory.create(db_session, user.id, kind=ChatKind.GENERIC)

    scoped = await client.get(f"/v1/conversations?projectId={project.id}", headers=headers)
    unscoped = await client.get("/v1/conversations", headers=headers)

    assert {c["_id"] for c in scoped.json()["conversations"]} == {str(plan.id), str(build.id)}
    # The liveness half: the generic chat exists and is listed when nothing scopes it out.
    assert str(generic.id) in {c["_id"] for c in unscoped.json()["conversations"]}


async def test_a_project_scoped_list_shows_nothing_for_a_project_the_caller_does_not_own(
    client, db_session
) -> None:
    headers, user = await _auth(db_session)
    other = await UserFactory.create(db_session)
    theirs = await ProjectFactory.create(db_session, other.id)
    mine = await ProjectFactory.create(db_session, user.id)
    await ConversationFactory.create(db_session, other.id, project_id=theirs.id)
    own = await ConversationFactory.create(db_session, user.id, project_id=mine.id)

    foreign = await client.get(f"/v1/conversations?projectId={theirs.id}", headers=headers)
    owned = await client.get(f"/v1/conversations?projectId={mine.id}", headers=headers)

    assert foreign.status_code == 200
    assert foreign.json() == {"conversations": []}
    assert [c["_id"] for c in owned.json()["conversations"]] == [str(own.id)]


async def test_the_list_returns_the_newest_200_and_leaves_the_oldest_out(
    client, db_session
) -> None:
    headers, user = await _auth(db_session)
    project = await ProjectFactory.create(db_session, user.id)
    start = datetime.datetime(2026, 7, 1, tzinfo=_UTC)
    made = [
        await ConversationFactory.create(
            db_session,
            user.id,
            project_id=project.id,
            updated_at=start + datetime.timedelta(minutes=minute),
        )
        for minute in range(201)
    ]

    resp = await client.get(f"/v1/conversations?projectId={project.id}", headers=headers)

    ids = [c["_id"] for c in resp.json()["conversations"]]
    assert len(ids) == 200
    assert ids == [str(c.id) for c in reversed(made[1:])]
    assert str(made[0].id) not in ids


async def test_requires_auth(client) -> None:
    assert (await client.get("/v1/conversations")).status_code == 401


def test_conversations_openapi_documents_models_and_codes() -> None:
    from src.main import create_app

    schema = create_app().openapi()
    paths = schema["paths"]
    get = paths["/v1/conversations/{conversation_id}"]["get"]["responses"]
    assert {"400", "404", "401", "500"} <= set(get)
    # No append endpoint exists for this router.
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
