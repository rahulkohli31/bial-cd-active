"""Build-session schemas — the frozen C3 control surface + the C7 brain interface.

Two contracts land here as executable, TESTED code (U8):

* **C3** (`C3-build-session-control-api.md`) — the portal↔SESSION-API control API:
  the `BuildSessionStatus` StrEnum plus the `start` / `stop` / `status` and lock-op
  request/response bodies. These cross the JSON wire, so they subclass the repo's
  `CamelModel` (snake_case Python ⇄ camelCase wire — `session_id` ⇄ `sessionId`).
  It is an **API** enum (a `StrEnum`), NOT a native PG enum: Stage 0 persists no
  durable `build_session` row, so no `sa.Enum` / migration lands until SESSION-API
  adds the row in Wave 1 (D7).

* **C7** (`C7-brain-interface-and-progress.md`) — the BRAIN↔SESSION-API seam: the
  tagged-union progress envelope (`ProgressEnvelope`), `BuildResult`, and the
  `run_build` protocol typing. Unlike C3's REST bodies, the envelope keeps
  **snake_case field names and snake_case `type` literals** — it is a streaming
  frame shape (kin to the chat relay's `{"delta":{"text":…}}`), byte-stable across
  BRAIN-emit → SESSION-API-relay → portal-consume. So the C7 models subclass plain
  `BaseModel` (no camelCase alias generator), discriminating on `type` exactly like
  the C2 `FileOp` union discriminates on `action` (`services/sandbox/base.py`).

These C7 shapes are shared code imported **read-only** by BRAIN (D3): freezing them
here — not doc-only — is what kills the "both sides invent the shape" divergence
between BRAIN (emits) and SESSION-API (relays over the C3 SSE feed).
"""

from __future__ import annotations

import enum
import uuid
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Annotated, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.core.integrity_types import WorkspaceState
from src.schemas import CamelModel
from src.services.sandbox import SandboxClient
from src.services.sandbox.base import CompileState

# =============================================================================
# C3 — Build-session control API
# =============================================================================


class BuildSessionStatus(enum.StrEnum):
    """The build-session lifecycle (C3 §1). Five members; the wire value equals the
    member's lowercase name. An **API** StrEnum, not a native PG enum — no durable
    row lands until SESSION-API's Wave-1 migration (D7).

    Forward path `PROVISIONING → BUILDING → READY`; any non-terminal state →
    `ENDED` (graceful) or `FAILED` (unrecoverable). `ENDED`/`FAILED` are absorbing.
    """

    PROVISIONING = "provisioning"  # session created; sandbox provisioning/attaching. No preview.
    BUILDING = "building"  # run_build's agentic loop is executing model steps + self-heal.
    READY = "ready"  # dev server up: a C7 `preview_ready` fired and `preview_url` is set.
    ENDED = "ended"  # terminal, GRACEFUL: user stop / idle-teardown / quota. Not a failure.
    FAILED = "failed"  # terminal, UNRECOVERABLE: self-heal exhausted / unrecoverable error.


# --- Frozen lock TTL + cadence constants (C3 §3) -----------------------------
# The Redis key namespace + TTLs are owned by C5; C3 freezes the client-facing
# cadence. SESSION-API's Wave-1 lock ops set these; the portal keep-alive loop
# renews/heartbeats to them.

LOCK_TTL_SECONDS = 900  # 15 min — lock auto-expires if not renewed (C5 reaper reconciles).
LOCK_RENEW_CADENCE_SECONDS = 300  # 5 min — client renews at ⅓ TTL (two renews of head-room).
HEARTBEAT_CADENCE_SECONDS = 30  # portal heartbeats every 30 s while the tab is open.
HEARTBEAT_TTL_SECONDS = 90  # 3× cadence → tolerate 2 missed beats before idle-teardown.
# A relaunched preview (#43) holds no lock and renews no heartbeat, so it gets an
# explicit STAY OF EXECUTION instead: 30 min. Long enough to actually look at the
# restored app (it is read-only and human-paced — read, click, close, not a
# multi-minute agentic build that renews as it works), short enough to bound an
# abandoned container on a metered subscription. Honored by the background sweep
# only; reconcile-on-start reaps through it (the incoming build needs the slot).
# A plain module constant like its C3-frozen neighbours above — deliberately NOT a
# Settings field: it is a frozen protocol constant, not deployment config.
RELAUNCH_PREVIEW_STAY_SECONDS = 1800  # 30 min

