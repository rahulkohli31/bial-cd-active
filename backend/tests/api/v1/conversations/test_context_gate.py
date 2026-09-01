"""The per-conversation guardrail, at the routes that enforce it.

★ THIS IS THE FILE WHOSE ABSENCE LET THE REGRESSION THROUGH. The old client-side guardrail died
with `ChatPage.tsx` in #170 and nothing turned red, because the only tests that covered it were
deleted in the same commit. Meanwhile an administrator had been setting a number in a field
whose help text promised a hard stop, and no call site anywhere — front or back — read it.

Every test here is about the number MEANING something. A gate that refused everything would
satisfy half of them, which is why the first one exists.
"""

from __future__ import annotations

import asyncio
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
    UserPromptPart,
)
from pydantic_ai.models.function import (
    AgentInfo,
    DeltaToolCall,
    DeltaToolCalls,
    FunctionModel,
)
from sqlalchemy import func, select

from src.api.v1.conversations._shared import chat_model as chat_model_dep
from src.config import settings
from src.db.models.conversation import ChatKind
from src.db.models.message import Message, MessageEntryKind
from src.db.models.token_usage import TokenUsage
from src.db.models.user_limit import UserLimit
from src.main import create_app
from src.services.auth.csrf import issue_csrf_token
from src.services.auth.session_jwt import mint_session_jwt
from src.services.messages.store import append_batch
from src.services.turns.copy import CHAT_TOO_LONG_CODE, CHAT_TOO_LONG_TEXT
from src.services.turns.engine import TurnEngine, set_turn_engine_for_tests
from src.services.turns.guard import _mid_reply
from src.services.usage.context_window import SYSTEM_PROMPT_RESERVE
from src.services.usage.limits import DEFAULT_CONTEXT_HARD
from tests.factories import ConversationFactory, ProjectFactory, UserFactory

_TTL = settings.auth.access_ttl_seconds


@pytest.fixture(autouse=True)
def _fresh_engine():
    _mid_reply.clear()
    engine = TurnEngine()
    set_turn_engine_for_tests(engine)
    yield engine
    set_turn_engine_for_tests(None)
    _mid_reply.clear()


@pytest.fixture(autouse=True)
def _override_billing(app, db_session) -> None:
    from src.api.v1.conversations._shared import billing_session_factory

    @contextlib.asynccontextmanager
    async def _session():
        yield db_session

    app.dependency_overrides[billing_session_factory] = lambda: lambda: _session()


@pytest.fixture(autouse=True)
def _a_model(app) -> None:
    """Every test here should be decided by the GUARDRAIL, never by a missing model. Bound for
    all of them so a 503 can never be mistaken for a refusal that worked."""
    from src.api.v1.conversations._shared import chat_model

    async def _stream(_messages: list[ModelMessage], _info: AgentInfo):
        yield "ok"

    app.dependency_overrides[chat_model] = lambda: FunctionModel(stream_function=_stream)


