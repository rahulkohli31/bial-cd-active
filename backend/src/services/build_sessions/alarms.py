"""Pinned structlog event names for the build-harness alarms (R32, ASM4).

There is no metrics system in this deployment — `api/v1/admin/schemas.py` says so outright — so
an alarm is a GREPPABLE EVENT CONSTANT an external log rule keys on, plus (where the outcome
needs to be counted rather than merely noticed) a relational record. That is the shape
`main.py::REDIS_PROBE_FAILED_EVENT`, `workers/reclamation.py::FLEET_THRESHOLD_EVENT` and
`turns/engine.py::LEASE_RENEW_FAILED_EVENT` already established; this module gathers the
harness's own so they stop being scattered across the modules that happen to raise them.

THE ONE RULE: each name appears exactly ONCE in the codebase. An alert cannot be written
against a string that exists in two spellings, and a second spelling is invisible until the
day it is the only one firing. Import the constant; never retype the literal — including in
tests, which is why the tests assert on these names rather than on string copies.

Distinguishing REASONS belong in structured fields, not in the event name, for the same
reason: one operational question, one event, filterable by field.
"""

from typing import Final

HMR_PROTOCOL_DRIFT_EVENT: Final = "compile_signal_protocol_drift"
"""The compile signal's canary fired: the supervisor connected to the dev server's HMR socket
SUCCESSFULLY and then received nothing it recognised.

This is the one event that separates "the protocol moved upstream" from "the socket is down".
Defensive parsing — ignore unknown frame verbs, never assume a field is present — is what keeps
a bundler upgrade from crashing the consumer, and is EXACTLY what would make a rename silent:
the consumer would receive frames forever and understand none of them, while the platform
reported a healthy app. The dev server sends its current state within milliseconds of a
connect, so silence after a successful connect has no innocent explanation.

Fields: `app_name`, `connect_generation`, `reason`. Raised at most once per successful connect
(the generation is what makes that possible) rather than once per poll.

WHAT TO DO: the frame verbs this consumer understands are `building` / `built` / `sync`, read
from `action` or `type`, in `sandbox/supervisor/app.py::_derive_compile`. Capture a few frames
from a live container's `/_next/webpack-hmr` and add the new verb there. Until that ships the
platform reports `UNKNOWN`, the preview cover holds rather than clearing, and no user sees a
framework error screen — degraded, not broken."""


RECOVERY_WRITE_DID_NOT_LAND_EVENT: Final = "recovery_write_did_not_land"
"""A turn ended and its work did not reach the recovery slot (U3, R8).

THIS IS THE RECORD THAT SETTLES 2026-08-18 THE NEXT TIME IT HAPPENS. The existing shape for a
failed turn-end autosave is swallow-and-log, which is right — a safety net that can fail a turn is
not a safety net — but the swallow is exactly what made that day's reframe unfalsifiable. Nobody
could say afterwards whether the platform had failed to CHECK the workspace or failed to make it
DURABLE, because a write that never landed left no trace an operator would ever look for.

Fires on all THREE ways a turn's work fails to reach the slot, distinguished by `reason` rather
than by three event names — one operational question, one event, filterable by field:

* `refused` — the guard would not promote this tree (an unreadable lineage, a head_sha that is
  not a sha). The existing copy is untouched.
* `diverted` — same refusal, and the bundle was preserved under `divert_key` instead, so the tree
  is recoverable by the U25 operator procedure rather than thrown away.
* `failed` — the bundle or the upload itself did not complete. This is the swallowed case, and it
  is raised from the CALL SITE, which is the only place that knows the write raised.

Fields: `app_id`, `reason`, and — where the guard formed an opinion — `recorded_head` and
`bundled_head`, which together say WHY a tree was refused.

WHAT TO DO: read the app's `divert/{app_id}/` prefix. A `diverted` event means a real tree is
sitting there; `services/build_sessions/snapshot.py::write_recovery_copy` documents the guard that
put it there, and U25's promote endpoint is how it gets moved back."""


WORKSPACE_LOST_WHILE_IDLE_EVENT: Final = "workspace_lost_while_idle"
"""A reversion was caught at the preview poll rather than at a turn (U4, R4/R7).

THE TURN MAY NEVER COME. A container can revert while the citizen is reading, in another tab, or
at lunch — and until this unit the platform only ever asked about the workspace at the start of a
turn. A standing "Build complete" claim would sit above a dead app for as long as the tab stayed
open, which on 2026-08-18 is precisely the shape of the failure: the screen went on saying the
thing that had stopped being true.

Distinct from the turn-time reversion, deliberately. The turn-time one is handled — quarantined,
restored, told — inside a flow the citizen is already watching. This one fires with nobody
watching, so it is the operator's only notice that it happened at all, and it is the record that
says how often it happens.

Fields: `app_id` and `app_name` (which container), `last_known_head` (the app's last good
state), `recovery_copy_available` (was there anything to put back), and `verdict` (which of the
four states was reached).

THE CONTAINER'S AGE IS DELIBERATELY NOT HERE, and the omission is a decision rather than an
oversight. The plan asks for it, from the container's own `bial-created-at` ARM tag — right, and
for the right reason (ADR-0029 §3 distrusts Azure's `createdAt` because its behaviour across a
recreate under the same name is undocumented). But that tag is only reachable through an ARM
listing, and this event fires on a path a browser tab drives every 45 seconds; adding an ARM
round trip there to enrich a log line would be a worse trade than the gap it closes. An operator
who needs the age has it in the reclamation pass records, keyed by the same `app_name`.

WHAT TO DO: the citizen has already been told on the preview pane and the standing completion
claim has been retracted, so this is not an emergency page. It is the number to watch. If it
fires more than rarely, the containers are being reclaimed or reset out from under live sessions
and the reclamation policy is what wants looking at, not this code."""
