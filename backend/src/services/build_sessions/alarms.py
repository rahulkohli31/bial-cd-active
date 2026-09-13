"""Pinned structlog event names for the build-harness alarms.

There is no metrics system in this deployment, so an alarm is a GREPPABLE EVENT CONSTANT an
external log rule keys on, plus a relational record where the outcome must be counted rather
than merely noticed. This module gathers the harness's own so they stop being scattered.

THE ONE RULE: each name appears exactly ONCE in the codebase. An alert cannot be written against
a string that exists in two spellings, and the second spelling is invisible until the day it is
the only one firing. Import the constant, never retype the literal — tests included. Reasons that
distinguish one firing from another belong in structured fields, not in the event name.

NOT EVERY NAME BELOW IS AN ALARM. The nine sandbox-lifecycle constants are LIFECYCLE NOTICES —
the handful of `info` lines one build now prints, in order, so an operator handed "a citizen saw
an error page for eight seconds" can reconstruct that build from the logs alone, with no database
query and no in-memory session object (which is evicted five minutes after the turn ends anyway).
Nothing pages on them. They live in this module regardless, because THE ONE RULE above is exactly
what keeps them greppable, and a second home for event names is how a second spelling gets
written.

THE DELETE PATH'S ALARM IS NOT HERE, and the omission is deliberate. The artefact-survived alarm
every teardown arm raises spans `api/v1/projects/`, `services/storage/`, `services/deploy/` and
`services/appdb/`, and `services/storage/` is imported while `src.config` is still initialising —
so importing anything under this package from there is a startup circular-import error. It lives
in `src/core/alarms.py`, a leaf that imports no `src.*` at all; the doctrine above governs it
identically.
"""

from typing import Final

HMR_PROTOCOL_DRIFT_EVENT: Final = "compile_signal_protocol_drift"
"""The compile signal's canary fired: the supervisor connected to the dev server's HMR socket
SUCCESSFULLY and then received nothing it recognised.

This is the one event that separates "the protocol moved upstream" from "the socket is down".
Defensive parsing — ignore unknown frame verbs, never assume a field is present — is what keeps
a bundler upgrade from crashing the consumer, and is EXACTLY what would make a rename silent:
the consumer would receive frames forever and understand none of them, while the platform
reported a healthy app.

THIS DOC USED TO CLAIM "the dev server sends its current state within milliseconds of a connect,
so silence after a successful connect has no innocent explanation". THAT WAS MEASURED FALSE on
2026-09-10, and the correction is kept here rather than quietly deleted because the sentence is
what made a whole day of false alarms look like a real one. There IS an innocent explanation:
on `next@16.3.1` the `sync` frame — the only connect-burst frame that carries a compile state —
is emitted behind an untimed `fetch('https://registry.npmjs.org/-/package/next/dist-tags')` made
from INSIDE the sandbox, memoised per dev-server process, so the supervisor's consumer is the one
client per container that ever pays it. Slow DNS, a throttled container, or blackholed egress
pushed that past the canary's window and this alarm fired against perfectly healthy dev servers,
always at `connect_generation=1`. The supervisor now distinguishes a verb it KNOWS which carries
no compile state (the `turbopack-connected` / `isrManifest` handshake) from a frame it cannot
read at all, and only the latter arms the canary — so a firing here once again means what this
doc says it means. A supervisor baked BEFORE that fix still false-alarms exactly once per
container: check the image tag before chasing a rename.

Fields: `app_name`, `connect_generation`, `reason`. Raised at most once per successful connect
(the generation is what makes that possible) rather than once per poll.

WHAT TO DO: the frame verbs this consumer understands are `building` / `built` / `sync`, read
from `action` or `type`, in `sandbox/supervisor/app.py::_derive_compile` — alongside
`_HMR_STATELESS_VERBS` there, the verbs it knows and expects no state from. Capture a few frames
from a live container's `/_next/hmr` (the path moved off `/_next/webpack-hmr` with Turbopack) and
add the new verb to whichever of the two it belongs in. Until that ships the platform reports
`UNKNOWN`, the preview cover holds rather than clearing, and no user sees a framework error
screen — degraded, not broken."""


