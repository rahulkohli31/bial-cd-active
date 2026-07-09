"""POST /v1/claude — daily gate (429 pre-stream), SSE frame shape, atomic billing, multimodal
(U13). The Foundry model is a TestModel (no network); billing is bound to the test session.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import uuid
from typing import Any

from pydantic_ai.models.test import TestModel
from sqlalchemy import select

from src.config import settings
from src.db.models.token_usage import TokenUsage
from src.db.models.user_limit import UserLimit
from src.services.auth.session_jwt import mint_session_jwt
from src.services.usage.gate import record_usage
from tests.factories import UserFactory

_TTL = settings.auth.access_ttl_seconds
_MESSAGES = [{"role": "user", "content": "hello"}]


def _conv() -> str:
    """A valid, not-yet-persisted conversation id — the legitimate first-turn case the
    relay must still stream (project context is simply not injected)."""
    return str(uuid.uuid4())


def _cookie(jwt: str) -> dict[str, str]:
    return {"Cookie": f"session={jwt}"}


async def _auth(db_session):
    user = await UserFactory.create(db_session)
    return _cookie(mint_session_jwt(user.id, user.token_version, _TTL)), user


# --- daily gate (AE2) ---------------------------------------------------------


async def test_over_limit_429_before_stream(client, db_session, set_chat_model) -> None:
    headers, user = await _auth(db_session)
    set_chat_model(TestModel(custom_output_text="should not stream"))
    db_session.add(UserLimit(user_id=user.id, daily_token_limit=10))
    await db_session.flush()
    await record_usage(db_session, user.id, input_tokens=10, output_tokens=0)

    resp = await client.post("/v1/claude", headers=headers, json={"messages": _MESSAGES})
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
    headers, _ = await _auth(db_session)
    set_chat_model(TestModel(custom_output_text="hello world"))
    resp = await client.post(
        "/v1/claude", headers=headers, json={"messages": _MESSAGES, "conversationId": _conv()}
    )
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    # Exact Express wire frames: compact delta JSON + the [DONE] sentinel.
    assert 'data: {"delta":{"text":"hello world"}}\n\n' in resp.text
    assert resp.text.endswith("data: [DONE]\n\n")


async def test_usage_billed_after_stream(client, db_session, set_chat_model) -> None:
    headers, user = await _auth(db_session)
    set_chat_model(TestModel(custom_output_text="billed"))
    resp = await client.post(
        "/v1/claude", headers=headers, json={"messages": _MESSAGES, "conversationId": _conv()}
    )
    assert resp.status_code == 200
    _ = resp.text  # fully drain the stream (billing lands before [DONE])

    row = await db_session.scalar(select(TokenUsage).where(TokenUsage.user_id == user.id))
    assert row is not None
    assert row.input_tokens + row.output_tokens > 0  # the turn was billed


async def test_billing_survives_client_disconnect_midstream(db_session) -> None:
    """U13 headline (review) invariant: a client disconnect mid-stream must NOT drop billing.

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
        factory, user.id, TestModel(custom_output_text="a b c d e"), "hi", [], "", 64
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


async def test_multimodal_request_streams(client, db_session, set_chat_model) -> None:
    headers, _ = await _auth(db_session)
    set_chat_model(TestModel(custom_output_text="saw the image"))
    png = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8).decode()
    messages = [
        {
            "role": "user",
            "content": [
                {"type": "text", "text": "look at this"},
                {
                    "type": "image",
                    "source": {"type": "base64", "media_type": "image/png", "data": png},
                },
            ],
        }
    ]
    resp = await client.post(
        "/v1/claude", headers=headers, json={"messages": messages, "conversationId": _conv()}
    )
    assert resp.status_code == 200
    assert 'data: {"delta":{"text":"saw the image"}}\n\n' in resp.text


# --- validation + config ------------------------------------------------------


async def test_empty_messages_400(client, db_session, set_chat_model) -> None:
    headers, _ = await _auth(db_session)
    set_chat_model(TestModel())
    resp = await client.post("/v1/claude", headers=headers, json={"messages": []})
    assert resp.status_code == 400
    # Migrated from _error → AppApiError: same byte-identical envelope.
    assert resp.json() == {"error": {"message": "messages must be a non-empty array."}}


# --- U3: the request contract is validated, not tolerantly read ---------------


