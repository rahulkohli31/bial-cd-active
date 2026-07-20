"""Foundry model wiring — the Foundry-only (AE5) guard and the api_key build path (U12)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from typing import Any

import httpx
import pytest
from anthropic import APITimeoutError, AsyncAnthropicFoundry
from anthropic.types import RawContentBlockDeltaEvent, TextBlock, TextDelta
from pydantic_ai.models.anthropic import AnthropicModel

from src.config import FoundryConfig
from src.services.agent.model import (
    FoundryOnlyError,
    _assert_foundry_only,
    build_foundry_client,
    build_foundry_model,
)


def _config(**overrides) -> FoundryConfig:
    data = {
        "resource": "myfoundry",
        "deployment": "claude-opus",
        "auth_mode": "api_key",
        "api_key": "k",
    }
    data.update(overrides)
    return FoundryConfig.model_validate(data)


def _timeout_of(client: AsyncAnthropicFoundry) -> httpx.Timeout:
    # `client.timeout` is typed `float | Timeout | None`; narrow to the httpx.Timeout we passed.
    assert isinstance(client.timeout, httpx.Timeout)
    return client.timeout


def test_guard_rejects_public_anthropic_api() -> None:
    # AE5: the public API endpoint is refused, fail-closed.
    with pytest.raises(FoundryOnlyError):
        _assert_foundry_only("https://api.anthropic.com/v1")


def test_guard_rejects_non_foundry_host() -> None:
    with pytest.raises(FoundryOnlyError):
        _assert_foundry_only("https://example.com/anthropic")


def test_guard_accepts_foundry_host() -> None:
    _assert_foundry_only("https://myfoundry.services.ai.azure.com/anthropic/")  # no raise


def test_build_client_targets_foundry() -> None:
    client = build_foundry_client(_config())
    base_url = str(client.base_url)
    assert ".services.ai.azure.com" in base_url
    assert "api.anthropic.com" not in base_url


def test_build_model_from_api_key_config() -> None:
    # AE5: a valid Foundry config builds an AnthropicModel (Foundry-backed).
    model = build_foundry_model(_config())
    assert isinstance(model, AnthropicModel)


def test_api_key_client_applies_configured_timeout_and_retries() -> None:
    # U8: the shared model client gets a FINITE, retried socket sourced from FoundryConfig, so a
    # dead server→model connection surfaces as a catchable timeout instead of a hang. Custom
    # values prove the wiring (config → SDK client), not just that a default happened to match.
    client = build_foundry_client(
        _config(read_timeout_s=99.0, connect_timeout_s=7.0, max_retries=4)
    )
    assert client.max_retries == 4
    timeout = _timeout_of(client)
    assert timeout.read == 99.0  # per-chunk idle bound on the streamed response
    assert timeout.connect == 7.0


def test_client_defaults_are_finite_and_retry_modest() -> None:
    # Out of the box the socket is already finite (never an unbounded hang) and retry-modest.
    client = build_foundry_client(_config())
    assert client.max_retries == 2
    timeout = _timeout_of(client)
    assert timeout.read == 120.0
    assert timeout.connect == 10.0


def test_entra_client_also_applies_timeout_and_retries(monkeypatch: pytest.MonkeyPatch) -> None:
    # The PRODUCTION path is managed-identity (entra), not api_key — patching only the api_key
    # branch would leave prod on an untuned socket. Stub the Azure credential + token provider
    # (never invoked at construction) so this exercises the real else-branch wiring.
    import azure.identity

    monkeypatch.setattr(azure.identity, "DefaultAzureCredential", lambda *a, **k: object())
    monkeypatch.setattr(azure.identity, "get_bearer_token_provider", lambda *a, **k: lambda: "t")
    client = build_foundry_client(
        _config(
            auth_mode="entra",
            api_key=None,
            read_timeout_s=99.0,
            connect_timeout_s=7.0,
            max_retries=4,
        )
    )
    assert client.max_retries == 4
    timeout = _timeout_of(client)
    assert timeout.read == 99.0
    assert timeout.connect == 7.0


# --- U8 behavioral scenarios --------------------------------------------------
# The wiring tests above prove the config LANDS on the client; these prove the SDK machinery it
# configures actually behaves: a transient connection failure is retried through to success, a
# dead endpoint surfaces a catchable timeout instead of a hang, and a slow-but-alive stream is
# never falsely aborted (the false-BuildResult(FAILED) scenario). Retry BACKOFF is zeroed via
# `INITIAL_RETRY_DELAY` so no test ever sleeps a real backoff; the two timeout tests run against
# a REAL localhost socket because `httpx.MockTransport` ignores timeouts entirely — a MockTransport
# timeout test would pass no matter how broken the timeout wiring was.

_A_COMPLETED_MESSAGE: dict[str, Any] = {
    "id": "msg_test",
    "type": "message",
    "role": "assistant",
    "model": "claude-opus",
    "content": [{"type": "text", "text": "made it"}],
    "stop_reason": "end_turn",
    "stop_sequence": None,
    "usage": {"input_tokens": 1, "output_tokens": 1},
}


def _sse(event: str, payload: dict[str, Any]) -> bytes:
    return f"event: {event}\ndata: {json.dumps(payload)}\n\n".encode()


# A minimal, complete Anthropic streaming turn ("hello" + " world"), dribbled chunk by chunk.
_SSE_TURN: tuple[bytes, ...] = (
    _sse(
        "message_start",
        {
            "type": "message_start",
            "message": {**_A_COMPLETED_MESSAGE, "content": [], "stop_reason": None},
        },
    ),
    _sse(
        "content_block_start",
        {"type": "content_block_start", "index": 0, "content_block": {"type": "text", "text": ""}},
    ),
    _sse(
        "content_block_delta",
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": "hello"},
        },
    ),
    _sse(
        "content_block_delta",
        {
            "type": "content_block_delta",
            "index": 0,
            "delta": {"type": "text_delta", "text": " world"},
        },
    ),
    _sse("content_block_stop", {"type": "content_block_stop", "index": 0}),
    _sse(
        "message_delta",
        {
            "type": "message_delta",
            "delta": {"stop_reason": "end_turn", "stop_sequence": None},
            "usage": {"output_tokens": 2},
        },
    ),
    _sse("message_stop", {"type": "message_stop"}),
)

_ConnectionHandler = Callable[[asyncio.StreamReader, asyncio.StreamWriter], Awaitable[None]]


@contextlib.asynccontextmanager
async def _local_server(handler: _ConnectionHandler) -> AsyncIterator[int]:
    """A real localhost HTTP socket (yields its port) so the configured httpx timeouts bind."""
    server = await asyncio.start_server(handler, "127.0.0.1", 0)
    try:
        yield server.sockets[0].getsockname()[1]
    finally:
        server.close()
        await server.wait_closed()


async def _read_http_request(reader: asyncio.StreamReader) -> None:
    """Consume one HTTP request (headers + Content-Length body) so the reply can be written."""
    header = await reader.readuntil(b"\r\n\r\n")
    length = re.search(rb"content-length:\s*(\d+)", header, re.IGNORECASE)
    if length:
        await reader.readexactly(int(length.group(1)))


async def test_transient_connection_errors_are_retried_to_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # U8(a): a connection that fails on every attempt but the last must be retried through to a
    # completed call by the machinery `max_retries` configured — the count proves it happened.
    monkeypatch.setattr("anthropic._base_client.INITIAL_RETRY_DELAY", 0.0)
    client = build_foundry_client(_config(max_retries=2))
    attempts = 0

    def flaky(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            raise httpx.ConnectError("connection refused")
        return httpx.Response(200, json=_A_COMPLETED_MESSAGE)

    # Swap the transport UNDER the built client so the SDK's own retry loop (the thing U8 tuned)
    # stays fully in play; only the network is faked.
    client._client._transport = httpx.MockTransport(flaky)
    msg = await client.messages.create(
        model="claude-opus", max_tokens=16, messages=[{"role": "user", "content": "hi"}]
    )
    assert attempts == 3  # max_retries + 1 — the retries actually happened
    block = msg.content[0]
    assert isinstance(block, TextBlock)
    assert block.text == "made it"


async def test_dead_endpoint_surfaces_a_catchable_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # U8(b): an endpoint that accepts the socket but never answers must become a catchable
    # `APITimeoutError` within the configured read-timeout budget (retries exhausted) — the
    # wedged server→model connection the module docstring promises never hangs.
    monkeypatch.setattr("anthropic._base_client.INITIAL_RETRY_DELAY", 0.0)
    connections = 0

    async def black_hole(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        nonlocal connections
        connections += 1
        with contextlib.suppress(Exception):
            await reader.read()  # swallow the request, answer nothing, wait for the client to quit
        writer.close()

    async with _local_server(black_hole) as port:
        client = build_foundry_client(
            _config(read_timeout_s=0.1, connect_timeout_s=1.0, max_retries=1)
        )
        # The AE5 guard already passed at build time; repoint at the local socket for the test.
        client.base_url = f"http://127.0.0.1:{port}"
        with pytest.raises(APITimeoutError):
            await client.messages.create(
                model="claude-opus", max_tokens=16, messages=[{"role": "user", "content": "hi"}]
            )
    assert connections == 2  # the timeout was retried once (max_retries=1), then surfaced


async def test_slow_but_alive_stream_survives_the_read_timeout() -> None:
    # U8(c): the read timeout is a PER-CHUNK idle bound, not a whole-turn deadline. A streamed
    # turn whose chunks each land inside the window must complete even though the WHOLE response
    # (7 chunks × 0.05 s ≈ 0.35 s) takes longer than read_timeout_s — the false-FAILED guard.
    async def dribble(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        with contextlib.suppress(Exception):
            await _read_http_request(reader)
            writer.write(
                b"HTTP/1.1 200 OK\r\ncontent-type: text/event-stream\r\nconnection: close\r\n\r\n"
            )
            for chunk in _SSE_TURN:
                await asyncio.sleep(0.05)  # per-chunk gap comfortably UNDER the 0.25 s read bound
                writer.write(chunk)
                await writer.drain()
        writer.close()

    async with _local_server(dribble) as port:
        client = build_foundry_client(
            _config(read_timeout_s=0.25, connect_timeout_s=1.0, max_retries=0)
        )
        client.base_url = f"http://127.0.0.1:{port}"
        stream = await client.messages.create(
            model="claude-opus",
            max_tokens=16,
            messages=[{"role": "user", "content": "hi"}],
            stream=True,
        )
        text = ""
        async for event in stream:
            if isinstance(event, RawContentBlockDeltaEvent) and isinstance(event.delta, TextDelta):
                text += event.delta.text
    assert text == "hello world"  # completed — no mid-stream timeout abort
