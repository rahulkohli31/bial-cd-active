"""The unified turn engine: one detached run per message, one
subscribable per-conversation event stream for chat AND build activity.

WHY THIS EXISTS
A message STARTS a turn server-side, detached from the HTTP connection: the subscriber
transport (`api/v1/conversations/turns.py`) only OBSERVES. Generalized from the build feed's
proven shape (`build_sessions/sse.py`, copy-not-share): an append-only per-turn frame RING is
the replay authority, subscriber queues are pure wakeups, and the terminal frame explicitly
closes the transport. Deliberately NO Redis: a run dies with the process, Postgres is the
durable log, and the ring is the one seam a Streams buffer would replace for multi-replica later.

Resume is catch-up-snapshot-then-tail: a subscriber that cannot prove gap-free continuity
(fresh subscribe, F5 with cursor=0, or a stale cursor) gets ONE consolidated `snapshot` frame
— persisted rows plus the in-memory text/step tail — then the live frames. One that CAN prove
continuity (same turn, cursor still in the ring) replays just the missed frames; falling past
the ring's tail degrades to a fresh snapshot, never a gap.

Gating on the chat's kind happens HERE, off the server's record, never the client request:
the run gets exactly `toolsets_for_kind(conversation.kind)` over the turn-pinned workspace,
and the instructions composed for that kind. Plan turns bill once for the whole turn,
disconnect-safe by construction — the task IS the drain; Write turns arrive with warm sessions
and bill per step through the harness. Ownership: the engine holds the per-conversation guard
(`turns/guard.py`) from claim to the task's `finally`, so a crashed run can never wedge its
conversation shut.
"""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from collections import deque
from collections.abc import AsyncIterable, Awaitable, Callable, Mapping
from contextlib import suppress
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from functools import partial
from typing import Any, Final, Literal, cast

import structlog
from pydantic_ai import Agent, BinaryContent, RunContext
from pydantic_ai._agent_graph import AgentNode
from pydantic_ai.exceptions import ModelAPIError, ModelHTTPError, UsageLimitExceeded
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
    ThinkingPart,
    ThinkingPartDelta,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models import Model
