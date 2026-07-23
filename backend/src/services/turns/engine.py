"""The unified turn engine (U10 / R6 / R8): one detached run per message, one
subscribable per-conversation event stream for chat AND build activity.

Sending a message STARTS a turn server-side, detached from the HTTP connection — the
subscriber transport (`api/v1/conversations/turns.py`) only OBSERVES. Generalized from the
build feed's proven shape (`build_sessions/sse.py`, copy-not-share per D6): an append-only
per-turn frame RING is the replay authority, subscriber queues are pure wakeups, and the
terminal frame explicitly closes the transport. Deliberately NO Redis (single replica: a
run dies with the process; Postgres is the durable log) — the ring is the one seam a
Redis Streams buffer would replace for multi-replica later.

Resume is catch-up-snapshot-then-tail: a subscriber that cannot prove gap-free continuity
(fresh subscribe, F5 with cursor=0, or a cursor that fell out of the ring) gets ONE
consolidated `snapshot` frame — the turn's persisted rows via the U6 projection plus the
in-memory text/step tail — then the live frames. A subscriber that CAN prove continuity
(same turn, cursor still in the ring) replays just the missed frames. Eviction is
therefore never data loss: falling past the ring's tail degrades to a fresh snapshot,
not a gap (the review's buffer-eviction finding, answered structurally).

Mode gating happens HERE (the server's record, never the client request): the run gets
exactly `toolsets_for_mode(conversation.mode)` over the turn-pinned workspace, and the
U9-composed instructions for that mode. Ask/Plan turns bill like the relay (one drain,
disconnect-safe by construction — the task IS the drain); Write turns arrive with U12's
warm sessions and bill per step through the harness.

Ownership: the engine holds the per-conversation guard (`turns/guard.py`, shared with the
relay until U13 retires it) from claim to the task's `finally` — a crashed run can never
wedge its conversation shut.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections import deque
from collections.abc import AsyncIterable, Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, Literal

import structlog
from pydantic_ai import BinaryContent, RunContext
from pydantic_ai.messages import (
    AgentStreamEvent,
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    ModelMessage,
    ModelResponse,
    PartDeltaEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.models import Model
from pydantic_ai.settings import ToolOrOutput
from pydantic_ai.tools import DeferredToolRequests
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.api.v1.conversations.schemas import (
    PlanOptionsFrame,
    SnapshotFrame,
    StepFrame,
    TextDeltaFrame,
    TurnEndedFrame,
    TurnErrorFrame,
    TurnStreamFrame,
)
from src.db.models.conversation import Conversation, ConversationMode
from src.db.models.message import MessageEntryKind, MessageVisibility
from src.services.agent.agent import ChatDeps, chat_agent
from src.services.agent.mode_prompts import PromptContext
from src.services.agent.read_tools import (
    EmptyProjectWorkspace,
    ExtractedSnapshotWorkspace,
    ReadOnlyWorkspace,
)
from src.services.agent.toolsets import plan_options_only_toolset, toolsets_for_mode
from src.services.messages.projection import (
    PLAN_OPTIONS_TOOL,
    DisplayItem,
    PlanOptionsItem,
    StepItem,
    classify_tool_call,
    step_detail,
)
from src.services.messages.store import append_batch
from src.services.storage.snapshot_read import NoAppYet, extract_snapshot
from src.services.turns.guard import claim_conversation, release_conversation
from src.services.usage.gate import record_usage

_log = structlog.get_logger()

# Per-turn frame ring: replay authority for gap-free resumes. Falling past the tail
# degrades to a fresh snapshot (never silent loss), so the cap bounds MEMORY, not
# correctness. Sized for a chatty turn (a delta per model chunk).
RING_MAXLEN = 2048

# How long an ENDED turn's state stays resumable (a late `GET /events` still gets its
# replay + terminal instead of a bare idle snapshot). After the TTL the DB is the record.
ENDED_TURN_TTL_S = 300.0

# In-flight friendly steps kept for snapshot consolidation (reads are chatty; the cap only
# guards a pathological run — the projection re-derives the full list from rows on reload).
_STEPS_CAP = 256

TurnStatus = Literal["running", "completed", "failed", "stopped"]

# What a turn failure tells the subscriber. Internal detail stays in the server log.
_TURN_FAILED_MESSAGE = "The assistant hit a problem and this turn was stopped."
_PERSIST_FAILED_MESSAGE = (
    "The reply could not be saved, so this turn was stopped. Try sending the message again."
)

# The row-meta kind stamping a pending options card (real or synthesized) — shared with
# `turns/plan_options.py`'s scan.
PENDING_META_KIND = "plan_options_pending"

# The ephemeral retry nudge (U11): rides `message_history` on the forced re-issue only —
# ModelResponse-only persistence keeps it out of the DB, same boundary as U14's reminders.
_FORCE_OPTIONS_NUDGE = (
    "<system-note>The plan above reads ready. Call present_plan_options now to show the "
    "user the confirmation buttons.</system-note>"
)


def _deferred_call(output: object) -> ToolCallPart | None:
    """The pending `present_plan_options` call when the run ended deferred, else None."""
    if not isinstance(output, DeferredToolRequests):
        return None
    for call in output.calls:
        if call.tool_name == PLAN_OPTIONS_TOOL:
            return call
    return None


def _looks_plan_shaped(text: str) -> bool:
    """Conservative plan-shape heuristic for the retry guarantee: a Plan turn that ends
    on a QUESTION is a legitimate clarifying turn (never retried); one that laid out
    list-shaped steps without presenting the options gets the one forced retry. Copy
    tuned against real traces later — the cost of a miss is one extra user message, the
    cost of a false fire is one cheap forced call."""
    stripped = text.rstrip()
    if not stripped:
        return False
    last_line = stripped.splitlines()[-1].strip()
    if last_line.endswith("?"):
        return False
    listish = sum(
        1
        for line in stripped.splitlines()
        if line.lstrip()[:2] in {"- ", "* ", "• "}
        or (line.lstrip()[:1].isdigit() and line.lstrip()[1:2] in {".", ")"})
    )
    return listish >= 2


class TurnUnsupportedError(Exception):
    """This conversation's mode cannot run through the engine yet (Write → U12)."""


