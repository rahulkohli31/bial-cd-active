"""The per-conversation guardrail, at the routes that enforce it.

★ THIS IS THE FILE WHOSE ABSENCE LET THE REGRESSION THROUGH. The old client-side guardrail died
with `ChatPage.tsx` and nothing turned red, because the only tests that covered it were
deleted in the same commit. Meanwhile an administrator had been setting a number in a field
whose help text promised a hard stop, and no call site anywhere — front or back — read it.

Every test here is about the number MEANING something. A gate that refused everything would
satisfy half of them, which is why the first one exists.

WHAT THE GATE READS CHANGED, AND EVERY TEST BELOW IS DRIVEN ACCORDINGLY. It used to estimate a
conversation's occupancy — characters to tokens, a flat nominal per attachment, a reserve for
the system prompt it could not see. It now reads the token count the PROVIDER reported for a
turn it served, and derives nothing. So `_stuff_the_conversation` persists a MEASUREMENT
rather than a pile of characters, and the rule itself is pinned next door in
`tests/services/usage/test_context_window.py`.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import uuid
from typing import Any

import pytest
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    UserPromptPart,
)
from pydantic_ai.models.function import (
    AgentInfo,
    DeltaToolCall,
    DeltaToolCalls,
    FunctionModel,
)
from pydantic_ai.usage import RequestUsage
from sqlalchemy import func, select

from src.api.v1.conversations._shared import (
    MAX_ATTACHMENT_BLOCKS,
    MAX_FILES_PER_MESSAGE,
    MAX_MESSAGE_TEXT_CHARS,
)
from src.api.v1.conversations._shared import chat_model as chat_model_dep
from src.api.v1.conversations.transition import PLAN_TOO_LONG_CODE
from src.db.models.conversation import ChatKind, Conversation
from src.db.models.message import Message, MessageEntryKind
from src.db.models.token_usage import TokenUsage
from src.db.models.user_limit import UserLimit
from src.main import create_app
from src.services.messages.store import append_batch
from src.services.turns.copy import CHAT_TOO_LONG_CODE, CHAT_TOO_LONG_TEXT
from src.services.turns.engine import PENDING_META_KIND
from src.services.turns.plan_options import find_pending
from src.services.usage.limits import DEFAULT_CONTEXT_HARD, MODEL_CONTEXT_WINDOW
from tests.api.v1.conversations.conftest import _headers
from tests.factories import ConversationFactory, ProjectFactory, UserFactory
from tests.pdfs import pdf_with_pages

# The turn-driving fixtures live in `conftest.py` — four files needed the same four, and
# two of them were the 3rd and 4th copy. Named here rather than autouse there, because the
# other files in this directory drive no turns.
pytestmark = pytest.mark.usefixtures("_fresh_engine", "_override_billing")


@pytest.fixture(autouse=True)
def _a_model(app) -> None:
    """Every test here should be decided by the GUARDRAIL, never by a missing model. Bound for
    all of them so a 503 can never be mistaken for a refusal that worked."""
    from src.api.v1.conversations._shared import chat_model

    async def _stream(_messages: list[ModelMessage], _info: AgentInfo):
        yield "ok"

    app.dependency_overrides[chat_model] = lambda: FunctionModel(stream_function=_stream)


async def _settle(engine: Any, conversation_id: uuid.UUID) -> None:
    state = engine.peek(conversation_id)
    if state is None or state.task is None:
        return
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(state.task, timeout=10)


async def _send(client, user, conversation_id: uuid.UUID, text: str = "carry on"):
    return await client.post(
        f"/v1/conversations/{conversation_id}/turns",
        headers=_headers(user),
        json={"message": {"text": text, "attachmentTexts": [], "attachmentIds": []}},
    )


async def _stuff_the_conversation(db_session, user, conversation, *, tokens: int) -> None:
    """Persist a conversation the provider REPORTED at `tokens`, through the real store — not a
    stub of it. The gate reads what `load_history` returns, so a history assembled any other way
    would prove the rule and not the wiring; the count has to survive the JSONB round trip to
    reach the gate at all, and here it does."""
    await append_batch(
        db_session,
        user_id=user.id,
        conversation_id=conversation.id,
        messages=[
            ModelRequest(parts=[UserPromptPart(content="tell me about the visitor log")]),
            ModelResponse(
                parts=[TextPart(content="here is what I would build")],
                usage=RequestUsage(input_tokens=tokens),
            ),
        ],
        entry_kind=MessageEntryKind.TURN,
        kind=conversation.kind,
    )
    await db_session.commit()


def _offering_model(call_id: str = "opt-build", plan: str = "Build the visitor log."):
    """A Plan turn whose whole output is the offer call — the shape that leaves a pending card
    for the handoff route to find."""

    async def _stream(_messages: list[ModelMessage], _info: AgentInfo):
        yield DeltaToolCalls(
            {
                0: DeltaToolCall(
                    name="present_plan_options",
                    json_args=json.dumps({"plan": plan}),
                    tool_call_id=call_id,
                )
            }
        )

    return FunctionModel(stream_function=_stream)


def _plain_model() -> FunctionModel:
    """A turn that answers with prose and offers nothing — so the card under test is the one
    the PREVIOUS turn left pending, never a fresh one this turn presented."""

    async def _stream(_messages: list[ModelMessage], _info: AgentInfo):
        yield "here is a revised idea"

    return FunctionModel(stream_function=_stream)


async def _a_conversation(db_session, *, kind: ChatKind = ChatKind.PLAN):
    user = await UserFactory.create(db_session)
    project = await ProjectFactory.create(db_session, user.id)
    conversation = await ConversationFactory.create(
        db_session, user.id, project_id=project.id, kind=kind
    )
    return user, project, conversation


# --- the gate has to let ordinary conversations through -------------------------------


async def test_a_short_conversation_starts_a_turn_normally(
    client, db_session, _fresh_engine
) -> None:
    """★ THE POSITIVE CASE, FIRST. Every other test here asserts a refusal, and a gate that
    refused every turn would pass all of them. This is the one that says the guardrail is a
    boundary rather than a wall."""
    user, _project, conversation = await _a_conversation(db_session)
    await _stuff_the_conversation(db_session, user, conversation, tokens=1_000)

    resp = await _send(client, user, conversation.id)

    assert resp.status_code == 202, resp.text
    await _settle(_fresh_engine, conversation.id)


async def test_the_accept_hands_back_the_very_number_it_admitted_on(
    client, db_session, _fresh_engine
) -> None:
    """★ THE METER AND THE WALL ARE ONE NUMBER, and this is the seam that makes it
    true: the 202 carries `contextTokens`, which is what `enforce_context_limit` measured to
    decide this exact request.

    Asserted as a BOUNDARY rather than as an equality alone, because equality to a figure the
    test itself seeded proves only that a number came back. The second send is one token past
    the ceiling and is refused with `occupied` equal to the figure the first send reported — so
    the number a citizen watches really is the number they will be stopped at, not a second
    reading of one scale.

    AND NOTHING WAS ASKED TO SIZE ANYTHING. The figure rides the send the citizen was making
    anyway; there is no endpoint that counts a message before it is sent, and this platform has
    settled that there will not be one."""
    user, _project, conversation = await _a_conversation(db_session)
    await _stuff_the_conversation(db_session, user, conversation, tokens=DEFAULT_CONTEXT_HARD - 1)

    accepted = await _send(client, user, conversation.id)

    assert accepted.status_code == 202, accepted.text
    shown = accepted.json()["contextTokens"]
    assert shown == DEFAULT_CONTEXT_HARD - 1
    await _settle(_fresh_engine, conversation.id)

    # One token more of the same conversation, and the wall is at the number the meter showed.
    await _stuff_the_conversation(db_session, user, conversation, tokens=DEFAULT_CONTEXT_HARD)
    refused = await _send(client, user, conversation.id)
    assert refused.status_code == 413
    assert refused.json()["error"]["detail"]["hardLimit"] == shown + 1


async def test_a_chat_nobody_has_measured_reports_no_figure_rather_than_zero(
    client, db_session, _fresh_engine
) -> None:
    """★ EDGE CASE: unmeasured is not empty. A brand-new chat has no served turn, so there is no
    count — and `0` would be a claim (this chat is empty) where `null` is the absence of one.

    It matters because the browser acts on the difference: `null` keeps the meter silent, and a
    number invites it to draw. Seeding the meter with a zero it was never given is how a guess
    creeps back in."""
    user, _project, conversation = await _a_conversation(db_session)

    resp = await _send(client, user, conversation.id)

    assert resp.status_code == 202, resp.text
    assert resp.json()["contextTokens"] is None
    await _settle(_fresh_engine, conversation.id)


# --- past the limit: refused, and nothing written --------------------------------------


async def test_an_over_long_conversation_is_refused_before_anything_persists(
    client, db_session, _fresh_engine
) -> None:
    """The refusal, and the property that makes it safe to refuse at all.

    `enforce_context_limit` runs after `load_history` and before the persist, so a refused
    message leaves NO turn row and NO usage row. Move the gate below the persist and this goes
    red on the counts rather than on the status — which is the failure that matters, because a
    half-recorded turn is a transcript that disagrees with what the citizen saw."""
    user, _project, conversation = await _a_conversation(db_session)
    await _stuff_the_conversation(db_session, user, conversation, tokens=DEFAULT_CONTEXT_HARD)
    rows_before = await db_session.scalar(
        select(func.count()).select_from(Message).where(Message.conversation_id == conversation.id)
    )

    resp = await _send(client, user, conversation.id)

    assert resp.status_code == 413
    body = resp.json()
    assert body["error"]["code"] == CHAT_TOO_LONG_CODE
    assert body["error"]["message"] == CHAT_TOO_LONG_TEXT

    rows_after = await db_session.scalar(
        select(func.count()).select_from(Message).where(Message.conversation_id == conversation.id)
    )
    assert rows_after == rows_before
    usage = await db_session.scalar(
        select(func.count()).select_from(TokenUsage).where(TokenUsage.user_id == user.id)
    )
    assert usage == 0
    # Nothing was claimed either — a refused turn must leave the conversation sendable.
    assert _fresh_engine.peek(conversation.id) is None


async def test_the_refusal_names_the_way_out(client, db_session) -> None:
    """The sentence a citizen reads is written on the SERVER and rendered verbatim, so this
    is where the property is pinned. Two facts, both load-bearing: what to do (start a new
    chat) and that the app survives it — without the second, "too long" reads as "lost"."""
    user, _project, conversation = await _a_conversation(db_session)
    await _stuff_the_conversation(db_session, user, conversation, tokens=DEFAULT_CONTEXT_HARD)

    message = (await _send(client, user, conversation.id)).json()["error"]["message"]

    assert "new chat" in message
    assert "stays exactly as it is" in message
    assert str(DEFAULT_CONTEXT_HARD) not in message  # never quotes the number
    assert "200,000" not in message


# --- the administrator's number is the boundary — the whole point of the unit ----------


async def test_an_administrator_override_changes_what_the_platform_accepts(
    client, db_session, _fresh_engine
) -> None:
    """★ THE TEST THAT PROVES THE ADMIN FIELD IS NO LONGER A LIE.

    One size, two users: the default limit sends it, a per-user hard limit set below that
    size refuses it, and nothing else differs — the number is the only thing that changed.
    Without this, a gate hard-wired to `DEFAULT_CONTEXT_HARD` would pass every other test
    here and leave `UsersLimitsPanel.tsx`'s "Hard stop" hint exactly as false as it was."""
    size = 40_000

    allowed_user, _p1, allowed_conv = await _a_conversation(db_session)
    await _stuff_the_conversation(db_session, allowed_user, allowed_conv, tokens=size)
    assert (await _send(client, allowed_user, allowed_conv.id)).status_code == 202
    await _settle(_fresh_engine, allowed_conv.id)

    capped_user, _p2, capped_conv = await _a_conversation(db_session)
    await _stuff_the_conversation(db_session, capped_user, capped_conv, tokens=size)
    db_session.add(UserLimit(user_id=capped_user.id, context_hard_limit=size // 2))
    await db_session.commit()

    refused = await _send(client, capped_user, capped_conv.id)

    assert refused.status_code == 413
    assert refused.json()["error"]["code"] == CHAT_TOO_LONG_CODE


async def test_an_override_above_the_model_window_is_clamped_not_honoured(
    client, db_session
) -> None:
    """`effective_context` caps a hard limit at the model's real window. An administrator cannot
    raise a chat past what the model can actually read, which is what the admin field's own
    "Between 16,000 and 1,000,000 (model window)" hint promises.

    THE CONVERSATION IS MEASURED AT THE WINDOW ITSELF, not at the default ceiling. Ten million
    would be honoured by a gate with no clamp, so the refusal here is the clamp and nothing
    else; and once the ceiling rose to 500,000, a conversation stuffed to the default would be
    admitted under the clamped limit and prove nothing."""
    user, _project, conversation = await _a_conversation(db_session)
    await _stuff_the_conversation(db_session, user, conversation, tokens=MODEL_CONTEXT_WINDOW)
    db_session.add(UserLimit(user_id=user.id, context_hard_limit=10_000_000))
    await db_session.commit()

    assert (await _send(client, user, conversation.id)).status_code == 413


async def test_a_user_with_no_override_is_governed_by_the_default(client, db_session) -> None:
    user, _project, conversation = await _a_conversation(db_session)
    existing = await db_session.scalar(
        select(func.count()).select_from(UserLimit).where(UserLimit.user_id == user.id)
    )
    assert existing == 0
    # Just past the default. Nothing is held back any more: the provider's count is of the
    # whole prompt, system segment and tool schemas included, so the ceiling is compared
    # against it directly.
    await _stuff_the_conversation(db_session, user, conversation, tokens=DEFAULT_CONTEXT_HARD)

    assert (await _send(client, user, conversation.id)).status_code == 413


# --- the second door --------------------------------------------------------------------


async def test_pressing_build_from_a_long_plan_chat_is_not_refused(
    client, app, db_session, _fresh_engine
) -> None:
    """★ THE SECOND DOOR, AND THE TRAP THAT WOULD HAVE BEEN BUILT UNDER IT.

    `build_it` HAS NO PER-CONVERSATION CONTEXT PREFLIGHT ANY MORE, and its deletion is what this
    test now guards. The call it used to make measured `history=[]` — a build chat is created
    empty, so it admitted every press it ever saw — and it could not have fired even fed the
    plan, which is capped at 64,000 characters against a ceiling of 500,000. It was an inert
    guard, and an inert guard reads to the next maintainer as a live one.

    WHAT THIS ASSERTS IS THE MISTAKE THE OBVIOUS REPAIR MAKES. Faced with a preflight that can
    never refuse here, the tempting fix is to measure the SOURCE plan chat, which is right there
    on the route. That would refuse "Build this plan" for exactly the citizens who planned
    longest — and send them to start a new chat, which is where the plan they are trying to build
    lives. The plan chat below is measured past the ceiling and the press still goes through,
    because a build chat is judged on its own history and it has none.

    Mutation check: reinstate the preflight over `rows`' history and this goes red. The bound
    that does hold on this door is asserted directly, below."""
    user, _project, plan_chat = await _a_conversation(db_session)
    app.dependency_overrides[chat_model_dep] = lambda: _offering_model()
    assert (await _send(client, user, plan_chat.id, "plan the visitors app")).status_code == 202
    await _settle(_fresh_engine, plan_chat.id)
    # The longest-planning citizen there is: this chat is past the ceiling that would refuse its
    # next message. It must not be past the ceiling for pressing Build.
    await _stuff_the_conversation(db_session, user, plan_chat, tokens=DEFAULT_CONTEXT_HARD)
    assert (await _send(client, user, plan_chat.id)).status_code == 413

    resp = await client.post(
        f"/v1/conversations/{plan_chat.id}/plan-options/opt-build/build",
        headers=_headers(user),
        json={"chatId": str(uuid.uuid7())},
    )

    assert resp.status_code == 200, resp.text


async def test_the_build_door_still_refuses_a_plan_that_is_too_long_to_build_from(
    client, db_session, _fresh_engine
) -> None:
    """★ ERROR PATH, AND THE REASON THE DELETED PREFLIGHT WAS NOT MISSED.

    The bound on this door was never the context ceiling — it is the plan's own. `plan_from_call`
    refuses an offer whose plan is past `MAX_MESSAGE_TEXT_CHARS`, REFUSED and never trimmed,
    with copy that asks for a SHORTER PLAN. That is the remedy that works here; "start a new
    chat" is not, because the plan being built lives in the chat the citizen would be leaving.

    This is the preflight's test, rewritten against the refusal that actually exists rather than
    deleted with the call — so the bound on this door stays guarded. The card is seeded
    directly because the engine now refuses to RECORD an over-ceiling offer at write time; this
    is the defence in depth behind that, for a row written before it existed.

    Mutation check: drop the `len(plan) > MAX_MESSAGE_TEXT_CHARS` arm from `plan_from_call` and
    this goes red — and nothing else on this route would notice."""
    user, _project, plan_chat = await _a_conversation(db_session)
    huge = "x" * (MAX_MESSAGE_TEXT_CHARS + 1)
    await append_batch(
        db_session,
        user_id=user.id,
        conversation_id=plan_chat.id,
        messages=[
            ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="present_plan_options",
                        args=json.dumps({"plan": huge}),
                        tool_call_id="opt-huge",
                    )
                ]
            )
        ],
        entry_kind=MessageEntryKind.TURN,
        kind=plan_chat.kind,
        meta={"kind": PENDING_META_KIND, "toolCallId": "opt-huge"},
    )
    await db_session.commit()

    minted = uuid.uuid7()
    resp = await client.post(
        f"/v1/conversations/{plan_chat.id}/plan-options/opt-huge/build",
        headers=_headers(user),
        json={"chatId": str(minted)},
    )

    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == PLAN_TOO_LONG_CODE
    # REFUSED, not truncated, and nothing started: no build chat exists for the minted id.
    assert await db_session.get(Conversation, minted) is None


