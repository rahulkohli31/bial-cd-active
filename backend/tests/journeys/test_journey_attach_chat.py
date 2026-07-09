"""Journey: attachment upload → chat relay → the bytes actually reach the model.

The SPA lets a user attach an image to a chat turn: the file is first uploaded to
`POST /v1/attachments` (persisted owner-scoped in the object store), and then — because
Azure-hosted Foundry has no Files API — the SAME bytes are base64-INLINED into the newest
turn's Anthropic-shaped `content` and sent through the stateless `POST /v1/claude` relay
(`src/services/agent/content.py` docstring: "the newest turn's binaries already
base64-inlined"). This journey proves the whole chain end to end:

    upload(raw)  →  stored blob == raw  →  download == raw  →  inline into /v1/claude
                                                              →  model receives BinaryContent(raw)

The load-bearing assertion is the last hop: `to_model_content` must map the inlined image
block into a `pydantic_ai.BinaryContent` (image/png) carrying the EXACT uploaded bytes, and
that content must arrive in the `UserPromptPart` the agent hands the model. If the attachment
did not reach the model, the generated app could not be "influenced" by the image at all.

`TestModel` does not expose the `list[ModelMessage]` it was handed, so — as in the sibling
`test_journey_multiturn_generate` — the capturing turn uses pydantic-ai's message-recording
sibling test model `FunctionModel`, whose `stream_function` receives the exact messages the
agent passed the model. Both are injected the same way via `set_chat_model`.

This is a CORRECT-behaviour (Express-parity) journey: the attachment→chat path is intact, so
it MUST PASS on a correct product.
"""

from __future__ import annotations

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
from src.services.auth.session_jwt import mint_session_jwt
from tests.factories import UserFactory
from tests.fakes import FakeStorage

_TTL = settings.auth.access_ttl_seconds

# A distinctive, minimal PNG: the 8-byte PNG signature (matches the upload allowlist's magic
# bytes) followed by an identifiable marker so an equality assert on the model's BinaryContent
# is unambiguous. Not a full valid PNG — the server checks only the leading magic bytes.
_RAW_PNG = b"\x89PNG\r\n\x1a\n" + b"gate-terminal-floor-plan-v2" + b"\x00\x00\x00\x00"
_B64_PNG = base64.b64encode(_RAW_PNG).decode()


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


async def _auth(db_session: Any, **overrides: Any) -> dict[str, str]:
    user = await UserFactory.create(db_session, **overrides)
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


async def test_uploaded_image_reaches_the_model_as_binary_content(
    client: Any, app: Any, db_session: Any, set_chat_model: Any
) -> None:
    headers = await _auth(db_session)

    # Point the attachment routes at an in-memory store so the upload never reaches Azure;
    # keep the handle so we can assert the blob was persisted verbatim.
    from src.api.v1.attachments.router import storage_dependency

    store = FakeStorage()
    app.dependency_overrides[storage_dependency] = lambda: store

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

    # The SPA base64-inlines the newest turn's binary alongside the instruction text — this is
    # the exact Anthropic `content` shape `to_model_content` must translate.
    messages = [
        {
            "role": "user",
            "content": [
                {
                    "type": "text",
                    "text": "Use this terminal floor-plan photo to build a gate-status board.",
                },
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/png", "data": _B64_PNG},
                },
            ],
        }
    ]
    chat = await client.post(
        "/v1/claude",
        headers=headers,
        # Project-first: every chat turn names its conversation. This one is not persisted yet
        # (the SPA writes the row after the stream), so no project context is injected.
        json={"messages": messages, "conversationId": str(uuid.uuid4())},
    )
    assert chat.status_code == 200
    assert chat.headers["content-type"].startswith("text/event-stream")
    # The generation streamed back (the attachment demonstrably influenced the output text).
    assert (
        "".join(_delta_texts(chat.text))
        == "Generated a gate-status board from the uploaded floor-plan image."
    )
    assert chat.text.endswith("data: [DONE]\n\n")

    # --- 4. LOAD-BEARING: the image arrived at the model as BinaryContent(raw) ----------
    # `chat.text` fully drained the stream, so the drain (and thus the model call) finished.
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
    # to_model_content mapped the inlined image block into exactly one BinaryContent...
    assert len(binaries) == 1
    binary = binaries[0]
    assert binary.media_type == "image/png"
    # ...carrying the EXACT bytes we uploaded — the attachment genuinely reached the model.
    assert binary.data == _RAW_PNG

    # --- 5. Cross-user isolation: user B cannot read user A's attachment ----------------
    headers_b = await _auth(db_session, email="intruder@rvaiglobal.com")
    leak = await client.get(f"/v1/attachments/{attachment_id}", headers=headers_b)
    assert leak.status_code == 404  # owner-scoped; the same id is not a shared handle
