"""The resumable turn transport (U10): POST starts a detached turn, GET subscribes.

`POST /conversations/{id}/turns` → 202 `{turnId}` — the run is detached the moment the
response leaves; closing the tab changes nothing (disconnect ≠ cancel; the explicit stop
endpoint is the ONLY cancel). `GET /conversations/{id}/events` is a pure OBSERVER of the
engine's frame ring: catch-up snapshot then live tail for any subscriber that cannot
prove gap-free continuity, plain replay for one that can (`?turn=&cursor=`). Multiple
simultaneous subscribers each get the identical stream — fan-out is the engine's, the
route only walks the ring.

Wire discipline (copied from the build feed + relay, D6): commit the SSE response and
emit the first frame BEFORE any model byte (the snapshot serves that role), `: ping`
keepalives only between complete frames, errors travel in-band, and the terminal
`turn_ended` frame is followed by `data: [DONE]` which closes the transport.

Several helpers are imported from `claude/router.py` (binaries resolution, prompt
assembly, history rehydration, the model/session-factory/storage dependencies): the relay
still serves live traffic until U13 retires it into this surface — these helpers re-home
here then, not before (one source meanwhile, no copies).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator

import sqlalchemy as sa
import structlog
from fastapi import APIRouter, Query
from fastapi.responses import StreamingResponse
from pydantic_ai.messages import ModelRequest, UserPromptPart
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.deps import CurrentUser, DbSession
from src.api.deps_csrf import RequireCsrf
from src.api.v1.claude.router import (
    ModelDep,
    SessionFactoryDep,
    StorageDep,
    TurnMessage,
    _history_rehydrator,
    _prompt_content,
    _resolve_binaries,
    _resolve_conversation_or_404,
)
from src.api.v1.conversations.schemas import (
    TurnStartResponse,
    TurnStopResponse,
    TurnStreamFrame,
)
from src.core.errors import AppApiError
from src.db.models.app_registry import AppRegistry
from src.db.models.message import MessageEntryKind
from src.db.models.project import Project
from src.schemas import AUTH_401, CamelModel, DailyTokenLimitBody, ErrorEnvelope, error_responses
from src.services.agent.mode_prompts import PromptContext
from src.services.messages.projection import DisplayItem, project_rows
from src.services.messages.store import (
    AttachmentRehydrationError,
    SeqContentionError,
    append_batch,
    load_history,
    load_rows,
)
from src.services.turns.engine import (
    TurnNotRunningError,
    TurnUnsupportedError,
    get_turn_engine,
)
from src.services.turns.guard import ConversationBusyError
from src.services.usage.gate import DailyTokenLimitExceededError, enforce_daily_limit

logger = structlog.get_logger()

router = APIRouter(prefix="/conversations", tags=["turns"])

_SSE_HEADERS = {"Cache-Control": "no-cache", "Connection": "keep-alive"}
_DONE = b"data: [DONE]\n\n"

# Keepalive cadence between complete frames — MUST stay well under the portal reader's
# stall window (`useConversationStream.ts` STREAM_STALL_TIMEOUT_MS = 60s; 4x margin).
# Both sides pin this inequality by test.
KEEPALIVE_SECONDS = 15.0


class StartTurnBody(CamelModel):
    """`POST /conversations/{id}/turns` — the new message only (R9); the conversation id
    rides the path."""

    message: TurnMessage


def _frame_bytes(frame: TurnStreamFrame) -> bytes:
    return (
        b"id: "
        + str(frame.seq).encode()
        + b"\ndata: "
        + frame.model_dump_json(by_alias=True).encode()
        + b"\n\n"
    )


async def _app_id_for_project(
    db: AsyncSession, user_id: uuid.UUID, project_id: uuid.UUID
) -> uuid.UUID | None:
    """The project's app, WITHOUT minting one (`resolve_app_for_project` upserts — a read
    path must never create). None = nothing was ever built."""
    app_id: uuid.UUID | None = await db.scalar(
        sa.select(AppRegistry.id).where(
            AppRegistry.project_id == project_id, AppRegistry.user_id == user_id
        )
    )
    return app_id


@router.post(
    "/{conversation_id}/turns",
    status_code=202,
    response_model=TurnStartResponse,
    dependencies=[RequireCsrf],
    responses=error_responses(
        (400, ErrorEnvelope, "Invalid message, or this mode's turns don't start here"),
        AUTH_401,
        (404, ErrorEnvelope, "Conversation not found"),
        (409, ErrorEnvelope, "A turn is already running for this conversation"),
        (429, DailyTokenLimitBody, "Daily token limit exceeded"),
        (503, ErrorEnvelope, "Claude client not configured"),
    ),
)
async def start_turn(
    conversation_id: uuid.UUID,
    body: StartTurnBody,
    user: CurrentUser,
    db: DbSession,
    model: ModelDep,
    factory: SessionFactoryDep,
    storage: StorageDep,
) -> TurnStartResponse:
    conversation = await _resolve_conversation_or_404(db, user.id, conversation_id)

    # Daily-token gate BEFORE anything persists — a capped user's message is refused
    # whole, never half-recorded.
    try:
        await enforce_daily_limit(db, user.id)
    except DailyTokenLimitExceededError as exc:
        raise AppApiError(429, "Daily token limit exceeded.") from exc
    if model is None:
        raise AppApiError(503, "Claude client not configured.")

    project = await db.get(Project, conversation.project_id)
    if project is None:  # FK guarantees this; fail loudly if it ever breaks
        raise AppApiError(404, "Conversation not found.")

    rehydrate = _history_rehydrator(db, storage, user.id)
    try:
        history = await load_history(
            db, user_id=user.id, conversation_id=conversation.id, rehydrate=rehydrate
        )
    except AttachmentRehydrationError as exc:
        raise AppApiError(400, str(exc)) from None
    binaries = await _resolve_binaries(db, storage, user.id, body.message.attachment_ids)
    prompt = _prompt_content(body.message, binaries)

    display_name = user.display_name or user.email
    prompt_context = PromptContext(
        user_name=display_name,
        project_name=project.name,
        project_description=project.description or None,
    )
    app_id = await _app_id_for_project(db, user.id, conversation.project_id)

    async def persist_user_turn() -> None:
        await append_batch(
            db,
            user_id=user.id,
            conversation_id=conversation.id,
            messages=[ModelRequest(parts=[UserPromptPart(content=prompt)])],
            entry_kind=MessageEntryKind.TURN,
            mode=conversation.mode,
        )

    engine = get_turn_engine()
    try:
        turn_id = await engine.start_turn(
            conversation=conversation,
            user_id=user.id,
            prompt=prompt,
            history=history,
            prompt_context=prompt_context,
            app_id=app_id,
            model=model,
            session_factory=factory,
            persist_user_turn=persist_user_turn,
        )
    except ConversationBusyError:
        raise AppApiError(409, "A turn is already running for this conversation.") from None
    except TurnUnsupportedError as exc:
        raise AppApiError(400, str(exc)) from None
    except SeqContentionError:
        raise AppApiError(
            409, "Another message is being recorded for this conversation. Try again."
        ) from None
    return TurnStartResponse(turn_id=str(turn_id))


@router.post(
    "/{conversation_id}/turns/{turn_id}/stop",
    response_model=TurnStopResponse,
    dependencies=[RequireCsrf],
    responses=error_responses(
        AUTH_401,
        (404, ErrorEnvelope, "Conversation not found"),
        (409, ErrorEnvelope, "That turn is not this conversation's in-flight turn"),
    ),
)
async def stop_turn(
    conversation_id: uuid.UUID,
    turn_id: uuid.UUID,
    user: CurrentUser,
    db: DbSession,
) -> TurnStopResponse:
    """The explicit cancel (disconnect never cancels)."""
    await _resolve_conversation_or_404(db, user.id, conversation_id)
    engine = get_turn_engine()
    try:
        cancelled = await engine.stop_turn(conversation_id, turn_id)
    except TurnNotRunningError:
        raise AppApiError(409, "That turn is not this conversation's in-flight turn.") from None
    return TurnStopResponse(status="stopping" if cancelled else "already_settled")


@router.get(
    "/{conversation_id}/events",
    responses=error_responses(AUTH_401, (404, ErrorEnvelope, "Conversation not found")),
)
async def turn_events(
    conversation_id: uuid.UUID,
    user: CurrentUser,
    db: DbSession,
    cursor: int = Query(default=0, ge=0),
    turn: uuid.UUID | None = Query(default=None),
) -> StreamingResponse:
    """Subscribe to the conversation's turn stream: snapshot-then-tail, or a gap-free
    replay when `turn` matches the live turn and `cursor` is still inside the ring.

    The DB read for the snapshot's persisted items happens HERE, before the response
    commits — the generator itself never touches the request session (an SSE lifetime
    must not hold a DB session; the streamed-reply learning)."""
    conversation = await _resolve_conversation_or_404(db, user.id, conversation_id)
    engine = get_turn_engine()
    state = engine.peek(conversation.id)

    # Continuity: same turn + cursor still replayable → tail-only resume.
    replay_only = False
    if state is not None and turn == state.turn_id and cursor > 0:
        _frames, gap = engine.frames_since(state, cursor)
        replay_only = not gap

    snapshot = None
    if not replay_only:
        items: list[DisplayItem] = []
        if state is not None:
            # The turn's persisted rows (its user turn now; U12 adds mid-build steps),
            # via the ONE U6 derivation — live and reload can never drift.
            rows = await load_rows(
                db, user_id=user.id, conversation_id=conversation.id, include_hidden=True
            )
            projected = project_rows(rows)
            items = projected[-8:]  # the turn's own tail; full history is the U6 GET
        snapshot = engine.build_snapshot(state, items=items)

    queue: asyncio.Queue[None] = asyncio.Queue(maxsize=1)
    if state is not None:
        state.subscribers.add(queue)

    async def generator() -> AsyncIterator[bytes]:
        last_sent = cursor if replay_only else 0
        try:
            if snapshot is not None:
                yield _frame_bytes(snapshot)
                last_sent = snapshot.seq
            if state is None:
                # Idle conversation: snapshot said so; close cleanly.
                yield _DONE
                return
            while True:
                frames, gap = engine.frames_since(state, last_sent)
                if gap:
                    # Fell past the ring's tail — consolidate instead of losing frames.
                    fresh = engine.build_snapshot(state)
                    yield _frame_bytes(fresh)
                    last_sent = fresh.seq
                    continue
                for frame in frames:
                    yield _frame_bytes(frame)
                    last_sent = frame.seq
                    if frame.type == "turn_ended":
                        yield _DONE
                        return
                if state.status != "running" and last_sent >= state.seq:
                    # Settled and fully replayed, but the ring never carried a terminal
                    # (evicted): close explicitly rather than hang.
                    yield _DONE
                    return
                try:
                    await asyncio.wait_for(queue.get(), timeout=KEEPALIVE_SECONDS)
                except TimeoutError:
                    yield b": ping\n\n"
        finally:
            if state is not None:
                state.subscribers.discard(queue)

    return StreamingResponse(generator(), media_type="text/event-stream", headers=_SSE_HEADERS)