# --- the contract the browser reads ------------------------------------------------------


def test_the_refusal_is_documented_where_it_can_actually_happen() -> None:
    """A refusal nothing documents is one a client is never told to expect — and an advertised
    refusal a route cannot produce is the opposite mistake, which is the one this now guards.

    The send route keeps its 413: that door reads a real measurement and really does refuse. The
    build door's 413 documented the preflight that is deleted, so it advertised a status no
    request could ever get back. Both halves are asserted, because dropping the second assertion
    rather than inverting it would leave the undeliverable status free to come back."""
    paths = create_app().openapi()["paths"]
    send = paths["/v1/conversations/{conversation_id}/turns"]["post"]
    build = paths["/v1/conversations/{conversation_id}/plan-options/{tool_call_id}/build"]["post"]
    assert "413" in send["responses"]
    assert "413" not in build["responses"]
    # LIVENESS: the build route is really in the document, so the absence above is an absence
    # and not a path this test failed to find. Its own refusals are still advertised.
    assert "400" in build["responses"]


def test_the_code_is_byte_stable() -> None:
    """Nothing in the refusal path is exhaustive — no `Literal` union, no native enum, no
    `assertNever`. Every code is an open string, so a rename is free, silent, and still
    compiles. This is the guard that notices."""
    assert CHAT_TOO_LONG_CODE == "context_hard_limit_exceeded"


