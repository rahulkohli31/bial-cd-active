"""Pinned structlog event names that NO SINGLE PACKAGE OWNS.

`build_sessions/alarms.py` gathers the build harness's own alarms and explains the doctrine:
there is no metrics system in this deployment, so an alarm is a GREPPABLE EVENT CONSTANT an
external log rule keys on; each name exists exactly ONCE in the codebase, is imported rather
than retyped (including by tests), and distinguishing reasons live in structured FIELDS rather
than in a second event name.

THIS MODULE EXISTS FOR THE ALARMS THAT SPAN PACKAGES. `src/core/` is a leaf — nothing in here
imports `src.*` — which is the property that matters: the delete path runs through
`api/v1/projects/`, `services/storage/`, `services/deploy/` and `services/appdb/`, and
`services/storage/` is imported while `src.config` is still initialising. A constant those arms
share therefore cannot live under a package with a heavy `__init__`, or importing it is a
startup circular-import error in production's import order (and only in production's, which is
the worst way to find out).
"""

from typing import Final

TEARDOWN_ARTEFACT_SURVIVED_EVENT: Final = "delete_left_an_artefact_behind"
"""A delete finished and something it was supposed to destroy is still out there.

NOBODY COLLECTS THIS AUTOMATICALLY, which is the whole reason it is an alarm rather than a
debug line. Every teardown arm on the delete path is best-effort by necessity — the rows are
already committed, so a raise there would 500 a delete that in fact succeeded — and until this
unit each arm's comment said the leftover would be picked up by "the scheduled sweep". It will
not be. The only reconciler on a timer is the sandbox reap, which runs solely when
`environment == "production"` (`build_sessions/destroy.py::may_destroy_on_this_control_plane`);
the storage reconciler and the per-project-database reconciler are operator-invoked, and the
latter deletes nothing at all (`storage/reconcile.py`, `appdb/reconcile.py`). So a surviving
blob, container, image, database or sandbox is collected by a human reading this event, or by
nobody, ever.

ONE EVENT FOR EVERY ARM, on purpose: the operational question is "what did a delete leave
behind", and the artefact class is a FIELD, not a second event name — otherwise the alert has
to be written five times and a sixth arm added later is invisible.

Fields: `artefact` (one of `blob`, `app_container`, `app_database`, `registry_repository`,
`published_app`, `sandbox_container`, `submission_bundle`), `artefact_id` (the key, name or id
an operator needs to find it), and `reason` (why the arm gave up). The project delete path ALSO
writes one `project:teardown-incomplete` audit row naming the same artefacts, so the leak is
countable and attributable, not only greppable — the log line is the notice, the row is the
record.

WHAT TO DO: the artefact named in the event still exists and still costs something (a running
container bills; a database keeps a copy of the citizen's data alive after they asked for it to
be destroyed). Delete it by hand, then fix the cause the `reason` field names — most often a
credential without the delete permission, which is a configuration change rather than a code
one."""

EMBEDDING_GUARD_VIOLATION_EVENT: Final = "embedding_guard_violation"
"""The Foundry-only fail-closed guard (`services/embeddings/client.py`) refused to build an
embedding client because the resolved endpoint was not an Azure Foundry host (#191 slice 3).

A SEPARATE event from `EMBEDDING_WRITE_FAILED_EVENT` on purpose (R26): an ordinary embed-call
failure (timeout, 5xx, rate limit) is an expected, survivable degrade-to-keyword-only case; this
one means the wiring itself is wrong — `FOUNDRY__RESOURCE`/`FOUNDRY__EMBEDDING_DEPLOYMENT` point
somewhere that is not this platform's Foundry resource. It should never fire in a correctly
configured deployment, so folding it into the generic failure event would bury a configuration
bug under routine noise. The guard also runs once at application startup (before any request is
served), so a mis-wired host fails the deploy rather than degrading silently into this path.

Fields: `endpoint` (the base URL the guard rejected)."""

EMBEDDING_WRITE_FAILED_EVENT: Final = "embedding_write_failed"
"""Writing or refreshing a project's description embedding failed (#191 slice 3, R25/R26).

NEVER RAISED INTO THE CALLER. The project write (create/patch) still succeeds — an embedding
failure must never be the reason a citizen cannot save a description — and the row's embedding
column is simply left absent/stale, which the hybrid search query already treats as
keyword-only for that row. This event is the only record that it happened.

Fields: `project_id`, `reason` (the exception type — never the message, which can carry request
content)."""

SANDBOX_POOL_BELOW_SIZE_EVENT: Final = "sandbox_pool_below_size"
"""A pass over one pool of ready sandboxes, plain or flight-data, ended below the size the settings
give for that time, and something stopped it filling: a create was refused, or a container sat
filling or claimed past its deadline. That pass made no further create for that pool; the next, a
minute later, tries again. The other pool fills regardless.

A START NEVER FAILS BECAUSE OF THIS. One that finds no ready sandbox creates its own, as it would
with no pool, only slower, and its start record names why. It never fires while the size is zero.

Fields: `project_type` (which pool), `target` (the size for that time), `ready` (how many were
ready as the pass ended), `refused` (whether a create was refused) and `overdue` (whether a row was
past its deadline).

WHAT TO DO: read the worker's log for that pass, where the refusal is logged beside it. An
authorization refusal means the worker's identity lacks the create, tag-write or environment-join
action on the sandbox resource group; for the flight-data pool alone it can also mean the worker
may not assign the lake's identity. A flight-data fill refused with
`sandbox_pool_connector_fill_without_a_lake` means the worker has no lake settings. Rows overdue
while filling mean the image reference or the registry credentials are wrong; a capacity refusal
means the environment is full. When it cannot be fixed soon, set that pool's sizes to zero."""

SANDBOX_POOL_WRONG_IDENTITY_EVENT: Final = "sandbox_pool_claim_wrong_identity"
"""A start claimed a ready sandbox that did not carry exactly the identity its pool requires: a
plain one carrying any identity, or a flight-data one carrying none or another. The container was
let go before the project's settings reached it, and the start created its own.

A START NEVER FAILS BECAUSE OF THIS, but it is a security signal: a plain container carrying an
identity could have handed a project data it was never granted.

Fields: `app_name` (the container), `expected` (the resource id of the identity its pool requires,
`None` for a plain one) and `carried` (the resource ids of the identities Azure had attached).

WHAT TO DO: a flight-data container carrying another identity means the lake identity changed
without the flight-data pool being drained, or the worker and the backend hold different lake
settings. Make them equal, then drain that pool. A plain container carrying any identity was
changed outside the platform: find out how before anything else."""
