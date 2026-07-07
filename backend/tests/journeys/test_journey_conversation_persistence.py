"""End-to-end JOURNEY: multi-turn conversation persistence (the SPA's chat/build history).

Drives the real FastAPI `/v1/conversations` surface the way `conversationApi.js` does across a
whole session — append several turns, read the transcript back, patch the builder header, list &
filter, then delete — and asserts the Express-parity + SPA-facing contract at each hop
(_CONTRACTS.md Journey 4). The message wire doc is the SPA shape: `{_id, role, parts, seq}` with
Mongo-style `_id` (the SPA normalizes `_id → id`), the `parts[]` JSONB round-tripping unchanged.

This is the "green baseline" journey — it should PASS: it proves the harness drives the real
persistence path end-to-end (append → get → patch → list → delete-cascade).
"""

from __future__ import annotations

import datetime
import uuid
from typing import Any

from sqlalchemy import select

from src.config import settings
from src.db.models.conversation import Conversation, ConversationKind
from src.db.models.message import Message
from src.services.auth.session_jwt import mint_session_jwt
from tests.factories import ConversationFactory, UserFactory
from tests.fakes import FakeStorage

_TTL = settings.auth.access_ttl_seconds
_UTC = datetime.UTC


def _cookie(jwt: str) -> dict[str, str]:
    return {"Cookie": f"session={jwt}"}


async def _auth(db_session):
    """Create a user + return (cookie-headers, user)."""
    user = await UserFactory.create(db_session)
    return _cookie(mint_session_jwt(user.id, user.token_version, _TTL)), user


def _header(kind: str = "builder", title: str = "My Builder App") -> dict[str, Any]:
    """The conversation-header envelope the SPA upserts on every append."""
    return {"kind": kind, "title": title}


# The four-turn transcript in the SPA message-doc shape (`_id`/role/parts/seq), user/assistant
# alternating, strictly increasing seq, mixed parts: text parts + `file` parts (image + document).
def _turns() -> list[dict[str, Any]]:
    return [
        {
            "_id": str(uuid.uuid4()),
            "role": "user",
            "seq": 0,
            "parts": [
                {"type": "text", "text": "Build me an expenses tracker, here's a mockup."},
                {
                    "type": "file",
                    "attachmentId": "att_mockup_0",
                    "kind": "image",
                    "mediaType": "image/png",
                    "name": "mockup.png",
                    "size": 2048,
                },
            ],
        },
        {
            "_id": str(uuid.uuid4()),
            "role": "assistant",
            "seq": 1,
            "parts": [{"type": "text", "text": "On it — scaffolding the tracker now."}],
        },
        {
            "_id": str(uuid.uuid4()),
            "role": "user",
            "seq": 2,
            "parts": [
                {"type": "text", "text": "Also import last quarter from this PDF."},
                {
                    "type": "file",
                    "attachmentId": "att_ledger_2",
                    "kind": "document",
                    "mediaType": "application/pdf",
                    "name": "q1-ledger.pdf",
                    "size": 4096,
                },
            ],
        },
        {
            "_id": str(uuid.uuid4()),
            "role": "assistant",
            "seq": 3,
            "parts": [{"type": "text", "text": "Imported 42 rows and wired the table."}],
        },
    ]


