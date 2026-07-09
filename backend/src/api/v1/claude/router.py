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

import sqlalchemy as sa
import structlog
from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
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
from src.core.errors import AppApiError
from src.db.base import async_session_factory
from src.db.models.app_registry import AppRegistry
from src.db.models.conversation import Conversation, ConversationKind
from src.db.models.project import Project
from src.schemas import AUTH_401, DailyTokenLimitBody, ErrorEnvelope, error_responses
from src.services.agent.agent import ChatDeps, chat_agent
from src.services.agent.content import to_model_content
from src.services.agent.model import build_foundry_model
from src.services.projects import extract_source
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
# Bound the project-code seed injected into a builder turn (U11) so a large snapshot can't
# blow the window; the description (U8) is already length-capped at the write boundary (KD-8).
_SEED_CODE_CHAR_BUDGET = 300_000

# SSE response headers (shared by the delta stream and the empty-completion stream).
_SSE_HEADERS = {"Cache-Control": "no-cache", "Connection": "keep-alive"}

# Terminal queue markers: the drain succeeded (bill + emit [DONE]) vs failed (no [DONE]).
_END_OK = object()
_END_FAIL = object()

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


def _clamp_max_tokens(value: Any) -> int:
    # Express `Math.min(Math.max(1, Number(max_tokens)||64000), 64000)`.
    try:
        parsed = int(value)
    except (TypeError, ValueError):  # fmt: skip  # ruff py314 strips parens
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


async def _project_context_system(
    db: AsyncSession, user_id: uuid.UUID, conversation_id_raw: Any, system: str
) -> str:
    """Augment the SPA-supplied system prompt with the turn's PROJECT context: the project
    description as shared grounding for every chat in the project (U8/R16), plus — for a
    builder session — the project's current app code so it continues from the last stage
    (U11/KD-9 seed). A missing / malformed / unknown / cross-user `conversationId` is a
    STRICT no-op: `system` is returned byte-identical, so a legacy request that names no
    conversation is unchanged (no regression). This is additive per-turn context, bounded
    by the description length cap (KD-8) and the code-seed budget; the transcript-resend
    cost reduction is deferred (see the projects Problem Frame)."""
    if not isinstance(conversation_id_raw, str):
        return system
    try:
        conversation_id = uuid.UUID(conversation_id_raw)
    except ValueError:
        return system
    conversation = await db.scalar(
        sa.select(Conversation).where(
            Conversation.id == conversation_id, Conversation.user_id == user_id
        )
    )
    if conversation is None:  # unknown or cross-user → no-op, never an error on a chat turn
        return system
    project = await db.get(Project, conversation.project_id)
    if project is None:
        return system

    additions: list[str] = []
    if project.description:  # U8: shared grounding for every chat in the project (R16)
        additions.append(f"Project context — {project.name}:\n{project.description}")
    if conversation.kind == ConversationKind.BUILDER:  # U11: seed the project's current code
        app = await db.scalar(
            sa.select(AppRegistry).where(
                AppRegistry.project_id == project.id, AppRegistry.user_id == user_id
            )
        )
        source = extract_source(app.current_code) if app is not None else ""
        if source:
            seed = source[:_SEED_CODE_CHAR_BUDGET]
            if len(source) > _SEED_CODE_CHAR_BUDGET:
                seed += "\n\n[... code truncated to fit the model context window ...]"
            additions.append(f"The project's current app code (continue from this):\n{seed}")

    if not additions:
        return system
    return "\n\n".join([system, *additions]) if system else "\n\n".join(additions)


def _delta_frame(text: str) -> bytes:
    # Compact JSON (no spaces) so the frame is byte-identical to Express `JSON.stringify`.
    return (
        b"data: " + json.dumps({"delta": {"text": text}}, separators=(",", ":")).encode() + b"\n\n"
    )


async def _read_capped_body(request: Request, limit: int) -> bytes | None:
    """Accumulate the request body from the stream, aborting (→ None) the moment the running
    total exceeds `limit` — enforces the cap on ACTUALLY-received bytes, not a spoofable
    Content-Length header."""
    total = 0
    chunks: list[bytes] = []
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


