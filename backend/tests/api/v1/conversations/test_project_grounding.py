"""The project's description grounds every turn, and only its owner's turns (R16, ADR-0004).

These three moved here from `tests/api/v1/projects/` when the legacy `POST /v1/claude` relay
was retired. They pinned the relay's `_compose_system`; the property they were really about —
"a turn is grounded in the project's own description, and a project's description never
reaches another user's turn" — belongs to whichever surface actually sends, so they now assert
it through `POST /v1/conversations/{id}/turns` and the real prompt composition
(`services/agent/mode_prompts.compose_kind_prompt`).

Deliberately at the ROUTE, not at `compose_kind_prompt`: `test_mode_prompts.py` already proves
the composer puts a supplied description into the text. What has no other test is the WIRING —
`turns.py` reading `project.description` off the owner-scoped row and handing it to that
composer. A dropped predicate or a dropped field there is a cross-user leak that a
composer-level test is structurally unable to see.
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from typing import Any

import pytest
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models.function import AgentInfo, FunctionModel

from src.db.models.conversation import ChatKind
from tests.api.v1.conversations.conftest import _headers
from tests.factories import ConversationFactory, ProjectFactory, UserFactory

# The turn-driving fixtures live in `conftest.py` — four files needed the same four, and
# two of them were the 3rd and 4th copy. Named here rather than autouse there, because the
# other files in this directory drive no turns.
pytestmark = pytest.mark.usefixtures("_fresh_engine", "_override_billing")


def _capturing_model() -> tuple[FunctionModel, dict[str, str]]:
    """A streaming FunctionModel that records the instructions the model actually received —
    the system prompt is composed per run, so this is the only place it is observable."""
    captured: dict[str, str] = {}

    async def _stream(_messages: list[ModelMessage], info: AgentInfo):
        captured["instructions"] = info.instructions or ""
        yield "streamed"

    return FunctionModel(stream_function=_stream), captured


async def _send(client: Any, headers: dict[str, str], conversation_id: uuid.UUID) -> Any:
    return await client.post(
        f"/v1/conversations/{conversation_id}/turns",
        headers=headers,
        json={"message": {"text": "hi", "attachmentTexts": [], "attachmentIds": []}},
    )


async def _settle(engine: Any, conversation_id: uuid.UUID) -> None:
    state = engine.peek(conversation_id)
    assert state is not None and state.task is not None
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(state.task, timeout=10)


async def test_project_description_reaches_the_model(
    client, db_session, set_chat_model, _fresh_engine
) -> None:
    model, captured = _capturing_model()
    set_chat_model(model)
    user = await UserFactory.create(db_session)
    project = await ProjectFactory.create(
        db_session, user.id, name="VIP", description="Tracks VIP movements at BIAL."
    )
    conv = await ConversationFactory.create(
        db_session, user.id, project_id=project.id, kind=ChatKind.PLAN
    )

    assert (await _send(client, _headers(user), conv.id)).status_code == 202
    await _settle(_fresh_engine, conv.id)

    assert "Tracks VIP movements at BIAL." in captured["instructions"]


async def test_a_null_description_grounds_the_turn_without_inventing_one(
    client, db_session, set_chat_model, _fresh_engine
) -> None:
    """The negative half, so the test above cannot be satisfied by a prompt that mentions
    every project ever: with no description written, the project's NAME still grounds the
    turn and no description clause appears."""
    model, captured = _capturing_model()
    set_chat_model(model)
    user = await UserFactory.create(db_session)
    project = await ProjectFactory.create(db_session, user.id, name="Untold")  # NULL description
    conv = await ConversationFactory.create(
        db_session, user.id, project_id=project.id, kind=ChatKind.PLAN
    )

    assert (await _send(client, _headers(user), conv.id)).status_code == 202
    await _settle(_fresh_engine, conv.id)

    assert '"Untold"' in captured["instructions"]
    # `_base` renders a described project as `on "name" — description`; with no description
    # there is no em-dash clause hanging off the name. The QUOTES are load-bearing in this
    # assertion — matching on the bare name lets an unconditional `f" — {None}"` through.
    assert '"Untold" —' not in captured["instructions"]


async def test_two_conversations_share_the_same_description(
    client, db_session, set_chat_model, _fresh_engine
) -> None:
    """R16: the description is the PROJECT's, not one chat's — a second conversation in the
    same project is grounded identically without anyone re-stating it."""
    user = await UserFactory.create(db_session)
    project = await ProjectFactory.create(
        db_session, user.id, description="Shared grounding text."
    )
    seen: list[str] = []
    for _ in range(2):
        conv = await ConversationFactory.create(
            db_session, user.id, project_id=project.id, kind=ChatKind.PLAN
        )
        model, captured = _capturing_model()
        set_chat_model(model)
        assert (await _send(client, _headers(user), conv.id)).status_code == 202
        await _settle(_fresh_engine, conv.id)
        seen.append(captured["instructions"])

    assert len(seen) == 2
    assert all("Shared grounding text." in instructions for instructions in seen)


async def test_cross_user_cannot_ground_a_turn_in_another_users_project(
    client, db_session, set_chat_model, _fresh_engine
) -> None:
    """`turns.py`'s owner-scoped conversation lookup is what stops user B grounding a turn in
    user A's project (ADR-0004).

    The owner's OWN turn is asserted FIRST — otherwise the leak assertion below could pass
    simply because nothing ever injects a description, which is the vacuity trap the relay
    version of this test was written to escape and which survives the move.
    """
    owner = await UserFactory.create(db_session)
    project = await ProjectFactory.create(
        db_session, owner.id, description="OWNER_SECRET_DESCRIPTION"
    )
    conv_a = await ConversationFactory.create(
        db_session, owner.id, project_id=project.id, kind=ChatKind.BUILD
    )

    model, captured = _capturing_model()
    set_chat_model(model)
    assert (await _send(client, _headers(owner), conv_a.id)).status_code == 202
    await _settle(_fresh_engine, conv_a.id)
    assert "OWNER_SECRET_DESCRIPTION" in captured["instructions"]

    # User B references A's conversation id → the owner-scoped lookup misses → a non-leaking
    # 404, and the model is never reached at all.
    intruder = await UserFactory.create(db_session, email="intruder@rvaiglobal.com")
    model, captured = _capturing_model()
    set_chat_model(model)
    resp = await _send(client, _headers(intruder), conv_a.id)
    assert resp.status_code == 404
    assert resp.json() == {"error": {"message": "Conversation not found."}}
    assert "OWNER_SECRET_DESCRIPTION" not in captured.get("instructions", "")