async def test_a_refused_turn_does_not_burn_a_pending_plan_card(
    client, app, db_session, _fresh_engine
) -> None:
    """★ THE ORDERING TRAP: `start_turn` resolves a pending plan-options card as an implicit
    "keep refining" once free text passes it — a WRITE the rollback does not cover, because
    `resolve_pending_as_refine` reaches `append_batch`, which owns its own commit. A refusal
    raised after that write leaves the card resolved on disk and the offer silently burned,
    with nothing on screen saying so. The gate sits ABOVE that write for exactly this reason,
    and the history is re-read afterwards only when the resolve actually wrote something.

    Mutation check: move `enforce_context_limit` back below `resolve_pending_as_refine` and
    this goes red while every other test in this file stays green."""
    user, _project, conversation = await _a_conversation(db_session)
    app.dependency_overrides[chat_model_dep] = lambda: _offering_model()
    assert (await _send(client, user, conversation.id, "plan it")).status_code == 202
    await _settle(_fresh_engine, conversation.id)
    assert await find_pending(db_session, user_id=user.id, conversation_id=conversation.id), (
        "the offer has to be pending, or this test asserts nothing"
    )

    await _stuff_the_conversation(db_session, user, conversation, tokens=DEFAULT_CONTEXT_HARD)

    refused = await _send(client, user, conversation.id, "actually, more like this")
    assert refused.status_code == 413
    assert refused.json()["error"]["code"] == CHAT_TOO_LONG_CODE

    # The card the citizen can still press.
    still_pending = await find_pending(
        db_session, user_id=user.id, conversation_id=conversation.id
    )
    assert still_pending is not None