# --- The R10 wall-clock liveness lease (C5 family 4, ADR-0029 §8) ------------
# A build in flight renews `bial:{env}:sandbox:lease:{user_id}` on the cadence below, and
# the reconciliation sweep reads it. It exists because NOTHING else here is legible to a
# process that is not running the build: the heartbeat above is seeded once per turn, so
# ~90 s in the only remaining shield is `sweep_all`'s in-process `live_users` set — empty
# everywhere else. Plain module constants like their C3-frozen neighbours: a frozen
# protocol constant, not deployment config.
#
# The TTL is MANDATORY, not a default. The registry hash's lack of one is the root cause
# of ADR-0029, and a lease is the worst family to repeat it in: one that never expires is
# a container that can never be reclaimed. It also bounds a lease abandoned by a process
# that died mid-renewal, and it is the ceiling the fail-closed read compares against, so a
# bad clock cannot buy a millennium.
#
# 120 s over a 30 s cadence = three missed renewals of head-room, the same shape as
# HEARTBEAT_TTL_SECONDS over HEARTBEAT_CADENCE_SECONDS. Head-room is the point: a cadence
# at or near the TTL lets a perfectly healthy build lose its lease between renewals, and
# the sweep then reaps mid-build and logs it as idle.
LIVENESS_LEASE_TTL_SECONDS = 120
LIVENESS_LEASE_RENEW_CADENCE_SECONDS = 30

# The ceiling needs slack, because the whole point of this family is that the WRITER and the
# READER are different processes — and therefore different clocks. The writer stores
# `its_now + TTL`; a reader whose clock lags by delta computes a ceiling of `its_now + TTL`,
# which is lower by exactly delta. Without grace, ANY positive skew makes a lease renewed one
# millisecond ago read as "absurd", fail closed, and reap a container an agent is working
# inside — the precise outcome this unit exists to prevent, arriving through the safety check.
#
# 30 s is two orders of magnitude above real NTP skew between two ACA containers, and still
# four times tighter than the error class the ceiling is actually for: a writer using
# milliseconds puts the deadline ~120_000 s out, not 30.
LIVENESS_LEASE_CLOCK_SKEW_GRACE_SECONDS = 30

# --- R14: what the generated app actually served ------------------------------
# Requests the app served to real users buy a BOUNDED extension, never indefinite life.
# Shorter than a deliberate builder action, because it is weaker evidence of intent: a
# left-open app tab polling in the background is still traffic, and nobody is working.
# Bounded means bounded — each report buys this much from now, and a container with
# nothing but background chatter still lapses inside the idle band.
SERVED_TRAFFIC_STAY_SECONDS = 900


class PreviewLifeState(enum.StrEnum):
    """What is (or is not) serving a project's preview right now — C3 §8.3.

    An **API** StrEnum like `BuildSessionStatus`, not a native PG enum: nothing persists it.
    The wire value equals the member's lowercase name.

    It exists because `preview-state` used to answer `alive: false` for four situations that
    a builder experiences as completely different things, one of which was not a situation at
    all but an ERROR — a registry read that threw came back as "your preview is gone", and the
    portal dutifully pulled a perfectly live app off the screen.

    An unknown gets its own member and its own UI. It is never folded into a neighbour, for
    exactly the reason `SaveState.dirty` is tri-state: the reassuring answer is the one you
    must never give on someone else's behalf."""

    ALIVE = "alive"  # a container is serving THIS project; `preview_url` is framable.
    # Built before, nothing serving it now. The next prompt brings it back from the durable
    # copy on Blob. NOT an error, NOT a loss — which is why no surface may style it as one.
    ASLEEP = "asleep"
    # Another of this user's projects holds the one-per-user workspace. `occupying_project_name`
    # names it, or is null when the live container matches no app this user owns (a ghost —
    # say nothing rather than guess a name into a sentence about someone's work).
    SLOT_TAKEN = "slot_taken"
    NEVER_BUILT = "never_built"  # no app row: nothing was built here, so nothing can serve it.
    # The coordination store could not be read. Claims NOTHING in either direction; a client
    # that renders this as "gone" has reintroduced the bug this enum was written to kill.
    UNKNOWN = "unknown"


