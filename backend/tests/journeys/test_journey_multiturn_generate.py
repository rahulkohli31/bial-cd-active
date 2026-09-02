"""Journey: multi-turn chat on the turn engine (U10/R9) — create → three turns → reload.

The full server-authoritative-history story, end to end through the real routes:

* `POST /v1/conversations` creates the row the SPA just minted (CSRF-protected), and
  re-POSTing the same mint is an idempotent 200.
* Three turns each send ONLY the new message; a recording `FunctionModel` proves the wire
  request to the model carried the prior transcript the SERVER assembled from the DB —
  turn N's request contains every earlier question and answer, in order, none of which
  rode the HTTP body.
* The reload read (`GET /v1/conversations/{id}`) projects the same conversation back as
  display items that match what streamed — the R8 reload story at the journey level.

This USED to drive the retired `POST /v1/claude` relay. The relay is gone (one turn engine,
one send path), so the journey now drives `POST /v1/conversations/{id}/turns` + the
`/events` subscription — the same server-assembled-history property, on the surface that
survived. Keeping it rather than deleting it with the relay is deliberate: the property it
pins (the browser sends one question, the server supplies the transcript) has no other
journey-level test.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from pydantic_ai.messages import ModelMessage, ModelRequest, UserPromptPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from src.config import settings
from src.services.auth.csrf import issue_csrf_token
from src.services.auth.session_jwt import mint_session_jwt
from src.services.turns.engine import TurnEngine, set_turn_engine_for_tests
from src.services.turns.guard import _mid_reply
from tests.factories import ProjectFactory, UserFactory

_TTL = settings.auth.access_ttl_seconds


# --- chat fixtures ------------------------------------------------------------
# `set_chat_model` + the billing override live in conftests scoped to tests/api/v1/.
# A journey under tests/journeys/ has no access to them, so they are inlined here.


@pytest.fixture(autouse=True)
def _fresh_engine():
    """A per-test engine, so one journey's detached turns can never be peeked at (or
    settled) by another's."""
    _mid_reply.clear()
    engine = TurnEngine()
    set_turn_engine_for_tests(engine)
    yield engine
    set_turn_engine_for_tests(None)
    _mid_reply.clear()


@pytest.fixture(autouse=True)
def _bind_a_workspace(app, fake_redis, monkeypatch: pytest.MonkeyPatch) -> None:  # noqa: ANN001
    """A sandbox client on both seams — a turn refuses 503 `workspace_unavailable` without
    one (R98). Inlined from `tests/api/v1/conversations/conftest.py`, which journeys cannot
    reach; `fake_redis` rides along because binding a workspace is what makes the send
    route's reclaim preflight reachable, and that preflight reads the coordination store."""
    from src.api.v1.build_sessions.deps import sandbox_dependency, sandbox_or_none_dependency
    from src.config import settings
    from tests.api.v1.build_sessions.conftest import _sandbox_config
    from tests.fakes import FakeSandboxClient

    monkeypatch.setattr(settings, "sandbox", _sandbox_config())
    sbx = FakeSandboxClient()
    app.dependency_overrides[sandbox_dependency] = lambda: sbx
    app.dependency_overrides[sandbox_or_none_dependency] = lambda: sbx


@pytest.fixture(autouse=True)
def _override_billing(app, db_session) -> None:  # noqa: ANN001
    from src.api.v1.conversations._shared import billing_session_factory

    @contextlib.asynccontextmanager
    async def _session() -> AsyncIterator[Any]:
        # Yield the rolled-back test session; the db_session fixture owns teardown.
        yield db_session

    app.dependency_overrides[billing_session_factory] = lambda: lambda: _session()


@pytest.fixture
def set_chat_model(app):  # noqa: ANN001, ANN201
    """Inject a Pydantic AI model (a TestModel / FunctionModel) for the turn engine."""

    def _set(model: object) -> None:
        from src.api.v1.conversations._shared import chat_model

        app.dependency_overrides[chat_model] = lambda: model

    return _set


# --- helpers ------------------------------------------------------------------


async def _auth(db_session: Any):
    """Cookie + CSRF headers (conversation-create AND turn-start are CSRF-protected)."""
    user = await UserFactory.create(db_session)
    jwt = mint_session_jwt(user.id, user.token_version, _TTL)
    csrf = issue_csrf_token(user.id, user.token_version)
    return {"Cookie": f"session={jwt}; csrf={csrf}", "X-CSRF-Token": csrf}, user


