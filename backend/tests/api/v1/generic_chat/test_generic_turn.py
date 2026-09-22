"""A turn in a chat that has no project and no container, through the real route and engine.

WHAT THIS FILE IS FOR. The send route runs four refusals that each ask about a project's live
container, and until a third kind existed every one of them was correct for every turn. A generic
chat has no project, so each would be asking about something that does not exist — and the first,
"no workspace service", would refuse the turn outright on a deployment with no sandbox at all.

NOTHING HERE BINDS A SANDBOX. See this directory's `conftest.py`: the sibling suite binds one
autouse, which would make the central assertion of this file unfalsifiable.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid

import pytest
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models.function import AgentInfo, FunctionModel

from src.db.models.conversation import ChatKind
from src.services.orchestrator.constants import GENERIC_EFFORT
from tests.api.v1.conversations.conftest import _headers
from tests.factories import ConversationFactory, ProjectFactory, UserFactory

pytestmark = pytest.mark.usefixtures("_fresh_engine", "_override_billing")


def _streaming_text(*chunks: str) -> FunctionModel:
    async def _stream(_messages: list[ModelMessage], _info: AgentInfo):
        for chunk in chunks:
            yield chunk

    return FunctionModel(stream_function=_stream)


async def _generic_chat(db_session):
    user = await UserFactory.create(db_session)
    conversation = await ConversationFactory.create(
        db_session, user.id, kind=ChatKind.GENERIC, project_id=None
    )
    return user, conversation


async def _post_turn(client, headers, conversation, text="What does this document say?"):
    return await client.post(
        f"/v1/conversations/{conversation.id}/turns",
        headers=headers,
        json={"message": {"text": text, "attachmentTexts": [], "attachmentIds": []}},
    )


async def _settle(engine, conversation_id) -> None:
    state = engine.peek(conversation_id)
    assert state is not None and state.task is not None
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(state.task, timeout=10)


# --- the assertion the whole unit rests on --------------------------------------------


async def test_a_generic_turn_is_answered_with_no_workspace_service_at_all(
    client, db_session, set_chat_model, _fresh_engine
) -> None:
    """★ THE ONE THAT PROVES THE UNIT, and the one most likely to pass for the wrong reason.

    No sandbox client is bound on either seam, so `OptionalSandbox` resolves to `None` — which is
    the state a deployment with no sandbox service is in, and the state in which the route's first
    workspace refusal fires. A generic turn must pass straight through it.

    Mutation receipt: remove the guard around the workspace block in
    `api/v1/conversations/turns.py` and this goes red with a 503."""
    user, conversation = await _generic_chat(db_session)
    set_chat_model(_streaming_text("It is a maintenance schedule for stand 42."))

    resp = await _post_turn(client, _headers(user), conversation)

    assert resp.status_code == 202, resp.text
    await _settle(_fresh_engine, conversation.id)
    state = _fresh_engine.peek(conversation.id)
    assert state is not None
    assert state.status == "completed"
    # NO CONTAINER WAS STARTED. `sandbox` is set by the attach inside the workspace pin, and the
    # pin is what a generic turn must never reach.
    assert state.sandbox is None
    assert state.write_session is None


async def test_the_reply_names_neither_a_project_nor_an_application(
    client, db_session, set_chat_model, _fresh_engine
) -> None:
    """★ AE4. The prompt the run is actually handed carries the citizen's name and nothing about
    a project — so a generic turn cannot open by grounding itself in an app that does not exist."""
    user, conversation = await _generic_chat(db_session)
    seen: dict[str, str] = {}

    async def _capture(messages: list[ModelMessage], _info: AgentInfo):
        seen["instructions"] = "\n".join(
            part.content
            for message in messages
            for part in getattr(message, "instructions", None) or []
            if isinstance(getattr(part, "content", None), str)
        ) or str(getattr(messages[0], "instructions", ""))
        yield "Happy to help."

    set_chat_model(FunctionModel(stream_function=_capture))

    resp = await _post_turn(client, _headers(user), conversation)
    assert resp.status_code == 202, resp.text
    await _settle(_fresh_engine, conversation.id)

    instructions = seen["instructions"]
    assert "BIAL CHAT" in instructions
    assert "PLAN MODE" not in instructions
    assert "WRITE MODE" not in instructions
    assert "You work inside" not in instructions
    assert "this conversation lives inside one of the user's projects" not in instructions


async def test_a_generic_turn_holds_no_workspace_while_another_project_is_using_it(
    client, db_session, set_chat_model, _fresh_engine, building
) -> None:
    """★ AE4's other half: the citizen's one workspace is already committed to another project,
    and a generic turn is answered anyway because it never asks for one."""
    user, conversation = await _generic_chat(db_session)
    set_chat_model(_streaming_text("Answered."))

    with building(user.id):
        resp = await _post_turn(client, _headers(user), conversation)

    assert resp.status_code == 202, resp.text
    await _settle(_fresh_engine, conversation.id)
    assert _fresh_engine.peek(conversation.id).status == "completed"


# --- what a generic turn keeps ---------------------------------------------------------


async def test_a_generic_turn_over_the_daily_cap_is_still_refused(
    client, db_session, set_chat_model
) -> None:
    """The cap is SHARED, which is what makes this refusal the right one to keep: spend recorded
    anywhere reaches every kind."""
    from src.db.models.user_limit import UserLimit
    from src.services.usage.gate import record_usage

    user, conversation = await _generic_chat(db_session)
    set_chat_model(_streaming_text("never reached"))
    db_session.add(UserLimit(user_id=user.id, daily_token_limit=10))
    await db_session.flush()
    await record_usage(db_session, user.id, input_tokens=10, output_tokens=0)

    resp = await _post_turn(client, _headers(user), conversation)

    assert resp.status_code == 429, resp.text
    assert resp.json()["error"]["code"] == "daily_token_limit_exceeded"


async def test_a_generic_turn_with_no_model_configured_is_still_refused(
    client, db_session
) -> None:
    """The model seam is unbound here, which is what a deployment with no Claude client looks
    like — the one 503 a generic turn keeps."""
    user, conversation = await _generic_chat(db_session)

    resp = await _post_turn(client, _headers(user), conversation)

    assert resp.status_code == 503, resp.text
    assert "Claude client not configured" in resp.text


async def test_a_second_generic_turn_while_one_is_mid_reply_is_refused(
    client, db_session, set_chat_model, _fresh_engine
) -> None:
    """Pure liveness — what the chat IS never enters it, which is why this refusal survived the
    move out of the workspace block."""
    gate = asyncio.Event()

    async def _paced(_messages: list[ModelMessage], _info: AgentInfo):
        yield "thinking "
        await gate.wait()
        yield "done"

    user, conversation = await _generic_chat(db_session)
    set_chat_model(FunctionModel(stream_function=_paced))
    headers = _headers(user)

    first = await _post_turn(client, headers, conversation)
    assert first.status_code == 202, first.text
    second = await _post_turn(client, headers, conversation, text="and another")
    assert second.status_code == 409, second.text

    gate.set()
    await _settle(_fresh_engine, conversation.id)


# --- the engine's own decisions ---------------------------------------------------------


async def test_a_generic_run_asks_for_the_lowest_effort(
    client, db_session, set_chat_model, _fresh_engine
) -> None:
    """Mutation receipt: point `_effort_for`'s generic arm at `PLAN_EFFORT` and this goes red
    while the plan and build arms stay green."""
    user, conversation = await _generic_chat(db_session)
    seen: dict[str, object] = {}

    async def _capture(_messages: list[ModelMessage], info: AgentInfo):
        seen["settings"] = info.model_settings
        yield "ok"

    set_chat_model(FunctionModel(stream_function=_capture))

    await _post_turn(client, _headers(user), conversation)
    await _settle(_fresh_engine, conversation.id)

    settings = seen["settings"]
    assert isinstance(settings, dict)
    assert settings["anthropic_effort"] == GENERIC_EFFORT
    assert GENERIC_EFFORT == "low"
    # Thinking stays ON at this level — see the constant's own note; switching it off is a
    # measurable change that has not been measured.
    assert settings["anthropic_thinking"]["type"] == "adaptive"


async def test_a_generic_run_is_handed_no_tools_at_all(
    client, db_session, set_chat_model, _fresh_engine
) -> None:
    user, conversation = await _generic_chat(db_session)
    seen: dict[str, object] = {}

    async def _capture(_messages: list[ModelMessage], info: AgentInfo):
        seen["tools"] = [tool.name for tool in info.function_tools]
        yield "ok"

    set_chat_model(FunctionModel(stream_function=_capture))

    await _post_turn(client, _headers(user), conversation)
    await _settle(_fresh_engine, conversation.id)

    assert seen["tools"] == []


async def test_reasoning_is_never_projected_into_the_transcript(
    client, db_session, set_chat_model, _fresh_engine
) -> None:
    """R19's display half. The projection is kind-blind and already drops reasoning for the other
    two kinds; this asks the question of the kind that has just joined them."""
    user, conversation = await _generic_chat(db_session)
    set_chat_model(_streaming_text("The schedule covers June."))

    await _post_turn(client, _headers(user), conversation)
    await _settle(_fresh_engine, conversation.id)

    detail = await client.get(f"/v1/conversations/{conversation.id}", headers=_headers(user))
    assert detail.status_code == 200, detail.text
    items = detail.json()["projection"]
    # The prose and the turn's terminal, and nothing between them: no reasoning item exists in
    # the projection's vocabulary, and this is the kind newly able to produce one.
    assert [item["type"] for item in items] == ["user_text", "assistant_text", "turn_terminal"]
    assert items[1]["text"] == "The schedule covers June."


# --- the two kinds that keep every refusal ----------------------------------------------


@pytest.mark.parametrize("kind", [ChatKind.PLAN, ChatKind.BUILD])
async def test_a_project_bearing_turn_is_still_refused_with_no_workspace_service(
    client, db_session, set_chat_model, kind: ChatKind
) -> None:
    """★ THE OTHER DIRECTION, and the one that makes the guard above a branch rather than a
    deletion: with no sandbox bound, a plan or build turn must still be refused."""
    user = await UserFactory.create(db_session)
    project = await ProjectFactory.create(db_session, user.id)
    conversation = await ConversationFactory.create(
        db_session, user.id, kind=kind, project_id=project.id
    )
    set_chat_model(_streaming_text("never reached"))

    resp = await client.post(
        f"/v1/conversations/{conversation.id}/turns",
        headers=_headers(user),
        json={"message": {"text": "hello", "attachmentTexts": [], "attachmentIds": []}},
    )

    assert resp.status_code == 503, resp.text
    assert "workspace" in resp.text.lower()


async def test_an_unknown_conversation_is_still_a_non_leaking_404(client, db_session) -> None:
    user = await UserFactory.create(db_session)

    resp = await client.post(
        f"/v1/conversations/{uuid.uuid4()}/turns",
        headers=_headers(user),
        json={"message": {"text": "hello", "attachmentTexts": [], "attachmentIds": []}},
    )

    assert resp.status_code == 404, resp.text
