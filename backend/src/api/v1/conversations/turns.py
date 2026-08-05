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

The turn plumbing this route shares with the relay (binaries resolution, prompt assembly,
history rehydration, the model/session-factory/storage dependencies) lives in `_shared.py`
alongside this module — one source, no copies, and no reaching into another router's
underscore-private names (ADR-0010).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator
from typing import Literal

import sqlalchemy as sa
import structlog
from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic_ai import BinaryContent
from pydantic_ai.messages import ModelMessage, ModelRequest, UserPromptPart
from pydantic_ai.models import Model
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.api.deps import CurrentUser, DbSession
from src.api.deps_csrf import RequireCsrf
from src.api.v1.build_sessions.deps import OptionalSandbox, SessionManagerDep
from src.api.v1.conversations._shared import (
    BUILD_IN_FLIGHT_MSG,
    ModelDep,
    SessionFactoryDep,
    StorageDep,
    TurnMessage,
    history_rehydrator,
    prompt_content,
    resolve_binaries,
    resolve_conversation_or_404,
)
from src.api.v1.conversations.schemas import (
    TurnStartResponse,
    TurnStopResponse,
    TurnStreamFrame,
)
from src.core.errors import AppApiError
from src.db.models.app_registry import AppRegistry
from src.db.models.conversation import Conversation, ConversationMode
from src.db.models.message import MessageEntryKind, MessageVisibility
from src.db.models.project import Project
from src.db.models.user import User
from src.schemas import AUTH_401, CamelModel, DailyTokenLimitBody, ErrorEnvelope, error_responses
from src.services.agent.mode_prompts import PromptContext
from src.services.build_sessions.manager import SessionManager
from src.services.messages.projection import DisplayItem, project_rows
from src.services.messages.store import (
    AttachmentRehydrationError,
    SeqContentionError,
    append_batch,
    load_history,
    load_rows,
)
from src.services.sandbox import SandboxClient
from src.services.turns.engine import (
    TurnNotRunningError,
    get_turn_engine,
)
from src.services.turns.guard import ConversationBusyError, conversation_is_mid_reply
from src.services.turns.plan_options import (
    NoPendingOptionsError,
    PlanOptionsExpiredError,
    resolve_pending_as_refine,
)
from src.services.turns.plan_options import (
    resolve as resolve_plan_options,
)
from src.services.usage.gate import DailyTokenLimitExceededError, enforce_daily_limit

logger = structlog.get_logger()

router = APIRouter(prefix="/conversations", tags=["turns"])

_SSE_HEADERS = {"Cache-Control": "no-cache", "Connection": "keep-alive"}
_DONE = b"data: [DONE]\n\n"

# Keepalive cadence between complete frames — MUST stay well under the portal reader's
# stall window (`turnStreamApi.ts` TURN_STREAM_STALL_TIMEOUT_MS = 60s; 4x margin).
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