async def test_an_accepted_turn_still_resolves_a_pending_card(
    client, app, db_session, _fresh_engine
) -> None:
    """The other half, so the fix above cannot be "never resolve anything": free text past a
    pending card, in a conversation comfortably under the limit, still resolves the card."""
    user, _project, conversation = await _a_conversation(db_session)
    app.dependency_overrides[chat_model_dep] = lambda: _offering_model()
    assert (await _send(client, user, conversation.id, "plan it")).status_code == 202
    await _settle(_fresh_engine, conversation.id)
    assert await find_pending(db_session, user_id=user.id, conversation_id=conversation.id)

    app.dependency_overrides[chat_model_dep] = lambda: _plain_model()
    assert (await _send(client, user, conversation.id, "more like this")).status_code == 202
    await _settle(_fresh_engine, conversation.id)

    assert await find_pending(db_session, user_id=user.id, conversation_id=conversation.id) is None


# --- documents — what an attachment costs on the way IN, which is now nothing -----------
#
# The platform no longer prices a document at admission. It used to charge a flat nominal per
# attachment and refuse against the total, and that charge was wrong by 47x in the direction
# that hurts; the window check now reads what the provider reported for a turn it served.
#
# TWO BOUNDS SURVIVE ON THE WAY IN, and both are COUNTS rather than derived token figures — the
# only kind of bound that can act before the provider has seen anything. The upload route
# refuses a document over 30 pages (`test_attachments.py`); the send route refuses a third
# document on one message. What shows up HERE is what they do to a real send.


