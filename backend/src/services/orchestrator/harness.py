"""`run_build` — the frozen C7 entry point, implemented as a multi-run self-heal state machine
(KD-1 / KD-3 / KD-11 / KD-12 / KD-13).

The build is a SEQUENCE of `agent.iter` runs. Within each run the harness drives the node loop
manually and meters EVERY model step: `enforce_daily_limit` strictly before the request fires,
`record_usage` strictly after off `CallToolsNode.model_response.usage`, in BRAIN's own per-step
billing session (BRAIN owns the commit). Between runs it verifies (`tsc` + dev health) and, if red,
starts the next run seeded with the redacted diagnostic (KD-5). Three ceilings, three outcomes
(KD-7): the daily quota → graceful `ended`; the flat self-heal budget / the per-run request limit →
`escalation` → `ended(failed)`.

INVARIANTS (KD-12): `run_build` never lets an Exception escape (a raise would strand SESSION-API's
task with no terminal); every path funnels to exactly one `BuildResult`.

BRAIN NEVER EMITS THE TERMINAL `ended` (R7). It never runs git, never tears down, never touches
the lock; SESSION-API performs the single C4 snapshot on return and renders the one authoritative
`ended` from the returned `BuildResult` (KD-11) — which is the only emission point that can report
a TRUE `snapshot_committed`, since anything BRAIN emitted would necessarily predate that snapshot.
So completion travels as DATA (`BuildResult.status` / `.reason` / `.preview_url`), not as a frame:
the funnel emits only the non-terminal context envelopes BRAIN owns (`quota_exceeded`,
`escalation`) and returns. On stop/idle SESSION-API cancels the task; BRAIN unwinds on
`CancelledError` and returns no value (SESSION-API owns that terminal too).
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import Awaitable, Callable, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from typing import Literal

import structlog
from pydantic_ai import Agent, BinaryContent
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models import Model
from pydantic_ai.models.anthropic import AnthropicModelSettings
from pydantic_ai.usage import RequestUsage, UsageLimits
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.v1.build_sessions.schemas import BuildError, BuildResult, BuildSessionStatus
from src.db.models.conversation import ConversationMode
from src.db.models.message import MessageEntryKind
from src.services.messages.store import append_batch
from src.services.orchestrator.agent import build_agent
from src.services.orchestrator.constants import (
    ATTACH_NOT_READY_RETRIES,
    ATTACH_RETRY_BACKOFF_S,
    CACHE_TTL,
    MAX_OUTPUT_TOKENS,
    MODEL_TURN_CEILING,
    READINESS_MAX_POLLS,
    READINESS_POLL_S,
    RUN_WALL_CLOCK_DEADLINE_S,
    SELF_HEAL_MAX_RETRIES,
    TEMPERATURE,
)
from src.services.orchestrator.deps import BuildDeps
from src.services.orchestrator.progress import ProgressEmitter
from src.services.orchestrator.prompt import build_repair_prompt
from src.services.orchestrator.selfheal import CONTINUE_PROMPT, dev_not_ready_error, verify
from src.services.orchestrator.trace import record_run_messages, record_tool_calls
from src.services.sandbox import (
    SandboxClient,
    SandboxGoneError,
    SandboxHandle,
    SandboxNotReadyError,
)
from src.services.usage.gate import (
    DailyTokenLimitExceededError,
    enforce_daily_limit,
    next_ist_midnight_iso,
    record_usage,
)

logger = structlog.get_logger()

_UNKNOWN_APP_ID = uuid.UUID(int=0)
"""Sentinel `app_id` used only when the run-context provider itself fails before an app_id is known
(a SESSION-API-side infra failure). SESSION-API reconciles the real id from its own records."""

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]
"""The per-model-step billing-session factory (C7 §6). `async_sessionmaker[AsyncSession]` satisfies
it structurally; tests bind it to the rolled-back test session."""


class TranscriptPersistError(Exception):
    """A per-step transcript write failed (U5). Raised INSIDE the node loop and funneled to a
    dedicated escalation: continuing would let the workspace and the durable transcript silently
    diverge — the exact failure mode U5 exists to delete — so the build fails loudly instead
    (the write-before-DONE policy, generalized from the relay turn to the build step)."""


@dataclass(frozen=True)
class BuildSpec:
    """What SESSION-API's run-context provider resolves for a session (KD-13): the build
    instruction and the app being built. Keeps `run_build` frozen at 4 params and BRAIN free of a
    direct read of SESSION-API's build-session table."""

    # R3 — MULTIMODAL: a bare `str` when the turn carried no attachments (byte-identical to the
    # pre-R3 path), or `[*attachment_content, prompt_text]` when it did — each attachment first
    # as fenced text (office/csv) or `BinaryContent` (image/PDF vision), the instruction LAST.
    # That order is Anthropic's documented vision ordering (text after files), and it is what
    # both assemblers actually emit: `_live_session_spec` server-side, `attachmentStore.js`
    # in the portal. Pinned by `test_run_context_spec.py`'s
    # `test_attachments_come_before_the_instruction`.
    # SESSION-API materializes the sequence at start (`build_sessions/attachments.py`); BRAIN just
    # hands it to `agent.iter`, which accepts `str | Sequence[UserContent]` natively. `run_build`
    # stays frozen at its 4 params — the widening is inside the run-context provider's return
    # shape, which C7 does not freeze.
    prompt: str | Sequence[str | BinaryContent]
    app_id: uuid.UUID
    # U5 — the thread this build's transcript persists into (`entry_kind='step'` rows). None
    # when the start named no conversation (an API-only caller): the build still runs, it just
    # has no thread to write to, exactly like the outcome record. The spec SHAPE is not frozen
    # by C7 (see the class docstring) — widening here keeps `run_build` at its 4 params.
    conversation_id: uuid.UUID | None = None


