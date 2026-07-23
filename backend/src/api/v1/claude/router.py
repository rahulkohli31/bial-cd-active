"""Streaming chat endpoint — `POST /v1/claude` (R9/R10, U7).

STATELESS TURNS: the browser sends only `{conversationId, message}` — the new message's typed
text, its attachment-content text blocks (office/csv fences the client already holds), and
owned attachment ids for stored binaries. The server loads the conversation's history from the
native message store (attachments rehydrated to base64 — Foundry has no Files API), composes
the system prompt entirely server-side (base-by-kind + portal self-description + project
context + the builder interview protocol), runs the turn, and persists both sides of it. The
full-transcript relay contract (`{model, max_tokens, system, messages}`) is retired — R9.

The typed body IS the sanitize boundary (plan: `sanitize_messages()` surface): text fields are
bounded strings that get redacted at the persistence seam like every other producer's output,
and attachment ids are pattern-checked here then OWNERSHIP-checked at resolution — nothing
else client-derived exists any more.

The wire contract is unchanged: exactly two frame types the SPA parses —
`data: {"delta":{"text":"…"}}\n\n` and a terminal `data: [DONE]\n\n` — with `: ping`
keepalives between frames. Billing is DISCONNECT-SAFE: the agent runs to completion in a
background task with its OWN session, shielded from the SSE generator's cancellation, so a
client disconnect still drains + bills the full turn. WRITE-BEFORE-DONE (U5): the user turn is
persisted before the run, the assistant's responses before the terminal `[DONE]`.

ONE REPLY AT A TIME (plan: per-conversation turn guard): a conversation with a reply already
being generated answers 409. The registry is a plain in-process set — the same single-replica
invariant `SessionManager._active_by_user` leans on (one process is the sole writer; absent
in-process state is authoritative). The claim is released by the drain's `finally`, which runs
on success, failure, AND client disconnect (the drain is shielded), so the guard cannot leak.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass
from typing import Annotated, Any, Literal

import sqlalchemy as sa
import structlog
from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
from pydantic import Field, ValidationError, model_validator
from pydantic_ai import BinaryContent
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, UserPromptPart
from pydantic_ai.models import Model
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.api.deps import CurrentUser, DbSession
from src.api.v1.claude.prompts import (
    ASSISTANT_IDENTITY_PROMPT,
    BUILD_INTERVIEW_PROTOCOL,
    PLANNING_SYSTEM_PROMPT,
    PORTAL_SELF_DESCRIPTION,
    SUMMARIZE_BRIEF_PROMPT,
)
from src.config import settings
from src.core.errors import AppApiError
from src.db.base import async_session_factory
from src.db.models.conversation import Conversation, ConversationKind, ConversationMode
from src.db.models.message import MessageEntryKind
from src.db.models.project import Project
from src.schemas import AUTH_401, CamelModel, DailyTokenLimitBody, ErrorEnvelope, error_responses
from src.services.agent.agent import ChatDeps, chat_agent
from src.services.agent.model import build_foundry_model
from src.services.messages.store import (
    AttachmentRehydrationError,
    Rehydrator,
    SeqContentionError,
    append_batch,
    attachment_rehydrator,
    load_history,
)
from src.services.storage import ObjectStorage, StorageUnconfiguredError, get_storage
from src.services.turns.guard import (
    ConversationBusyError,
    claim_conversation,
    release_conversation,
)
from src.services.usage.gate import (
    DailyTokenLimitExceededError,
    enforce_daily_limit,
    record_usage,
)

logger = structlog.get_logger()

router = APIRouter(prefix="/claude", tags=["claude"])

# Body ceiling. The 35 MB base64-attachment era is over — the body now carries typed prose +
# fenced attachment text (client caps: 256 KB per text file, 512 KB per conversation, office
# extractions server-truncated) + attachment id tokens. 8 MB is far above any legitimate send
# and still an order of magnitude below the old cap.
_BODY_LIMIT_BYTES = 8 * 1024 * 1024
_MAX_OUTPUT_TOKENS = 64_000

# Per-field bounds for the typed body (the sanitize boundary). Sized ABOVE the client's own
# caps so they only fire on abuse or a client bug — exactly when a 400 beats a mystery
# context overflow.
_MAX_MESSAGE_TEXT_CHARS = 64_000
_MAX_ATTACHMENT_TEXT_CHARS = 600_000
_MAX_ATTACHMENT_BLOCKS = 8

# The SPA's client-minted attachment token (Express `ID_RE` heritage — bounded, not a UUID).
_ATTACHMENT_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")

# Attachment media types that may enter the model prompt as base64 vision blocks. Office
# bytes are stored but NEVER model-visible (their extracted text rides `attachmentTexts`);
# text files are never uploaded at all.
_VISION_MEDIA_PREFIX = "image/"
_PDF_MEDIA_TYPE = "application/pdf"

# SSE response headers (shared by the delta stream and the empty-completion stream).
_SSE_HEADERS = {"Cache-Control": "no-cache", "Connection": "keep-alive"}

# Mid-stream keepalive cadence (F1). When the drain produces nothing for this long — a server→model
# retry backoff or the model "thinking" — the generator emits an SSE comment frame so the client's
# idle watchdog can tell "server working" from "socket dead". It must sit WELL under the client's
# stall window (portal `STREAM_STALL_TIMEOUT_MS`, 60s) so a couple of delayed pings never
# false-trip it; 15s gives a 4× margin.
_KEEPALIVE_SECONDS = 15.0

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


def chat_storage() -> ObjectStorage | None:
    """The object store, or None when unconfigured — NEVER an eager raising `Depends` (the
    eager-Depends learning: a raise here would 500 every text-only turn on a storage-less
    boot). The None arm fails IN-BODY, typed, exactly where refs are actually needed: sending
    an attachment id → 503; loading a history that carries stored references → the
    rehydrator's own typed failure."""
    try:
        return get_storage()
    except StorageUnconfiguredError:
        return None