async def _stream(
    session_factory: BillingSessionFactory,
    user_id: uuid.UUID,
    model: Model,
    prompt: Any,
    history: list[ModelMessage],
    system: str,
    max_tokens: int,
) -> StreamingResponse:
    queue: asyncio.Queue[str | object] = asyncio.Queue()
    # Mutated by the SSE generator on client disconnect so the shielded drain stops enqueuing
    # (the queue stays bounded) yet keeps consuming to completion for `result.usage` billing.
    state = {"disconnected": False}

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
                        # Once disconnected, stop enqueuing (bounded queue) but keep draining so
                        # `result.usage` is complete and the turn is still billed.
                        if not state["disconnected"]:
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
            # Never leak an internal error into the stream. Signal failure so the caller returns a
            # clean 500 (pre-delta) or closes without a false [DONE] (mid-stream). Billing is
            # best-effort on an upstream failure (parity with Express dropping the increment).
            logger.exception("chat_stream_drain_failed")
            queue.put_nowait(_END_FAIL)
            return
        queue.put_nowait(_END_OK)

    task = asyncio.create_task(_drain())
    _drains.add(task)
    task.add_done_callback(_drains.discard)

    # Await the FIRST item before committing to a StreamingResponse: a failure before any delta
    # is a clean 500 JSON (never a half-open 200 stream).
    first = await queue.get()
    if first is _END_FAIL:
        # Explicit route-level 500 rendered as the `{"error":{"message"}}` envelope (NOT the
        # global `{"detail"}` 500) — pre-delta, so never a half-open 200 stream.
        raise AppApiError(500, "The model request failed.")
    if first is _END_OK:
        # Completed with no text — emit only the terminal frame.
        async def _empty() -> AsyncIterator[bytes]:
            yield b"data: [DONE]\n\n"

        return StreamingResponse(_empty(), media_type="text/event-stream", headers=_SSE_HEADERS)

    async def generator() -> AsyncIterator[bytes]:
        item: str | object = first
        try:
            while True:
                if item is _END_OK:
                    yield b"data: [DONE]\n\n"
                    return
                if item is _END_FAIL:
                    # Mid-stream upstream failure — close WITHOUT [DONE] so the SPA sees a
                    # truncated stream, not a false success.
                    return
                if isinstance(item, str):
                    yield _delta_frame(item)
                item = await queue.get()
        finally:
            # Client disconnected (GeneratorExit) — tell the shielded drain to stop enqueuing; it
            # still runs to completion + bills the full turn.
            state["disconnected"] = True

    return StreamingResponse(generator(), media_type="text/event-stream", headers=_SSE_HEADERS)


@router.post(
    "",
    # SSE stream / JSON errors — no response_model. The daily-token 429 keeps its
    # dedicated `as_response()` body (5 keys), documented as DailyTokenLimitBody. The
    # explicit 500 overrides the v1 `DetailBody` default with the `ErrorEnvelope`.
    responses=error_responses(
        (400, ErrorEnvelope, "messages must be a non-empty array"),
        AUTH_401,
        (413, ErrorEnvelope, "Request body is too large"),
        (429, DailyTokenLimitBody, "Daily token limit exceeded"),
        (500, ErrorEnvelope, "The model request failed"),
        (503, ErrorEnvelope, "Claude client not configured"),
    ),
)
async def claude_chat(
    request: Request, user: CurrentUser, db: DbSession, model: ModelDep, factory: SessionFactoryDep
) -> Any:
    # Fast-reject on a declared oversize body (Express `limit:'35mb'`) before buffering…
    content_length = request.headers.get("content-length")
    if (
        content_length is not None
        and content_length.isdigit()
        and int(content_length) > _BODY_LIMIT_BYTES
    ):
        raise AppApiError(413, "Request body is too large.")

    # Daily-token gate — a 429 BEFORE any SSE header (never a half-open stream).
    try:
        await enforce_daily_limit(db, user.id)
    except DailyTokenLimitExceededError as exc:
        return exc.as_response()

    if model is None:
        raise AppApiError(503, "Claude client not configured.")

    # …and enforce the cap on ACTUALLY-received bytes (a lying/absent Content-Length can't bypass).
    raw = await _read_capped_body(request, _BODY_LIMIT_BYTES)
    if raw is None:
        raise AppApiError(413, "Request body is too large.")
    try:
        body: Any = json.loads(raw) if raw else {}
    except (ValueError, TypeError):  # fmt: skip  # ruff py314 strips parens
        body = {}
    messages = body.get("messages") if isinstance(body, dict) else None
    if not isinstance(messages, list) or not messages:
        raise AppApiError(400, "messages must be a non-empty array.")
    system_raw = body.get("system")
    system = system_raw if isinstance(system_raw, str) else ""
    # When the turn names its conversation, fold the project description (U8) + builder code
    # seed (U11) into the system prompt; absent/unknown conversationId → byte-identical no-op.
    system = await _project_context_system(db, user.id, body.get("conversationId"), system)
    max_tokens = _clamp_max_tokens(body.get("max_tokens"))
    prompt, history = _split_messages(messages)

    return await _stream(factory, user.id, model, prompt, history, system, max_tokens)