async def test_malformed_json_body_400_names_the_json_defect(
    client, db_session, set_chat_model
) -> None:
    # Was misreported as "messages must be a non-empty array" (the body coerced to `{}`).
    headers, _ = await _auth(db_session)
    set_chat_model(TestModel())
    resp = await client.post(
        "/v1/claude",
        headers={**headers, "Content-Type": "application/json"},
        content=b'{"messages": [',
    )
    assert resp.status_code == 400
    assert resp.json() == {"error": {"message": "Invalid JSON body."}}


async def test_non_object_body_400(client, db_session, set_chat_model) -> None:
    headers, _ = await _auth(db_session)
    set_chat_model(TestModel())
    resp = await client.post("/v1/claude", headers=headers, json=[{"role": "user"}])
    assert resp.status_code == 400
    assert resp.json() == {"error": {"message": "Request body must be a JSON object."}}


async def test_non_string_system_400(client, db_session, set_chat_model) -> None:
    # Was silently dropped to "" — the entire system prompt vanished with no signal.
    headers, _ = await _auth(db_session)
    set_chat_model(TestModel())
    for bad in (["a"], 7, {"text": "a"}):
        resp = await client.post(
            "/v1/claude", headers=headers, json={"messages": _MESSAGES, "system": bad}
        )
        assert resp.status_code == 400, bad
        assert resp.json() == {"error": {"message": "system must be a string."}}, bad


async def test_bad_max_tokens_400(client, db_session, set_chat_model) -> None:
    # Was coerced to the DEFAULT, i.e. silently granting the MAXIMUM budget — worst direction.
    headers, _ = await _auth(db_session)
    set_chat_model(TestModel())
    for bad in ("abc", 0, -5, 1.5, True):
        resp = await client.post(
            "/v1/claude", headers=headers, json={"messages": _MESSAGES, "max_tokens": bad}
        )
        assert resp.status_code == 400, bad
        assert resp.json() == {"error": {"message": "max_tokens must be a positive integer."}}, bad


def test_max_output_tokens_absent_default_and_clamp() -> None:
    # The arms that must NOT change: absent (and its `null` spelling) → the defined 64 000
    # default; a valid over-large value is still clamped DOWN to the server ceiling.
    from src.api.v1.claude.router import _MAX_OUTPUT_TOKENS, _max_output_tokens

    assert _max_output_tokens(None) == _MAX_OUTPUT_TOKENS
    assert _max_output_tokens(999_999) == _MAX_OUTPUT_TOKENS
    assert _max_output_tokens(1_024) == 1_024


async def test_malformed_transcript_entry_400(client, db_session, set_chat_model) -> None:
    # A non-dict entry or an unknown role was silently skipped, shrinking the model's context.
    headers, _ = await _auth(db_session)
    set_chat_model(TestModel())
    cases = [
        (["not-a-dict", {"role": "user", "content": "hi"}], "messages[0] must be an object."),
        (
            [{"role": "system", "content": "x"}, {"role": "user", "content": "hi"}],
            "messages[0].role must be user or assistant.",
        ),
        ([{"role": "bot", "content": "hi"}], "messages[0].role must be user or assistant."),
    ]
    for messages, expected in cases:
        resp = await client.post("/v1/claude", headers=headers, json={"messages": messages})
        assert resp.status_code == 400, messages
        assert resp.json() == {"error": {"message": expected}}, messages


async def test_empty_newest_content_400(client, db_session, set_chat_model) -> None:
    # An unusable prompt must not reach the model as an empty turn.
    headers, _ = await _auth(db_session)
    set_chat_model(TestModel())
    empty_contents: list[Any] = ["", [], None, 7]
    for content in empty_contents:
        resp = await client.post(
            "/v1/claude",
            headers=headers,
            json={"messages": [{"role": "user", "content": content}]},
        )
        assert resp.status_code == 400, content
        assert resp.json() == {"error": {"message": "The newest message must carry content."}}


async def test_absent_conversation_id_400(client, db_session, set_chat_model) -> None:
    # Project-first: the client mints the conversation id up front. Omitting it used to
    # silently drop the project description + builder code seed from the turn.
    headers, _ = await _auth(db_session)
    set_chat_model(TestModel())
    resp = await client.post("/v1/claude", headers=headers, json={"messages": _MESSAGES})
    assert resp.status_code == 400
    assert resp.json() == {"error": {"message": "conversationId is required."}}


