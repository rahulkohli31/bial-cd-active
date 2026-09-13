"""Build-session schemas — the frozen control surface plus the brain interface.

The portal↔session-API control API (`BuildSessionStatus`, `start`/`stop`/`status`, lock-op
bodies) crosses the JSON wire, so it subclasses `CamelModel` (snake_case ⇄ camelCase). The
status is an API `StrEnum`, not a native PG enum — no durable row is persisted.

The brain seam is the tagged-union progress envelope, `BuildResult`, and the `run_build`
protocol. It keeps snake_case fields AND `type` literals — a streaming frame that must stay
byte-stable emit→relay→portal-consume — so these subclass plain `BaseModel` with no alias
generator, discriminating on `type` like `FileOp` on `action`. Freezing them here as shared
read-only code stops both sides inventing the shape separately.
"""

from __future__ import annotations

import enum
import uuid
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime
from typing import Annotated, Final, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.core.integrity_types import WorkspaceState
from src.schemas import CamelModel
from src.services.sandbox import SandboxClient
from src.services.sandbox.base import CompileState

# =============================================================================
# Build-session control API
# =============================================================================


class BuildSessionStatus(enum.StrEnum):
    """The build-session lifecycle. Five members; the wire value equals the
    member's lowercase name. An **API** StrEnum, not a native PG enum — no durable
    row lands until SESSION-API's migration.

    Forward path `PROVISIONING → BUILDING → READY`; any non-terminal state →
    `ENDED` (graceful) or `FAILED` (unrecoverable). `ENDED`/`FAILED` are absorbing.
    """

    PROVISIONING = "provisioning"  # session created; sandbox provisioning/attaching. No preview.
    BUILDING = "building"  # run_build's agentic loop is executing model steps + self-heal.
    READY = "ready"  # dev server up: a `preview_ready` envelope fired and `preview_url` is set.
    ENDED = "ended"  # terminal, GRACEFUL: user stop / idle-teardown / quota. Not a failure.
    FAILED = "failed"  # terminal, UNRECOVERABLE: self-heal exhausted / unrecoverable error.


# --- Frozen lock TTL + cadence constants -------------------------------------
# There is no portal keep-alive loop anymore and no HTTP surface to renew them from a browser
# (the `lock/renew` / `heartbeat` routes were retired) — the server itself is the only renewer
# now, in-process, via `locks.py`/`manager.py`/`turns/engine.py`/`reaper.py`.

LOCK_TTL_SECONDS = 900  # 15 min — lock auto-expires if not renewed (the reaper reconciles).
# THE TWO CADENCES BELOW HAVE NO RUNTIME READER LEFT, and saying so is the point: they were
# written for the browser that renewed on a timer, and that caller is gone. TWO server-side
# renewers replaced it, and neither reads these numbers. `manager.on_progress` calls
# `renew_lock` + `write_heartbeat` on every non-terminal progress envelope — frame-driven, so a
# build renews as fast as it produces frames. That is not enough on its own: a build that spends
# longer than `HEARTBEAT_TTL_SECONDS` inside one tool call emits no frame, so the frame-driven
# renewer alone lets the heartbeat expire under a live build. So the turn engine's
# liveness-lease loop (`turns/engine.py::_hold_liveness_lease`) calls the same pair once per
# `LIVENESS_LEASE_RENEW_CADENCE_SECONDS` — 30 s, comfortably inside the 90 s heartbeat TTL below,
# and that is where the real wall-clock cadence lives.
# They stay as the frozen HEAD-ROOM RATIOS the live TTLs are sized against — each is ⅓ of
# the TTL beside it, so two renewals may be missed before anything lapses. `test_locks.py::
# test_lock_ttl_has_renew_headroom` pins the lock half of that inequality; the seconds
# themselves are pinned by `test_schemas.py::test_cadence_constants_are_the_frozen_values`.
LOCK_RENEW_CADENCE_SECONDS = 300  # 5 min — ⅓ TTL, i.e. two renews of head-room.
HEARTBEAT_CADENCE_SECONDS = 30  # ⅓ of the heartbeat TTL below, on the same ratio.
HEARTBEAT_TTL_SECONDS = 90  # 3× cadence → tolerate 2 missed beats before idle-teardown.
# The bounded STAY OF EXECUTION written onto a relaunched preview: 30 min. Long enough to
# actually look at the restored app (it is read-only and human-paced — read, click, close, not
# a multi-minute agentic build that renews as it works), short enough to bound an abandoned
# container on a metered subscription. A plain module constant like its frozen neighbours above
# — deliberately NOT a Settings field: it is a frozen protocol constant, not deployment config.
RELAUNCH_PREVIEW_STAY_SECONDS = 1800  # 30 min