# --- Control operations: start / stop / status (C3 §2) -----------------------


class StartBuildRequest(CamelModel):
    """`POST /v1/build-sessions` body (C3 §2.1)."""

    project_id: uuid.UUID  # REQUIRED — project-first; no lazy Default project (never reintroduce).
    prompt: str  # the citizen-dev's natural-language build instruction for this turn (non-empty).
    # R3 — OPTIONAL, back-compat: the thread whose attachments ground this build (`conversationId`
    # on the wire). Present → the server materializes that conversation's file parts into the
    # agent's prompt (images/PDF as vision, office/csv as fenced extracted text); absent → a
    # text-only build, byte-identical to the pre-R3 behaviour. Attachments travel by REFERENCE,
    # not payload: the portal already persisted the parts before calling start, so the bytes need
    # no second trip through the browser. Amends a frozen C3 request body — additive and optional,
    # recorded in C3 §2.1 by U8. Owner- AND project-scoped at resolution: a conversation that is
    # not the caller's, or belongs to a different project than `project_id`, is a non-leaking 404
    # (a build must never be grounded in another project's files).
    conversation_id: uuid.UUID | None = None


class StartBuildResponse(CamelModel):
    """`POST /v1/build-sessions` → 201 (C3 §2.1)."""

    session_id: uuid.UUID  # the build-session id — path key for status/stop/lock/SSE + run_build.
    project_id: uuid.UUID
    app_id: uuid.UUID  # the app_registry row being built (== BIAL_APP_ID, C9). Fresh per project.
    status: BuildSessionStatus  # always `provisioning` on a fresh start.
    preview_url: str | None = None  # always null here (dev server not up yet); set once `ready`.
    created_at: datetime


class RelaunchPreviewRequest(CamelModel):
    """`POST /v1/build-sessions/relaunch` body (#43). Project-scoped, not session-scoped: the
    old build session is long gone (~5 min after teardown), but `app_id` is durable and
    resolved from the project."""

    project_id: uuid.UUID  # REQUIRED — the owning project; the app is resolved from it.
    # Put the LAST SAVED version back instead of resuming the newest workspace. Default False
    # because the newest tree is what the user was looking at, and restoring an older one over
    # it is the failure that costs them work. Neither choice promotes anything: the saved
    # bundle is untouched either way, so `dirty` stays true and Save is still their click.
    # Set from an explicit user action ("go back to my last saved version"), never inferred.
    prefer_saved: bool = False


class RelaunchPreviewResponse(CamelModel):
    """`POST /v1/build-sessions/relaunch` → 200 (#43). No `session_id`/`created_at`: relaunch
    registers NO in-process build session (Decision 6 — it must not occupy the build slot), so
    there is nothing to poll or stop.

    `preview_url` is always framable; `ready` says whether it is SERVING yet. The two came apart
    when relaunch stopped 503ing on a slow app (R6/SL-20): an attached container whose root route
    outruns the readiness budget still hands back its URL, because the alternative — condemning
    the container — cost a citizen their unsaved work."""

    app_id: uuid.UUID
    # the framable https://{fqdn}/ root. Live whenever `ready`; on a degraded attach it is the
    # right URL for a server that has not answered yet.
    preview_url: str
    status: BuildSessionStatus  # `ready`, or `provisioning` when the app is not serving yet.
    # U6's "last saved version" signal (#43): True when the project's NEWEST recorded build
    # outcome was FAILED — `_do_finalize` snapshots pass and fail alike, so the restored
    # workspace is the last SAVED state, not that build's intent. The portal labels the
    # relaunched preview accordingly instead of presenting an unqualified "ready".
    restored_from_failed_build: bool
    # Is the app SERVING the URL above yet? False only on the attach arm's fail-open path — the
    # container is alive and holds the user's work, the app is just slow to answer. The portal
    # frames the URL either way and keeps its labelled wait up until the frame loads. Defaulted
    # so an older client that ignores the field reads the historic "relaunch returns ready".
    ready: bool = True