RECOVERY_WRITE_DID_NOT_LAND_EVENT: Final = "recovery_write_did_not_land"
"""A turn ended and its work did not reach the recovery slot.

Fires on all THREE ways a turn's work fails to reach the slot, distinguished by `reason` rather
than by three event names — one operational question, one event, filterable by field:

* `refused` — the guard would not promote this tree (an unreadable lineage, a head_sha that is
  not a sha). The existing copy is untouched.
* `diverted` — same refusal, and the bundle was preserved under `divert_key` instead, so the tree
  is recoverable by the operator promote procedure rather than thrown away.
* `failed` — the bundle or the upload itself did not complete. This is the swallowed case, and it
  is raised from the CALL SITE, which is the only place that knows the write raised. THE SWALLOW
  STAYS — a safety net that can fail a turn is not a safety net — so this event is the whole of
  the trace such a failure leaves, and without it nobody can tell a platform that failed to CHECK
  the workspace from one that failed to make it DURABLE.

Fields: `app_id`, `reason`, and — where the guard formed an opinion — `recorded_head` and
`bundled_head`, which together say WHY a tree was refused.

WHAT TO DO: read the app's `divert/{app_id}/` prefix. A `diverted` event means a real tree is
sitting there; `services/build_sessions/snapshot.py::write_recovery_copy` documents the guard that
put it there, and the operator promote endpoint is how it gets moved back."""


WORKSPACE_LOST_WHILE_IDLE_EVENT: Final = "workspace_lost_while_idle"
"""A reversion was caught at the preview poll rather than at a turn.

THE TURN MAY NEVER COME. A container can revert while the citizen is reading, in another tab, or
at lunch, and the workspace is otherwise only ever asked about at the start of a turn — so
without this poll a standing "Build complete" claim sits above a dead app for as long as the tab
stays open.

Distinct from the turn-time reversion, deliberately. The turn-time one is handled — quarantined,
restored, told — inside a flow the citizen is already watching. This one fires with nobody
watching, so it is the operator's only notice that it happened at all, and it is the record that
says how often it happens.

Fields: `app_id` and `app_name` (which container), `last_known_head` (the app's last good
state), `recovery_copy_available` (was there anything to put back), and `verdict` (which of the
four states was reached).

THE CONTAINER'S AGE IS DELIBERATELY NOT HERE: reading it costs an ARM listing, which a poll a
browser tab drives every 45 seconds will not pay. An operator who needs it has the age in the
reclamation pass records, keyed by the same `app_name`.

WHAT TO DO: the citizen has already been told on the preview pane and the standing completion
claim has been retracted, so this is not an emergency page. It is the number to watch. If it
fires more than rarely, the containers are being reclaimed or reset out from under live sessions
and the reclamation policy is what wants looking at, not this code."""


# --- the sandbox build lifecycle, in order ------------------------------------------------
# The nine notices. structlog ONLY — a bare `logging.getLogger` line is dropped on the floor in
# this process — at `info` unless the constant says otherwise, and each one carries its fields IN
# ADDITION to the contextvars bound for the whole build (`build_id`, `user_id`, `project_id`,
# `app_id`, `app_name`). That binding is what makes one build one grep; without it these are nine
# unrelated lines. Never a token, a DSN, or a DSN's password sub-token.


APP_STOPPED_WHILE_IDLE_EVENT: Final = "app_stopped_while_idle"
"""An idle tab's check found an intact app whose dev server is not running, and acted on it.

A DIFFERENT FAULT FROM `WORKSPACE_LOST_WHILE_IDLE_EVENT`: the files are fine and the process is
gone — exited, killed, or never brought back after a restart. Nothing else reports it:
`preview-state` answers from the registry and cannot make a container call, and the integrity
verdict reads the git tree, which a dead process does not change. The citizen was looking at a wait
that could not end.

Fields: `app_id`, `app_name`, `exit_code` (the supervisor's post-mortem of its child, when it had
one) and `put_away` — True when the container was put away, so the next reading offers the saved
app and its start control; False when the durable-copy gate spared it, and the reaper's "not
provably preserved" warning beside this line says why.

READING `exit_code`: the supervisor reports `Popen.poll()`, so a signal death is the NEGATIVE
signal number. `-9` is a SIGKILL that landed on the supervisor's own child; `137` is the same
SIGKILL reported by a shell in between. Both are the out-of-memory killer's usual signature — and
an agent's `pkill -9` looks identical, which is why this names no cause.

WHAT TO DO: nothing for a one-off — the citizen presses Launch and the app comes back running.
Repeats for the same app mean its dev server keeps dying under it: suspect memory first, and the
app's own build is where to look."""