# --- The wall-clock liveness lease (sandbox key family 4) --------------------
# A build in flight renews `bial:{env}:sandbox:lease:{user_id}` on the cadence below, and
# the reconciliation sweep reads it. It exists because NOTHING else here is legible to a
# process that is not running the build: the heartbeat above is seeded once per turn, so
# ~90 s in the only remaining shield is `sweep_all`'s in-process `live_users` set — empty
# everywhere else.
#
# The TTL is MANDATORY, not a default. The registry hash's own missing TTL is exactly the
# mistake a lease must not repeat: one that never expires is a container that can never be
# reclaimed. It also bounds a lease abandoned by a process that died mid-renewal, and it is
# the ceiling the fail-closed read compares against, so a bad clock cannot buy a millennium.
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

# --- The start-in-flight marker (sandbox key family 5) ----------------------
# A start in flight is a fact the platform holds, not one a tab remembers: `_holding_user_lock`
# (the one skeleton behind the build start, the relaunch, and the turn's `ensure_sandbox`) writes
# this marker for the duration of provisioning, so every tab, every session and a reloaded page
# read the SAME answer instead of each guessing from its own request history.
#
# The TTL is MANDATORY, not a default — a marker with no expiry is the registry hash's own
# mistake repeated in a new key. Bounded by the cold-start budget plus margin for the
# provisioning that runs BEFORE the wait even starts (container create, the snapshot pull, one
# retry of either): `_COLD_READY_BUDGET_SECONDS` (120s) covers only the final `wait_ready` leg,
# and `_RESTORE_ATTEMPTS` (2) means a transient blip can pay that leg's setup twice. 300s (5 min)
# is double the wait budget plus that margin, and still well inside `LOCK_TTL_SECONDS` (900s) —
# the marker is a bounded claim on top of the lock, never a longer-lived one.
STARTING_MARKER_TTL_SECONDS = 300  # 5 min

# --- What the generated app actually served ------------------------------
# Requests the app served to real users buy a BOUNDED extension, never indefinite life.
# Shorter than a deliberate builder action, because it is weaker evidence of intent: a
# left-open app tab polling in the background is still traffic, and nobody is working.
# Bounded means bounded — each report buys this much from now, and a container with
# nothing but background chatter still lapses inside the idle band.
SERVED_TRAFFIC_STAY_SECONDS = 900

# --- A turn that changed nothing ----------------------------------
# The WEAKEST evidence of the four `DeadlineWriter`s, deliberately: it is pure keyboard, with
# nothing on the container side to show for it — no file changed, no tool ran that could have.
# Bounds the cost of pinning the whole workspace every turn — both chat kinds do it — without a
# branch on kind: a Plan-kind chat's ordinary Q&A, or a Build-kind chat's question that wrote
# nothing, both land here. Long enough to read the reply and ask a follow-up without paying a cold
# restore on the very next message; far short of the stay a write or a deliberate action earns
# (`RELAUNCH_PREVIEW_STAY_SECONDS`/`SERVED_TRAFFIC_STAY_SECONDS` above). Monotonic extension
# (`grant_stay_of_execution`'s `max(existing, computed)`) is what keeps this from ever SHORTENING a
# longer stay a prior write turn already bought — see `locks.py`.
TURN_ENDED_UNCHANGED_STAY_SECONDS = 300  # 5 min

# --- The shared-runtime view's absolute session ceiling (#198) --------------
# Independent of `DeadlineWriter.APP_SERVED_TRAFFIC`'s renewable stay above, so a wedged or
# spoofed supervisor report can never buy a shared view immortality: every renewable signal in
# this file bounds how long a container survives WITHOUT proof of use, and this bounds how long
# it may survive no matter how much proof arrives. Four hours — the same scale reclaim.py's own
# `REAL_APP_AGE` uses for "this has run long enough that it is worth a fresh look" — long enough
# that a colleague reading through a shared app across a working session is never cut off
# mid-read by a number nobody chose on purpose, short enough that a spoofed or wedged traffic
# report cannot keep a container alive indefinitely.
SHARED_PREVIEW_ABSOLUTE_CEILING_SECONDS = 4 * 60 * 60  # 4 hours