@pytest.fixture
def shared_storage(fake_storage, monkeypatch):
    """ONE store for both consumers of it. The upload route takes its store by injected
    dependency; the send route's rehydrator reaches the accessor-level `get_storage()`. The
    directory fixture binds only the first, so a test that uploads and then SENDS the upload
    needs the accessor bound to the same object or the send answers 503 and proves nothing."""
    from src.services.storage import accessor

    monkeypatch.setattr(accessor, "_backend_singleton", fake_storage)
    return fake_storage


async def _upload(
    client, user, conversation_id: uuid.UUID, attachment_id: str, media_type: str, data: bytes
) -> None:
    """Upload one file AGAINST a conversation, which the door now requires.

    The id was always available at this call site; what changed is that the server wants it, so a
    file's owner is knowable at the door rather than stamped on afterwards by the send route.
    """
    resp = await client.post(
        "/v1/attachments",
        headers=_headers(user),
        json={
            "conversationId": str(conversation_id),
            "attachmentId": attachment_id,
            "mediaType": media_type,
            "base64": base64.b64encode(data).decode(),
            "name": f"{attachment_id}.bin",
        },
    )
    assert resp.status_code == 201, resp.text


async def _send_with(client, user, conversation_id: uuid.UUID, ids: list[str], text="here"):
    return await client.post(
        f"/v1/conversations/{conversation_id}/turns",
        headers=_headers(user),
        json={"message": {"text": text, "attachmentTexts": [], "attachmentIds": ids}},
    )