class StopBuildRequest(CamelModel):
    """`POST /v1/build-sessions/{sessionId}/stop` body (C3 §2.2)."""

    reason: str | None = None  # optional free-text reason for the audit/activity feed.


class StopBuildResponse(CamelModel):
    """`POST /v1/build-sessions/{sessionId}/stop` → 200 (C3 §2.2)."""

    session_id: uuid.UUID
    status: BuildSessionStatus  # `ended` after a graceful stop.


class BuildSessionStatusResponse(CamelModel):
    """`GET /v1/build-sessions/{sessionId}` → 200 (C3 §2.3). The poll surface and the
    source of the framable `preview_url`."""

    session_id: uuid.UUID
    project_id: uuid.UUID
    app_id: uuid.UUID
    status: BuildSessionStatus
    preview_url: str | None  # the sandbox `next dev` root (un-prefixed). Null until `ready`.
    last_seq: int | None  # highest C7 envelope `seq` so far; a client resumes SSE from it (§4).
    created_at: datetime
    updated_at: datetime


# --- Lock operations: acquire / renew / release / force-end / heartbeat (C3 §3) ---
# The lock ops carry no request body; only the response bodies are typed.


class LockStateResponse(CamelModel):
    """`.../lock/acquire` and `.../lock/renew` → 200 (C3 §3.1, §3.2)."""

    session_id: uuid.UUID
    held: bool  # true when the caller now holds the lock.
    owner_user_id: uuid.UUID  # the current holder (always the caller when held).
    ttl_seconds: int  # the TTL just (re)set — LOCK_TTL_SECONDS.
    expires_at: datetime  # UTC instant the lock lapses if not renewed.


class LockReleaseResponse(CamelModel):
    """`.../lock/release` → 200 (C3 §3.3). Idempotent."""

    session_id: uuid.UUID
    released: bool  # true if the lock was held-and-released or already free.


class ForceEndResponse(CamelModel):
    """`.../lock/force-end` → 200 (C3 §3.4). The owner-only kill switch."""

    session_id: uuid.UUID
    status: BuildSessionStatus  # `ended`.


class HeartbeatResponse(CamelModel):
    """`.../heartbeat` → 200 (C3 §3.5). The portal's liveness ping."""

    session_id: uuid.UUID
    alive: bool  # true — the session is live and the heartbeat was recorded.
    cadence_seconds: int  # HEARTBEAT_CADENCE_SECONDS — the client's next-beat interval.
    heartbeat_expires_at: datetime  # UTC instant the reaper considers the session idle.


# --- the app's own client-error report (U13, R17 runtime half) ----------------
#
# The generated app relays its `window.onerror` / `unhandledrejection` / `console.*` captures to
# the framing portal by postMessage (`sandbox/template/components/bial/error-capture.tsx`); the
# portal validates the origin and POSTs the payload here. Every field below is text the app wrote,
# so the caps are the boundary's job: parked verbatim, redacted and framed at the point of use.

CLIENT_ERROR_SOURCE_MAX_CHARS = 64
"""Cap on the reporter LABEL (`window.onerror`, `unhandledrejection`, `console.error`, …).

A bounded free string rather than an enum of the four the template emits today, deliberately. The
value is a label on a diagnostic, never a branch: pinning the set here would mean that the day the
template learns a fifth capture point, the backend answers 422 and the crash it was reporting
becomes invisible — fail-closed on the one signal this whole unit exists to make visible."""

CLIENT_ERROR_TITLE_MAX_CHARS = 1_000
"""Cap on the report's headline. The template already slices its own titles to 500; this leaves
room for a reporter that does not, without accepting a payload of arbitrary size."""

CLIENT_ERROR_STACK_MAX_CHARS = 20_000
"""Cap on the report's stack. Generous — a deep component tree produces a long stack and cutting a
real one short would cost the agent the frame that names the faulty file — but finite, because the
writer is a crashing browser inside an app whose code we did not author. Anything past this is a
422; `declutter` then truncates what IS accepted to `CLEANED_STACK_MAX_CHARS` anyway."""


