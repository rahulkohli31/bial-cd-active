"""POST /v1/claude — daily gate (429 pre-stream), SSE frame shape, atomic billing, multimodal
(U13). The Foundry model is a TestModel (no network); billing is bound to the test session.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib

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
    assert body["error"]["code"] == "daily_token_limit_exceeded"
    assert body["error"]["limit"] == 10


# --- SSE stream + billing -----------------------------------------------------


async def test_stream_emits_delta_and_done_frames(client, db_session, set_chat_model) -> None:
    headers, _ = await _auth(db_session)
    set_chat_model(TestModel(custom_output_text="hello world"))
    resp = await client.post("/v1/claude", headers=headers, json={"messages": _MESSAGES})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/event-stream")
    # Exact Express wire frames: compact delta JSON + the [DONE] sentinel.
    assert 'data: {"delta":{"text":"hello world"}}\n\n' in resp.text
    assert resp.text.endswith("data: [DONE]\n\n")


async def test_usage_billed_after_stream(client, db_session, set_chat_model) -> None:
    headers, user = await _auth(db_session)
    set_chat_model(TestModel(custom_output_text="billed"))
    resp = await client.post("/v1/claude", headers=headers, json={"messages": _MESSAGES})
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
    from typing import Any, cast

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
    gen = cast(
        "AsyncGenerator[bytes]",
        _stream(factory, user.id, TestModel(custom_output_text="a b c d e"), "hi", [], "", 64),
    )
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
    resp = await client.post("/v1/claude", headers=headers, json={"messages": messages})
    assert resp.status_code == 200
    assert 'data: {"delta":{"text":"saw the image"}}\n\n' in resp.text


# --- validation + config ------------------------------------------------------


async def test_empty_messages_400(client, db_session, set_chat_model) -> None:
    headers, _ = await _auth(db_session)
    set_chat_model(TestModel())
    resp = await client.post("/v1/claude", headers=headers, json={"messages": []})
    assert resp.status_code == 400


async def test_not_configured_returns_503(client, db_session) -> None:
    # No model override → chat_model() returns None (Foundry unset in test) → 503.
    headers, _ = await _auth(db_session)
    resp = await client.post("/v1/claude", headers=headers, json={"messages": _MESSAGES})
    assert resp.status_code == 503
    assert resp.json() == {"error": {"message": "Claude client not configured."}}


async def test_requires_auth(client) -> None:
    resp = await client.post("/v1/claude", json={"messages": _MESSAGES})
    assert resp.status_code == 401
