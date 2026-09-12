"""The Redis key namespace for the sandbox lifecycle.

These key strings are a **byte-stable cross-track contract**, built only through the single
`ns()` choke point — a hand-written key drifts a prefix invisible to another track. Builders take
`uuid.UUID` and enforce it at RUNTIME: a UUID cannot contain a `:`, so the type IS the boundary.

Five sandbox families share the environment-scoped root `bial:{environment}:sandbox:`:

    lock:{user_id}       string — one-per-user lock (SET NX EX)
    heartbeat:{user_id}  string — idle timer (presence = active)
    registry:{user_id}   hash   — see REGISTRY_FIELD_* below
    lease:{user_id}      string — liveness lease (epoch seconds, TTL mandatory)
    starting:{user_id}   string — start-in-flight marker (project id, TTL mandatory)

A sixth family, taskiq's queue in `src/broker.py`, sits under `bial:` but outside `sandbox:` —
`bial:{env}:taskiq:stream`, where those braces are a literal Redis hash tag, not a placeholder.
Only the library-derived `autoclaim:<group>:<stream>` lock has a literal prefix outside `bial:`.
There is deliberately NO `:channel` family — single-replica means build progress is in-process.

WHY THIS EXISTS. Production shares one Redis instance with other BIAL apps, and a scheduled job
reads this namespace as a spare-list and deletes Azure containers on the strength of it — a
process pointed at the wrong instance must not act on another deployment's fleet. The registry
hash is the ONE family with no TTL and the sole input to the fleet sweep and Azure inventory, so
a moved or forgotten key permanently strands every container live at that instant. A fleet scan
issues current AND legacy as two literals, never one `bial:*:` glob, which would reach into
another environment's fleet; a legacy match is dual-read too (`locks.read_registry`), and the
legacy prefix stays read-only."""

from __future__ import annotations

import uuid
from typing import Final

# The reserved product root, shared with the taskiq families in `src/broker.py`.
KEY_ROOT: Final = "bial:"

# The sandbox domain segment, below the environment.
KEY_DOMAIN: Final = "sandbox:"

# The legacy root, from before the environment segment was added to sandbox keys, frozen as
# HISTORY rather than taste: it is what the live fleet was registered under, so a typo here
# silently un-reaches every container the dual-read exists to keep visible.
# READ-ONLY — nothing writes it, and it goes once the fleet inventory reports zero
# legacy-prefix records.
LEGACY_KEY_PREFIX: Final = "bial:sandbox:"

# The family discriminators. Named rather than inlined so `ns()` callers and the scan
# patterns cannot disagree about a spelling.
FAMILY_LOCK: Final = "lock"
FAMILY_HEARTBEAT: Final = "heartbeat"
FAMILY_REGISTRY: Final = "registry"
FAMILY_LEASE: Final = "lease"
FAMILY_STARTING: Final = "starting"


def _environment() -> str:
    """This process's environment segment — the scope every sandbox key sits under.

    Delegated to `src.core.runtime_env`, a leaf with no module-scope imports: `src.config` reaches
    `src.settings.api` reaches `src.services.redis.config`, which imports THIS package, so asking
    `src.config` directly at module level would close the cycle. Kept as its own function since
    what this scopes is coordination state, a different question from which control plane judges
    a container."""
    from src.core.runtime_env import environment_segment

    return environment_segment()


def key_prefix() -> str:
    """`bial:{environment}:sandbox:` — the environment-scoped root every sandbox family
    sits under."""
    return f"{KEY_ROOT}{_environment()}:{KEY_DOMAIN}"


def ns(family: str, user_id: uuid.UUID) -> str:
    """THE choke point. Every sandbox key in the platform is this string.

    The `uuid.UUID` check is a runtime guard, not a redundant assertion of the annotation. User ids
    arrive from JSON, from Redis key names and from ARM tags — all places the type checker cannot
    reach — and a `str` is the one input that could smuggle a `:` in and cross a segment boundary,
    forging a different family or a different environment. Fail loudly instead.
    """
    if not isinstance(user_id, uuid.UUID):
        raise TypeError(
            f"a sandbox key is built from a uuid.UUID, never a {type(user_id).__name__}: "
            "a string user id could carry a ':' and forge a different key family"
        )
    return f"{key_prefix()}{family}:{user_id}"


def lock_key(user_id: uuid.UUID) -> str:
    """`bial:{env}:sandbox:lock:{user_id}` — the one-per-user lock. Held via `SET … NX EX`;
    `NX` is the enforcement point for the client ABC's one-sandbox-per-user rule. Released
    LAST (compare-and-delete) in the snapshot / teardown / reaper ordering."""
    return ns(FAMILY_LOCK, user_id)


def heartbeat_key(user_id: uuid.UUID) -> str:
    """`bial:{env}:sandbox:heartbeat:{user_id}` — the idle timer. Rewritten with a fresh
    expiry on each activity; **expiry = idle** (eligible for reaper teardown, which snapshots
    first)."""
    return ns(FAMILY_HEARTBEAT, user_id)


