"""The window measurement — what it counts, and what it must never be confused with.

The unit under test answers ONE question: how much of the model's context window will this
turn's prompt occupy. The failure this whole guardrail exists to fix was that nobody was
asking it; the failure that would replace it is asking it with the billing helpers, which
discount a cached prefix to a tenth and would report a full conversation as an empty one.
"""

from __future__ import annotations

import dataclasses
from typing import Any, cast

import pytest
from pydantic_ai import BinaryContent
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

from src.services.usage.context_window import (
    CHARS_PER_TOKEN,
    NOMINAL_BINARY_TOKENS,
    SYSTEM_PROMPT_RESERVE,
    occupied_window,
)
from src.services.usage.gate import weighted_spend


def _user(text: str) -> ModelRequest:
    return ModelRequest(parts=[UserPromptPart(content=text)])


def _assistant(text: str) -> ModelResponse:
    return ModelResponse(parts=[TextPart(content=text)])


def test_an_empty_conversation_is_the_reserve_and_nothing_else() -> None:
    # The floor is not zero: the system prompt this cannot see is still going to be there.
    assert occupied_window([], None) == SYSTEM_PROMPT_RESERVE


def test_prose_counts_at_four_characters_to_the_token() -> None:
    history: list[ModelMessage] = [_user("a" * 400), _assistant("b" * 800)]
    assert occupied_window(history, None) == SYSTEM_PROMPT_RESERVE + 100 + 200


def test_the_message_about_to_be_sent_counts_too() -> None:
    # Measured BEFORE it is persisted, so it is not in the history — counting only the history
    # would let the one message that pushes a conversation over the edge through every time.
    before = occupied_window([_user("x" * 400)], None)
    after = occupied_window([_user("x" * 400)], "y" * 4_000)
    assert after - before == 1_000


def test_a_regenerate_counts_its_prompt_once() -> None:
    # A regenerate replays the trailing request rather than sending a new one, so the route
    # passes `prompt=None`. Charging a phantom second copy would refuse a retry the original
    # turn was allowed.
    history = [_user("q" * 4_000)]
    assert occupied_window(history, None) == SYSTEM_PROMPT_RESERVE + 1_000


def test_a_binary_is_a_flat_charge_not_its_byte_length() -> None:
    """★ The over-count that would refuse every conversation containing a photo.

    A 3 MB image base64s to ~4 M characters. Counted as text that is a million tokens, and no
    conversation carrying one could ever start. Mutation check: delete the `BinaryContent` arm
    in `_tokens_in` so the generic dataclass walk descends into `.data`, and this goes red."""
    big = BinaryContent(data=b"\x89PNG" + b"\x00" * 3_000_000, media_type="image/png")
    history = [ModelRequest(parts=[UserPromptPart(content=["look at this", big])])]

    measured = occupied_window(history, None)

    prose = -(-len("look at this") // CHARS_PER_TOKEN)
    assert measured == SYSTEM_PROMPT_RESERVE + NOMINAL_BINARY_TOKENS + prose
    # And emphatically not the byte length, in either direction.
    assert measured < 10_000


def test_tool_traffic_occupies_the_window_like_anything_else() -> None:
    """A Build turn's window is mostly tool calls and their results. A measure that saw only
    prose would read a 180k build conversation as a few thousand tokens — which is exactly the
    conversation this guardrail is for."""
    prose: list[ModelMessage] = [_user("hi"), _assistant("hello")]
    prose_only = occupied_window(prose, None)
    with_tools = occupied_window(
        [
            _user("hi"),
            ModelResponse(parts=[ToolCallPart(tool_name="read_file", args={"path": "p" * 4_000})]),
            ModelRequest(
                parts=[
                    ToolReturnPart(
                        tool_name="read_file", content="f" * 8_000, tool_call_id="call-1"
                    )
                ]
            ),
            _assistant("hello"),
        ],
        None,
    )
    assert with_tools - prose_only >= 3_000


def test_an_unknown_part_shape_is_still_counted() -> None:
    """The walk is structural, not a per-part-type table, and this is why.

    pydantic-ai's part union grows. A table would stop counting whatever it did not recognise,
    and under-counting is the direction that HURTS — it is what lets an over-long conversation
    past the guard. A dataclass this module has never heard of is still measured."""

    @dataclasses.dataclass
    class SomeFuturePart:
        content: str

    # An unknown shape sitting where a part would; the walk descends by structure, so it is
    # measured without this module ever having heard of it.
    parts = cast(Any, [TextPart(content="short"), SomeFuturePart(content="z" * 4_000)])
    response = ModelResponse(parts=parts)

    assert occupied_window([response], None) >= SYSTEM_PROMPT_RESERVE + 1_000


def test_the_window_is_not_the_bill_and_cache_is_the_reason() -> None:
    """★ KTD-2, and the mutation that must go red.

    A long conversation is served to the model with most of its prompt read from cache. The
    BILL for that turn is tiny — `weighted_spend` discounts a cache read to a tenth, correctly,
    because that is what it costs. The WINDOW is full regardless: every one of those tokens is
    in the prompt.

    So the two numbers must disagree, and by a lot. Route the window check through the spend
    helper and a conversation at 190,000 reports as ~30,000 — the guardrail never fires, the
    administrator's number means nothing, and this test is the only thing that notices.
    """
    # ~150k tokens of conversation: the shape that would be almost entirely cache-read.
    history: list[ModelMessage] = [_user("a" * 300_000), _assistant("b" * 300_000)]

    occupancy = occupied_window(history, None)
    assert occupancy > 150_000

    # What the same turn would be BILLED, with 90% of its prompt served from cache.
    billed = weighted_spend(
        input_tokens=150_000,
        output_tokens=0,
        cache_read_tokens=135_000,
        cache_write_tokens=0,
    )
    assert billed < 30_000
    # Not "different by rounding" — different by an order of magnitude. A window measured with
    # the billing weights would be under a 200k limit while the real prompt was over it.
    assert occupancy > billed * 4


@pytest.mark.parametrize("chars", [0, 1, 3, 4, 5])
def test_a_partial_token_rounds_up(chars: int) -> None:
    # Floor division would report a 3-character message as zero tokens. Harmless once;
    # systematic across thousands of small tool returns it is a real under-count.
    expected = -(-chars // CHARS_PER_TOKEN)
    assert occupied_window([_user("x" * chars)], None) == SYSTEM_PROMPT_RESERVE + expected