RunContextProvider = Callable[[uuid.UUID], Awaitable[BuildSpec]]
"""`session_id -> BuildSpec`, an owner-scoped construction-time dependency BRAIN calls once at
entry (KD-13)."""


@dataclass(frozen=True)
class _QuotaHit:
    limit: int
    used: int


@dataclass(frozen=True)
class _Terminal:
    """The loop's terminal decision — funneled to exactly one `ended` (KD-12). The quota numbers
    are carried as plain ints (0 when not a quota terminal) so the funnel needs no narrowing."""

    kind: Literal["completed", "quota", "escalated"]
    preview_url: str | None = None
    quota_limit: int = 0
    quota_used: int = 0
    escalation_reason: str = ""
    escalation_detail: str = ""
    ended_reason: str = ""
    last_error: BuildError | None = None


class BuildOrchestrator:
    """BRAIN's agentic build engine. Constructed with its per-step billing session factory, the
    KD-13 run-context provider, and the Foundry model (or an injected fake); `run_build` is the
    frozen `RunBuild` Protocol SESSION-API awaits as a background task."""

    def __init__(
        self,
        *,
        model: Model,
        session_factory: SessionFactory,
        run_context_provider: RunContextProvider,
        readiness_max_polls: int = READINESS_MAX_POLLS,
        readiness_poll_s: float = READINESS_POLL_S,
    ) -> None:
        self._model = model
        self._session_factory = session_factory
        self._run_context_provider = run_context_provider
        self._readiness_max_polls = readiness_max_polls
        self._readiness_poll_s = readiness_poll_s

    async def run_build(
        self,
        session_id: uuid.UUID,
        user_id: uuid.UUID,
        sandbox_client: SandboxClient,
        on_progress: Callable[..., Awaitable[None]],
    ) -> BuildResult:
        emitter = ProgressEmitter(on_progress)
        app_id = _UNKNOWN_APP_ID
        try:
            spec = await self._run_context_provider(session_id)
            app_id = spec.app_id
            handle = await _attach_with_patience(sandbox_client, str(user_id))
            deps = BuildDeps(
                sandbox_client=sandbox_client,
                handle=handle,
                emitter=emitter,
                user_id=user_id,
                app_id=app_id,
            )
            await sandbox_client.dev_start(handle)
            if handle.ready:  # a resumed, already-ready sandbox → the initial-load preview trigger
                await emitter.preview_ready(preview_url=handle.preview_url)
            terminal = await self._run_loop(
                emitter,
                deps,
                spec.prompt,
                session_id=session_id,
                conversation_id=spec.conversation_id,
            )
        except asyncio.CancelledError:
            # STOP / IDLE: SESSION-API cancelled us and owns the terminal + snapshot (KD-11).
            # Unwind WITHOUT emitting and return no value (the cancellation propagates).
            raise
        except TranscriptPersistError:
            # U5 — the durable transcript could not keep up with the build. Fail loudly rather
            # than let the workspace and the record silently diverge (write-before-DONE).
            terminal = _escalation(
                reason="transcript_write_failed",
                detail="the build's durable transcript could not be written",
                ended_reason="build_failed",
            )
        except SandboxGoneError:
            terminal = _escalation(
                reason="sandbox_gone",
                detail="the sandbox is gone; a restore is required",
                ended_reason="escalated",
            )
        except UsageLimitExceeded:
            terminal = _escalation(
                reason="request_limit",
                detail="the per-run model-request limit was exceeded",
                ended_reason="build_failed",
            )
        except Exception:
            # An unmodeled crash still funnels to exactly one terminal — never a raised exception
            # that strands SESSION-API's task (KD-12). Bind only non-secret context (KD-9).
            logger.exception(
                "run_build_crashed",
                session_id=str(session_id),
                user_id=str(user_id),
                app_id=str(app_id),
            )
            terminal = _escalation(
                reason="internal_error",
                detail="an unexpected error ended the build",
                ended_reason="build_failed",
            )
        try:
            return await self._funnel(emitter, app_id, terminal)
        except asyncio.CancelledError:
            # Cancelled during the funnel: SESSION-API owns the terminal on cancel (KD-11) —
            # re-raise. A cancellation landing after `escalation` / `quota` now costs only the
            # verdict, never a torn terminal: SESSION-API still emits its own `ended` from the
            # stop path, so the feed always terminates exactly once.
            raise
        except Exception:
            # The funnel is designed non-throwing (the sink swallows-and-logs, `_result` cannot
            # fail), but a truly unexpected funnel error must STILL never strand SESSION-API's task
            # (KD-12) — fall back to a minimal failed result rather than let it escape.
            logger.exception("run_build_funnel_failed", app_id=str(app_id))
            return _result(
                BuildSessionStatus.FAILED,
                app_id,
                None,
                emitter.last_seq,
                None,
                reason="build_failed",
            )

    async def _run_loop(
        self,
        emitter: ProgressEmitter,
        deps: BuildDeps,
        prompt: str | Sequence[str | BinaryContent],
        *,
        session_id: uuid.UUID,
        conversation_id: uuid.UUID | None,
    ) -> _Terminal:
        """The multi-run self-heal loop (KD-1). Emits intermediate `error` / `preview_ready`
        events and returns the terminal decision.

        Only the FIRST turn's prompt can be multimodal (R3): every subsequent `turn_prompt` is a
        plain repair/continue string, because the attachments are already in `messages` — the
        accumulated history the next `agent.iter` run replays. Re-sending the bytes each turn
        would re-pay for them with no added context."""
        messages: list[ModelMessage] = []
        budget = SELF_HEAL_MAX_RETRIES
        turn_prompt: str | Sequence[str | BinaryContent] = prompt
        log_cursor = 0
        preview_emitted = deps.handle.ready
        # A monotonic wall-clock deadline over the WHOLE self-heal loop — the count ceilings
        # (`MODEL_TURN_CEILING`, the `budget` below) bound request/run COUNT but not elapsed time,
        # so a slow/wedged `run_command` could otherwise hold the container + sandbox lock for
        # hours. Use `time.monotonic()` (never wall-clock time-of-day, which can jump).
        loop_started = time.monotonic()

        while True:
            if time.monotonic() - loop_started > RUN_WALL_CLOCK_DEADLINE_S:
                # Wall-clock ceiling hit — escalate through the SAME funnel the turn/self-heal
                # ceilings use (`_escalation` → `ended(failed)`) rather than looping into another
                # multi-minute run. Checked between iterations, so a run already in flight finishes
                # first; the bound is this deadline plus one in-flight command.
                return _escalation(
                    reason="wall_clock_deadline_exceeded",
                    detail="the build exceeded its wall-clock deadline without completing",
                    ended_reason="build_failed",
                    preview_url=deps.handle.preview_url if preview_emitted else None,
                )
            deps.done_requested = False
            quota, messages = await self._run_one(
                deps, messages, turn_prompt, session_id=session_id, conversation_id=conversation_id
            )
            if quota is not None:
                return _Terminal(
                    kind="quota",
                    quota_limit=quota.limit,
                    quota_used=quota.used,
                    preview_url=deps.handle.preview_url if preview_emitted else None,
                )

            outcome, log_cursor = await verify(
                deps.sandbox_client,
                deps.handle,
                log_cursor=log_cursor,
                max_polls=self._readiness_max_polls,
                poll_s=self._readiness_poll_s,
            )
            if outcome.dev_ready and not preview_emitted:  # readiness false→true transition
                await emitter.preview_ready(
                    preview_url=outcome.preview_url or deps.handle.preview_url
                )
                preview_emitted = True

            if deps.done_requested:  # resolve the spinner `declare_done` opened (C7 §3.1)
                await emitter.step(
                    name="declare_done",
                    label="Build verified." if outcome.green else "Not green yet — continuing.",
                    state="ok" if outcome.green else "failed",
                )

            if outcome.green and deps.done_requested:  # objective done-gate (KD-6)
                return _Terminal(kind="completed", preview_url=outcome.preview_url)

            # Not complete → a repair is needed; the flat budget bounds it (KD-7). A NOT-green
            # outcome with no tsc/crash diagnostic means the dev server never became ready within
            # the poll budget — synthesize a server error so neither the repair prompt nor a
            # budget-exhausted escalation is ever left diagnostic-free. `error is None` does NOT
            # imply green (open-Q F): only `CONTINUE_PROMPT` below may run on a truly green build.
            error = outcome.error
            if error is None and not outcome.green:
                error = dev_not_ready_error()

            if budget <= 0:
                return _escalation(
                    reason="self_heal_budget_exhausted",
                    detail="the self-heal budget was exhausted without a green build",
                    ended_reason="build_failed",
                    last_error=error,
                    preview_url=outcome.preview_url,
                )
            if error is not None:
                await emitter.error(error)  # a repair run is coming (KD-5)
                turn_prompt = build_repair_prompt(error)
            else:
                # green but declare_done not called — nudge, not an error envelope (KD-7)
                turn_prompt = CONTINUE_PROMPT
            budget -= 1

    async def _run_one(
        self,
        deps: BuildDeps,
        messages: list[ModelMessage],
        turn_prompt: str | Sequence[str | BinaryContent],
        *,
        session_id: uuid.UUID,
        conversation_id: uuid.UUID | None,
    ) -> tuple[_QuotaHit | None, list[ModelMessage]]:
        """One `agent.iter` run driven node-by-node with per-model-step metering (KD-1/KD-3)
        and per-step transcript persistence (U5). Returns `(quota_hit, accumulated_messages)`;
        on a quota hit the offending model request never fires and the messages are unchanged.

        PERSISTENCE CADENCE: one `step` row per model step — `[the step's request, its
        response]` — persisted after that step's tools have executed. A step's tool RETURNS
        travel in the NEXT step's request (that is where the graph appends them), so a crash
        loses at most the in-flight step and the load seam's dangling-call repair covers the
        orphaned calls. The delta cursor starts at `len(messages)` — the prior runs' history
        was persisted by the runs that produced it."""
        quota_hit: _QuotaHit | None = None
        persisted_from = len(messages)
        async with build_agent.iter(
            turn_prompt,
            deps=deps,
            model=self._model,
            message_history=messages,
            usage_limits=UsageLimits(request_limit=MODEL_TURN_CEILING),
            # Clamp EVERY model step: cap output (else pydantic-ai's 4096 default truncates a
            # whole-file write) and pin temperature to 0.0 for a deterministic build (KD-8).
            # The three cache flags put Anthropic breakpoints on the context this loop re-sends
            # VERBATIM on every single step (R1): the system prompt and the tool definitions are
            # byte-identical across a whole build, and `anthropic_cache` (automatic) walks a
            # breakpoint forward over the message history as the run grows. 3 of Anthropic's 4
            # breakpoint slots, all at `CACHE_TTL` (see the constant for why 1h, not 5m).
            model_settings=AnthropicModelSettings(
                max_tokens=MAX_OUTPUT_TOKENS,
                temperature=TEMPERATURE,
                anthropic_cache_instructions=CACHE_TTL,
                anthropic_cache_tool_definitions=CACHE_TTL,
                anthropic_cache=CACHE_TTL,
            ),
        ) as run:
            node = run.next_node
            while not Agent.is_end_node(node):
                if Agent.is_model_request_node(node):
                    # Enforce in a short read-only session that CLOSES before the model call, so
                    # no DB connection is held idle-in-transaction across the (multi-minute) LLM
                    # request (the chat-relay pattern — enforce and billing never share the model
                    # call's lifetime). A DB error here fails closed → escalation.
                    async with self._session_factory() as db:
                        try:
                            await enforce_daily_limit(db, deps.user_id)  # strictly BEFORE (KD-1)
                        except DailyTokenLimitExceededError as exc:
                            quota_hit = _QuotaHit(limit=exc.limit, used=exc.used)
                            break  # the model request never fires (KD-7 graceful path)
                    node = await run.next(node)  # fires the model request holding NO DB connection
                    if Agent.is_call_tools_node(node):
                        # White-box trace (opt-in, BRAIN_TRACE_DIR): the ordered per-tool call
                        # record incl. read_file + raw args, which the SSE step feed omits.
                        record_tool_calls(deps.app_id, node.model_response)
                        # Record in its OWN short session on a fresh (pre-pinged) connection AFTER
                        # the response — per-step, strictly AFTER (KD-1); BRAIN owns the commit.
                        await self._record_step(deps.user_id, node.model_response.usage)
                else:
                    node = await run.next(node)
                    # U5 — the step's tools have now executed (their returns are in the run's
                    # history), so the step is complete: persist the delta before the next
                    # model request fires.
                    persisted_from = await self._persist_step(
                        deps.user_id,
                        session_id=session_id,
                        conversation_id=conversation_id,
                        history=run.all_messages(),
                        persisted_from=persisted_from,
                    )
            result = run.result
            if quota_hit is None and result is not None:
                messages = result.all_messages()  # thread history into the next run (KD-1)
                # White-box trace (opt-in): the full, durable message history for this run.
                record_run_messages(deps.app_id, messages)
                # U5 — the run-ending response (and anything since the last complete step).
                await self._persist_step(
                    deps.user_id,
                    session_id=session_id,
                    conversation_id=conversation_id,
                    history=messages,
                    persisted_from=persisted_from,
                )
        return quota_hit, messages

    async def _persist_step(
        self,
        user_id: uuid.UUID,
        *,
        session_id: uuid.UUID,
        conversation_id: uuid.UUID | None,
        history: list[ModelMessage],
        persisted_from: int,
    ) -> int:
        """Durably append `history[persisted_from:]` as one `step` row and return the new
        cursor. No-op without a conversation (an API-only build has no thread — same rule as
        the outcome record). Unlike `_record_step`'s best-effort billing write, a failure here
        RAISES (`TranscriptPersistError` → the dedicated escalation): a lost usage row is a
        bounded under-count, a lost transcript step is a silently diverging record."""
        if conversation_id is None:
            return len(history)
        delta = list(history[persisted_from:])
        if not delta:
            return persisted_from
        try:
            async with self._session_factory() as db:
                await append_batch(
                    db,
                    user_id=user_id,
                    conversation_id=conversation_id,
                    messages=delta,
                    entry_kind=MessageEntryKind.STEP,
                    mode=ConversationMode.WRITE,
                    meta={"kind": "build_step", "sessionId": str(session_id)},
                )
        except Exception as exc:
            raise TranscriptPersistError(str(exc)) from exc
        return len(history)

    async def _record_step(self, user_id: uuid.UUID, usage: RequestUsage) -> None:
        """Fold one model step's usage into today's row in its own short session (KD-3), separate
        from the enforce session so the model call holds no DB connection. BRAIN commits.

        Best-effort: a transient DB blip on this WRITE is logged and the build continues — it
        must not fail an otherwise-healthy build over one lost accounting row. The exposure is a
        bounded under-count (one step's tokens); `enforce_daily_limit` still gates every step
        fail-CLOSED before the request fires, so the quota ceiling itself never loosens."""
        try:
            async with self._session_factory() as db:
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
            logger.exception(
                "usage_record_step_failed_continuing",
                user_id=str(user_id),
                input_tokens=usage.input_tokens,
                output_tokens=usage.output_tokens,
            )

    async def _funnel(
        self, emitter: ProgressEmitter, app_id: uuid.UUID, terminal: _Terminal
    ) -> BuildResult:
        """The single terminal funnel: emit the non-terminal context envelopes BRAIN owns and
        return the verdict. `quota_exceeded` / `escalation` still egress here — they are
        informational, not the terminal boundary. The terminal `ended` itself is NOT emitted:
        it is SESSION-API's, rendered from this `BuildResult` after the C4 snapshot (R7/KD-11),
        which is why `reason` travels on the verdict."""
        if terminal.kind == "completed":
            return _result(
                BuildSessionStatus.ENDED,
                app_id,
                terminal.preview_url,
                emitter.last_seq,
                None,
                reason="completed",
            )
        if terminal.kind == "quota":
            await emitter.quota_exceeded(
                limit=terminal.quota_limit,
                used=terminal.quota_used,
                resets_at=next_ist_midnight_iso(),
            )
            return _result(
                BuildSessionStatus.ENDED,
                app_id,
                terminal.preview_url,
                emitter.last_seq,
                None,
                reason="quota_exceeded",
            )
        # escalated
        await emitter.escalation(
            reason=terminal.escalation_reason,
            detail=terminal.escalation_detail,
            last_error=terminal.last_error,
        )
        return _result(
            BuildSessionStatus.FAILED,
            app_id,
            terminal.preview_url,
            emitter.last_seq,
            terminal.last_error,
            reason=terminal.ended_reason,
        )