class PreviewLifeState(enum.StrEnum):
    """What is (or is not) serving a project's preview right now. An **API** StrEnum like
    `BuildSessionStatus`, not a native PG enum — wire value equals the member's lowercase name.

    Exists because `preview-state` once collapsed four different situations into one `alive:
    false`, including a registry-read ERROR misread as "gone" — pulling a live app off screen.
    UNKNOWN keeps its own member, never folded in, for the same reason `SaveState.dirty` is
    tri-state: never give the reassuring answer for someone else."""

    # A container is SERVING this project — served, not merely scheduled — and `preview_url` is
    # framable. THE WORDING HERE DID NOT CHANGE WITH THE SERVING PROOF; THE CODE FINALLY MATCHES
    # IT. This member has always said "is serving", but until the registry hash carried a
    # `serving_since` stamp the platform answered it from `state == ready`, which means an ACA
    # container was CREATED. On 2026-09-10 that gap measured eight seconds in which a citizen's
    # pane framed nginx's "This app isn't running right now" page while the live region
    # announced "Your app preview is live". ALIVE now additionally requires the stamp, written
    # only where something watched the app answer a request.
    #
    # THE MONOTONICITY INVARIANT — the one a future editor must not "optimise" away: for any one
    # container, `alive` is emitted no EARLIER than the pre-stamp code emitted it. Same instant
    # or later, never sooner. That property is what licenses deploying this backend with no
    # lockstep portal deploy: an already-loaded tab reads `starting` where it used to read a
    # premature `alive`, and `starting` is a reading those tabs already withhold the frame on.
    # Relaxing the stamp check — "ready is good enough on the fast path" — breaks the invariant
    # silently and reopens the eight seconds, which is why it is written down beside the member
    # rather than left in a plan document.
    #
    # WHAT THE INVARIANT DOES NOT SAY, stated plainly because a naive reading of it forbids
    # something this design does on purpose: it is NOT a latch. `clear_serving` retracts the
    # stamp on the debounced crash edge, so one container can read ALIVE and later read
    # STARTING. That is a claim being WITHDRAWN once it stopped being true — the thing the old
    # code could never do, and the reason an app that died after serving used to go on reading
    # ALIVE over a 404 forever. The invariant governs the FIRST edge only: nothing may make the
    # first `alive` for a container arrive sooner than it arrived before the stamp existed.
    ALIVE = "alive"
    # Built before, nothing serving it now. The next prompt brings it back from the durable
    # copy on Blob. NOT an error, NOT a loss — which is why no surface may style it as one.
    ASLEEP = "asleep"
    # NOTHING IS PROVEN TO BE SERVING THIS PROJECT YET, and TWO situations reach that one word.
    # (1) A build start, a relaunch or a turn's `ensure_sandbox` is IN FLIGHT right now: the
    # `starting` Redis marker names this project. (2) THE COVERAGE THE SERVING PROOF ADDED — the
    # container EXISTS but has never answered a request: the registry names this project's app
    # with `state = ready` and its `serving_since` stamp is still the empty sentinel. Arm (2) is
    # where the measured 48s→56s window now lives; it used to answer ALIVE, hand out a preview
    # URL, and let a tab frame a page that was not up.
    #
    # Still not `alive` (nothing has been watched to serve) and not `asleep` (something is
    # actively under way) — a citizen watching this deserves a third word, not one of the other
    # two stretched to also mean this. The two arms deliberately do NOT split into two members:
    # the citizen does the same thing in both (wait), no surface would draw them differently,
    # and the engineering difference between them is already named in the log by
    # `sandbox_registry_marked_pending` and `app_first_served`. A member nobody renders
    # differently is a member that only gives two code paths a way to disagree about one wait.
    STARTING = "starting"
    # Another of this user's projects holds the one-per-user workspace. `occupying_project_name`
    # names it, or is null when the live container matches no app this user owns (a ghost —
    # say nothing rather than guess a name into a sentence about someone's work).
    SLOT_TAKEN = "slot_taken"
    NEVER_BUILT = "never_built"  # no app row: nothing was built here, so nothing can serve it.
    # The coordination store could not be read. Claims NOTHING in either direction; a client
    # that renders this as "gone" has reintroduced the bug this enum was written to kill.
    UNKNOWN = "unknown"


class PreviewStateAction(enum.StrEnum):
    """What a citizen may be OFFERED for a `PreviewLifeState` — kept as data, not a docstring
    claim, because this path has a recorded data-loss incident. THREE BUCKETS: `RETRY` (never
    destructive, safe even on an ambiguous read); `NEITHER` (nothing to offer); `REMEDY`
    (consequential — releases ANOTHER project's container via `release_project_sandbox`, only
    on a CONFIRMED occupying-project fact, never a guess). THE RULE A TEST ENFORCES: `UNKNOWN`
    maps to `RETRY`, never `REMEDY` — an ambiguous or timed-out read must never route to a
    consequential remedy, the discipline missing when a readiness TIMEOUT was once read as
    death and routed straight into teardown-then-restore."""

    RETRY = "retry"  # try again; by construction this can never destroy anything.
    REMEDY = "remedy"  # a specific, nameable fix exists — and it may be consequential.
    NEITHER = "neither"  # nothing to offer: already settled, or nothing exists to act on.