def registry_key(user_id: uuid.UUID) -> str:
    """`bial:{env}:sandbox:registry:{user_id}` — the sandbox record hash. Read on
    `attach_existing` to reconnect. Fields are the `REGISTRY_FIELD_*` constants below;
    `state` is the reaper's durable mark-ending marker.

    `serving_since` is the first field on this hash to mean THE APP ANSWERED A REQUEST, and it
    is a different question from `state`, whose two values are reaper-lifecycle labels about the
    container. Reading `state == ready` as evidence that anything served is the mistake that
    field exists to end.

    THE ONLY WRITE TARGET for the registry. The legacy key below is read-only."""
    return ns(FAMILY_REGISTRY, user_id)


def lease_key(user_id: uuid.UUID) -> str:
    """`bial:{env}:sandbox:lease:{user_id}` — the wall-clock liveness lease (family 4).

    The value is a deadline in Unix epoch seconds (`time.time()`, never `time.monotonic()` — a
    monotonic reading means nothing outside the process that took it, and cross-process
    readability is the whole point). Renewed by the turn engine for the duration of a turn, read
    by the reconciliation sweep, and it **must** carry a TTL: a lease that never expires is a
    container that can never be reclaimed. `build_sessions/locks.py` owns the three primitives."""
    return ns(FAMILY_LEASE, user_id)


def starting_key(user_id: uuid.UUID) -> str:
    """`bial:{env}:sandbox:starting:{user_id}` — the start-in-flight marker (family 5).

    Value is the `project_id` being started; **must** carry a TTL bounded by the cold-start
    budget plus margin — this marker spares a container from reclamation, so one that never
    expires spares it forever. Written once by `_holding_user_lock`, cleared on the same exit.

    DELIBERATELY NOT A REGISTRY FIELD: written before a container exists, it would read as a
    live container that is not there, and get spared rather than collected."""
    return ns(FAMILY_STARTING, user_id)


def legacy_registry_key(user_id: uuid.UUID) -> str:
    """`bial:sandbox:registry:{user_id}` — the legacy registry key. **READ-ONLY.**

    Every write goes to `registry_key`. This exists so the dual-read window can still reach a
    fleet registered before the environment segment did, and so `delete_registry` can clear the
    key a migration may have left behind."""
    if not isinstance(user_id, uuid.UUID):
        raise TypeError(
            f"a sandbox key is built from a uuid.UUID, never a {type(user_id).__name__}"
        )
    return f"{LEGACY_KEY_PREFIX}{FAMILY_REGISTRY}:{user_id}"


def registry_scan_patterns() -> tuple[str, ...]:
    """Every registry pattern a FLEET SCAN must cover, current first.

    Two literals, never one widened `bial:*:sandbox:registry:*` glob, and never enough on its
    own — the module docstring above says why on both counts.
    """
    return (f"{key_prefix()}{FAMILY_REGISTRY}:*", f"{LEGACY_KEY_PREFIX}{FAMILY_REGISTRY}:*")


# --- Registry hash fields (frozen — SESSION-API writes/reads these, never a
# hand-typed field string) --------------------------------------------------

REGISTRY_FIELD_APP_NAME: Final = "app_name"
REGISTRY_FIELD_FQDN: Final = "fqdn"
# A REFERENCE to the supervisor bearer token — NEVER the raw token: the raw token
# lives only in the sandbox container's own env and in-process in SandboxHandle.token.
REGISTRY_FIELD_TOKEN_REF: Final = "token_ref"
REGISTRY_FIELD_CREATED_AT: Final = "created_at"
REGISTRY_FIELD_STATE: Final = "state"

# THE SERVING PROOF: the ISO-8601 UTC instant at which something WATCHED this container's app
# answer a request. The only field on the hash that means "the app worked" — `state` above says
# a container was SCHEDULED, and the platform reporting a scheduled container as running is the
# defect this field exists to end.
#
# THREE READINGS, AND NOTHING ELSE IS LEGAL. This comment IS the rollout contract:
#
#   absent    -> a hash written before this field existed: PRE-CUTOVER, read as PROVEN, which is
#                the old behaviour and keeps a live fleet framed across the deploy.
#   ""        -> the container exists and has NEVER served: UNPROVEN.
#   ISO-8601  -> the instant of first serve: PROVEN.
#
# ABSENCE IS IMPOSSIBLE ON ANY RECORD WRITTEN AFTER THE CUTOVER, and that is what makes the
# grandfather arm safely deletable once the fleet has turned over: `_write_registry` seeds the
# `""` sentinel at create time, and the legacy-prefix adoption in `build_sessions/locks.py`
# writes the adopted record's own `created_at` rather than copying an absence forward.
#
# WRITTEN ONLY by that create-time seed and by `build_sessions/locks.py::mark_serving` /
# `clear_serving`, which are Lua compare-and-sets against this same hash's `app_name` — the
# registry key is per USER and outlives a container swap, so an unguarded write can stamp a
# container the writer never watched. Retracted to `""`, NEVER `HDEL`-ed: deleting the field
# resurrects the pre-cutover reading and would report a crashed app as running.
REGISTRY_FIELD_SERVING_SINCE: Final = "serving_since"

