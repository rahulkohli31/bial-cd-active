"""Foundry model wiring — the Foundry-only guard and the api_key build path."""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from types import SimpleNamespace
from typing import Any

# THE SDK'S VENDORED HTTPX, NOT THE ONE `src/` USES. The Anthropic client moved onto a fork
# (`httpx2`) in 1.x, so its transports and its request/response types are no longer the
# top-level `httpx` ones, and a plain `httpx.MockTransport` under its client is a type error.
# Declared in the `dev` group rather than reached through `anthropic._base_client`, which is a
# private re-export mypy and pyright both refuse. If the SDK ever vendors something else this
# fails loudly at the swap below, which is the right way for it to fail.
import httpx2
import pytest
from anthropic import APITimeoutError, AsyncAnthropic, AsyncAnthropicFoundry, Timeout
from anthropic.types import RawContentBlockDeltaEvent, TextBlock, TextDelta
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    TextPart,
    UserPromptPart,
)
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.models import anthropic as anthropic_models
from pydantic_ai.models.anthropic import AnthropicModel

from src.config import FoundryConfig
from src.services.agent import model as model_module
from src.services.agent.model import (
    FoundryOnlyError,
    InlineSystemPromptShapeError,
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


def _timeout_of(client: AsyncAnthropicFoundry) -> Timeout:
    # `client.timeout` is typed `float | Timeout | None`; narrow to the `Timeout` we passed —
    # the SDK's public re-export, which is what `build_foundry_client` constructs.
    assert isinstance(client.timeout, Timeout)
    return client.timeout


def test_guard_rejects_public_anthropic_api() -> None:
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
    model = build_foundry_model(_config())
    assert isinstance(model, AnthropicModel)


def test_api_key_client_applies_configured_timeout_and_retries() -> None:
    # The shared model client gets a FINITE, retried socket sourced from FoundryConfig, so a
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


# --- behavioral scenarios --------------------------------------------------
# The wiring tests above prove the config LANDS on the client; these prove the SDK machinery it
# configures actually behaves: a transient connection failure is retried through to success, a
# dead endpoint surfaces a catchable timeout instead of a hang, and a slow-but-alive stream is
# never mistaken for a failed one. Retry BACKOFF is zeroed via `INITIAL_RETRY_DELAY` so no test
# ever sleeps a real backoff; the two timeout tests run against a REAL localhost socket because a
# MockTransport ignores timeouts entirely — a MockTransport timeout test would pass no matter how
# broken the timeout wiring was.

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
    # A connection that fails on every attempt but the last must be retried through to a
    # completed call by the machinery `max_retries` configured — the count proves it happened.
    monkeypatch.setattr("anthropic._base_client.INITIAL_RETRY_DELAY", 0.0)
    client = build_foundry_client(_config(max_retries=2))
    attempts = 0

    def flaky(request: httpx2.Request) -> httpx2.Response:
        nonlocal attempts
        attempts += 1
        if attempts <= 2:
            raise httpx2.ConnectError("connection refused")
        return httpx2.Response(200, json=_A_COMPLETED_MESSAGE)

    # Swap the transport UNDER the built client so the SDK's own retry loop stays fully in
    # play; only the network is faked.
    client._client._transport = httpx2.MockTransport(flaky)
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
        # The guard already passed at build time; repoint at the local socket for the test.
        client.base_url = f"http://127.0.0.1:{port}"
        with pytest.raises(APITimeoutError):
            await client.messages.create(
                model="claude-opus", max_tokens=16, messages=[{"role": "user", "content": "hi"}]
            )
    assert connections == 2  # the timeout was retried once (max_retries=1), then surfaced


async def test_slow_but_alive_stream_survives_the_read_timeout() -> None:
    # The read timeout is a PER-CHUNK idle bound, not a whole-turn deadline. A streamed
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


# --- the widened inline-system-prompt exclusion -----------------------------------------
#
# The platform sends one turn-scoped system message at the tail of a request. The library
# excludes the Foundry client from serving those as `{'role': 'system'}` entries, and the
# fallback is not "drop it" — it is a rewrite into the citizen's own user message, or a hoist
# into the shared top-level system block. Both move bytes the cached prefix depends on, so this
# module widens the exclusion at import. What follows pins that it still works and that it
# still fails loudly when it stops working.

_A_TAIL_SENTENCE = "the app may have moved on."


def _system_capable_model(deployment: str = "claude-opus-5") -> AnthropicModel:
    """A real Foundry-client model, on a deployment name the profile recognises.

    The name matters as much as the client: the base flag is set per model family, so a
    deployment the profile does not know carries no inline-system capability whatever this
    module does to the exclusion."""
    return build_foundry_model(_config(deployment=deployment))


async def _wire(model: AnthropicModel, messages: list[Any]) -> tuple[Any, list[dict[str, Any]]]:
    """What the provider is actually handed, through the library's own two steps.

    `prepare_messages` is half the answer here rather than an implementation detail — it is the
    step that performs the `<system>`-tagged rewrite when the capability is absent, so a mapping
    that skipped it would show the tail sentence surviving on a transport that cannot serve it."""
    params = model.customize_request_parameters(ModelRequestParameters())
    prepared = model.prepare_messages(messages, params)
    system, entries = await model._map_message(prepared, params, {})  # noqa: SLF001 — pinned seam
    return system, [dict(entry) for entry in entries]


def _a_conversation_ending_in_a_tail_sentence() -> list[Any]:
    """Two turns and a system part at the very end — mid-conversation, not leading.

    The leading request's opening system parts are the run's own prompt and hoist to the
    top-level `system` field by design. Only a part that follows other messages is the thing
    under test, so the fixture has to have something before it."""
    return [
        ModelRequest(parts=[UserPromptPart(content="add a visitors chart")]),
        ModelResponse(parts=[TextPart(content="the chart is in.")]),
        ModelRequest(
            parts=[
                UserPromptPart(content="and a date filter"),
                SystemPromptPart(content=_A_TAIL_SENTENCE),
            ]
        ),
    ]


async def test_a_foundry_request_carries_the_tail_sentence_as_a_system_entry() -> None:
    """★ THE TRANSPORT, on the built request rather than on the setting that asks for it.

    The second half is the mutation, kept in the same test because it is the whole reason the
    first half is worth asserting: put the exclusion back and the sentence does not disappear —
    it reappears as `<system>`-tagged text inside the citizen's message, which is the outcome
    this override exists to prevent and the one a presence-only assertion would not notice."""
    _, entries = await _wire(_system_capable_model(), _a_conversation_ending_in_a_tail_sentence())

    assert entries[-1]["role"] == "system"
    assert entries[-1]["content"] == [{"text": _A_TAIL_SENTENCE, "type": "text"}]
    assert "<system>" not in json.dumps(entries)

    with pytest.MonkeyPatch.context() as restore:
        restore.setattr(
            anthropic_models,
            "_INLINE_SYSTEM_PROMPT_UNSUPPORTED_CLIENTS",
            (AsyncAnthropicFoundry,),
        )
        _, fallback = await _wire(
            _system_capable_model(), _a_conversation_ending_in_a_tail_sentence()
        )

    assert [entry for entry in fallback if entry["role"] == "system"] == []
    assert f"<system>{_A_TAIL_SENTENCE}</system>" in json.dumps(fallback)


async def test_a_deployment_the_profile_does_not_know_carries_no_system_entry() -> None:
    """The override is necessary but not sufficient, and the missing half is configuration.

    `FOUNDRY__DEPLOYMENT` is an operator-chosen name. Anthropic publishes the feature per model
    family, so a resource whose deployment is called something else gets no inline system
    capability at all — which is why the emitter asks the model rather than assuming."""
    _, entries = await _wire(
        _system_capable_model("our-house-model"), _a_conversation_ending_in_a_tail_sentence()
    )

    assert [entry for entry in entries if entry["role"] == "system"] == []


def test_the_shape_assertion_refuses_a_symbol_that_is_not_a_tuple_of_clients(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Asserted against a STAND-IN module, never by editing the real one.

    Three ways the symbol can stop being what this module patches, and all three have to raise
    rather than leave the patch quietly doing nothing: gone, renamed into something of another
    type, or still a tuple of client types that no longer names Foundry.

    THE MESSAGE IS MATCHED, not just the exception type. Both halves of the guard raise the same
    class, so `raises(...)` alone stays green when the symbol check is deleted — the derivation
    half fires instead and the test cannot tell the difference."""
    for broken in (None, "a tuple once", (AsyncAnthropic,)):
        stand_in = SimpleNamespace()
        if broken is not None:
            stand_in._INLINE_SYSTEM_PROMPT_UNSUPPORTED_CLIENTS = broken
        monkeypatch.setattr(model_module, "anthropic_models", stand_in)
        with pytest.raises(InlineSystemPromptShapeError, match="no longer a tuple"):
            model_module._widen_foundry_inline_system_prompts()


def test_the_shape_assertion_refuses_a_flag_that_no_longer_derives_from_the_tuple(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★ THE HALF THAT CATCHES AN INERT PATCH, which is the failure with no symptom.

    The tuple can keep its name, its type and its membership while the decision moves somewhere
    else — and then emptying it changes nothing, the sentence rides a channel it was never
    meant to, and every assertion about the symbol still passes. The stand-in here is
    well-formed on purpose; what makes it raise is that the real library's flag no longer
    answers to it."""
    stand_in = SimpleNamespace(_INLINE_SYSTEM_PROMPT_UNSUPPORTED_CLIENTS=(AsyncAnthropicFoundry,))
    monkeypatch.setattr(model_module, "anthropic_models", stand_in)

    with pytest.raises(InlineSystemPromptShapeError, match="no longer decides anything"):
        model_module._widen_foundry_inline_system_prompts()


def test_the_probe_ignores_a_foundry_base_url_in_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """★ THE PROBE MUST NOT BE ABLE TO STOP THE API FROM BOOTING.

    The Foundry client reads `ANTHROPIC_FOUNDRY_BASE_URL` whenever `base_url` is None, and then
    refuses a `base_url` and a `resource` together. Built with a `resource`, this probe therefore
    raises on any deployment that exports that variable — at import, so nothing serves, with a
    traceback naming a helper that has nothing to do with that variable.

    Mutation check: build the probe with `resource=...` again and this goes red with
    `base_url and resource are mutually exclusive`."""
    monkeypatch.setenv(
        "ANTHROPIC_FOUNDRY_BASE_URL", "https://someone-elses.services.ai.azure.com/anthropic/"
    )

    model = model_module._foundry_probe_model()

    assert "inline-system-probe" in str(model.client.base_url), (
        "the probe took its address from the environment, so it is not the address this "
        "module reasoned about"
    )