async def test_malformed_conversation_id_400(client, db_session, set_chat_model) -> None:
    headers, _ = await _auth(db_session)
    set_chat_model(TestModel())
    bad_ids: list[Any] = ["not-a-uuid", 7, ["x"]]
    for bad in bad_ids:
        resp = await client.post(
            "/v1/claude", headers=headers, json={"messages": _MESSAGES, "conversationId": bad}
        )
        assert resp.status_code == 400, bad
        assert resp.json() == {"error": {"message": "conversationId is invalid."}}, bad


async def test_unknown_conversation_id_still_streams(client, db_session, set_chat_model) -> None:
    # The load-bearing no-op: the SPA persists the conversation row only AFTER the stream, so
    # the first turn of every new chat names a not-yet-stored id and must not 4xx.
    headers, _ = await _auth(db_session)
    set_chat_model(TestModel(custom_output_text="first turn"))
    resp = await client.post(
        "/v1/claude", headers=headers, json={"messages": _MESSAGES, "conversationId": _conv()}
    )
    assert resp.status_code == 200
    assert resp.text.endswith("data: [DONE]\n\n")


async def test_valid_turn_with_history_and_system_still_streams(
    client, db_session, set_chat_model
) -> None:
    # Regression guard: the well-formed shape is untouched by the tightening.
    headers, _ = await _auth(db_session)
    set_chat_model(TestModel(custom_output_text="ok"))
    resp = await client.post(
        "/v1/claude",
        headers=headers,
        json={
            "messages": [
                {"role": "user", "content": "first"},
                {"role": "assistant", "content": "reply"},
                {"role": "user", "content": "second"},
            ],
            "system": "BE BRIEF",
            "max_tokens": 512,
            "conversationId": _conv(),
        },
    )
    assert resp.status_code == 200
    assert resp.text.endswith("data: [DONE]\n\n")


async def test_body_too_large_413(client, db_session, set_chat_model, monkeypatch) -> None:
    # The content-length fast-reject (migrated _error → AppApiError) still 413s.
    monkeypatch.setattr("src.api.v1.claude.router._BODY_LIMIT_BYTES", 8)
    headers, _ = await _auth(db_session)
    set_chat_model(TestModel())
    resp = await client.post("/v1/claude", headers=headers, json={"messages": _MESSAGES})
    assert resp.status_code == 413
    assert resp.json() == {"error": {"message": "Request body is too large."}}


async def test_lying_content_length_413(client, db_session, set_chat_model, monkeypatch) -> None:
    # The Content-Length fast-reject can be bypassed by a chunked request (no/absent
    # Content-Length), so the cap is ALSO enforced on the actually-received bytes:
    # `_read_capped_body` aborts to None and the route raises the 413 envelope
    # (router.py:288). Drive that path with a chunked body larger than the cap.
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
    # The drain's broad-except failure path (migrated _error → AppApiError): a pre-delta
    # upstream failure raises the explicit 500 ErrorEnvelope, never a half-open stream.
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
        await _stream(factory, user.id, TestModel(custom_output_text="x"), "hi", [], "", 64)
    assert exc_info.value.status_code == 500
    assert exc_info.value.message == "The model request failed."


def test_claude_openapi_documents_full_error_set() -> None:
    from src.main import create_app

    responses = create_app().openapi()["paths"]["/v1/claude"]["post"]["responses"]
    assert {"400", "401", "413", "429", "500", "503"} <= set(responses)
    # 429 documents the dedicated 5-key body; the explicit 500 documents the envelope
    # (overriding the v1 DetailBody 500 default).
    ref_429 = responses["429"]["content"]["application/json"]["schema"]["$ref"]
    assert ref_429.endswith("DailyTokenLimitBody")
    ref_500 = responses["500"]["content"]["application/json"]["schema"]["$ref"]
    assert ref_500.endswith("ErrorEnvelope")


async def test_not_configured_returns_503(client, db_session) -> None:
    # No model override → chat_model() returns None (Foundry unset in test) → 503.
    headers, _ = await _auth(db_session)
    resp = await client.post("/v1/claude", headers=headers, json={"messages": _MESSAGES})
    assert resp.status_code == 503
    assert resp.json() == {"error": {"message": "Claude client not configured."}}


async def test_requires_auth(client) -> None:
    resp = await client.post("/v1/claude", json={"messages": _MESSAGES})
    assert resp.status_code == 401