# A relaunched preview's STAY OF EXECUTION: the ISO-8601 UTC instant its bounded
# lease lapses. A relaunched preview holds no lock and renews no heartbeat, so
# absent this field the background sweep would reap a preview the user is still
# looking at. Honored by `sweep_all` ONLY — reconcile-on-start reaps regardless,
# because the incoming build needs the one-per-user slot.
REGISTRY_FIELD_PREVIEW_STAY_UNTIL: Final = "preview_stay_until"
# WHICH NAMED WRITER last moved the stay above. Provenance, not control flow:
# nothing branches on it, and it exists so an operator staring at a container that refuses
# to lapse can answer "what is holding this open?" without guessing. A deadline with no
# attributable author is the state this field exists to remove.
REGISTRY_FIELD_STAY_WRITER: Final = "stay_writer"

# THIS PROCESS ADOPTED THIS RECORD FROM THE LEGACY PREFIX during the dual-read window. Written
# only by `_adopt_a_pre_cutover_record`, read only by `delete_registry`, and it goes with the
# rest of the legacy arm.
#
# It exists because the legacy prefix is the one namespace with NO environment segment, so
# `bial:sandbox:registry:{user}` means different containers in different deployments that share a
# Redis instance. `delete_registry` deleted it unconditionally: a process reaping its own session
# also deleted whatever another environment had under that key — leaving the owning environment a
# running container with no record, which is exactly the class of orphan the scheduled fleet sweep
# exists to collect, manufactured by the environment-scoping migration's own cleanup. The adoption
# path already refuses to delete on read for this reason; this marker extends the same rule to the
# one place that still deletes.
#
# Durable rather than in-process, because the delete happens in a later session — often a later
# process — than the adoption.
REGISTRY_FIELD_ADOPTED_FROM_LEGACY: Final = "adopted_from_legacy"

# --- shared-runtime identity (#198) — written ONLY when this slot holds a `shr-` container ---
#
# The per-user slot this hash describes can hold EITHER the user's own build sandbox OR a
# colleague's shared project, restored read-only into their slot. `app_name` alone cannot say
# which: `shr_name_for` hashes the (app, recipient) pair, so nothing may reverse-parse an app id
# or project id back out of it (same forward-match-only rule `app_name_for`/`published_app_name`
# follow). Written once, at Launch, so the occupancy check a future slice adds (recognizing "you
# already hold a shared view" before a new build silently reclaims it) never needs a backfill.

REGISTRY_FIELD_SHARED_PROJECT_ID: Final = "shared_project_id"
"""The shared `projects.id` this slot is a read-only view of. Absent on every other registry
record — including an ordinary build sandbox's — which is exactly the signal a reader needs to
tell the two occupants of this one slot apart."""

REGISTRY_FIELD_SHARED_OWNER_ID: Final = "shared_owner_id"
"""The project's owner — the user whose saved snapshot this view was restored from. NOT the
`user_id` the registry key itself is keyed by (that is the RECIPIENT, whose slot this is); see
`KIND_SHARED_SANDBOX`'s own docstring in `sandbox/base.py` for why the ARM tags make the same
choice."""

REGISTRY_FIELD_SHARED_SERVED_COUNT: Final = "shared_served_count"
"""The supervisor's `/served` COUNT as of the last sweep that checked it — a monotonically
increasing total, never a delta. The sweep compares this against a fresh read to decide whether
to grant `DeadlineWriter.APP_SERVED_TRAFFIC`; an unchanged count between two sweeps means nobody
new has looked at the app since the last pass, not that the count reset. Absent until the first
sweep observes this slot."""

# The complete frozen field set (a completeness/disjointness anchor for tests and
# for SESSION-API's hydration of the registry hash).
REGISTRY_FIELDS: Final = frozenset(
    {
        REGISTRY_FIELD_APP_NAME,
        REGISTRY_FIELD_FQDN,
        REGISTRY_FIELD_TOKEN_REF,
        REGISTRY_FIELD_CREATED_AT,
        REGISTRY_FIELD_STATE,
        REGISTRY_FIELD_SERVING_SINCE,
        REGISTRY_FIELD_PREVIEW_STAY_UNTIL,
        REGISTRY_FIELD_STAY_WRITER,
        REGISTRY_FIELD_ADOPTED_FROM_LEGACY,
        REGISTRY_FIELD_SHARED_PROJECT_ID,
        REGISTRY_FIELD_SHARED_OWNER_ID,
        REGISTRY_FIELD_SHARED_SERVED_COUNT,
    }
)

# The two lifecycle values the reaper writes to REGISTRY_FIELD_STATE: `ready`
# is the normal live state; `ending` is the durable mark-ending marker set FIRST in
# the reaper ordering (mark-ending → teardown → release lock) so a concurrent
# attach sees a dying container and does not reconnect.
REGISTRY_STATE_READY: Final = "ready"
REGISTRY_STATE_ENDING: Final = "ending"
