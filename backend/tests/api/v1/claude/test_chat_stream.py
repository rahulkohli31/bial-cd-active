"""POST /v1/claude — daily gate (429 pre-stream), SSE frame shape, atomic billing, and the U7
typed-body validation. The Foundry model is a TestModel (no network); billing is bound to the
test session. The body is the STATELESS-TURN contract: `{conversationId, message}` — the
full-transcript `{messages, system, max_tokens}` relay shape is retired (R9), and the
conversation row must exist (404 otherwise; `POST /v1/conversations` creates it).
"""

from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import AsyncIterator
from typing import Any

from pydantic_ai.messages import ModelMessage
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.models.test import TestModel
from sqlalchemy import select

from src.config import settings
from src.db.models.token_usage import TokenUsage
from src.db.models.user_limit import UserLimit
from src.services.auth.session_jwt import mint_session_jwt
from src.services.usage.gate import record_usage
from tests.factories import ConversationFactory, ProjectFactory, UserFactory

_TTL = settings.auth.access_ttl_seconds


def _cookie(jwt: str) -> dict[str, str]:
    return {"Cookie": f"session={jwt}"}


async def _auth(db_session):
    user = await UserFactory.create(db_session)
    return _cookie(mint_session_jwt(user.id, user.token_version, _TTL)), user


async def _auth_with_conversation(db_session):
    headers, user = await _auth(db_session)
    project = await ProjectFactory.create(db_session, user.id)
    conversation = await ConversationFactory.create(db_session, user.id, project_id=project.id)
    return headers, user, conversation


def _body(conversation, text: str = "hello") -> dict[str, Any]:
    return {"conversationId": str(conversation.id), "message": {"text": text}}


# --- daily gate (AE2) ---------------------------------------------------------


async def test_over_limit_429_before_stream(client, db_session, set_chat_model) -> None:
    headers, user, conversation = await _auth_with_conversation(db_session)
    set_chat_model(TestModel(custom_output_text="should not stream"))
    db_session.add(UserLimit(user_id=user.id, daily_token_limit=10))
    await db_session.flush()
    await record_usage(db_session, user.id, input_tokens=10, output_tokens=0)

    resp = await client.post("/v1/claude", headers=headers, json=_body(conversation))
    assert resp.status_code == 429
    # Clean JSON, never a half-open event-stream.
    assert "text/event-stream" not in resp.headers["content-type"]
    body = resp.json()
    # Covers AE2: the dedicated 429 body keeps all five inner keys (as_response is
    # untouched) — a plain ErrorEnvelope would drop limit/used/remaining.
    assert set(body["error"]) == {"message", "code", "limit", "used", "remaining"}
    assert body["error"]["code"] == "daily_token_limit_exceeded"
    assert body["error"]["limit"] == 10
    assert body["error"]["used"] == 10
    assert body["error"]["remaining"] == 0
    assert "daily limit of 10 tokens" in body["error"]["message"]


# --- SSE stream + billing -----------------------------------------------------


async def test_stream_emits_delta_and_done_frames(client, db_session, set_chat_model) -> None:
    headers, _, conversation = await _auth_with_conversation(db_session)
    set_chat_model(TestModel(custom_output_text="hello world"))
    resp = await client.post("/v1/claude", headers=headers, json=_body(conversation))
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    # Exact Express wire frames: compact delta JSON + the [DONE] sentinel.
    assert 'data: {"delta":{"text":"hello world"}}\n\n' in resp.text
    assert resp.text.endswith("data: [DONE]\n\n")


async def test_keepalive_leaves_a_normal_stream_untouched(
    client, db_session, set_chat_model
) -> None:
    # F1 no-regression: the mid-stream `: ping` keepalive must not alter a stream with no idle gap
    # — a fast model streams straight through with the exact delta + [DONE] frames, no stray ping.
    headers, _, conversation = await _auth_with_conversation(db_session)
    set_chat_model(TestModel(custom_output_text="no gap here"))
    resp = await client.post("/v1/claude", headers=headers, json=_body(conversation))
    assert resp.status_code == 200
    assert 'data: {"delta":{"text":"no gap here"}}\n\n' in resp.text
    assert ": ping" not in resp.text  # no idle gap → no keepalive noise
    assert resp.text.endswith("data: [DONE]\n\n")


