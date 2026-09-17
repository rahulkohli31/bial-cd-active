"""The cache: the breakpoints we spend, and the numbers we read back.

WHY THIS FILE EXISTS
Anthropic allows four cache breakpoints per request and answers a fifth with an HTTP 400. Our
requests spend them like this, and the arithmetic is the reason a second marker inside long
build turns is NOT shipped:

| slot | who takes it |
|---|---|
| 1 | the server-applied breakpoint automatic caching adds (`anthropic_cache`, live on Foundry) |
| 2 | the tool definitions (`anthropic_cache_tool_definitions`) |
| 3 | the instructions (`anthropic_cache_instructions`) |
| 4 | the pin in front of the turn-scoped system message |

With automatic caching on, the library caps EXPLICIT breakpoints at three rather than four,
because the server-applied one has already consumed a slot. Two of those three are spent before
any message is looked at, so exactly one message-slot breakpoint survives — the pin's. Anything
else that wants a marker inside the messages is competing for a slot that is already taken.

AND THE COMPETITION IS SILENT, which is the whole danger. `_limit_cache_points` does not raise
when the budget is exceeded: it walks the messages newest-first and DELETES the excess from the
older ones. An emitter that placed a second marker earlier in the conversation would look
correct at every point a test normally looks — the marker is in the message list — while the
request on the wire carried nothing there at all.

Every assertion here is therefore on the request as the adapter finishes building it, never on
the setting that asked for a marker or on the list the emitter handed over.

The last test is about the other half — what comes BACK. Whether one event is logged per model
call rather than per run is asserted where it can only be answered honestly, in the live gate at
`tests/e2e/test_cache_gate_live.py`; what is asserted here is the part a live run cannot tell
you, which is whether the number in that event is the provider's or a derived one.
"""

from __future__ import annotations

from typing import Any, cast

from pydantic_ai.messages import (
    CachePoint,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    UserPromptPart,
)
from pydantic_ai.models import ModelRequestContext, ModelRequestParameters
from pydantic_ai.models.anthropic import AnthropicModel, AnthropicModelSettings
from pydantic_ai.usage import RequestUsage

from src.config import FoundryConfig
from src.services.agent.capabilities import TurnScopedSystemMessage
from src.services.agent.model import build_foundry_model
from src.services.orchestrator.constants import CACHE_TTL
from src.services.turns.engine import _raw_cache_tokens

# The three cache settings both model call sites pass, verbatim. Read from the same constant the
# engine reads, so a TTL change moves this file with it rather than past it.
_PRODUCTION_CACHE_SETTINGS = AnthropicModelSettings(
    anthropic_cache_instructions=CACHE_TTL,
    anthropic_cache_tool_definitions=CACHE_TTL,
    anthropic_cache=CACHE_TTL,
)


def _foundry_model() -> AnthropicModel:
    """Production's own wiring: a Foundry client on the configured model family."""
    return build_foundry_model(
        FoundryConfig.model_validate(
            {
                "resource": "myfoundry",
                "deployment": "claude-opus-5",
                "auth_mode": "api_key",
                "api_key": "k",
            }
        )
    )


async def _appended_by_the_emitter(
    model: AnthropicModel, messages: list[ModelMessage]
) -> list[ModelMessage]:
    """Run the real capability over `messages` and hand back what it will send.

    THE EMITTER RATHER THAN A HAND-BUILT COPY OF ITS OUTPUT. A fixture that spelled out the pin
    and the sentence itself would keep passing after the pin was dropped from the emitter, which
    is exactly the regression this file is here to catch."""
    request_context = ModelRequestContext(
        model=model,
        messages=list(messages),
        model_settings=_PRODUCTION_CACHE_SETTINGS,
        model_request_parameters=ModelRequestParameters(),
    )

    async def _handler(_ctx: ModelRequestContext) -> ModelResponse:
        return ModelResponse(parts=[TextPart(content="noted.")])

    capability = TurnScopedSystemMessage(should_send=lambda: True, on_sent=lambda: None)
    await capability.wrap_model_request(
        cast(Any, None), request_context=request_context, handler=_handler
    )
    return request_context.messages


async def _a_turn_ending_in_the_pinned_sentence(model: AnthropicModel) -> list[ModelMessage]:
    """Two turns, with the emitter's tail on the last request."""
    return await _appended_by_the_emitter(
        model,
        [
            ModelRequest(parts=[UserPromptPart(content="add a visitors chart")]),
            ModelResponse(parts=[TextPart(content="the chart is in.")]),
            ModelRequest(parts=[UserPromptPart(content="and a date filter")]),
        ],
    )


async def _entries(model: AnthropicModel, messages: list[ModelMessage]) -> list[dict[str, Any]]:
    params = model.customize_request_parameters(ModelRequestParameters())
    prepared = model.prepare_messages(messages, params)
    _, entries = await model._map_message(  # noqa: SLF001 — pinned-version seam
        prepared, params, _PRODUCTION_CACHE_SETTINGS
    )
    return [dict(entry) for entry in entries]


def _marked_blocks(entries: list[dict[str, Any]]) -> list[tuple[str, int]]:
    """Every block carrying a `cache_control`, as (role, index within its entry)."""
    marked: list[tuple[str, int]] = []
    for entry in entries:
        content = entry["content"]
        if isinstance(content, str):
            continue
        for index, block in enumerate(cast(list[dict[str, Any]], content)):
            if "cache_control" in block:
                marked.append((str(entry["role"]), index))
    return marked