ModelDep = Annotated[Model | None, Depends(chat_model)]
SessionFactoryDep = Annotated[BillingSessionFactory, Depends(billing_session_factory)]
StorageDep = Annotated[ObjectStorage | None, Depends(chat_storage)]


class TurnMessage(CamelModel):
    """The new message — the ONLY content the browser sends (R9).

    `attachment_texts` are complete, client-built `<attachment …>…</attachment>` fence blocks:
    inline text files (whose bytes are never uploaded) and office extractions (whose bytes are
    stored but never model-visible). They are opaque text to this route — fencing/neutralizing
    happened where the content was assembled, and redaction happens at the persistence seam.
    `attachment_ids` are owned references to STORED binaries (image/PDF), resolved to base64
    server-side at send."""

    text: str = Field(max_length=_MAX_MESSAGE_TEXT_CHARS)
    attachment_texts: list[str] = Field(default_factory=list, max_length=_MAX_ATTACHMENT_BLOCKS)
    attachment_ids: list[str] = Field(default_factory=list, max_length=_MAX_ATTACHMENT_BLOCKS)

    @model_validator(mode="after")
    def _bounded_and_non_empty(self) -> TurnMessage:
        for block in self.attachment_texts:
            if len(block) > _MAX_ATTACHMENT_TEXT_CHARS:
                raise ValueError("an attachment text block is too large")
        for attachment_id in self.attachment_ids:
            if not _ATTACHMENT_ID_RE.fullmatch(attachment_id):
                raise ValueError("an attachment id is invalid")
        return self


