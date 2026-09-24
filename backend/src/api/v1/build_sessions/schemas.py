"""Build-session schemas — the frozen control surface, plus the self-heal error shape.

The portal↔session-API control API (`BuildSessionStatus` and the request/response bodies)
crosses the JSON wire, so it subclasses `CamelModel` (snake_case ⇄ camelCase). The
status is an API `StrEnum`, not a native PG enum — no durable row is persisted.

`BuildError` is the self-heal error shape, classed by `ErrorSource`: read in-process by the
repair prompt and the deploy failure path, so it subclasses plain `BaseModel` with no alias
generator rather than `CamelModel`.
"""

from __future__ import annotations

import enum
import uuid
from collections.abc import Mapping
from datetime import datetime
from typing import Final

from pydantic import BaseModel, ConfigDict, Field

from src.core.integrity_types import WorkspaceState
from src.schemas import CamelModel
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
    BUILDING = "building"  # the agentic build loop is executing model steps + self-heal.
    READY = "ready"  # dev server serving; set on `RelaunchPreviewResponse.status`.
    ENDED = "ended"  # terminal, GRACEFUL: user stop / idle-teardown / quota. Not a failure.
    FAILED = "failed"  # terminal, UNRECOVERABLE: self-heal exhausted / unrecoverable error.


# --- Frozen lock TTL + cadence constants -------------------------------------
# The lock and the heartbeat have no browser renewal surface: they are renewed in-process, by
# `locks.py` and `turns/engine.py`. A browser renews ONE thing and one only
# — the preview's stay of execution, through `projects/{project_id}/renew`, bounded by the
# absolute ceiling below so a tab left open cannot make a container immortal.

LOCK_TTL_SECONDS = 900  # 15 min — lock auto-expires if not renewed (the reaper reconciles).
# THE TWO CADENCES BELOW HAVE NO RUNTIME READER LEFT, and saying so is the point: they were
# written for the browser that renewed on a timer, and that caller is gone. The turn engine's
# liveness-lease loop (`turns/engine.py::_hold_liveness_lease`) is the only renewer left: it
# calls `renew_lock` + `write_heartbeat` once per `LIVENESS_LEASE_RENEW_CADENCE_SECONDS` — 30 s,
# comfortably inside the 90 s heartbeat TTL below — and that is where the real wall-clock
# cadence lives.
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
TURN_ENDED_STAY_SECONDS = 300  # 5 min

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

# --- A surface that is holding the project open ------------------------------
# PRESENCE RENEWS; SILENCE IS DEPARTURE. Every screen that can frame a project already polls
# `preview-state`; on that same tick it renews this stay. Navigating away, closing the tab,
# sleeping the machine and losing the network all stop the renewals, so all four become one
# event with no code of their own and nothing to fail to arrive.
#
# TWO BUDGETS, BECAUSE A HIDDEN TAB IS NOT A RELIABLE CLOCK. Chrome throttles background timers
# hard, Edge sleeps tabs by default after minutes, Safari suspends them — so a hidden surface
# asks for a longer budget and renews on waking as well as on its tick. A visible surface is
# renewing every 45 seconds and needs no more than the grace a departure earns.
#
# Both are UNDER `RELAUNCH_PREVIEW_STAY_SECONDS`, and that is a hard constraint rather than a
# coincidence: `locks.py::stay_of_execution_is_current` reads any deadline beyond that bound as
# absurd and fails closed, so a presence budget above it would spare nothing at all.
SURFACE_PRESENT_STAY_SECONDS = 300  # 5 min — the grace a departure earns.
HIDDEN_SURFACE_PRESENT_STAY_SECONDS = 1200  # 20 min — a throttled tab's budget.


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


class RelaunchPreviewRequest(CamelModel):
    """`POST /v1/build-sessions/relaunch` body. Project-scoped, not session-scoped: the
    old build session is long gone (~5 min after teardown), but `app_id` is durable and
    resolved from the project."""

    project_id: uuid.UUID  # REQUIRED — the owning project; the app is resolved from it.