async def test_keepalive_pings_fill_a_mid_stream_idle_gap(
    client, db_session, set_chat_model, monkeypatch
) -> None:
    # When the model queue sits idle past _KEEPALIVE_SECONDS, the generator emits `: ping`
    # comment frames so the client watchdog can tell a working server from a dead socket —
    # and the data frames still arrive intact around them.
    monkeypatch.setattr("src.api.v1.claude.router._KEEPALIVE_SECONDS", 0.02)

    async def _stalling_stream(
        messages: list[ModelMessage], agent_info: AgentInfo
    ) -> AsyncIterator[str]:
        yield "tick"
        await asyncio.sleep(0.3)  # far past the tiny keepalive window, still a fast test
        yield "tock"

    headers, _, conversation = await _auth_with_conversation(db_session)
    set_chat_model(FunctionModel(stream_function=_stalling_stream))
    resp = await client.post("/v1/claude", headers=headers, json=_body(conversation))
    assert resp.status_code == 200
    assert ": ping\n\n" in resp.text  # the idle gap was bridged by at least one keepalive
    # The normal data frames still arrive intact around the pings.
    assert 'data: {"delta":{"text":"tick"}}\n\n' in resp.text
    assert 'data: {"delta":{"text":"tock"}}\n\n' in resp.text
    assert resp.text.endswith("data: [DONE]\n\n")


async def test_usage_billed_after_stream(client, db_session, set_chat_model) -> None:
    headers, user, conversation = await _auth_with_conversation(db_session)
    set_chat_model(TestModel(custom_output_text="billed"))
    resp = await client.post("/v1/claude", headers=headers, json=_body(conversation))
    assert resp.status_code == 200
    _ = resp.text  # fully drain the stream (billing lands before [DONE])

    row = await db_session.scalar(select(TokenUsage).where(TokenUsage.user_id == user.id))
    assert row is not None
    assert row.input_tokens + row.output_tokens > 0  # the turn was billed


async def test_billing_survives_client_disconnect_midstream(db_session) -> None:
    """A client disconnect mid-stream must NOT drop billing.

    Drive `_stream` directly, consume one delta frame, then `aclose()` the SSE generator BEFORE
    [DONE] — this is the disconnect. The billing drain is a decoupled background task, so it must
    still run to completion and bill the full turn. A regression that billed *inside* the SSE
    generator would be cancelled here and write no usage row, failing this test.
    """
    from collections.abc import AsyncGenerator
    from typing import cast

    from fastapi.responses import StreamingResponse

    from src.api.v1.claude.router import _drains, _stream

    user = await UserFactory.create(db_session)

    @contextlib.asynccontextmanager
    async def _factory() -> AsyncGenerator[Any]:
        # The drain's own session double; the db_session fixture owns rollback.
        yield db_session

    # `_factory` is a zero-arg callable → async-CM session, matching how `_drain` uses the
    # production `async_sessionmaker`; typed Any as an intentional test double.
    factory: Any = _factory
    before = set(_drains)
    resp = await _stream(
        factory,
        user.id,
        TestModel(custom_output_text="a b c d e"),
        "hi",
        [],
        "",
        None,
        uuid.uuid4(),
    )
    assert isinstance(resp, StreamingResponse)
    gen = cast("AsyncGenerator[bytes]", resp.body_iterator)
    first = await gen.__anext__()  # one frame → the drain has started producing
    assert first.startswith(b"data: ")
    await gen.aclose()  # cancel the SSE generator (client disconnect) before [DONE]

    mine = [task for task in _drains if task not in before]
    assert mine, "the billing drain must be a decoupled task that outlives the SSE generator"
    await asyncio.gather(*mine)  # the shielded drain must still finish + bill

    row = await db_session.scalar(select(TokenUsage).where(TokenUsage.user_id == user.id))
    assert row is not None
    assert row.input_tokens + row.output_tokens > 0  # billed despite the disconnect


