"""POST /v1/conversations/{id}/messages (append) + DELETE (delete-with-cleanup) — U9.
Byte-stable with Express (409 write-IDOR, idempotent message insert, attachment sweep)."""

from __future__ import annotations

import uuid

import pytest
from sqlalchemy import select

from src.config import settings
from src.db.models.attachment import Attachment
from src.db.models.conversation import Conversation
from src.db.models.message import Message
from src.services.auth.session_jwt import mint_session_jwt
from tests.factories import ConversationFactory, MessageFactory, ProjectFactory, UserFactory

_TTL = settings.auth.access_ttl_seconds


def _cookie(jwt: str) -> dict[str, str]:
    return {"Cookie": f"session={jwt}"}


async def _auth(db_session):
    user = await UserFactory.create(db_session)
    return _cookie(mint_session_jwt(user.id, user.token_version, _TTL)), user


async def _setup(db_session):
    """Auth + a project to create conversations inside (project-first: header.projectId
    is required on the create branch)."""
    headers, user = await _auth(db_session)
    project = await ProjectFactory.create(db_session, user.id)
    return headers, user, project


def _message(seq: int = 0, parts=None) -> dict:
    return {
        "_id": str(uuid.uuid4()),
        "role": "user",
        "seq": seq,
        "parts": parts or [{"type": "text", "text": "hi"}],
    }


def _header(project_id, kind: str = "planning") -> dict:
    return {"kind": kind, "title": "My chat", "projectId": str(project_id)}


# --- append -------------------------------------------------------------------


