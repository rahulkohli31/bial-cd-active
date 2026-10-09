"""What a reply ended at a ceiling leaves to store: its own steps, and none that would brick the
chat on replay."""

from __future__ import annotations

from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

from src.services.turns.engine import _steps_of_the_cut_run


def _prompt(text: str) -> ModelRequest:
    return ModelRequest(parts=[UserPromptPart(content=text)])


def _call(call_id: str, *before: TextPart | ThinkingPart) -> ModelResponse:
    return ModelResponse(
        parts=[*before, ToolCallPart(tool_name="run_python", args={}, tool_call_id=call_id)]
    )


def _answer(call_id: str) -> ModelRequest:
    return ModelRequest(
        parts=[ToolReturnPart(tool_name="run_python", content="exit 0", tool_call_id=call_id)]
    )


def test_only_what_follows_the_runs_own_prompt_is_kept() -> None:
    called, answered = _call("b"), _answer("b")
    earlier: list[ModelMessage] = [
        _prompt("first"),
        _call("a"),
        _answer("a"),
        ModelResponse(parts=[TextPart("ok")]),
    ]
    captured: list[ModelMessage] = [*earlier, _prompt("second"), called, answered]

    assert _steps_of_the_cut_run(captured) == [called, answered]


def test_requests_merged_ahead_of_the_prompt_do_not_shift_where_the_run_begins() -> None:
    called, answered = _call("c"), _answer("c")
    merged = ModelRequest(parts=[UserPromptPart("tried once"), UserPromptPart("tried twice")])
    captured: list[ModelMessage] = [merged, _prompt("third"), called, answered]

    assert _steps_of_the_cut_run(captured) == [called, answered]


def test_a_call_nobody_answered_goes_and_the_text_beside_it_stays() -> None:
    called = _call("d", TextPart("Let me add these up."))

    (kept,) = _steps_of_the_cut_run([_prompt("q"), called])

    assert isinstance(kept, ModelResponse)
    assert kept.parts == [TextPart("Let me add these up.")]


def test_a_response_left_with_nothing_or_only_thinking_goes() -> None:
    captured: list[ModelMessage] = [
        _prompt("q"),
        _call("e"),
        _call("f", ThinkingPart("adding the column")),
    ]

    assert _steps_of_the_cut_run(captured) == []


def test_an_empty_interrupted_request_goes() -> None:
    called, answered = _call("g"), _answer("g")
    captured: list[ModelMessage] = [
        _prompt("q"),
        called,
        answered,
        ModelRequest(parts=[], state="interrupted"),
    ]

    assert _steps_of_the_cut_run(captured) == [called, answered]


def test_a_run_that_never_sent_its_prompt_stores_nothing() -> None:
    assert _steps_of_the_cut_run([]) == []
