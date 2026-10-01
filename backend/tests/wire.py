"""Would the provider accept this history? The check a scripted model cannot make.

`FunctionModel` takes any message list, and pydantic-ai repairs a dangling call on its own before
each request, so a turn that completes proves nothing about the history it was handed. This
states the provider's rules directly: once consecutive same-role messages are merged, every tool
call is answered exactly once in the very next message, those answers lead that message, and no
answer is left without its call.
"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    RetryPromptPart,
    ToolCallPart,
    ToolReturnPart,
)


def _is_tool_answer(part: object) -> bool:
    # A nameless retry prompt is sent as plain user text, not as a tool result.
    return isinstance(part, ToolReturnPart) or (
        isinstance(part, RetryPromptPart) and part.tool_name is not None
    )


def _merged_roles(history: Sequence[ModelMessage]) -> list[tuple[bool, list[object]]]:
    """`(is_user, parts)` per provider message, with consecutive same-role messages merged."""
    merged: list[tuple[bool, list[object]]] = []
    for message in history:
        is_user = isinstance(message, ModelRequest)
        if merged and merged[-1][0] == is_user:
            merged[-1][1].extend(message.parts)
        else:
            merged.append((is_user, list(message.parts)))
    return merged


def assert_wire_valid(history: Sequence[ModelMessage], *, sending: bool = False) -> None:
    """`sending`: this is a request about to go out, so it must also end with the user."""
    assert history, "an empty history cannot be sent"
    assert isinstance(history[0], ModelRequest), "a conversation opens with the user"
    if sending:
        assert isinstance(history[-1], ModelRequest), "a request ends with the user"
    call_ids = [
        part.tool_call_id
        for message in history
        if isinstance(message, ModelResponse)
        for part in message.parts
        if isinstance(part, ToolCallPart)
    ]
    assert len(call_ids) == len(set(call_ids)), f"a tool call id is used twice: {call_ids}"

    merged = _merged_roles(history)
    for index, (is_user, parts) in enumerate(merged):
        previous_calls = (
            sorted(p.tool_call_id for p in merged[index - 1][1] if isinstance(p, ToolCallPart))
            if index > 0
            else []
        )
        if not is_user:
            calls = [p for p in parts if isinstance(p, ToolCallPart)]
            assert not calls or index + 1 < len(merged), (
                f"message {index} ends the history with unanswered calls "
                f"{[c.tool_call_id for c in calls]}"
            )
            continue
        answers = [p for p in parts if _is_tool_answer(p)]
        answered = sorted(getattr(p, "tool_call_id", "") for p in answers)
        assert answered == previous_calls, (
            f"message {index} answers {answered} but the message before it called {previous_calls}"
        )
        assert parts[: len(answers)] == answers, f"message {index}: tool answers must lead it"