class DiscardRequest(CamelModel):
    """`POST /v1/build-sessions/projects/{project_id}/discard` body. `conversation_id` names the
    chat the Discard was pressed in, so its line comes back in the answer; absent on the project
    page."""

    conversation_id: uuid.UUID | None = None


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
    # outcome was FAILED. Nothing about a failed verdict withheld the snapshot of the day, so
    # the restored workspace is the last SAVED state, not that build's intent. The portal labels
    # the relaunched preview accordingly instead of presenting an unqualified "ready".
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


class SurfacePresence(enum.StrEnum):
    """Whether the surface asking for a renewal is on screen, which is the whole of what the
    server needs to pick a budget.

    The CLIENT says which; the SERVER maps it to seconds. A client that named its own number
    would be a client that could ask for a longer life than the ceiling permits."""

    VISIBLE = "visible"
    HIDDEN = "hidden"


class RenewalOutcome(enum.StrEnum):
    """THREE NAMED STATES, never a boolean, following `StopActiveBuildResponse`.

    A `renewed: false` collapses "your container is gone" into "the container answering for this
    user is a different project's" — and the second is the ordinary reading a moment after
    somebody opens another project, where nothing is wrong and nothing should be said."""

    #: The stay was pushed forward. The container is this project's and it is holding.
    RENEWED = "renewed"
    #: A container is registered for this citizen, but it is serving a DIFFERENT project. Nothing
    #: was written — the other project's container is not this surface's to hold open.
    NOT_THIS_CONTAINER = "not_this_container"
    #: No registry record at all. Nothing is running to renew, and no hash is conjured to say so.
    NOTHING_RUNNING = "nothing_running"


class RenewPresenceRequest(CamelModel):
    """`POST /v1/build-sessions/projects/{projectId}/renew` body.

    Carries the surface's visibility and NOTHING ELSE — above all, no container name. The server
    resolves which container this project owns from its own app row; a name on the wire would be
    a name a caller could substitute."""

    presence: SurfacePresence = SurfacePresence.VISIBLE


class RenewPresenceResponse(CamelModel):
    """`POST /v1/build-sessions/projects/{projectId}/renew` → 200.

    Every outcome is a 200. None of the three is an error: the client re-arms its poll on the two
    that are not `renewed` and renders nothing, because a lease that genuinely lapsed surfaces
    through `preview-state`, which is the one route allowed to say a preview is gone."""

    outcome: RenewalOutcome
    #: When the stay now lapses, or `None` when nothing was renewed. The client does not display
    #: it; it is what makes a renewal auditable from a response body.
    stay_until: datetime | None = None


# =============================================================================
# Self-heal error shape
# =============================================================================


class ErrorSource(enum.StrEnum):
    """The self-heal error origin, carried on `BuildError.source` and the turn stream's
    `DiagnosticFrame.source`."""

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
    read by the self-heal repair prompt and the deploy failure path.

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
    # audiences with opposite needs: the model, through `build_repair_prompt`, and the citizen,
    # through the diagnostic it renders. For a `client`-class report those two must diverge — the
    # text was written by code inside the generated app, so it may reach the model (which can act
    # on it) and must not reach the user (for whom a JS stack trace is not a product surface).
    #
    # `exclude=True` is what makes that structural instead of a convention: the field is dropped
    # from EVERY serialization, so nothing built from this shape can carry it out by simply
    # forgetting about it. `build_repair_prompt` reads the attribute in-process, which is the
    # only path that sees it at all.
    #
    # Absent (None) on every other source, where `cleaned_stack` is already safe to render and the
    # repair prompt uses it unchanged — so nothing about the tsc / server / next_build arms moves.
    agent_only_detail: str | None = Field(default=None, exclude=True)
