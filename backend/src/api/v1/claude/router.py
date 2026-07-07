"""Streaming chat endpoint — `POST /v1/claude` (R10, R13, R12).

The order is byte-faithful to Express `server.js`: authenticate → daily-token gate (a 429
`daily_token_limit_exceeded` JSON BEFORE any SSE header, never a half-open stream) → stream text
deltas → bill the turn. The wire contract is exactly two frame types the SPA parses:
`data: {"delta":{"text":"…"}}\n\n` and a terminal `data: [DONE]\n\n` — no usage/error frames.

The chat is a STATELESS RELAY: the SPA sends the full Anthropic-shaped `{model, max_tokens,
system, messages}` and the server persists NO messages (the SPA owns persistence via the
conversations API); the only write is token usage. Billing is DISCONNECT-SAFE: the agent runs to
completion in a background task with its OWN session, shielded from the SSE generator's
cancellation, so a client disconnect still drains + bills the full turn (matching Express, which
has no AbortSignal). Cache tokens fold into the daily cap (U6); model access is Foundry-only (U12).
"""

from __future__ import annotations

import asyncio
import json
import uuid
from collections.abc import AsyncIterator
from typing import Annotated, Any

import structlog
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic_ai.messages import (
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    UserPromptPart,
)
from pydantic_ai.models import Model
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.api.deps import CurrentUser, DbSession
from src.config import settings
from src.db.base import async_session_factory
from src.services.agent.agent import ChatDeps, chat_agent
from src.services.agent.content import to_model_content
from src.services.agent.model import build_foundry_model
from src.services.usage.gate import (
    DailyTokenLimitExceededError,
    enforce_daily_limit,
    record_usage,
)

logger = structlog.get_logger()

router = APIRouter(prefix="/claude", tags=["claude"])

# Request-body ceiling (Express mount `limit:'35mb'` — base64 attachment blocks) and the
# server-side output clamp (Express `MAX_OUTPUT_TOKENS`).
_BODY_LIMIT_BYTES = 35 * 1024 * 1024
_MAX_OUTPUT_TOKENS = 64_000

# Keep strong references to in-flight billing drains so the loop can't GC a task whose SSE
# generator was cancelled by a client disconnect (the drain must run to completion + bill).
_drains: set[asyncio.Task[None]] = set()

# The billing/agent session factory — a dependency (like storage) so tests bind it to the
# rolled-back test session instead of committing to the real DB.
BillingSessionFactory = async_sessionmaker[AsyncSession]


def chat_model() -> Model | None:
    """The Foundry-backed Pydantic AI model, or None when Foundry isn't configured (dev/test
    boot without it). A dependency so tests inject a `TestModel` via `dependency_overrides`."""
    if settings.foundry is None:
        return None
    return build_foundry_model(settings.foundry)


def billing_session_factory() -> BillingSessionFactory:
    """The session factory the disconnect-safe drain uses (its own session, decoupled from the
    request). A dependency so tests bind it to the rolled-back test session."""
    return async_session_factory


ModelDep = Annotated[Model | None, Depends(chat_model)]
SessionFactoryDep = Annotated[BillingSessionFactory, Depends(billing_session_factory)]


def _error(message: str, status_code: int) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"error": {"message": message}})


def _clamp_max_tokens(value: Any) -> int:
    # Express `Math.min(Math.max(1, Number(max_tokens)||64000), 64000)`.
    try:
        parsed = int(value)
    except TypeError, ValueError:
        parsed = _MAX_OUTPUT_TOKENS
    if parsed <= 0:
        parsed = _MAX_OUTPUT_TOKENS
    return min(max(1, parsed), _MAX_OUTPUT_TOKENS)