async def test_five_files_of_any_mix_send_and_the_sixth_is_refused(
    client, db_session, shared_storage, _fresh_engine
) -> None:
    """★ ONE NUMBER GOVERNS EVERY ATTACHMENT.

    This replaces three tests built on the per-DOCUMENT cap of two, which is removed: a citizen
    attaching five files should not have to know which of them the platform files as expensive.
    The page cap and the flat charge are what actually protect the budget and both stay; the
    document count was the belt beside those braces.

    THE MIX IS THE POINT. Five files here are images and text blocks together, because the two
    lists used to be bounded separately at eight apiece — sixteen files on one message against a
    composer that offers five. Counting them apart is what made the stricter number the one that
    was not the trust boundary.

    Mutation receipt: restore either per-list bound in place of the sum and the sixth file sends.
    """
    user, _project, conversation = await _a_conversation(db_session)
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
    for index in range(4):
        await _upload(client, user, conversation.id, f"mix_{index}", "image/png", png)

    at_the_cap = await client.post(
        f"/v1/conversations/{conversation.id}/turns",
        headers=_headers(user),
        json={
            "message": {
                "text": "five files",
                "attachmentTexts": ['<attachment name="a.csv" type="text">x</attachment>'],
                "attachmentIds": [f"mix_{index}" for index in range(4)],
            }
        },
    )
    assert at_the_cap.status_code == 202, at_the_cap.text
    await _settle(_fresh_engine, conversation.id)

    await _upload(client, user, conversation.id, "mix_4", "image/png", png)
    over = await client.post(
        f"/v1/conversations/{conversation.id}/turns",
        headers=_headers(user),
        json={
            "message": {
                "text": "six files",
                "attachmentTexts": ['<attachment name="a.csv" type="text">x</attachment>'],
                "attachmentIds": [f"mix_{index}" for index in range(5)],
            }
        },
    )
    assert over.status_code == 422, over.text