class WorkspaceCheckResponse(CamelModel):
    """`POST /v1/build-sessions/projects/{projectId}/workspace-check` → 200 (U4, R4/R7).

    Does the container still hold this app? — asked by an IDLE tab, so a reversion that happens
    while nobody is sending messages is caught at the preview poll rather than at a turn that may
    never come.

    `reverted` IS THE WHOLE ANSWER for the client, and it is a separate field from `state` rather
    than something the client derives. Only one of the four states may be acted on, and leaving
    the client to write `state !== "intact"` is leaving it to retract a completion claim on the
    two states that mean "we could not tell" — which is the mistake the server side of this
    verdict is built to make impossible. The state is carried for the operator surface and for
    diagnosis; the client reads the boolean."""

    state: WorkspaceState
    reverted: bool


class CompileStateResponse(CamelModel):
    """`GET /v1/build-sessions/projects/{projectId}/compile-state` → 200 (R17/R18).

    The compile signal for a tab with NO LIVE TURN. During a turn the state arrives on the turn
    stream as a `compile` frame; the moment the turn ends that producer stops, so a tab that
    reloads afterwards would otherwise have nothing to cover a broken preview with.

    `unknown` is a real answer and the caller must HOLD its current cover on it — never read it
    as clean. It is what an app with no live container, no app row, or a container predating the
    signal all answer."""

    state: CompileState


class ClientErrorReportRequest(CamelModel):
    """`POST /v1/build-sessions/projects/{projectId}/client-error` body (U13).

    Mirrors the payload the app's capture component posts to the portal, minus two fields the
    portal must NOT forward: `type` (the postMessage discriminator — it has done its job by the
    time the portal is calling us) and `ts` (the app's own clock, which is app-controlled and
    would only ever be used for expiry, where our own arrival time is the honest value)."""

    source: str = Field(max_length=CLIENT_ERROR_SOURCE_MAX_CHARS)
    title: str = Field(max_length=CLIENT_ERROR_TITLE_MAX_CHARS)
    # Defaulted because the template sends `""` for the `console.error` / `console.warn` arms —
    # those have a message and no stack, and a required field would reject the commonest report.
    stack: str = Field(default="", max_length=CLIENT_ERROR_STACK_MAX_CHARS)


class ClientErrorReportResponse(CamelModel):
    """`POST /v1/build-sessions/projects/{projectId}/client-error` → 202 (U13).

    `recorded: false` is a SUCCESS, and it is the one thing worth saying here: this app already
    has as many reports waiting for the next health verdict as the store keeps, so this one was
    dropped. Answering an unqualified 202 would tell a crash loop that all four hundred of its
    copies were collected, and would leave a client with no way to see it is being throttled."""

    recorded: bool


# =============================================================================
# C7 — Brain interface + tagged-union progress envelope
# =============================================================================
#
# Snake_case field names + snake_case `type` literals, NO camelCase alias generator
# (C7 "Wire casing"): the envelope is a streaming frame whose keys must be byte-stable
# across BRAIN-emit → SESSION-API-relay → U8-test → portal-consume.


class ErrorSource(enum.StrEnum):
    """The self-heal error origin (C7 §3). Shared by `ErrorEvent`,
    `EscalationEvent.last_error`, and `BuildResult.error`."""

    TSC = "tsc"  # `tsc` typecheck failure, read over C1 /exec.
    NEXT_BUILD = "next_build"  # `next build` failure, read over C1 /exec.
    SERVER = "server"  # dev-server stderr, read over C1 /dev/logs.
    # The browser client-error arm — LIVE as of U13, and no longer a class the user never
    # hears about. Its REPORT stays agent-only (see `agent_only_detail`); what reaches the
    # citizen is the platform's own sentence for the class, which `errors.user_facing` owns
    # and `DiagnosticFrame` carries (U16). "Never emitted" and "never rendered" are both
    # stale readings of this member — do not restore either.
    CLIENT = "client"