# --- U7: the typed stateless-turn contract ------------------------------------


async def test_unknown_conversation_is_404(client, db_session, set_chat_model) -> None:
    # U7 retires the load-bearing no-op arm: conversations are created BEFORE the first turn,
    # so an unknown id is a client bug — and a cross-user id must be indistinguishable.
    headers, _ = await _auth(db_session)
    set_chat_model(TestModel(custom_output_text="never streams"))
    resp = await client.post(
        "/v1/claude",
        headers=headers,
        json={"conversationId": str(uuid.uuid4()), "message": {"text": "hello"}},
    )
    assert resp.status_code == 404
    assert resp.json() == {"error": {"message": "Conversation not found."}}


async def test_cross_user_conversation_is_the_same_404(client, db_session, set_chat_model) -> None:
    headers_a, _, conversation_a = await _auth_with_conversation(db_session)
    headers_b, _ = await _auth(db_session)
    set_chat_model(TestModel(custom_output_text="never streams"))
    resp = await client.post("/v1/claude", headers=headers_b, json=_body(conversation_a))
    assert resp.status_code == 404
    assert resp.json() == {"error": {"message": "Conversation not found."}}


async def test_malformed_json_body_400(client, db_session, set_chat_model) -> None:
    headers, _ = await _auth(db_session)
    set_chat_model(TestModel())
    resp = await client.post(
        "/v1/claude",
        headers={**headers, "Content-Type": "application/json"},
        content=b'{"conversationId": [',
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["message"].startswith("Invalid request:")


async def test_empty_message_400(client, db_session, set_chat_model) -> None:
    # A turn with no text, no attachment text, and no attachment refs carries nothing to say.
    headers, _, conversation = await _auth_with_conversation(db_session)
    set_chat_model(TestModel())
    resp = await client.post(
        "/v1/claude",
        headers=headers,
        json={"conversationId": str(conversation.id), "message": {"text": "   "}},
    )
    assert resp.status_code == 400
    assert "carry content" in resp.json()["error"]["message"]


async def test_legacy_full_transcript_body_400(client, db_session, set_chat_model) -> None:
    # The retired relay shape must fail loudly, not be half-read: `messages` is not a field of
    # the typed body, and `message` is required.
    headers, _, conversation = await _auth_with_conversation(db_session)
    set_chat_model(TestModel())
    resp = await client.post(
        "/v1/claude",
        headers=headers,
        json={
            "conversationId": str(conversation.id),
            "messages": [{"role": "user", "content": "hello"}],
            "system": "legacy",
        },
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["message"].startswith("Invalid request:")


async def test_bad_attachment_id_400(client, db_session, set_chat_model) -> None:
    headers, _, conversation = await _auth_with_conversation(db_session)
    set_chat_model(TestModel())
    resp = await client.post(
        "/v1/claude",
        headers=headers,
        json={
            "conversationId": str(conversation.id),
            "message": {"text": "look", "attachmentIds": ["../escape"]},
        },
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["message"].startswith("Invalid request:")


async def test_oversized_attachment_text_400(client, db_session, set_chat_model) -> None:
    from src.api.v1.claude.router import _MAX_ATTACHMENT_TEXT_CHARS

    headers, _, conversation = await _auth_with_conversation(db_session)
    set_chat_model(TestModel())
    resp = await client.post(
        "/v1/claude",
        headers=headers,
        json={
            "conversationId": str(conversation.id),
            "message": {
                "text": "see attachment",
                "attachmentTexts": ["x" * (_MAX_ATTACHMENT_TEXT_CHARS + 1)],
            },
        },
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["message"].startswith("Invalid request:")


async def test_ephemeral_and_regenerate_are_mutually_exclusive_400(
    client, db_session, set_chat_model
) -> None:
    headers, _, conversation = await _auth_with_conversation(db_session)
    set_chat_model(TestModel())
    resp = await client.post(
        "/v1/claude",
        headers=headers,
        json={
            "conversationId": str(conversation.id),
            "message": {"text": "x"},
            "ephemeral": "summarize_brief",
            "regenerate": True,
        },
    )
    assert resp.status_code == 400
    assert resp.json()["error"]["message"].startswith("Invalid request:")


async def test_body_too_large_413(client, db_session, set_chat_model, monkeypatch) -> None:
    # The content-length fast-reject still 413s.
    monkeypatch.setattr("src.api.v1.claude.router._BODY_LIMIT_BYTES", 8)
    headers, _ = await _auth(db_session)
    set_chat_model(TestModel())
    resp = await client.post(
        "/v1/claude", headers=headers, json={"conversationId": str(uuid.uuid4())}
    )
    assert resp.status_code == 413
    assert resp.json() == {"error": {"message": "Request body is too large."}}


async def test_lying_content_length_413(client, db_session, set_chat_model, monkeypatch) -> None:
    # The Content-Length fast-reject can be bypassed by a chunked request (no/absent
    # Content-Length), so the cap is ALSO enforced on the actually-received bytes.
    monkeypatch.setattr("src.api.v1.claude.router._BODY_LIMIT_BYTES", 8)
    headers, _ = await _auth(db_session)
    set_chat_model(TestModel())

    async def _oversize_chunks():
        # No length known → httpx sends Transfer-Encoding: chunked (no Content-Length),
        # so the header fast-reject is skipped and the streamed bytes exceed the 8-byte cap.
        yield b"x" * 64

    resp = await client.post("/v1/claude", headers=headers, content=_oversize_chunks())
    assert resp.status_code == 413
    assert resp.json() == {"error": {"message": "Request body is too large."}}


async def test_stream_pre_delta_failure_raises_500(db_session) -> None:
    # The drain's broad-except failure path: a pre-delta upstream failure raises the explicit
    # 500 ErrorEnvelope, never a half-open stream.
    from collections.abc import AsyncGenerator

    import pytest

    from src.api.v1.claude.router import _stream
    from src.core.errors import AppApiError

    user = await UserFactory.create(db_session)

    @contextlib.asynccontextmanager
    async def _boom_factory() -> AsyncGenerator[Any]:
        raise RuntimeError("upstream session failed")
        yield db_session  # unreachable — present only to make this an async generator

    factory: Any = _boom_factory
    with pytest.raises(AppApiError) as exc_info:
        await _stream(
            factory,
            user.id,
            TestModel(custom_output_text="x"),
            "hi",
            [],
            "",
            None,
            uuid.uuid4(),
        )
    assert exc_info.value.status_code == 500
    assert exc_info.value.message == "The model request failed."


def test_claude_openapi_documents_full_error_set() -> None:
    from src.main import create_app

    responses = create_app().openapi()["paths"]["/v1/claude"]["post"]["responses"]
    assert {"400", "401", "404", "409", "413", "429", "500", "503"} <= set(responses)
    # 429 documents the dedicated 5-key body; the explicit 500 documents the envelope
    # (overriding the v1 DetailBody 500 default).
    ref_429 = responses["429"]["content"]["application/json"]["schema"]["$ref"]
    assert ref_429.endswith("DailyTokenLimitBody")
    ref_500 = responses["500"]["content"]["application/json"]["schema"]["$ref"]
    assert ref_500.endswith("ErrorEnvelope")


async def test_not_configured_returns_503(client, db_session) -> None:
    # No model override → chat_model() returns None (Foundry unset in test) → 503.
    headers, _, conversation = await _auth_with_conversation(db_session)
    resp = await client.post("/v1/claude", headers=headers, json=_body(conversation))
    assert resp.status_code == 503
    assert resp.json() == {"error": {"message": "Claude client not configured."}}


async def test_requires_auth(client) -> None:
    resp = await client.post(
        "/v1/claude", json={"conversationId": str(uuid.uuid4()), "message": {"text": "hi"}}
    )
    assert resp.status_code == 401