async def test_automatic_caching_is_on_for_foundry_and_costs_a_slot() -> None:
    """The fact the whole budget rests on, taken from the adapter rather than assumed.

    `anthropic_cache` is automatic, server-applied caching, and Foundry is one of the transports
    that supports it — so the parameter goes on the request and the library drops the explicit
    allowance from four to three. Were this to become a per-block fallback instead (as it is on
    Bedrock and Vertex), the arithmetic below would be a different arithmetic."""
    model = _foundry_model()
    top_level, ttl = model._build_automatic_cache_control(  # noqa: SLF001 — pinned-version seam
        _PRODUCTION_CACHE_SETTINGS
    )

    assert top_level == {"type": "ephemeral", "ttl": CACHE_TTL}
    assert ttl == CACHE_TTL


async def test_the_pin_marks_the_block_before_the_sentence_and_not_the_sentence() -> None:
    """★ WHERE THE PIN LANDS, which is the only placement that does any good.

    A breakpoint that covered the sentence would cache a prefix ending in it, and the next
    request of the turn — which no longer carries the sentence — could not read that back. The
    marker therefore sits on the last block of the user content, so the cached prefix ends
    exactly where the sentence begins and every later request in the turn still hits it."""
    model = _foundry_model()
    entries = await _entries(model, await _a_turn_ending_in_the_pinned_sentence(model))

    assert entries[-1]["role"] == "system", "the sentence is not the last thing on the request"
    assert _marked_blocks(entries) == [("user", 0)]
    last_user = next(entry for entry in reversed(entries) if entry["role"] == "user")
    assert last_user["content"][0]["text"] == "and a date filter"


async def test_a_second_message_breakpoint_is_deleted_without_a_word() -> None:
    """★ THE GO/NO-GO FOR AN INTERMEDIATE MARKER INSIDE LONG BUILD TURNS.

    With the server-applied breakpoint, the tool definitions and the instructions accounted for,
    one message slot remains. This puts two markers in the messages — an earlier one, as a
    long-turn marker would, and the pin at the tail — and asks the adapter what it sends.

    It sends one. The older marker is removed, not reported, so an emitter placing it would pass
    every assertion made about its own output while the request carried no breakpoint there.
    That is why the intermediate marker cannot be added on top of the pin: the budget it needs
    is already spent, and the failure mode is silence."""
    model = _foundry_model()
    conversation: list[ModelMessage] = [
        ModelRequest(parts=[UserPromptPart(content="add a visitors chart")]),
        ModelResponse(parts=[TextPart(content="the chart is in.")]),
        # Where a long-turn marker would go: mid-conversation, walking forward as the turn runs.
        ModelRequest(
            parts=[
                UserPromptPart(content="now the filter"),
                UserPromptPart(content=[CachePoint(ttl=CACHE_TTL)]),
            ]
        ),
        ModelResponse(parts=[TextPart(content="on it.")]),
        *(await _a_turn_ending_in_the_pinned_sentence(model))[2:],
    ]
    entries = await _entries(model, conversation)
    assert len(_marked_blocks(entries)) == 2, "the fixture did not place two markers to begin with"

    # What the instructions and the tool definitions contribute, as the adapter would have them:
    # one marked system block and one marked tool. Spelled out rather than mapped, because the
    # question here is the BUDGET, and the budget only cares how many arrive marked.
    system_prompt = [
        {"type": "text", "text": "the standing contract"},
        {"type": "text", "text": "the examples", "cache_control": {"type": "ephemeral"}},
    ]
    tools = [{"name": "check_the_app", "cache_control": {"type": "ephemeral"}}]
    model._limit_cache_points(  # noqa: SLF001 — pinned-version seam
        cast(Any, system_prompt), cast(Any, entries), cast(Any, tools), automatic_caching=True
    )

    assert _marked_blocks(entries) == [("user", 0)], (
        "more than the pin survived the budget — the arithmetic this file records has changed"
    )
    surviving = next(entry for entry in reversed(entries) if entry["role"] == "user")
    assert surviving["content"][0]["text"] == "and a date filter", (
        "the marker that survived is not the pin at the tail"
    )


async def test_the_logged_counters_are_the_providers_own_and_not_the_normalised_ones() -> None:
    """★ WHICH NUMBER IS BEING READ, on the one construction where it matters.

    pydantic-ai's `cache_read_tokens` is a normalised figure and `input_tokens` already folds
    the cache classes into itself, so on a healthy cached request the normalised numbers say
    "lots of input" whatever the cache did. The raw fields the provider returned are in
    `details`, and they are the only ones that answer the question this whole track asks.

    The fixture drives them apart deliberately: a reader that reached for the normalised
    attributes would come back with 9s, and one that reached for the raw keys comes back with
    the numbers the provider actually sent."""
    usage = RequestUsage(
        input_tokens=9,
        cache_read_tokens=9,
        cache_write_tokens=9,
        details={
            "input_tokens": 12,
            "cache_creation_input_tokens": 4_096,
            "cache_read_input_tokens": 90_000,
        },
    )

    assert _raw_cache_tokens(usage) == {
        "input_tokens": 12,
        "cache_creation_input_tokens": 4_096,
        "cache_read_input_tokens": 90_000,
    }


def test_a_response_that_reports_no_caching_reads_as_zero_rather_than_missing() -> None:
    """Anthropic omits the cache keys entirely on a request that neither wrote nor read one.

    Defaulted rather than required, because the gate's arithmetic runs over every call and one
    uncached call must not take the measurement down with it."""
    assert _raw_cache_tokens(RequestUsage(input_tokens=5, details={"input_tokens": 5})) == {
        "input_tokens": 5,
        "cache_creation_input_tokens": 0,
        "cache_read_input_tokens": 0,
    }
