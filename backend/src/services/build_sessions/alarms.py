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