class BuildError(BaseModel):
    """The structured, self-heal-relevant error shape (C7 §3) — `{source, title,
    cleaned_stack}`, reused by the `error` envelope, `escalation.last_error`, and
    `BuildResult.error`.

    THIS SHAPE IS THE MODEL'S, and U16 deliberately left it alone. `title` is BUILT to be the
    compiler's own first meaningful line — that is what makes it useful to a repair run, and
    what made rendering it the most developer-looking thing a citizen ever read. The fix was to
    stop rendering it, not to soften it: the citizen-facing sentence + next action live in
    `errors.user_facing` and travel on `DiagnosticFrame`, so `title` and `cleaned_stack` stay
    byte-identical for a given raw input and the self-heal loop reads exactly what it always
    did. Anyone tempted to make these two fields friendlier is about to break the repair prompt.
    """

    model_config = ConfigDict(extra="forbid")

    source: ErrorSource
    title: str  # short human summary (first meaningful error line).
    cleaned_stack: str  # de-noised diagnostic BRAIN feeds back into the self-heal prompt.
    # U13 — the AGENT-ONLY half of a deliberately dual-purpose object. `BuildError` is read by two
    # audiences with opposite needs: it becomes the portal's `error` envelope / `diagnostic` frame
    # AND the next run's repair prompt. For a `client`-class report those two must diverge — the
    # text was written by code inside the generated app, so it may reach the model (which can act
    # on it) and must not reach the user (for whom a JS stack trace is not a product surface).
    #
    # `exclude=True` is what makes that structural instead of a convention: the field is dropped
    # from EVERY serialization, so `BuildResult.error` and `EscalationEvent.last_error` cannot
    # carry it out to the portal by simply forgetting about it. `build_repair_prompt` reads the
    # attribute in-process, which is the only path that sees it at all.
    #
    # Absent (None) on every other source, where `cleaned_stack` is already safe to render and the
    # repair prompt uses it unchanged — so nothing about the tsc / server / next_build arms moves.
    agent_only_detail: str | None = Field(default=None, exclude=True)


class _ProgressEventBase(BaseModel):
    """Shared envelope base: every C7 event carries the monotonic `seq` (§2). Extra
    keys are forbidden so a mis-shaped payload fails discrimination loudly."""

    model_config = ConfigDict(extra="forbid")

    seq: int  # per-session, starts at 1, strictly +1, gap-free (§2). The SSE `id:` cursor.


class StepEvent(_ProgressEventBase):
    """`step` — a high-level phase marker for the activity feed (C7 §3.1)."""

    type: Literal["step"] = "step"
    name: str  # stable-ish step id, e.g. "scaffold" | "install_deps" | "dev_start" | "self_heal".
    label: str  # human one-liner, e.g. "Installing dependencies…".
    state: Literal["started", "ok", "failed"]  # drives the UI spinner → check/cross.
    # F3/U3 — read-only + housekeeping steps are dropped from the VISIBLE feed (the raw command
    # still reaches the model). Additive + defaulted, so pre-U3 emitters stay wire-valid.
    hidden: bool = False


class LogEvent(_ProgressEventBase):
    """`log` — a raw log line (build/install output, dev-server tail) (C7 §3.2)."""

    type: Literal["log"] = "log"
    source: str  # "exec" (a C1 /exec run) or "dev" (a C1 /dev/logs tail).
    stream: Literal["stdout", "stderr"]
    text: str  # one LF-normalized line.


class ErrorEvent(_ProgressEventBase):
    """`error` — the structured error BRAIN reacts to; carries `{source, title,
    cleaned_stack}` (C7 §3.3). Stage 0 emits only tsc | next_build | server."""

    type: Literal["error"] = "error"
    source: ErrorSource
    title: str
    cleaned_stack: str


class PreviewReadyEvent(_ProgressEventBase):
    """`preview_ready` — the dev server is live and framable; carries `preview_url`
    (C7 §3.4). Flips C3 status → `ready` and triggers the portal iframe (re)load."""

    type: Literal["preview_ready"] = "preview_ready"
    preview_url: str  # the sandbox `next dev` root — un-prefixed `https://{fqdn}/` (C2).