async def test_journey_conversation_persistence(client, app, db_session) -> None:
    # The DELETE endpoint depends on the attachment object store; this journey lives outside
    # tests/api/v1/conversations/ (whose conftest auto-overrides it), so swap in the in-memory
    # fake here — no file parts have real Attachment rows, so nothing is actually swept.
    from src.api.v1.attachments.router import storage_dependency

    app.dependency_overrides[storage_dependency] = lambda: FakeStorage()

    headers, user = await _auth(db_session)
    cid = str(uuid.uuid4())  # the SPA mints the conversation id (a builder id == the future appId)
    turns = _turns()

    # 1. Multi-turn append: each turn is its own request (a real session accretes over time).
    #    The first append creates the header; subsequent ones upsert it (no duplicate row).
    for turn in turns:
        resp = await client.post(
            f"/v1/conversations/{cid}/messages",
            headers=headers,
            json={"message": turn, "header": _header()},
        )
        assert resp.status_code == 201, turn["seq"]
        assert resp.json() == {"ok": True, "message": {"_id": turn["_id"], "seq": turn["seq"]}}

    # 2. GET the transcript: all four turns come back, seq-ordered, with the SPA doc shape and
    #    `parts[]` round-tripped unchanged (text + file parts, byte-for-byte the same structure).
    resp = await client.get(f"/v1/conversations/{cid}", headers=headers)
    assert resp.status_code == 200
    body = resp.json()
    assert body["conversation"]["_id"] == cid
    assert body["conversation"]["kind"] == "builder"
    assert body["conversation"]["title"] == "My Builder App"
    assert body["conversation"]["createdAt"].endswith("Z")

    messages = body["messages"]
    assert len(messages) == 4
    assert [m["seq"] for m in messages] == [0, 1, 2, 3]  # seq-ascending, all four present
    assert [m["role"] for m in messages] == ["user", "assistant", "user", "assistant"]
    assert [m["_id"] for m in messages] == [t["_id"] for t in turns]  # _id preserved, not remapped
    # The load-bearing assertion: every message's parts array is identical to what was sent.
    assert [m["parts"] for m in messages] == [t["parts"] for t in turns]

    # 3. Builder PATCH: rename + save a code snapshot. The server wraps the snapshot under
    #    `current` (the SPA reads it back at header.code.current.source).
    snapshot = {"source": "export default function App(){ return <div>v2</div>; }", "entry": "App"}
    resp = await client.patch(
        f"/v1/conversations/{cid}",
        headers=headers,
        json={"title": "Renamed App", "code": snapshot},
    )
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}

    reread = (await client.get(f"/v1/conversations/{cid}", headers=headers)).json()
    assert reread["conversation"]["title"] == "Renamed App"
    assert reread["conversation"]["code"] == {"current": snapshot}
    assert reread["conversation"]["code"]["current"]["source"] == snapshot["source"]
    # The transcript survives the header patch untouched.
    assert [m["seq"] for m in reread["messages"]] == [0, 1, 2, 3]

    # 4. Seed two more of the caller's conversations with explicit, unambiguous updated_at so the
    #    newest-first ordering is deterministic (transaction `now()` ties, so pin the extremes).
    newest = await ConversationFactory.create(
        db_session,
        user.id,
        kind=ConversationKind.PLANNING,
        title="freshest",
        updated_at=datetime.datetime(2030, 1, 1, tzinfo=_UTC),
    )
    oldest = await ConversationFactory.create(
        db_session,
        user.id,
        kind=ConversationKind.PLANNING,
        title="stalest",
        updated_at=datetime.datetime(2020, 1, 1, tzinfo=_UTC),
    )

    # 5. List (no filter): scoped to the caller, newest-first by updated_at. The 2030 row leads,
    #    the 2020 row trails, and the returned timestamps are monotonically non-increasing.
    convs = (await client.get("/v1/conversations", headers=headers)).json()["conversations"]
    ids = [c["_id"] for c in convs]
    assert cid in ids  # the builder conversation is in the caller's list
    assert ids[0] == str(newest.id)
    assert ids[-1] == str(oldest.id)
    updated_ats = [c["updatedAt"] for c in convs]
    # Fixed-width UTC ISO-8601 (`...Z`) sorts lexicographically == chronologically.
    assert updated_ats == sorted(updated_ats, reverse=True)

    # 6. `?kind` filter: builder returns only the builder conversation; planning returns the two
    #    planning rows newest-first; an unknown kind is a 400 (not a silent empty list).
    builder_list = (
        await client.get("/v1/conversations?kind=builder", headers=headers)
    ).json()["conversations"]
    assert [c["_id"] for c in builder_list] == [cid]

    planning_list = (
        await client.get("/v1/conversations?kind=planning", headers=headers)
    ).json()["conversations"]
    assert [c["_id"] for c in planning_list] == [str(newest.id), str(oldest.id)]

    bogus = await client.get("/v1/conversations?kind=bogus", headers=headers)
    assert bogus.status_code == 400
    assert bogus.json() == {"error": {"message": "Unknown kind."}}

    # 7. Cross-user isolation guard: user B can neither read nor delete user A's conversation.
    headers_b, _ = await _auth(db_session)
    assert (await client.get(f"/v1/conversations/{cid}", headers=headers_b)).status_code == 404
    assert (await client.delete(f"/v1/conversations/{cid}", headers=headers_b)).status_code == 404

    # 8. DELETE cascades: the conversation and ALL four of its messages are purged; the caller's
    #    other conversations are untouched (owner-scoped, single-row delete).
    resp = await client.delete(f"/v1/conversations/{cid}", headers=headers)
    assert resp.status_code == 200
    assert resp.json() == {"ok": True}

    assert (await client.get(f"/v1/conversations/{cid}", headers=headers)).status_code == 404
    cid_uuid = uuid.UUID(cid)
    assert (
        await db_session.scalar(select(Conversation).where(Conversation.id == cid_uuid))
    ) is None
    surviving_messages = (
        (await db_session.execute(select(Message).where(Message.conversation_id == cid_uuid)))
        .scalars()
        .all()
    )
    assert surviving_messages == []  # FK ON DELETE CASCADE swept the messages
    still_there = (await client.get("/v1/conversations", headers=headers)).json()["conversations"]
    assert {c["_id"] for c in still_there} == {str(newest.id), str(oldest.id)}
