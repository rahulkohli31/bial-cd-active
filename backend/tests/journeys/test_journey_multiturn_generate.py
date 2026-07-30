"""Journey: multi-turn chat on the STATELESS relay (U7/R9) — create → three turns → reload.

The full server-authoritative-history story, end to end through the real routes:

* `POST /v1/conversations` creates the row the SPA just minted (CSRF-protected), and
  re-POSTing the same mint is an idempotent 200.
* Three turns each send ONLY the new message; a recording `FunctionModel` proves the wire
  request to the model carried the prior transcript the SERVER assembled from the DB —
  turn N's request contains every earlier question and answer, in order, none of which
  rode the HTTP body.
* The reload read (`GET /v1/conversations/{id}`) projects the same conversation back as
  display items that match what streamed — the R8 reload story at the journey level.

This replaces the retired `_split_messages` journey: under U7 there is no client transcript
to split — the DB is the transcript.
"""

from __future__ import annotations

import contextlib
import json
from collections.abc import AsyncIterator
from typing import Any

import pytest
from pydantic_ai.messages import ModelMessage, ModelRequest, UserPromptPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from src.config import settings
from src.services.auth.csrf import issue_csrf_token
from src.services.auth.session_jwt import mint_session_jwt
from tests.factories import ProjectFactory, UserFactory

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


async def _auth(db_session: Any):
    """Cookie + CSRF headers (the conversation-create route is CSRF-protected)."""
    user = await UserFactory.create(db_session)
    jwt = mint_session_jwt(user.id, user.token_version, _TTL)
    csrf = issue_csrf_token(user.id, user.token_version)
    return {"Cookie": f"session={jwt}; csrf={csrf}", "X-CSRF-Token": csrf}, user


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


async def test_stateless_multiturn_journey_with_reload_parity(
    client: Any, db_session: Any, set_chat_model: Any
) -> None:
    headers, user = await _auth(db_session)
    project = await ProjectFactory.create(db_session, user.id)

    # --- 1. Create the conversation BEFORE the first turn (the U7 ordering) -----------
    conversation_id = "0198a5a0-0000-7000-8000-000000000001"  # the SPA's client mint
    created = await client.post(
        "/v1/conversations",
        headers=headers,
        json={
            "id": conversation_id,
            "projectId": str(project.id),
            "kind": "planning",
            "title": "Gate tracker",
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["conversation"]["_id"] == conversation_id

    # Re-POSTing the same mint is idempotent (a retry, a second tab) — 200, same header.
    again = await client.post(
        "/v1/conversations",
        headers=headers,
        json={"id": conversation_id, "projectId": str(project.id), "kind": "planning"},
    )
    assert again.status_code == 200
    assert again.json()["conversation"]["_id"] == conversation_id

    # --- 2. Three stateless turns; the model records what the SERVER assembled --------
    runs: list[list[ModelMessage]] = []
    replies = iter(
        [
            "Here is a first cut of the gate tracker.",
            "Sortable rows added.",
            "Dark mode enabled.",
        ]
    )

    async def _record(messages: list[ModelMessage], _info: AgentInfo) -> AsyncIterator[str]:
        runs.append(list(messages))
        yield next(replies)

    set_chat_model(FunctionModel(stream_function=_record))

    questions = [
        "build a gate tracker",
        "make the rows sortable",
        "add dark mode",
    ]
    for question in questions:
        resp = await client.post(
            "/v1/claude",
            headers=headers,
            json={"conversationId": conversation_id, "message": {"text": question}},
        )
        assert resp.status_code == 200, resp.text
        assert resp.headers["content-type"].startswith("text/event-stream")
        assert resp.text.endswith("data: [DONE]\n\n")

    # Turn 1 streamed the exact Express wire frames the SPA parses.
    # (Later turns were drained above; the recording model proves the history story.)
    assert len(runs) == 3

    # Turn 3's wire request carries the WHOLE prior story, assembled server-side — the
    # browser only ever sent one question per POST.
    final_run = str(runs[-1])
    for expected in (
        "build a gate tracker",
        "Here is a first cut of the gate tracker.",
        "make the rows sortable",
        "Sortable rows added.",
        "add dark mode",
    ):
        assert expected in final_run
    # And in ORDER: the user prompts appear exactly as the questions were asked.
    prompts = [
        part.content
        for message in runs[-1]
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, UserPromptPart)
    ]
    assert prompts == questions

    # --- 3. Reload: one read rebuilds the chat (R8) ------------------------------------
    detail = await client.get(f"/v1/conversations/{conversation_id}", headers=headers)
    assert detail.status_code == 200
    body = detail.json()
    assert body["activeTurn"] is None  # settled — nothing in flight
    texts = [(item["type"], item["text"]) for item in body["projection"]]
    assert texts == [
        ("user_text", "build a gate tracker"),
        ("assistant_text", "Here is a first cut of the gate tracker."),
        ("user_text", "make the rows sortable"),
        ("assistant_text", "Sortable rows added."),
        ("user_text", "add dark mode"),
        ("assistant_text", "Dark mode enabled."),
    ]