BUILD_WORKSPACE_CLAIMED_EVENT: Final = "build_workspace_claimed"
"""The one-per-user workspace is held and the start-in-flight marker is written.

The first line of a build, and the only one that answers "did this citizen get a slot at all,
and whose slot was it" — today inferrable only backwards, from a later failure.

Fields: `arm` (`build` | `relaunch` | `ensure_sandbox` — which of the three doors into a
container this was), `reclaimed` (did reconcile evict a prior holder to get here: one citizen's
build ending another of their own is invisible otherwise), `lock_wait_ms`."""


SANDBOX_CONTAINER_CREATED_EVENT: Final = "sandbox_container_created"
"""The container exists — the ARM create returned.

The ARM layer is silent today on success AND on failure, so the most expensive step of a build
leaves no trace of how long it took or how many attempts it cost.

Fields: `arm` (`provision_new` | `restore_from_snapshot`), `create_ms`, `attempts`,
`fqdn_present` — the BOOL and not the FQDN, because its presence is the only bit anyone reads
and a half-formed ARM reply is not worth the line width."""


SANDBOX_REGISTRY_MARKED_PENDING_EVENT: Final = "sandbox_registry_marked_pending"
"""The registry hash was hydrated for a container that has not served anything yet.

THE NAME IS THE POINT. The platform used to treat this instant as "the app is running", and the
log said nothing at all — so the lie had no line to be caught on. This one says SCHEDULED, in
those words, and conspicuously does not say serving. Its distance from `app_first_served` below
is the window a citizen spends watching a pane that used to claim otherwise.

Fields: `app_name`, `serving_since` (the empty sentinel, logged verbatim so the reading is on
the record rather than implied by the event name)."""


SANDBOX_DEV_STARTED_EVENT: Final = "sandbox_dev_started"
"""The supervisor accepted `dev_start` and the dev server is coming up.

STILL NOT PROOF THAT ANYTHING SERVED, and the gap between this line and `app_first_served` is
the interesting one: a build that reaches here and stops has a dev server that started and never
compiled a route.

Fields: `arm`, `already_running` (an attach found it up rather than starting it)."""


APP_FIRST_SERVED_EVENT: Final = "app_first_served"
"""THE LINE THAT DID NOT EXIST. Something watched this container's app answer a request, and the
compare-and-set that records it returned 1.

Fields: `app_name`, `serving_since`, `ms_since_container_created` (computed from the registry
hash's own `created_at`, so nobody has to subtract two timestamps by hand — this is the number
the 2026-09-10 measurement had to be reconstructed from a screen recording to get), `observer`
(`turn_watcher` | `turn_verify` | `relaunch_wait` | `relaunch_continuation` | `reconciler` —
WHICH watcher won, the only way to tell a normal build from one the five-minute backstop
rescued), `cold`.

THE VOCABULARY IS WHAT THE CODE EMITS, and it has already drifted once. An `attach_snapshot`
stood here for an observer that was designed and then deliberately NOT built: the attach seam's
`/dev/status` answer is single-flight cached for five seconds, and a five-second-stale
affirmative is too weak to be a permanent first-serve stamp. `turn_verify` was built and was
missing from this list. A value named here that nothing emits sends an operator hunting for a
watcher that does not exist; one emitted but unnamed makes their filter silently drop rows.

Emitted at most once per container by construction: the stamp is first-serve-wins, so a second
observer's refusal raises `SERVING_PROOF_STAMP_REFUSED` and never a second line here."""


APP_FIRST_SERVE_NOT_OBSERVED_EVENT: Final = "app_first_serve_not_observed"
"""A wait ended with no proof: the budget lapsed, the detached continuation gave up, or a turn
finished having never framed a preview. WARNING.

This is what turns "the citizen stared at a wait card" from an unanswerable report into a line.
It is NOT a claim that the app is dead — the reconciler may still stamp it afterwards, and
`app_first_served` carrying `observer=reconciler` is how that shows up.

Fields: `waited_ms`, `budget_ms`, `arm`, `dev_running` and `dev_compile` (the last reading taken
from the supervisor, which is what separates "never started" from "started and never compiled"
from "compiled and never answered")."""