class TurnBody(CamelModel):
    """`POST /v1/claude` — the stateless-turn contract (U7).

    `ephemeral='summarize_brief'`: run against the conversation's history (which IS the
    planning transcript) with the summarize prompt, persist NOTHING — the one-off brief
    extraction the planning chat's "move to builder" flow uses.

    `regenerate=true`: the user turn of the previous attempt is already durable
    (persist-before-stream), so the run replays history WITHOUT a new prompt and without a
    second user-turn row — pydantic-ai adopts the trailing user request as the prompt, which
    is exactly what a regenerate means."""

    conversation_id: uuid.UUID
    message: TurnMessage
    ephemeral: Literal["summarize_brief"] | None = None
    regenerate: bool = False

    @model_validator(mode="after")
    def _coherent(self) -> TurnBody:
        if self.ephemeral is not None and self.regenerate:
            raise ValueError("ephemeral and regenerate are mutually exclusive")
        has_content = bool(
            self.message.text.strip()
            or self.message.attachment_texts
            or self.message.attachment_ids
        )
        if not self.regenerate and not has_content:
            raise ValueError("the message must carry content")
        return self


@dataclass(frozen=True)
class _TurnPersist:
    """Where this turn's transcript lands: the resolved conversation and its mode. None for
    ephemeral turns (they persist nothing, by contract)."""

    conversation_id: uuid.UUID
    mode: ConversationMode


async def _resolve_conversation_or_404(
    db: AsyncSession, user_id: uuid.UUID, conversation_id: uuid.UUID
) -> Conversation:
    """The turn's owner-scoped conversation row. U7 retires the old load-bearing None arm:
    conversations are created BEFORE the first turn (`POST /v1/conversations`), so an unknown
    id is a client bug and a cross-user id is indistinguishable from it — one non-leaking 404
    (ADR-0004)."""
    conversation: Conversation | None = await db.scalar(
        sa.select(Conversation).where(
            Conversation.id == conversation_id, Conversation.user_id == user_id
        )
    )
    if conversation is None:
        raise AppApiError(404, "Conversation not found.")
    return conversation


def _base_prompt(conversation: Conversation, ephemeral: str | None) -> str:
    """The server-selected base system prompt (U7): ephemeral summarize wins outright; else
    the conversation's kind decides. ASSISTANT kind shares the builder's identity line — it is
    the generic portal-assistant voice."""
    if ephemeral == "summarize_brief":
        return SUMMARIZE_BRIEF_PROMPT
    if conversation.kind == ConversationKind.PLANNING:
        return PLANNING_SYSTEM_PROMPT
    return ASSISTANT_IDENTITY_PROMPT


async def _compose_system(
    db: AsyncSession, conversation: Conversation, *, ephemeral: str | None
) -> str:
    """base-by-kind + portal self-description (#6/R5) + project context + (builder-thread
    only, non-ephemeral) the interview protocol. Entirely server-owned — the SPA's `system`
    field died with the full-transcript payload (R9)."""
    sections = [_base_prompt(conversation, ephemeral), PORTAL_SELF_DESCRIPTION]
    project = await db.get(Project, conversation.project_id)
    if project is not None and project.description:
        sections.append(f"Project context — {project.name}:\n{project.description}")
    if conversation.kind == ConversationKind.BUILDER and ephemeral is None:
        # 003-U2: the builder thread runs the ask-then-brief interview. An ephemeral
        # summarize must NOT carry it — two competing output contracts in one prompt.
        sections.append(BUILD_INTERVIEW_PROTOCOL)
    return "\n\n".join(sections)


def _history_rehydrator(
    db: AsyncSession, storage: ObjectStorage | None, user_id: uuid.UUID
) -> Rehydrator:
    """The rehydrator `load_history` swaps stored reference markers through. With storage
    unconfigured it fails TYPED — and only if the history actually carries a reference, so a
    text-only conversation on a storage-less boot keeps working."""
    if storage is not None:
        return attachment_rehydrator(db, storage, user_id)

    async def unconfigured(_attachment_id: str) -> tuple[str, str]:
        raise AttachmentRehydrationError(
            "an attached file could not be read (file storage is not configured)"
        )

    return unconfigured


