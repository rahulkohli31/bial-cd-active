"""Journey: multi-turn generate → iterate on the stateless Claude relay (`POST /v1/claude`).

Covers the UNTESTED `_split_messages` / multi-turn reconstruction path
(`src/api/v1/claude/router.py:_split_messages`, `:_stream`).

* Turn 1 — initial generation: a single user message ("build a gate tracker") streams back as
  SSE, asserting the exact wire contract the SPA parses (`data: {"delta":{"text":…}}` frames +
  a terminal `data: [DONE]`).
* Turn 2 — refine: a 3-message transcript ``[user, assistant(prior), user(refine)]`` is posted,
  and we assert the agent received the message_history reconstructed by `_split_messages` — the
  prior USER turn replayed as a `ModelRequest(UserPromptPart)`, the prior ASSISTANT turn as a
  `ModelResponse(TextPart)`, and the newest user prompt streamed last, in transcript order.

`TestModel` does not expose the `list[ModelMessage]` it was handed, so turn 2 uses pydantic-ai's
`FunctionModel` — its message-recording sibling test model — whose `stream_function` receives the
exact messages the agent passed the model. Both are injected the same way via `set_chat_model`.

This is a CORRECT-behaviour (Express-parity) journey for a path with no existing coverage: it must
PASS on a correct product.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    UserPromptPart,
)
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel

from src.config import settings
from src.services.auth.session_jwt import mint_session_jwt
from tests.factories import UserFactory

_TTL = settings.auth.access_ttl_seconds


# --- chat fixtures ------------------------------------------------------------
# `set_chat_model` + the billing override live in a conftest scoped to tests/api/v1/claude/.
# A journey under tests/journeys/ has no access to them, so they are inlined here verbatim
# (copied from tests/api/v1/claude/conftest.py).


@pytest.fixture(autouse=True)
def _override_billing(app, db_session) -> None:  # noqa: ANN001
    from src.api.v1.claude.router import billing_session_factory

    @contextlib.asynccontextmanager
    async def _session() -> AsyncIterator[Any]:
        # Yield the rolled-back test session; the db_session fixture owns teardown.
        yield db_session

    app.dependency_overrides[billing_session_factory] = lambda: lambda: _session()


@pytest.fixture
def set_chat_model(app):  # noqa: ANN001, ANN201
    """Inject a Pydantic AI model (a TestModel / FunctionModel) for the chat endpoint."""

    def _set(model: object) -> None:
        from src.api.v1.claude.router import chat_model

        app.dependency_overrides[chat_model] = lambda: model

    return _set


# --- helpers ------------------------------------------------------------------


def _cookie(jwt: str) -> dict[str, str]:
    return {"Cookie": f"session={jwt}"}


async def _auth(db_session: Any) -> dict[str, str]:
    user = await UserFactory.create(db_session)
    return _cookie(mint_session_jwt(user.id, user.token_version, _TTL))


def _delta_texts(sse: str) -> list[str]:
    """Pull the ordered ``delta.text`` payloads out of an SSE event-stream body."""
    texts: list[str] = []
    for line in sse.splitlines():
        if not line.startswith("data: "):
            continue
        payload = line.removeprefix("data: ")
        if payload == "[DONE]":
            continue
        texts.append(json.loads(payload)["delta"]["text"])
    return texts


# --- journey ------------------------------------------------------------------


async def test_multiturn_generate_then_iterate_replays_history_in_order(
    client: Any, db_session: Any, set_chat_model: Any
) -> None:
    headers = await _auth(db_session)

    # --- Turn 1: initial generation — one user message → delta frames + [DONE] ---------
    set_chat_model(TestModel(custom_output_text="Here is your gate tracker app."))
    turn1 = await client.post(
        "/v1/claude",
        headers=headers,
        json={"messages": [{"role": "user", "content": "build a gate tracker"}]},
    )
    assert turn1.status_code == 200
    assert turn1.headers["content-type"].startswith("text/event-stream")
    # Exact Express wire frames the SPA parses: compact delta JSON + the [DONE] sentinel.
    assert 'data: {"delta":{"text":"Here is your gate tracker app."}}\n\n' in turn1.text
    assert turn1.text.endswith("data: [DONE]\n\n")

    prior_assistant = "Here is your gate tracker app."

    # --- Turn 2: refine — 3-message transcript; capture what the model was handed ------
    seen: list[list[ModelMessage]] = []

    async def _record(messages: list[ModelMessage], _info: AgentInfo) -> AsyncIterator[str]:
        # Snapshot the exact message list the agent handed the model (before mutation).
        seen.append(list(messages))
        yield "Added a status column to the gate tracker."

    set_chat_model(FunctionModel(stream_function=_record))

    transcript = [
        {"role": "user", "content": "build a gate tracker"},  # prior user turn
        {"role": "assistant", "content": prior_assistant},  # prior assistant turn
        {"role": "user", "content": "add a status column"},  # newest refine (must stream)
    ]
    turn2 = await client.post("/v1/claude", headers=headers, json={"messages": transcript})
    assert turn2.status_code == 200
    # The newest user prompt is the turn that streams back as assistant text.
    assert "".join(_delta_texts(turn2.text)) == "Added a status column to the gate tracker."
    assert turn2.text.endswith("data: [DONE]\n\n")

    # `resp.text` fully drains the stream, so the drain (and thus the model call) has finished:
    # the agent was handed exactly one request.
    assert len(seen) == 1
    history = seen[0]

    # `_split_messages` reconstructs the transcript as:
    #   prior user      → ModelRequest(UserPromptPart)
    #   prior assistant → ModelResponse(TextPart)
    #   newest user     → ModelRequest(UserPromptPart)   (streamed this turn)
    # in the SAME order the SPA sent them.
    assert len(history) == 3

    prior_user_msg = history[0]
    assert isinstance(prior_user_msg, ModelRequest)
    assert [p.content for p in prior_user_msg.parts if isinstance(p, UserPromptPart)] == [
        "build a gate tracker"
    ]

    prior_assistant_msg = history[1]
    assert isinstance(prior_assistant_msg, ModelResponse)
    assert [p.content for p in prior_assistant_msg.parts if isinstance(p, TextPart)] == [
        prior_assistant
    ]

    newest_user_msg = history[2]
    assert isinstance(newest_user_msg, ModelRequest)
    assert [p.content for p in newest_user_msg.parts if isinstance(p, UserPromptPart)] == [
        "add a status column"
    ]
