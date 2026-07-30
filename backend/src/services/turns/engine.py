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
from contextlib import suppress
from dataclasses import dataclass, field
from functools import partial
from typing import Any, Literal

import structlog
from pydantic_ai import Agent, BinaryContent, RunContext
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.messages import (
    AgentStreamEvent,
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    PartDeltaEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models import Model
from pydantic_ai.models.anthropic import AnthropicModelSettings
from pydantic_ai.settings import ToolOrOutput
from pydantic_ai.tools import DeferredToolRequests
from pydantic_ai.usage import RequestUsage, RunUsage, UsageLimits
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.api.v1.conversations.schemas import (
    DiagnosticFrame,
    PlanOptionsFrame,
    PreviewFrame,
    QuotaFrame,
    SnapshotFrame,
    StepFrame,
    TextDeltaFrame,
    TurnEndedFrame,
    TurnErrorFrame,
    TurnStreamFrame,
    WorkspaceFrame,
)
from src.core.redaction import redact_secrets
from src.db.models.conversation import Conversation, ConversationMode
from src.db.models.message import MessageEntryKind, MessageVisibility
from src.db.models.user import User
from src.services.agent.agent import ChatDeps, chat_agent
from src.services.agent.mode_prompts import PromptContext, mode_reminder
from src.services.agent.read_tools import (
    EmptyProjectWorkspace,
    ExtractedSnapshotWorkspace,
    LiveSandboxWorkspace,
    ReadOnlyWorkspace,
)
from src.services.agent.toolsets import plan_options_only_toolset, toolsets_for_mode
from src.services.build_sessions.manager import (
    BuildSession,
    BuildSessionConflictError,
    SessionManager,
    SnapshotUnavailableError,
)
from src.services.messages.projection import (
    PLAN_OPTIONS_TOOL,
    DisplayItem,
    PlanOptionsItem,
    StepItem,
    classify_tool_call,
    step_detail,
)
from src.services.messages.store import append_batch
from src.services.orchestrator.constants import (
    CACHE_TTL,
    MAX_OUTPUT_TOKENS,
    MODEL_TURN_CEILING,
    READINESS_MAX_POLLS,
    READINESS_POLL_S,
    RUN_WALL_CLOCK_DEADLINE_S,
    SELF_HEAL_MAX_RETRIES,
    TEMPERATURE,
)
from src.services.orchestrator.deps import SandboxSession
from src.services.orchestrator.prompt import build_repair_prompt
from src.services.orchestrator.selfheal import CONTINUE_PROMPT, dev_not_ready_error, verify
from src.services.sandbox import SandboxClient, SandboxError
from src.services.storage.snapshot_read import NoAppYet, extract_snapshot
from src.services.turns.guard import claim_conversation, release_conversation
from src.services.turns.plan_options import META_PENDING
from src.services.usage.gate import (
    DailyTokenLimitExceededError,
    enforce_daily_limit,
    next_ist_midnight_iso,
    record_usage,
)

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

# The row-meta kind stamping a pending options card (real or synthesized). IMPORTED, not
# re-spelled: `plan_options._scan` reads rows by this exact string, so two literals meant a
# typo in either one would silently stop every card from being found.
PENDING_META_KIND = META_PENDING

# The ephemeral retry nudge (U11): rides `message_history` on the forced re-issue only —
# ModelResponse-only persistence keeps it out of the DB, same boundary as U14's reminders.
_FORCE_OPTIONS_NUDGE = (
    "<system-note>The plan above reads ready. Call present_plan_options now to show the "
    "user the confirmation buttons. This note is between you and the platform — keep it out "
    "of your reply.</system-note>"
)

# U14 (D3): the ephemeral mode-reminder cadence. Long conversations bury the per-run
# instructions at the top of context, so the active mode's rules re-ride near the TAIL on
# a deterministic cadence — a FULL restatement every 8th turn in the mode, the one-line
# nudge every 4th between, silence otherwise. The persisted mode-switch marker rows (U4)
# are the anchor: a switch resets the count and the new mode's first turn gets an
# immediate full reminder (its history actively contradicts the fresh toolset). The
# reminder rides `message_history` only — `new_messages()` structurally excludes injected
# history, so no post-hoc filtering ever has to remember to strip it.
REMINDER_FULL_EVERY = 8
REMINDER_NUDGE_EVERY = 4

# `mode_switch_marker_text` (store.py) always opens with this literal. Detecting it in
# the rehydrated payload is deliberate: hiddenness lives on the ROW, so the text is the
# one in-band signal — and a user typing the prefix themselves merely nudges the cadence.
_MODE_MARKER_PREFIX = "[mode changed:"


def _turns_since_mode_anchor(history: list[ModelMessage]) -> tuple[int, bool]:
    """(user turns since the newest mode-switch marker, whether a marker was seen).
    Counts real user prompts only — tool-return requests (plan-options resolutions) and
    responses don't advance the cadence. The current turn's prompt is NOT in `history`
    (it rides separately), so the count IS this turn's 0-based ordinal in the mode."""
    count = 0
    for message in reversed(history):
        if not isinstance(message, ModelRequest):
            continue
        for part in message.parts:
            if isinstance(part, UserPromptPart):
                if isinstance(part.content, str) and part.content.startswith(_MODE_MARKER_PREFIX):
                    return count, True
                count += 1
                break
    return count, False


def _plan_options_outstanding(history: list[ModelMessage]) -> bool:
    """Is a confirmation card already with the user (N9b)?

    True when the newest `present_plan_options` call has no USER PROMPT after it — which covers
    both states the reminder must not talk over: the card is still pending, or the user has just
    answered it (their click is a tool RETURN, not a prompt, so it does not clear this) and the
    resolution turn is the one about to run.

    Read from history rather than from the store because history is what the model sees and what
    this function already has; the stored resolution against the `toolCallId` is the same fact,
    one layer down. A user prompt after the call is the honest "we have moved on" signal.
    """
    seen_prompt = False
    for message in reversed(history):
        if isinstance(message, ModelRequest):
            for part in message.parts:
                if isinstance(part, UserPromptPart) and not (
                    isinstance(part.content, str) and part.content.startswith(_MODE_MARKER_PREFIX)
                ):
                    seen_prompt = True
        elif isinstance(message, ModelResponse):
            # A DISTINCT name from the request loop above: reusing `part` narrows it to the
            # request-part union and mypy rejects the response-part assignment.
            for response_part in message.parts:
                if (
                    isinstance(response_part, ToolCallPart)
                    and response_part.tool_name == PLAN_OPTIONS_TOOL
                ):
                    return not seen_prompt
    return False


def _reminder_text(mode: ConversationMode, history: list[ModelMessage]) -> str | None:
    """The reminder riding THIS turn, or None between cadence points. Turn 0 of a fresh
    conversation stays silent (the instructions are right there); turn 0 after a SWITCH
    gets the full reminder."""
    ordinal, after_switch = _turns_since_mode_anchor(history)
    # The cadence decides WHETHER to speak; the card state decides what the Plan reminder may
    # say. Keeping the two separate is why an outstanding card quiets the call-the-tool
    # sentence without also silencing the mode anchor the cadence exists to re-assert.
    outstanding = mode is ConversationMode.PLAN and _plan_options_outstanding(history)
    if after_switch and ordinal == 0:
        return mode_reminder(mode, full=True, plan_options_outstanding=outstanding)
    if ordinal > 0 and ordinal % REMINDER_FULL_EVERY == 0:
        return mode_reminder(mode, full=True, plan_options_outstanding=outstanding)
    if ordinal > 0 and ordinal % REMINDER_NUDGE_EVERY == 0:
        return mode_reminder(mode, full=False, plan_options_outstanding=outstanding)
    return None


def _deferred_call(output: object) -> ToolCallPart | None:
    """The pending `present_plan_options` call when the run ended deferred, else None."""
    if not isinstance(output, DeferredToolRequests):
        return None
    for call in output.calls:
        if call.tool_name == PLAN_OPTIONS_TOOL:
            return call
    return None


def _persistable_messages(new_messages: list[ModelMessage]) -> list[ModelMessage]:
    """The durable transcript slice of a run's `new_messages()`: every `ModelResponse`, PLUS
    every `ModelRequest` that carries tool returns but NOT a fresh user prompt.

    Persisting the tool-return requests is load-bearing: they answer the `ToolCallPart`s the
    responses make (read_file / search_files / run_command). Dropping them — as a
    responses-only filter does — leaves each persisted call unanswered, so the reload's
    dangling-call repair papers over a real, successful tool result with a synthesized
    "interrupted" one, corrupting the replayed transcript. Requests bearing a `UserPromptPart`
    are excluded: the user turn is already persisted before the run, and the ephemeral mode
    reminder / force-options nudge ride `message_history` and must never fossilize into a row
    (the `new_messages()` boundary the whole persistence design leans on)."""
    kept: list[ModelMessage] = []
    for message in new_messages:
        if isinstance(message, ModelResponse):
            kept.append(message)
        elif isinstance(message, ModelRequest) and not any(
            isinstance(part, UserPromptPart) for part in message.parts
        ):
            kept.append(message)
    return kept


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


class TurnNotRunningError(Exception):
    """Stop named a turn that is not the conversation's in-flight turn."""


def _sandbox_unavailable_message(exc: Exception) -> str:
    """Citizen copy for a workspace that would not come up.

    `SnapshotUnavailableError` gets its own sentence because it is not a generic outage —
    it is the platform REFUSING to hand the model a blank template in place of an app it
    could not read. The user needs to know their work is intact and that retrying is the
    right move, not that "something went wrong"."""
    if isinstance(exc, SnapshotUnavailableError):
        return (
            "Your saved app could not be loaded just now, so the assistant stopped rather "
            "than start from an empty one. Nothing was changed — please try again shortly."
        )
    if isinstance(exc, BuildSessionConflictError):
        return "Another chat is using your workspace. Finish or stop that one first."
    return "Your workspace could not be started right now. Please try again shortly."


class _WriteEndedError(Exception):
    """A Write turn that stopped for a NAMED reason rather than a crash.

    Distinct from a bare `Exception` because the four ways a build legitimately runs out —
    daily quota, self-heal budget, wall clock, model step ceiling — are not bugs, and telling
    a citizen "the assistant hit a problem" when they simply spent their token budget sends
    them to support instead of to tomorrow."""

    def __init__(self, reason: str, message: str) -> None:
        super().__init__(reason)
        self.reason = reason
        self.message = message


class _PersistFailedError(Exception):
    """A transcript append (or its commit) failed — the turn failed on the WRITE-BEFORE-DONE
    seam specifically, so the subscriber gets the "could not be saved, try again" message
    rather than the generic failure copy. Wraps the underlying DB error."""


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
    # A stop has been asked for. Set BEFORE `task.cancel()`, so a second Stop landing while
    # the first is still unwinding answers "already asked" instead of firing a second cancel
    # into the cleanup path (which lands inside the CancelledError arm and can eat the
    # terminal frame the subscriber is waiting for).
    stop_requested: bool = False
    # The pinned extraction's head SHA (Plan turns stamp it onto their options card for
    # U12's stale-plan check); None when no app exists yet.
    head_sha: str | None = None
    # The user-facing reason a turn failed, set alongside the in-band `TurnErrorFrame`. The
    # frame lives only in the ring, so a subscriber whose cursor fell past it (or who arrives
    # after) would otherwise read `turn_status="failed"` with no reason attached.
    error_message: str | None = None
    # The Write turn's newest workspace/preview facts, for the same reason: a `preview` frame
    # that fired before the client connected is gone from the ring by the time a mid-build
    # reconnect asks, so the snapshot carries them instead of a second REST round-trip. None
    # on a chat turn — there is no workspace to describe.
    workspace_state: Literal["preparing", "ready", "unavailable"] | None = None
    preview_url: str | None = None
    preview_state: Literal["ready", "reconnecting"] | None = None
    # WRITE only. `sandbox` is what the six tools act through; `write_session` is the
    # manager's registry entry, kept so the terminal can hand it back (the P0 save). Both
    # None on a chat turn. `preview_task` is the per-turn dev-server watcher — cancelled and
    # AWAITED before the terminal frame, or a late preview frame lands after `[DONE]`.
    sandbox: SandboxSession | None = None
    write_session: BuildSession | None = None
    preview_task: asyncio.Task[None] | None = None
    # Why a non-completed turn ended, in the vocabulary `TurnEndedFrame.reason` publishes.
    end_reason: str | None = None
    # True once the finalize actually pushed a snapshot. TRI-STATE on the wire: None here
    # means "nothing to say" (a chat turn, or a Write turn that never reached the save).
    snapshot_committed: bool | None = None
    # Claim-once for the preview frame, shared by the watcher and the between-verify
    # fallback: whichever sees the dev server first emits, the other stays quiet.
    preview_framed: bool = False

    def text_so_far(self) -> str:
        return "".join(self.text_parts)

    def claim_preview_frame(self) -> bool:
        """True for exactly one caller. Synchronous on purpose — no await between the read
        and the write, so two concurrent emitters cannot both win."""
        if self.preview_framed:
            return False
        self.preview_framed = True
        return True


PersistUserTurn = Callable[[], Awaitable[None]]
SessionFactory = async_sessionmaker[AsyncSession]


def _workspace_of(ctx: RunContext[ChatDeps]) -> ReadOnlyWorkspace:
    """The ChatDeps accessor the mode toolsets resolve the workspace through. Fail-first:
    a mode-gated run without a workspace is a programming error, not an empty app."""
    workspace = ctx.deps.workspace
    if workspace is None:
        raise RuntimeError("mode-gated turn ran without a turn-pinned workspace")
    return workspace


def _sandbox_of(ctx: RunContext[ChatDeps]) -> SandboxSession:
    """The ChatDeps accessor Write's sandbox toolset resolves through — the same shape as
    `_workspace_of`, one field over.

    Fail-first for the same reason: a Write run reaching a tool with no sandbox attached is
    a programming error in the attach path, and the only honest response is to say so. The
    degraded alternative — hand the tool a scratch directory, or let it no-op — would let a
    build report success having written nothing anyone can ever reach."""
    session = ctx.deps.sandbox
    if session is None:
        raise RuntimeError("Write turn ran without an attached sandbox")
    return session


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
        project_id: uuid.UUID,
        manager: SessionManager,
        sandbox_client: SandboxClient | None = None,
    ) -> uuid.UUID:
        """Claim the conversation, persist the user turn (caller-supplied writer, so the
        route's typed seq-contention mapping stays where the route owns it), spawn the
        detached run, return the turn id. Raises `ConversationBusyError` (guard) or whatever
        `persist_user_turn` raises — with the claim released.

        WRITE no longer refuses here (U5). A Write turn is an ordinary turn with more tools,
        so it needs two extra things the read modes never do: `manager` + `sandbox_client` to
        attach a live sandbox, and `project_id` to resolve which app that sandbox serves.
        `sandbox_client` stays optional because a deployment without a configured sandbox is
        a supported state for Ask/Plan — the Write path fails loudly on None rather than
        making every read turn 503 on a dependency it does not use."""
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
                    project_id=project_id,
                    model=model,
                    session_factory=session_factory,
                    manager=manager,
                    sandbox_client=sandbox_client,
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
        if state.status != "running" or state.task is None or state.stop_requested:
            return False
        state.stop_requested = True
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
        project_id: uuid.UUID,
        model: Model,
        session_factory: SessionFactory,
        manager: SessionManager,
        sandbox_client: SandboxClient | None,
    ) -> None:
        """The whole turn, detached: workspace pin → mode-gated run (streaming frames) →
        transcript append → billing → terminal. Mirrors the relay drain's ordering; every
        exit path funnels to exactly one terminal frame, the guard release, AND the billing of
        whatever tokens the model actually consumed.

        WRITE forks at the run (U5), not at the guard: same pin, same reminder, same terminal
        arms, same `finally` — but a node-by-node self-heal loop in place of the single
        `agent.run`, because a build meters and persists per model step rather than once at
        the end. Everything around that fork is deliberately shared, so a fix to the terminal
        handling can never apply to only one of the two."""
        # One usage accumulator threaded through both the primary run and the forced retry
        # (they increment it in place). Because it survives the run, an explicit Stop that
        # cancels the model mid-flight — or a DB error after the model replied — still bills the
        # tokens already spent, closing the daily-cap bypass a start→stop loop would open.
        turn_usage = RunUsage()
        billed = False

        async def _bill_once() -> None:
            """Fold the accumulated spend into today's cap exactly once, in a FRESH session
            (the run's own session may already be unwound on the cancel/error paths).
            Best-effort like the relay drain — a billing failure must never mask the outcome."""
            nonlocal billed
            if billed:
                return
            billed = True
            if turn_usage.input_tokens == 0 and turn_usage.output_tokens == 0:
                return  # the model produced nothing (failed before its first response)
            try:
                async with session_factory() as bill_db:
                    await record_usage(
                        bill_db,
                        state.user_id,
                        input_tokens=turn_usage.input_tokens,
                        output_tokens=turn_usage.output_tokens,
                        cache_read_tokens=turn_usage.cache_read_tokens,
                        cache_write_tokens=turn_usage.cache_write_tokens,
                    )
                    await bill_db.commit()
            except Exception:
                _log.exception(
                    "turn_billing_failed",
                    conversation_id=str(state.conversation_id),
                    turn_id=str(state.turn_id),
                )

        try:
            workspace = await self._pin_workspace(
                state,
                app_id,
                project_id=project_id,
                session_factory=session_factory,
                manager=manager,
                sandbox_client=sandbox_client,
            )
            # U14: the ephemeral mode reminder rides as a fresh tail message on the run's
            # history at cadence turns. Rebinding `history` here keeps the retry run below
            # consistent (it extends the same list) while `new_messages()` — the only thing
            # persisted — structurally never contains it.
            reminder = _reminder_text(state.mode, history)
            if reminder is not None:
                history = [*history, ModelRequest(parts=[UserPromptPart(content=reminder)])]
            if state.mode is ConversationMode.WRITE:
                # A Write turn bills PER MODEL STEP, inside the loop — `record_usage` is
                # called once per step and that is the only fold. Claiming the turn as
                # already billed here is what stops `_bill_once` from folding the same
                # tokens a second time at the terminal and doubling every build's daily
                # spend. (`turn_usage` stays untouched: no `usage=` is passed to the run.)
                billed = True
                await self._run_write(
                    state,
                    prompt=prompt,
                    history=history,
                    prompt_context=prompt_context,
                    workspace=workspace,
                    model=model,
                    session_factory=session_factory,
                )
            else:
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
                        usage=turn_usage,
                        event_stream_handler=self._event_handler(state),
                    )
                    batches: list[tuple[list[ModelMessage], dict[str, Any] | None]] = []
                    deferred = _deferred_call(result.output)
                    batches.append(
                        (
                            _persistable_messages(result.new_messages()),
                            self._pending_meta(state, deferred),
                        )
                    )

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
                        # is ephemeral (only response + tool-return rows persist). A retry that
                        # STILL produces no call falls through to the synthesized card rather than
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
                                usage=turn_usage,
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
                            deferred = _deferred_call(retry.output)
                            batches.append(
                                (
                                    _persistable_messages(retry.new_messages()),
                                    self._pending_meta(state, deferred),
                                )
                            )

                    # WRITE-BEFORE-DONE (U5 policy): the reply must be durable before the
                    # turn may claim success. A failure of the persist seam is DISTINCT from
                    # a model failure — it raises the typed `_PersistFailedError` so the
                    # subscriber sees the "could not be saved" message, not the generic one.
                    # The user request is already durable (pre-run write) — append the
                    # responses PLUS their tool-return requests.
                    try:
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
                        await db.commit()
                    except Exception as exc:
                        raise _PersistFailedError from exc
            await _bill_once()
            self._finish(state, "completed")
        except asyncio.CancelledError:
            # The explicit stop endpoint cancelled us. The user turn stays (it happened); no
            # reply row is written (none finished) — but the tokens the model already produced
            # still count toward the daily cap.
            #
            # TERMINAL FIRST, THEN BILL. `_finish` is synchronous, so emitting it here reaches
            # every subscriber with no await in between for a second cancellation to land in.
            # Billing awaits, so it is the one step a repeat cancel could interrupt — and a
            # lost billing row is a far smaller wrong than a subscriber that never learns the
            # turn ended and hangs until its stall timeout.
            self._finish(state, "stopped")
            with suppress(asyncio.CancelledError):
                await _bill_once()
        except _WriteEndedError as ended:
            # A Write turn that stopped for a NAMED reason rather than a failure: the quota
            # ran out, the self-heal budget did, the wall clock did, or the model hit its
            # step ceiling. The spend is already billed per step; what is left is to say why
            # in words the citizen can act on, and to carry the reason onto the terminal
            # frame so the client can render the right banner instead of a generic error.
            # Bound to locals: Python unbinds the `as` name at the end of the except block,
            # so a closure that reads it is a latent NameError waiting on a refactor.
            state.end_reason = ended.reason
            message = ended.message
            state.error_message = message
            self._emit(state, lambda seq: TurnErrorFrame(seq=seq, message=message))
            await _bill_once()
            self._finish(state, "failed")
        except _PersistFailedError:
            _log.exception(
                "turn_persist_failed",
                conversation_id=str(state.conversation_id),
                turn_id=str(state.turn_id),
            )
            # The model spend still counts even though the reply could not be saved.
            await _bill_once()
            state.error_message = _PERSIST_FAILED_MESSAGE
            self._emit(
                state,
                lambda seq: TurnErrorFrame(seq=seq, message=_PERSIST_FAILED_MESSAGE),
            )
            self._finish(state, "failed")
        except Exception:
            _log.exception(
                "turn_run_failed",
                conversation_id=str(state.conversation_id),
                turn_id=str(state.turn_id),
            )
            # Partial spend before the failure still counts (mirrors the relay's bill-what-ran).
            await _bill_once()
            state.error_message = _TURN_FAILED_MESSAGE
            self._emit(
                state,
                lambda seq: TurnErrorFrame(seq=seq, message=_TURN_FAILED_MESSAGE),
            )
            self._finish(state, "failed")
        finally:
            # THE SAVE, on every single terminal arm — completed, stopped, persist-failed,
            # named-end, or a genuine bug. `finish_turn_sandbox` is the only thing that pushes
            # the sandbox tree to Blob storage, so anything that reaches this `finally`
            # without it has silently thrown the user's work away.
            #
            # `asyncio.shield` is not belt-and-braces: the STOPPED path arrives here because
            # the task was cancelled, and an unshielded await would be cancelled again the
            # instant it yielded — losing precisely the edits a user hit Stop to keep.
            # Errors are suppressed for the end-sequence reason: a Blob blip must not stop
            # the guard from being released and wedge the conversation shut forever.
            if state.write_session is not None and sandbox_client is not None:
                with suppress(Exception):
                    await asyncio.shield(
                        manager.finish_turn_sandbox(
                            state.write_session,
                            sandbox_client,
                            # Only a turn that MUTATED the tree is worth bundling. An Ask or
                            # Plan turn holds no tool that could set this, so it releases the
                            # sandbox without paying for an upload of a tree it only read.
                            touched=state.sandbox is not None and state.sandbox.workspace_touched,
                        )
                    )
            release_conversation(state.conversation_id)

    async def _pin_workspace(
        self,
        state: _TurnState,
        app_id: uuid.UUID | None,
        *,
        project_id: uuid.UUID,
        session_factory: SessionFactory,
        manager: SessionManager,
        sandbox_client: SandboxClient | None,
    ) -> ReadOnlyWorkspace:
        """Resolve the turn-pinned read surface ONCE, for EVERY mode: the project's live
        container.

        Ask and Plan used to read a different thing entirely — a git checkout of the app's
        saved bundle, unpacked onto the control-plane server's own disk. Two problems with
        that, and only one of them was staleness. It described a COPY: anything the agent had
        done since the last save was invisible, and an answer about the copy could be
        confidently wrong about the app the user was looking at. And the copy is a bare
        checkout, so nothing in it could ever be run — no dependencies, no build, no dev
        server. One workspace for every mode removes both by construction, and the
        coherence question ("does Ask see what Write just did?") stops being something we
        have to keep getting right.

        A NEW PROJECT GETS THE CONTAINER TOO (user, 2026-07-30). An earlier cut of this
        withheld it until a snapshot existed, on the grounds that a fresh project's container
        is the golden template and Ask might describe scaffolding as the user's work. That is
        the wrong trade: Plan writing the FIRST build is exactly when the agent most needs to
        run real commands against a real tree, and a mode that cannot do that produces a plan
        built on nothing. The template is a truthful answer about a project with no code yet —
        the model can see it is a template. Withholding the tree was the more misleading
        option, and it is gone.

        `head_sha` is stamped from the snapshot on the read paths only. It pins a Plan card
        to a version so Build-it can notice the app moved underneath it; a live tree has no
        fixed version to pin, which is the point of it being live."""
        attach = partial(
            self._attach_sandbox,
            state,
            project_id=project_id,
            session_factory=session_factory,
            manager=manager,
            sandbox_client=sandbox_client,
        )
        if sandbox_client is None and state.mode is not ConversationMode.WRITE:
            # THE SANDBOX SERVICE IS NOT CONFIGURED — a deployment fact, and the only reason
            # left to read anything but the container. Not to be confused with "this project
            # is new": a new project gets the container like everybody else (below). Read
            # modes degrade to the last saved bundle rather than failing outright; Write falls
            # through and fails loudly, because it cannot do its job without a container.
            if app_id is None:
                return EmptyProjectWorkspace(app_id=uuid.UUID(int=0))
            extracted = await extract_snapshot(app_id)
            if isinstance(extracted, NoAppYet):
                return EmptyProjectWorkspace(app_id=app_id)
            state.head_sha = extracted.head_sha
            return ExtractedSnapshotWorkspace(root=extracted.root)
        if state.mode is ConversationMode.PLAN and app_id is not None:
            # Best-effort, and only for the version PIN — a Plan card records a snapshot head
            # so Build-it can notice the app moved underneath it. It does not decide what Plan
            # READS; that is the container, below. No snapshot yet simply means no pin, which
            # costs a stale-plan warning and never the turn.
            extracted = await extract_snapshot(app_id)
            if not isinstance(extracted, NoAppYet):
                state.head_sha = extracted.head_sha
        return LiveSandboxWorkspace(session=await attach())

    # -- the WRITE run -------------------------------------------------------------------

    async def _attach_sandbox(
        self,
        state: _TurnState,
        *,
        project_id: uuid.UUID,
        session_factory: SessionFactory,
        manager: SessionManager,
        sandbox_client: SandboxClient | None,
    ) -> SandboxSession:
        """Get this turn a live sandbox, narrating the wait.

        Provisioning or restoring takes 30-60s, which is why it happens HERE — detached,
        after the 202 — rather than inside the request. A citizen watching a spinner on a
        POST that has not answered in a minute assumes the product is broken; a citizen
        watching "Getting your workspace ready" knows what is happening. That is the whole
        reason `WorkspaceFrame` exists.

        Fails LOUDLY. `SnapshotUnavailableError` in particular must never degrade into a
        fresh blank template: the model would start editing an empty app and commit the
        result over the user's real one."""
        if sandbox_client is None:
            raise _WriteEndedError(
                "sandbox_unavailable",
                "The workspace service is not available right now. Please try again shortly.",
            )
        state.workspace_state = "preparing"
        self._emit(
            state,
            lambda seq: WorkspaceFrame(
                seq=seq, state="preparing", message="Getting your workspace ready…"
            ),
        )
        try:
            # A SHORT session, opened and closed around the attach: the turn that follows
            # runs for minutes, and holding a pooled connection across it would pin one
            # idle-in-transaction for the whole build.
            async with session_factory() as db:
                user = await db.get(User, state.user_id)
                if user is None:  # the FK guarantees this; fail loudly if it ever breaks
                    raise _WriteEndedError("sandbox_unavailable", _TURN_FAILED_MESSAGE)
                session = await manager.ensure_sandbox(
                    db, user, project_id, sandbox_client=sandbox_client
                )
        except _WriteEndedError:
            raise
        except Exception as exc:
            _log.exception(
                "write_sandbox_attach_failed",
                conversation_id=str(state.conversation_id),
                turn_id=str(state.turn_id),
            )
            state.workspace_state = "unavailable"
            message = _sandbox_unavailable_message(exc)
            self._emit(
                state, lambda seq: WorkspaceFrame(seq=seq, state="unavailable", message=message)
            )
            raise _WriteEndedError("sandbox_unavailable", message) from exc

        state.write_session = session
        state.sandbox = SandboxSession(
            sandbox_client=sandbox_client,
            handle=session.handle,
            app_id=session.app_id,
            # No emitter: the turn engine renders the run's own tool events as step frames,
            # and a second feed would draw every step twice.
            emitter=None,
        )
        state.workspace_state = "ready"
        self._emit(state, lambda seq: WorkspaceFrame(seq=seq, state="ready"))
        state.preview_task = asyncio.create_task(self._watch_preview(state))
        return state.sandbox

    async def _run_write(
        self,
        state: _TurnState,
        *,
        prompt: str | list[str | BinaryContent],
        history: list[ModelMessage],
        prompt_context: PromptContext,
        workspace: ReadOnlyWorkspace,
        model: Model,
        session_factory: SessionFactory,
    ) -> None:
        """The self-heal loop: run, verify, repair, repeat until the app is objectively
        green and the model has said it is done — or a bound stops us.

        Both halves of that gate are load-bearing. `declare_done` alone is the model's
        opinion, and a model that has just written a type error is not a reliable witness;
        `green` alone would end the turn mid-thought the first time the tree happened to
        compile. Only the conjunction means finished."""
        sandbox = state.sandbox
        if sandbox is None:  # `_pin_workspace` sets it or raises; belt for the impossible
            raise _WriteEndedError("sandbox_unavailable", _TURN_FAILED_MESSAGE)
        budget = SELF_HEAL_MAX_RETRIES
        turn_prompt: str | list[str | BinaryContent] = prompt
        messages: list[ModelMessage] = list(history)
        log_cursor = 0
        iteration = 0
        # Monotonic, never wall-clock time-of-day (which can jump). The count ceilings bound
        # how many requests and repairs may run, but not how long any one of them takes — a
        # wedged `npm install` would otherwise hold the container and the user's one build
        # slot for hours.
        loop_started = time.monotonic()
        try:
            while True:
                if time.monotonic() - loop_started > RUN_WALL_CLOCK_DEADLINE_S:
                    raise _WriteEndedError(
                        "wall_clock_deadline_exceeded",
                        "This is taking much longer than expected, so it has been stopped. "
                        "Your work is saved — send a message to pick it back up.",
                    )
                # The count ceilings bound requests and repairs; this bounds elapsed time,
                # which neither of them does. Checked BETWEEN iterations, so a run already
                # in flight finishes rather than being torn out mid-write.
                if iteration:
                    # The machine's own re-prompt, persisted HIDDEN. It has to be in the DB:
                    # the delta filter drops it (it is not the user's words), and without a
                    # row a later turn replays two consecutive model responses with nothing
                    # between them. `load_history` ignores visibility so the model still
                    # reads it; the projection skips hidden rows so the citizen never does.
                    await self._persist_write_reprompt(state, turn_prompt, session_factory)
                sandbox.done_requested = False  # per-iteration; the flag means "this run"
                try:
                    messages = await self._run_write_once(
                        state,
                        turn_prompt=turn_prompt,
                        messages=messages,
                        prompt_context=prompt_context,
                        workspace=workspace,
                        model=model,
                        session_factory=session_factory,
                    )
                except UsageLimitExceeded as exc:
                    # The model burned its per-run request ceiling — usually a loop, not a
                    # hard problem. Named, because "hit a problem" would send the user to
                    # support when the right advice is to narrow the ask.
                    raise _WriteEndedError(
                        "request_limit",
                        "The assistant took too many steps on this one without finishing. "
                        "Your work is saved — try asking for a smaller change.",
                    ) from exc
                iteration += 1

                # THE MUTATION GUARD. A Write turn where the model only read files and
                # answered a question is an ordinary chat turn that happened to have write
                # tools available. Verifying it would spend 30s of the user's time and a
                # `tsc` run to confirm nothing changed, then nudge the model to keep going.
                if not (sandbox.workspace_touched or sandbox.done_requested):
                    return

                self._emit_verify_step(state, iteration, phase="started")
                outcome, log_cursor = await verify(
                    sandbox.sandbox_client,
                    sandbox.handle,
                    log_cursor=log_cursor,
                    max_polls=READINESS_MAX_POLLS,
                    poll_s=READINESS_POLL_S,
                )
                self._emit_verify_step(state, iteration, phase="finished", ok=outcome.green)

                if outcome.dev_ready and state.claim_preview_frame():
                    self._emit_preview_ready(
                        state, outcome.preview_url or sandbox.handle.preview_url
                    )

                if outcome.green and sandbox.done_requested:
                    state.snapshot_committed = None  # the finalize answers this, not us
                    return

                # `error is None` does NOT imply green: a clean `tsc` with a dev server that
                # never came up is red with nothing to report. Synthesize the server error
                # or a budget-exhausted turn ends with no diagnostic at all.
                error = outcome.error
                if error is None and not outcome.green:
                    error = dev_not_ready_error()
                if budget <= 0:
                    raise _WriteEndedError(
                        "self_heal_budget_exhausted",
                        "Your app still has an error the assistant could not fix on its own. "
                        "Your work is saved — tell it what you see and it will try again.",
                    )
                if error is not None:
                    source, title, stack = error.source, error.title, error.cleaned_stack
                    self._emit(
                        state,
                        lambda seq: DiagnosticFrame(
                            seq=seq,
                            source=source,
                            title=title,
                            cleaned_stack=stack,
                        ),
                    )
                    turn_prompt = build_repair_prompt(error)
                else:
                    # Green, but the model never said it was done — a nudge, not an error.
                    turn_prompt = CONTINUE_PROMPT
                budget -= 1
        finally:
            # Cancel AND await the watcher before any terminal frame is emitted: a preview
            # frame that lands after `turn_ended` arrives after the transport has already
            # sent `[DONE]`, so it is not late — it is lost.
            await self._stop_preview_watcher(state)

    async def _run_write_once(
        self,
        state: _TurnState,
        *,
        turn_prompt: str | list[str | BinaryContent],
        messages: list[ModelMessage],
        prompt_context: PromptContext,
        workspace: ReadOnlyWorkspace,
        model: Model,
        session_factory: SessionFactory,
    ) -> list[ModelMessage]:
        """One `agent.iter` run, walked node by node, returning the accumulated history.

        Walked rather than `agent.run` for two reasons that both matter. The daily cap is
        enforced before EVERY model request, in its own short session closed before the call
        — the route's single check at the top would let one long build spend a whole day's
        budget after passing it once. And each step's tokens are recorded as they are spent,
        so a build that dies at step 40 has still paid for steps 1-39."""
        deps = ChatDeps(
            user_id=state.user_id,
            mode=ConversationMode.WRITE,
            prompt_context=prompt_context,
            workspace=workspace,
            sandbox=state.sandbox,
        )
        persisted_from = len(messages)
        async with chat_agent.iter(
            turn_prompt,
            deps=deps,
            model=model,
            message_history=messages,
            toolsets=toolsets_for_mode(ConversationMode.WRITE, _workspace_of, _sandbox_of),
            output_type=str,
            usage_limits=UsageLimits(request_limit=MODEL_TURN_CEILING),
            # Without `max_tokens` pydantic-ai's Anthropic default of 4096 truncates a
            # whole-file `write_file` mid-string — the file lands syntactically broken and
            # the model spends a self-heal round repairing its own truncation. The three
            # cache flags put breakpoints on the context this loop re-sends VERBATIM every
            # step: the instructions and tool definitions never change across a build.
            model_settings=AnthropicModelSettings(
                max_tokens=MAX_OUTPUT_TOKENS,
                temperature=TEMPERATURE,
                anthropic_cache_instructions=CACHE_TTL,
                anthropic_cache_tool_definitions=CACHE_TTL,
                anthropic_cache=CACHE_TTL,
            ),
            # Deliberately NO `usage=`: this run's spend is folded per step below, and
            # passing the turn accumulator as well would bill every token twice.
        ) as run:
            node = run.next_node
            while not Agent.is_end_node(node):
                if Agent.is_model_request_node(node):
                    async with session_factory() as gate_db:
                        try:
                            await enforce_daily_limit(gate_db, state.user_id)
                        except DailyTokenLimitExceededError as exc:
                            # The request never fires. Graceful, not a crash: the work so
                            # far is real and the finalize will save it.
                            limit, used = exc.limit, exc.used
                            resets_at = next_ist_midnight_iso()
                            self._emit(
                                state,
                                lambda seq: QuotaFrame(
                                    seq=seq,
                                    limit=limit,
                                    used=used,
                                    resets_at=resets_at,
                                ),
                            )
                            raise _WriteEndedError(
                                "quota_exceeded",
                                "You have used today's token budget. Your work is saved — "
                                "this picks back up after midnight IST.",
                            ) from exc
                    async with node.stream(run.ctx) as stream:
                        async for event in stream:
                            self._on_event(state, event)
                    node = await run.next(node)
                    if Agent.is_call_tools_node(node):
                        await self._record_write_step(
                            state, node.model_response.usage, session_factory
                        )
                elif Agent.is_call_tools_node(node):
                    # A DISTINCT loop variable from the model-request branch above: the two
                    # nodes stream different event unions, and reusing the name pins it to
                    # whichever one mypy saw first.
                    async with node.stream(run.ctx) as tool_stream:
                        async for tool_event in tool_stream:
                            self._on_event(state, tool_event)
                    node = await run.next(node)
                    # The step's tools have executed and their returns are in the history,
                    # so the step is complete — persist before the next request fires.
                    persisted_from = await self._persist_write_step(
                        state,
                        history=run.all_messages(),
                        persisted_from=persisted_from,
                        session_factory=session_factory,
                    )
                else:
                    # The user-prompt node: no model call, no tools, nothing to stream.
                    node = await run.next(node)
                    # The cursor's true origin (KTD-7). The node above just CLEANED the
                    # injected history — consecutive ModelRequests merged, the list shrunk —
                    # so the pre-clean `len(messages)` seeded outside the loop overshoots,
                    # and the first persist would skip the run's first ModelResponse: the
                    # row whose orphaned tool answers brick the conversation on every later
                    # turn. `new_message_index` is the same post-clean accounting
                    # `result.new_messages()` is built on — the one that keeps Ask/Plan
                    # immune. One shared expression, N readers.
                    persisted_from = run.ctx.deps.new_message_index
            result = run.result
            if result is not None:
                messages = result.all_messages()
                await self._persist_write_step(
                    state,
                    history=messages,
                    persisted_from=persisted_from,
                    session_factory=session_factory,
                )
        return messages

    async def _record_write_step(
        self, state: _TurnState, usage: RequestUsage, session_factory: SessionFactory
    ) -> None:
        """Fold ONE model step's spend, in its own session with its own commit. Best-effort
        by design: a metering failure must not kill a build that is otherwise going fine, and
        the next step's `enforce_daily_limit` still reads whatever did land."""
        try:
            async with session_factory() as db:
                await record_usage(
                    db,
                    state.user_id,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    cache_read_tokens=usage.cache_read_tokens,
                    cache_write_tokens=usage.cache_write_tokens,
                )
                await db.commit()
        except Exception:
            _log.exception(
                "write_step_billing_failed",
                conversation_id=str(state.conversation_id),
                turn_id=str(state.turn_id),
            )

    async def _persist_write_step(
        self,
        state: _TurnState,
        *,
        history: list[ModelMessage],
        persisted_from: int,
        session_factory: SessionFactory,
    ) -> int:
        """Append one step's messages and return the new cursor.

        `_persistable_messages` is what makes a Write step's rows honest: the user's prompt
        is already durable (written before the run started), so the delta's leading
        `UserPromptPart` has to be dropped or it is stored twice and the second copy renders
        as a second user bubble in the transcript. That is the whole of the duplicate-seed
        defect, killed structurally — no step row can carry a user prompt at all."""
        delta = _persistable_messages(list(history[persisted_from:]))
        if not delta:
            return len(history)
        try:
            async with session_factory() as db:
                await append_batch(
                    db,
                    user_id=state.user_id,
                    conversation_id=state.conversation_id,
                    messages=delta,
                    entry_kind=MessageEntryKind.STEP,
                    mode=ConversationMode.WRITE,
                    meta={"kind": "write_step", "turnId": str(state.turn_id)},
                )
                await db.commit()
        except Exception as exc:
            raise _PersistFailedError from exc
        return len(history)

    async def _persist_write_reprompt(
        self,
        state: _TurnState,
        turn_prompt: str | list[str | BinaryContent],
        session_factory: SessionFactory,
    ) -> None:
        """The repair/continue prompt, stored HIDDEN. Best-effort: losing it costs a slightly
        odd replay in a later turn, while raising here would fail a build that is working."""
        if not isinstance(turn_prompt, str):
            return
        try:
            async with session_factory() as db:
                await append_batch(
                    db,
                    user_id=state.user_id,
                    conversation_id=state.conversation_id,
                    messages=[ModelRequest(parts=[UserPromptPart(content=turn_prompt)])],
                    entry_kind=MessageEntryKind.STEP,
                    mode=ConversationMode.WRITE,
                    visibility=MessageVisibility.HIDDEN,
                    meta={"kind": "write_reprompt", "turnId": str(state.turn_id)},
                )
                await db.commit()
        except Exception:
            _log.exception(
                "write_reprompt_persist_failed",
                conversation_id=str(state.conversation_id),
                turn_id=str(state.turn_id),
            )

    def _emit_verify_step(
        self,
        state: _TurnState,
        iteration: int,
        *,
        phase: Literal["started", "finished"],
        ok: bool = False,
    ) -> None:
        """The verify spinner. Synthetic and never persisted — it is a progress affordance
        for a 30s wait, not part of the record, and it is correct for it to vanish on
        reload."""
        started = phase == "started"
        item = StepItem(
            seq=0,
            mode=ConversationMode.WRITE.value,
            tool="verify",
            label=(
                "Checking your app…"
                if started
                else ("Build verified." if ok else "Not green yet — continuing.")
            ),
            # `pending` IS the in-flight state in this vocabulary — the same one a real
            # tool call sits in between its call and its return.
            state="pending" if started else ("ok" if ok else "failed"),
            hidden=False,
            detail=step_detail(None, None),
        )
        # The SAME tool_call_id for both phases, which is how the client replaces the
        # pending card in place instead of stacking two rows.
        tool_call_id = f"verify-{state.turn_id}-{iteration}"
        self._emit(
            state,
            lambda seq: StepFrame(seq=seq, tool_call_id=tool_call_id, phase=phase, item=item),
        )

    def _emit_preview_ready(self, state: _TurnState, preview_url: str | None) -> None:
        state.preview_url = preview_url
        state.preview_state = "ready"
        self._emit(
            state,
            lambda seq: PreviewFrame(seq=seq, state="ready", preview_url=preview_url),
        )

    async def _watch_preview(self, state: _TurnState) -> None:
        """Poll the dev server so the preview appears the moment it is servable, and so a
        crash is REPORTED rather than left as a blank iframe.

        This has to live server-side: `/dev/status` is bearer-guarded, so the browser cannot
        ask, and only the server holds the supervisor token. Every failure is swallowed — a
        watcher that raised would take the build down with it over a polling blip."""
        sandbox = state.sandbox
        if sandbox is None:
            return
        reconnecting = False
        while True:
            try:
                status = await sandbox.sandbox_client.dev_status(sandbox.handle)
            except SandboxError:
                # A supervisor blip, or a sandbox that is genuinely gone. Neither is the
                # watcher's to escalate — the between-steps verify and the loop own health.
                # A poll error must never ESCAPE a managed task, or it resurfaces as an
                # unretrieved exception at teardown.
                await asyncio.sleep(READINESS_POLL_S)
                continue
            if status.ready:
                if state.claim_preview_frame() or reconnecting:
                    # First serve, or recovered after a crash — either way the client needs
                    # the url to (re)mount its iframe on.
                    self._emit_preview_ready(state, sandbox.handle.preview_url)
                reconnecting = False
            elif state.preview_framed and not status.running and not reconnecting:
                # The dev process exited AFTER we framed. Said once, distinctly: without it
                # a dead iframe masquerades as "still building" until the turn ends.
                reconnecting = True
                state.preview_state = "reconnecting"
                self._emit(state, lambda seq: PreviewFrame(seq=seq, state="reconnecting"))
            await asyncio.sleep(READINESS_POLL_S)

    async def _stop_preview_watcher(self, state: _TurnState) -> None:
        task = state.preview_task
        if task is None:
            return
        state.preview_task = None
        task.cancel()
        # AWAIT the cancellation, do not just request it: an un-awaited watcher can still be
        # mid-`_emit` and land a frame after the terminal, which the transport has closed.
        with suppress(asyncio.CancelledError, Exception):
            await task

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
            # Redacted HERE, at the frame boundary — the persistence seam redacts the rows,
            # so without this the LIVE stream showed a secret the reload had already masked.
            # Same function both sides, so the two renderings can never disagree.
            detail=step_detail(redact_secrets(args_json), None),
        )

    def _resolve_step(self, state: _TurnState, event: FunctionToolResultEvent) -> StepItem | None:
        pending = state.steps.get(event.tool_call_id)
        if pending is None:
            return None
        part = event.part
        failed = not isinstance(part, ToolReturnPart)  # a RetryPromptPart = refused/failed
        content = (
            redact_secrets(part.model_response_str()) if isinstance(part, ToolReturnPart) else None
        )
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
            # EVERY in-flight step, hidden ones included. `hidden` is a RENDER hint (the live
            # tail and the reload projection both ship hidden steps with full detail); making
            # it a payload filter HERE meant a client that reconnected mid-turn silently lost
            # steps the other two paths kept.
            steps=list(state.steps.values()),
            error_message=state.error_message,
            workspace_state=state.workspace_state,
            preview_url=state.preview_url,
            preview_state=state.preview_state,
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