async def _settle(engine: Any, conversation_id: Any) -> None:
    """Await the DETACHED turn task, not just its stream: the run's `finally` still writes
    the durable turn-terminal row after the last frame, on a session of its own."""
    state = engine.peek(conversation_id)
    assert state is not None and state.task is not None
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(state.task, timeout=10)


def _delta_texts(sse: str) -> list[str]:
    """Pull the ordered `text_delta` payloads out of a turn-stream SSE body."""
    texts: list[str] = []
    for line in sse.splitlines():
        if not line.startswith("data: "):
            continue
        payload = line.removeprefix("data: ")
        if payload == "[DONE]":
            continue
        frame = json.loads(payload)
        if frame.get("type") == "text_delta":
            texts.append(frame["text"])
    return texts


# --- journey ------------------------------------------------------------------


async def test_stateless_multiturn_journey_with_reload_parity(
    client: Any, db_session: Any, set_chat_model: Any, _fresh_engine: Any
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
            "kind": "plan",
            "title": "Gate tracker",
        },
    )
    assert created.status_code == 201, created.text
    assert created.json()["conversation"]["_id"] == conversation_id

    # Re-POSTing the same mint is idempotent (a retry, a second tab) — 200, same header.
    again = await client.post(
        "/v1/conversations",
        headers=headers,
        json={"id": conversation_id, "projectId": str(project.id), "kind": "plan"},
    )
    assert again.status_code == 200
    assert again.json()["conversation"]["_id"] == conversation_id

    # --- 2. Three turns; the model records what the SERVER assembled ------------------
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
            f"/v1/conversations/{conversation_id}/turns",
            headers=headers,
            json={"message": {"text": question, "attachmentTexts": [], "attachmentIds": []}},
        )
        assert resp.status_code == 202, resp.text
        # The turn is DETACHED: settle the task before starting the next one, or turn N+1's
        # history load races turn N's persistence.
        await _settle(_fresh_engine, uuid.UUID(conversation_id))

    # The transport still carries the answer to a subscriber: replay the settled turn.
    events = await client.get(f"/v1/conversations/{conversation_id}/events", headers=headers)
    assert events.status_code == 200
    assert events.headers["content-type"].startswith("text/event-stream")
    assert events.text.endswith("data: [DONE]\n\n")
    # A settled turn replays as snapshot-only (the terminal IS the snapshot), so the answer
    # rides the snapshot's ordered `parts` rather than deltas — assert on whichever carried it.
    snapshot = json.loads(
        next(
            line.removeprefix("data: ")
            for line in events.text.splitlines()
            if line.startswith("data: ") and '"snapshot"' in line
        )
    )
    snapshot_text = "".join(part["text"] for part in snapshot["parts"] if part["type"] == "text")
    assert snapshot_text + "".join(_delta_texts(events.text)) == "Dark mode enabled."

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
    #
    # `<system-note>` parts are filtered out, and that is not a fudge — the turn engine
    # injects U14's ephemeral workspace reminder as a user-role part on the wire and
    # NEVER persists it. Keeping it out of this assertion is what lets the assertion say
    # what it means ("the citizen's questions, in order"); step 3 below then proves the
    # note stayed out of the durable transcript too.
    prompts = [
        part.content
        for message in runs[-1]
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, UserPromptPart)
        if not (isinstance(part.content, str) and part.content.startswith("<system-note>"))
    ]
    assert prompts == questions

    # --- 3. Reload: one read rebuilds the chat (R8) ------------------------------------
    detail = await client.get(f"/v1/conversations/{conversation_id}", headers=headers)
    assert detail.status_code == 200
    body = detail.json()
    assert body["activeTurn"] is None  # settled — nothing in flight
    # The FULL shape first, so the prose filter below cannot hide a lost or doubled row:
    # each turn projects question → answer → its durable terminal, three times over. The
    # ephemeral `<system-note>` the model saw on the wire appears nowhere — it was never
    # persisted, which is the whole point of injecting it as an instruction-time part.
    assert [item["type"] for item in body["projection"]] == [
        "user_text",
        "assistant_text",
        "turn_terminal",
    ] * 3
    texts = [
        (item["type"], item["text"])
        for item in body["projection"]
        if item["type"] in ("user_text", "assistant_text")
    ]
    assert texts == [
        ("user_text", "build a gate tracker"),
        ("assistant_text", "Here is a first cut of the gate tracker."),
        ("user_text", "make the rows sortable"),
        ("assistant_text", "Sortable rows added."),
        ("user_text", "add dark mode"),
        ("assistant_text", "Dark mode enabled."),
    ]