class TurnNotRunningError(Exception):
    """Stop named a turn that is not the conversation's in-flight turn."""


@dataclass(frozen=True)
class ActiveTurnInfo:
    """What the U6 conversation read reports: the in-flight turn and its newest seq (the
    cursor a subscriber resumes the event stream from)."""

    turn_id: uuid.UUID
    last_seq: int


@dataclass
class _TurnState:
    """One turn's in-memory story: the frame ring (replay), the consolidated tail
    (snapshot material), and the fan-out wakeups."""

    turn_id: uuid.UUID
    conversation_id: uuid.UUID
    user_id: uuid.UUID
    mode: ConversationMode
    status: TurnStatus = "running"
    seq: int = 0
    ring: deque[TurnStreamFrame] = field(default_factory=lambda: deque(maxlen=RING_MAXLEN))
    text_parts: list[str] = field(default_factory=list)
    steps: dict[str, StepItem] = field(default_factory=dict)  # tool_call_id → newest item
    subscribers: set[asyncio.Queue[None]] = field(default_factory=set)
    task: asyncio.Task[None] | None = None
    ended_monotonic: float | None = None
    # The pinned extraction's head SHA (Plan turns stamp it onto their options card for
    # U12's stale-plan check); None when no app exists yet.
    head_sha: str | None = None

    def text_so_far(self) -> str:
        return "".join(self.text_parts)


PersistUserTurn = Callable[[], Awaitable[None]]
SessionFactory = async_sessionmaker[AsyncSession]


def _workspace_of(ctx: RunContext[ChatDeps]) -> ReadOnlyWorkspace:
    """The ChatDeps accessor the mode toolsets resolve the workspace through. Fail-first:
    a mode-gated run without a workspace is a programming error, not an empty app."""
    workspace = ctx.deps.workspace
    if workspace is None:
        raise RuntimeError("mode-gated turn ran without a turn-pinned workspace")
    return workspace