async def _resolve_binaries(
    db: AsyncSession, storage: ObjectStorage | None, user_id: uuid.UUID, attachment_ids: list[str]
) -> list[BinaryContent]:
    """Owned attachment refs → base64-backed `BinaryContent` for the model prompt (the plan's
    refs→base64-at-send resolver). Rides the store's own rehydrator — owner-scoped row, magic
    re-check, authoritative media type — then gates on WHAT may enter the prompt: only
    image/PDF vision content. Office originals and anything else are a 400 (their content
    travels as `attachmentTexts`), and an unknown/foreign id fails the same typed way the
    rehydrator words it."""
    if not attachment_ids:
        return []
    if storage is None:
        raise AppApiError(503, "File storage is not configured.")
    rehydrate = attachment_rehydrator(db, storage, user_id)
    binaries: list[BinaryContent] = []
    for attachment_id in attachment_ids:
        try:
            data_b64, media_type = await rehydrate(attachment_id)
        except AttachmentRehydrationError as exc:
            raise AppApiError(400, str(exc)) from None
        if not (media_type.startswith(_VISION_MEDIA_PREFIX) or media_type == _PDF_MEDIA_TYPE):
            raise AppApiError(
                400,
                "an attached file of this type cannot be sent to the assistant as a file; "
                "its extracted text travels with the message instead",
            )
        binaries.append(
            BinaryContent(
                data=base64.b64decode(data_b64), media_type=media_type, identifier=attachment_id
            )
        )
    return binaries


