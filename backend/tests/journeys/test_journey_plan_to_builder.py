"""Journey: Plan → Builder handoff — a planning transcript, then a DISTINCT builder build.

Maps to `_CONTRACTS.md` Journey 4 (conversation/build persistence) + the persistence half of
Journey 5. The SPA drives a PLANNING conversation (the chat transcript the user reasons in),
then hands off to a BUILDER conversation whose client-minted id IS the future `appId`
(`app_registry.conversation_id` soft-links to it — `conversation.py:1-8`). The two are SEPARATE
rows with distinct `kind`s, BOTH owner-scoped, and the builder keeps its generation turn.

This is the green baseline: FastAPI ports the Express `/api/conversations` wire shape verbatim
(`_id`, camelCase timestamps, `{error:{message}}`), so the whole journey MUST pass. Every
assertion is a promise `conversationApi.js` / `builderHistory.js` already rely on.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import func, select

from src.config import settings
from src.db.models.conversation import Conversation, ConversationKind
from src.db.models.message import Message
from src.services.auth.session_jwt import mint_session_jwt
from tests.factories import UserFactory

_TTL = settings.auth.access_ttl_seconds


def _cookie(jwt: str) -> dict[str, str]:
    return {"Cookie": f"session={jwt}"}


async def _auth(db_session, **overrides: Any):
    """Create a user + return (cookie-headers, user)."""
    user = await UserFactory.create(db_session, **overrides)
    return _cookie(mint_session_jwt(user.id, user.token_version, _TTL)), user


def _text_turn(role: str, seq: int, text: str) -> dict[str, Any]:
    """A neutral SPA message doc (`_id` is the client-minted key the server preserves)."""
    return {
        "_id": str(uuid.uuid4()),
        "role": role,
        "seq": seq,
        "parts": [{"type": "text", "text": text}],
    }


async def _append(client, headers, cid: str, message: dict[str, Any], kind: str, title: str):
    return await client.post(
        f"/v1/conversations/{cid}/messages",
        headers=headers,
        json={"message": message, "header": {"kind": kind, "title": title}},
    )


async def test_plan_to_builder_handoff_two_distinct_kinded_conversations(
    client, db_session
) -> None:
    headers, user = await _auth(db_session)

    # --- 1. PLANNING conversation: a user turn + an assistant turn ---------------
    plan_cid = str(uuid.uuid4())
    plan_user = _text_turn("user", 0, "I need an app to track visitor passes.")
    plan_assistant = _text_turn(
        "assistant", 1, "Let's plan: a form to log visitors and a table to view them."
    )
    for turn in (plan_user, plan_assistant):
        resp = await _append(client, headers, plan_cid, turn, "planning", "Visitor passes")
        assert resp.status_code == 201, resp.text
        assert resp.json() == {"ok": True, "message": {"_id": turn["_id"], "seq": turn["seq"]}}

    # Read the planning transcript back: header kind=planning, turns seq-ordered.
    plan_view = (await client.get(f"/v1/conversations/{plan_cid}", headers=headers)).json()
    assert plan_view["conversation"]["_id"] == plan_cid
    assert plan_view["conversation"]["kind"] == ConversationKind.PLANNING.value == "planning"
    assert [m["seq"] for m in plan_view["messages"]] == [0, 1]
    assert [m["role"] for m in plan_view["messages"]] == ["user", "assistant"]

    # --- 2. BUILDER conversation: the handoff mints a DISTINCT row ---------------
    # The SPA navigates to /workspace/builder with a fresh conversation id (Journey 5) — it is
    # NOT the planning id; the builder conversation's id becomes the deployed appId.
    build_cid = str(uuid.uuid4())
    assert build_cid != plan_cid

    # A builder kicks off with the (summarized) prompt as its first user turn...
    build_prompt = _text_turn("user", 0, "Build: a visitor-pass logger with a form and a table.")
    resp = await _append(client, headers, build_cid, build_prompt, "builder", "Visitor passes app")
    assert resp.status_code == 201, resp.text

    # ...then the builder persists its GENERATION turn (the assistant's generated app output).
    generation = _text_turn(
        "assistant",
        1,
        "export default function VisitorPassApp(){ return <div>Visitor Passes</div>; }",
    )
    resp = await _append(client, headers, build_cid, generation, "builder", "Visitor passes app")
    assert resp.status_code == 201, resp.text

    # The builder generation turn round-trips (role, seq, parts) on read.
    build_view = (await client.get(f"/v1/conversations/{build_cid}", headers=headers)).json()
    assert build_view["conversation"]["kind"] == ConversationKind.BUILDER.value == "builder"
    gen_msg = next(m for m in build_view["messages"] if m["_id"] == generation["_id"])
    assert gen_msg["role"] == "assistant"
    assert gen_msg["seq"] == 1
    assert gen_msg["parts"] == generation["parts"]  # generated code preserved verbatim

    # --- 3. Two DISTINCT rows, correctly kinded, both scoped to the one owner ----
    convs = (
        (
            await db_session.execute(
                select(Conversation)
                .where(Conversation.user_id == user.id)
                .order_by(Conversation.kind)
            )
        )
        .scalars()
        .all()
    )
    assert len(convs) == 2  # planning and builder are SEPARATE rows, not one reused row
    by_kind = {c.kind: c for c in convs}
    assert set(by_kind) == {ConversationKind.PLANNING, ConversationKind.BUILDER}
    plan_row = by_kind[ConversationKind.PLANNING]
    build_row = by_kind[ConversationKind.BUILDER]
    assert str(plan_row.id) == plan_cid
    assert str(build_row.id) == build_cid
    assert plan_row.id != build_row.id
    # Correct user scoping: both rows owned by the caller (a dropped predicate is a leak).
    assert plan_row.user_id == user.id
    assert build_row.user_id == user.id

    # The stored kinds are the ConversationKind enum members (native PG enum, not raw strings).
    assert plan_row.kind is ConversationKind.PLANNING
    assert build_row.kind is ConversationKind.BUILDER
    # And their wire values are exactly the enum's declared value set.
    assert {plan_row.kind.value, build_row.kind.value} <= {k.value for k in ConversationKind}
    assert {k.value for k in ConversationKind} == {"planning", "assistant", "builder"}

    # --- 4. append(header.kind="builder") CREATES then KEEPS kind=builder --------
    # The generation append above was the second builder write; kind must still be builder
    # (kind is set once at creation and never flipped by a later append).
    followup = _text_turn("user", 2, "Add a search box to the table.")
    resp = await _append(client, headers, build_cid, followup, "builder", "Visitor passes app")
    assert resp.status_code == 201, resp.text
    refreshed = await db_session.scalar(
        select(Conversation).where(Conversation.id == uuid.UUID(build_cid))
    )
    assert refreshed is not None
    assert refreshed.kind is ConversationKind.BUILDER  # still builder — creates/keeps

    # The builder now holds three seq-ordered turns; the planning transcript is untouched.
    build_msg_count = await db_session.scalar(
        select(func.count())
        .select_from(Message)
        .where(Message.conversation_id == uuid.UUID(build_cid))
    )
    assert build_msg_count == 3
    plan_msg_count = await db_session.scalar(
        select(func.count())
        .select_from(Message)
        .where(Message.conversation_id == uuid.UUID(plan_cid))
    )
    assert plan_msg_count == 2

    # --- 5. The kind filter distinguishes the two conversations at the wire ------
    planning_list = (
        await client.get("/v1/conversations?kind=planning", headers=headers)
    ).json()["conversations"]
    builder_list = (
        await client.get("/v1/conversations?kind=builder", headers=headers)
    ).json()["conversations"]
    assert [c["_id"] for c in planning_list] == [plan_cid]
    assert [c["_id"] for c in builder_list] == [build_cid]
    assert all(c["kind"] == "planning" for c in planning_list)
    assert all(c["kind"] == "builder" for c in builder_list)

    # --- 6. Cross-user isolation: user B can neither read nor hijack the build ----
    headers_b, _user_b = await _auth(db_session, email="intruder@rvaiglobal.com")
    read_b = await client.get(f"/v1/conversations/{build_cid}", headers=headers_b)
    assert read_b.status_code == 404
    # And user B cannot append under the builder id (write-IDOR closed → 409, not overwrite).
    hijack = await _append(
        client, headers_b, build_cid, _text_turn("user", 0, "mine now"), "builder", "stolen"
    )
    assert hijack.status_code == 409
    # The row is still owned by, and still visible to, the original owner.
    still_owned = await db_session.scalar(
        select(Conversation).where(Conversation.id == uuid.UUID(build_cid))
    )
    assert still_owned is not None
    assert still_owned.user_id == user.id