from pydantic_ai.models.anthropic import AnthropicModelSettings
from pydantic_ai.result import FinalResult
from pydantic_ai.tools import DeferredToolRequests
from pydantic_ai.usage import RequestUsage, RunUsage, UsageLimits
from pydantic_graph import End
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
    TurnPart,
    TurnStepPart,
    TurnStreamFrame,
    TurnTextPart,
    WorkingFrame,
    WorkspaceFrame,
)
from src.core.integrity_types import BaselineIdentity
from src.db.models.conversation import ChatKind, Conversation
from src.db.models.harness_counter import HarnessCounter
from src.db.models.message import MessageEntryKind, MessageVisibility
from src.db.models.user import User
from src.services.agent.agent import ChatDeps, chat_agent
from src.services.agent.attachment_tools import AttachmentReader
from src.services.agent.mode_prompts import PromptContext, workspace_note
from src.services.agent.read_tools import (
    LiveSandboxWorkspace,
    ReadOnlyWorkspace,
)
from src.services.agent.toolsets import toolsets_for_kind
from src.services.attachments.materialize import (
    AttachmentDelivery,
    AttachmentPlacementError,
)
from src.services.build_sessions.alarms import (
    APP_FIRST_SERVED_EVENT,
    APP_SERVING_LOST_EVENT,
    HMR_PROTOCOL_DRIFT_EVENT,
    SANDBOX_DEV_STARTED_EVENT,
    SERVING_PROOF_STAMP_REFUSED,
)
from src.services.build_sessions.counters import count
from src.services.build_sessions.integrity import (
    baseline_identity,
    has_ever_been_built,
    stamp_the_watermark,
)
from src.services.build_sessions.locks import (
    an_instant_on_the_hash,
    clear_serving,
    elapsed_ms,
    mark_serving,
    read_registry,
    release_liveness_lease,
    renew_liveness_lease,
    renew_lock,
    write_heartbeat,
)
from src.services.build_sessions.manager import (
    BuildSession,
    BuildSessionConflictError,
    RecoveryNews,
    SandboxReclaimBlockedError,
    SessionManager,
    SnapshotUnavailableError,
    StopOutcome,
    WorkspaceUnreadableError,
    app_name_for,
)
from src.services.build_sessions.outcome import STOPPED_BY_USER
from src.services.messages.projection import (
    PLAN_OPTIONS_TOOL,
    PLATFORM_TEXT_KIND,
    PROPOSE_SLICE_TOOL,
    TELL_THE_USER_TOOL,
    TURN_TERMINAL_KIND,
    DisplayItem,
    PlanOptionsItem,
    StepItem,
    agreed_slice,
    classify_tool_call,
    finished_from_args,
    finished_slice,
    long_operation_line,
    proposal_from_args,
    update_from_args,
)
from src.services.messages.store import append_batch
from src.services.orchestrator.client_errors import discard_client_errors
from src.services.orchestrator.constants import (
    ADAPTIVE_THINKING,
    BUILD_EFFORT,
    CACHE_TTL,
    CRASH_EDGE_CONSECUTIVE_POLLS,
    MAX_OUTPUT_TOKENS,
    MODEL_TURN_CEILING,
    PLAN_EFFORT,
    READINESS_MAX_POLLS,
    READINESS_POLL_S,
    RUN_TOKEN_BUDGET,
    RUN_WALL_CLOCK_DEADLINE_S,
    SELF_HEAL_MAX_RETRIES,
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
from src.services.redis.keys import (
    REGISTRY_FIELD_APP_NAME,
    REGISTRY_FIELD_CREATED_AT,
    REGISTRY_FIELD_SERVING_SINCE,
)
from src.services.sandbox import SandboxClient, SandboxError
from src.services.sandbox.base import CompileState
from src.services.turns.copy import (
    CANNOT_TELL_WHAT_REMAINS_TEXT,
    CHAT_TOO_LONG_CODE,
    CHAT_TOO_LONG_TEXT,
    COULD_NOT_CHECK_TEXT,
    COULD_NOT_CONFIRM_TEXT,
    DID_NOT_COME_TOGETHER_TEXT,
    MODEL_UNAVAILABLE_CODE,
    MODEL_UNAVAILABLE_PLAN_TEXT,
    MODEL_UNAVAILABLE_TEXT,
    NOT_RECOVERED_TEXT,
    PLAN_NOT_KEPT_TEXT,
    RECOVERED_TEXT,
    REMAINDER_TEXT,
    SPENT_ENOUGH_TEXT,
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
    weighted_spend,
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

# HOW THE PROVIDER SAYS "THAT PROMPT DID NOT FIT", and why anything reads it at all.
#
# `usage/context_window.enforce_context_limit` refuses an over-full conversation at the route,
# before a word is persisted. It reads the token count the provider reported for the LAST turn
# it served, because nobody can count the message about to be sent without asking the provider —
# so a chat admitted just under the ceiling, sent with a large attachment, overflows the served
# window AFTER the check has passed. That is the one case no pre-flight can pre-empt, and it is
# the only reason this match exists. It is error handling, not a second estimate: it needs no
# number, and it must never grow one.
#
# MEASURED THROUGH THIS EXACT STACK, NOT ASSUMED. An over-window send through pydantic-ai's
# `AnthropicModel` over `AsyncAnthropicFoundry` raises `ModelHTTPError` — never the SDK's own
# `BadRequestError`, which pydantic-ai wraps and re-raises — carrying `status_code == 400` and a
# parsed body of `{'type': 'error', 'error': {'type': 'invalid_request_error', 'message':
# 'prompt is too long: 1963668 tokens > 1000000 maximum'}, 'request_id': ...}`.
#
# THE STATUS IS NOT THE MATCH, AND THAT IS THE WHOLE CARE HERE. Every malformed request Foundry
# refuses is a 400 — an unsupported media type, a bad tool schema, a password-protected PDF —
# and answering any of them with "this chat is full, start a new chat" sends the citizen to a
# new chat that fails identically — a loop the citizen cannot leave by following the advice they
# were given.
# So the provider's own sentence decides and the status only narrows it. The second marker is
# the provider's other phrasing for the same fact: the prompt fits, the prompt plus the reply it
# is allowed to write does not.
_CONTEXT_OVERFLOW_MARKERS: Final = ("prompt is too long", "exceed context limit")

# THE OTHER 400 A DOCUMENT CAN EARN, and it is not the same fact as a full chat.
#
# The provider refuses any PDF over 600 pages outright — `messages.0.content.0.pdf.source.
# base64.data: A maximum of 600 PDF pages may be provided.` — measured through this exact stack
# against the deployment in use. It is not a size refusal: a 0.84 MB PDF of 601 text pages is
# refused while a 10 MB scan of forty is not, so no byte cap at the upload door can see it
# coming, and the door deliberately imposes no page cap.
#
# WHY IT IS NAMED RATHER THAN LEFT GENERIC. This is a permanent property of the file the citizen
# just attached, and "the assistant hit a problem and this turn was stopped" invites the one
# thing that cannot work — sending again.
#
# AND WHY THE REMEDY NAMES BOTH HALVES. The user turn is persisted before the model is called,
# and `load_history` rehydrates its bytes into every later turn, so the document is a permanent
# resident of the chat it landed in and that chat refuses identically for as long as it exists.
# Naming only a new chat — the overflow sentence — sends them somewhere the same file fails
# again. Naming only a shorter document leaves them in a chat that will refuse it. Neither half
# works alone.
#
# In practice the window usually bites first — a page costs ~2,900 tokens measured, so a
# ~175-page document already fills the conversation — and that path is the overflow arm above.
# This arm is for the documents that reach 600 pages while staying cheap enough per page not to
# have overflowed on the way.
_PDF_TOO_MANY_PAGES_MARKERS: Final = ("maximum of 600 pdf pages", "pdf pages may be provided")

DOCUMENT_TOO_LONG_TEXT: Final = (
    "That PDF has too many pages for the assistant to read, and it stays in this chat, so "
    "every message here will hit the same limit. Start a new chat and attach a shorter "
    "document — or split this one and attach just the part you need."
)
"""What the citizen reads when the provider refuses a document on its page count.

Names the file as the cause and gives a remedy that works from where they are standing. Quotes no
page number deliberately: the limit is the provider's, not the platform's, and a number stated
here would be one more thing to keep true across a deployment change."""

DOCUMENT_TOO_LONG_CODE: Final = "DOCUMENT_TOO_MANY_PAGES"
"""The machine-readable half, riding out on the terminal frame beside the sentence."""


def _provider_refusal_message(exc: ModelHTTPError) -> str:
    """The provider's own sentence for a 400, or "" when there is not one to read.

    Defensive on the body's SHAPE while staying narrow on its CONTENT: the documented shape is
    a parsed `{"error": {"message": ...}}`, and a body that is a bare string (a gateway that
    answered with something other than the provider's JSON) is read as the message itself.
    Anything else yields no message, and therefore matches nothing — an unreadable body is not
    evidence of any particular cause."""
    if exc.status_code != 400:
        return ""
    body: object = exc.body
    if isinstance(body, Mapping):
        error: object = body.get("error")
        if isinstance(error, Mapping):
            candidate: object = error.get("message")
            if isinstance(candidate, str):
                return candidate.lower()
    elif isinstance(body, str):
        return body.lower()
    return ""


def _is_document_too_long(exc: ModelHTTPError) -> bool:
    """Whether this 400 means the attached PDF has more pages than the provider will read.

    THE STATUS IS NOT THE MATCH, for the same reason it is not the match for the overflow above:
    every malformed request is a 400, and answering an unsupported media type with "that PDF has
    too many pages" is the same class of untrue sentence read from the other end."""
    return any(marker in _provider_refusal_message(exc) for marker in _PDF_TOO_MANY_PAGES_MARKERS)


def _is_context_overflow(exc: ModelHTTPError) -> bool:
    """Whether this provider refusal means the prompt did not fit, rather than any other 400.

    Narrow on CONTENT, and defensive about the body's shape through
    `_provider_refusal_message`: a body it cannot read yields no message and therefore no match,
    because an unreadable body is not evidence that a chat is full."""
    return any(marker in _provider_refusal_message(exc) for marker in _CONTEXT_OVERFLOW_MARKERS)


# THE TWO THINGS THE HARNESS SAYS WHEN NOTHING ELSE IS SPEAKING.
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
# after the turn starts would otherwise get a still screen, which is the whole point of the
# acknowledgement.
# Between two `TextPart`s of one response. Blank, not nothing: concatenating them raw ran the
# last sentence of a block into the first word of the next ("…the workspace.Now let me…").
TEXT_BLOCK_SEPARATOR: Final = "\n\n"

ACK_TEXT = "Getting started on that…"
# The reserved tool name the acknowledgement rides under, so it is identifiable as the
# harness's own row rather than a step the agent took.
#
# THE FILTERING IS ENTIRELY ON THIS SIDE, and there is no client-side half to look for. The
# ack is held in `_TurnState.acknowledgement` rather than in `steps`, so it never reaches the
# persisted rows, and it is cleared by the first real step.
ACK_TOOL = "__ack__"
ACK_TOOL_CALL_ID = "__ack__"

# THE STILLNESS THRESHOLD. An operation still running after this long earns a
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

# WHAT A FINISHED BUILD SAYS WHEN THE AGENT HANDED US NOTHING TO SAY.
#
# `declare_done` is terminal now, so the summary it carries is the whole of the completion
# message — and a model that calls it with an empty string would otherwise end a working build
# in silence. The fallback is never the model's own text: the alternative to a summary is a
# sentence the harness wrote, not a scrape of whatever prose happened to precede the tool call,
# because that prose is exactly the register a completion message must not use.
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

# The one greppable name for "this turn's liveness lease did not land". A constant
# rather than two inline literals because the two failure shapes — the store would not answer,
# and there was no registry hash to attach the lease to — are one operational question ("is
# anything protecting live builds right now?"), and an alert cannot be written against a
# string that exists in two spellings. The reason is a field, not part of the event name.
LEASE_RENEW_FAILED_EVENT = "liveness_lease_renew_failed"

# The lock + heartbeat half of the same loop, spelled with the SAME two log events
# `SessionManager.on_progress` already writes for this exact pair of calls. Identical strings on
# purpose: the lock now has two renewers, and "did a live build lose its lock?" is one
# operational question that must not need two alerts to answer. Named here rather than inlined
# for the reason `LEASE_RENEW_FAILED_EVENT` is — an alert cannot be written against a string
# that exists in two spellings.
LOCK_LOST_EVENT = "build session lock lost during an active build"
LOCK_RENEW_FAILED_EVENT = "liveness renew/heartbeat failed during build"

# WHICH observer watched the app answer, in `APP_FIRST_SERVED_EVENT`'s `observer` vocabulary.
# Two of them live in this file, and they are two DIFFERENT sightings rather than one written
# twice: the watcher polls `/dev/status` every second for the life of the turn, while the
# self-heal verify asks between model steps. A first serve credited to `turn_verify` says the
# 1s watcher was behind — it had gone round its `except SandboxError` arm, or the loop was
# between polls — which is precisely the condition that used to mean NOTHING EVER STAMPED, so
# collapsing the two names would hide the one case worth seeing.
#
# A closed `Literal` for the same reason the manager's sibling has one: a typo would mint an
# observer that never existed, and the log rule keyed on the name would silently match nothing.
_ServingObserver = Literal["turn_watcher", "turn_verify"]
_OBSERVER_TURN_WATCHER: Final[_ServingObserver] = "turn_watcher"
_OBSERVER_TURN_VERIFY: Final[_ServingObserver] = "turn_verify"

# THE STORE WOULD NOT ANSWER, which is not the same fact as the compare-and-set REFUSING.
# `SERVING_PROOF_STAMP_REFUSED` means Redis answered and said no — the hash was gone, ending, or
# named another container — and its WHAT-TO-DO sends an operator looking for a reclaim. This one
# means nothing was written at all because the round trip failed, and the remedy is Redis. One
# alert cannot serve both, so they are two names. Local rather than in `build_sessions/alarms.py`
# for the reason `LEASE_RENEW_FAILED_EVENT` above is: it is a transport failure in one caller,
# not a lifecycle notice the whole platform emits.
SERVING_PROOF_WRITE_FAILED_EVENT = "serving_proof_write_failed"


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

    Tolerant of a malformed argument object rather than raising: unparseable JSON means no plan,
    the same answer as an empty one, and not a reason to fail an otherwise-working turn. (A pre-
    migration call took no arguments and reads as absent too; revision 0035 resolved every one of
    those cards.) Split out so `transition._refusal_for` can tell "no plan" from "plan too long" by
    which branch of `plan_from_call` returned `None` — a third rejection reason here would
    misreport itself as "too long"."""
    try:
        args = part.args_as_dict()
    except Exception:
        return None
    plan = args.get("plan")
    if not isinstance(plan, str):
        return None
    return plan.strip() or None


def plan_from_call(part: ToolCallPart) -> str | None:
    """The plan an offer carries, or None when the call cannot be honoured.

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

    Removed rather than stored-and-skipped: "no offer is recorded" has to be true at two
    independent readers, `plan_options._scan` (row meta) and the projection (the stored call
    itself), and teaching both to ignore an unhonourable call is two rules to keep in step —
    the projection's would also have to tell a migrated call (no argument, still rendered)
    from a new one (no argument, never rendered). Not writing it is one rule, and it leaves
    the dangling-call repair nothing to stitch."""
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
    """The durable transcript slice of a run's `new_messages()`: every `ModelResponse`, PLUS every
    `ModelRequest` that carries tool returns but NOT a fresh user prompt.

    Persisting the tool-return requests is load-bearing: they answer the `ToolCallPart`s the
    responses make, and a responses-only filter leaves each call unanswered, so reload's dangling-
    call repair papers over a real result with a synthesized "interrupted" one. Requests bearing a
    `UserPromptPart` are excluded: the user turn is already persisted, and the ephemeral workspace
    note injected onto `message_history` must never fossilize into a row."""
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
# used to live.
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


THINKING_TOKENS_KEY: Final = "thinking_tokens"
"""Where the provider reports what it spent THINKING, inside `RunUsage.details`.

Anthropic bills thinking WITHIN `output_tokens` rather than beside it, so this is a readable
SUBSET of the output total and never an addition to it — subtracting it is sound, adding it
would double-count. The key is omitted entirely when a response used no thinking, which is why
every reader defaults it to zero rather than requiring it."""


def _citizen_output_tokens(usage: RunUsage | RequestUsage) -> int:
    """The output tokens that are the CITIZEN's, with the platform's thinking taken back out.

    Reasoning is a choice the platform made on the citizen's behalf — they did not ask for it,
    cannot see it, cannot turn it off — so charging their daily allowance for it would move their
    budget for a reason they cannot act on. Subtracted HERE, ONCE, so the per-run ceiling and the
    row the daily meter sums get the same policy. Floored at zero: a negative answer means the
    provider's two numbers disagree, and charging nothing is the honest failure there."""
    return max(usage.output_tokens - usage.details.get(THINKING_TOKENS_KEY, 0), 0)


def _run_spend(usage: RunUsage) -> int:
    """What this run has spent, weighted the way the citizen's daily meter weights it.

    NOT `usage.total_tokens`: under pydantic-ai `input_tokens` already folds in the cache buckets
    (10 fresh + a 90k cache read arrives as `input_tokens == 90_010`), so a raw bound prices a
    cached prefix at full rate — the incident `billable_spend` records, where one calculator build
    booked 956k of a 1M daily cap on 68 tokens of fresh input. Weighting lives in `usage/gate.py`
    beside the daily meter's own column expression: two ceilings weighting tokens differently would
    be two numbers described to the citizen as one word."""
    return weighted_spend(
        input_tokens=usage.input_tokens,
        output_tokens=_citizen_output_tokens(usage),
        cache_read_tokens=usage.cache_read_tokens,
        cache_write_tokens=usage.cache_write_tokens,
    )


def _is_transient_model_status(status_code: int) -> bool:
    """The statuses the Anthropic SDK itself retries (`_should_retry` in `anthropic._base_client`):
    408, 409, 429 and every 5xx. A failure with one of these has already been retried by the time
    it reaches the engine, so it is the model service's problem and gets the named ending; any
    other 4xx is a request the platform built wrong and keeps the generic one."""
    return status_code in (408, 409, 429) or status_code >= 500


_ERROR_CHAIN_LIMIT: Final = 5
# The provider's error `type` (`overloaded_error`) is the one string in the signature that
# arrives from outside the process, so anything but a short lowercase token is dropped.
_PROVIDER_ERROR_TYPE: Final = re.compile(r"[a-z][a-z0-9_]{0,63}")


def _error_signature(exc: BaseException) -> str:
    """What ended a failed turn, as a short record for the terminal row — class names, never text.

    WHY THIS EXISTS. On 2026-09-11 a build turn ended through the generic arm and the only record
    of the exception was the `turn_run_failed` log line, in an archive the team cannot read; the
    database row said `reason: null` and nothing else. Foundry's metrics then ruled out a rate
    limit or an HTTP error, which left the cause unrecoverable. Written onto the terminal row, the
    same question is one query away next time.

    CLASS NAMES ALONG THE CAUSE CHAIN, plus the HTTP status and the provider's error TYPE token
    when the chain carries them (`ModelAPIError <- APIStatusError status=200 type=overloaded_error`
    is what a stream that ended in an error event looks like), and WHERE it surfaced in the code
    (`_raise_site`). NEVER THE MESSAGE: exception text
    can carry bound SQL parameters, file content or a provider's echo of the prompt, and this
    codebase already refuses to log bound parameters."""
    names: list[str] = []
    status: int | None = None
    provider_type: str | None = None
    seen: set[int] = set()
    current: BaseException | None = exc
    while current is not None and id(current) not in seen and len(names) < _ERROR_CHAIN_LIMIT:
        seen.add(id(current))
        names.append(type(current).__name__)
        code: object = getattr(current, "status_code", None)
        if status is None and isinstance(code, int):
            status = code
        body: object = getattr(current, "body", None)
        if provider_type is None and isinstance(body, dict):
            error = cast("dict[str, object]", body).get("error")
            if isinstance(error, dict):
                token = cast("dict[str, object]", error).get("type")
                if isinstance(token, str) and _PROVIDER_ERROR_TYPE.fullmatch(token):
                    provider_type = token
        current = current.__cause__ or current.__context__
    signature = " <- ".join(names)
    if status is not None:
        signature += f" status={status}"
    if provider_type is not None:
        signature += f" type={provider_type}"
    site = _raise_site(exc)
    if site is not None:
        signature += f" at={site}"
    return signature


def _raise_site(exc: BaseException) -> str | None:
    """Where `exc` surfaced, as `module:function:line`: the innermost frame in the platform's own
    code when the traceback has one, otherwise the innermost frame at all. A class name alone
    (`KeyError`) does not say which line to open, and the traceback that does sits in the log
    archive; a place in the code carries no data, so it is safe on the row."""
    site: str | None = None
    tb = exc.__traceback__
    while tb is not None:
        module = str(tb.tb_frame.f_globals.get("__name__", "?"))
        here = f"{module}:{tb.tb_frame.f_code.co_name}:{tb.tb_lineno}"
        if site is None or module.startswith("src.") or not site.startswith("src."):
            site = here
        tb = tb.tb_next
    return site


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
        # the system does not know.
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
    """What the conversation read reports: the in-flight turn and its newest seq (the
    cursor a subscriber resumes the event stream from)."""

    turn_id: uuid.UUID
    last_seq: int


@dataclass
class _TextBlock:
    """One contiguous stretch of the turn's prose, as the citizen reads it.

    A BLOCK, NOT A DELTA. `text` grows while the model is still writing the same `TextPart`
    and is sealed the moment anything else — a step, or a fresh `TextPart` — takes its place
    in `_TurnState.parts`. That is what makes one stretch of writing render as one paragraph
    block live, exactly as the reload projection renders one item per stored text part."""

    text: str


@dataclass
class _StepRef:
    """A step's PLACE in the turn, held apart from the step itself.

    `_TurnState.steps` is keyed by tool-call id because a step arrives twice (started, then
    finished) and the second must replace the first in place. That map cannot also record
    ORDER — a resolved step would move to the end of it — so the position lives here and the
    item is resolved through the map when the snapshot is built. A ref whose id has left the
    map (evicted at the cap, or withdrawn like the plan-options status) is dropped with it."""

    tool_call_id: str


_TurnPart = _TextBlock | _StepRef
"""What a turn produced, in the order it produced it — the thing the live stream and the
reload projection have to agree about. Reload has always emitted one item per part in part
order; before this existed the live path could only append every step and then one block of
text, so the two agreed only while a turn was guaranteed at most one text block."""


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
    #: The turn's prose and its steps, INTERLEAVED, in emission order — the catch-up
    #: snapshot's whole content and the reason a reattached citizen reads the same turn as
    #: one who never left.
    parts: list[_TurnPart] = field(default_factory=list)
    steps: dict[str, StepItem] = field(default_factory=dict)  # tool_call_id → newest item
    # The acknowledgement, held OUT of `steps` and beside it. `steps` is what gets PERSISTED, so
    # the ack must stay out of it — but the catch-up SNAPSHOT is the only way a subscriber ever
    # learns about a frame emitted before it connected, and every client connects after
    # `start_turn` has already run. Keeping the ack out of both meant it reached nobody. Cleared
    # by the first real step, which is what "replaced by the first real step" has to mean on a
    # transport where the ring frame is unreachable.
    acknowledgement: StepItem | None = None
    # The "Writing up the plan…" status's call id, tracked for exactly the reason the ack above
    # is: it is a PLATFORM-owned status with no durable counterpart, so the only thing that can
    # ever take it off a watching tab is this engine deciding to. The offer arm withdraws it when
    # the plan lands; a Plan turn that fails or is stopped in the window between the block opening
    # and the argument completing never reaches that arm, and without this the terminal has no way
    # to name the step it must withdraw. `None` on every turn that never opened one.
    plan_status_tool_call_id: str | None = None
    #: Does the model HAVE THE FLOOR right now — see `_set_working` for the six sites that move it.
    #:
    #: A boolean, never the text. The blocks are stored so the next turn can replay them and
    #: are never projected, never framed and never sent to the browser; what the citizen gets
    #: is one status line saying the agent is working. This said "set when a reasoning part opens"
    #: after that stopped being the only raiser — the last outstanding tool returning raises it
    #: too, and that is the window a hung-looking transcript was actually sitting in.
    working: bool = False
    subscribers: set[asyncio.Queue[None]] = field(default_factory=set)
    task: asyncio.Task[None] | None = None
    # The per-tool-call "this is still running" narrators, keyed by tool call id.
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
    # The turn's newest workspace/preview facts, for the same reason: a `preview` frame
    # that fired before the client connected is gone from the ring by the time a mid-build
    # reconnect asks, so the snapshot carries them instead of a second REST round-trip. None
    # only until `_attach_sandbox` runs, or after it failed.
    workspace_state: Literal["preparing", "ready", "unavailable"] | None = None
    preview_url: str | None = None
    preview_state: Literal["ready", "reconnecting"] | None = None
    # SET ON EVERY TURN OF BOTH KINDS — `_pin_workspace` has one arm and both kinds attach the
    # project's live container (its docstring carries the two-workspace history this block used to
    # restate). `sandbox` is that container session — Build's eight sandbox-routed tools call it
    # and Plan's read tools reach the same session through `LiveSandboxWorkspace`. `write_session`
    # is the manager's registry entry, kept so the terminal can hand it back.
    # `preview_task` is the per-turn dev-server watcher — cancelled and AWAITED before the
    # terminal frame, or a late preview frame lands after `[DONE]`. All three are None only on a
    # turn whose attach never completed.
    sandbox: SandboxSession | None = None
    # The conversation's code-lane attachments and the store they live in, or
    # None when it holds none. Set by the send route, which is the only layer holding both the
    # database session and the object store; used twice — once inside the attach, to put the
    # files in the container before the agent's first read, and once at the top of the run, to
    # tell the agent they are there. Carrying the storage HANDLE rather than the bytes is what
    # keeps a detached turn from pinning tens of megabytes for its whole life.
    attachments: AttachmentDelivery | None = None
    write_session: BuildSession | None = None
    preview_task: asyncio.Task[None] | None = None
    # The liveness lease renewal. Started where the container is attached,
    # released after it is handed back. `None` on a turn that never took a container — a turn
    # with nothing to keep alive must not stamp a lease over whoever does hold the user's slot.
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
    # HAS THIS TURN FINISHED ASKING WHETHER THE SERVING PROOF IS DOWN? Set once either observer
    # gets a DEFINITIVE answer out of Redis — it stamped, someone else already had, or the
    # compare-and-set refused and said why. Retracted by the crash edge, so a container that
    # comes back re-proves itself and earns a second `app_first_served`.
    #
    # DELIBERATELY NOT `preview_framed`, and that distinction is the whole of this change.
    # `preview_framed` is a once-per-TURN one-shot that either of two callers may consume
    # WITHOUT writing anything; riding the proof on it meant that whenever the self-heal verify
    # won the claim, the app served and nothing ever stamped — the pane then sat on "Getting
    # your app ready" over a working app until the five-minute reaper. This flag is set only by
    # a call that actually attempted the write, so whichever observer arrives first, the stamp
    # lands. It is a latch against REPEATING the ask (the watcher asks once a second), never a
    # token that gates it.
    serving_proof_settled: bool = False
    # `SERVING_PROOF_WRITE_FAILED_EVENT` is said once per turn, for the reason
    # `said_it_could_not_check` is: a Redis outage under a 1s poll would otherwise write one
    # identical line per second per live turn, and the second one tells an operator nothing the
    # first did not. The RETRY is not suppressed — only the sentence.
    said_the_proof_would_not_write: bool = False
    # Has any turn on this app ever done real work? Resolved ONCE where the workspace is pinned
    # (one HEAD on the recovery slot) and carried, because it cannot change inside one turn and
    # the self-heal loop asks the health verdict for it up to four times. It gates the content
    # check: a brand-new project is SUPPOSED to be showing the starter template, and checking it
    # would manufacture an accusation rather than catch one.
    had_prior_building_turns: bool = False
    # `UNVERIFIED_TEXT` describes the state of the app, not an event, so it is said once
    # and then not again. Repeating it would train the reader to skip the one sentence most
    # likely to matter.
    said_it_could_not_check: bool = False
    # WRITE only, and the whole difference between the two zero-mutation endings. A Write
    # turn the citizen typed into may legitimately touch nothing (they asked a question);
    # a turn started from a Build-it click was ASKED to build, so touching nothing is a
    # FAILURE, not a quiet success. Nothing else about the two turns differs, so the caller
    # has to say which one this is — the engine cannot infer it from the prompt.
    expects_mutation: bool = False
    # What ended a FAILED turn, when the platform did not name it — see `_error_signature`.
    # Written onto the terminal row's meta as `error`; None on every other ending.
    error_signature: str | None = None
    #: Did THIS turn bring a container up, rather than joining one already serving? Set at the
    #: attach seam from `BuildSession.attached`, and read only by the container-start success
    #: ratio's numerator — a turn that started nothing must not be able to report a start that
    #: reached a serving page.
    started_a_container: bool = False
    #: Tokens this turn has spent across every `agent.iter` run it has made. A build
    #: turn makes several — the first attempt plus each repair round — and the bound is on the
    #: TURN, because that is the thing that ends and says what remains. `run.usage` covers
    #: only the run in flight, so finished runs are folded here as they close.
    tokens_spent: int = 0
    #: What the citizen last agreed to build first. Seeded from the conversation's own
    #: rows at turn start and replaced by any proposal made during the turn — latest wins, the
    #: same rule the offer follows. Empty means nothing was ever proposed, which is the ordinary
    #: case and produces no closing remainder at all.
    agreed_pieces: list[str] = field(default_factory=list)
    #: Which of those the agent marked finished as they landed. A SET, so a piece marked twice
    #: counts once — the citizen reads a list of what is left, and a double mark must not be
    #: able to make a piece disappear from it twice or appear as still outstanding.
    finished_pieces: set[str] = field(default_factory=set)

    def text_blocks(self) -> list[str]:
        """The prose the citizen has been given so far, one entry per block."""
        return [part.text for part in self.parts if isinstance(part, _TextBlock)]

    def drop_step(self, tool_call_id: str) -> None:
        """Withdraw a step entirely — from the map AND from the order.

        Dropping it from the map alone would leave a ref pointing at nothing, which the
        snapshot skips but never reclaims; on a build that runs for minutes and evicts at the
        cap that is an unbounded list of dead positions."""
        self.steps.pop(tool_call_id, None)
        self.parts = [
            part
            for part in self.parts
            if not (isinstance(part, _StepRef) and part.tool_call_id == tool_call_id)
        ]

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
    """Which of `DID_NOT_COME_TOGETHER_TEXT`'s three arms this verdict earns. Read off the
    verdict, not inferred; ordering makes each arm true rather than merely plausible. Not
    serving comes first, since a down app has no version to describe. Then the starter
    template, a specific and actionable thing to show. The earlier-version arm is last, the
    residual: everything genuinely the user's app, just without this change. A verdict with no
    serving answer at all takes the same arm as one that is down — the honest small
    over-claim, since telling someone their app is fine on a check that never completed is the
    failure this exists to avoid."""
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


def _reader_of(ctx: RunContext[ChatDeps]) -> AttachmentReader:
    """The ChatDeps accessor Plan's attachment reader resolves through.

    It reads the same field `_sandbox_of` does, and that is the point rather than a duplication:
    the reader runs `python3` inside the container, which is a capability no read-only workspace
    can express — `LiveSandboxWorkspace` routes every command through `check_the_guest_list`, and
    `python3` is deliberately absent from it. What keeps Plan read-only is the TOOLSET it is
    handed, which contains nothing that writes; it was never the absence of this field.

    Fail-first for the same reason as its neighbour: a run reaching this tool with no container is
    a bug in the attach path, and the honest response is to say so rather than to answer about a
    file nobody placed.
    """
    session = ctx.deps.sandbox
    if session is None:
        raise RuntimeError("attachment reader resolved on a turn with no attached sandbox")
    return AttachmentReader(session=session)


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
        """The `activeTurn` answer: only a RUNNING turn counts."""
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
        attachments: AttachmentDelivery | None = None,
    ) -> uuid.UUID:
        """Claim the conversation, persist the user turn (caller-supplied writer, so the
        route's typed seq-contention mapping stays with the route), spawn the detached run,
        and return the turn id. Raises `ConversationBusyError` or whatever `persist_user_turn`
        raises, with the claim released. `sandbox_client` stays Optional only as a signature
        shape now that both kinds attach a live container — the send route refuses a `None`
        sandbox outright, and `_attach_sandbox` fails loudly if one ever reaches it anyway.
        `expects_mutation` is the Build-it caller's declaration that this turn OWES a file
        change; only the plan-card path opts in (see the mutation guard in `_run_write`).

        `attachments` is the conversation's code-lane files. The ROUTE resolves them
        because only the route holds the database session and the object store together; the
        engine holds the container and the model's context, which is where both halves of the
        delivery happen. None until someone attaches a spreadsheet."""
        claim_conversation(conversation.id)
        try:
            await persist_user_turn()
            state = _TurnState(
                turn_id=uuid.uuid7(),
                conversation_id=conversation.id,
                user_id=user_id,
                kind=conversation.kind,
                expects_mutation=expects_mutation,
                attachments=attachments,
            )
            self._by_conversation[conversation.id] = state
            # ANSWER THE SCREEN BEFORE ANYTHING CAN BE SLOW.
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

    async def stop_user_turn_and_wait(
        self, user_id: uuid.UUID, *, timeout_s: float
    ) -> StopOutcome:
        """Stop this user's turn, WAIT for it to unwind, and report which of three things happened.

        `stop_turn` cannot serve "stop and switch": it is keyed on a conversation the caller does
        not have, and returns the instant `task.cancel()` is issued — before the turn's `finally`
        bills tokens, emits its terminal frame and runs `finish_turn_sandbox`, which is what makes
        the workspace releasable. Releasing earlier tears a container out from under a running
        task. THE VERDICT IS READ FROM THE TASK, not from having asked: `stop_requested` alone
        reads True mid-`finally`. `timeout_s` bounds the wait, not the shielded unwind."""
        state = next(
            (
                s
                for s in self._by_conversation.values()
                if s.user_id == user_id and s.status == "running" and s.task is not None
            ),
            None,
        )
        if state is None or state.task is None:
            return StopOutcome.NOTHING_WAS_RUNNING
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
        # THE TASK IS THE SOURCE OF TRUTH for whether this turn has finished unwinding. A wait
        # that expired leaves it pending, and saying anything else here is how a container gets
        # taken from a turn still writing.
        return StopOutcome.STOPPED if state.task.done() else StopOutcome.STILL_RUNNING

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
        transcript append → billing → terminal. Every exit path funnels to exactly one
        terminal frame, the guard release, AND the billing of whatever tokens the model
        actually consumed. WRITE forks at the run, not at the guard: same pin, same
        reminder, same terminal arms, same `finally` — but a node-by-node self-heal loop in
        place of the single `agent.run`. Everything around that fork is shared, so a fix to
        the terminal handling can never apply to only one of the two."""
        # ONE BUILD, ONE GREP — and it has to be bound HERE, inside the detached task, not at
        # the request seam that spawned it.
        #
        # `asyncio` copies the ambient context at task CREATION, so a bind in `start_turn`
        # covers the request and not reliably this task's lifetime — which is where every line
        # worth correlating is actually emitted: the attach's failures, `sandbox_dev_started`,
        # `app_first_served`, `app_serving_lost`, the terminal. `merge_contextvars` is already
        # wired as the first structlog processor (`main.py`) and until now nothing in
        # `backend/src` ever called `bind_contextvars`, so it was configured and inert. These
        # seven keys make the ~200 EXISTING failure lines in this file joinable retroactively,
        # with no edit to any of them.
        #
        # `build_id` IS the turn id for a turn (a relaunch mints its own), spelled as its own
        # key so one grep spans both doors into a container. `app_id`/`app_name` are what the
        # CALLER believes; `_attach_sandbox` re-binds both off the session it actually got,
        # because a Plan chat can carry no app id at all and the container is the authority.
        log_context = structlog.contextvars.bind_contextvars(
            build_id=str(state.turn_id),
            user_id=str(state.user_id),
            project_id=str(project_id),
            app_id=str(app_id) if app_id is not None else None,
            app_name=app_name_for(app_id) if app_id is not None else None,
            conversation_id=str(state.conversation_id),
            turn_id=str(state.turn_id),
        )
        # One usage accumulator threaded through both the primary run and the forced retry
        # (they increment it in place). Because it survives the run, an explicit Stop that
        # cancels the model mid-flight — or a DB error after the model replied — still bills the
        # tokens already spent, closing the daily-cap bypass a start→stop loop would open.
        turn_usage = RunUsage()
        billed = False

        async def _bill_once() -> None:
            """Fold the accumulated spend into today's cap exactly once, in a FRESH session
            (the run's own session may already be unwound on the cancel/error paths).
            Best-effort — a billing failure must never mask the outcome."""
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
                        output_tokens=_citizen_output_tokens(turn_usage),
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

        async def _fail_generically(exc: BaseException) -> None:
            """How a turn ends when it broke for a reason the citizen cannot act on.

            A CLOSURE BECAUSE THE SPECIFIC ARM HAS TO BE ABLE TO HAND AN ERROR BACK. The
            context-overflow match below sits ahead of the broad handler and catches a whole
            exception CLASS, most of which is not its business — and it cannot simply `raise` the
            rest onward, because a sibling `except` never catches what another one raises.
            Re-raising would carry an unrelated provider error clean out of this method, past the
            billing and past the terminal frame, leaving the turn "running" for every subscriber
            until their stall timeout. So the two arms share one ending rather than one of them
            having none."""
            _log.exception(
                "turn_run_failed",
                conversation_id=str(state.conversation_id),
                turn_id=str(state.turn_id),
            )
            state.error_signature = _error_signature(exc)
            # Partial spend before the failure still counts: bill what actually ran.
            await _bill_once()
            state.error_message = _TURN_FAILED_MESSAGE
            self._emit(
                state,
                lambda seq: TurnErrorFrame(seq=seq, message=_TURN_FAILED_MESSAGE),
            )
            self._finish(state, "failed")

        async def _end_model_unavailable(exc: Exception) -> None:
            """The model service failed mid-turn — named, not generic.

            NAMED for the reason the quota, the run bounds and the overflow are named: the
            assistant is fine, the workspace is as the last write left it, and the thing to do is
            send again — none of which "the assistant hit a problem" says. ONLY the service's own
            failures count: a status the SDK retries (`_is_transient_model_status`) or a
            `ModelAPIError` (a connection that never answered, or a stream that ended in an error
            event). A 400 about a media type or a 401 keeps the generic ending. A Build turn
            secures its tree through the function the run bounds use, so `{kept}` is verified
            before it is said; a Plan turn has no tree."""
            state.error_signature = _error_signature(exc)
            _log.warning(
                "turn_model_unavailable",
                conversation_id=str(state.conversation_id),
                turn_id=str(state.turn_id),
                status_code=getattr(exc, "status_code", None),
                error=state.error_signature,
            )
            await _bill_once()
            if state.kind is ChatKind.BUILD:
                # A STOP WHILE THE TREE IS SECURED STILL ENDS THE TURN. This runs inside an
                # `except`, so a cancellation landing in this await (up to a minute of container
                # round trips) is not the `except asyncio.CancelledError` arm's to catch, and
                # escaping would skip `_finish`: no terminal frame, no terminal row, a turn
                # "running" until every subscriber's stall timeout. The run bounds secure BEFORE
                # they raise, inside the `try`, which is why they never needed this.
                try:
                    ending = await at_limit_ending(state.sandbox, sentence=MODEL_UNAVAILABLE_TEXT)
                except asyncio.CancelledError:
                    state.end_reason = STOPPED_BY_USER
                    self._finish(state, "stopped")
                    return
                message = ending.message
            else:
                message = MODEL_UNAVAILABLE_PLAN_TEXT
            state.end_reason = MODEL_UNAVAILABLE_CODE
            state.error_message = message
            self._emit(state, lambda seq: TurnErrorFrame(seq=seq, message=message))
            self._finish(state, "failed")

        try:
            workspace = await self._pin_workspace(
                state,
                app_id,
                project_id=project_id,
                session_factory=session_factory,
                manager=manager,
                sandbox_client=sandbox_client,
            )
            # THE WORKSPACE NOTE, UNCONDITIONALLY, on every turn that pinned a sandbox: an
            # ephemeral tail on `message_history`, structurally excluded from the persisted rows
            # because `new_messages()` never contains injected history.
            #
            # It is the ONLY thing injected here, and it is injected on EVERY turn rather than on
            # a cadence, because it tells the model a fact about the app that its history cannot
            # know — one that can change between any two turns.
            if workspace is not None:
                note = await self._workspace_note(state)
                history = [*history, ModelRequest(parts=[UserPromptPart(content=note)])]
            # AND THE ATTACHED FILES, ON THE SAME CARRIER AND FOR THE SAME REASON.
            #
            # An ephemeral tail rather than part of the citizen's own message: the paths are a
            # fact about THIS container, and a container is not what a conversation is stored
            # against. Persisting them would leave a transcript naming files at paths a later
            # container may spell differently — and `new_messages()` never contains injected
            # history, so this cannot reach the stored rows even by accident.
            #
            # UNCONDITIONAL WHENEVER THERE IS A FILE, on every turn rather than the turn it was
            # uploaded on. The issue calls the agent writing its own parser the single failure
            # this design exists to prevent, and an agent only knows not to when it is told —
            # every time, because a model reads the turn in front of it.
            if state.attachments is not None:
                history = [
                    *history,
                    ModelRequest(parts=[UserPromptPart(content=state.attachments.note())]),
                ]
            # WHAT WAS AGREED, READ OUT OF THE CONVERSATION ITSELF. No column, no
            # table, no project field: the agreement is the arguments of the last honourable
            # proposal call in these rows, which is the same bounded route the plan travels. A
            # Plan chat that proposed and a Build chat that then builds are two conversations,
            # so this is empty in the second — and an empty agreement produces no closing
            # remainder at all, which is the honest answer rather than a missing one.
            #
            # SEEDED FOR BOTH KINDS, AND IT HAS TO BE. Only a Build turn renders the remainder,
            # so this looks like work a Plan turn could skip — but the live emitter checks every
            # mark against this list before recording it, and that branch is kind-blind because
            # the voice channel is on both arms of the toolset. Moving this inside the Build
            # fork would silently drop legitimate marks in a Plan chat that proposed a slice.
            state.agreed_pieces = agreed_slice(history)
            # AND THE MARKS WITH IT. Seeding only the agreement gave the two halves different
            # memories: the agreement survived a turn and the completion did not, so the second
            # turn of a piece-at-a-time build named the first turn's finished piece as still
            # outstanding — the platform asserting that finished work is undone, which is the
            # false fact this unit exists to prevent, arriving through the other door. Both
            # halves now come from the same record.
            state.finished_pieces = finished_slice(history)
            if state.kind is ChatKind.BUILD:
                # A READER OF THE KIND, AND IT ASKS WHICH HARNESS RUNS THE TURN — the node loop
                # with its per-step billing fold versus a single `chat_agent.run`. That is the
                # whole of what the kind decides here. Unifying the two loops would mean giving
                # a Plan run the streaming node loop and the per-step billing it has no steps
                # for, so the fork is a shape, not a behaviour.
                #
                # What the model CAN DO and what it is TOLD are decided in `agent/toolsets.py`
                # and `agent/mode_prompts.py`, and nothing downstream of either asks again.
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
                        # SET ON THIS ARM TOO NOW. It used to be Build-only, and the
                        # comment on the field said so — but the Plan arm's attachment reader
                        # runs `python3` in the same container, which is a capability no
                        # read-only workspace can express: `LiveSandboxWorkspace` routes through
                        # `check_the_guest_list`, and `python3` is deliberately not on it.
                        # Handing over the session does NOT widen what Plan may do: the toolset
                        # built below is what decides that, and Plan's does not contain a single
                        # tool that writes.
                        sandbox=state.sandbox,
                    )
                    # THE READER IS OFFERED ONLY WHEN THERE IS SOMETHING TO READ. A tool named
                    # `read_attachment` on a chat with no attachment is an invitation to invent a
                    # path and then explain the failure; `toolsets_for_kind` takes the accessor as
                    # optional for exactly this, and registration is per-run.
                    toolsets = toolsets_for_kind(
                        state.kind,
                        _workspace_of,
                        reader_of=_reader_of if state.attachments is not None else None,
                    ).toolsets
                    # UNCONDITIONAL, BECAUSE THE TOOLSET HAS ALREADY DECIDED IT. A run can only
                    # end deferred if a tool that DEFERS was registered on it, and
                    # `present_plan_options` — the one `CallDeferred` in the tree — is on the
                    # Plan arm and nowhere else.
                    #
                    # Widening costs nothing on a run that produced text: pydantic-ai strips
                    # `DeferredToolRequests` out of the output types and keeps a single flag,
                    # so the request is byte-identical and the flag is only ever read when a
                    # deferred call is actually present. `: Any` stays — the heterogeneous list
                    # is what the `run` overloads need to see to type-check.
                    output_type: Any = [str, DeferredToolRequests]
                    result = await chat_agent.run(
                        prompt,
                        deps=deps,
                        message_history=history,
                        model=model,
                        toolsets=toolsets,
                        output_type=output_type,
                        usage=turn_usage,
                        event_stream_handler=self._event_handler(state),
                        # MAX_TOKENS IS A CEILING, NOT A TUNING KNOB: the provider's 4096
                        # default truncates a long plan mid-argument, so the offer is refused and
                        # the citizen pays for a turn with nothing they can press. Effort is
                        # medium because a plan is a conversation with the person still in it;
                        # adaptive, not a budget — the model refuses one (ADAPTIVE_THINKING).
                        #
                        # THE SAME THREE CACHE BREAKPOINTS THE BUILD LOOP SETS, MISSING HERE
                        # until 2026-09-10. The provider caches only where the REQUEST carries
                        # `cache_control`, and these three settings place those markers — so every
                        # plan turn re-read its whole prefix at full price. It hid because build's
                        # `iter` loop pays off inside one turn; a plan's `run` only across turns.
                        # NO `temperature`, AND ITS ABSENCE IS THE FIX FOR A WARNING ON EVERY
                        # SINGLE CALL. The deployed models — every `claude-opus-4-7` and newer
                        # in pydantic-ai's profile — carry `anthropic_disallows_sampling_settings`,
                        # so `prepare_request` STRIPS `temperature`/`top_p`/`top_k` and raises
                        # `UserWarning: Sampling parameters ['temperature'] are not supported`.
                        # It was being sent anyway, so the constant's promise of deterministic
                        # generation was never in force and the log carried the library saying so
                        # once per model step. Reasoning models do their own sampling; the knobs
                        # that DO reach this deployment are the effort and the thinking mode
                        # below. Re-add it only against a profile that accepts it, and prove that
                        # with `prepare_request` rather than by assuming.
                        model_settings=AnthropicModelSettings(
                            max_tokens=MAX_OUTPUT_TOKENS,
                            anthropic_thinking=ADAPTIVE_THINKING,
                            anthropic_effort=PLAN_EFFORT,
                            anthropic_cache_instructions=CACHE_TTL,
                            anthropic_cache_tool_definitions=CACHE_TTL,
                            anthropic_cache=CACHE_TTL,
                        ),
                    )
                    persistable = _persistable_messages(result.new_messages())
                    deferred = _deferred_call(result.output)

                    # AN OFFER IS EITHER HONOURABLE OR IT IS NOT WRITTEN AT ALL.
                    # An empty plan, one past the stored-message ceiling, or a pre-migration
                    # call with no argument leaves nothing to press — so the call comes off
                    # what is persisted, no pending record is written, and the turn says so in
                    # one platform-authored line. Nothing unbuildable is left on screen, and
                    # the live feed already agreed: the event handler pushed no plan and
                    # emitted no card for the same call.
                    #
                    # A TURN THAT PRODUCED NO WORDS NOW PRODUCES NO ASSISTANT MESSAGE. There
                    # used to be a second arm here that pushed a platform-written sentence
                    # whenever the turn had said nothing, because the narration drop had made a
                    # wordless turn possible for the first time and a screen of finished steps
                    # with no message read as the product forgetting to answer. The drop is
                    # gone, so a wordless turn now means the model genuinely chose not to
                    # speak — and putting words in its mouth to cover a case that no longer
                    # exists is the thing this unit removes.
                    platform_text: str | None = None
                    if deferred is not None and plan_from_call(deferred) is None:
                        _log.info(
                            "plan_options_offer_refused",
                            conversation_id=str(state.conversation_id),
                            turn_id=str(state.turn_id),
                        )
                        persistable = _without_the_call(persistable, deferred.tool_call_id)
                        deferred = None
                        self._push_text(state, PLAN_NOT_KEPT_TEXT)
                        platform_text = PLAN_NOT_KEPT_TEXT

                    batches: list[
                        tuple[list[ModelMessage], dict[str, Any] | None, MessageEntryKind]
                    ] = [(persistable, self._pending_meta(deferred), MessageEntryKind.TURN)]

                    # NO SECOND MODEL REQUEST IS ISSUED HERE, and none is issued anywhere as a
                    # consequence of what the model wrote.

                    # WRITE-BEFORE-DONE: the reply must be durable before the
                    # turn may claim success. A failure of the persist seam is DISTINCT from
                    # a model failure — it raises the typed `_PersistFailedError` so the
                    # subscriber sees the "could not be saved" message, not the generic one.
                    # The user request is already durable (pre-run write) — append the
                    # responses PLUS their tool-return requests.
                    try:
                        for messages, meta, entry_kind in batches:
                            if messages:
                                await append_batch(
                                    db,
                                    user_id=state.user_id,
                                    conversation_id=state.conversation_id,
                                    messages=messages,
                                    entry_kind=entry_kind,
                                    kind=state.kind,
                                    meta=meta,
                                )
                        if platform_text is not None:
                            # ITS OWN ROW, IN NOBODY'S NAME BUT THE PLATFORM'S. This sentence
                            # explains a refusal the platform made. It used to be appended to
                            # the run's own messages as a `ModelResponse`, which stored it as
                            # something the model had written — and `load_history` flattens
                            # every row's payload, so the next turn handed the model its own
                            # explanation back as a paragraph it had authored and could build
                            # on or repeat.
                            #
                            # AN EMPTY PAYLOAD, WITH THE WORDS IN `meta`, which is the pattern
                            # the turn-terminal row already uses and the one `load_history`'s
                            # docstring names: hiddenness is a render predicate and was never a
                            # statement about what the model may see, so a row that must not
                            # reach the model carries no messages at all. The projection reads
                            # the sentence straight out of `meta` and renders it exactly as it
                            # did before — the citizen's transcript is unchanged; only the
                            # model's copy is gone.
                            #
                            # NO SECOND LIVE HOME. A durable, typed home for platform speech is
                            # coming to the turn-terminal row; when it lands this is the row it
                            # adopts. Routing the sentence to a live-only banner in the meantime
                            # would give the citizen the same words twice.
                            await append_batch(
                                db,
                                user_id=state.user_id,
                                conversation_id=state.conversation_id,
                                messages=[],
                                entry_kind=MessageEntryKind.SYSTEM_EVENT,
                                kind=state.kind,
                                meta={
                                    "kind": PLATFORM_TEXT_KIND,
                                    "turnId": str(state.turn_id),
                                    "text": platform_text,
                                },
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
        except ModelHTTPError as exc:
            # THE ONE OVERFLOW NO PRE-FLIGHT CAN CATCH, answered in the platform's words.
            #
            # AHEAD OF THE BROAD ARM, AND NARROW INSIDE IT. Only a refusal that says the prompt
            # did not fit is translated; every other provider error — a 429, a 500, a 400 about
            # a media type — takes the same generic ending it took before this arm existed, and
            # takes it HERE rather than by being re-raised, because a sibling `except` would not
            # catch it. See `_fail_generically` for what re-raising would cost.
            if _is_context_overflow(exc):
                _log.info(
                    "turn_context_overflow",
                    conversation_id=str(state.conversation_id),
                    turn_id=str(state.turn_id),
                    status_code=exc.status_code,
                )
                # The spend that got this far still counts, exactly as it does on every other
                # ending: the refused request is free, the turns before it were not.
                await _bill_once()
                # THE SAME SENTENCE AND THE SAME CODE THE ADMISSION CHECK REFUSES WITH — the
                # 413 `turns.start_turn` raises when the conversation is already past the
                # ceiling. One condition, one remedy, one wording: a second sentence for "this
                # chat is full" is a second thing to keep true, and the citizen cannot tell the
                # two situations apart anyway. The code rides out on the terminal frame as the
                # machine-readable half, which is how the browser offers the same way forward.
                state.end_reason = CHAT_TOO_LONG_CODE
                state.error_message = CHAT_TOO_LONG_TEXT
                self._emit(
                    state,
                    lambda seq: TurnErrorFrame(seq=seq, message=CHAT_TOO_LONG_TEXT),
                )
                self._finish(state, "failed")
            elif _is_document_too_long(exc):
                # A PROPERTY OF THE FILE, NOT OF THE CHAT, so it gets its own sentence rather
                # than the overflow's. Same shape as the arm above otherwise: bill what ran,
                # name the cause, finish failed.
                _log.info(
                    "turn_document_too_many_pages",
                    conversation_id=str(state.conversation_id),
                    turn_id=str(state.turn_id),
                    status_code=exc.status_code,
                )
                await _bill_once()
                state.end_reason = DOCUMENT_TOO_LONG_CODE
                state.error_message = DOCUMENT_TOO_LONG_TEXT
                self._emit(
                    state,
                    lambda seq: TurnErrorFrame(seq=seq, message=DOCUMENT_TOO_LONG_TEXT),
                )
                self._finish(state, "failed")
            elif _is_transient_model_status(exc.status_code):
                await _end_model_unavailable(exc)
            else:
                await _fail_generically(exc)
        except ModelAPIError as exc:
            # A connection that never answered, or a stream that ended in an error event — both
            # wrapped by pydantic-ai as `ModelAPIError`. AFTER the HTTP arm, necessarily:
            # `ModelHTTPError` IS a `ModelAPIError`, so this arm first would swallow the overflow
            # match above, and Python would say nothing about it.
            await _end_model_unavailable(exc)
        except Exception as exc:
            await _fail_generically(exc)
        finally:
            # THE DURABLE TERMINAL, FIRST IN THE FINALLY. `_finish` emits the live
            # `TurnEndedFrame` and cannot write it — it is synchronous by design, so that a
            # terminal frame reaches every subscriber with no await in between for a second
            # cancellation to slip through. So the row is written here, which is the same
            # boundary: `finally` runs exactly once per turn, on every arm, after whichever
            # `_finish` above set the status. Writing it from the arms instead would mean five
            # call sites and a turn that could leave two rows.
            await self._write_turn_terminal(state, session_factory)
            # The watcher dies FIRST, on every terminal arm and in BOTH kinds. The Build
            # loop already stops its own on the way out, but a Plan turn attaches the same
            # live container — `_attach_sandbox` starts the watcher for whoever attaches —
            # and has no loop of its own to stop it, so every Plan turn leaked a polling task
            # that could land a preview frame after the transport sent [DONE].
            # Idempotent by construction: the Write path's stop leaves `preview_task` None
            # and this backstop finds nothing to do.
            await self._stop_preview_watcher(state)
            # The status-line narrators go too, for the same reason and on every
            # arm. `_finish` already cancelled them; this is where they are actually awaited,
            # so none is still unwinding when the transport closes.
            await self._drain_long_operations(state)
            # THE RELEASE, on every single terminal arm — completed, stopped, persist-failed,
            # named-end, or a genuine bug. NO SAVE happens here: the bundle reaches
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
                                # Only a turn that MUTATED the tree is worth bundling. A Plan
                                # turn holds no tool that could set this, so it releases the
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
                # ordering is the same lesson as the shield above. `_stop_liveness_lease`
                # awaits, so on the stopped path a second cancellation can propagate out of
                # it; if the guard release sat after it, that would skip `release_conversation`
                # and every later turn in the conversation would answer 409 for the rest of
                # the process's life. Nothing depends on the lease outliving the guard: the
                # next turn's reconcile-on-start certifies death and deletes it anyway.
                release_conversation(state.conversation_id)
                # The lease goes LAST, once the container has actually been handed back.
                # Releasing it before `finish_turn_sandbox` would leave that snapshot
                # -and-pardon sequence — which can easily outlive the 90-second heartbeat TTL
                # — exposed to a concurrent sweep with nothing at all vouching for it.
                await self._stop_liveness_lease(state)
                # AND THE CORRELATION COMES OFF LAST, once every line this turn will ever
                # write has been written. Hygiene rather than a leak fix: a task's context is
                # its own copy, so these bindings die with the task even unreset — but
                # `_run_turn` is awaitable directly (the tests do exactly that), and an
                # abandoned binding there would stamp one turn's build id onto the next.
                structlog.contextvars.reset_contextvars(**log_context)

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
        """Resolve the turn-pinned read surface ONCE, for BOTH KINDS: the project's live container.
        (`api/v1/conversations/turns.py` quotes that first sentence — keep in sync.)

        HISTORY: Ask and Plan used to read a git checkout of the saved bundle — a COPY, stale the
        moment the agent changed anything, and unrunnable besides. A new project gets the live
        container too, since withholding it only made Plan's first build run on nothing. Nothing
        pins a version here any more: that warning is paid for better in the Build chat's own
        prompt, which works weeks later rather than only when two snapshot heads differ."""
        attach = partial(
            self._attach_sandbox,
            state,
            project_id=project_id,
            session_factory=session_factory,
            manager=manager,
            sandbox_client=sandbox_client,
        )
        # ONE ARM. There is no branch here at all any more — every turn, in both kinds,
        # resolves the project's LIVE container and nothing else.
        #
        # What used to sit above this was the last way a chat could answer from a saved copy:
        # `sandbox_client is None and this is not a Build chat` fell through to extracting the
        # newest snapshot bundle. That condition was never about the chat — `sandbox_client is
        # None` is a deployment fact wearing a branch on the kind — and the behaviour it bought
        # was a silent downgrade: the citizen asked about their app and got an answer about a
        # copy of it, with nothing on screen to say which. The same fact is now asked one layer
        # up, where a person can be told about it (`api/v1/conversations/turns.py`), so a
        # send that cannot reach a workspace is refused before it is spent.
        return LiveSandboxWorkspace(session=await attach())

    # -- the WRITE run -------------------------------------------------------------------

    async def _say_what_the_workspace_did(self, state: _TurnState, news: RecoveryNews) -> None:
        """Turn the integrity gate's finding into the one sentence the citizen reads.

        THE MANAGER KNOWS WHAT HAPPENED; THIS KNOWS HOW TO SAY IT. The gate cannot import
        these strings — `services.turns` reaches `build_sessions`, so an import back would
        close the cycle — and which words a citizen sees is a product decision besides.
        `UNVERIFIED` IS SAID ONCE PER TURN, NEVER AGAIN: it describes the state of the app
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

        Provisioning or restoring takes 30-60s, which is why it happens HERE — detached, after the
        202 — rather than inside the request: a citizen watching "Getting your workspace ready"
        knows what is happening, one watching an unanswered POST assumes it is broken. That is the
        whole reason `WorkspaceFrame` exists. Fails LOUDLY: `SnapshotUnavailableError` must never
        degrade into a fresh blank template, or the model starts editing an empty app and commits
        over the real one."""
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
                    # THE SENTENCE HAS TO ARRIVE BEFORE THE SLOW WORK, not after it. The
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

        if not session.attached:
            # THE CONTAINER-START SUCCESS RATIO'S DENOMINATOR. `relaunch_preview` counts the
            # explicit start control; this counts the other way a container comes up — on the way
            # to answering a question, which is how most of them come up. One name, two writers,
            # and neither fires on the other's path: `relaunch_preview` is never called from a
            # turn (the route is its only caller), and this line is inside the turn's own attach.
            #
            # `not session.attached` IS THE WHOLE CONDITION, and it is why `attached` had to be
            # forwarded onto the session at all. This method runs on EVERY turn of EVERY kind,
            # and on most of them the container is already up and serving — those turns started
            # nothing. Counting them would make the denominator "turns" and hand the ratio a
            # value near 1 that means nothing. What is left is exactly the two arms that bring a
            # container up: a fresh provision and a restore.
            #
            # ABOVE THE TWO INTEGRITY HOLDS BELOW ON PURPOSE. An UNRECOVERABLE or a RESTORED
            # turn ends without running the agent — but a container did come up, and it will
            # never reach the numerator. That gap is precisely what this ratio exists to expose,
            # so excluding these from the denominator would hide it.
            #
            # No duration row here: a 15-second attach budget and a 120-second cold budget
            # averaged together produce a number that describes neither. How long a COLD start
            # takes is a separate concern.
            state.started_a_container = True
            await count(HarnessCounter.APP_START_ATTEMPTED, app_id=session.app_id)
        # PUBLISHED BEFORE THE TWO INTEGRITY HOLDS, and the ordering is the whole point.
        #
        # `ensure_sandbox` has already REGISTERED this session in `_active_by_user` and ADOPTED
        # the user's build lock. The `finally` that hands both back is guarded on
        # `state.write_session is not None` — so while this assignment sat BELOW the two raises,
        # either of them left a registered session with `ended_at` never set, no renewer, and
        # nothing that could ever release it. `_active_by_user` never evicts an unended session,
        # so for the remaining life of the process that user was answered:
        #   • 409 `already_building_here` on every turn, in every conversation
        #   • 409 on relaunch
        #   • `still_running`, for ever, from `stop-active-build` — with no running turn to cancel
        # which is "I cannot create an application any more, and nothing I press helps".
        #
        # Both arms are reached precisely when a workspace came back wrong, so the citizen most
        # likely to hit this is the one already having a bad day.
        #
        # NOTHING IS BUNDLED BY MOVING IT. The `finally` passes `touched` from `state.sandbox`,
        # which is still `None` on both arms (it is assigned below), so the release skips the
        # snapshot exactly as it should — an UNRECOVERABLE turn must never make a template
        # permanent, which is what the first hold exists to prevent in the first place.
        state.write_session = session
        # THE CONTAINER IS THE AUTHORITY ON WHICH APP THIS BUILD IS ABOUT, so the correlation
        # bound at the top of the turn is corrected here off what `ensure_sandbox` actually
        # handed back. A Plan chat opened before anything was built carries no app id at all,
        # and this is the seam where one exists — so without this re-bind, the lines that matter
        # most (`sandbox_dev_started`, `app_first_served`, `app_serving_lost`) would carry a null
        # `app_name`, which is the one key that joins this trace to the registry hash, the
        # container name and the app's own URL path. Re-binding rather than binding for the first
        # time keeps ONE spelling per key; the turn's `finally` resets both with its own tokens.
        structlog.contextvars.bind_contextvars(
            app_id=str(session.app_id), app_name=session.handle.app_name
        )

        if session.news is RecoveryNews.UNRECOVERABLE:
            # Nothing was put back, and the container is showing a template. The one thing
            # that must not happen is the agent building on it and the turn-end copy making that
            # permanent, so the turn ends here.
            raise _WriteEndedError("workspace_unrecoverable", NOT_RECOVERED_TEXT)
        if session.restored:
            # THE HELD MESSAGE. The instruction was written against a workspace that no
            # longer exists; running it now would execute an instruction whose premise was true
            # when it was typed and false when it ran. The citizen re-sends when they have looked
            # at what came back.
            raise _WriteEndedError("workspace_restored", RECOVERED_TEXT)
        state.sandbox = SandboxSession(
            sandbox_client=sandbox_client,
            handle=session.handle,
            app_id=session.app_id,
            # No emitter: the turn engine renders the run's own tool events as step frames,
            # and a second feed would draw every step twice.
            emitter=None,
        )
        # THE ATTACHED FILES GO IN NOW — BEFORE THE AGENT'S FIRST READ.
        #
        # HERE, RATHER THAN ANYWHERE ELSE, because this is the one place a turn of either kind
        # first holds a live container, and because the container it holds may be a NEW one. The
        # attachments root is a sibling of the app tree precisely so no snapshot or restore
        # carries it, and the price of that is that a recycled container comes back empty — so a
        # conversation's files are placed on every turn, not on the turn they were uploaded.
        # `place` skips what is already there at the right size, so the ordinary second turn on a
        # surviving container transfers nothing.
        #
        # A FAILURE ENDS THE TURN. Carrying on would answer a question about a file the agent
        # cannot see, and every failure mode of that is silent: the reader says `missing`, and the
        # model either apologises or describes the file from its name. Neither is recoverable by
        # the citizen, and the second is indistinguishable from a real answer.
        if state.attachments is not None:
            try:
                await state.attachments.place(state.sandbox)
            except AttachmentPlacementError as exc:
                _log.warning(
                    "attachment_placement_failed",
                    conversation_id=str(state.conversation_id),
                    turn_id=str(state.turn_id),
                    app_id=str(session.app_id),
                    # THE CAUSE, NOT JUST THE FACT. `AttachmentPlacementError`'s own message is
                    # written for the citizen and says only that the file could not be placed;
                    # the storage or supervisor error underneath it is the half an operator
                    # needs. Bound as a field rather than through `exc_info=True`: this
                    # process's processor chain renders neither a traceback nor frame locals,
                    # and the frame it would try to render holds the supervisor bearer.
                    reason=str(exc.__cause__ or exc),
                )
                # The sentence is already citizen-facing — `place` words its own refusals for
                # the person who attached the file, naming it.
                unplaced = str(exc)
                state.workspace_state = "unavailable"
                self._emit(
                    state,
                    lambda seq: WorkspaceFrame(seq=seq, state="unavailable", message=unplaced),
                )
                raise _WriteEndedError("attachment_unavailable", unplaced) from exc
        # FENCE OFF ANY BROWSER CRASH REPORT THAT PREDATES THIS TURN. A report describes
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
        # HAS THIS APP EVER BEEN BUILT? One HEAD on the recovery slot, resolved here because
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
        # always done this at attach (it called `dev_start` right after attaching);
        # this brings unified chat to parity.
        #
        # Best-effort BY DESIGN. This is an optimization, never a gate: `verify`'s dead-child
        # rescue is the backstop, so a supervisor blip costs the preview a few seconds and not
        # the turn. And deliberately no `wait_ready` — `_watch_preview` polls at 1s and owns
        # framing; blocking the turn's start on readiness would trade one latency problem for
        # another one the user can see.
        try:
            await sandbox_client.dev_start(session.handle)
            # THE DEV SERVER IS COMING UP — AND THAT IS ALL THIS SAYS. The gap between this
            # line and `app_first_served` is the interesting one: a build that reaches here and
            # stops has a dev server that started and never compiled a route, which today is
            # indistinguishable in the log from a build that never got a container.
            #
            # `already_running` reads `handle.ready`, the `/dev/status` snapshot taken at handle
            # construction — BEFORE this call — which is hard-coded False on both birth arms and
            # a real reading only on the attach arm. That is exactly the question the field
            # asks: did an attach find something already answering, or did this call start it?
            _log.info(
                SANDBOX_DEV_STARTED_EVENT,
                arm="ensure_sandbox",
                already_running=session.handle.ready,
            )
        except SandboxError:
            _log.warning(
                "write_dev_start_at_attach_failed",
                conversation_id=str(state.conversation_id),
                turn_id=str(state.turn_id),
                app=session.handle.app_name,
                exc_info=True,
            )
        state.preview_task = asyncio.create_task(self._watch_preview(state))
        # AND THE LIVENESS LEASE, from the moment this turn owns the container.
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
        """Run, verify, repair — until the app is green AND the model says done, or a bound stops.

        The self-heal loop. Both halves of that gate are load-bearing: `declare_done` alone is the
        model's opinion (unreliable right after it wrote a type error), `green` alone would end the
        turn mid-thought the moment the tree happened to compile. Only the conjunction means done.
        Passing buys no extra round-trip: the model used to be asked for a whole further request
        whose only product was a closing paragraph in its own register, and `declare_done`'s
        summary says it in the reader's."""
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
                    # ONE ENDING FOR ALL THREE BOUNDS. What stood here named the
                    # bound and then told the citizen to "click Save to keep them" — the exact
                    # sentence `at_limit_ending`'s docstring records as the one that secured
                    # nothing and asserted something nobody had checked. This arm is the one
                    # MOST likely to be reached with a wedged container, so it is the one that
                    # can least afford to promise a save it never performed.
                    raise _WriteEndedError(
                        "wall_clock_deadline_exceeded",
                        await self._bounded_run_ending(state),
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
                # Per-iteration; the flag means "this run". THE SUMMARY IS RESET WITH IT,
                # because the two are one fact: a summary written before a verdict that came
                # back red describes a build that then failed, and leaving it standing would let
                # a later `declare_done` with an empty summary end the turn on stale praise for
                # work that had to be repaired.
                sandbox.done_requested = False
                sandbox.done_summary = ""
                # MARK "NOW" IN THE CONTAINER BEFORE THE AGENT RUNS. Everything the
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
                    # hard problem. It ends the same way the other two internal ceilings do:
                    # the tree is secured first, one sentence that names no bound,
                    # and the remainder from what was agreed. `end_reason` keeps which bound
                    # fired distinguishable for the person who can act on it.
                    raise _WriteEndedError(
                        "request_limit",
                        await self._bounded_run_ending(state),
                    ) from exc
                iteration += 1

                # THE MUTATION GUARD. A Write turn where the model only read files and
                # answered a question is an ordinary chat turn that happened to have write
                # tools available. Verifying it would spend 30s of the user's time and a
                # `tsc` run to confirm nothing changed, then nudge the model to keep going.
                #
                # …UNLESS the turn was asked to build. The same zero-mutation outcome means
                # opposite things on the two paths, and a bare `return` gives BOTH of them the
                # caller's `_finish(state, "completed")` — which announces a finished build over
                # a container still serving the golden template. A build that produced nothing
                # is a failure and has to end as one.
                #
                # …and on the build path the guard asks a NARROWER question, because
                # `done_requested` is not evidence of a mutation — it is the model's own claim to
                # have finished. A model that wrote nothing and simply declared itself done would
                # otherwise satisfy both halves of this disjunction and reach that same false
                # announcement by asking the accused for a character reference. On a turn that
                # EXPECTS a mutation, only a real write counts.
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

                if outcome.dev_ready:
                    # THE PROOF RIDES THE OBSERVATION, NEVER THE CLAIM — and this is the caller
                    # that made the difference. `claim_preview_frame` is a once-per-TURN
                    # one-shot with two consumers, this one and the 1s watcher, and it is a
                    # coin toss which arrives first: the watcher's `except SandboxError` arm
                    # sleeps a whole poll, and `verify` runs the moment a model step ends. Had
                    # the stamp sat inside the claim, every turn where THIS line won it would
                    # have served a working app while the registry still said "never served" —
                    # the pane stuck on "Getting your app ready" until the five-minute reaper,
                    # which is strictly worse than the defect the stamp exists to fix.
                    #
                    # `outcome.dev_ready` is the same fact the watcher reads: `/dev/status.ready`
                    # means a request to the app root actually succeeded. Stamping is
                    # once-only by the compare-and-set's own first-serve-wins rule, so both
                    # observers may say it and the second one costs a refused EVAL.
                    #
                    # ITS OWN READING, because `outcome.dev_ready` is a BOOLEAN distilled from
                    # `Readiness.READY` and cannot say what the root answered WITH. One extra
                    # supervisor call, taken between model steps rather than on the 1s poll, and
                    # a blip simply leaves the stamp to the watcher.
                    try:
                        verified = await sandbox.sandbox_client.dev_status(sandbox.handle)
                        page_is_up = verified.shows_a_page
                    except SandboxError:
                        page_is_up = False
                    await self._prove_it_serves(
                        state, sandbox, observer=_OBSERVER_TURN_VERIFY, shows_a_page=page_is_up
                    )
                    # AND THE FRAME RIDES THE READING THE STAMP RODE, for the reason the watcher's
                    # arm sets out at length. This caller had the same split as that one: the
                    # reading was taken, the stamp correctly refused a root that answered without
                    # a page — and then the claim was spent and the frame emitted anyway, so a
                    # verify that won the claim framed the 404 the REST poll was still, correctly,
                    # calling STARTING. Whichever of the two emitters gets there first, the
                    # browser is told to mount an iframe over a blank document.
                    #
                    # `page_is_up` IS TESTED FIRST SO THE CLAIM SURVIVES A REFUSAL. `and`
                    # short-circuits, and the order is the whole of it: written the other way
                    # round this would consume the turn's one-shot on a page-less reading and
                    # leave the 1s watcher — the only other emitter there is — with nothing to
                    # frame the app WITH once it finally has a page.
                    #
                    # A BLIP ON THAT READING WAITS RATHER THAN FRAMES. The `except SandboxError`
                    # above answers False, which here means "not yet, and not from here": one
                    # supervisor call failed between model steps, and the watcher's next poll is a
                    # second away.
                    if page_is_up and state.claim_preview_frame():
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
                    # THE HEADLINE NUMBER: how often the platform would have told a
                    # citizen their app was finished when it was not. Counted only on a POSITIVE
                    # verdict of "not finished" — an unanswerable one blocked nothing, it merely
                    # asked again, and folding the two together would make the number this
                    # counter exists to produce unreadable.
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
                    # claim the work is "saved": there is no auto-save — the
                    # changes sit in the workspace until the user's Save click.
                    if error is None:
                        raise _WriteEndedError(
                            "self_heal_budget_exhausted",
                            "Your app checks out — the assistant just ran out of steps "
                            "before wrapping up. Your changes are still in the workspace — "
                            "click Save to keep them, or send a message to continue.",
                        )
                    # THE HONEST ENDING. The sentence it replaces named a defect
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
                    # A CLIENT-CLASS REPORT IS AGENT INPUT, NOT NARRATIVE. The whole
                    # user-visible consequence of a browser-side crash is that the completion
                    # claim does not appear; the report itself was written by code inside the
                    # generated app, and the citizen is shown no developer surfaces. It still
                    # repairs — `build_repair_prompt` below is reached exactly as for any other
                    # source — it just does not narrate.
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
            # NO `temperature` — see the plan turn's note above. It is stripped by the
            # deployed model's profile and warns once per step; the effort and thinking mode
            # below are the knobs that actually reach this deployment.
            model_settings=AnthropicModelSettings(
                max_tokens=MAX_OUTPUT_TOKENS,
                anthropic_thinking=ADAPTIVE_THINKING,
                anthropic_effort=BUILD_EFFORT,
                anthropic_cache_instructions=CACHE_TTL,
                anthropic_cache_tool_definitions=CACHE_TTL,
                anthropic_cache=CACHE_TTL,
            ),
            # Deliberately NO `usage=`: this run's spend is folded per step below, and
            # passing the turn accumulator as well would bill every token twice.
        ) as run:
            # ANNOTATED, AND WALKED WITH `isinstance` RATHER THAN `Agent.is_end_node` — the
            # classmethod's `TypeIs` binds its type-var to `Unknown` on the bare class, so the
            # NEGATIVE branch this loop needs does not narrow. This is the only place the
            # graph is walked this way now; the deleted harness was the other one.
            node: AgentNode[ChatDeps, str] | End[FinalResult[str]] = run.next_node
            cut_short = False
            pending_answers: ModelRequest | None = None
            while not isinstance(node, End):
                if Agent.is_model_request_node(node):
                    # THE SESSION CLOSES BEFORE THE ENDING IS BUILT, which is why the `try`
                    # is on the outside now. `at_limit_ending` bundles and uploads the
                    # citizen's tree, and doing that inside the `async with` would pin a
                    # pooled connection for the duration of a container round trip — on the
                    # one path where every user who hits their cap in the same hour arrives
                    # at once. Nothing else about this block moved.
                    try:
                        async with session_factory() as gate_db:
                            await enforce_daily_limit(gate_db, state.user_id)
                    except DailyTokenLimitExceededError as exc:
                        # The request never fires. Graceful, not a crash: the work so
                        # far is real, and it is already durable rather than leaving it
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
                    # THE PLATFORM'S OWN BOUND, at the same seam and for the same reason.
                    # Inside the loop, before the request fires, where the run's
                    # accumulated spend is already known and nothing can skip it. The citizen
                    # can see the meter and the agent cannot, so this is the only party that
                    # can hold the line — and it is a number rather than an instruction
                    # precisely because an instruction is not a guardrail.
                    #
                    # ACROSS THE WHOLE TURN, not one `agent.iter`. A build turn makes several
                    # runs — the first attempt and each repair round — and a per-run bound
                    # would reset on every repair, which is the shape that ran away in the
                    # first place. `state.tokens_spent` carries the closed runs; `run.usage`
                    # carries the one in flight.
                    #
                    # THE SAME SECURING FUNCTION AS THE QUOTA ARM ABOVE, with its own sentence.
                    # Copy first, then say: this is the one path in the codebase where getting
                    # that ordering wrong loses a citizen's tree, so there is one function that
                    # does it and two sentences it can carry.
                    spent = state.tokens_spent + _run_spend(run.usage)
                    if spent >= RUN_TOKEN_BUDGET:
                        _log.info(
                            "run_token_budget_reached",
                            conversation_id=str(state.conversation_id),
                            turn_id=str(state.turn_id),
                            spent=spent,
                            budget=RUN_TOKEN_BUDGET,
                        )
                        raise _WriteEndedError(
                            "run_budget_reached",
                            await self._bounded_run_ending(state),
                        )
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
                    # AND THIS IS WHERE `declare_done` STOPS BUYING A ROUND-TRIP.
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
                    # The cursor's true origin. The node above just CLEANED the
                    # injected history — consecutive ModelRequests merged, the list shrunk —
                    # so the pre-clean `len(messages)` seeded outside the loop overshoots,
                    # and the first persist would skip the run's first ModelResponse: the
                    # row whose orphaned tool answers brick the conversation on every later
                    # turn. `new_message_index` is the same post-clean accounting
                    # `result.new_messages()` is built on — the one that keeps the Plan arm's
                    # single `chat_agent.run` immune. One shared expression, N readers.
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
            # FOLD THIS RUN'S SPEND ON THE WAY OUT. Inside the `async with`, so it runs
            # on the cut-short arm and the completed one alike — a repair round that stopped
            # early still spent what it spent, and a bound that forgot it would reset on every
            # repair, which is the runaway shape it exists to stop.
            state.tokens_spent += _run_spend(run.usage)
        return messages

    async def _record_write_step(
        self, state: _TurnState, usage: RequestUsage, session_factory: SessionFactory
    ) -> None:
        """Fold ONE model step's spend, in its own session with its own commit. Best-effort
        by design: a metering failure must not kill a build that is otherwise going fine, and
        the next step's `enforce_daily_limit` still reads whatever did land.

        THE PLATFORM'S THINKING IS TAKEN OFF THE ROW, not off the reader — see
        `_citizen_output_tokens`. Subtracting here is what makes the daily meter, the admin
        roster and the per-run ceiling agree without any of them knowing reasoning exists."""
        try:
            async with session_factory() as db:
                await record_usage(
                    db,
                    state.user_id,
                    input_tokens=usage.input_tokens,
                    output_tokens=_citizen_output_tokens(usage),
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
        """THE COMPLETION MESSAGE, WRITTEN FROM `done_summary`.

        `declare_done` stores its `summary`, so the sentence ending a build is a bounded field the
        prompt shapes — not the free-form paragraph of file paths and framework names the citizen
        used to read. BOTH FRAME AND ROW: `_push_text` puts it on the live stream for a mid-turn
        re-snapshot, the row is what reload projects, and persisting it closes the exchange
        (cutting the run at the tool leaves a return with no response). THE PERSIST MAY FAIL THE
        TURN: a reply not stored has not been given."""
        text = sandbox.done_summary.strip() or _BUILD_FINISHED_FALLBACK
        remainder = self._what_is_still_outstanding(
            state, workspace_touched=sandbox.workspace_touched
        )
        if remainder is not None:
            text = f"{text}{TEXT_BLOCK_SEPARATOR}{remainder}"
        # ITS OWN BLOCK, which `_push_text` now guarantees rather than this call site. An
        # earlier response in the same turn has very likely written prose of its own, and the
        # closing message running into that prose's last sentence is the defect the separator
        # constant exists to prevent — inside a composed message, which is what it still does
        # for the remainder sentence above.
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

    async def _bounded_run_ending(self, state: _TurnState) -> str:
        """THREE BOUNDS, ONE ENDING: durable first, then the sentence, then what is left.
        Request count, wall clock and spend can each end a run; which one fired is not
        something a citizen can act on differently, so it lives in `end_reason` and the logs,
        never in the copy. All three route through one securing function, `at_limit_ending`,
        so "your changes are still in the workspace" is verified rather than resting on a
        best-effort autosave that could fail silently. The daily quota is NOT one of these: it
        is the citizen's own budget, resets at midnight, and keeps its own sentence.
        `workspace_touched=False` truthfully means nothing was built."""
        message = (await at_limit_ending(state.sandbox, sentence=SPENT_ENOUGH_TEXT)).message
        remainder = self._what_is_still_outstanding(
            state,
            workspace_touched=state.sandbox is not None and state.sandbox.workspace_touched,
        )
        if remainder is None:
            return message
        return f"{message}{TEXT_BLOCK_SEPARATOR}{remainder}"

    def _what_is_still_outstanding(
        self, state: _TurnState, *, workspace_touched: bool
    ) -> str | None:
        """What was agreed and not built, from the platform's own record, or None.
        Not simply `agreed − marked`: the finished half is AGENT-SUPPLIED, and an agent that
        built everything but marked nothing is indistinguishable from one that built nothing.
        Keyed instead on `workspace_touched`, the turn's only platform-held evidence anything
        was built: marks landed → `agreed − marked`; no marks and nothing touched → the whole
        agreed list; no marks but work landed → say we could not tell, naming nothing
        outstanding. No agreement means no sentence, and only `workspace_touched` is read off
        the run — never the sandbox session itself."""
        if not state.agreed_pieces:
            return None
        if not state.finished_pieces:
            if workspace_touched:
                return CANNOT_TELL_WHAT_REMAINS_TEXT
            return REMAINDER_TEXT.format(pieces=", ".join(state.agreed_pieces))
        outstanding = [
            piece for piece in state.agreed_pieces if piece not in state.finished_pieces
        ]
        if not outstanding:
            return None
        return REMAINDER_TEXT.format(pieces=", ".join(outstanding))

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
        """What this app's workspace is doing RIGHT NOW, as a private note for the model. THE CHEAP
        HALF OF THE HEALTH VERDICT: runs on every turn in both chat kinds, so it must not cost what
        `verify` costs — a bounded readiness poll plus one exec, no `tsc`. "STILL STARTING UP"
        reports as "COULD NOT TELL", never "DOWN", since a note composed inside `dev_start`'s 5-7s
        compile window would otherwise call every cold turn's app dead. Unlike the verdict, the
        baseline check also runs for a brand-new project: a false positive there is costly, while
        the note just tells the model what the user is looking at, true even for an unbuilt app.
        NEVER RAISES — a failure's value is not knowing."""
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
        """The verify spinner. Synthetic and never persisted — it is a progress affordance for a
        30s wait, not part of the record, and it is correct for it to vanish on reload.

        THREE FINISHED ARMS, not two, and the third is why this takes the verdict rather than a
        bool. "Not working yet" over a check that could not be REACHED tells the citizen their
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
        watcher's. Warming here, at the emit rather than at discovery, cannot race
        `_watch_preview`'s own 1s poll, and `claim_preview_frame` upstream means it fires once
        per turn. The warm call gates nothing — it cannot raise or veto the frame — and the
        `finally` covers its cost: without it, a cancellation mid-warm would leave the frame
        permanently claimed and never emitted, since the one-shot guard means no later poll
        re-claims it. `_emit` is fully SYNCHRONOUS, no `await` anywhere, so terminal ordering
        holds even from a cancelled unwind."""
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
        covers its frame with. Rides the preview watcher rather than owning a loop: it already
        polls once a second for the whole turn, the cadence "appears and clears within
        seconds" needs, so a second loop would double the timers and cancellation paths for
        nothing. Emitted on change, since the ring is sized for narrative and one frame per
        poll would be hundreds per build. `compile_state` NEVER RAISES, so nothing is caught
        here — an exception on this path would kill the watcher that also owns crash
        detection."""
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

    async def _prove_it_serves(
        self,
        state: _TurnState,
        sandbox: SandboxSession,
        *,
        observer: _ServingObserver,
        shows_a_page: bool,
    ) -> None:
        """Record that something WATCHED this container's app answer a request — the one fact
        the preview pane is allowed to say "your app is running" off.

        THIS IS THE WHOLE CHANGE. `state == ready` on the registry hash says a container was
        SCHEDULED, and the platform reporting that as running is the defect the `serving_since`
        stamp exists to end. Both of this file's observers call this, and so does the relaunch
        path in the manager; the compare-and-set in `build_sessions/locks.py` is what makes
        "first serve wins" true across all of them, and what refuses a stamp aimed at a
        container the one-per-user slot no longer holds.

        ASKED ONCE PER TURN, NOT ONCE PER POLL — but latched on the ANSWER, never on a token
        somebody else can take. `state.serving_proof_settled` is set only by a call that got a
        definitive reply out of Redis, so a store failure keeps retrying on the next poll while
        a success (or a diagnosed refusal) stops asking.

        NOTHING HERE MAY RAISE, and `RedisError` is too narrow to hold that promise: `get_redis()`
        answers an unconfigured store with `RedisNotConfiguredError`, a `RuntimeError`. This runs
        inside the preview watcher, which also owns crash detection — a proof that could kill it
        would take the crash edge down with it, and the app would go dark with nothing watching."""
        if state.serving_proof_settled:
            return
        # A PAGE, NOT MERELY AN ANSWER — and this is the second half of the change, added after a
        # real build put a BLANK document on screen under a live-preview label. `/dev/status.ready`
        # is fail-open by the supervisor's own design (any response counts, 4xx included) so that a
        # compile error cannot wedge it False and mislead the model. A build spends its first
        # seconds answering 404s, genuinely "ready" with nothing to show, and stamping there framed
        # exactly that. NOT LATCHED: this is the ordinary case on the way up, so the next poll asks
        # again a second later rather than concluding anything.
        if not shows_a_page:
            return
        app_name = sandbox.handle.app_name
        when = datetime.now(UTC)
        try:
            redis = get_redis()
            stamped = await mark_serving(redis, state.user_id, app_name=app_name, when=when)
            # ONE EXTRA READ, AND ONLY ONCE PER TURN. On the way in it buys the registry's own
            # `created_at`, so `ms_since_container_created` is an answer rather than a
            # subtraction the operator has to do across two log lines — this is the eight-second
            # number the 2026-09-10 measurement had to be reconstructed from a screen recording
            # to get. On the refusal path it is the only way to tell the ordinary case (another
            # observer already proved this same container) from the dangerous one (the hash is
            # gone, ending, or names a different app), which `locks.mark_serving` cannot tell
            # apart on its own and says so.
            registry = await read_registry(redis, state.user_id)
            state.serving_proof_settled = True
            if stamped:
                _log.info(
                    APP_FIRST_SERVED_EVENT,
                    app_name=app_name,
                    serving_since=when.isoformat(),
                    ms_since_container_created=elapsed_ms(
                        an_instant_on_the_hash(registry, REGISTRY_FIELD_CREATED_AT), when
                    ),
                    observer=observer,
                    # WHETHER THIS TURN BROUGHT THE CONTAINER UP, read off the same field the
                    # container-start ratio's denominator is gated on. A turn that joined a
                    # container already serving is not a cold start, and calling it one would
                    # put a sub-second window beside a sixty-second one under the same name.
                    cold=state.started_a_container,
                )
                return
            if registry is not None and registry.get(REGISTRY_FIELD_APP_NAME) == app_name:
                if registry.get(REGISTRY_FIELD_SERVING_SINCE):
                    # ALREADY PROVEN, BY AN OBSERVER THAT GOT HERE FIRST — the verify path, an
                    # earlier turn, or the relaunch that started this container. Silent on
                    # purpose: `app_first_served` means FIRST, so a second line under that name
                    # would make `ms_since_container_created` meaningless.
                    return
                # The hash still names our container and the field is still the empty sentinel.
                # Two ways to get here and neither is the near-miss: the compare-and-set refused
                # on `state`, meaning the reaper has already marked this container `ending` and
                # it is going away — or the crash edge retracted a proof in the window between
                # the write and this read, and the next poll will re-stamp. Nothing to alarm on.
                return
            _log.warning(
                SERVING_PROOF_STAMP_REFUSED,
                expected_app=app_name,
                # A BOOL, NEVER THE NAME THAT WAS FOUND. The other name belongs to another of
                # this citizen's projects, and the id vocabulary in this log stays user-scoped.
                found_app_present=bool(registry and registry.get(REGISTRY_FIELD_APP_NAME)),
                observer=observer,
            )
        except Exception:
            # NOT LATCHED — the next poll asks again, because a store that would not answer has
            # told us nothing about whether the app served. Said ONCE per turn, for the reason
            # `said_it_could_not_check` is: a Redis outage under a 1s poll would write one
            # identical line per second per live turn.
            if state.said_the_proof_would_not_write:
                return
            state.said_the_proof_would_not_write = True
            _log.warning(
                SERVING_PROOF_WRITE_FAILED_EVENT,
                app_name=app_name,
                observer=observer,
                exc_info=True,
            )

    async def _retract_serving_proof(
        self,
        state: _TurnState,
        sandbox: SandboxSession,
        *,
        unanswered_polls: int,
        exit_code: int | None,
    ) -> None:
        """Take the serving proof back off a container that has stopped answering.

        RETRACTED TO THE EMPTY SENTINEL, NEVER DELETED — `redis/keys.py` carries the reading:
        an ABSENT `serving_since` is the pre-cutover grandfather arm and reads as PROVEN, so a
        delete here would turn a crashed app into a running one, which is the bug upside down.

        The read runs BEFORE the clear because the clear overwrites the very field
        `served_for_ms` is measured from. Silent when nothing was standing: the compare-and-set
        answering 0 is what stops a container that never served logging a loss that never
        happened. Nothing raises, for the reason `_prove_it_serves` gives."""
        app_name = sandbox.handle.app_name
        # RE-ARMED BEFORE THE WRITE IS EVEN ATTEMPTED, so a Redis blip on the way down cannot
        # leave the turn unable to re-prove a container that comes back. The compare-and-set is
        # the authority on whether a re-stamp is legitimate; costing it one refused EVAL is the
        # cheaper mistake.
        state.serving_proof_settled = False
        try:
            redis = get_redis()
            registry = await read_registry(redis, state.user_id)
            served_since = an_instant_on_the_hash(registry, REGISTRY_FIELD_SERVING_SINCE)
            if not await clear_serving(redis, state.user_id, app_name=app_name):
                return
            _log.warning(
                APP_SERVING_LOST_EVENT,
                app_name=app_name,
                unanswered_polls=unanswered_polls,
                # The supervisor's post-mortem of the dead child — 137 is the OOM killer, and
                # it is the most common answer there is. None while it is alive, when it never
                # started, or on an image that predates the field.
                exit_code=exit_code,
                served_for_ms=elapsed_ms(served_since, datetime.now(UTC)),
            )
        except Exception:
            if state.said_the_proof_would_not_write:
                return
            state.said_the_proof_would_not_write = True
            _log.warning(
                SERVING_PROOF_WRITE_FAILED_EVENT,
                app_name=app_name,
                observer=_OBSERVER_TURN_WATCHER,
                exc_info=True,
            )

    async def _watch_preview(self, state: _TurnState) -> None:
        """Poll the dev server so the preview appears the moment the app has a PAGE to show, and
        so a crash is REPORTED rather than left as a blank iframe.

        SERVABLE IS NOT THE SAME AS SHOWING SOMETHING, which is the distinction the whole ready
        arm below turns on: `/dev/status.ready` is fail-open and counts a 404, so a container
        framed on it alone puts a blank document under a live-preview label.

        Lives server-side because `/dev/status` is bearer-guarded — only the server holds the
        supervisor token. Every failure is swallowed; a watcher that raised would take the build
        down over a polling blip. The crash arm is DEBOUNCED over `CRASH_EDGE_CONSECUTIVE_POLLS` —
        this is the only watcher left, and any future second watcher must read that same
        constant: two watchers emitting the same signal to the same pane, debounced on only one
        of them, would make the crash edge depend on which code path built the app."""
        sandbox = state.sandbox
        if sandbox is None:
            return
        reconnecting = False
        # THE CRASH EDGE'S OTHER LATCH, kept apart from `reconnecting` on purpose. That flag is
        # gated on `state.preview_framed` — correct for the SSE frame, which must not announce a
        # reconnect for an iframe it never told the client to mount — but wrong for the serving
        # proof, which is a fact about the CONTAINER and outlives any one turn's framing. A turn
        # that attaches a container stamped by an earlier turn and watches it die never frames
        # anything, so a proof-clear riding `preview_framed` would leave a dead app reading
        # RUNNING. This one fires on the streak alone and re-arms when the app answers again.
        proof_retracted = False
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
                # THE PROOF GOES DOWN ON THE OBSERVATION, ABOVE THE CLAIM AND AHEAD OF THE
                # FRAME. Two reasons, in that order of weight.
                #
                # ABOVE THE CLAIM, because `claim_preview_frame` is a once-per-turn one-shot
                # the self-heal verify can take first — see the matching note at its other
                # caller. Stamping under `if first_serve or reconnecting:` meant that whenever
                # verify won, this block never ran and NOTHING EVER STAMPED. The
                # compare-and-set's own first-serve-wins rule supplies the once-only property
                # the claim was being borrowed for, so the claim goes back to meaning what its
                # name says: who emits the frame.
                #
                # AHEAD OF THE FRAME, which is the one place this file's own "bookkeeping goes
                # behind the thing it books" rule does NOT apply — and the counter below still
                # obeys it. The stamp is not bookkeeping; it is the fact the REST poll reads to
                # decide whether the pane may say "your app is running". Frame first and a poll
                # landing in between answers STARTING while the live stream says ready. The price
                # is one Lua EVAL before the citizen's preview, and one per second until it lands.
                #
                # ORDERING ALONE DOES NOT CLOSE THE SPLIT-BRAIN, and this paragraph used to be
                # written as though it did. It closes the RACE — poll and stream can no longer
                # disagree merely because they read at different instants. The other half is a
                # disagreement about the FACT: `_prove_it_serves` declines a root that answered
                # without a page, and the frame went out anyway on the very next line, so the poll
                # said STARTING while the stream framed a 404. The gate below is what closes that
                # one; sequencing the two writes could never have.
                await self._prove_it_serves(
                    state,
                    sandbox,
                    observer=_OBSERVER_TURN_WATCHER,
                    shows_a_page=status.shows_a_page,
                )
                proof_retracted = False
                # AND THE FRAME WAITS FOR A PAGE, WHICH `status.ready` IS NOT. The supervisor's
                # readiness is fail-open by its own design — ANY answer on the dev port counts,
                # 404 and 500 included, so that a compile error cannot wedge it False and mislead
                # the model — and the stamp above already declines a root that answered without a
                # page. Emitting the frame off the bare `ready` is what left this branch's two
                # emitters disagreeing about the same container: the REST poll read the missing
                # stamp and answered STARTING, while this stream told the browser to mount its
                # iframe. The browser wins that argument, and it framed the 404 — the blank white
                # pane measured on 2026-09-10, arriving over SSE instead of over the poll, on a
                # container whose root was still 404ing because the agent had not written
                # `app/page.tsx` yet.
                #
                # THE GATE IS AROUND THE CLAIM, NOT AROUND THE EMIT, and that is the difference
                # between a fix and a worse defect. `claim_preview_frame` is a once-per-TURN
                # one-shot: spend it on a page-less reading and the real first serve — a second
                # later, in this same loop — has nothing left to emit with, so the pane never
                # frames at all. Refusing the claim costs nothing, because the next poll asks
                # again. `reconnecting` is inside for the identical reason: it is the re-frame a
                # recovered client is still waiting for, and clearing it here on a root with no
                # page would drop that client's reconnect just as permanently.
                #
                # WHAT IS ABOUT THE CONTAINER STAYS ON `ready`: the unanswered-poll streak above,
                # the crash edge's re-arm, and the compile poll before it. "Is anything answering
                # the dev port" and "does the app have a page yet" are different questions, and a
                # dev server 404ing its way through the first seconds of a build is alive — read
                # that as death and the streak would retract a good proof and report a crash that
                # did not happen.
                #
                # A SUPERVISOR THAT CANNOT SAY STILL FRAMES: `shows_a_page` reads an absent
                # `root_status` as today's behaviour on purpose, so the pre-`root_status` fleet
                # keeps its preview instead of being locked out of it by a field it never sends.
                if status.shows_a_page:
                    first_serve = state.claim_preview_frame()
                    if first_serve or reconnecting:
                        # First serve, or recovered after a crash — either way the client needs
                        # the url to (re)mount its iframe on.
                        await self._emit_preview_ready(state, sandbox.handle.preview_url)
                    if first_serve and state.started_a_container:
                        # THE CONTAINER-START SUCCESS RATIO'S NUMERATOR, and this is the only
                        # place the turn learns the answer.
                        #
                        # IT RIDES THE PAGE GATE ABOVE rather than the bare `ready`, because
                        # `relaunch_preview` refuses its own `ready` for a root that answered
                        # without a page — so both writers count a start that reached a SERVING
                        # PAGE, and neither can quietly start counting something else.
                        #
                        # AFTER THE FRAME, NEVER BEFORE. This is an await on the one code path
                        # between the app becoming servable and the citizen seeing it, so counting
                        # first would delay their preview by a database round trip to record that
                        # their preview arrived. Bookkeeping goes behind the thing it books.
                        #
                        # NOT `session.handle.ready`, which looks like this fact and is not one:
                        # on both birth arms it is hard-coded False, and on the attach arm it is a
                        # `/dev/status` snapshot taken BEFORE this turn's own `dev_start`. Reading
                        # it at the attach seam would report a near-zero success rate and measure
                        # the container's birth rather than the app's.
                        #
                        # GATED ON THE CLAIM, NOT ON `_emit_preview_ready`. Two emitters call that
                        # method — this watcher and the self-heal verify — and this watcher calls
                        # it again on every crash RECOVERY (the `or reconnecting` above). The
                        # claim is the synchronous once-per-turn one-shot, so counting on it is
                        # once by construction. And gated on `started_a_container`, or every turn
                        # that joined a container already serving would land in the numerator
                        # without a matching denominator row.
                        await count(
                            HarnessCounter.APP_START_REACHED_RUNNING, app_id=sandbox.app_id
                        )
                    reconnecting = False
            else:
                # Counted on the PAIR (nothing answering AND no child alive), not on the framed/
                # reconnecting bookkeeping, so the streak means exactly what its name says. A
                # live child that is merely still compiling resets it immediately.
                unanswered_polls = unanswered_polls + 1 if not status.running else 0
                if (
                    not proof_retracted
                    and not status.running
                    and unanswered_polls >= CRASH_EDGE_CONSECUTIVE_POLLS
                ):
                    # THE APP HAD SERVED AND HAS STOPPED, so the proof comes off the registry
                    # and the pane falls back to "getting your app ready" instead of framing
                    # nginx's app-gone page.
                    #
                    # THE SAME DEBOUNCE THE FRAME USES, AND NEVER ANYTHING WEAKER. `ready=False`
                    # alone is not a death — the supervisor's readiness answer lapses benignly
                    # while a slow root route renders — and a transport error is not a death
                    # either, which is why the `except SandboxError` arm above still just
                    # continues. `not status.running` is spelled out even though the streak
                    # already implies it (a live child resets the count to zero), so that
                    # nobody later relaxes the streak into a readiness-only test and turns a
                    # slow render into a retracted proof.
                    #
                    # ONLY WHILE A TURN IS STREAMING. This watcher is created at the attach and
                    # cancelled at the terminal, so the commoner shape — the build finishes, the
                    # turn ends, the citizen keeps using the app, the dev server dies — is not
                    # reachable from here at all. The out-of-turn death is the reconciler's.
                    proof_retracted = True
                    await self._retract_serving_proof(
                        state,
                        sandbox,
                        unanswered_polls=unanswered_polls,
                        exit_code=status.exit_code,
                    )
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

        The watcher that reports `building` is cancelled at the terminal, so a turn that ends
        between "compiling" and "compiled" would otherwise strand the preview with no remaining
        producer. One poll usually settles it to `clean` or `failed`. Runs ONLY on `building`,
        so the common path pays nothing; if it is STILL building afterwards the cover stays up
        — honest, and the next turn resolves it."""
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
        """Publishes that a build is live in this user's container. The heartbeat seeds once a turn
        on a 90s TTL; past that only `sweep_all`'s in-process `live_users` set keeps the sweep off
        it — empty in every other process — so nothing that can destroy a container may run outside
        the API process until this exists. Wall clock, never `time.monotonic()`: cross-process
        readable, and its TTL expires an abandoned lease rather than pinning the container. ALSO
        RENEWS THE LOCK AND HEARTBEAT, their only clock: `on_progress` renews both per frame, so a
        tool call past the TTL silently drops the lock. Best-effort, never silent: both failures
        logged, lock arm caught apart from lease so one store error costs only its own renewal."""
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
            write_session = state.write_session
            if write_session is not None:
                # GUARDED FOR THE REASON THE LEASE IS. The lock and the heartbeat are keyed by
                # USER, not by turn, so a turn that took no container of its own would renew —
                # and vouch for the liveness of — whatever this user's slot is actually holding
                # somewhere else. Read fresh each tick rather than closed over: the attach that
                # sets it runs before this task starts today, and re-reading is what keeps that
                # an implementation detail rather than a precondition.
                try:
                    redis = get_redis()
                    if not await renew_lock(redis, state.user_id, write_session.lock_token):
                        # The lock lapsed under an active build (reaped / expired / taken), so
                        # the slot may now be double-allocated. Best-effort still — ending the
                        # turn here would destroy the work the lock was protecting — but never
                        # invisible. Same sentence `on_progress` logs, for one alert.
                        _log.warning(
                            LOCK_LOST_EVENT,
                            session_id=str(write_session.session_id),
                            user_id=str(state.user_id),
                            conversation_id=str(state.conversation_id),
                            turn_id=str(state.turn_id),
                        )
                    # Written even when the renewal above said no: the heartbeat answers a
                    # different question (is anyone working in there?) and the reaper reads it
                    # on its own, so withholding it would add an idle-teardown to a lost lock.
                    await write_heartbeat(redis, state.user_id)
                except Exception:
                    _log.exception(
                        LOCK_RENEW_FAILED_EVENT,
                        session_id=str(write_session.session_id),
                        conversation_id=str(state.conversation_id),
                        turn_id=str(state.turn_id),
                    )
            await asyncio.sleep(LIVENESS_LEASE_RENEW_CADENCE_SECONDS)

    async def _stop_liveness_lease(self, state: _TurnState) -> None:
        """Stop renewing and drop the lease. Idempotent — the turn's `finally` is reached by five
        different terminal arms, and a second call finds nothing to do.

        RELEASED AFTER THE SANDBOX FINALIZE, unlike `_stop_preview_watcher` (which dies first
        because a late frame is lost): releasing the lease early opens a reap window over
        `finish_turn_sandbox`, which can outlive the 90-second heartbeat TTL that is otherwise the
        container's only cover. Failures are swallowed — a Redis blip must not wedge the
        conversation guard shut, and the cost of not landing is bounded by the lease's own TTL."""
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

        NOT THE PLAN: `meta` is JSONB a redaction pass walks, and putting up to 64,000 characters
        of plan here would be a third durable copy of a string the tool call's own `args` already
        holds authoritatively — one that could silently disagree with it. NOT A SNAPSHOT HEAD
        either: the "app moved underneath the plan" warning that would buy is paid for better in
        the Build chat's own prompt, which works for a plan built weeks later."""
        if deferred is None:
            return None
        return {"kind": PENDING_META_KIND, "toolCallId": deferred.tool_call_id}

    # NOTHING SYNTHESIZES A CARD ANY MORE. `_synthesize_options` used to fabricate one — a
    # hidden `plan_options_pending` system row with `synthesized: True`. `plan_options._scan`
    # still READS the synthesized shape, and must: rows written by the retired writer are in
    # the database, and revision 0035 resolved their cards rather than deleting them.

    def _emit_plan_status(self, state: _TurnState, tool_call_id: str) -> None:
        """The "writing up the plan" line, held in `state.steps` so a client that subscribes mid-
        argument sees it in the catch-up snapshot like any other in-flight step.

        NO DURABLE COUNTERPART, deliberately: a status that exists only while streaming has nothing
        to say on a reloaded transcript — same reasoning as the turn's opening acknowledgement.
        REMEMBERED ON THE STATE so the terminal can withdraw it: every other `state.steps` entry is
        a real tool step with an authoritative row, so `_finish` cannot find this one by looking,
        and a Plan turn failing mid-argument would otherwise leave it spinning."""
        item = StepItem(
            seq=0,  # transient: no row, so no row seq
            tool=PLAN_OPTIONS_TOOL,
            label=WRITING_UP_THE_PLAN_LABEL,
            state="pending",
            hidden=False,
        )
        state.plan_status_tool_call_id = tool_call_id
        self._open_step(state, tool_call_id, item)

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

        PLAN ONLY — passed at the single `chat_agent.run` the Plan arm makes; Build drives its
        own node loop (`_run_write_once`) and calls `_on_event` directly. pydantic-ai invokes
        this once per NODE, and a response's text and tool calls arrive from two different
        nodes, text first — the order the citizen reads them in, so each event goes straight
        out as it lands."""

        async def handle(
            _ctx: RunContext[ChatDeps], events: AsyncIterable[AgentStreamEvent]
        ) -> None:
            async for event in events:
                self._on_event(state, event)

        return handle

    def _on_event(self, state: _TurnState, event: AgentStreamEvent) -> None:
        if isinstance(event, PartStartEvent):
            if isinstance(event.part, ThinkingPart):
                # THE FLAG, AND NOTHING ELSE. `event.part.content` is the reasoning text and is
                # deliberately not read here or anywhere on the way to the browser: the whole
                # of what reasoning is allowed to become is a status line saying the agent is
                # working. The blocks themselves go to the payload, because the provider
                # rejects the NEXT turn's tool call if its reasoning block is missing.
                self._set_working(state, True)
                return
            if isinstance(event.part, TextPart) and event.part.content:
                self._push_text(state, event.part.content, new_block=True)
            elif (
                isinstance(event.part, ToolCallPart) and event.part.tool_name == PLAN_OPTIONS_TOOL
            ):
                # A STATUS THE MOMENT THE BLOCK OPENS, and only for this one tool.
                #
                # The name is available before any argument is: the provider's
                # `content_block_start` for a tool use carries `name` with an empty `input`,
                # which pydantic-ai surfaces here as a `ToolCallPart` whose `tool_name` is
                # already set. That matters because the plan now rides the ARGUMENT, not the
                # response text: thousands of tokens stream between this event and the call
                # resolving, and none of them are prose, so without this the screen shows
                # nothing new for the whole of it.
                #
                # NOT WIDENED TO EVERY TOOL, deliberately: the others resolve fast and already
                # emit at `FunctionToolCallEvent`, so emitting at both events would double
                # every step row in the transcript.
                self._emit_plan_status(state, event.part.tool_call_id)
        elif isinstance(event, PartDeltaEvent):
            if isinstance(event.delta, ThinkingPartDelta):
                # A reasoning delta says the model is STILL thinking and says nothing else. The
                # delta's own content is never read, for the same reason its part's is not.
                self._set_working(state, True)
                return
            if isinstance(event.delta, TextPartDelta) and event.delta.content_delta:
                self._push_text(state, event.delta.content_delta, new_block=False)
        elif isinstance(event, FunctionToolCallEvent):
            # NOTHING IS DISCARDED HERE, and that is the change. This call used to delete
            # every paragraph the response had opened with, on the rule that prose beside a
            # tool call is narration. The prose was stored all along and suppressed on the way
            # out; now it reaches the citizen in the place it was written, and the step this
            # event opens takes the position after it.
            if event.part.tool_name == PLAN_OPTIONS_TOOL:
                # The plan FIRST, then the card beneath it — the order the citizen reads, and
                # the same order the reload projection produces from this one stored call.
                # A call carrying no usable plan pushes nothing and offers nothing; the turn's
                # own closing line says so, once, from the persist path below.
                plan = plan_from_call(event.part)
                if plan is not None:
                    self._push_text(state, plan)
                    # The options card, not a step: the call defers (the user's click is the
                    # result), so there is no 'finished' counterpart to wait for. It REPLACES
                    # the status on the same tool_call_id rather than stacking beside it.
                    self._emit_plan_options(state, event.part.tool_call_id)
                self._retract_step(state, event.part.tool_call_id)
                return
            if event.part.tool_name == TELL_THE_USER_TOOL:
                # THE WORDS, AND NOT A STEP. Rendered here at the CALL event rather than at
                # the result, and that placement is the whole guarantee: tool bodies run
                # concurrently and their results arrive in completion order, while a reloaded
                # transcript renders in part order — so a response that spoke and also read a
                # file would put the two in one order live and the other order on reload.
                # Call events arrive in part order, which is the order the projection uses.
                #
                # `update_from_args` is the same function the projection calls, so an update
                # the tool body will refuse pushes nothing here either, without this site
                # knowing what the bound is. Nothing is put in `state.steps`: there is no
                # 'finished' frame to wait for, and a step row saying the agent decided to
                # speak is the row this channel exists to avoid.
                spoken = update_from_args(event.part.args)
                if spoken:
                    self._push_text(state, spoken)
                # THE MARK, RECORDED FROM THE SAME CALL that carried the words, and
                # CHECKED HERE RATHER THAN TRUSTED FROM THE BODY.
                #
                # A call event is emitted while pydantic-ai validates the batch — every
                # `FunctionToolCallEvent` is yielded by `_validate_function_calls`, and only
                # then does `_call_tools` run a body — so `tell_the_user`'s refusal of a piece
                # nobody agreed to has NOT happened at this point. An earlier version of this
                # site assumed it had.
                #
                # THE COST OF BEING WRONG IS THE TRI-STATE, not an untidy set. The remainder picks
                # its honest "I could not tell" arm on `not finished_pieces`, so one hallucinated
                # mark makes the set truthy and turns "could not tell" into "still to do:
                # <everything agreed>" — the platform asserting in its own voice that finished work
                # is outstanding, which is the exact false fact this tri-state exists to prevent,
                # arriving through the one door that skipped the check.
                #
                # So it validates for itself, like the proposal branch below: what survives is
                # a subset of what was agreed, whatever the model sent and whenever the body
                # runs. The body still refuses too — that is what teaches the model — but no
                # reader downstream depends on the ordering between the two.
                marked = finished_from_args(event.part.args)
                if marked is not None and marked in state.agreed_pieces:
                    state.finished_pieces.add(marked)
                return
            if event.part.tool_name == PROPOSE_SLICE_TOOL:
                # THE PROPOSAL, rendered exactly like a spoken line — and the arguments are also
                # the AGREEMENT. Recording it here rather than re-reading the rows later keeps
                # one rule ("the latest honourable proposal wins") and one parser between the
                # live path and every reader: `agreed_slice` seeded this list from history at
                # turn start, and this replaces it the moment a new proposal is made.
                proposal = proposal_from_args(event.part.args)
                if proposal:
                    self._push_text(state, proposal)
                    state.agreed_pieces = agreed_slice([ModelResponse(parts=[event.part])])
                    state.finished_pieces.clear()
                return
            item = self._step_item(state, event.part.tool_name, event.part.args_as_json_str())
            self._open_step(state, event.part.tool_call_id, item)
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
            # THE GAP AFTER THE LAST TOOL RETURNS, which is where the screen used to go quiet.
            #
            # Every tool has come back and the model has the floor again: the next thing that
            # happens is a request nobody can see, lasting as long as it lasts. The status used to
            # be raised only by a THINKING block, so a response that opened with a tool call, or
            # one the provider answered without any reasoning at all, left the transcript showing
            # the last finished step and nothing else — indistinguishable from a hang, and reported
            # as exactly that ("I don't know if the chat is currently thinking or not").
            #
            # GUARDED ON NOTHING STILL PENDING, because tools can overlap: raising it while another
            # call is still out would claim the model is waiting on itself when it is waiting on a
            # container. Every reset already exists and needs no counterpart here — `_open_step`
            # takes it down when the next call opens, `_push_text` when the first word arrives, and
            # `_finish` at the terminal. Edge-triggered, so this costs at most one frame per model
            # request.
            if not any(item.state == "pending" for item in state.steps.values()):
                self._set_working(state, True)

    # -- the long-operation status line --------------------------------------

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
        THE HARNESS SAYS THIS, NOT THE AGENT. The composite tool operations that removed
        per-step narration are precisely the ones that leave these gaps, so a citizen
        watching a three-minute install would otherwise watch a row frozen for minutes. It
        re-emits the SAME step (`tool_call_id`, still `phase="started"`) with a restated
        label, replacing the row in place rather than stacking a second one; the base label
        stays untouched in `state.steps` so re-deriving stays byte-identical across
        refreshes. Under the threshold nothing is emitted: a 300ms flicker is a glitch."""
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

    def _retire_acknowledgement(self, state: _TurnState) -> None:
        """Take the opening acknowledgement off the screen, on the wire and in the snapshot.

        Clearing `state.acknowledgement` alone only hides it from a LATE subscriber's catch-up
        snapshot — a client already connected got it as a live step frame and has no way to learn
        it is over, leaving "Getting started on that…" under a build that finished long ago. So it
        RIDES `hidden`: re-emitting the same id hidden replaces the row in place on a path both
        readers already filter, cheaper than a new frame kind. IDEMPOTENT, so the plan-status arm
        and the first real step cannot both retract it and leave two frames behind."""
        ack = state.acknowledgement
        if ack is None:
            return
        state.acknowledgement = None
        retired = ack.model_copy(update={"hidden": True})
        self._emit(
            state,
            lambda seq: StepFrame(
                seq=seq, tool_call_id=ACK_TOOL_CALL_ID, phase="finished", item=retired
            ),
        )

    def _retract_step(self, state: _TurnState, tool_call_id: str) -> None:
        """Withdraw a step from the snapshot AND from the feed a tab is already watching.

        `drop_step` alone only covers a LATE subscriber's catch-up snapshot; a client already
        connected got the started frame and has no way to learn the step is over. Same defect as
        the acknowledgement's, same fix: re-emit the id `finished` and hidden. NOT USED BY THE
        STEPS-CAP EVICTION — an evicted step is a REAL step whose row is authoritative, and
        bounding memory must not tell a watching tab the work never happened. IDEMPOTENT: the plan-
        status bookkeeping is cleared here too."""
        item = state.steps.get(tool_call_id)
        state.drop_step(tool_call_id)
        if state.plan_status_tool_call_id == tool_call_id:
            state.plan_status_tool_call_id = None
        if item is None:
            return
        self._emit(
            state,
            lambda seq: StepFrame(
                seq=seq,
                tool_call_id=tool_call_id,
                phase="finished",
                item=item.model_copy(update={"hidden": True}),
            ),
        )

    def _set_working(self, state: _TurnState, working: bool) -> None:
        """Turn the working status on or off, and frame the CHANGE only.

        "WORKING" IS THE MODEL HAVING THE FLOOR, not a reasoning block streaming — see
        `WorkingFrame`. Three sites raise it — a thinking part opening, a thinking delta arriving,
        and the last outstanding tool returning — and three lower it (`_open_step`, `_push_text`,
        `_finish`). The raisers are the windows nothing else can narrate; the lowerers are the
        moments something real appears to replace it.

        WHAT IS NOT A RAISER, because an earlier draft of this docstring claimed it was: a tool
        call's ARGUMENTS opening. No such call site exists. The window is real — a turn whose very
        first act is a tool call, with no reasoning before it, streams that call's arguments with
        the flag still down — but it is narrated by the acknowledgement row that is already open at
        that point, not by this flag. Adding a raiser there would double every step row, which is
        why the neighbouring `PLAN_OPTIONS_TOOL` case deliberately does not.

        THE FLAG RIDES THE TURN, NOT A MESSAGE — an earlier draft got this seam wrong. The browser
        synthesises a CONTENT-FREE reasoning part at the TAIL of the streaming message while this
        is true; pinning it to the head instead put "Working on your app" above paragraphs already
        read, making the turn jump down the screen. Content-free is the point: no text, so "status
        only" is structural. EDGE-TRIGGERED, because a frame per reasoning delta would flood the
        ring."""
        if state.working == working:
            return
        state.working = working
        self._emit(state, lambda seq: WorkingFrame(seq=seq, working=working))

    def _open_step(self, state: _TurnState, tool_call_id: str, item: StepItem) -> None:
        """Record a step, its POSITION in the turn, and announce it on the wire.

        A step arrives twice — started, then finished — and the second must replace the first in
        place, not move it, or a citizen watching would see the turn reorder: the map is written
        every time, the ref appended once. RETIRING THE ACK LIVES HERE rather than at each caller,
        so no caller can forget it and leave a row that never resolves under a group that never
        seals; `_push_text` enforces it the same way. The started frame goes out from here too, and
        `_emit` being synchronous keeps retraction, working flag, then step in order."""
        self._retire_acknowledgement(state)
        # A step means the model has stopped thinking and started doing.
        self._set_working(state, False)
        if tool_call_id not in state.steps:
            state.parts.append(_StepRef(tool_call_id))
        state.steps[tool_call_id] = item
        self._emit(
            state,
            lambda seq: StepFrame(seq=seq, tool_call_id=tool_call_id, phase="started", item=item),
        )

    def _push_text(self, state: _TurnState, text: str, *, new_block: bool = True) -> None:
        """Prose to the citizen: onto the turn's ordered parts AND onto the wire, at once.

        THE ONE TEXT SINK, holding nothing itself any more: prose beside a tool call used to be
        buffered and discarded as mere narration — which threw away real explanation, the opposite
        of this product's voice. `new_block` keeps the live feed matching a reloaded one: reload
        emits ONE item per stored `TextPart`, so a `PartStartEvent` opens a block here and every
        delta extends it, splitting where a STEP would force a reload to split. UNGATED by default
        — platform-rendered blocks are always their own block too."""
        # THE ACK IS OVER THE MOMENT THERE ARE WORDS ON SCREEN. A turn that answers in prose
        # and calls nothing would otherwise keep the opening row under an answer the citizen is
        # already reading — and the board's rule for that turn is that it shows nothing at all
        # beyond the answer.
        self._retire_acknowledgement(state)
        # Words on screen mean the thinking is over — see `_set_working`.
        self._set_working(state, False)
        newest = state.parts[-1] if state.parts else None
        if new_block or not isinstance(newest, _TextBlock):
            state.parts.append(_TextBlock(text))
            opened = True
        else:
            newest.text += text
            opened = False
        self._emit(state, lambda seq: TextDeltaFrame(seq=seq, text=text, new_block=opened))

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
        # NOTHING IS HIDDEN WHEN SOMETHING WENT WRONG, whatever class it belongs to — the same
        # rule the reload projection applies, restated here because a live feed and a reloaded
        # one that disagree about which rows exist is the failure this whole seam is arranged
        # to prevent. A housekeeping command is plumbing while it works and the whole story the
        # moment it does not, and the group's problem count has to name a row the citizen can
        # actually see.
        resolved = pending.model_copy(
            update={"state": "failed", "hidden": False} if failed else {"state": "ok"}
        )
        state.steps[event.tool_call_id] = resolved
        if len(state.steps) > _STEPS_CAP:
            # Drop the oldest RESOLVED step — snapshot material only; rows are authoritative.
            # THROUGH `drop_step`, so its POSITION goes with it: a ref left pointing at an
            # evicted step is skipped by the snapshot but never reclaimed, and a build that
            # evicts for minutes would accumulate one dead entry per step it ever ran.
            #
            # "RESOLVED" IS NOW ENFORCED RATHER THAN ASSERTED. This read `next(iter(state.steps))`,
            # which is the oldest INSERTED key whatever its state — so a slow call that overlapped
            # a cap's worth of quick ones was itself the victim. Two things broke: the slow call's
            # own finished frame was lost (the lookup at the top of this method returns None once
            # the id is gone, leaving it pending on screen forever), and the working flag's raise
            # guard — which asks this very dict whether anything is still out — was told no while a
            # container was mid-command, claiming the model had the floor when it did not.
            #
            # Evicting NOTHING when every step is somehow still pending is the deliberate other
            # half: the overshoot is bounded by however many calls are genuinely outstanding at
            # once, which is small, and a bounded overshoot is a better failure than a lost frame
            # and a lying status line.
            oldest_settled = next(
                (sid for sid, item in state.steps.items() if item.state != "pending"), None
            )
            if oldest_settled is not None:
                state.drop_step(oldest_settled)
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
        # The status-line narrators are silenced BEFORE the terminal frame, synchronously.
        # A narrator is always parked on a sleep, so cancelling here means the CancelledError
        # lands at that sleep and it can never reach `_emit` again: no status row after the
        # terminal, with no await in between for one to slip through. The `finally` then awaits
        # them (`_drain_long_operations`) — this only makes the ordering unloseable.
        for task in state.long_operation_tasks.values():
            task.cancel()
        # NO GROUP IS LEFT OPEN BY THE PLATFORM'S OWN ROW. A turn that ran no tools and wrote
        # nothing — one that failed at the first request, or was stopped before it started —
        # never reached either of the sites that retire the acknowledgement, so it would end
        # with "Getting started on that…" still spinning under a turn that is over. Idempotent,
        # so the ordinary turn that already retired it emits nothing here.
        self._retire_acknowledgement(state)
        # NOR BY THE PLAN'S OWN STATUS, for the same reason. "Writing up the plan…" opens the
        # moment the tool's block does and is withdrawn when the argument lands, but a turn that
        # fails or is stopped in the thousands of tokens between those two moments never reaches
        # the offer arm — so a watching tab kept a spinning row under a turn that had ended, and
        # only a reload cleared it. Nothing else in `state.steps` is retracted here: every other
        # entry is a real tool step whose row is authoritative, and telling a tab that work it
        # watched happen never happened is the opposite defect.
        if state.plan_status_tool_call_id is not None:
            self._retract_step(state, state.plan_status_tool_call_id)
        # THE STATUS CANNOT OUTLIVE THE TURN. A turn that ended while the last thing it did was
        # think — a failure mid-reasoning, a stop — would otherwise leave "Working on your app"
        # under a turn that is over.
        self._set_working(state, False)
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

        BOTH KINDS, UNCONDITIONALLY — nothing here reads `state.kind`, since a weaker path for one
        of them is the kind nobody notices until a citizen is looking at a frozen transcript.
        NOTHING IS WRITTEN FOR A TURN THAT DID NOT REACH A TERMINAL: the absence of this row IS the
        ended-unknown signal. BEST-EFFORT: a raise here, after the reply is durable and the
        subscriber told, would take down the release and watcher teardown below it for a row whose
        only job is a LATER reload. Logged and swallowed."""
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
                        # WHAT BROKE IT, on a turn that reached the generic ending or the
                        # model-service one (a stop that landed while that ending secured the tree
                        # included) — class names, status, provider type and code location only
                        # (`_error_signature`). Every other ending carries none, so a completed
                        # turn's row is byte-identical to what it always was. The projection reads
                        # named keys and never forwards this to a browser.
                        **(
                            {"error": state.error_signature}
                            if state.error_signature is not None
                            else {}
                        ),
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
        are resolved by the ROUTE before the stream commits.

        KNOWN AND OPEN: a mid-stream gap re-snapshot carries the in-memory tail only. It is
        the one path that never re-reads the database, so a step already evicted by the ring
        cap cannot be recovered on it — a client that reconnects after a long gap sees the
        tail and not the evicted middle."""
        if state is None:
            return SnapshotFrame(seq=0, turn_id=None, turn_status="idle")
        # THE ACK GOES FIRST, and this is the only place it can. It is emitted at `seq == 1`
        # before any client can subscribe, and the route sets `last_sent = snapshot.seq`, so
        # the ring frame is already behind every subscriber's cursor. Same reasoning the
        # preview/compile/error_message fields are carried for — a frame that fired before the
        # client connected lives only in the ring, and the snapshot is what makes a
        # subscription self-sufficient.
        parts: list[TurnPart] = []
        if state.acknowledgement is not None:
            parts.append(TurnStepPart(tool_call_id=ACK_TOOL_CALL_ID, item=state.acknowledgement))
        for part in state.parts:
            if isinstance(part, _TextBlock):
                parts.append(TurnTextPart(text=part.text))
                continue
            # EVERY in-flight step, hidden ones included. `hidden` is a RENDER hint (the live
            # tail and the reload projection both ship hidden steps); making it a payload
            # filter HERE meant a client that reconnected mid-turn silently lost steps the
            # other two paths kept. A ref whose step has been withdrawn resolves to nothing
            # and is skipped — see `_TurnState.drop_step`.
            item = state.steps.get(part.tool_call_id)
            if item is not None:
                parts.append(TurnStepPart(tool_call_id=part.tool_call_id, item=item))
        return SnapshotFrame(
            seq=state.seq,
            turn_id=str(state.turn_id),
            turn_status=state.status,
            items=items or [],
            # PROSE AND STEPS IN ONE ORDERED LIST, because a reattaching citizen has to read
            # the same turn as one who never left. The snapshot used to carry a flat
            # `text_so_far` string beside an unordered step map, which cannot express a turn
            # that wrote, acted, and wrote again — and the moment prose stopped being held
            # beside a tool call, that became every interesting turn.
            parts=parts,
            working=state.working,
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