PREVIEW_STATE_ACTION: Final[Mapping[PreviewLifeState, PreviewStateAction]] = {
    PreviewLifeState.ALIVE: PreviewStateAction.NEITHER,
    PreviewLifeState.ASLEEP: PreviewStateAction.RETRY,
    PreviewLifeState.STARTING: PreviewStateAction.NEITHER,
    PreviewLifeState.SLOT_TAKEN: PreviewStateAction.REMEDY,
    PreviewLifeState.NEVER_BUILT: PreviewStateAction.NEITHER,
    PreviewLifeState.UNKNOWN: PreviewStateAction.RETRY,
}
"""A TOTAL FUNCTION over the enum, by construction rather than by convention: a `PreviewLifeState`
added later with no entry here raises `KeyError` on lookup rather than silently rendering a button
whose meaning nobody chose (`tests/api/v1/build_sessions/test_preview_state.py`
asserts every member is present). See `PreviewStateAction` for what each bucket may do, and for the
one rule this mapping exists to enforce: `UNKNOWN` never maps to `REMEDY`."""


# --- Control operations: stop / status ---------------------------------------
#
# THE START ROUTE IS GONE and these two shapes outlive it. The bare `POST` on the build-sessions
# collection was deleted with the whole harness behind it — it had had no browser client for a
# long time before the deletion. `StartBuildRequest` stays because `test_import_graph.py` freezes
# this package's schema re-export set at this location and imports it by name; `StartBuildResponse`
# stays beside it so the pair documents the wire shape the transcript's surviving `build_started`
# rows were written against. Neither is served by any route, and neither should grow a field.


class StartBuildRequest(CamelModel):
    """The body the deleted start route took. NO ROUTE ACCEPTS IT — kept as the frozen schema
    re-export `test_import_graph.py` pins."""

    project_id: uuid.UUID  # REQUIRED — project-first; no lazy Default project (never reintroduce).
    prompt: str  # the citizen-dev's natural-language build instruction for this turn (non-empty).
    # OPTIONAL, back-compat: the thread whose attachments ground this build (`conversationId`
    # on the wire). Present → the server materializes that conversation's file parts into the
    # agent's prompt (images/PDF as vision, office/csv as fenced extracted text); absent → a
    # text-only build, byte-identical to the behaviour before this field existed. Attachments
    # travel by REFERENCE, not payload: the portal already persisted the parts before calling
    # start, so the bytes need no second trip through the browser. Amends a frozen request
    # body — additive and optional. Owner- AND project-scoped at resolution: a conversation that is
    # not the caller's, or belongs to a different project than `project_id`, is a non-leaking 404
    # (a build must never be grounded in another project's files).
    conversation_id: uuid.UUID | None = None


class StartBuildResponse(CamelModel):
    """The 201 the deleted start route returned. NO ROUTE PRODUCES IT — and with it went the last
    live producer of a session id the browser could hold. What still reaches the portal is a
    `build_started` transcript row written before the deletion; those rows are permanent, which
    is why `status`/`stop`/`events` survive as their reader."""

    session_id: uuid.UUID  # the build-session id — path key for status/stop/SSE.
    project_id: uuid.UUID
    app_id: uuid.UUID  # the app_registry row being built (== BIAL_APP_ID). Fresh per project.
    status: BuildSessionStatus  # always `provisioning` on a fresh start.
    preview_url: str | None = None  # always null here (dev server not up yet); set once `ready`.
    created_at: datetime