async def start_conversation_turn(
    *,
    db: AsyncSession,
    user: User,
    conversation: Conversation,
    prompt: str | list[str | BinaryContent],
    history: list[ModelMessage],
    prompt_context: PromptContext,
    app_id: uuid.UUID | None,
    model: Model,
    factory: async_sessionmaker[AsyncSession],
    manager: SessionManager,
    sandbox: SandboxClient | None,
    visibility: MessageVisibility = MessageVisibility.VISIBLE,
    meta: dict[str, object] | None = None,
    expects_mutation: bool = False,
) -> uuid.UUID:
    """Persist the user turn and start the run — ONE expression, two readers.

    `POST /turns` and `Build it` differ only in where the prompt came from, whether the user
    is meant to see it, and whether a file change is OWED; everything after that (the durable
    pre-run write, the engine claim, the two typed conflict mappings) is identical, and two
    copies of it would drift the moment either grew a guard.

    `visibility` is what makes Build-it's seed work: the machine-authored "execute the
    approved plan" text has to be in the model's history and must never render as something
    the citizen typed. A HIDDEN row is both, with no projection change — `load_history`
    ignores visibility, `project_rows` skips it.

    `expects_mutation` is the other half of that asymmetry, and it travels to the engine
    rather than into a row: a Build-it turn that changes no file is a FAILED build, while a
    typed Write message that changes no file is just a question answered. Only the caller
    knows which it started, so only the caller can say."""

    async def persist_user_turn() -> None:
        await append_batch(
            db,
            user_id=user.id,
            conversation_id=conversation.id,
            messages=[ModelRequest(parts=[UserPromptPart(content=prompt)])],
            entry_kind=MessageEntryKind.TURN,
            mode=conversation.mode,
            visibility=visibility,
            meta=meta,
        )

    engine = get_turn_engine()
    try:
        return await engine.start_turn(
            conversation=conversation,
            user_id=user.id,
            prompt=prompt,
            history=history,
            prompt_context=prompt_context,
            app_id=app_id,
            project_id=conversation.project_id,
            model=model,
            session_factory=factory,
            persist_user_turn=persist_user_turn,
            manager=manager,
            sandbox_client=sandbox,
            expects_mutation=expects_mutation,
        )
    except ConversationBusyError:
        raise AppApiError(409, "A turn is already running for this conversation.") from None
    except SeqContentionError:
        raise AppApiError(
            409, "Another message is being recorded for this conversation. Try again."
        ) from None


