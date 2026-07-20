"""`FunctionModel` scripting helpers (U4) — drive the build agent with a deterministic sequence
of model turns, each carrying its own `RequestUsage` so per-model-step metering is assertable
(KD-1). A run ends when the model returns a text turn (no tool call); a multi-run build is one
flat list of turns, the runs delimited by those text turns.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from pydantic_ai.messages import ModelMessage, ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import AgentInfo, FunctionModel
from pydantic_ai.usage import RequestUsage

DEFAULT_USAGE = RequestUsage(input_tokens=10, output_tokens=5)


def tool_turn(
    tool_name: str, args: dict[str, Any], *, usage: RequestUsage = DEFAULT_USAGE
) -> ModelResponse:
    """A model turn that calls one tool (the agent will execute it, then request the next turn)."""
    return ModelResponse(
        parts=[ToolCallPart(tool_name=tool_name, args=args, tool_call_id=f"call_{tool_name}")],
        usage=usage,
    )


def text_turn(content: str = "Done.", *, usage: RequestUsage = DEFAULT_USAGE) -> ModelResponse:
    """A text-only model turn — ends the current `agent.iter` run."""
    return ModelResponse(parts=[TextPart(content=content)], usage=usage)


def scripted_model(turns: Sequence[ModelResponse]) -> FunctionModel:
    """A `FunctionModel` that replays `turns` in order (one per model request), across as many
    `agent.iter` runs as the harness starts. Over-driving past the script yields a benign text
    turn so a run always terminates."""
    remaining = list(turns)
    cursor = {"i": 0}

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        i = cursor["i"]
        if i >= len(remaining):
            return ModelResponse(
                parts=[TextPart(content="(script exhausted)")],
                usage=RequestUsage(input_tokens=1, output_tokens=1),
            )
        cursor["i"] = i + 1
        return remaining[i]

    return FunctionModel(respond)