async def _attach_with_patience(sandbox_client: SandboxClient, user_id: str) -> SandboxHandle:
    """Bounded re-probe for the attach: cold-ACA ingress can wake slower than the client's single
    ~8s reachability probe, so a `SandboxNotReadyError` gets `ATTACH_NOT_READY_RETRIES` more tries
    spaced `ATTACH_RETRY_BACKOFF_S` apart (~30s total tolerance) before escalating exactly as
    today. `SandboxGoneError` is NOT caught — gone still escalates immediately (restore, not
    patience) — and `asyncio.CancelledError` is a `BaseException`, never caught here."""
    attempts_left = ATTACH_NOT_READY_RETRIES
    while True:
        try:
            return await sandbox_client.attach_existing(user_id)
        except SandboxNotReadyError:
            if attempts_left <= 0:
                raise
            attempts_left -= 1
            await asyncio.sleep(ATTACH_RETRY_BACKOFF_S)


def _escalation(
    *,
    reason: str,
    detail: str,
    ended_reason: str,
    last_error: BuildError | None = None,
    preview_url: str | None = None,
) -> _Terminal:
    return _Terminal(
        kind="escalated",
        escalation_reason=reason,
        escalation_detail=detail,
        ended_reason=ended_reason,
        last_error=last_error,
        preview_url=preview_url,
    )


def _result(
    status: Literal[BuildSessionStatus.ENDED, BuildSessionStatus.FAILED],
    app_id: uuid.UUID,
    preview_url: str | None,
    last_seq: int,
    error: BuildError | None,
    *,
    reason: str,
) -> BuildResult:
    return BuildResult(
        status=status,
        reason=reason,  # SESSION-API renders this into the one terminal `ended` (R7)
        app_id=app_id,
        preview_url=preview_url,
        last_seq=last_seq,
        # Always False, and true-by-construction: BRAIN returns strictly BEFORE the C4 snapshot
        # SESSION-API owns (KD-11). The frame's real value is folded in there, not here.
        snapshot_committed=False,
        error=error,
    )