@router.post(
    "/{conversation_id}/turns",
    status_code=202,
    response_model=TurnStartResponse,
    dependencies=[RequireCsrf],
    responses=error_responses(
        (400, ErrorEnvelope, "Invalid message"),
        AUTH_401,
        (403, ErrorEnvelope, "CSRF check failed"),
        (404, ErrorEnvelope, "Conversation not found"),
        (409, ErrorEnvelope, "The agent is already working in this conversation"),
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
    manager: SessionManagerDep,
    sandbox: OptionalSandbox,
) -> TurnStartResponse | JSONResponse:
    conversation = await resolve_conversation_or_404(db, user.id, conversation_id)

    # Daily-token gate BEFORE anything persists — a capped user's message is refused
    # whole, never half-recorded. The error carries its own byte-stable body (limit/used/
    # remaining, what the SPA's interceptor reads), so it is RETURNED, not flattened into
    # the plain envelope — the same contract `claude/router.py` honours.
    try:
        await enforce_daily_limit(db, user.id)
    except DailyTokenLimitExceededError as exc:
        return exc.as_response()
    if model is None:
        raise AppApiError(503, "Claude client not configured.")

    # Every side-effect-free rejection lands BEFORE `resolve_pending_as_refine`, which is a
    # WRITE: a refused start must never burn the user's pending plan-options card. Both
    # checks are re-made downstream (the engine owns the real, race-free claim) — these are
    # the early, cheap copies that keep the write from happening at all.
    #
    # THE ONE GATE, server side: while the agent is working in this thread — a reply in flight
    # OR a build running — no new turn starts. The build arm is a LIVENESS check, not the mode
    # check below it: the two genuinely disagree (a build's first seconds run before the flip;
    # `POST /build-sessions` never touches the mode at all), and only liveness answers "is the
    # agent building THIS thread right now".
    if manager.live_session_for_conversation(conversation.id) is not None:
        raise AppApiError(409, BUILD_IN_FLIGHT_MSG)
    if conversation_is_mid_reply(conversation.id):
        raise AppApiError(409, "A turn is already running for this conversation.")
    # WRITE only, and BELOW the mid-reply guard on purpose: a Write send during a streaming
    # reply must still 409 as a busy conversation, or it races `transcript_head_seq`. This
    # one asks a different question — is this user's single sandbox already committed to a
    # DIFFERENT conversation? Cheap and synchronous; the expensive provision happens inside
    # the detached turn, because blocking the POST on 30-60s recreates the dead end the
    # composer contract exists to remove.
    if conversation.mode is ConversationMode.WRITE:
        active = manager.active_session_for(user.id)
        if active is not None and active.conversation_id != conversation.id:
            raise AppApiError(409, BUILD_IN_FLIGHT_MSG)

    project = await db.get(Project, conversation.project_id)
    if project is None:  # FK guarantees this; fail loudly if it ever breaks
        raise AppApiError(404, "Conversation not found.")

    # Free text while plan options are pending resolves them as an implicit "keep
    # refining" (U11) — BEFORE history loads, so the model always sees a resolved call.
    await resolve_pending_as_refine(db, user_id=user.id, conversation_id=conversation.id)

    rehydrate = history_rehydrator(db, storage, user.id)
    try:
        history = await load_history(
            db, user_id=user.id, conversation_id=conversation.id, rehydrate=rehydrate
        )
    except AttachmentRehydrationError as exc:
        raise AppApiError(400, str(exc)) from None
    binaries = await resolve_binaries(db, storage, user.id, body.message.attachment_ids)
    prompt = prompt_content(body.message, binaries)

    display_name = user.display_name or user.email
    prompt_context = PromptContext(
        user_name=display_name,
        project_name=project.name,
        project_description=project.description or None,
    )
    app_id = await _app_id_for_project(db, user.id, conversation.project_id)

    turn_id = await start_conversation_turn(
        db=db,
        user=user,
        conversation=conversation,
        prompt=prompt,
        history=history,
        prompt_context=prompt_context,
        app_id=app_id,
        model=model,
        factory=factory,
        manager=manager,
        sandbox=sandbox,
    )
    return TurnStartResponse(turn_id=str(turn_id))


@router.post(
    "/{conversation_id}/turns/{turn_id}/stop",
    response_model=TurnStopResponse,
    dependencies=[RequireCsrf],
    responses=error_responses(
        AUTH_401,
        (403, ErrorEnvelope, "CSRF check failed"),
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
    await resolve_conversation_or_404(db, user.id, conversation_id)
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
    conversation = await resolve_conversation_or_404(db, user.id, conversation_id)
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

    # Every DB read this route needs is done. Commit now so the pooled connection goes back
    # BEFORE the response starts streaming — otherwise one long-lived SSE pins one connection
    # for the whole turn and a handful of open tabs drains the pool (the streamed-reply
    # learning the docstring above already promises).
    await db.commit()

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


class ResolvePlanOptionsBody(CamelModel):
    """The user's click. Only `refine` resolves HERE — `build` goes through U12's atomic
    Build-it transition endpoint (record + flip mode + lock + start, one operation), so a
    resolved-build can never exist without its build."""

    choice: Literal["refine"]


class ResolvePlanOptionsResponse(CamelModel):
    state: Literal["refine", "build", "build_failed"]
    already_resolved: bool


@router.post(
    "/{conversation_id}/plan-options/{tool_call_id}/resolve",
    response_model=ResolvePlanOptionsResponse,
    dependencies=[RequireCsrf],
    responses=error_responses(
        (400, ErrorEnvelope, "Unknown card, or a choice this endpoint does not record"),
        AUTH_401,
        (403, ErrorEnvelope, "CSRF check failed"),
        (404, ErrorEnvelope, "Conversation not found"),
        (409, ErrorEnvelope, "The card is superseded by a newer one"),
    ),
)
async def resolve_plan_options_route(
    conversation_id: uuid.UUID,
    tool_call_id: str,
    body: ResolvePlanOptionsBody,
    user: CurrentUser,
    db: DbSession,
) -> ResolvePlanOptionsResponse:
    """Record "Keep refining" — idempotent on the card id (a second click or second tab
    reads back the stored resolution; a reload can never show resolved-with-no-record)."""
    await resolve_conversation_or_404(db, user.id, conversation_id)
    try:
        resolution = await resolve_plan_options(
            db,
            user_id=user.id,
            conversation_id=conversation_id,
            tool_call_id=tool_call_id,
            choice=body.choice,
        )
    except NoPendingOptionsError:
        raise AppApiError(400, "No such plan options card.") from None
    except PlanOptionsExpiredError:
        raise AppApiError(409, "A newer plan supersedes these options.") from None
    return ResolvePlanOptionsResponse(
        state=resolution.choice, already_resolved=resolution.already_resolved
    )
