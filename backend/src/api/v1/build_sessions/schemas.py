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

from src.schemas import CamelModel
from src.services.sandbox import SandboxClient

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


# --- Control operations: start / stop / status (C3 §2) -----------------------


class StartBuildRequest(CamelModel):
    """`POST /v1/build-sessions` body (C3 §2.1)."""

    project_id: uuid.UUID  # REQUIRED — project-first; no lazy Default project (never reintroduce).
    prompt: str  # the citizen-dev's natural-language build instruction for this turn (non-empty).


class StartBuildResponse(CamelModel):
    """`POST /v1/build-sessions` → 201 (C3 §2.1)."""

    session_id: uuid.UUID  # the build-session id — path key for status/stop/lock/SSE + run_build.
    project_id: uuid.UUID
    app_id: uuid.UUID  # the app_registry row being built (== BIAL_APP_ID, C9). Fresh per project.
    status: BuildSessionStatus  # always `provisioning` on a fresh start.
    preview_url: str | None = None  # always null here (dev server not up yet); set once `ready`.
    created_at: datetime


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
    CLIENT = "client"  # RESERVED — the browser client-error arm. NOT emitted in Stage 0 (C7 §7).


class BuildError(BaseModel):
    """The structured, self-heal-relevant error shape (C7 §3) — `{source, title,
    cleaned_stack}`, reused by the `error` envelope, `escalation.last_error`, and
    `BuildResult.error`."""

    model_config = ConfigDict(extra="forbid")

    source: ErrorSource
    title: str  # short human summary (first meaningful error line).
    cleaned_stack: str  # de-noised diagnostic BRAIN feeds back into the self-heal prompt.


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
    `data: [DONE]\\n\\n` and closes. `status` equals `BuildResult.status`."""

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
    | EscalationEvent
    | QuotaExceededEvent
    | EndedEvent,
    Field(discriminator="type"),
]
"""The C7 tagged-union progress envelope — seven members, discriminated on `type`
(C7 §3). BRAIN emits one per `await on_progress(env)`; SESSION-API relays each over
the C3 SSE feed verbatim (snake_case, `seq` preserved)."""


class BuildResult(BaseModel):
    """BRAIN's structured terminal verdict (C7 §1), returned to SESSION-API
    **in-process** (never serialized to the wire). Agrees with the terminal `ended`
    envelope: `BuildResult.status` == the `ended` envelope's `status`."""

    model_config = ConfigDict(extra="forbid")

    # Terminal state, narrowed to the two absorbing members — agrees with the `ended` envelope's
    # `status`; a non-terminal value (e.g. `building`) fails validation.
    status: Literal[BuildSessionStatus.ENDED, BuildSessionStatus.FAILED]
    app_id: uuid.UUID  # the built app (app_registry.id == BIAL_APP_ID, C9).
    preview_url: str | None = None  # the live preview URL if the dev server came up, else None.
    last_seq: int  # the final envelope `seq` emitted — reconciles the feed + C3 `status.last_seq`.
    snapshot_committed: bool  # True if the C4 git snapshot was pushed before returning.
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