def test_the_server_file_count_is_the_composer_s_number() -> None:
    """AE19 — the composer's limit and the server's are the same number.

    They were not: the composer offered five and the server admitted eight, so the stricter of
    the two was the one that is not the trust boundary. The portal mirror is
    `MAX_FILES_PER_MESSAGE` in `portal/src/utils/attachmentInput.ts`; this asserts the value it
    has to agree with, so a change here without a change there fails on a stated number rather
    than in a browser."""
    assert MAX_FILES_PER_MESSAGE == 5
    assert MAX_ATTACHMENT_BLOCKS == MAX_FILES_PER_MESSAGE


async def test_uploading_a_document_computes_and_stores_no_token_figure(
    client, db_session, shared_storage, _fresh_engine
) -> None:
    """★ THE TEST THAT REPLACED A REFUSAL THIS ROUTE NO LONGER MAKES.

    Documents used to be charged a flat 75,000 tokens on the way in, replacing an earlier guess
    of 1,600 for the same file — so the property to pin now is the one that is true today: a
    thirty-page document, the longest the upload route admits, is uploaded and then SENT on a
    conversation the provider has already reported at 60,000 tokens. Nothing derives a figure
    for it, nothing stores one, and the turn starts. The daily-usage ledger is the place a token
    figure would land if anything computed one, and it is empty until the provider has served
    the turn and said what it cost.

    THE CONTROL IS THE LOAD-BEARING HALF: the same conversation, once the provider HAS reported
    it past the ceiling, is refused. Without it this test passes against a gate wired to admit
    anything at all."""
    user, _project, conversation = await _a_conversation(db_session)
    await _stuff_the_conversation(db_session, user, conversation, tokens=60_000)
    await _upload(client, user, conversation.id, "spec", "application/pdf", pdf_with_pages(30))

    # Nothing about an upload writes usage — no charge is computed at admission any more.
    assert (
        await db_session.scalar(
            select(func.count()).select_from(TokenUsage).where(TokenUsage.user_id == user.id)
        )
        == 0
    )

    resp = await _send_with(client, user, conversation.id, ["spec"], text="read this")

    assert resp.status_code == 202, resp.text
    await _settle(_fresh_engine, conversation.id)

    # CONTROL: the same conversation, once the provider has reported it past the ceiling, is
    # refused — so the 202 above is a measurement admitting it, not a gate that admits anything.
    await _stuff_the_conversation(db_session, user, conversation, tokens=DEFAULT_CONTEXT_HARD)
    refused = await _send(client, user, conversation.id)
    assert refused.status_code == 413, refused.text
    assert refused.json()["error"]["code"] == CHAT_TOO_LONG_CODE
    assert refused.json()["error"]["message"] == CHAT_TOO_LONG_TEXT
