"""Optional flight-recorder for the build harness — white-box agent observability (dev/E2E only).

A build's live progress already streams as C7 `step` envelopes over SSE, but that feed omits
`read_file`, carries redacted labels instead of raw tool arguments, and is not durable past the
~5-minute post-terminal retention window. When the `BRAIN_TRACE_DIR` environment variable is set,
this module records the FULL agent path to `<BRAIN_TRACE_DIR>/<app_id>.jsonl`: every model step's
tool calls (name + raw args, `read_file` included) and, per run, the complete pydantic-ai message
history (every tool call, every tool return, and the model's own reasoning). It also trace-logs
each tool call through structlog so a build can be tailed live from stdout.

OFF by default: with no env var there is no directory, no file write, and no log line, so the
production path is byte-for-byte unchanged. This is test instrumentation, never a prod feature —
which is why every entry point fails soft (a trace error is logged and swallowed, mirroring the
harness's best-effort `_record_step`): observability must never be able to fail a real build.
"""

from __future__ import annotations

import json
import os
import uuid
from pathlib import Path
from typing import Any

import structlog
from pydantic_ai.messages import (
    ModelMessage,
    ModelMessagesTypeAdapter,
    ModelResponse,
    ToolCallPart,
)

logger = structlog.get_logger(__name__)

_TRACE_DIR_ENV = "BRAIN_TRACE_DIR"
_ARGS_LOG_MAX_CHARS = 800


def _sink_dir() -> Path | None:
    """The trace directory when tracing is on, else None. Created lazily so the whole feature
    stays inert until an operator opts in by setting `BRAIN_TRACE_DIR`."""
    raw = os.getenv(_TRACE_DIR_ENV)
    if not raw:
        return None
    sink = Path(raw)
    sink.mkdir(parents=True, exist_ok=True)
    return sink


def record_tool_calls(app_id: uuid.UUID, response: ModelResponse) -> None:
    """Trace-log and persist every tool call in one model response (name + raw args), including
    `read_file` (which emits no SSE `step`). No-op unless `BRAIN_TRACE_DIR` is set."""
    if os.getenv(_TRACE_DIR_ENV) is None:
        return
    try:
        sink = _sink_dir()
        if sink is None:
            return
        calls = [
            {"tool": part.tool_name, "tool_call_id": part.tool_call_id, "args": part.args}
            for part in response.parts
            if isinstance(part, ToolCallPart)
        ]
        if not calls:
            return
        for call in calls:
            logger.info(
                "brain_tool_call",
                app_id=str(app_id),
                tool=call["tool"],
                args=_short(call["args"]),
            )
        _append(
            sink / f"{app_id}.jsonl",
            {"kind": "tool_calls", "app_id": str(app_id), "calls": calls},
        )
    except Exception:
        # Best-effort telemetry (mirrors `_record_step`): a trace failure must never break a build.
        logger.warning("brain_trace_tool_calls_failed", app_id=str(app_id))


def record_run_messages(app_id: uuid.UUID, messages: list[ModelMessage]) -> None:
    """Persist the full pydantic-ai message history for one completed run — the durable,
    replayable white-box trace (every tool call, every tool return, and the model's reasoning).
    No-op unless `BRAIN_TRACE_DIR` is set."""
    if os.getenv(_TRACE_DIR_ENV) is None:
        return
    try:
        sink = _sink_dir()
        if sink is None:
            return
        dumped = ModelMessagesTypeAdapter.dump_python(messages, mode="json")
        _append(
            sink / f"{app_id}.jsonl",
            {"kind": "run_messages", "app_id": str(app_id), "messages": dumped},
        )
    except Exception:
        # Best-effort telemetry (mirrors `_record_step`): a trace failure must never break a build.
        logger.warning("brain_trace_run_messages_failed", app_id=str(app_id))


def _append(path: Path, record: dict[str, Any]) -> None:
    """Append one JSON line to the session trace file."""
    with path.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, default=str) + "\n")


def _short(args: object) -> str:
    """Render tool args for the live log line, bounded so a whole-file `write_file` payload does
    not flood stdout (the full args are preserved verbatim in the JSONL)."""
    text = args if isinstance(args, str) else json.dumps(args, default=str)
    return text if len(text) <= _ARGS_LOG_MAX_CHARS else text[:_ARGS_LOG_MAX_CHARS] + "…"