def _prompt_content(
    message: TurnMessage, binaries: list[BinaryContent]
) -> str | list[str | BinaryContent]:
    """The turn's user content: binaries first, fenced attachment text next, the typed prose
    LAST (Anthropic's files-before-text ordering — the same shape `BuildSpec` pins). A plain
    text-only message stays a bare string (the historical single-string shape)."""
    if not binaries and not message.attachment_texts:
        return message.text
    return [*binaries, *message.attachment_texts, message.text]


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
    prompt: str | list[str | BinaryContent] | None,
    history: list[ModelMessage],
    system: str,
    persist: _TurnPersist | None,
    conversation_id: uuid.UUID,
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
                    model_settings={"max_tokens": _MAX_OUTPUT_TOKENS},
                ) as result:
                    async for delta in result.stream_text(delta=True):
                        # Once disconnected, stop enqueuing (bounded queue) but keep draining so
                        # `result.usage` is complete and the turn is still billed.
                        if not state["disconnected"]:
                            queue.put_nowait(delta)
                    usage = result.usage
                    # U5 — WRITE-BEFORE-DONE: the assistant's response lands in the durable
                    # transcript before the client is told the turn succeeded. Only the
                    # RESPONSES persist — the run's request was pre-written by `claude_chat`
                    # (clean, instructions-free). A failure here raises into the except arm:
                    # no [DONE], the SPA sees a truncated stream, the DB never lied.
                    if persist is not None:
                        responses = [
                            message
                            for message in result.new_messages()
                            if isinstance(message, ModelResponse)
                        ]
                        if responses:
                            await append_batch(
                                db,
                                user_id=user_id,
                                conversation_id=persist.conversation_id,
                                messages=responses,
                                entry_kind=MessageEntryKind.TURN,
                                mode=persist.mode,
                            )
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
        finally:
            # The one release point for the per-conversation turn guard: the drain runs to
            # completion on success, failure, AND disconnect, so the claim can never leak.
            release_conversation(conversation_id)
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
                # Fetch the next item, emitting a `: ping` comment for every idle gap so the
                # client watchdog can distinguish a working server from a dead socket. The inner
                # loop keeps the already-yielded `item` out of the way — a ping never re-emits it.
                while True:
                    try:
                        item = await asyncio.wait_for(queue.get(), _KEEPALIVE_SECONDS)
                        break
                    except TimeoutError:
                        yield b": ping\n\n"
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
        (400, ErrorEnvelope, "Invalid body, message bounds, or attachment reference"),
        AUTH_401,
        (404, ErrorEnvelope, "Conversation not found (or not owned by the caller)"),
        (409, ErrorEnvelope, "A reply is already being generated, or a concurrent write"),
        (413, ErrorEnvelope, "Request body is too large"),
        (429, DailyTokenLimitBody, "Daily token limit exceeded"),
        (500, ErrorEnvelope, "The model request failed"),
        (503, ErrorEnvelope, "Claude client not configured"),
    ),
)
async def claude_chat(
    request: Request,
    user: CurrentUser,
    db: DbSession,
    model: ModelDep,
    factory: SessionFactoryDep,
    storage: StorageDep,
) -> Any:
    # Fast-reject on a declared oversize body before buffering…
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

    # …and enforce the cap on ACTUALLY-received bytes (a lying/absent Content-Length can't
    # bypass), then parse the typed contract — every 4xx lands BEFORE any SSE byte.
    raw = await _read_capped_body(request, _BODY_LIMIT_BYTES)
    if raw is None:
        raise AppApiError(413, "Request body is too large.")
    try:
        body = TurnBody.model_validate_json(raw)
    except ValidationError as exc:
        first_error = exc.errors()[0]
        raise AppApiError(400, f"Invalid request: {first_error['msg']}") from None

    conversation = await _resolve_conversation_or_404(db, user.id, body.conversation_id)

    # ONE REPLY AT A TIME — claim before anything is persisted, so a losing concurrent POST
    # leaves no orphan user-turn row. Synchronous check+add on one event loop: no TOCTOU.
    # The guard is SHARED with the U10 turn engine (`services/turns/guard.py`): during the
    # U10→U13 window both surfaces exist, and one conversation must never run both.
    try:
        claim_conversation(conversation.id)
    except ConversationBusyError:
        raise AppApiError(
            409, "A reply is already being generated for this conversation."
        ) from None

    try:
        # History FIRST, then the new user turn — appending first would double the newest
        # message (once in history, once as the prompt).
        rehydrate = _history_rehydrator(db, storage, user.id)
        try:
            history = await load_history(
                db, user_id=user.id, conversation_id=conversation.id, rehydrate=rehydrate
            )
        except AttachmentRehydrationError as exc:
            # A stored reference no longer resolves (blob gone, storage down/unconfigured).
            # Typed and pre-stream — never a mystery 500 mid-conversation.
            raise AppApiError(400, str(exc)) from None
        if body.regenerate and not history:
            raise AppApiError(400, "There is nothing to regenerate in this conversation.")

        binaries = await _resolve_binaries(db, storage, user.id, body.message.attachment_ids)
        system = await _compose_system(db, conversation, ephemeral=body.ephemeral)

        persist: _TurnPersist | None = None
        prompt: str | list[str | BinaryContent] | None
        if body.regenerate:
            # The previous attempt's user turn is already durable — replay history with no new
            # prompt (pydantic-ai adopts the trailing user request) and persist only the reply.
            prompt = None
            persist = _TurnPersist(conversation_id=conversation.id, mode=conversation.mode)
        else:
            prompt = _prompt_content(body.message, binaries)
            if body.ephemeral is None:
                # U5 — persist the USER turn before the run: a crashed run still leaves the
                # question in the durable record. Binaries carry their attachment identifier,
                # so the store externalizes them to reference markers (never bytes in a row).
                try:
                    await append_batch(
                        db,
                        user_id=user.id,
                        conversation_id=conversation.id,
                        messages=[ModelRequest(parts=[UserPromptPart(content=prompt)])],
                        entry_kind=MessageEntryKind.TURN,
                        mode=conversation.mode,
                    )
                except SeqContentionError:
                    raise AppApiError(
                        409, "Another message is being recorded for this conversation. Try again."
                    ) from None
                persist = _TurnPersist(conversation_id=conversation.id, mode=conversation.mode)
    except BaseException:
        # Anything that stops the turn between claim and drain-start releases the claim; once
        # `_stream` has spawned the drain, the drain's `finally` owns the release.
        release_conversation(conversation.id)
        raise

    return await _stream(
        factory, user.id, model, prompt, history, system, persist, conversation.id
    )