def _headers(user) -> dict[str, str]:
    jwt = mint_session_jwt(user.id, user.token_version, _TTL)
    csrf = issue_csrf_token(user.id, user.token_version)
    return {"Cookie": f"session={jwt}; csrf={csrf}", "X-CSRF-Token": csrf}


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
    """Persist a conversation that MEASURES at roughly `tokens`, through the real store — not a
    stub of it. The gate reads what `load_history` returns, so a history assembled any other way
    would prove the measurement and not the wiring."""
    chars = tokens * 4
    await append_batch(
        db_session,
        user_id=user.id,
        conversation_id=conversation.id,
        messages=[
            ModelRequest(parts=[UserPromptPart(content="q" * (chars // 2))]),
            ModelResponse(parts=[TextPart(content="a" * (chars // 2))]),
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


async def _a_conversation(db_session, *, kind: ChatKind = ChatKind.PLAN):
    user = await UserFactory.create(db_session)
    project = await ProjectFactory.create(db_session, user.id)
    conversation = await ConversationFactory.create(
        db_session, user.id, project_id=project.id, kind=kind
    )
    return user, project, conversation


# =============================================================================
# The gate has to let ordinary conversations through
# =============================================================================


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


# =============================================================================
# Past the limit: refused, and nothing written
# =============================================================================


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
    """The sentence a citizen reads is written on the SERVER and rendered verbatim, so what it
    says is a server-side property and this is where it is pinned.

    Two facts, both load-bearing: what to do (start a new chat) and that the app survives it.
    Without the second, "this chat has got too long" reads as "you have lost your work"."""
    user, _project, conversation = await _a_conversation(db_session)
    await _stuff_the_conversation(db_session, user, conversation, tokens=DEFAULT_CONTEXT_HARD)

    message = (await _send(client, user, conversation.id)).json()["error"]["message"]

    assert "new chat" in message
    assert "stays exactly as it is" in message
    # And it does NOT do the thing the old admin copy implied it would: quote the number.
    assert str(DEFAULT_CONTEXT_HARD) not in message
    assert "200,000" not in message


# =============================================================================
# The administrator's number is the boundary — the whole point of the unit
# =============================================================================


async def test_an_administrator_override_changes_what_the_platform_accepts(
    client, db_session, _fresh_engine
) -> None:
    """★ THE TEST THAT PROVES THE ADMIN FIELD IS NO LONGER A LIE.

    One size, two users. Under the default limit the conversation sends. With a per-user hard
    limit set below that size it is refused. Nothing else differs — so the ONLY thing that can
    have changed the answer is the number an administrator typed.

    Without this test the unit has not done its job: a gate hard-wired to `DEFAULT_CONTEXT_HARD`
    would pass every other test in this file and leave `UsersLimitsPanel.tsx:193`'s "Hard stop
    for a single chat" exactly as false as it was."""
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
    "Max 200,000 (model window)" hint promises."""
    user, _project, conversation = await _a_conversation(db_session)
    await _stuff_the_conversation(db_session, user, conversation, tokens=DEFAULT_CONTEXT_HARD)
    db_session.add(UserLimit(user_id=user.id, context_hard_limit=10_000_000))
    await db_session.commit()

    assert (await _send(client, user, conversation.id)).status_code == 413


async def test_a_user_with_no_override_is_governed_by_the_default(client, db_session) -> None:
    user, _project, conversation = await _a_conversation(db_session)
    existing = await db_session.scalar(
        select(func.count()).select_from(UserLimit).where(UserLimit.user_id == user.id)
    )
    assert existing == 0
    # Just past the default, allowing for the reserve the gate holds back.
    await _stuff_the_conversation(
        db_session, user, conversation, tokens=DEFAULT_CONTEXT_HARD - SYSTEM_PROMPT_RESERVE + 10
    )

    assert (await _send(client, user, conversation.id)).status_code == 413


# =============================================================================
# The second door (KTD-4)
# =============================================================================


async def test_the_same_refusal_fires_on_the_build_from_plan_path(
    client, app, db_session, _fresh_engine
) -> None:
    """★ THE MOST LIKELY IMPLEMENTATION MISTAKE, and the reason the preflight is one function.

    `build_it` is the second route that starts a conversation turn. Wire the guardrail to the
    send route alone and pressing "Build this plan" walks straight past the administrator's
    number — the exact shape the daily cap's three hand-copied call sites are on record for.

    Driven with a hard limit below the reserve, so the plan's own size is not what is being
    asserted: what this pins is that the route consults the limit at all."""
    # A REAL pending offer, produced through the genuine engine path — the handoff refuses a
    # press with no card long before it reaches any limit, so the card has to be real for the
    # GATE to be what this test is about rather than the check above it.
    user, _project, plan_chat = await _a_conversation(db_session)
    app.dependency_overrides[chat_model_dep] = lambda: _offering_model()
    planned = await _send(client, user, plan_chat.id, "plan the visitors app")
    assert planned.status_code == 202, planned.text
    await _settle(_fresh_engine, plan_chat.id)

    # NOW the administrator's ceiling arrives — below anything, so the press is past it.
    db_session.add(UserLimit(user_id=user.id, context_hard_limit=1))
    await db_session.commit()
    rows_before = await db_session.scalar(
        select(func.count()).select_from(Message).where(Message.user_id == user.id)
    )

    resp = await client.post(
        f"/v1/conversations/{plan_chat.id}/plan-options/opt-build/build",
        headers=_headers(user),
        json={"chatId": str(uuid.uuid7())},
    )

    # Whatever else this press would have hit, it must not be allowed to start a build for a
    # user whose ceiling it is already past.
    assert resp.status_code == 413, resp.text
    assert resp.json()["error"]["code"] == CHAT_TOO_LONG_CODE
    # And no build chat was created — the offer row is all that is there.
    written = await db_session.scalar(
        select(func.count()).select_from(Message).where(Message.user_id == user.id)
    )
    assert written == rows_before


# =============================================================================
# The contract the browser reads
# =============================================================================


def test_the_refusal_is_documented_on_both_routes() -> None:
    """A refusal nothing documents is one a client is never told to expect. Both doors carry the
    same status, because a browser that had to learn two would learn one."""
    paths = create_app().openapi()["paths"]
    send = paths["/v1/conversations/{conversation_id}/turns"]["post"]
    build = paths["/v1/conversations/{conversation_id}/plan-options/{tool_call_id}/build"]["post"]
    assert "413" in send["responses"]
    assert "413" in build["responses"]


def test_the_code_is_byte_stable() -> None:
    """Nothing in the refusal path is exhaustive — no `Literal` union, no native enum, no
    `assertNever`. Every code is an open string compared by hand, so a rename is free and silent
    and every reader keeps compiling. This is the guard that notices."""
    assert CHAT_TOO_LONG_CODE == "context_hard_limit_exceeded"