class RelaunchPreviewRequest(CamelModel):
    """`POST /v1/build-sessions/relaunch` body. Project-scoped, not session-scoped: the
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
    """`POST /v1/build-sessions/relaunch` → 200. No `session_id`/`created_at`: relaunch
    registers NO in-process build session (it must not occupy the build slot), so
    there is nothing to poll or stop.

    `preview_url` is always framable; `ready` says whether it is SERVING yet. The two came apart
    when relaunch stopped 503ing on a slow app: an attached container whose root route
    outruns the readiness budget still hands back its URL, because the alternative — condemning
    the container — cost a citizen their unsaved work."""

    app_id: uuid.UUID
    # The framable PUBLIC address — `https://<apps-host>/a/<app-name>/`, NOT the container's own
    # ACA fqdn, which an internal environment publishes no public DNS for and a BIAL desk cannot
    # resolve. Live whenever `ready`; on a degraded attach it is the right URL for a server that
    # has not answered yet.
    preview_url: str
    status: BuildSessionStatus  # `ready`, or `provisioning` when the app is not serving yet.
    # The "last saved version" signal: True when the project's NEWEST recorded build
    # outcome was FAILED — `_do_finalize` snapshots pass and fail alike, so the restored
    # workspace is the last SAVED state, not that build's intent. The portal labels the
    # relaunched preview accordingly instead of presenting an unqualified "ready".
    restored_from_failed_build: bool
    # Is the app SERVING the URL above yet? False only on the attach arm's fail-open path — the
    # container is alive and holds the user's work, the app is just slow to answer. The portal
    # frames the URL either way and keeps its labelled wait up until the frame loads. Defaulted
    # so an older client that ignores the field reads the historic "relaunch returns ready".
    #
    # UNCHANGED BY THE SERVING PROOF, AND DEFINITIONALLY THE SAME FACT AS A PROVEN
    # `serving_since` STAMP — say it here so the two cannot drift. `wait_ready` returning
    # without `SandboxNotReadyError` is what sets this True, and that IDENTICAL success arm is
    # where `relaunch_preview` stamps the registry. Anyone who makes one of the two conditional
    # has to make the other conditional in the same commit, or this wire field says "ready"
    # while the preview-state poll goes on answering `starting` about the same container.
    #
    # What changes is only who MINTS A STATE from it. `ready: false` is no longer a card of its
    # own on the pane: it leaves the citizen waiting and lets the poll — which now reads the
    # stamp — be the single authority on what is serving. The field itself stays because old
    # clients read it and the backend's own start-success numerator is gated on it.
    ready: bool = True


class SharedPreviewResponse(CamelModel):
    """`POST /v1/build-sessions/projects/{projectId}/shared-launch` and `.../shared-refresh`
    → 200 (#198). `RelaunchPreviewResponse`'s sibling for a colleague's read-only view of a
    project shared with them — no `session_id`/`status`/`restored_from_failed_build`: a shared
    view registers no build session, has no build-outcome history of its own to qualify, and
    `ready` alone says whether the frame is serving yet."""

    app_id: uuid.UUID
    preview_url: str
    ready: bool
    # When the snapshot NOW BEING SERVED was saved — the answer to "how current is what I'm
    # looking at", which only matters here: a builder's own relaunch is always their newest
    # work, but a colleague's view is frozen at whatever the owner last saved, and Refresh's
    # entire point is moving this forward. `None` only when the store could not be asked for
    # the timestamp; the restore itself already confirmed the snapshot exists.
    snapshot_taken_at: datetime | None


class StopBuildRequest(CamelModel):
    """`POST /v1/build-sessions/{sessionId}/stop` body."""

    reason: str | None = None  # optional free-text reason for the audit/activity feed.


class StopBuildResponse(CamelModel):
    """`POST /v1/build-sessions/{sessionId}/stop` → 200."""

    session_id: uuid.UUID
    status: BuildSessionStatus  # `ended` after a graceful stop.


class BuildSessionStatusResponse(CamelModel):
    """`GET /v1/build-sessions/{sessionId}` → 200. The poll surface and the
    source of the framable `preview_url`."""

    session_id: uuid.UUID
    project_id: uuid.UUID
    app_id: uuid.UUID
    status: BuildSessionStatus
    # The PUBLIC address the portal frames — prefixed with the app's key. It is NOT the sandbox
    # `next dev` root: the control plane reaches that directly and privately, and the two are
    # deliberately different hosts. Null until `ready`.
    preview_url: str | None
    last_seq: int | None  # highest envelope `seq` so far; a client resumes SSE from it.
    created_at: datetime
    updated_at: datetime


# --- Lock operations: none left ------------------------------------------------
# `acquire` / `renew` / `release` / `heartbeat` were retired along with their response models
# (`LockStateResponse`, `LockReleaseResponse`, `HeartbeatResponse`) — the portal's keep-alive
# loop that was their only caller was itself deleted, and nothing else ever called these
# routes. `force-end` was the sole survivor and its route is now gone too: it had had no
# control on any surface since the block banner's Force-end button went, which both
# `buildSessionApi.ts` and `useBuildSession.ts` said in their own comments.


class ForceEndResponse(CamelModel):
    """The 200 the deleted force-end lock op returned. NO ROUTE PRODUCES IT.
    `SessionManager.force_end` itself survives — it is one of the two entry points into the
    end sequence and carries the terminal-commit race invariant its service tests pin — but
    nothing calls it any more, and retiring it is a separate change that reaches into
    `_do_finalize`'s `force_ended` arms."""

    session_id: uuid.UUID
    status: BuildSessionStatus  # `ended`.


# --- the app's own client-error report ----------------
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
    """`POST /v1/build-sessions/projects/{projectId}/workspace-check` → 200. Does the container
    still hold this app? — asked by an IDLE tab, so a reversion is caught at the preview poll
    rather than waiting for a turn that may never come.

    `reverted` IS THE WHOLE ANSWER — a separate field from `state`, not something the client
    derives. Deriving `state !== "intact"` would retract a completion claim on the two states
    that mean "we could not tell", the mistake this verdict is built to make impossible. `state`
    is for the operator surface and diagnosis only."""

    state: WorkspaceState
    reverted: bool


class CompileStateResponse(CamelModel):
    """`GET /v1/build-sessions/projects/{projectId}/compile-state` → 200.

    The compile signal for a tab with NO LIVE TURN — during a turn it arrives on the turn stream
    as a `compile` frame; once the turn ends that producer stops, so a reloading tab would
    otherwise have nothing to cover a broken preview with.

    `unknown` is a real answer: the caller must HOLD its current cover, never read it as clean.
    It is what no live container, no app row, or a container predating the signal all answer."""

    state: CompileState


class ClientErrorReportRequest(CamelModel):
    """`POST /v1/build-sessions/projects/{projectId}/client-error` body.

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
    """`POST /v1/build-sessions/projects/{projectId}/client-error` → 202.

    `recorded: false` is a SUCCESS, and it is the one thing worth saying here: this app already
    has as many reports waiting for the next health verdict as the store keeps, so this one was
    dropped. Answering an unqualified 202 would tell a crash loop that all four hundred of its
    copies were collected, and would leave a client with no way to see it is being throttled."""

    recorded: bool


# =============================================================================
# Brain interface + tagged-union progress envelope
# =============================================================================
#
# Snake_case field names + snake_case `type` literals, NO camelCase alias generator
# on purpose: the envelope is a streaming frame whose keys must be byte-stable
# across BRAIN-emit → SESSION-API-relay → the pinned schema test → portal-consume.


class ErrorSource(enum.StrEnum):
    """The self-heal error origin. Shared by `ErrorEvent`,
    `EscalationEvent.last_error`, and `BuildResult.error`."""

    TSC = "tsc"  # `tsc` typecheck failure, read over the supervisor's /exec.
    NEXT_BUILD = "next_build"  # `next build` failure, read over the supervisor's /exec.
    SERVER = "server"  # dev-server stderr, read over the supervisor's /dev/logs.
    # The browser client-error arm. Its REPORT stays agent-only (see `agent_only_detail`): it
    # still reaches the agent channel (`build_repair_prompt` acts on it, a repair run follows)
    # and the health verdict (`outcome.error` carries it unchanged), but the one emit site —
    # `turns/engine.py` — skips the `DiagnosticFrame` emit for this source on purpose, so it is
    # NOT rendered to the citizen today. It still gets a real citizen-facing sentence + action in
    # `errors.user_facing` (not a placeholder), so that if this ever gets rendered, the copy
    # already speaks product language rather than a JS stack trace.
    CLIENT = "client"


class BuildError(BaseModel):
    """The structured, self-heal-relevant error shape — `{source, title, cleaned_stack}`,
    reused by the `error` envelope, `escalation.last_error`, and `BuildResult.error`.

    THIS SHAPE IS THE MODEL'S — deliberately left alone. `title` is the compiler's own first
    line, which made rendering it the most developer-looking thing a citizen read; the fix
    was to stop RENDERING it, not soften it. Citizen text lives in `errors.user_facing` on
    `DiagnosticFrame`, so these two fields stay byte-identical and self-heal reads exactly what
    it always did — do not make them friendlier, or the repair prompt breaks."""

    model_config = ConfigDict(extra="forbid")

    source: ErrorSource
    title: str  # short human summary (first meaningful error line).
    cleaned_stack: str  # de-noised diagnostic BRAIN feeds back into the self-heal prompt.
    # The AGENT-ONLY half of a deliberately dual-purpose object. `BuildError` is read by two
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
    """Shared envelope base: every progress event carries the monotonic `seq`. Extra
    keys are forbidden so a mis-shaped payload fails discrimination loudly."""

    model_config = ConfigDict(extra="forbid")

    seq: int  # per-session, starts at 1, strictly +1, gap-free. The SSE `id:` cursor.


class StepEvent(_ProgressEventBase):
    """`step` — a high-level phase marker for the activity feed."""

    type: Literal["step"] = "step"
    name: str  # stable-ish step id, e.g. "scaffold" | "install_deps" | "dev_start" | "self_heal".
    label: str  # human one-liner, e.g. "Installing dependencies…".
    state: Literal["started", "ok", "failed"]  # drives the UI spinner → check/cross.
    # Read-only + housekeeping steps are dropped from the VISIBLE feed (the raw command
    # still reaches the model). Additive + defaulted, so emitters written before this field
    # existed stay wire-valid.
    hidden: bool = False


class ErrorEvent(_ProgressEventBase):
    """`error` — the structured error BRAIN reacts to; carries `{source, title,
    cleaned_stack}`. Currently only tsc | next_build | server are emitted."""

    type: Literal["error"] = "error"
    source: ErrorSource
    title: str
    cleaned_stack: str


class PreviewReadyEvent(_ProgressEventBase):
    """`preview_ready` — the dev server is live and framable; carries `preview_url`.
    Flips the session status → `ready` and triggers the portal iframe (re)load."""

    type: Literal["preview_ready"] = "preview_ready"
    # The PUBLIC address — `https://<apps-host>/a/<app-name>/`. Handed straight to the portal's
    # iframe, so it must be the address a browser can actually resolve, never the container's.
    preview_url: str


class PreviewReconnectingEvent(_ProgressEventBase):
    """`preview_reconnecting` — the dev-server PROCESS exited (the port closed) AFTER the preview
    was already framed. A feed-only status SIGNAL, not a lifecycle transition: the
    `BuildSessionStatus` enum is frozen at five members with no "reconnecting" state, so this never
    changes the session status (a completed build stays `ended`, a live one stays `ready`). The
    portal reads it to show a DISTINCT reconnecting visual — never the "building" spinner — over
    the now-dead frame, and a following `preview_ready` re-frames once the dev server serves. The
    FRONTEND cannot originate this: `/dev/status` is supervisor-internal + bearer-guarded, so crash
    detection is backend-only (the early readiness watcher owns it)."""

    type: Literal["preview_reconnecting"] = "preview_reconnecting"


class EscalationEvent(_ProgressEventBase):
    """`escalation` — the self-heal loop gave up; a human or next turn must intervene.
    Informational; the terminal boundary is the following `ended`."""

    type: Literal["escalation"] = "escalation"
    reason: str  # machine-ish code, e.g. "self_heal_budget_exhausted".
    detail: str  # human explanation for the activity feed.
    last_error: BuildError | None = None  # the final error that triggered escalation, or null.


class QuotaExceededEvent(_ProgressEventBase):
    """`quota_exceeded` — the per-user daily token cap was hit at a model step.
    BRAIN emits this, then gracefully ends."""

    type: Literal["quota_exceeded"] = "quota_exceeded"
    limit: int  # the effective daily cap (from DailyTokenLimitExceededError.limit).
    used: int  # tokens used today (from .used).
    resets_at: str  # next IST-midnight, UTC ISO-8601 (gate.next_ist_midnight_iso).


class EndedEvent(_ProgressEventBase):
    """`ended` — the terminal envelope. After it, the SSE feed emits
    `data: [DONE]\\n\\n` and closes. `status` equals `BuildResult.status`, and exactly one is
    emitted per session."""

    type: Literal["ended"] = "ended"
    # Narrowed to the two terminal members: a terminal frame carrying a non-terminal status
    # (e.g. `building`) must fail validation, not slip through.
    status: Literal[BuildSessionStatus.ENDED, BuildSessionStatus.FAILED]
    preview_url: str | None = None  # the final live preview URL, or null if it never came up.
    snapshot_committed: bool  # True if the snapshot pushed before end.
    reason: str  # "completed" | "stopped_by_user" | "idle_teardown" | "quota_exceeded" | …


ProgressEnvelope = Annotated[
    StepEvent
    | ErrorEvent
    | PreviewReadyEvent
    | PreviewReconnectingEvent
    | EscalationEvent
    | QuotaExceededEvent
    | EndedEvent,
    Field(discriminator="type"),
]
"""The tagged-union progress envelope — seven members, discriminated on `type`.
BRAIN emits one per `await on_progress(env)`;
SESSION-API relays each over the SSE feed verbatim (snake_case, `seq` preserved).

A later cleanup retired the `log` member: no production BRAIN path had ever called the
emitter's `log` helper (a dead-code audit finding, not a behavior change), so removing it
drops the portal's unreachable raw-output consumer arm along with it."""


class BuildResult(BaseModel):
    """BRAIN's structured terminal verdict, returned to SESSION-API
    **in-process** (never serialized to the wire).

    This — NOT an envelope — is how BRAIN's completion travels back, so `status` / `reason` /
    `preview_url` here are the source the terminal frame's fields are built from."""

    model_config = ConfigDict(extra="forbid")

    # Terminal state, narrowed to the two absorbing members — becomes the `ended` envelope's
    # `status`; a non-terminal value (e.g. `building`) fails validation.
    status: Literal[BuildSessionStatus.ENDED, BuildSessionStatus.FAILED]
    reason: str  # becomes the `ended` envelope's `reason`: "completed" | "quota_exceeded" | …
    app_id: uuid.UUID  # the built app (app_registry.id == BIAL_APP_ID).
    preview_url: str | None = None  # the live preview URL if the dev server came up, else None.
    last_seq: int  # the final envelope `seq` emitted — reconciles the feed + `status.last_seq`.
    # ALWAYS False by construction — this value is taken before the snapshot runs. NEVER read it
    # as the answer to "was the work saved?": only the terminal `ended` frame carries that. Kept
    # solely so the frozen verdict shape keeps its field.
    snapshot_committed: bool
    error: BuildError | None = None  # populated on `failed`; None on a clean end.


# --- The run_build interface -----------------------------------------

ProgressSink = Callable[[ProgressEnvelope], Awaitable[None]]
"""The in-process async sink SESSION-API supplies. BRAIN `await`s it for every
envelope it emits — this IS the transport (an `asyncio.Queue` put), never Redis."""


class RunBuild(Protocol):
    """The frozen BRAIN entry point — a callable Protocol BRAIN's orchestrator
    implements; imported READ-ONLY here. Exactly four parameters; the
    `prompt` is intentionally NOT one of them (how BRAIN obtains the instruction is a
    SESSION-API↔BRAIN internal, outside this frozen surface).

    `sandbox_client` is the `SandboxClient` ABC (BRAIN calls the exec/files/dev
    subset through it); `on_progress` is the `ProgressSink`.
    """

    async def __call__(
        self,
        session_id: uuid.UUID,  # the build session (the SSE feed key). Identifies the run.
        user_id: uuid.UUID,  # the session OWNER — all metering is charged here
        sandbox_client: SandboxClient,  # the client ABC instance. Imported READ-ONLY.
        on_progress: ProgressSink,  # the in-process sink for every emitted envelope.
    ) -> BuildResult: ...


BillingSessionFactory = async_sessionmaker[AsyncSession]
"""BRAIN's per-model-step metering session factory. Because `run_build`'s
signature is frozen at four params, the factory is a **construction-time dependency**
of BRAIN's orchestrator (not a `run_build` argument): BRAIN opens its own
`AsyncSession` per model step from it and OWNS the commit — `record_usage` does not
commit, and there is no request-scoped `get_db` on a background task. Tests bind it to
the rolled-back test session — the same substitution the conversation suites make via
`dependency_overrides` on `_shared.billing_session_factory`.
"""


class ParkedTree(CamelModel):
    """One tree set aside instead of promoted: a `quarantine` is what a restore was about to
    write over, a `divert` is what the recovery guard refused to promote. Both live under
    per-occurrence keys, so a later one never overwrites an earlier one."""

    key: str
    kind: Literal["quarantine", "divert"]
    head_sha: str | None
    size_bytes: int
    taken_at: datetime | None


class ParkedTreesResponse(CamelModel):
    """`POST /v1/build-sessions/internal/apps/{app_id}/parked` → 200.

    THE TREES WOULD OTHERWISE BE WRITE-ONLY: no reader, no retention, no runbook. In a
    false-`REVERTED` case those objects hold the only copy of a citizen's newest work, so this
    response must not reproduce that write-only shape. Newest first, because the useful one is
    almost always the last one."""

    trees: list[ParkedTree]


class PromoteParkedRequest(CamelModel):
    """`POST /v1/build-sessions/internal/apps/{app_id}/promote` — put one parked tree back.

    The key is named explicitly rather than "the newest": an operator promoting the wrong tree
    over somebody's recovery slot is the failure, and a request that
    cannot name what it means is one that can be misread."""

    key: str


class PromoteParkedResponse(CamelModel):
    """What the promotion did. `promoted` is False when the guard refused it — which is not an
    error and must not read as one: it means the tree is not a descendant of what the slot holds,
    and forcing it would be the data loss the guard exists to prevent."""

    promoted: bool
    detail: str