class TurnEngine:
    """The in-process turn registry + lifecycle (single-replica: this process is the sole
    writer, exactly the `SessionManager._active_by_user` invariant)."""

    def __init__(self) -> None:
        self._by_conversation: dict[uuid.UUID, _TurnState] = {}

    # -- registry reads -----------------------------------------------------------------

    def peek(self, conversation_id: uuid.UUID) -> _TurnState | None:
        """The conversation's newest turn state (running or ended-within-TTL), else None."""
        state = self._by_conversation.get(conversation_id)
        if state is None:
            return None
        if (
            state.status != "running"
            and state.ended_monotonic is not None
            and time.monotonic() - state.ended_monotonic > ENDED_TURN_TTL_S
        ):
            # Lazy TTL eviction — the DB is the record now.
            self._by_conversation.pop(conversation_id, None)
            return None
        return state

    def active_turn_info(self, conversation_id: uuid.UUID) -> ActiveTurnInfo | None:
        """The U6 `activeTurn` answer: only a RUNNING turn counts."""
        state = self.peek(conversation_id)
        if state is None or state.status != "running":
            return None
        return ActiveTurnInfo(turn_id=state.turn_id, last_seq=state.seq)

    # -- lifecycle ----------------------------------------------------------------------

    async def start_turn(
        self,
        *,
        conversation: Conversation,
        user_id: uuid.UUID,
        prompt: str | list[str | BinaryContent],
        history: list[ModelMessage],
        prompt_context: PromptContext,
        app_id: uuid.UUID | None,
        model: Model,
        session_factory: SessionFactory,
        persist_user_turn: PersistUserTurn,
    ) -> uuid.UUID:
        """Claim the conversation, persist the user turn (caller-supplied writer, so the
        route's typed seq-contention mapping stays where the route owns it), spawn the
        detached run, return the turn id. Raises `ConversationBusyError` (guard),
        `TurnUnsupportedError` (Write pre-U12), or whatever `persist_user_turn` raises —
        with the claim released."""
        if conversation.mode == ConversationMode.WRITE:
            raise TurnUnsupportedError(
                "Write turns start through the Build-it transition (U12), not a direct post."
            )
        claim_conversation(conversation.id)
        try:
            await persist_user_turn()
            state = _TurnState(
                turn_id=uuid.uuid7(),
                conversation_id=conversation.id,
                user_id=user_id,
                mode=conversation.mode,
            )
            self._by_conversation[conversation.id] = state
            state.task = asyncio.create_task(
                self._run_turn(
                    state,
                    prompt=prompt,
                    history=history,
                    prompt_context=prompt_context,
                    app_id=app_id,
                    model=model,
                    session_factory=session_factory,
                )
            )
        except BaseException:
            release_conversation(conversation.id)
            raise
        return state.turn_id

    async def stop_turn(self, conversation_id: uuid.UUID, turn_id: uuid.UUID) -> bool:
        """Explicit stop (disconnect ≠ cancel — this endpoint is the ONLY cancel). True
        when a running turn was cancelled; False when that turn already settled (stopping
        twice is not an error). A mismatched id raises `TurnNotRunningError`."""
        state = self.peek(conversation_id)
        if state is None or state.turn_id != turn_id:
            raise TurnNotRunningError
        if state.status != "running" or state.task is None:
            return False
        state.task.cancel()
        return True

    # -- the detached run ---------------------------------------------------------------

    async def _run_turn(
        self,
        state: _TurnState,
        *,
        prompt: str | list[str | BinaryContent],
        history: list[ModelMessage],
        prompt_context: PromptContext,
        app_id: uuid.UUID | None,
        model: Model,
        session_factory: SessionFactory,
    ) -> None:
        """The whole turn, detached: workspace pin → mode-gated run (streaming frames) →
        transcript append → billing → terminal. Mirrors the relay drain's ordering; every
        exit path funnels to exactly one terminal frame and the guard release."""
        try:
            workspace = await self._pin_workspace(state, app_id)
            async with session_factory() as db:
                deps = ChatDeps(
                    db=db,
                    user_id=state.user_id,
                    mode=state.mode,
                    prompt_context=prompt_context,
                    workspace=workspace,
                )
                toolsets = toolsets_for_mode(state.mode, _workspace_of)
                # Plan mode may DEFER on present_plan_options — the run then ends with a
                # DeferredToolRequests output instead of text (the pending card state).
                output_type: Any = (
                    [str, DeferredToolRequests] if state.mode == ConversationMode.PLAN else str
                )
                result = await chat_agent.run(
                    prompt,
                    deps=deps,
                    message_history=history,
                    model=model,
                    toolsets=toolsets,
                    output_type=output_type,
                    event_stream_handler=self._event_handler(state),
                )
                usages = [result.usage]
                batches: list[tuple[list[ModelResponse], dict[str, Any] | None]] = []
                deferred = _deferred_call(result.output)
                responses = [m for m in result.new_messages() if isinstance(m, ModelResponse)]
                batches.append((responses, self._pending_meta(state, deferred)))

                if (
                    state.mode == ConversationMode.PLAN
                    and deferred is None
                    and _looks_plan_shaped(state.text_so_far())
                ):
                    # The retry guarantee (U11): the model narrated a ready-looking plan
                    # but never called the tool — ONE re-issue with the tool as forced as
                    # the framework allows: the retry offers ONLY `present_plan_options`
                    # (`ToolOrOutput` restriction + an options-only toolset — a stricter
                    # `tool_choice` raises pydantic-ai's static guard, and
                    # `DeferredToolRequests` cannot be the sole output type). The nudge
                    # is ephemeral (only ModelResponse rows persist). A retry that STILL
                    # produces no call falls through to the synthesized card rather than
                    # failing the turn — the buttons ALWAYS appear.
                    try:
                        retry: Any = await chat_agent.run(
                            _FORCE_OPTIONS_NUDGE,
                            deps=deps,
                            message_history=[*history, *result.new_messages()],
                            model=model,
                            toolsets=plan_options_only_toolset(),
                            output_type=[str, DeferredToolRequests],
                            model_settings={
                                "tool_choice": ToolOrOutput(function_tools=[PLAN_OPTIONS_TOOL])
                            },
                            event_stream_handler=self._event_handler(state),
                        )
                    except Exception:
                        _log.warning(
                            "plan_options_forced_retry_failed",
                            conversation_id=str(state.conversation_id),
                            turn_id=str(state.turn_id),
                            exc_info=True,
                        )
                    else:
                        usages.append(retry.usage)
                        deferred = _deferred_call(retry.output)
                        retry_responses = [
                            m for m in retry.new_messages() if isinstance(m, ModelResponse)
                        ]
                        batches.append((retry_responses, self._pending_meta(state, deferred)))

                # WRITE-BEFORE-DONE (U5 policy): the reply must be durable before the turn
                # may claim success. The user request is already durable (pre-run write) —
                # append only the response side of `new_messages()`.
                for messages, meta in batches:
                    if messages:
                        await append_batch(
                            db,
                            user_id=state.user_id,
                            conversation_id=state.conversation_id,
                            messages=messages,
                            entry_kind=MessageEntryKind.TURN,
                            mode=state.mode,
                            meta=meta,
                        )
                if (
                    state.mode == ConversationMode.PLAN
                    and deferred is None
                    and _looks_plan_shaped(state.text_so_far())
                ):
                    # Retry cap reached with a plan on screen and no card — synthesize the
                    # options as a system record so the user is never stranded planless.
                    await self._synthesize_options(state, db)
                for usage in usages:
                    await record_usage(
                        db,
                        state.user_id,
                        input_tokens=usage.input_tokens,
                        output_tokens=usage.output_tokens,
                        cache_read_tokens=usage.cache_read_tokens,
                        cache_write_tokens=usage.cache_write_tokens,
                    )
                await db.commit()
            self._finish(state, "completed")
        except asyncio.CancelledError:
            # The explicit stop endpoint cancelled us. The user turn stays (it happened);
            # no reply row is written (none finished) — the durable record is truthful.
            self._finish(state, "stopped")
        except Exception:
            _log.exception(
                "turn_run_failed",
                conversation_id=str(state.conversation_id),
                turn_id=str(state.turn_id),
            )
            self._emit(
                state,
                lambda seq: TurnErrorFrame(seq=seq, message=_TURN_FAILED_MESSAGE),
            )
            self._finish(state, "failed")
        finally:
            release_conversation(state.conversation_id)

    async def _pin_workspace(
        self, state: _TurnState, app_id: uuid.UUID | None
    ) -> ReadOnlyWorkspace:
        """Resolve the turn-pinned read surface ONCE (no mid-turn version drift): the
        app's extracted snapshot, or the truthful empty workspace when nothing was ever
        built. Stashes the extraction's head SHA on the state — a Plan turn stamps it
        onto its pending options card (U12's stale-plan check compares it at Build-it).
        U12 swaps in the live workspace when a Write sandbox is attached."""
        if app_id is None:
            # No app row was ever minted — the nil id mirrors the harness's unknown-app
            # sentinel; the workspace only answers "no app exists yet" regardless.
            return EmptyProjectWorkspace(app_id=uuid.UUID(int=0))
        extracted = await extract_snapshot(app_id)
        if isinstance(extracted, NoAppYet):
            return EmptyProjectWorkspace(app_id=app_id)
        state.head_sha = extracted.head_sha
        return ExtractedSnapshotWorkspace(root=extracted.root)

    def _pending_meta(
        self, state: _TurnState, deferred: ToolCallPart | None
    ) -> dict[str, Any] | None:
        """The row meta for a batch that carries the pending options call: the card's id
        and the plan-time snapshot pin (row-level — never inside the native payload)."""
        if deferred is None:
            return None
        return {
            "kind": PENDING_META_KIND,
            "toolCallId": deferred.tool_call_id,
            "headSha": state.head_sha,
        }

    async def _synthesize_options(self, state: _TurnState, db: AsyncSession) -> None:
        """The retry-cap fallback: no real tool call exists, so the card is a system
        record (`plan_options_pending`, synthesized) — the user still gets their buttons,
        the wire history stays clean, and the miss is logged for prompt tuning."""
        tool_call_id = f"synthesized-{uuid.uuid4().hex[:12]}"
        _log.warning(
            "plan_options_synthesized_fallback",
            conversation_id=str(state.conversation_id),
            turn_id=str(state.turn_id),
        )
        await append_batch(
            db,
            user_id=state.user_id,
            conversation_id=state.conversation_id,
            messages=[],
            entry_kind=MessageEntryKind.SYSTEM_EVENT,
            mode=state.mode,
            visibility=MessageVisibility.HIDDEN,
            meta={
                "kind": PENDING_META_KIND,
                "toolCallId": tool_call_id,
                "headSha": state.head_sha,
                "synthesized": True,
            },
        )
        self._emit_plan_options(state, tool_call_id)

    def _emit_plan_options(self, state: _TurnState, tool_call_id: str) -> None:
        item = PlanOptionsItem(
            seq=0,  # live card; the reload projection assigns the row seq
            mode=state.mode.value,
            tool_call_id=tool_call_id,
            state="pending",
        )
        self._emit(
            state,
            lambda seq: PlanOptionsFrame(seq=seq, item=item),
        )

    # -- streaming ----------------------------------------------------------------------

    def _event_handler(
        self, state: _TurnState
    ) -> Callable[[RunContext[ChatDeps], AsyncIterable[AgentStreamEvent]], Awaitable[None]]:
        """The pydantic-ai event_stream_handler: model/tool events → typed frames."""

        async def handle(
            _ctx: RunContext[ChatDeps], events: AsyncIterable[AgentStreamEvent]
        ) -> None:
            async for event in events:
                self._on_event(state, event)

        return handle

    def _on_event(self, state: _TurnState, event: AgentStreamEvent) -> None:
        if isinstance(event, PartStartEvent):
            if isinstance(event.part, TextPart) and event.part.content:
                self._push_text(state, event.part.content)
        elif isinstance(event, PartDeltaEvent):
            if isinstance(event.delta, TextPartDelta) and event.delta.content_delta:
                self._push_text(state, event.delta.content_delta)
        elif isinstance(event, FunctionToolCallEvent):
            if event.part.tool_name == PLAN_OPTIONS_TOOL:
                # The options card, not a step: the call defers (the user's click is the
                # result), so there is no 'finished' counterpart to wait for.
                self._emit_plan_options(state, event.part.tool_call_id)
                return
            item = self._step_item(state, event.part.tool_name, event.part.args_as_json_str())
            state.steps[event.part.tool_call_id] = item
            self._emit(
                state,
                lambda seq: StepFrame(
                    seq=seq, tool_call_id=event.part.tool_call_id, phase="started", item=item
                ),
            )
        elif isinstance(event, FunctionToolResultEvent):
            resolved = self._resolve_step(state, event)
            if resolved is not None:
                self._emit(
                    state,
                    lambda seq: StepFrame(
                        seq=seq,
                        tool_call_id=event.tool_call_id,
                        phase="finished",
                        item=resolved,
                    ),
                )

    def _push_text(self, state: _TurnState, text: str) -> None:
        state.text_parts.append(text)
        self._emit(state, lambda seq: TextDeltaFrame(seq=seq, text=text))

    def _step_item(self, state: _TurnState, tool_name: str, args_json: str) -> StepItem:
        label, hidden = classify_tool_call(tool_name, args_json)
        return StepItem(
            seq=0,  # live steps have no row seq; the reload projection assigns real ones
            mode=state.mode.value,
            tool=tool_name,
            label=label,
            state="pending",
            hidden=hidden,
            detail=step_detail(args_json, None),
        )

    def _resolve_step(self, state: _TurnState, event: FunctionToolResultEvent) -> StepItem | None:
        pending = state.steps.get(event.tool_call_id)
        if pending is None:
            return None
        part = event.part
        failed = not isinstance(part, ToolReturnPart)  # a RetryPromptPart = refused/failed
        content = part.model_response_str() if isinstance(part, ToolReturnPart) else None
        resolved = pending.model_copy(
            update={
                "state": "failed" if failed else "ok",
                "detail": step_detail(pending.detail.args, content),
            }
        )
        state.steps[event.tool_call_id] = resolved
        if len(state.steps) > _STEPS_CAP:
            # Drop the oldest resolved step — snapshot material only; rows are authoritative.
            oldest = next(iter(state.steps))
            state.steps.pop(oldest, None)
        return resolved

    # -- frames, ring, fan-out ----------------------------------------------------------

    def _emit(self, state: _TurnState, build: Callable[[int], TurnStreamFrame]) -> TurnStreamFrame:
        state.seq += 1
        frame = build(state.seq)
        state.ring.append(frame)
        for queue in tuple(state.subscribers):
            try:
                queue.put_nowait(None)
            except asyncio.QueueFull:  # a wakeup is already pending — that is enough
                pass
        return frame

    def _finish(
        self, state: _TurnState, status: Literal["completed", "failed", "stopped"]
    ) -> None:
        state.status = status
        state.ended_monotonic = time.monotonic()
        self._emit(
            state,
            lambda seq: TurnEndedFrame(seq=seq, turn_id=str(state.turn_id), status=status),
        )

    # -- subscription -------------------------------------------------------------------

    def frames_since(self, state: _TurnState, last_seq: int) -> tuple[list[TurnStreamFrame], bool]:
        """The ring frames with seq > last_seq, plus whether a GAP separates them from the
        cursor (evicted frames — the caller must re-snapshot instead of replaying)."""
        frames = [frame for frame in state.ring if frame.seq > last_seq]
        if not frames:
            return [], False
        gap = frames[0].seq > last_seq + 1
        return frames, gap

    def build_snapshot(
        self, state: _TurnState | None, *, items: list[DisplayItem] | None = None
    ) -> SnapshotFrame:
        """The consolidated catch-up frame. `items` (the turn's persisted rows, projected)
        are resolved by the ROUTE before the stream commits — a mid-stream gap re-snapshot
        carries the in-memory tail only (TODO(U12): persisted mid-turn step rows join when
        Write turns ride this transport)."""
        if state is None:
            return SnapshotFrame(seq=0, turn_id=None, turn_status="idle")
        return SnapshotFrame(
            seq=state.seq,
            turn_id=str(state.turn_id),
            turn_status=state.status,
            items=items or [],
            text_so_far=state.text_so_far(),
            steps=[item for item in state.steps.values() if not item.hidden],
        )


_engine: TurnEngine | None = None


def get_turn_engine() -> TurnEngine:
    """The process singleton (same accessor discipline as the session manager)."""
    global _engine
    if _engine is None:
        _engine = TurnEngine()
    return _engine


def set_turn_engine_for_tests(engine: TurnEngine | None) -> None:
    global _engine
    _engine = engine