class PreviewReconnectingEvent(_ProgressEventBase):
    """`preview_reconnecting` — the dev-server PROCESS exited (the port closed) AFTER the preview
    was already framed (F8/U5). A feed-only status SIGNAL, not a lifecycle transition: the C3
    `BuildSessionStatus` enum is frozen at five members with no "reconnecting" state, so this never
    changes the session status (a completed build stays `ended`, a live one stays `ready`). The
    portal reads it to show a DISTINCT reconnecting visual — never the "building" spinner — over
    the now-dead frame, and a following `preview_ready` re-frames once the dev server serves. The
    FRONTEND cannot originate this: `/dev/status` is supervisor-internal + bearer-guarded, so crash
    detection is backend-only (the early readiness watcher owns it)."""

    type: Literal["preview_reconnecting"] = "preview_reconnecting"


class EscalationEvent(_ProgressEventBase):
    """`escalation` — the self-heal loop gave up; a human or next turn must intervene
    (C7 §3.5). Informational; the terminal boundary is the following `ended`."""

    type: Literal["escalation"] = "escalation"
    reason: str  # machine-ish code, e.g. "self_heal_budget_exhausted".
    detail: str  # human explanation for the activity feed.
    last_error: BuildError | None = None  # the final error that triggered escalation, or null.


class QuotaExceededEvent(_ProgressEventBase):
    """`quota_exceeded` — the per-user daily token cap was hit at a model step (C7
    §3.6). BRAIN emits this, then gracefully ends (§8)."""

    type: Literal["quota_exceeded"] = "quota_exceeded"
    limit: int  # the effective daily cap (from DailyTokenLimitExceededError.limit).
    used: int  # tokens used today (from .used).
    resets_at: str  # next IST-midnight, UTC ISO-8601 (gate.next_ist_midnight_iso).


class EndedEvent(_ProgressEventBase):
    """`ended` — the terminal envelope (C7 §3.7). After it, the C3 SSE feed emits
    `data: [DONE]\\n\\n` and closes. `status` equals `BuildResult.status`.

    SESSION-API is its SOLE emitter, and it emits exactly one per session from
    `_do_finalize` — AFTER the C4 snapshot commit, so `snapshot_committed` is the true
    post-commit value (R7). BRAIN never emits it: an `ended` from BRAIN necessarily
    precedes the snapshot SESSION-API owns, so it could only ever report
    `snapshot_committed=false`. BRAIN returns its verdict on `BuildResult` instead."""

    type: Literal["ended"] = "ended"
    # Narrowed to the two terminal members: a terminal frame carrying a non-terminal status
    # (e.g. `building`) must fail validation, not slip through.
    status: Literal[BuildSessionStatus.ENDED, BuildSessionStatus.FAILED]
    preview_url: str | None = None  # the final live preview URL, or null if it never came up.
    snapshot_committed: bool  # True if the C4 snapshot pushed before end.
    reason: str  # "completed" | "stopped_by_user" | "idle_teardown" | "quota_exceeded" | …


ProgressEnvelope = Annotated[
    StepEvent
    | LogEvent
    | ErrorEvent
    | PreviewReadyEvent
    | PreviewReconnectingEvent
    | EscalationEvent
    | QuotaExceededEvent
    | EndedEvent,
    Field(discriminator="type"),
]
"""The C7 tagged-union progress envelope — eight members, discriminated on `type`
(C7 §3; `preview_reconnecting` added by F8/U5). BRAIN emits one per `await on_progress(env)`;
SESSION-API relays each over the C3 SSE feed verbatim (snake_case, `seq` preserved)."""