async def test_append_creates_header_and_message(client, db_session) -> None:
    headers, user, project = await _setup(db_session)
    cid = str(uuid.uuid4())
    msg = _message()
    resp = await client.post(
        f"/v1/conversations/{cid}/messages",
        headers=headers,
        json={"message": msg, "header": _header(project.id)},
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
    headers, user, project = await _setup(db_session)
    cid = str(uuid.uuid4())
    await client.post(
        f"/v1/conversations/{cid}/messages",
        headers=headers,
        json={"message": _message(seq=0), "header": _header(project.id)},
    )
    await client.post(
        f"/v1/conversations/{cid}/messages",
        headers=headers,
        json={
            "message": _message(seq=1, parts=[{"type": "text", "text": "again"}]),
            "header": _header(project.id),
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
    headers_a, _user_a, project_a = await _setup(db_session)
    cid = str(uuid.uuid4())
    await client.post(
        f"/v1/conversations/{cid}/messages",
        headers=headers_a,
        json={"message": _message(), "header": _header(project_a.id)},
    )
    # User B tries to append under the same conversation id → 409 (the id-collision
    # check fires before any project resolution).
    headers_b, _user_b, project_b = await _setup(db_session)
    resp = await client.post(
        f"/v1/conversations/{cid}/messages",
        headers=headers_b,
        json={"message": _message(), "header": _header(project_b.id)},
    )
    assert resp.status_code == 409
    assert resp.json() == {"error": {"message": "Conversation id already in use."}}


async def test_append_idempotent_message(client, db_session) -> None:
    headers, _user, project = await _setup(db_session)
    cid = str(uuid.uuid4())
    msg = _message()
    body = {"message": msg, "header": _header(project.id)}
    r1 = await client.post(f"/v1/conversations/{cid}/messages", headers=headers, json=body)
    r2 = await client.post(f"/v1/conversations/{cid}/messages", headers=headers, json=body)
    assert r1.status_code == 201 and r2.status_code == 201
    msgs = (
        (await db_session.execute(select(Message).where(Message.id == uuid.UUID(msg["_id"]))))
        .scalars()
        .all()
    )
    assert len(msgs) == 1  # duplicate _id is idempotent, not a second row


# The two collision tests below assert on the RESPONSE only: the append's failed insert leaves
# the session needing a rollback, so `db_session` cannot be queried afterwards. The property
# under test is exactly the response — today's bug is a 201 that writes nothing at all.
# (The filtered SAWarning is a test-harness artifact, as in the PATCH-vs-delete race test: the
# failed flush deassociates the transaction the fixture then rolls back on the same connection.)
_DEASSOCIATED = "ignore:transaction already deassociated:sqlalchemy.exc.SAWarning"


@pytest.mark.filterwarnings(_DEASSOCIATED)
async def test_append_foreign_message_id_is_not_a_false_success(client, db_session) -> None:
    # The idempotency short-circuit is owner+conversation scoped, so a client-minted `_id`
    # colliding with ANOTHER user's message can no longer be reported as a 201 while nothing
    # is written (ADR-0004). B's insert falls through to the PK constraint → a loud 409.
    headers_a, _user_a, project_a = await _setup(db_session)
    cid_a = str(uuid.uuid4())
    msg = _message()
    assert (
        await client.post(
            f"/v1/conversations/{cid_a}/messages",
            headers=headers_a,
            json={"message": msg, "header": _header(project_a.id)},
        )
    ).status_code == 201
    # A's turn really is stored under A's conversation (the state B must not be able to mask).
    # Select the COLUMN, not the entity: loading the ORM instance into this shared session's
    # identity map would collide with the router's own `Message(id=...)` insert below.
    owner_cid = await db_session.scalar(
        select(Message.conversation_id).where(Message.id == uuid.UUID(msg["_id"]))
    )
    assert owner_cid == uuid.UUID(cid_a)

    headers_b, _user_b, project_b = await _setup(db_session)
    cid_b = str(uuid.uuid4())
    resp = await client.post(
        f"/v1/conversations/{cid_b}/messages",
        headers=headers_b,
        json={"message": msg, "header": _header(project_b.id)},
    )
    assert resp.status_code == 409  # was a false 201 that dropped B's turn on the floor
    assert resp.json() == {"error": {"message": "message._id is already in use."}}


@pytest.mark.filterwarnings(_DEASSOCIATED)
async def test_append_same_message_id_in_another_conversation_is_not_a_duplicate(
    client, db_session
) -> None:
    # Same user, same `_id`, a DIFFERENT conversation: not the caller's own prior insert into
    # THIS conversation, so the short-circuit must not fire (it would silently drop the turn).
    headers, _user, project = await _setup(db_session)
    first_cid = str(uuid.uuid4())
    msg = _message()
    assert (
        await client.post(
            f"/v1/conversations/{first_cid}/messages",
            headers=headers,
            json={"message": msg, "header": _header(project.id)},
        )
    ).status_code == 201

    second_cid = str(uuid.uuid4())
    resp = await client.post(
        f"/v1/conversations/{second_cid}/messages",
        headers=headers,
        json={"message": msg, "header": _header(project.id)},
    )
    assert resp.status_code == 409
    assert resp.json() == {"error": {"message": "message._id is already in use."}}


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
    headers, _user, project = await _setup(db_session)
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
            json={"message": message, "header": _header(project.id)},
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


@pytest.mark.filterwarnings("ignore:transaction already deassociated:sqlalchemy.exc.SAWarning")
async def test_patch_losing_race_to_delete_is_404_not_500(client, db_session, monkeypatch) -> None:
    # (the filtered SAWarning is a test-harness artifact: the endpoint's failed commit
    # plus the fixture's outer rollback double-clean the same connection)
    # Builder auto-save PATCH vs a concurrent delete: the deleted row makes the flush
    # UPDATE match zero rows (StaleDataError) — the loser must get the same non-leaking
    # 404 a one-second-later PATCH would, never a 500.
    import sqlalchemy as sa

    import src.api.v1.conversations.router as conv_router

    headers, user = await _auth(db_session)
    conv = await ConversationFactory.create(db_session, user.id)

    real_load = conv_router._load_owned

    async def _load_then_lose_race(db, user_id, conversation_id):
        owned = await real_load(db, user_id, conversation_id)
        # The concurrent DELETE commits between our load and our flush.
        # synchronize_session=False: a REAL concurrent delete happens in another
        # session, so THIS session's identity map must not learn about it — the
        # flush then emits the zero-row UPDATE exactly as in production.
        await db.execute(
            sa.delete(Conversation).where(Conversation.id == owned.id),
            execution_options={"synchronize_session": False},
        )
        return owned

    monkeypatch.setattr(conv_router, "_load_owned", _load_then_lose_race)
    resp = await client.patch(f"/v1/conversations/{conv.id}", json={"title": "T"}, headers=headers)
    assert resp.status_code == 404
    assert resp.json() == {"error": {"message": "Conversation not found."}}


async def test_delete_invalid_id_400(client, db_session) -> None:
    headers, _ = await _auth(db_session)
    resp = await client.delete("/v1/conversations/bad!id", headers=headers)
    assert resp.status_code == 400
    assert resp.json() == {"error": {"message": "Invalid conversation id."}}


async def test_append_requires_auth(client) -> None:
    resp = await client.post(f"/v1/conversations/{uuid.uuid4()}/messages", json={})
    assert resp.status_code == 401


# --- U5: project binding (project-first: header.projectId required) ------------


async def test_append_binds_to_owned_project_and_lists_by_project(client, db_session) -> None:
    # AE2: a new conversation created "inside" a project carries its project_id, and listing
    # by that project returns only its children.
    headers, user, project = await _setup(db_session)
    other_project = await ProjectFactory.create(db_session, user.id)
    cid = str(uuid.uuid4())
    resp = await client.post(
        f"/v1/conversations/{cid}/messages",
        headers=headers,
        json={"message": _message(), "header": _header(project.id)},
    )
    assert resp.status_code == 201
    conv = await db_session.scalar(select(Conversation).where(Conversation.id == uuid.UUID(cid)))
    assert conv is not None and conv.project_id == project.id

    # A second conversation in a different project.
    other_cid = str(uuid.uuid4())
    await client.post(
        f"/v1/conversations/{other_cid}/messages",
        headers=headers,
        json={"message": _message(), "header": _header(other_project.id)},
    )
    listed = await client.get(f"/v1/conversations?projectId={project.id}", headers=headers)
    ids = {c["_id"] for c in listed.json()["conversations"]}
    assert ids == {cid}  # only the first project's child


async def test_append_cross_user_project_404(client, db_session) -> None:
    headers, _ = await _auth(db_session)
    other = await UserFactory.create(db_session)
    stranger_project = await ProjectFactory.create(db_session, other.id)
    cid = str(uuid.uuid4())
    resp = await client.post(
        f"/v1/conversations/{cid}/messages",
        headers=headers,
        json={"message": _message(), "header": _header(stranger_project.id)},
    )
    assert resp.status_code == 404
    # Nothing written.
    assert (
        await db_session.scalar(select(Conversation).where(Conversation.id == uuid.UUID(cid)))
        is None
    )


async def test_append_without_project_is_400(client, db_session) -> None:
    # Project-first: every chat lives in a project, so a create naming none is rejected
    # (there is no fallback project) and nothing is written.
    headers, _ = await _auth(db_session)
    cid = str(uuid.uuid4())
    resp = await client.post(
        f"/v1/conversations/{cid}/messages",
        headers=headers,
        json={"message": _message(), "header": {"kind": "planning", "title": "My chat"}},
    )
    assert resp.status_code == 400
    assert resp.json() == {"error": {"message": "header.projectId is required"}}
    assert (
        await db_session.scalar(select(Conversation).where(Conversation.id == uuid.UUID(cid)))
        is None
    )


async def test_append_invalid_project_id_400(client, db_session) -> None:
    headers, _ = await _auth(db_session)
    cid = str(uuid.uuid4())
    resp = await client.post(
        f"/v1/conversations/{cid}/messages",
        headers=headers,
        json={"message": _message(), "header": {"kind": "planning", "projectId": "not-a-uuid"}},
    )
    assert resp.status_code == 400
