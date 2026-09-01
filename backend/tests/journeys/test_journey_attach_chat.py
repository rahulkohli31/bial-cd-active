"""Journey: attachment upload → turn → the bytes actually reach the model.

The SPA lets a user attach an image to a chat turn: the file is first uploaded to
`POST /v1/attachments` (persisted owner-scoped in the object store), and then — because
Azure-hosted Foundry has no Files API — the server rehydrates that owned REFERENCE back to
real bytes at send time and inlines them into the turn's prompt (`prompt_content`,
`api/v1/conversations/_shared.py`). This journey proves the whole chain end to end:

    upload(raw)  →  stored blob == raw  →  download == raw  →  send with the attachment id
                                                              →  model receives BinaryContent(raw)

The load-bearing assertion is the last hop: the stored reference must arrive at the model as
a `pydantic_ai.BinaryContent` (image/png) carrying the EXACT uploaded bytes, inside the
`UserPromptPart` the agent hands the model. If the attachment did not reach the model, the
generated app could not be "influenced" by the image at all.

The send step USED to be the retired `POST /v1/claude` relay; it is now
`POST /v1/conversations/{id}/turns`. The chain is the same one — the rehydrator and
`prompt_content` were always shared plumbing — so what moved is the door, not the property.

`TestModel` does not expose the `list[ModelMessage]` it was handed, so — as in the sibling
`test_journey_multiturn_generate` — the capturing turn uses pydantic-ai's message-recording
sibling test model `FunctionModel`, whose `stream_function` receives the exact messages the
agent passed the model. Both are injected the same way via `set_chat_model`.

This is a CORRECT-behaviour journey: the attachment→model path is intact, so it MUST PASS on
a correct product.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import uuid
from collections.abc import AsyncIterator
from typing import Any

import pytest
from pydantic_ai import BinaryContent
from pydantic_ai.messages import ModelMessage, ModelRequest, UserPromptPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from src.config import settings
from src.services.auth.csrf import issue_csrf_token
from src.services.auth.session_jwt import mint_session_jwt
from src.services.turns.engine import TurnEngine, set_turn_engine_for_tests
from src.services.turns.guard import _mid_reply
from tests.factories import UserFactory
from tests.fakes import FakeSandboxClient

_TTL = settings.auth.access_ttl_seconds

# A distinctive, minimal PNG: the 8-byte PNG signature (matches the upload allowlist's magic
# bytes) followed by an identifiable marker so an equality assert on the model's BinaryContent
# is unambiguous. Not a full valid PNG — the server checks only the leading magic bytes.
_RAW_PNG = b"\x89PNG\r\n\x1a\n" + b"gate-terminal-floor-plan-v2" + b"\x00\x00\x00\x00"
_B64_PNG = base64.b64encode(_RAW_PNG).decode()


# --- chat fixtures ------------------------------------------------------------
# `set_chat_model` + the billing override live in conftests scoped to tests/api/v1/.
# A journey under tests/journeys/ has no access to them, so they are inlined here.


@pytest.fixture(autouse=True)
def _fresh_engine():  # noqa: ANN201
    """A per-test turn engine, so this journey's detached turn is the only one to settle."""
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
    from src.services.sandbox.config import SandboxConfig
    from tests.api.v1.build_sessions.conftest import _sandbox_config

    assert isinstance(_sandbox_config(), SandboxConfig)
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
    """Inject a Pydantic AI model (a TestModel / FunctionModel) for the chat endpoint."""

    def _set(model: object) -> None:
        from src.api.v1.conversations._shared import chat_model

        app.dependency_overrides[chat_model] = lambda: model

    return _set


# --- helpers ------------------------------------------------------------------


async def _auth(db_session: Any, **overrides: Any):
    """Cookie + CSRF headers (the U7 conversation-create step is CSRF-protected) and the
    user (the journey seeds a project for the create)."""
    user = await UserFactory.create(db_session, **overrides)
    jwt = mint_session_jwt(user.id, user.token_version, _TTL)
    csrf = issue_csrf_token(user.id, user.token_version)
    return {"Cookie": f"session={jwt}; csrf={csrf}", "X-CSRF-Token": csrf}, user


def _answer_of(sse: str) -> str:
    """The assistant's whole answer out of a turn-stream body: the snapshot's `textSoFar`
    plus every `text_delta` after it. A settled turn replays as snapshot-only and a live one
    tails deltas, so summing both is what makes the assertion independent of which happened."""
    text = ""
    for line in sse.splitlines():
        if not line.startswith("data: "):
            continue
        payload = line.removeprefix("data: ")
        if payload == "[DONE]":
            continue
        frame = json.loads(payload)
        if frame.get("type") == "snapshot":
            text += frame["textSoFar"]
        elif frame.get("type") == "text_delta":
            text += frame["text"]
    return text


async def _settle(engine: Any, conversation_id: uuid.UUID) -> None:
    """Await the DETACHED turn task, not just its stream: the run's `finally` still writes
    the durable turn-terminal row after the last frame, on a session of its own."""
    state = engine.peek(conversation_id)
    assert state is not None and state.task is not None
    with contextlib.suppress(asyncio.CancelledError):
        await asyncio.wait_for(state.task, timeout=10)


