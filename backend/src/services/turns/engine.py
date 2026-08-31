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
exactly `toolsets_for_kind(conversation.kind)` over the turn-pinned workspace, and the
U9-composed instructions for that kind. Plan turns bill like the relay (one drain,
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
from dataclasses import dataclass, field, replace
from functools import partial
from typing import Any, Final, Literal

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
from pydantic_ai.tools import DeferredToolRequests
from pydantic_ai.usage import RequestUsage, RunUsage, UsageLimits
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.api.v1.build_sessions.schemas import LIVENESS_LEASE_RENEW_CADENCE_SECONDS, ErrorSource

# The ONE ceiling, imported rather than re-spelled: the offer refuses a plan the send route
# would refuse as a message, and two literals is how those two numbers drift apart.
from src.api.v1.conversations._shared import MAX_MESSAGE_TEXT_CHARS
from src.api.v1.conversations.schemas import (
    CompileFrame,
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
from src.core.integrity_types import BaselineIdentity
from src.db.models.conversation import ChatKind, Conversation
from src.db.models.harness_counter import HarnessCounter
from src.db.models.message import MessageEntryKind, MessageVisibility
from src.db.models.user import User
from src.services.agent.agent import ChatDeps, chat_agent
from src.services.agent.mode_prompts import PromptContext, workspace_note
from src.services.agent.read_tools import (
    LiveSandboxWorkspace,
    ReadOnlyWorkspace,
)
from src.services.agent.toolsets import toolsets_for_kind
from src.services.build_sessions.alarms import HMR_PROTOCOL_DRIFT_EVENT
from src.services.build_sessions.counters import count
from src.services.build_sessions.integrity import (
    baseline_identity,
    has_ever_been_built,
    stamp_the_watermark,
)
from src.services.build_sessions.locks import release_liveness_lease, renew_liveness_lease
from src.services.build_sessions.manager import (
    BuildSession,
    BuildSessionConflictError,
    RecoveryNews,
    SandboxReclaimBlockedError,
    SessionManager,
    SnapshotUnavailableError,
    WorkspaceUnreadableError,
)
from src.services.build_sessions.outcome import STOPPED_BY_USER
from src.services.messages.projection import (
    PLAN_OPTIONS_TOOL,
    TURN_TERMINAL_KIND,
    DisplayItem,
    PlanOptionsItem,
    StepItem,
    classify_tool_call,
    long_operation_line,
)
from src.services.messages.store import append_batch
from src.services.orchestrator.client_errors import discard_client_errors
from src.services.orchestrator.constants import (
    CACHE_TTL,
    CRASH_EDGE_CONSECUTIVE_POLLS,
    MAX_OUTPUT_TOKENS,
    MODEL_TURN_CEILING,
    READINESS_MAX_POLLS,
    READINESS_POLL_S,
    RUN_WALL_CLOCK_DEADLINE_S,
    SELF_HEAL_MAX_RETRIES,
    TEMPERATURE,
    WORKSPACE_NOTE_MAX_POLLS,
)
from src.services.orchestrator.deps import SandboxSession
from src.services.orchestrator.prompt import build_repair_prompt
from src.services.orchestrator.selfheal import (
    CONTINUE_PROMPT,
    HealthState,
    Readiness,
    VerifyOutcome,
    dev_not_ready_error,
    verify,
    where_are_we,
)
from src.services.redis import get_redis
from src.services.sandbox import SandboxClient, SandboxError
from src.services.sandbox.base import CompileState
from src.services.turns.copy import (
    COULD_NOT_CHECK_TEXT,
    COULD_NOT_CONFIRM_TEXT,
    DID_NOT_COME_TOGETHER_TEXT,
    NOT_RECOVERED_TEXT,
    PLAN_NOT_KEPT_TEXT,
    RECOVERED_TEXT,
    STILL_SHOWING_EARLIER,
    STILL_SHOWING_NOTHING,
    STILL_SHOWING_TEMPLATE,
    UNVERIFIED_TEXT,
    WRITING_UP_THE_PLAN_LABEL,
)
from src.services.turns.guard import claim_conversation, release_conversation
from src.services.turns.plan_options import META_PENDING
from src.services.usage.gate import (
    DailyTokenLimitExceededError,
    at_limit_ending,
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

# =====================================================================================
# U17/R24 — THE TWO THINGS THE HARNESS SAYS WHEN NOTHING ELSE IS SPEAKING
# =====================================================================================
#
# Both are the PLATFORM's words, never the agent's, and both are pinned here rather than asked
# for in a prompt. An acknowledgement the model has to remember to write is an acknowledgement
# that arrives AFTER the first model request — which is exactly the silence it exists to cover.
#
# THE ACKNOWLEDGEMENT is a transient feed row, not a transcript message. It is emitted
# synchronously inside `start_turn`, before the detached task is even created, so "before any
# model work" is a structural fact rather than a timing hope. It is deliberately never written
# into `state.steps`, which is what keeps it out of the persisted rows: a build's transcript must
# not accumulate one "Getting started" per turn. It IS carried on the catch-up snapshot (held in
# `state.acknowledgement`, retired by the first real step) — a client that subscribes a moment
# after the turn starts would otherwise get a still screen, which is the whole point of U17.
# Between two `TextPart`s of one response. Blank, not nothing: concatenating them raw ran the
# last sentence of a block into the first word of the next ("…the workspace.Now let me…").
TEXT_BLOCK_SEPARATOR: Final = "\n\n"

ACK_TEXT = "Getting started on that…"
# The reserved tool name the acknowledgement rides under, so it is identifiable as the
# harness's own row rather than a step the agent took. The portal keys on the same string
# (`ACK_STEP_NAME` in `BuildProgress.tsx`) to keep it out of the finished build's step history
# — it is REPLACED by the first real step, never listed beside it.
ACK_TOOL = "__ack__"
ACK_TOOL_CALL_ID = "__ack__"

# THE STILLNESS THRESHOLD (R24). An operation still running after this long earns a
# plain-language status line of its own, refreshed until it completes. Stated as a number
# rather than as "a stated threshold": eight seconds is the point at which a screen with
# nothing moving on it stops reading as "fast" and starts reading as "stuck", and it is
# comfortably longer than every ordinary file write, so a normal step never flickers one on.
LONG_OPERATION_THRESHOLD_MS = 8_000
# How often that line is re-emitted while the operation runs. The TEXT is stable by
# construction — `long_operation_line` re-derives it from the step's own label, never from a
# clock — so a refresh that changes nothing changes no pixels, and therefore produces no second
# screen-reader announcement. The portal caps announcements at one per 10s on top of that.
LONG_OPERATION_REFRESH_MS = 5_000

# U18/R22 — WHAT A FINISHED BUILD SAYS WHEN THE AGENT HANDED US NOTHING TO SAY.
#
# `declare_done` is terminal now, so the summary it carries is the whole of the completion
# message — and a model that calls it with an empty string would otherwise end a working build
# in silence. The fallback is never the model's own text: the alternative to a summary is a
# sentence the harness wrote, not a scrape of whatever prose happened to precede the tool call,
# because that prose is exactly the register this plan removes.
#
# It says the two things a completion has to: the app is ready, and what the reader can do next.
# Checked against the same no-jargon bar as `services/turns/copy.py` — no file, no command, no
# library, no framework.
_BUILD_FINISHED_FALLBACK = (
    "Your app is ready. Open the preview and try it out, and send another message if you'd "
    "like anything changed."
)

# The row-meta kind stamping a pending options card (real or synthesized). IMPORTED, not
# re-spelled: `plan_options._scan` reads rows by this exact string, so two literals meant a
# typo in either one would silently stop every card from being found.
PENDING_META_KIND = META_PENDING

# The one greppable name for "this turn's R10 liveness lease did not land" (U12). A constant
# rather than two inline literals because the two failure shapes — the store would not answer,
# and there was no registry hash to attach the lease to — are one operational question ("is
# anything protecting live builds right now?"), and an alert cannot be written against a
# string that exists in two spellings. The reason is a field, not part of the event name.
LEASE_RENEW_FAILED_EVENT = "liveness_lease_renew_failed"


def _deferred_call(output: object) -> ToolCallPart | None:
    """The pending `present_plan_options` call when the run ended deferred, else None."""
    if not isinstance(output, DeferredToolRequests):
        return None
    for call in output.calls:
        if call.tool_name == PLAN_OPTIONS_TOOL:
            return call
    return None


def plan_argument_of(part: ToolCallPart) -> str | None:
    """The `plan` argument an offer was called with, stripped — WITHOUT the length ceiling.

    A PRE-MIGRATION CALL TOOK NO ARGUMENTS AT ALL and reads as absent here, which is correct —
    there is no plan in it to find. Those cards were all resolved by revision 0035, so nothing
    live depends on this answer; the handoff refuses them by name.

    Deliberately tolerant of a malformed argument object rather than raising: this runs on the
    turn's own path, and a model that emitted unparseable JSON has produced no plan, which is
    the same answer as an empty one and not a reason to fail a turn that otherwise worked.

    SPLIT OUT SO THE REFUSAL COPY CAN ASK THE QUESTION RATHER THAN INFER THE ANSWER.
    `transition._refusal_for` has to tell "there is no plan here" from "the plan is too long",
    and it used to do that by reverse-engineering which of `plan_from_call`'s branches returned
    `None` — correct only while there are exactly two. A third rejection reason added below
    would have silently reported itself as "too long" to the one person it is not true for."""
    try:
        args = part.args_as_dict()
    except Exception:
        return None
    plan = args.get("plan")
    if not isinstance(plan, str):
        return None
    return plan.strip() or None


def plan_from_call(part: ToolCallPart) -> str | None:
    """The plan an offer carries, or None when the call cannot be honoured (R28a / R44).

    TWO REFUSALS, AND BOTH ARE STRUCTURAL RATHER THAN CHECKS SOMEBODY REMEMBERS. An empty
    argument means the offer would carry nothing to build — the defect the retired prose
    heuristic used to manufacture, a Build it button under a plan nobody wrote. An argument
    past the stored-message ceiling is REFUSED, never trimmed: a plan cut mid-sentence is one
    the citizen agrees to and the build never sees the end of."""
    plan = plan_argument_of(part)
    if plan is None or len(plan) > MAX_MESSAGE_TEXT_CHARS:
        return None
    return plan


def _without_the_call(messages: list[ModelMessage], tool_call_id: str) -> list[ModelMessage]:
    """The run's persistable slice with one tool call removed, and any response it emptied.

    WHY REMOVE IT RATHER THAN STORE IT AND SKIP IT LATER. "No offer is recorded" has to be true
    at EVERY reader, and there are two that answer independently: `plan_options._scan`, which
    finds pending cards from the row meta, and the projection, which draws the card from the
    stored tool call itself. Leaving an unhonourable call on the wire and teaching each reader
    to ignore it is two rules to keep in step, and the projection's rule would have to
    distinguish a migrated call (no argument, still rendered) from a new one (no argument,
    never rendered). Not writing it is one rule, at one place, and it leaves the dangling-call
    repair nothing to stitch."""
    kept: list[ModelMessage] = []
    for message in messages:
        if not isinstance(message, ModelResponse):
            kept.append(message)
            continue
        parts = [
            part
            for part in message.parts
            if not (isinstance(part, ToolCallPart) and part.tool_call_id == tool_call_id)
        ]
        if len(parts) == len(message.parts):
            kept.append(message)
        elif parts:
            kept.append(replace(message, parts=parts))
    return kept


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


# NOTHING HERE READS THE AGENT'S PROSE TO DECIDE PRODUCT STATE, and this is where three things
# that did used to live (R23).
#
# `_looks_plan_shaped` counted list items and looked for a trailing `?` to decide whether the
# model had written a plan. When it said yes and no tool call had been made, a FORCED RETRY
# re-issued the run with the options tool as the only thing it could reach; when that also
# produced no call, `_synthesize_options` FABRICATED a card so the buttons appeared anyway.
#
# It fired wrongly, and the trace is on record (turn 019fc05f-d3df-729d-a688-d33a309bddfd): the
# model laid out three options as A/B/C and closed with "I'm not going to start writing code
# until one of us has moved" — an explicit refusal to finalize. The heuristic read the three
# ANSWER CHOICES as three plan steps, saw no `?` on the final line, and put a Build-it button
# under a plan nobody had agreed to. It was widened twice; the shape of the defect is that no
# amount of widening fixes reading prose to infer intent.
#
# What replaces all three is one deliberate act by the agent: it calls the offer tool and passes
# the plan as the argument. A turn that never calls the tool simply produced no plan, which is
# the correct outcome rather than a defect to compensate for — and the buttons and the plan are
# now the same act, so there is no longer a question of WHICH text the plan was.


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
    if isinstance(exc, SandboxReclaimBlockedError):
        # The route's preflight normally turns this into a 409 the client renders as a choice,
        # so reaching here means the incumbent appeared in the window between the two. Name the
        # project anyway: "could not be started right now" invites a retry that will fail the
        # same way, and hides the one action — saving the other project — that resolves it.
        #
        # Hedge on the tri-state exactly as `reclaim_blocked_response` and the dialog do.
        # `dirty=None` means nobody could question that container — including the arm where we
        # could not even reach it — and stating "has unsaved changes" there asserts something
        # the system does not know (#83 review, finding 10).
        unsaved = "has unsaved changes" if exc.dirty else "may have unsaved changes"
        return (
            f"“{exc.project_name}” is still open and {unsaved}. "
            "Save or close it, then send this again."
        )
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
    kind: ChatKind
    status: TurnStatus = "running"
    seq: int = 0
    ring: deque[TurnStreamFrame] = field(default_factory=lambda: deque(maxlen=RING_MAXLEN))
    text_parts: list[str] = field(default_factory=list)
    # U15/R20 — BUILD-chat prose, held until we know what it is. Text streams BEFORE the tool
    # call that would mark it as narration-between-tools, so the decision cannot be made at
    # delta time; one model response's prose accumulates here and is either dropped by
    # `_discard_pending_text` (a tool call followed → it was narration) or committed by
    # `_flush_pending_text` (the response ended with no tool call → it was the turn's answer).
    # Never used in a Plan chat, where the prose IS the deliverable and streams as it arrives.
    pending_text: list[str] = field(default_factory=list)
    steps: dict[str, StepItem] = field(default_factory=dict)  # tool_call_id → newest item
    # U17's acknowledgement, held OUT of `steps` and beside it. The distinction the original
    # comment collapsed: `steps` is what gets PERSISTED, so the ack must stay out of it — but the
    # catch-up SNAPSHOT is the only way a subscriber ever learns about a frame emitted before it
    # connected, and every client connects after `start_turn` has already run. Keeping the ack out
    # of both meant it reached nobody. Cleared by the first real step, which is what "replaced by
    # the first real step" has to mean on a transport where the ring frame is unreachable.
    acknowledgement: StepItem | None = None
    subscribers: set[asyncio.Queue[None]] = field(default_factory=set)
    task: asyncio.Task[None] | None = None
    # U17 — the per-tool-call "this is still running" narrators, keyed by tool call id.
    # Cancelled the moment the call resolves (and again, synchronously, in `_finish`), then
    # AWAITED in the turn's `finally`: a narrator left running past the terminal would land a
    # step frame after the transport sent `[DONE]`.
    long_operation_tasks: dict[str, asyncio.Task[None]] = field(default_factory=dict)
    ended_monotonic: float | None = None
    # A stop has been asked for. Set BEFORE `task.cancel()`, so a second Stop landing while
    # the first is still unwinding answers "already asked" instead of firing a second cancel
    # into the cleanup path (which lands inside the CancelledError arm and can eat the
    # terminal frame the subscriber is waiting for).
    stop_requested: bool = False
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
    # The R10 liveness lease renewal (C5 family 4). Started where the container is attached,
    # released after it is handed back. `None` on a turn that never took a container — an Ask
    # or Plan turn with nothing to keep alive must not stamp a lease over whoever does hold
    # the user's slot.
    lease_task: asyncio.Task[None] | None = None
    # Why a non-completed turn ended, in the vocabulary `TurnEndedFrame.reason` publishes.
    end_reason: str | None = None
    # True once the finalize actually pushed a snapshot. TRI-STATE on the wire: None here
    # means "nothing to say" (a chat turn, or a Write turn that never reached the save).
    snapshot_committed: bool | None = None
    # The newest compile state PUBLISHED to the client, so the watcher can emit on CHANGE
    # rather than once per poll. `None` = nothing emitted yet, which is not the same as
    # `UNKNOWN` (a state we have said out loud). A turn that never learns anything sends no
    # compile frame at all, and the pane keeps whatever it was showing.
    compile_state: CompileState | None = None
    # The supervisor connect the protocol-drift alarm has already fired for. The canary is a
    # per-connect fact, so keyed on the generation the alarm is raised once per connect
    # instead of once per second for the life of a drifted container.
    compile_drift_generation: int | None = None
    # Claim-once for the preview frame, shared by the watcher and the between-verify
    # fallback: whichever sees the dev server first emits, the other stays quiet.
    preview_framed: bool = False
    # Has any turn on this app ever done real work? Resolved ONCE where the workspace is pinned
    # (one HEAD on the recovery slot) and carried, because it cannot change inside one turn and
    # the self-heal loop asks the health verdict for it up to four times. It gates U6's content
    # check: a brand-new project is SUPPOSED to be showing the starter template, and checking it
    # would manufacture an accusation rather than catch one.
    had_prior_building_turns: bool = False
    # U2 — `UNVERIFIED_TEXT` describes the state of the app, not an event, so it is said once
    # and then not again. Repeating it would train the reader to skip the one sentence most
    # likely to matter.
    said_it_could_not_check: bool = False
    # WRITE only, and the whole difference between the two zero-mutation endings. A Write
    # turn the citizen typed into may legitimately touch nothing (they asked a question);
    # a turn started from a Build-it click was ASKED to build, so touching nothing is a
    # FAILURE, not a quiet success. Nothing else about the two turns differs, so the caller
    # has to say which one this is — the engine cannot infer it from the prompt.
    expects_mutation: bool = False

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


def _what_it_is_showing(outcome: VerifyOutcome, *, ever_built: bool) -> str:
    """Which of `DID_NOT_COME_TOGETHER_TEXT`'s three arms this verdict earns (U7, R13).

    Read off the verdict rather than inferred, and the ordering is what makes each arm true rather
    than merely plausible. Not serving comes first: an app that is down has no version to describe,
    and calling it "an earlier version of itself" would send the citizen looking for a page nobody
    is serving. Then the starter template, which is a specific and actionable thing to be shown.
    The earlier-version arm is last because it is the residual — everything that is genuinely the
    user's app, just without this change in it.

    A verdict with no serving answer at all (the probe never came back) takes the same arm as one
    that is down. That is a small over-claim and the honest one available: we could not reach the
    app, and telling someone their app is fine on the strength of a check that never completed is
    the failure this whole plan exists to remove."""
    if outcome.served is None or not (200 <= outcome.served.status < 400):
        return STILL_SHOWING_NOTHING
    if outcome.baseline is BaselineIdentity.STILL_THE_BASELINE:
        return STILL_SHOWING_TEMPLATE
    if not ever_built:
        # THE FIRST BUILD, which is the likeliest way this sentence ever appears. The content
        # check is deliberately not asked of an app nobody has built yet — a brand-new project is
        # SUPPOSED to be showing the template, and asking would manufacture an accusation — so
        # `baseline` is None here and the residual arm below would tell the citizen their app is
        # "showing an earlier version of itself" while they look at the starting template. There
        # is no earlier version. This is it.
        return STILL_SHOWING_TEMPLATE
    return STILL_SHOWING_EARLIER


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
        expects_mutation: bool = False,
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
        making every read turn 503 on a dependency it does not use.

        `expects_mutation` is the Build-it caller's declaration that this turn OWES the user
        a file change. It defaults False so every conversational turn keeps its existing
        behaviour untouched; only the plan-card path opts in (see the mutation guard in
        `_run_write`)."""
        claim_conversation(conversation.id)
        try:
            await persist_user_turn()
            state = _TurnState(
                turn_id=uuid.uuid7(),
                conversation_id=conversation.id,
                user_id=user_id,
                kind=conversation.kind,
                expects_mutation=expects_mutation,
            )
            self._by_conversation[conversation.id] = state
            # U17/R24 — ANSWER THE SCREEN BEFORE ANYTHING CAN BE SLOW.
            #
            # HERE, and not one line later, is the whole point: this runs before
            # `asyncio.create_task`, so there is no ordering to get wrong and no window in which
            # a cold provision, a snapshot restore, or a first model request can leave the
            # citizen looking at a still screen. It is the first frame of every turn (`seq == 1`)
            # and it is never persisted — `state.steps` is untouched, so neither the catch-up
            # snapshot nor `append_batch` ever sees it.
            ack = StepItem(
                seq=0,  # transient: no row, so no row seq
                tool=ACK_TOOL,
                label=ACK_TEXT,
                state="pending",
                hidden=False,
            )
            state.acknowledgement = ack
            self._emit(
                state,
                lambda seq: StepFrame(
                    seq=seq, tool_call_id=ACK_TOOL_CALL_ID, phase="started", item=ack
                ),
            )
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

    async def stop_user_turn_and_wait(self, user_id: uuid.UUID, *, timeout_s: float) -> bool:
        """Stop whatever turn this user is running, and WAIT for it to actually unwind.

        The "stop and switch" flow needs this and `stop_turn` cannot provide it, for
        two reasons. It is keyed on a conversation the caller does not have — the refusal names
        a PROJECT, and a Write turn's manager session carries no conversation id — and it
        returns the instant `task.cancel()` is issued, which is the one thing a caller about to
        tear the container down must not act on.

        THE WAIT IS THE POINT. Cancellation is a request, not an event: the turn still has to
        unwind its `finally`, emit its terminal frame, bill the tokens already spent and run
        `finish_turn_sandbox` — and only that last step pops the user out of the manager's
        `_active_by_user`, which is what makes the workspace releasable. Releasing before it
        lands tears a container out from under a running task, the exact strand the build
        session module exists to prevent.

        At most one turn can be running per user (`_claim_the_one_build_slot` refuses a second
        allocation), so the scan below finds one or none. Returns True when a running turn was
        found and awaited. `timeout_s` bounds the wait rather than hanging the request: the
        caller must treat a timeout as "still running", never as "safe to proceed"."""
        state = next(
            (
                s
                for s in self._by_conversation.values()
                if s.user_id == user_id and s.status == "running" and s.task is not None
            ),
            None,
        )
        if state is None or state.task is None:
            return False
        if not state.stop_requested:
            state.stop_requested = True
            state.task.cancel()
        # `shield` so THIS request being cancelled (the user closed the tab) cannot cancel the
        # turn's own unwind a second time — a double cancel lands inside the cleanup path and
        # can eat the terminal frame a subscriber is still waiting for, which `stop_requested`
        # exists to prevent. `suppress` because the task ending in CancelledError is the
        # SUCCESS case here: we asked for it.
        with suppress(TimeoutError, asyncio.CancelledError, Exception):
            await asyncio.wait_for(asyncio.shield(state.task), timeout=timeout_s)
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
            # U8 / R14 — THE WORKSPACE NOTE, UNCONDITIONALLY, on every turn that pinned a
            # sandbox: an ephemeral tail on `message_history`, structurally excluded from the
            # persisted rows because `new_messages()` never contains injected history.
            #
            # It is the ONLY thing injected here now. The per-turn restatement that used to
            # ride beside it — "you are in Plan mode", on a cadence — went with the modes it
            # restated: a chat's kind is fixed at creation, and the toolset is what carries
            # which chat this is. This note stayed because it is a different claim: it tells
            # the model a FACT about the app that its history cannot know, and it holds on
            # every turn rather than one in four.
            if workspace is not None:
                note = await self._workspace_note(state)
                history = [*history, ModelRequest(parts=[UserPromptPart(content=note)])]
            if state.kind is ChatKind.BUILD:
                # THE SECOND AND LAST READER OF THE KIND, and a different question from the
                # toolset registry's: this one selects a HARNESS SHAPE — the node loop with
                # its per-step billing fold versus a single `chat_agent.run`, and with it the
                # `output_type` below. Unifying the two loops would mean giving a Plan run the
                # streaming node loop and the per-step billing it has no steps for.
                #
                # A Build turn bills PER MODEL STEP, inside the loop — `record_usage` is
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
                        kind=state.kind,
                        prompt_context=prompt_context,
                        workspace=workspace,
                    )
                    toolsets = toolsets_for_kind(state.kind, _workspace_of).toolsets
                    # A Plan chat may DEFER on present_plan_options — the run then ends with a
                    # DeferredToolRequests output instead of text (the pending card state).
                    output_type: Any = (
                        [str, DeferredToolRequests] if state.kind is ChatKind.PLAN else str
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
                        # A CEILING, NOT A TUNING KNOB. This run passed no model settings at
                        # all and inherited the provider default of 4096 output tokens. A plan
                        # is written for a person to read and can run long — and truncation
                        # here does not degrade, it wipes: the argument carrying the plan is
                        # cut mid-string, the offer is refused, and the citizen pays for a turn
                        # that produced nothing they can press. The same two settings the build
                        # loop already passes, for the same reason.
                        model_settings=AnthropicModelSettings(
                            max_tokens=MAX_OUTPUT_TOKENS, temperature=TEMPERATURE
                        ),
                    )
                    persistable = _persistable_messages(result.new_messages())
                    deferred = _deferred_call(result.output)

                    # AN OFFER IS EITHER HONOURABLE OR IT IS NOT WRITTEN AT ALL (R28a / R44).
                    # An empty plan, one past the stored-message ceiling, or a pre-migration
                    # call with no argument leaves nothing to press — so the call comes off
                    # what is persisted, no pending record is written, and the turn says so in
                    # one platform-authored line. Nothing unbuildable is left on screen, and
                    # the live feed already agreed: the event handler pushed no plan and
                    # emitted no card for the same call.
                    if deferred is not None and plan_from_call(deferred) is None:
                        _log.info(
                            "plan_options_offer_refused",
                            conversation_id=str(state.conversation_id),
                            turn_id=str(state.turn_id),
                        )
                        persistable = _without_the_call(persistable, deferred.tool_call_id)
                        deferred = None
                        self._push_plan(state, PLAN_NOT_KEPT_TEXT)
                        persistable = [
                            *persistable,
                            ModelResponse(parts=[TextPart(content=PLAN_NOT_KEPT_TEXT)]),
                        ]

                    batches: list[tuple[list[ModelMessage], dict[str, Any] | None]] = [
                        (persistable, self._pending_meta(deferred))
                    ]

                    # NO SECOND MODEL REQUEST IS ISSUED HERE, and none is issued anywhere as a
                    # consequence of what the model wrote. The forced retry that used to sit at
                    # this point re-ran the turn with the offer tool as the only thing the model
                    # could reach, on the strength of a prose heuristic — see the note where
                    # that heuristic used to be defined.

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
                                    kind=state.kind,
                                    meta=meta,
                                )
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
            # Name the stop before finishing: `_finish` reads `end_reason` onto the terminal
            # frame, and "stopped" alone does not tell a client whether the citizen pressed the
            # button or something upstream cancelled the request.
            state.end_reason = STOPPED_BY_USER
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
            # THE DURABLE TERMINAL, FIRST IN THE FINALLY. `_finish` emits the live
            # `TurnEndedFrame` and cannot write it — it is synchronous by design, so that a
            # terminal frame reaches every subscriber with no await in between for a second
            # cancellation to slip through. So the row is written here, which is the same
            # boundary: `finally` runs exactly once per turn, on every arm, after whichever
            # `_finish` above set the status. Writing it from the arms instead would mean five
            # call sites and a turn that could leave two rows.
            await self._write_turn_terminal(state, session_factory)
            # The watcher dies FIRST, on every terminal arm and for EVERY mode. The Write
            # loop already stops its own on the way out, but Ask and Plan attach the same
            # live container — `_attach_sandbox` starts the watcher for whoever attaches —
            # and had no loop of their own to stop it, so every read-mode turn leaked a
            # polling task that could land a preview frame after the transport sent [DONE].
            # Idempotent by construction: the Write path's stop leaves `preview_task` None
            # and this backstop finds nothing to do.
            await self._stop_preview_watcher(state)
            # U17 — and the status-line narrators with it, for the same reason and on every
            # arm. `_finish` already cancelled them; this is where they are actually awaited,
            # so none is still unwinding when the transport closes.
            await self._drain_long_operations(state)
            # THE RELEASE, on every single terminal arm — completed, stopped, persist-failed,
            # named-end, or a genuine bug. NO SAVE happens here (KTD-5e): the bundle reaches
            # Blob only on the user's Save click (`save_project_snapshot`). What
            # `finish_turn_sandbox` does is free the build slot and pardon the container, so
            # the preview stays up and the next message can start — anything that reaches
            # this `finally` without it leaves the conversation wedged shut.
            #
            # `asyncio.shield` is not belt-and-braces: the STOPPED path arrives here because
            # the task was cancelled, and an unshielded await would be cancelled again the
            # instant it yielded — leaving the slot held and the container to the reaper.
            # Errors are suppressed for the end-sequence reason: a Redis blip must not stop
            # the guard from being released and wedge the conversation shut forever.
            # THE RELEASE IS ITS OWN `finally`, and that nesting is load-bearing rather than
            # tidiness. `suppress(Exception)` does not catch `CancelledError` — it is a
            # `BaseException` — so a SECOND cancellation delivered while the shielded call is
            # suspended propagates straight out of this block. Flat, that skipped
            # `release_conversation` entirely and the guard never expires on its own
            # (`guard.py`), so every later turn in that conversation answered 409 for the rest
            # of the process's life. It was unreachable while the shielded body was two Redis
            # round trips; it stops being unreachable the moment that body does real work.
            try:
                if state.write_session is not None and sandbox_client is not None:
                    with suppress(Exception):
                        await asyncio.shield(
                            manager.finish_turn_sandbox(
                                state.write_session,
                                sandbox_client,
                                # Only a turn that MUTATED the tree is worth bundling. An Ask or
                                # Plan turn holds no tool that could set this, so it releases the
                                # sandbox without paying for an upload of a tree it only read.
                                touched=(
                                    state.sandbox is not None and state.sandbox.workspace_touched
                                ),
                            )
                        )
            finally:
                # AFTER the sandbox work, not before: releasing early would let the next turn in
                # this conversation start before `finish_turn_sandbox` frees the one-per-user
                # build slot, turning a clean wait on the guard into a 409 on the slot.
                #
                # FIRST in this block, though — ahead of the lease release below — and that
                # ordering is the same P0 lesson as the shield above. `_stop_liveness_lease`
                # awaits, so on the stopped path a second cancellation can propagate out of
                # it; if the guard release sat after it, that would skip `release_conversation`
                # and every later turn in the conversation would answer 409 for the rest of
                # the process's life. Nothing depends on the lease outliving the guard: the
                # next turn's reconcile-on-start certifies death and deletes it anyway.
                release_conversation(state.conversation_id)
                # The R10 lease goes LAST, once the container has actually been handed back
                # (U12). Releasing it before `finish_turn_sandbox` would leave that snapshot
                # -and-pardon sequence — which can easily outlive the 90-second heartbeat TTL
                # — exposed to a concurrent sweep with nothing at all vouching for it.
                await self._stop_liveness_lease(state)

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

        NOTHING PINS A VERSION HERE ANY MORE. A Plan turn used to stamp the snapshot's head
        onto its options card so Build-it could warn that the app had moved underneath the
        plan, and the writer sat inside a branch on the chat's kind — the last of those. What
        the pin bought is paid for better and for more cases: the instruction to follow the
        code's reality where it differs from what the plan assumed lives in the Build chat's
        own prompt, which works for a plan built weeks later rather than only when two snapshot
        heads happen to differ."""
        attach = partial(
            self._attach_sandbox,
            state,
            project_id=project_id,
            session_factory=session_factory,
            manager=manager,
            sandbox_client=sandbox_client,
        )
        # ONE ARM. There is no branch here at all any more — every turn, in both kinds,
        # resolves the project's LIVE container and nothing else (R18).
        #
        # What used to sit above this was the last way a chat could answer from a saved copy:
        # `sandbox_client is None and this is not a Build chat` fell through to extracting the
        # newest snapshot bundle. That condition was never about the chat — `sandbox_client is
        # None` is a deployment fact wearing a branch on the kind — and the behaviour it bought
        # was a silent downgrade: the citizen asked about their app and got an answer about a
        # copy of it, with nothing on screen to say which. The same fact is now asked one layer
        # up, where a person can be told about it (R98, `api/v1/conversations/turns.py`), so a
        # send that cannot reach a workspace is refused before it is spent.
        return LiveSandboxWorkspace(session=await attach())

    # -- the WRITE run -------------------------------------------------------------------

    async def _say_what_the_workspace_did(self, state: _TurnState, news: RecoveryNews) -> None:
        """Turn the integrity gate's finding into the one sentence the citizen reads (U2).

        THE MANAGER KNOWS WHAT HAPPENED; THIS KNOWS HOW TO SAY IT. The gate cannot import these
        strings — `services.turns` reaches `build_sessions`, so an import back would close the
        cycle — and it should not want to: which words a citizen sees is a product decision that
        belongs beside the rest of them.

        `UNVERIFIED` IS SAID ONCE PER TURN AND THEN NOT AGAIN. It describes the state of the app
        rather than an event, so repeating it would train the reader to skip the one sentence
        most likely to matter."""
        if news is RecoveryNews.UNVERIFIED:
            if state.said_it_could_not_check:
                return
            state.said_it_could_not_check = True
        message = {
            RecoveryNews.RESTORING: RECOVERED_TEXT,
            RecoveryNews.UNRECOVERABLE: NOT_RECOVERED_TEXT,
            RecoveryNews.UNVERIFIED: UNVERIFIED_TEXT,
        }[news]
        # A NOTICE, NOT A MESSAGE. `message` narrates the phase and is replaced by the next
        # one; this is a statement about the app that has to survive the phase passing.
        self._emit(state, lambda seq: WorkspaceFrame(seq=seq, state="preparing", notice=message))

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
                    db,
                    user,
                    project_id,
                    sandbox_client=sandbox_client,
                    # TAKEN FROM THE TOOL SURFACE ITSELF, not re-derived from the enum.
                    # `toolsets_for_kind` gives a Plan run a read-only toolset and only a Build
                    # run the `sandbox_toolset` that can mutate files, and it returns
                    # `may_write` alongside them — so this is literally the same answer the
                    # model's abilities give, rather than a second reading that convention
                    # keeps in step. Downstream guards cannot recover it (both kinds pin the
                    # container identically), and reading it as "always writing" once made a
                    # read-only question refuse the Save button and claim the app was building.
                    may_write=toolsets_for_kind(state.kind, _workspace_of, _sandbox_of).may_write,
                    # U2 — THE SENTENCE HAS TO ARRIVE BEFORE THE SLOW WORK, not after it. The
                    # recovery path adds tens of seconds of otherwise-silent latency, and the
                    # gate calls this the moment it knows, from inside the attach.
                    announce=partial(self._say_what_the_workspace_did, state),
                )
        except WorkspaceUnreadableError as exc:
            # RETRYABLE, and deliberately not a verdict about the app. The container is still
            # running and still attached, so the retry has something to attach to.
            _log.warning(
                "workspace_integrity_unreadable",
                conversation_id=str(state.conversation_id),
                app_id=str(exc.app_id),
            )
            state.workspace_state = "unavailable"
            self._emit(
                state,
                lambda seq: WorkspaceFrame(
                    seq=seq, state="unavailable", notice=COULD_NOT_CHECK_TEXT
                ),
            )
            raise _WriteEndedError("workspace_unreadable", COULD_NOT_CHECK_TEXT) from exc
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

        if session.news is RecoveryNews.UNRECOVERABLE:
            # AE3. Nothing was put back, and the container is showing a template. The one thing
            # that must not happen is the agent building on it and the turn-end copy making that
            # permanent, so the turn ends here.
            raise _WriteEndedError("workspace_unrecoverable", NOT_RECOVERED_TEXT)
        if session.restored:
            # THE HELD MESSAGE (R5). The instruction was written against a workspace that no
            # longer exists; running it now would execute an instruction whose premise was true
            # when it was typed and false when it ran. The citizen re-sends when they have looked
            # at what came back.
            raise _WriteEndedError("workspace_restored", RECOVERED_TEXT)

        state.write_session = session
        state.sandbox = SandboxSession(
            sandbox_client=sandbox_client,
            handle=session.handle,
            app_id=session.app_id,
            # No emitter: the turn engine renders the run's own tool events as step frames,
            # and a second feed would draw every step twice.
            emitter=None,
        )
        # U13 — FENCE OFF ANY BROWSER CRASH REPORT THAT PREDATES THIS TURN. A report describes
        # the tree the browser was rendering when it crashed, and this turn is about to change
        # that tree; draining it at the end would fail a verify on a fault the agent may have
        # just fixed. The gap between turns is not even a quiet one: the pane reloads its frame
        # at every terminal, so it actively manufactures reports about the OLD tree. Discarded
        # once, here, where "the agent has not started yet" is still true.
        discarded = discard_client_errors(session.handle.app_name)
        if discarded:
            _log.info(
                "client_error_reports_fenced",
                conversation_id=str(state.conversation_id),
                app_name=session.handle.app_name,
                discarded=discarded,
            )
        # U6 — HAS THIS APP EVER BEEN BUILT? One HEAD on the recovery slot, resolved here because
        # this is where the turn already holds the app id and because the answer cannot change
        # while the turn runs. It gates the content half of the health verdict: a brand-new
        # project is legitimately showing the starter template, and the whole point of the check
        # is to catch an app that is showing it AFTER someone asked for something else.
        state.had_prior_building_turns = await has_ever_been_built(session.app_id)
        state.workspace_state = "ready"
        self._emit(state, lambda seq: WorkspaceFrame(seq=seq, state="ready"))
        # BOOT THE DEV SERVER THE MOMENT WE HOLD THE CONTAINER, not after the whole model run
        # plus a `tsc`. Next's first route compile is 5-7s, and until now the only thing that
        # ever called `dev_start` on this path was `selfheal.verify` — so the compile ran
        # strictly AFTER the agent had finished, instead of alongside its first request. Worse
        # for a turn the model only READS in: the mutation guard returns before verify, so the
        # server was never started at all and no preview ever appeared. The legacy harness has
        # always done this at attach (`harness.py:201`); this brings unified chat to parity.
        #
        # Best-effort BY DESIGN. This is an optimization, never a gate: `verify`'s dead-child
        # rescue is the backstop, so a supervisor blip costs the preview a few seconds and not
        # the turn. And deliberately no `wait_ready` — `_watch_preview` polls at 1s and owns
        # framing; blocking the turn's start on readiness would trade one latency problem for
        # another one the user can see.
        try:
            await sandbox_client.dev_start(session.handle)
        except SandboxError:
            _log.warning(
                "write_dev_start_at_attach_failed",
                conversation_id=str(state.conversation_id),
                turn_id=str(state.turn_id),
                app=session.handle.app_name,
                exc_info=True,
            )
        state.preview_task = asyncio.create_task(self._watch_preview(state))
        # AND THE LIVENESS LEASE, from the moment this turn owns the container (R10/U12).
        # Same lifecycle as the watcher above — a background task the turn owns, stopped in
        # its `finally`, idempotent — because the reasoning is the same: it exists only for
        # as long as there is a container to say something about.
        state.lease_task = asyncio.create_task(self._hold_liveness_lease(state))
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
        compile. Only the conjunction means finished.

        U18/R30 CHANGES WHAT HAPPENS ON THE PASSING SIDE OF THAT GATE, AND NOTHING ELSE ABOUT
        IT. The conjunction is untouched — a failing verdict after `declare_done` still sends
        the turn into repair exactly as before. What is gone is the round-trip the passing side
        used to buy: the model called the tool, was told to stand by, and was then asked for one
        more full request whose entire product was a closing paragraph. That paragraph is the
        message the 2026-08-18 build wrote in 2,397 words of file paths and framework names.
        The summary the tool already carries says the same thing in the register the reader
        actually has, so the harness renders THAT and ends the turn on it."""
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
                        "Your changes are still in the workspace — click Save to keep them, "
                        "then send a message to pick it back up.",
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
                # Per-iteration; the flag means "this run". THE SUMMARY IS RESET WITH IT (U18),
                # because the two are one fact: a summary written before a verdict that came
                # back red describes a build that then failed, and leaving it standing would let
                # a later `declare_done` with an empty summary end the turn on stale praise for
                # work that had to be repaired.
                sandbox.done_requested = False
                sandbox.done_summary = ""
                # U9 / R15 — MARK "NOW" IN THE CONTAINER BEFORE THE AGENT RUNS. Everything the
                # dev server prints after this point is about a tree the agent is currently
                # changing; everything before it may be about one it has already fixed. The
                # health verdict asks the difference before it buys a repair round-trip.
                #
                # Best-effort and deliberately unchecked: a failed stamp makes the follow-up
                # question unanswerable, which the verdict reads as "change nothing" — today's
                # behaviour, and never a reason to fail a turn.
                await stamp_the_watermark(sandbox.sandbox_client, sandbox.handle)
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
                        "Your changes are still in the workspace — click Save to keep them, "
                        "then try asking for a smaller change.",
                    ) from exc
                iteration += 1

                # THE MUTATION GUARD. A Write turn where the model only read files and
                # answered a question is an ordinary chat turn that happened to have write
                # tools available. Verifying it would spend 30s of the user's time and a
                # `tsc` run to confirm nothing changed, then nudge the model to keep going.
                #
                # …UNLESS the turn was asked to build. The same zero-mutation outcome means
                # opposite things on the two paths, and the bare `return` gave BOTH of them
                # the caller's `_finish(state, "completed")`: a real build once spent 65k
                # tokens, wrote not one file, and told the citizen "Build complete — your app
                # is live below" over a container still serving the golden template. A build
                # that produced nothing is a failure and has to end as one.
                #
                # …and on the build path the guard asks a NARROWER question, because
                # `done_requested` is not evidence of a mutation — it is the model's own claim to
                # have finished, and `declare_done` used to set `workspace_touched` alongside it.
                # A model that wrote nothing and simply declared itself done therefore satisfied
                # both halves of this disjunction and walked straight back into "Build complete —
                # your app is live below" over an untouched template. That is the same lie the
                # guard was added to stop, reached by asking the accused for a character
                # reference. On a turn that EXPECTS a mutation, only a real write counts.
                mutated = sandbox.workspace_touched
                if not (mutated or sandbox.done_requested):
                    if state.expects_mutation:
                        raise _WriteEndedError(
                            "build_wrote_nothing",
                            "Nothing was built — the assistant finished this run without "
                            "creating or changing a single file, so your app is unchanged. "
                            "Send a message describing what you want built and it will "
                            "try again.",
                        )
                    return
                if state.expects_mutation and not mutated:
                    raise _WriteEndedError(
                        "build_wrote_nothing",
                        "Nothing was built — the assistant reported the build as finished "
                        "without creating or changing a single file, so your app is unchanged. "
                        "Send a message describing what you want built and it will "
                        "try again.",
                    )

                self._emit_verify_step(state, iteration, phase="started")
                outcome, log_cursor = await verify(
                    sandbox.sandbox_client,
                    sandbox.handle,
                    log_cursor=log_cursor,
                    max_polls=READINESS_MAX_POLLS,
                    poll_s=READINESS_POLL_S,
                    app_id=sandbox.app_id,
                    # Resolved ONCE at attach and carried on the turn state — the content half of
                    # the verdict is only meaningful for an app that has been built before, and
                    # re-asking the store on every self-heal pass would be three HEAD requests to
                    # learn a fact that cannot change inside one turn.
                    had_prior_building_turns=state.had_prior_building_turns,
                )
                self._emit_verify_step(state, iteration, phase="finished", verdict=outcome.state)

                if outcome.dev_ready and state.claim_preview_frame():
                    await self._emit_preview_ready(
                        state, outcome.preview_url or sandbox.handle.preview_url
                    )

                if outcome.green and sandbox.done_requested:
                    state.snapshot_committed = None  # the finalize answers this, not us
                    await self._render_completion(state, sandbox, session_factory)
                    return

                # UNANSWERABLE IS NOT A DEFECT, and this is the line where that stops
                # being true if the condition is written as `not outcome.green`. `verify`
                # returns INDETERMINATE with no error BY CONSTRUCTION, so a green-shaped test
                # here synthesizes `dev_not_ready_error()` for it — whose prose says the dev
                # server never reported ready, about an app that reported ready. The citizen
                # then reads a fabricated defect, the model is re-seeded to repair a fault that
                # does not exist, and a repair run is charged for it. That is precisely the
                # misdiagnosis the third state was added to remove, reappearing one arm
                # downstream of where it was fixed.
                #
                # An unanswerable verdict ends the turn instead, and only when the model has
                # claimed to be finished: it gates the COMPLETION CLAIM, so with no claim
                # outstanding there is nothing for it to gate and the loop carries on as
                # before. Nothing here spends a repair attempt on it.
                if not outcome.green and outcome.state is not HealthState.INDETERMINATE:
                    # U25/R32 — THE HEADLINE NUMBER: how often the platform would have told a
                    # citizen their app was finished when it was not. Counted only on a POSITIVE
                    # verdict of "not finished" — an unanswerable one blocked nothing, it merely
                    # asked again, and folding the two together would make the number that
                    # measures this plan's whole point unreadable.
                    #
                    # Fire-and-forget by construction (`count` owns its own session and swallows
                    # everything), because a counter that can fail the turn it is counting is
                    # worse than no counter.
                    await count(
                        HarnessCounter.CLAIM_BLOCKED,
                        app_id=state.write_session.app_id if state.write_session else None,
                        served_head=outcome.served.head if outcome.served else None,
                    )
                if outcome.state is HealthState.INDETERMINATE:
                    # BOUNDED HERE, because this arm `continue`s past the budget guard below and
                    # an unanswerable verdict that repeats would otherwise spin against the wall
                    # clock alone. The budget is checked before it is spent, so the last
                    # iteration ends the turn rather than buying a run it cannot pay for.
                    if sandbox.done_requested or budget <= 0:
                        raise _WriteEndedError("verdict_unanswerable", COULD_NOT_CONFIRM_TEXT)
                    turn_prompt = CONTINUE_PROMPT
                    budget -= 1
                    continue
                # `error is None` does NOT imply green: a clean `tsc` with a dev server that
                # never came up is red with nothing to report. Synthesize the server error
                # or a budget-exhausted turn ends with no diagnostic at all.
                error = outcome.error
                if error is None and outcome.state is HealthState.UNHEALTHY:
                    error = dev_not_ready_error()
                if budget <= 0:
                    # Exhausted is not one state but two, and only one of them is a defect.
                    # `error is None` here means every check came back green (the red case
                    # always synthesizes an error above) and the model simply never called
                    # `declare_done` — telling THAT user their app "still has an error"
                    # sends them hunting for a defect that does not exist. Neither arm may
                    # claim the work is "saved": there is no auto-save (KTD-5e) — the
                    # changes sit in the workspace until the user's Save click.
                    if error is None:
                        raise _WriteEndedError(
                            "self_heal_budget_exhausted",
                            "Your app checks out — the assistant just ran out of steps "
                            "before wrapping up. Your changes are still in the workspace — "
                            "click Save to keep them, or send a message to continue.",
                        )
                    # U7 / R13 — THE HONEST ENDING. The sentence it replaces named a defect
                    # ("your app still has an error") and left the citizen to work out what they
                    # were looking at; this one says what the app is currently showing, from the
                    # verdict rather than from a guess, because that is what decides what they
                    # should do next. The holding state on the preview stops with it.
                    raise _WriteEndedError(
                        "self_heal_budget_exhausted",
                        DID_NOT_COME_TOGETHER_TEXT.format(
                            showing=_what_it_is_showing(
                                outcome, ever_built=state.had_prior_building_turns
                            )
                        ),
                    )
                if error is not None:
                    # U13 / R17 — A CLIENT-CLASS REPORT IS AGENT INPUT, NOT NARRATIVE. The whole
                    # user-visible consequence of a browser-side crash is that the completion
                    # claim does not appear; the report itself was written by code inside the
                    # generated app, and this plan removes developer surfaces rather than adding
                    # one. It still repairs — `build_repair_prompt` below is reached exactly as
                    # for any other source — it just does not narrate.
                    #
                    # THE TRAP, and it is why this guard is here and not in `verify`: making
                    # `verify` return `green=False, error=None` for this class would look like
                    # the tidier fix and is strictly worse. Ten lines up, a red outcome with no
                    # error synthesizes `dev_not_ready_error()` — so the user would get a SERVER
                    # diagnostic that is both rendered AND wrong, and the model would be handed
                    # the same misdiagnosis to chase. The verdict has to carry the real error;
                    # only the RENDER is skipped.
                    #
                    # A later plan brings this class into a split-audience rendering with copy of
                    # its own. Until then, silence is the honest surface.
                    if error.source is not ErrorSource.CLIENT:
                        # `error.title` and `error.cleaned_stack` are deliberately NOT read
                        # here. They are the model's half and they stay server-side, on the
                        # `BuildError` the repair prompt below is built from; the frame carries
                        # the class and the citizen's sentence, and nothing that came out of a
                        # compiler.
                        source = error.source
                        self._emit(state, lambda seq: DiagnosticFrame(seq=seq, source=source))
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
            # …and only then settle a compile state left mid-build, for the same reason in
            # reverse: after the watcher is down, nothing else will ever report on this app.
            await self._settle_compile_state(state)

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
            kind=ChatKind.BUILD,
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
            toolsets=toolsets_for_kind(ChatKind.BUILD, _workspace_of, _sandbox_of).toolsets,
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
            cut_short = False
            pending_answers: ModelRequest | None = None
            while not Agent.is_end_node(node):
                if Agent.is_model_request_node(node):
                    # THE SESSION CLOSES BEFORE THE ENDING IS BUILT, which is why the `try`
                    # is on the outside now (U24). `at_limit_ending` bundles and uploads the
                    # citizen's tree, and doing that inside the `async with` would pin a
                    # pooled connection for the duration of a container round trip — on the
                    # one path where every user who hits their cap in the same hour arrives
                    # at once. Nothing else about this block moved.
                    try:
                        async with session_factory() as gate_db:
                            await enforce_daily_limit(gate_db, state.user_id)
                    except DailyTokenLimitExceededError as exc:
                        # The request never fires. Graceful, not a crash: the work so
                        # far is real, and U24 makes it DURABLE here rather than leaving it
                        # to whether the exit path's best-effort autosave happens to succeed.
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
                            (await at_limit_ending(state.sandbox)).message,
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
                    # THE FLUSH BOUNDARY, and it has to be here rather than at the end of the
                    # model-request stream above (U15/R20). pydantic-ai streams a response's
                    # TEXT and its TOOL CALLS from two different nodes, text first, so at the
                    # end of the text stream we do not yet know which kind of prose this was.
                    # By this line the tool-call node has been drained: a response that called
                    # tools has already emptied the buffer via `_discard_pending_text`, so this
                    # is a no-op for it, and a response that called none still holds its prose —
                    # which is the citizen's answer, and the only thing that will ever say it.
                    self._flush_pending_text(state)
                    node = await run.next(node)
                    # The step's tools have executed and their returns are in the history,
                    # so the step is complete — persist before the next request fires.
                    persisted_from = await self._persist_write_step(
                        state,
                        history=run.all_messages(),
                        persisted_from=persisted_from,
                        session_factory=session_factory,
                    )
                    # U18/R30 — AND THIS IS WHERE `declare_done` STOPS BUYING A ROUND-TRIP.
                    # `node` is already the NEXT model request; walking into it spends a full
                    # request whose only product is a closing paragraph the harness has just
                    # stopped rendering. Cut here instead — the verdict still decides whether
                    # the turn is over (`_run_write`'s conjunction is untouched), and a red one
                    # re-enters this run with the repair prompt exactly as before.
                    #
                    # THE PENDING REQUEST IS TAKEN OFF THE NODE ON THE WAY OUT, and it has to
                    # be. A `ModelRequestNode` carries the tool ANSWERS and only appends them to
                    # the history when it runs — which is the thing we are declining to do — so
                    # `run.all_messages()` here ends on a `ModelResponse` whose tool calls look
                    # unanswered. Left that way, the repair pass hands pydantic-ai a new user
                    # prompt over unprocessed tool calls (it refuses outright), and the
                    # `declare_done` return never reaches a row.
                    if state.sandbox is not None and state.sandbox.done_requested:
                        if Agent.is_model_request_node(node):
                            pending_answers = node.request
                        cut_short = True
                        break
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
            elif cut_short:
                # `run.result` is set by the END node, which a cut-short run never reaches — so
                # the accumulated history has to be read off the run itself, with the tool
                # answers the node above was holding put back on the end. Without this the
                # caller would keep the PRE-RUN history and a repair pass would re-ask the model
                # to build from scratch, having thrown away everything it just wrote.
                messages = list(run.all_messages())
                if pending_answers is not None:
                    messages.append(pending_answers)
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
                    kind=ChatKind.BUILD,
                    meta={"kind": "write_step", "turnId": str(state.turn_id)},
                )
                await db.commit()
        except Exception as exc:
            raise _PersistFailedError from exc
        return len(history)

    async def _render_completion(
        self,
        state: _TurnState,
        sandbox: SandboxSession,
        session_factory: SessionFactory,
    ) -> None:
        """THE COMPLETION MESSAGE, WRITTEN FROM `done_summary` (U18/R22).

        THE FIELD WAS ALWAYS WRITTEN AND NEVER READ. `declare_done` has stored its `summary`
        since the tool existed and three model-facing prompts have asked for it; the reader is
        what was missing, so what the citizen actually read at the end of a build was whatever
        free-form paragraph the model produced on one more round-trip. On 2026-08-18 that
        paragraph was file paths and framework names. Rendering the field instead is the whole
        of this change: same fact, a bounded field the prompt shapes, and no request bought to
        obtain it.

        BOTH FRAMES AND THE ROW, because the completion has to survive a reload. `_push_text`
        puts it on the live stream and into `text_so_far` for a mid-turn re-snapshot; the row
        is what the transcript projects tomorrow. Persisting it also closes the exchange
        honestly — cutting the run at the tool leaves a tool return with no response after it,
        and this is that response.

        THE PERSIST MAY FAIL THE TURN, deliberately, and it is the same rule the rest of Write
        already keeps: a reply that was not stored has not been given. Silently swallowing it
        would end a build with a completion on screen that vanishes on the next reload."""
        text = sandbox.done_summary.strip() or _BUILD_FINISHED_FALLBACK
        # An earlier iteration of this same turn can have flushed prose of its own (a response
        # that called no tool — see `_flush_pending_text`), and `text_parts` is joined with
        # nothing between entries. Without this the closing message runs into that prose's last
        # sentence, which is the defect `TEXT_BLOCK_SEPARATOR` exists to prevent.
        if state.text_parts:
            self._push_text(state, TEXT_BLOCK_SEPARATOR)
        self._push_text(state, text)
        try:
            async with session_factory() as db:
                await append_batch(
                    db,
                    user_id=state.user_id,
                    conversation_id=state.conversation_id,
                    messages=[ModelResponse(parts=[TextPart(content=text)])],
                    entry_kind=MessageEntryKind.STEP,
                    kind=ChatKind.BUILD,
                    meta={"kind": "write_completion", "turnId": str(state.turn_id)},
                )
                await db.commit()
        except Exception as exc:
            raise _PersistFailedError from exc

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
                    kind=ChatKind.BUILD,
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

    async def _workspace_note(self, state: _TurnState) -> str:
        """What this app's workspace is doing RIGHT NOW, as a private note for the model (U8/R14).

        THE CHEAP HALF OF THE HEALTH VERDICT, and cheap is a requirement rather than a preference:
        this runs on every turn in every mode, including a one-line Ask question, so it must not
        cost what `verify` costs. A bounded readiness poll plus one exec — no `tsc`, no full
        readiness budget, and no serving GET that would block on a cold first-route compile.

        "STILL STARTING UP" IS REPORTED AS "COULD NOT TELL", NOT AS "DOWN". `dev_start` fires at
        attach, and Next's first route compile is measured at 5-7s, so a note composed the instant
        after would call almost every cold turn's app dead. `Readiness.STILL_TRYING` is exactly
        that state and it maps to the honest answer.

        The baseline check runs for a brand-new project too, unlike the health verdict's, and the
        difference is what each one is FOR: the verdict decides whether to block a completion
        claim, where accusing an unbuilt app of showing the template would be a false positive;
        the note tells the model what the user is looking at, where "there is no app on the home
        page yet" is true, useful, and exactly what Ask mode's own segment asks it to say.

        NEVER RAISES. A note that could fail would take the turn down with it, and every failure
        already has a value: not knowing."""
        sandbox = state.sandbox
        if sandbox is None:
            return workspace_note(serving=None, still_the_template=None)
        serving: bool | None
        try:
            readiness = await where_are_we(
                sandbox.sandbox_client,
                sandbox.handle,
                max_polls=WORKSPACE_NOTE_MAX_POLLS,
                poll_s=READINESS_POLL_S,
            )
        except SandboxError:
            serving = None
        else:
            serving = {
                Readiness.READY: True,
                Readiness.DIED: False,
                Readiness.STILL_TRYING: None,
            }[readiness]
        still_the_template: bool | None = None
        if serving:
            baseline = await baseline_identity(sandbox.sandbox_client, sandbox.handle)
            if baseline is not BaselineIdentity.UNANSWERABLE:
                still_the_template = baseline is BaselineIdentity.STILL_THE_BASELINE
        return workspace_note(serving=serving, still_the_template=still_the_template)

    def _emit_verify_step(
        self,
        state: _TurnState,
        iteration: int,
        *,
        phase: Literal["started", "finished"],
        verdict: HealthState | None = None,
    ) -> None:
        """The verify spinner. Synthetic and never persisted — it is a progress affordance
        for a 30s wait, not part of the record, and it is correct for it to vanish on
        reload.

        THREE FINISHED ARMS, not two, and the third is why this takes the verdict rather than a
        bool (U6). "Not working yet" over a check that could not be REACHED tells the citizen their
        app is broken on the strength of our own timeout — the platform blaming the app for its
        own silence, which is the same shape of untruth as claiming a build finished when it did
        not. An unreachable verdict resolves the spinner neutrally and says so."""
        started = phase == "started"
        label: str
        step_state: Literal["ok", "failed", "pending"]
        if started:
            label, step_state = "Checking your app…", "pending"
        elif verdict is HealthState.INDETERMINATE:
            label, step_state = "Still checking…", "ok"
        elif verdict is HealthState.HEALTHY:
            label, step_state = "Build verified.", "ok"
        else:
            label, step_state = "Not working yet — fixing it.", "failed"
        item = StepItem(
            seq=0,
            tool="verify",
            label=label,
            # `pending` IS the in-flight state in this vocabulary — the same one a real
            # tool call sits in between its call and its return.
            state=step_state,
            hidden=False,
        )
        # The SAME tool_call_id for both phases, which is how the client replaces the
        # pending card in place instead of stacking two rows.
        tool_call_id = f"verify-{state.turn_id}-{iteration}"
        self._emit(
            state,
            lambda seq: StepFrame(seq=seq, tool_call_id=tool_call_id, phase=phase, item=item),
        )

    async def _emit_preview_ready(self, state: _TurnState, preview_url: str | None) -> None:
        """The single chokepoint BOTH Write-path preview emits pass through — verify's and the
        watcher's — which is exactly why the warm request belongs here and not where readiness
        is discovered. `_watch_preview` polls `/dev/status` on its own 1s cadence and will see
        `ready` independently of anything verify concludes, so warming at the point of
        DISCOVERY races the watcher. Warming at the emit cannot be raced, and the
        `claim_preview_frame` guard upstream means it happens once per turn, not once per
        observer.

        The warm call gates nothing (R6) — it cannot raise and it cannot veto the frame. It also
        cannot COST the frame, which is what the `finally` is for. The caller has already burned
        the one-shot `claim_preview_frame()` guard by the time it gets here, so a cancellation
        landing inside the (up to 8s) warm request would leave the frame permanently claimed and
        never emitted — the citizen loses the preview for the whole turn, and the guard means no
        later poll will re-claim it. `_stop_preview_watcher` cancels this task at every terminal,
        so that window is walked on ordinary turn ends, not just on exotic ones.

        Emitting from a cancellation unwind is seq-safe because `_emit` is fully SYNCHRONOUS: it
        assigns `state.seq`, appends to the ring and wakes subscribers with `put_nowait`, with no
        await anywhere. So the frame is on the ring before `_stop_preview_watcher`'s `await task`
        returns, which is what preserves terminal ordering."""
        sandbox = state.sandbox
        try:
            if sandbox is not None:
                await sandbox.sandbox_client.someone_has_to_go_first(sandbox.handle)
        finally:
            state.preview_url = preview_url
            state.preview_state = "ready"
            self._emit(
                state,
                lambda seq: PreviewFrame(seq=seq, state="ready", preview_url=preview_url),
            )

    async def _poll_compile_state(self, state: _TurnState, sandbox: SandboxSession) -> None:
        """Ask the container what it is compiling and publish it — the signal the preview pane
        covers its frame with (R17/R18).

        RIDES THE PREVIEW WATCHER rather than owning a loop. The watcher already polls once a
        second for the whole turn, which is the cadence "appears and clears within seconds"
        needs; a second task would double the timers, the cancellation paths and the ways a
        frame can land after the terminal, and buy nothing.

        EMITTED ON CHANGE. The ring is sized for narrative, and one frame per poll would be
        several hundred per build — the compile state is a level, not an event.

        `compile_state` NEVER RAISES (see the client), so there is nothing to catch here. That
        is deliberate: an exception on this path would kill the watcher that also owns crash
        detection, trading a covered preview for an undetected dead dev server."""
        report = await sandbox.sandbox_client.compile_state(sandbox.handle)
        if report.protocol_drifted and state.compile_drift_generation != report.connect_generation:
            # Once per SUCCESSFUL connect. The alarm says the frame vocabulary moved upstream,
            # which no amount of retrying fixes and which nothing else in the system can see:
            # defensive parsing means a renamed protocol looks exactly like a quiet one.
            state.compile_drift_generation = report.connect_generation
            _log.warning(
                HMR_PROTOCOL_DRIFT_EVENT,
                app_name=sandbox.handle.app_name,
                connect_generation=report.connect_generation,
                reason=report.reason,
            )
        if report.state is state.compile_state:
            return
        state.compile_state = report.state
        self._emit(state, lambda seq: CompileFrame(seq=seq, state=report.state))

    async def _watch_preview(self, state: _TurnState) -> None:
        """Poll the dev server so the preview appears the moment it is servable, and so a
        crash is REPORTED rather than left as a blank iframe.

        This has to live server-side: `/dev/status` is bearer-guarded, so the browser cannot
        ask, and only the server holds the supervisor token. Every failure is swallowed — a
        watcher that raised would take the build down with it over a polling blip.

        The crash arm is DEBOUNCED over `CRASH_EDGE_CONSECUTIVE_POLLS` — see that constant, and
        keep `orchestrator/harness.py::_watch_preview` in step with it: the two emit the same
        signal to the same pane, so a debounce on one of them only would make the crash edge
        depend on which code path built the app."""
        sandbox = state.sandbox
        if sandbox is None:
            return
        reconnecting = False
        unanswered_polls = 0
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
            await self._poll_compile_state(state, sandbox)
            if status.ready:
                unanswered_polls = 0
                if state.claim_preview_frame() or reconnecting:
                    # First serve, or recovered after a crash — either way the client needs
                    # the url to (re)mount its iframe on.
                    await self._emit_preview_ready(state, sandbox.handle.preview_url)
                reconnecting = False
            else:
                # Counted on the PAIR (nothing answering AND no child alive), not on the framed/
                # reconnecting bookkeeping, so the streak means exactly what its name says. A
                # live child that is merely still compiling resets it immediately.
                unanswered_polls = unanswered_polls + 1 if not status.running else 0
                if (
                    state.preview_framed
                    and not reconnecting
                    and unanswered_polls >= CRASH_EDGE_CONSECUTIVE_POLLS
                ):
                    # The dev process exited AFTER we framed. Said once, distinctly: without it
                    # a dead iframe masquerades as "still building" until the turn ends.
                    reconnecting = True
                    state.preview_state = "reconnecting"
                    self._emit(state, lambda seq: PreviewFrame(seq=seq, state="reconnecting"))
            await asyncio.sleep(READINESS_POLL_S)

    async def _settle_compile_state(self, state: _TurnState) -> None:
        """One last compile poll when the turn ends mid-build, so the pane is not left holding a
        cover nothing will ever lower.

        `building` cannot outlive the turn that caused it, but the watcher that reports it is
        cancelled at the terminal — so a turn that ends in the second between "compiling" and
        "compiled" strands the preview under "Putting the latest change together…" with no
        remaining producer. One poll usually settles it to `clean` or `failed`, either of which
        resolves the pane correctly.

        ONLY on `building`, so the common path pays nothing. If it is STILL building afterwards
        the cover stays up, which is at least honest — and the next turn resolves it."""
        sandbox = state.sandbox
        if sandbox is None or state.compile_state is not CompileState.BUILDING:
            return
        await self._poll_compile_state(state, sandbox)

    async def _stop_preview_watcher(self, state: _TurnState) -> None:
        task = state.preview_task
        if task is None:
            return
        state.preview_task = None
        task.cancel()
        # AWAIT the cancellation, do not just request it: an un-awaited watcher can still be
        # mid-`_emit` and land a frame after the terminal, which the transport has closed.
        # Only the cancellation itself is expected here — any OTHER exception means the
        # watcher died on its own some time mid-turn, and swallowing that hides a real
        # defect. Logged, never re-raised: this runs on the terminal path, where an error
        # must not stop the guard release (same narrowing as `harness._stop_watcher`).
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            _log.exception(
                "preview_watcher_failed",
                conversation_id=str(state.conversation_id),
                turn_id=str(state.turn_id),
            )

    async def _hold_liveness_lease(self, state: _TurnState) -> None:
        """Publish, on a wall clock, that a build is happening inside this user's container —
        for a reader that is not this process (R10, C5 family 4, ADR-0029 §8).

        THE PROBLEM IT SOLVES. The heartbeat is seeded ONCE per turn against a 90-second TTL,
        so from roughly a minute and a half into any build the only thing keeping the
        reconciliation sweep off a live container is `sweep_all`'s in-process `live_users`
        set — which is empty in every other process. That is why nothing capable of
        destroying a container may run outside the API process until this loop exists: a
        sweep on the worker would have torn down in-flight builds on its first pass.

        Wall clock, never `time.monotonic()`: a monotonic reading is meaningless outside the
        process that took it, and being readable from another process IS the feature. The
        write carries a TTL, so a lease abandoned by a process that died mid-renewal expires
        instead of pinning the container forever — the registry hash's missing TTL is the
        root cause of the whole reclamation problem and must not be repeated here.

        BEST-EFFORT, BUT NEVER SILENT. A Redis blip may not take a ten-minute build down, so
        every failure is caught and the loop carries on. What it may not do is let the turn
        proceed BELIEVING itself protected with nothing written, so both failure shapes are
        logged under one greppable event: a store that would not answer, and a renewal that
        found no registry hash to attach itself to."""
        if state.sandbox is None:
            # NO CONTAINER, NOTHING TO VOUCH FOR — the same guard, for the same reason, as
            # `_watch_preview`'s. The lease is keyed by USER, not by turn, so a turn that
            # never attached anything would otherwise stamp a lease over whatever this
            # user's slot is actually holding — a chat turn in one conversation buying a
            # reprieve for a build in another. Only the attach path starts this task today;
            # the guard is what keeps that true if a second caller ever appears.
            return
        while True:
            try:
                if not await renew_liveness_lease(get_redis(), state.user_id):
                    _log.warning(
                        LEASE_RENEW_FAILED_EVENT,
                        conversation_id=str(state.conversation_id),
                        turn_id=str(state.turn_id),
                        reason="no_registry",
                    )
            except Exception:
                _log.exception(
                    LEASE_RENEW_FAILED_EVENT,
                    conversation_id=str(state.conversation_id),
                    turn_id=str(state.turn_id),
                    reason="store_unavailable",
                )
            await asyncio.sleep(LIVENESS_LEASE_RENEW_CADENCE_SECONDS)

    async def _stop_liveness_lease(self, state: _TurnState) -> None:
        """Stop renewing and drop the lease. Idempotent — the turn's `finally` is reached by
        five different terminal arms, and a second call finds nothing to do.

        RELEASED AFTER THE SANDBOX FINALIZE, NEVER BEFORE IT — which is the one way this
        differs from `_stop_preview_watcher`, and the difference is deliberate. The watcher
        dies first because a frame landing after the terminal is lost; the lease is the
        opposite shape, because releasing it early opens a reap window over
        `finish_turn_sandbox` — which snapshots and can comfortably outlive the 90-second
        heartbeat TTL that would otherwise be the container's only cover.

        Failures are swallowed rather than raised: this runs on the terminal path, where the
        remaining work is releasing the conversation guard, and a Redis blip must not wedge a
        conversation shut. The cost of not landing is bounded by the lease's own TTL."""
        task = state.lease_task
        if task is None:
            # NOTHING WAS PUBLISHED, SO NOTHING MAY BE REVOKED. The key is per-USER, not
            # per-turn: an unconditional delete here would let a chat turn that never took a
            # container strip the lease off a build running in another of the same user's
            # conversations, and hand the sweep a live container to reap.
            return
        state.lease_task = None
        task.cancel()
        # Await the cancellation rather than just requesting it, and narrow exactly as
        # `_stop_preview_watcher` does: only the cancellation itself is expected, and any
        # OTHER exception means the renewal loop died on its own mid-turn — a real defect
        # that must be logged rather than hidden by the shutdown.
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception:
            _log.exception(
                "liveness_lease_task_failed",
                conversation_id=str(state.conversation_id),
                turn_id=str(state.turn_id),
            )
        # `shield`, for the same reason `finish_turn_sandbox` has one: the stopped path
        # arrives here BECAUSE the task was cancelled, and an unshielded await would be
        # cancelled again the instant it yielded — leaving the lease held, and the next
        # sweep sparing a container nobody is building in for up to a TTL.
        with suppress(Exception):
            await asyncio.shield(release_liveness_lease(get_redis(), state.user_id))

    def _pending_meta(self, deferred: ToolCallPart | None) -> dict[str, Any] | None:
        """The row meta for a batch that carries the pending options call: the card's id, and
        nothing else.

        TWO SHORT SCALARS, AND NOT THE PLAN. `meta` is JSONB that a redaction pass walks, and
        putting up to 64,000 characters of plan here would be a third durable copy of a string
        the tool call's own `args` already holds authoritatively. A copy that can silently
        disagree with the call it describes is worse than no copy — every reader goes to the
        args instead.

        THE SNAPSHOT PIN IS GONE TOO (U6). It recorded the app's head at plan time so Build-it
        could warn that the app had moved underneath the plan. Its only writer sat inside a
        mode branch, and what it bought is paid for better: the instruction to follow the
        code's reality where it differs from what the plan assumed now lives in the Build
        chat's own prompt, where it works for a plan built weeks later rather than only when
        two snapshot heads happen to differ."""
        if deferred is None:
            return None
        return {"kind": PENDING_META_KIND, "toolCallId": deferred.tool_call_id}

    # NOTHING SYNTHESIZES A CARD ANY MORE. `_synthesize_options` used to fabricate one — a
    # hidden `plan_options_pending` system row with `synthesized: True` — when the heuristic
    # said a plan had been written and neither the run nor the forced retry had called the
    # tool, so that "the buttons ALWAYS appear". They appeared under plans nobody had agreed
    # to. `plan_options._scan` still READS the synthesized shape, and must: rows written by
    # the retired writer are in the database, and revision 0035 resolved their cards rather
    # than deleting them.

    def _emit_plan_status(self, state: _TurnState, tool_call_id: str) -> None:
        """The "writing up the plan" line, held in `state.steps` so a client that subscribes
        mid-argument sees it in the catch-up snapshot like any other in-flight step.

        IT HAS NO DURABLE COUNTERPART, and that is deliberate rather than an omission: a status
        that exists only while a turn is streaming has nothing to say on a reloaded transcript,
        which shows the plan and the offer and never the moment before them. Same reasoning as
        the turn's opening acknowledgement, which is also never persisted."""
        item = StepItem(
            seq=0,  # transient: no row, so no row seq
            tool=PLAN_OPTIONS_TOOL,
            label=WRITING_UP_THE_PLAN_LABEL,
            state="pending",
            hidden=False,
        )
        state.acknowledgement = None
        state.steps[tool_call_id] = item
        self._emit(
            state,
            lambda seq: StepFrame(seq=seq, tool_call_id=tool_call_id, phase="started", item=item),
        )

    def _push_plan(self, state: _TurnState, plan: str) -> None:
        """The plan onto the live stream, through the ungated text sink.

        Separated from `_push_text` only by the block separator: an earlier response in the
        same turn may already have flushed prose, and `text_parts` joins with nothing between
        entries, so without this the plan runs into that prose's last sentence."""
        if state.text_parts:
            self._push_text(state, TEXT_BLOCK_SEPARATOR)
        self._push_text(state, plan)

    def _emit_plan_options(self, state: _TurnState, tool_call_id: str) -> None:
        item = PlanOptionsItem(
            seq=0,  # live card; the reload projection assigns the row seq
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
        """The pydantic-ai event_stream_handler: model/tool events → typed frames.

        ASK/PLAN ONLY — Write drives its own node loop (`_run_write_once`) and calls
        `_on_event` directly. No flush here on purpose: pydantic-ai invokes this handler once
        per NODE, and a response's text and its tool calls arrive from two different nodes,
        text first. Flushing at the end of this iteration would therefore commit prose before
        the tool call that classifies it has been seen — which is the leak, restated. Ask/Plan
        never hold anything anyway (`_stream_text` commits immediately outside Write).
        """

        async def handle(
            _ctx: RunContext[ChatDeps], events: AsyncIterable[AgentStreamEvent]
        ) -> None:
            async for event in events:
                self._on_event(state, event)

        return handle

    def _on_event(self, state: _TurnState, event: AgentStreamEvent) -> None:
        if isinstance(event, PartStartEvent):
            if isinstance(event.part, TextPart) and event.part.content:
                self._stream_text(state, event.part.content, new_block=True)
            elif (
                isinstance(event.part, ToolCallPart) and event.part.tool_name == PLAN_OPTIONS_TOOL
            ):
                # A STATUS THE MOMENT THE BLOCK OPENS, and only for this one tool.
                #
                # The name is available before any argument is: the provider's
                # `content_block_start` for a tool use carries `name` with an empty `input`,
                # which pydantic-ai surfaces here as a `ToolCallPart` whose `tool_name` is
                # already set. That matters because the plan now rides the argument —
                # thousands of tokens stream between this event and the call resolving, and
                # with prose held beside a tool call the screen shows nothing new for the
                # whole of it.
                #
                # NOT WIDENED TO EVERY TOOL, deliberately: the others resolve fast and already
                # emit at `FunctionToolCallEvent`, so emitting at both events would double
                # every step row in the transcript.
                self._emit_plan_status(state, event.part.tool_call_id)
        elif isinstance(event, PartDeltaEvent):
            if isinstance(event.delta, TextPartDelta) and event.delta.content_delta:
                self._stream_text(state, event.delta.content_delta, new_block=False)
        elif isinstance(event, FunctionToolCallEvent):
            # This response is DOING something, so any prose it opened with was the model
            # narrating its way to the tool — not a message to the citizen. Dropped before
            # it can reach the wire, which is why the live feed and a later reload agree.
            self._discard_pending_text(state)
            if event.part.tool_name == PLAN_OPTIONS_TOOL:
                # The plan FIRST, then the card beneath it — the order the citizen reads, and
                # the same order the reload projection produces from this one stored call.
                # A call carrying no usable plan pushes nothing and offers nothing; the turn's
                # own closing line says so, once, from the persist path below.
                plan = plan_from_call(event.part)
                if plan is not None:
                    self._push_plan(state, plan)
                    # The options card, not a step: the call defers (the user's click is the
                    # result), so there is no 'finished' counterpart to wait for. It REPLACES
                    # the status on the same tool_call_id rather than stacking beside it.
                    self._emit_plan_options(state, event.part.tool_call_id)
                state.steps.pop(event.part.tool_call_id, None)
                return
            item = self._step_item(state, event.part.tool_name, event.part.args_as_json_str())
            # REPLACED, not accumulated beside: the first real step retires the ack from the
            # snapshot, so a client that subscribes later never sees both.
            state.acknowledgement = None
            state.steps[event.part.tool_call_id] = item
            self._emit(
                state,
                lambda seq: StepFrame(
                    seq=seq, tool_call_id=event.part.tool_call_id, phase="started", item=item
                ),
            )
            self._start_long_operation(state, event.part.tool_call_id, hidden=item.hidden)
        elif isinstance(event, FunctionToolResultEvent):
            # BEFORE the resolved frame, so the status line is gone from the row the instant
            # the operation completes rather than one refresh later.
            self._stop_long_operation(state, event.tool_call_id)
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

    # -- the long-operation status line (U17 / R24) --------------------------------------

    def _start_long_operation(self, state: _TurnState, tool_call_id: str, *, hidden: bool) -> None:
        """Arm the stillness narrator for one tool call.

        HIDDEN STEPS ARE NOT NARRATED, and that is a correctness point rather than a taste one:
        a hidden step renders nowhere, so refreshing it would change no pixels while still
        burning a frame every few seconds — narration that cannot be seen is noise by
        definition. The visible row shows the neutral "Working…" placeholder for that window,
        which is the honest thing to say about work the citizen was never shown."""
        if hidden:
            return
        state.long_operation_tasks[tool_call_id] = asyncio.create_task(
            self._narrate_long_operation(state, tool_call_id)
        )

    def _stop_long_operation(self, state: _TurnState, tool_call_id: str) -> None:
        """Disarm one narrator — the operation finished. Synchronous, so no await sits between
        the operation completing and the status line being unable to speak again."""
        task = state.long_operation_tasks.pop(tool_call_id, None)
        if task is not None:
            task.cancel()

    async def _narrate_long_operation(self, state: _TurnState, tool_call_id: str) -> None:
        """One live status row for an operation that has outrun `LONG_OPERATION_THRESHOLD_MS`.

        THE HARNESS SAYS THIS, NOT THE AGENT — which is why it lives here and not in a prompt.
        The composite operations U21 and U23 introduce are precisely the ones that REMOVE the
        per-step narration filling these gaps today, so a citizen watching a three-minute
        install would otherwise watch a row that stopped changing several minutes ago.

        It re-emits the SAME step (same `tool_call_id`, still `phase="started"`) with the
        restated label, so it replaces the row in place — one live line, never a second one
        accumulating beside it — and the ordinary `finished` frame clears it. The base label is
        deliberately left untouched in `state.steps`: re-deriving from it keeps the text
        byte-identical across refreshes, and keeps a mid-turn reconnect's snapshot clean.

        Under the threshold nothing is emitted at all. A fast turn must not flicker a status
        line on and off — a line that appears for 300ms reads as a glitch, not as reassurance."""
        try:
            await asyncio.sleep(LONG_OPERATION_THRESHOLD_MS / 1000)
            while True:
                pending = state.steps.get(tool_call_id)
                if pending is None or pending.state != "pending":
                    return  # resolved out from under us — nothing left to narrate
                still = pending.model_copy(update={"label": long_operation_line(pending.label)})
                self._emit(
                    state,
                    lambda seq: StepFrame(
                        seq=seq, tool_call_id=tool_call_id, phase="started", item=still
                    ),
                )
                await asyncio.sleep(LONG_OPERATION_REFRESH_MS / 1000)
        except Exception:
            # Never the turn's problem. Reassurance failing is a cosmetic loss; a narrator
            # taking a build down with it would be the unit causing the outage it prevents.
            # `CancelledError` is a BaseException and so passes straight through — the ordinary
            # way this ends.
            _log.warning(
                "long_operation_narration_failed",
                conversation_id=str(state.conversation_id),
                turn_id=str(state.turn_id),
                exc_info=True,
            )

    async def _drain_long_operations(self, state: _TurnState) -> None:
        """Cancel and AWAIT every remaining narrator, on every terminal arm.

        The same lesson as `_stop_preview_watcher`: cancelling without awaiting leaves a task
        that may be mid-`_emit`, and the frame it lands arrives after the transport closed."""
        tasks = list(state.long_operation_tasks.values())
        state.long_operation_tasks.clear()
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                _log.exception(
                    "long_operation_narrator_failed",
                    conversation_id=str(state.conversation_id),
                    turn_id=str(state.turn_id),
                )

    def _push_text(self, state: _TurnState, text: str) -> None:
        """Commit prose to the citizen: onto the snapshot tail AND onto the wire.

        DELIBERATELY UNGATED, and `_render_completion` is why — the completion message is
        delivered through this same call (U18). A gate here rather than at the streaming
        call site would silence the one message the U15 drop is relying on to survive.
        """
        state.text_parts.append(text)
        self._emit(state, lambda seq: TextDeltaFrame(seq=seq, text=text))

    def _stream_text(self, state: _TurnState, text: str, *, new_block: bool) -> None:
        """Prose arriving mid-response, before we know whether a tool call follows it.

        A Plan chat commits immediately — there the prose IS the deliverable and a held
        stream would be a dead screen. A Build chat holds it: see `_TurnState.pending_text`.

        `new_block` marks a fresh `TextPart` rather than a delta continuing the current one.
        Blocks were previously concatenated with nothing between them, which ran the last
        sentence of one into the first word of the next ("…the workspace.Now let me…") on
        the live feed only — reload always kept them as separate items.
        """
        if state.kind is not ChatKind.BUILD:
            if new_block and state.text_parts:
                self._push_text(state, TEXT_BLOCK_SEPARATOR)
            self._push_text(state, text)
            return
        if new_block and state.pending_text:
            state.pending_text.append(TEXT_BLOCK_SEPARATOR)
        state.pending_text.append(text)

    def _discard_pending_text(self, state: _TurnState) -> None:
        """Drop held prose — a tool call proved it was narration between tools (U15/R20)."""
        state.pending_text.clear()

    def _flush_pending_text(self, state: _TurnState) -> None:
        """Commit held prose — the response ended without calling a tool, so this is the
        turn's own answer. The zero-mutation Write ending depends on this: that turn never
        calls `declare_done`, so nothing else would ever say anything."""
        held = "".join(state.pending_text)
        state.pending_text.clear()
        if not held.strip():
            return
        if state.text_parts:
            self._push_text(state, TEXT_BLOCK_SEPARATOR)
        self._push_text(state, held)

    def _step_item(self, state: _TurnState, tool_name: str, args_json: str) -> StepItem:
        """A step, live. `args_json` is READ and never transmitted: it decides the friendly
        label and whether the step is a hidden read, and then it is done.

        THE ARGUMENTS USED TO RIDE THE FRAME, redacted here at the boundary because the
        persistence seam redacted the rows and the two renderings had to agree. They agree
        trivially now: neither carries them. Redaction at a boundary is only ever as good as
        the redactor, and the thing that cannot leak is the thing that was never sent."""
        label, hidden = classify_tool_call(tool_name, args_json)
        return StepItem(
            seq=0,  # live steps have no row seq; the reload projection assigns real ones
            tool=tool_name,
            label=label,
            state="pending",
            hidden=hidden,
        )

    def _resolve_step(self, state: _TurnState, event: FunctionToolResultEvent) -> StepItem | None:
        pending = state.steps.get(event.tool_call_id)
        if pending is None:
            return None
        part = event.part
        failed = not isinstance(part, ToolReturnPart)  # a RetryPromptPart = refused/failed
        # THE RESULT IS NOT READ, and that is the unit's point rather than an oversight: a
        # tool's return is the single richest thing a turn holds — file contents, command
        # output, whatever the sandbox said — and it used to be clipped, redacted and shipped
        # on every step. Whether the call succeeded is the whole of what a step reports now.
        resolved = pending.model_copy(update={"state": "failed" if failed else "ok"})
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
        # U17 — the status-line narrators are silenced BEFORE the terminal frame, synchronously.
        # A narrator is always parked on a sleep, so cancelling here means the CancelledError
        # lands at that sleep and it can never reach `_emit` again: no status row after the
        # terminal, with no await in between for one to slip through. The `finally` then awaits
        # them (`_drain_long_operations`) — this only makes the ordering unloseable.
        for task in state.long_operation_tasks.values():
            task.cancel()
        state.status = status
        state.ended_monotonic = time.monotonic()
        self._emit(
            state,
            # `reason` rides out with the terminal. It was set on `_TurnState` and then read
            # nowhere, so the frame that names WHY a turn stopped never carried the why — and the
            # portal already had a green fixture asserting `reason: 'stopped_by_user'` against a
            # contract the server did not fulfil. The human sentence still reaches the citizen via
            # `TurnErrorFrame`; this is the machine-readable half.
            lambda seq: TurnEndedFrame(
                seq=seq,
                turn_id=str(state.turn_id),
                status=status,
                reason=state.end_reason,
            ),
        )

    async def _write_turn_terminal(
        self, state: _TurnState, session_factory: SessionFactory
    ) -> None:
        """One hidden `system_event` row saying how this turn ended.

        BOTH KINDS, UNCONDITIONALLY, and nothing here reads `state.kind`: a Plan turn and a
        Build turn resume the same way or one of them has the weaker path, and the weaker one
        is always the one nobody notices until a citizen is looking at a frozen transcript.

        NOTHING IS WRITTEN FOR A TURN THAT DID NOT REACH A TERMINAL — a process killed
        mid-flight never gets here at all, which is the point: the absence of this row IS the
        ended-unknown signal, and a row written on the way out of an unfinished turn would
        destroy it.

        BEST-EFFORT, AND SAID SO. This runs in the terminal path, after the reply is already
        durable and after the subscriber has already been told the turn ended. A raise here
        would take down the release and the watcher teardown below it — a leaked container and
        a hung feed — to protect a row whose only job is to make a LATER reload truthful. So it
        is logged and swallowed, exactly as `write_build_outcome` treats seq contention, and a
        reload that finds no terminal falls back to the honest ended-unknown reading."""
        if state.status not in ("completed", "failed", "stopped"):
            return
        try:
            async with session_factory() as db:
                await append_batch(
                    db,
                    user_id=state.user_id,
                    conversation_id=state.conversation_id,
                    # AN EMPTY PAYLOAD, and it is the whole of why this row is safe to write on
                    # every turn. `load_history` flattens every row's payload — hidden ones
                    # INCLUDED, because a hidden row can carry the tool return that answers a
                    # deferred call, and dropping it would hand the model a dangling call. A row
                    # with no messages contributes nothing to that flattening, so the model's
                    # context is untouched. The fact lives entirely in `meta`, which only the
                    # projection reads. A one-part `ModelResponse` here — even an empty string —
                    # would put a blank assistant message into every subsequent prompt of the
                    # conversation, for the rest of its life.
                    messages=[],
                    entry_kind=MessageEntryKind.SYSTEM_EVENT,
                    kind=state.kind,
                    visibility=MessageVisibility.HIDDEN,
                    meta={
                        "kind": TURN_TERMINAL_KIND,
                        "turnId": str(state.turn_id),
                        "status": state.status,
                        "reason": state.end_reason,
                    },
                )
        except Exception:
            _log.exception(
                "turn_terminal_row_failed",
                conversation_id=str(state.conversation_id),
                turn_id=str(state.turn_id),
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
            # THE ACK RIDES HERE, and this is the only place it can. It is emitted at `seq == 1`
            # before any client can subscribe, and the route sets `last_sent = snapshot.seq`, so
            # the ring frame is already behind every subscriber's cursor. Same reasoning the
            # preview/compile/error_message fields above are carried for — a frame that fired
            # before the client connected lives only in the ring, and the snapshot is what makes
            # a subscription self-sufficient. Ordered FIRST so it reads as the oldest row.
            steps=([state.acknowledgement] if state.acknowledgement else [])
            + list(state.steps.values()),
            error_message=state.error_message,
            workspace_state=state.workspace_state,
            preview_url=state.preview_url,
            preview_state=state.preview_state,
            compile_state=state.compile_state,
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