def _assistant_text(content: Any) -> str:
    """Flatten an assistant turn's content to text for the replayed history."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(
            block["text"]
            for block in content
            if isinstance(block, dict)
            and block.get("type") == "text"
            and isinstance(block.get("text"), str)
        )
    return ""


def _split_messages(messages: list[Any]) -> tuple[Any, list[ModelMessage]]:
    """Split the Anthropic-shaped transcript into (newest user prompt, prior message_history).
    Prior user turns → ModelRequest, prior assistant turns → ModelResponse."""
    history: list[ModelMessage] = []
    for message in messages[:-1]:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if message.get("role") == "user":
            history.append(ModelRequest(parts=[UserPromptPart(content=to_model_content(content))]))
        elif message.get("role") == "assistant":
            history.append(ModelResponse(parts=[TextPart(content=_assistant_text(content))]))
    prompt = to_model_content(
        messages[-1].get("content") if isinstance(messages[-1], dict) else ""
    )
    return prompt, history


def _delta_frame(text: str) -> bytes:
    # Compact JSON (no spaces) so the frame is byte-identical to Express `JSON.stringify`.
    return (
        b"data: " + json.dumps({"delta": {"text": text}}, separators=(",", ":")).encode() + b"\n\n"
    )


def _stream(
    session_factory: BillingSessionFactory,
    user_id: uuid.UUID,
    model: Model,
    prompt: Any,
    history: list[ModelMessage],
    system: str,
    max_tokens: int,
) -> AsyncIterator[bytes]:
    queue: asyncio.Queue[str | None] = asyncio.Queue()

    async def _drain() -> None:
        # Runs to completion even if the SSE generator is cancelled (client disconnect), so the
        # full turn is always billed. Uses its OWN session (survives the request scope).
        try:
            async with session_factory() as db:
                deps = ChatDeps(db=db, user_id=user_id, system=system)
                async with chat_agent.run_stream(
                    prompt,
                    deps=deps,
                    message_history=history,
                    model=model,
                    model_settings={"max_tokens": max_tokens},
                ) as result:
                    async for delta in result.stream_text(delta=True):
                        queue.put_nowait(delta)
                    usage = result.usage
                # Bill on the completed run — cache tokens fold into the daily cap (U6).
                await record_usage(
                    db,
                    user_id,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    cache_read_tokens=usage.cache_read_tokens,
                    cache_write_tokens=usage.cache_write_tokens,
                )
                await db.commit()
        except Exception:
            # Never leak an internal error into the stream; the turn's billing is best-effort
            # on an upstream failure (parity with Express dropping the increment on stream error).
            logger.exception("chat_stream_drain_failed")
        finally:
            queue.put_nowait(None)

    async def generator() -> AsyncIterator[bytes]:
        task = asyncio.create_task(_drain())
        _drains.add(task)
        task.add_done_callback(_drains.discard)
        while True:
            item = await queue.get()
            if item is None:
                break
            yield _delta_frame(item)
        yield b"data: [DONE]\n\n"

    return generator()


@router.post("")
async def claude_chat(
    request: Request, user: CurrentUser, db: DbSession, model: ModelDep, factory: SessionFactoryDep
) -> Any:
    # Body-size ceiling before buffering (Express `limit:'35mb'`).
    content_length = request.headers.get("content-length")
    if (
        content_length is not None
        and content_length.isdigit()
        and int(content_length) > _BODY_LIMIT_BYTES
    ):
        return _error("Request body is too large.", 413)

    # Daily-token gate — a 429 BEFORE any SSE header (never a half-open stream).
    try:
        await enforce_daily_limit(db, user.id)
    except DailyTokenLimitExceededError as exc:
        return exc.as_response()

    if model is None:
        return _error("Claude client not configured.", 503)

    try:
        body: Any = await request.json()
    except ValueError, TypeError:
        body = {}
    messages = body.get("messages") if isinstance(body, dict) else None
    if not isinstance(messages, list) or not messages:
        return _error("messages must be a non-empty array.", 400)
    system_raw = body.get("system")
    system = system_raw if isinstance(system_raw, str) else ""
    max_tokens = _clamp_max_tokens(body.get("max_tokens"))
    prompt, history = _split_messages(messages)

    stream = _stream(factory, user.id, model, prompt, history, system, max_tokens)
    return StreamingResponse(
        stream,
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
    )