class BuildResult(BaseModel):
    """BRAIN's structured terminal verdict (C7 §1), returned to SESSION-API
    **in-process** (never serialized to the wire).

    This — NOT an envelope — is how BRAIN's completion travels: SESSION-API renders the one
    terminal `ended` from it after the C4 snapshot (R7), so `status`/`reason`/`preview_url`
    here are the source those frame fields are built from."""

    model_config = ConfigDict(extra="forbid")

    # Terminal state, narrowed to the two absorbing members — becomes the `ended` envelope's
    # `status`; a non-terminal value (e.g. `building`) fails validation.
    status: Literal[BuildSessionStatus.ENDED, BuildSessionStatus.FAILED]
    reason: str  # becomes the `ended` envelope's `reason`: "completed" | "quota_exceeded" | …
    app_id: uuid.UUID  # the built app (app_registry.id == BIAL_APP_ID, C9).
    preview_url: str | None = None  # the live preview URL if the dev server came up, else None.
    last_seq: int  # the final envelope `seq` emitted — reconciles the feed + C3 `status.last_seq`.
    # BRAIN's at-return-time view, and by construction ALWAYS False: BRAIN returns strictly
    # BEFORE the C4 snapshot SESSION-API owns. NEVER read this as the answer to "was the work
    # saved?" — only the terminal `ended` frame carries that truth (R7). Kept solely so the C7 §1
    # verdict shape stays frozen for the pilot; removing it is a clean U8 follow-up.
    snapshot_committed: bool
    error: BuildError | None = None  # populated on `failed`; None on a clean end.


# --- The run_build interface (C7 §1) -----------------------------------------

ProgressSink = Callable[[ProgressEnvelope], Awaitable[None]]
"""The in-process async sink SESSION-API supplies (C7 §4). BRAIN `await`s it for every
envelope it emits — this IS the transport (an `asyncio.Queue` put), never Redis."""


class RunBuild(Protocol):
    """The frozen BRAIN entry point (C7 §1) — a callable Protocol BRAIN's orchestrator
    implements in Wave 1; imported READ-ONLY here (D3). Exactly four parameters; the
    `prompt` is intentionally NOT one of them (how BRAIN obtains the instruction is a
    SESSION-API↔BRAIN internal, out of C7's frozen surface).

    `sandbox_client` is the C2 `SandboxClient` ABC (BRAIN calls the exec/files/dev
    subset through it); `on_progress` is the `ProgressSink`.
    """

    async def __call__(
        self,
        session_id: uuid.UUID,  # the C3 build session (the SSE feed key). Identifies the run.
        user_id: uuid.UUID,  # the session OWNER — all metering is charged here (ADR-0025)
        sandbox_client: SandboxClient,  # the C2 ABC instance. Imported READ-ONLY.
        on_progress: ProgressSink,  # the in-process sink for every emitted envelope (§4).
    ) -> BuildResult: ...


BillingSessionFactory = async_sessionmaker[AsyncSession]
"""BRAIN's per-model-step metering session factory (C7 §6). Because `run_build`'s
signature is frozen at four params, the factory is a **construction-time dependency**
of BRAIN's orchestrator (not a `run_build` argument): BRAIN opens its own
`AsyncSession` per model step from it and OWNS the commit — `record_usage` does not
commit, and there is no request-scoped `get_db` on a background task. Tests bind it to
the rolled-back test session (the exact substitution `claude/router.py` makes via
`dependency_overrides` on its `billing_session_factory`). Same shape as the chat relay.
"""


class ParkedTree(CamelModel):
    """One tree this plan set aside — a U2 quarantine or a U3 divert."""

    key: str
    kind: Literal["quarantine", "divert"]
    head_sha: str | None
    size_bytes: int
    taken_at: datetime | None


class ParkedTreesResponse(CamelModel):
    """`POST /v1/build-sessions/internal/apps/{app_id}/parked` → 200 (U25).

    THE TREES THIS PLAN SETS ASIDE WOULD OTHERWISE BE WRITE-ONLY: no reader, no retention, no
    runbook. In a false-`REVERTED` case those objects hold the only copy of a citizen's newest
    work, and this plan names exactly that shape as a defect elsewhere — so it must not reproduce
    it. Newest first, because the useful one is almost always the last one."""

    trees: list[ParkedTree]


class PromoteParkedRequest(CamelModel):
    """`POST /v1/build-sessions/internal/apps/{app_id}/promote` — put one parked tree back.

    The key is named explicitly rather than "the newest": an operator promoting the wrong tree
    over somebody's recovery slot is the failure this whole plan is about, and a request that
    cannot name what it means is one that can be misread."""

    key: str


class PromoteParkedResponse(CamelModel):
    """What the promotion did. `promoted` is False when the guard refused it — which is not an
    error and must not read as one: it means the tree is not a descendant of what the slot holds,
    and forcing it would be the data loss the guard exists to prevent."""

    promoted: bool
    detail: str