APP_SERVING_LOST_EVENT: Final = "app_serving_lost"
"""A container that HAD served stopped answering, and the standing proof was retracted. WARNING.

The debounced crash edge, at the instant the stamp is cleared. Today that edge pushes an SSE
frame to whoever is watching and leaves nothing behind for anyone who is not.

Fields: `unanswered_polls` (the debounce that was actually met — a lone bad poll is not a dead
app), `exit_code` from the supervisor's post-mortem of the child (137 is the OOM killer, and it
is the most common answer there is), `served_for_ms`."""


PREVIEW_STATE_REPORTED_UNKNOWN_EVENT: Final = "preview_state_reported_unknown"
"""The preview-state read itself failed, so the platform told the citizen nothing. WARNING.

THE LOG IS THE WHOLE RECORD HERE. The pane deliberately does not draw this state — an unreadable
read leaves a standing frame framed and a standing card put, rather than telling a citizen their
working app does not exist — which means that without this line the failure is invisible from
both ends. Rate-limited per user: the caller is a browser timer.

ORDINARY PREVIEW-STATE ANSWERS ARE DELIBERATELY NOT LOGGED, recorded here as a decision so
nobody adds it later. A per-tab poll on two surfaces would drown the stream and tell an operator
nothing that the marked-pending / first-served / serving-lost lines and their timestamps do not
already reconstruct."""


SANDBOX_TORN_DOWN_EVENT: Final = "sandbox_torn_down"
"""A container's life ended cleanly. The clean finish is silent today, so the log holds starts
with no ends and no way to tell a tidy shutdown from a process that simply vanished.

Fields: `reason` (`turn_finalize` | `reap_idle` | `reclaim_for_other_project` | `operator` — one
event, four reasons in a FIELD, per THE ONE RULE), `pardoned`, `lifetime_ms`, and `served: bool`
read off the serving stamp — the most useful retrospective field in the set, because it answers
whether this container was ever any use to anybody at all."""


# --- the two pinned alarms ----------------------------------------------------------------


SERVING_PROOF_ABSENT_AT_TEARDOWN: Final = "serving_proof_absent_at_teardown"
"""A container reached teardown with no serving proof on record. WARNING.

THE EMPTY SENTINEL PROVES NOTHING ON ITS OWN. A container that never served leaves it behind,
and so does one that DID serve and had the proof retracted — the out-of-turn observer clears it,
on this module's own sweep or a relaunch that finds the app up but not painting, and neither one
tears the container down. Both histories reach teardown identically, so this alarm says the proof
is absent and stops there; it does not say the container never served. It is an alarm rather than
merely the `served: bool` field on the teardown line above because a field nobody greps raises
nothing.

Fields: `lifetime_ms`, `reason` (the teardown reason, so the reap of a genuinely idle container
reads differently from a turn that finalised over a dead app).

WHAT TO DO: the container is already gone, so this is not a page — it is the number to watch.
Read the same build's trace for `app_first_served`: present, and the container served before its
proof was retracted; absent, and it never did. If it fires more than rarely with no
`app_first_served` anywhere in the trace, the generated-app template is shipping something that
does not boot, and that is what wants looking at, not this code."""


SERVING_PROOF_STAMP_REFUSED: Final = "serving_proof_stamp_refused"
"""An observer watched an app serve and the compare-and-set REFUSED to record it: the registry
hash was gone, marked `ending`, or named a different container. WARNING.

THE NEAR-MISS OF STAMPING THE WRONG CONTAINER, which is the single most dangerous thing in this
design — the one-per-user slot flipped between the observation and the write, so a slow observer
came back holding evidence about a container that no longer occupies the slot. The
compare-and-set is what turns that into this line instead of into a preview pane framing one
project's app on another project's proof. It gets its own warning-level constant rather than a
`latched: bool` inside an `info` line precisely because nothing pages on a boolean.

Fields: `expected_app`, and `found_app_present` — a BOOL, deliberately never the other project's
container name. The id vocabulary in this log stays user-scoped, and naming the loser would put
one of the citizen's projects into another's build trace.

WHAT TO DO: expected on a race a reclaim or a reap genuinely won — the citizen's own newer build
took their slot — and the pane recovers on its next poll either way, so one firing is not an
incident. Read it beside `build_workspace_claimed`'s `reclaimed` field for the same user: a
refusal with no reclaim anywhere near it means an observer outlived its container without
anything evicting it, and that is a bounded-wait bug in whichever `observer` the neighbouring
`app_first_serve_not_observed` line names."""