# --- journey ------------------------------------------------------------------


async def test_uploaded_image_reaches_the_model_as_binary_content(
    client: Any,
    app: Any,
    db_session: Any,
    set_chat_model: Any,
    fake_storage: Any,
    _fresh_engine: Any,
) -> None:
    headers, user = await _auth(db_session)

    # The accessor-level fake serves BOTH consumers of the store: the upload route
    # (`storage_dependency` → `get_storage()`) and the turn route's send-time rehydrator
    # (`chat_storage` → `get_storage()`); keep the handle to assert the blob persisted.
    store = fake_storage

    attachment_id = "att_gate_floorplan_1"

    # --- 1. Upload the image attachment → 201, persisted owner-scoped -----------------
    up = await client.post(
        "/v1/attachments",
        headers=headers,
        json={
            "attachmentId": attachment_id,
            "mediaType": "image/png",
            "base64": _B64_PNG,
            "name": "terminal-floor-plan.png",
        },
    )
    assert up.status_code == 201, up.text
    att = up.json()["attachment"]
    assert att["attachmentId"] == attachment_id
    assert att["mediaType"] == "image/png"
    assert att["kind"] == "image"  # images map to the `image` content-part kind
    assert att["size"] == len(_RAW_PNG)
    key = att["key"]
    # The exact bytes landed in the store under the owner-scoped key (not a dangling row).
    assert store.objects[key] == _RAW_PNG

    # --- 2. Download round-trips the exact bytes (content-type sniffed from magic) -----
    down = await client.get(f"/v1/attachments/{attachment_id}", headers=headers)
    assert down.status_code == 200
    assert down.content == _RAW_PNG
    assert down.headers["content-type"] == "image/png"

    # --- 3. The chat turn inlines the SAME image and streams a generation --------------
    # Capture the exact `list[ModelMessage]` the agent handed the model (TestModel hides it,
    # so use FunctionModel — pydantic-ai's message-recording test model).
    seen: list[list[ModelMessage]] = []

    async def _record(messages: list[ModelMessage], _info: AgentInfo) -> AsyncIterator[str]:
        seen.append(list(messages))
        yield "Generated a gate-status board from the uploaded floor-plan image."

    set_chat_model(FunctionModel(stream_function=_record))

    # U7: the SPA sends only the new message — the typed text plus the OWNED reference to
    # the stored upload. The server rehydrates the reference to real bytes at send. The
    # conversation must exist first (`POST /v1/conversations`, the U7 ordering).
    from tests.factories import ProjectFactory

    project = await ProjectFactory.create(db_session, user.id)
    conversation_id = str(uuid.uuid4())
    created = await client.post(
        "/v1/conversations",
        headers=headers,
        json={"id": conversation_id, "projectId": str(project.id), "kind": "plan"},
    )
    assert created.status_code == 201, created.text

    started = await client.post(
        f"/v1/conversations/{conversation_id}/turns",
        headers=headers,
        json={
            "message": {
                "text": "Use this terminal floor-plan photo to build a gate-status board.",
                "attachmentTexts": [],
                "attachmentIds": [attachment_id],
            },
        },
    )
    assert started.status_code == 202, started.text
    await _settle(_fresh_engine, uuid.UUID(conversation_id))

    chat = await client.get(f"/v1/conversations/{conversation_id}/events", headers=headers)
    assert chat.status_code == 200
    assert chat.headers["content-type"].startswith("text/event-stream")
    # The generation streamed back (the attachment demonstrably influenced the output text).
    assert _answer_of(chat.text) == (
        "Generated a gate-status board from the uploaded floor-plan image."
    )
    assert chat.text.endswith("data: [DONE]\n\n")

    # --- 4. LOAD-BEARING: the image arrived at the model as BinaryContent(raw) ----------
    # The turn settled above, so the model call finished.
    assert len(seen) == 1, "the agent must have been handed exactly one model request"
    history = seen[0]

    user_parts = [
        part
        for message in history
        if isinstance(message, ModelRequest)
        for part in message.parts
        if isinstance(part, UserPromptPart)
    ]
    assert user_parts, "the model must have received the user prompt"
    content = user_parts[-1].content
    assert isinstance(content, list), "a multimodal turn maps to an ordered [str | BinaryContent]"

    texts = [c for c in content if isinstance(c, str)]
    binaries = [c for c in content if isinstance(c, BinaryContent)]
    # The instruction text survives alongside the binary, in order.
    assert any("floor-plan" in t for t in texts)
    # The stored REFERENCE was rehydrated server-side into exactly one BinaryContent...
    assert len(binaries) == 1
    binary = binaries[0]
    assert binary.media_type == "image/png"
    # ...carrying the EXACT bytes we uploaded — the attachment genuinely reached the model.
    assert binary.data == _RAW_PNG

    # --- 5. Cross-user isolation: user B cannot read user A's attachment ----------------
    headers_b, _ = await _auth(db_session, email="intruder@rvaiglobal.com")
    leak = await client.get(f"/v1/attachments/{attachment_id}", headers=headers_b)
    assert leak.status_code == 404  # owner-scoped; the same id is not a shared handle
