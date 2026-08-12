"""The Redis key namespace for the sandbox lifecycle (contract C5).

These key strings are a **byte-stable cross-track contract**. Every key is built here, through the
single `ns()` choke point, so no module ever hand-writes one — drift in a prefix is a cross-track
break (a lock written under one format is invisible to a reaper reading another). The builders take
`uuid.UUID` and enforce it at RUNTIME, not just in the annotation: a canonical UUID cannot contain
a `:`, so the `user_id` axis can never forge a different family, and the type IS the boundary.

Four sandbox families share the environment-scoped root `bial:{environment}:sandbox:`; the segment
after it is the family discriminator (C5):

    bial:{env}:sandbox:lock:{user_id}        string  — one-per-user lock (SET NX EX)
    bial:{env}:sandbox:heartbeat:{user_id}   string  — idle timer (presence = active)
    bial:{env}:sandbox:registry:{user_id}    hash    — {app_name, fqdn, token_ref, created_at,
                                                        state, preview_stay_until?}
    bial:{env}:sandbox:lease:{user_id}       string  — R10 liveness lease (wall-clock deadline,
                                                        epoch seconds, TTL mandatory)

The fifth C5 family is the taskiq queue, built in `src/broker.py`: `bial:{env}:taskiq:stream` plus
the library-derived `autoclaim:<group>:<stream>`, whose literal prefix cannot be moved under
`bial:` and therefore sits outside the environment-scoping guarantee by construction.

Single-replica deployment ⇒ there is intentionally **NO** `:channel` family: build progress is an
in-process asyncio channel (C7), not Redis pub/sub.

WHY THE ENVIRONMENT SEGMENT EXISTS (R22, ADR-0029)
--------------------------------------------------
Production reuses a Redis instance shared with other BIAL GenAI applications, and the old root
carried no environment axis. That was benign for as long as nothing deleted; it stops being benign
the moment a scheduled job reads this namespace as a spare-list and destroys containers from Azure
on the strength of it. A process pointed at the wrong instance must not be able to act on another
environment's fleet, and the cheapest way to guarantee that is to make the keys not match.

WHY THE LEGACY PREFIX IS STILL HERE, AND WHEN IT GOES
-----------------------------------------------------
A straight cutover would have been catastrophic. This root is the sole input to `sweep_all`'s scan
AND to the report-only Azure inventory, and the registry hash is the one family with **no TTL** —
so changing it in one deploy would make every container live at that instant permanently invisible
to both, manufacturing wholesale the exact orphan class ADR-0029 exists to collect. So the change
ships across two releases: this one WRITES only the new prefix but READS both, migrating any legacy
hash it finds; a later one deletes the legacy arm once the inventory reports zero legacy records.
C5 §"The prefix change ships as a DUAL-READ WINDOW" carries the full checklist for that removal.
"""

from __future__ import annotations

import uuid
from typing import Final

# The reserved product root, shared with the taskiq families in `src/broker.py`.
KEY_ROOT: Final = "bial:"

# The sandbox domain segment, below the environment.
KEY_DOMAIN: Final = "sandbox:"

# The pre-R22 root, frozen as HISTORY rather than taste: it is what the live fleet was registered
# under, so a typo here silently un-reaches every container the dual-read exists to keep visible.
# READ-ONLY — nothing writes it, and release B deletes it (C5).
LEGACY_KEY_PREFIX: Final = "bial:sandbox:"

# The family discriminators (C5). Named rather than inlined so `ns()` callers and the scan
# patterns cannot disagree about a spelling.
FAMILY_LOCK: Final = "lock"
FAMILY_HEARTBEAT: Final = "heartbeat"
FAMILY_REGISTRY: Final = "registry"
FAMILY_LEASE: Final = "lease"


def _environment() -> str:
    """This process's environment segment — the scope every sandbox key sits under (C5).

    Delegated to `src.core.runtime_env`, which is a leaf with no module-scope imports: `src.config`
    reaches `src.settings.api`, which imports `src.services.redis.config` — which imports
    THIS package — so asking `src.config` directly at module level would close the cycle and make
    `src.config` unimportable. That workaround used to be written out here AND in
    `sandbox/base.py`, twice, in full.

    Kept as its own named function rather than calling the accessor at each site: what this scopes
    is coordination state, which is a different question from which control plane may judge a
    container, and the two are free to diverge."""
    from src.core.runtime_env import environment_segment

    return environment_segment()


def key_prefix() -> str:
    """`bial:{environment}:sandbox:` — the environment-scoped root every sandbox family
    sits under."""
    return f"{KEY_ROOT}{_environment()}:{KEY_DOMAIN}"


def ns(family: str, user_id: uuid.UUID) -> str:
    """THE choke point. Every sandbox key in the platform is this string (C5).

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
    """`bial:{env}:sandbox:lock:{user_id}` — the one-per-user lock (C5). Held via `SET … NX EX`;
    `NX` is the enforcement point for C2's one-sandbox-per-user rule. Released LAST
    (compare-and-delete) in the C4 / reaper ordering."""
    return ns(FAMILY_LOCK, user_id)


def heartbeat_key(user_id: uuid.UUID) -> str:
    """`bial:{env}:sandbox:heartbeat:{user_id}` — the idle timer (C5). Rewritten with a fresh
    expiry on each activity; **expiry = idle** (eligible for reaper teardown, which snapshots
    first per C4)."""
    return ns(FAMILY_HEARTBEAT, user_id)


def registry_key(user_id: uuid.UUID) -> str:
    """`bial:{env}:sandbox:registry:{user_id}` — the sandbox record hash (C5). Read on
    `attach_existing` (C2) to reconnect. Fields are the `REGISTRY_FIELD_*` constants below;
    `state` is the reaper's durable mark-ending marker.

    THE ONLY WRITE TARGET for the registry. The legacy key below is read-only."""
    return ns(FAMILY_REGISTRY, user_id)


def lease_key(user_id: uuid.UUID) -> str:
    """`bial:{env}:sandbox:lease:{user_id}` — the R10 wall-clock liveness lease (C5 family 4).

    The value is a deadline in Unix epoch seconds (`time.time()`, never `time.monotonic()` — a
    monotonic reading means nothing outside the process that took it, and cross-process
    readability is the whole point). Renewed by the turn engine for the duration of a turn,
    read by the reconciliation sweep, and it **must** carry a TTL: the registry hash's lack of
    one is the root cause of ADR-0029, and a lease that never expires is a container that can
    never be reclaimed. `build_sessions/locks.py` owns the three primitives (U12)."""
    return ns(FAMILY_LEASE, user_id)


def legacy_registry_key(user_id: uuid.UUID) -> str:
    """`bial:sandbox:registry:{user_id}` — the pre-R22 registry key. **READ-ONLY.**

    Every write goes to `registry_key`. This exists so the dual-read window can still reach a
    fleet registered before the environment segment did, and so `delete_registry` can clear the
    key a migration may have left behind. Deleted in release B (C5)."""
    if not isinstance(user_id, uuid.UUID):
        raise TypeError(
            f"a sandbox key is built from a uuid.UUID, never a {type(user_id).__name__}"
        )
    return f"{LEGACY_KEY_PREFIX}{FAMILY_REGISTRY}:{user_id}"


def registry_scan_patterns() -> tuple[str, ...]:
    """Every registry pattern a FLEET SCAN must cover, current first (C5).

    Two literals, never one widened glob. `bial:*:sandbox:registry:*` is the tempting one-liner
    and it is exactly wrong: it would match OTHER ENVIRONMENTS, which is the hazard R22 closes.
    The environment segment is never wildcarded.

    Scanning alone is not enough and must not be mistaken for the fix — `sweep_all` extracts the
    user id from the key name and then issues a fresh POINT READ, so the dual-read has to live
    there too. This just makes the legacy keys reachable in the first place.
    """
    return (f"{key_prefix()}{FAMILY_REGISTRY}:*", f"{LEGACY_KEY_PREFIX}{FAMILY_REGISTRY}:*")


# --- Registry hash fields (frozen — SESSION-API writes/reads these, never a
# hand-typed field string) --------------------------------------------------

REGISTRY_FIELD_APP_NAME: Final = "app_name"
REGISTRY_FIELD_FQDN: Final = "fqdn"
# A REFERENCE to the supervisor bearer token — NEVER the raw token (C5): the raw
# token lives only in the sandbox env (C1) and in-process in SandboxHandle.token.
REGISTRY_FIELD_TOKEN_REF: Final = "token_ref"
REGISTRY_FIELD_CREATED_AT: Final = "created_at"
REGISTRY_FIELD_STATE: Final = "state"
# A relaunched preview's STAY OF EXECUTION: the ISO-8601 UTC instant its bounded
# lease lapses (#43). A relaunched preview holds no lock and renews no heartbeat, so
# absent this field the background sweep would reap a preview the user is still
# looking at. Honored by `sweep_all` ONLY — reconcile-on-start reaps regardless,
# because the incoming build needs the one-per-user slot.
REGISTRY_FIELD_PREVIEW_STAY_UNTIL: Final = "preview_stay_until"
# WHICH NAMED WRITER last moved the stay above (U13, R13). Provenance, not control flow:
# nothing branches on it, and it exists so an operator staring at a container that refuses
# to lapse can answer "what is holding this open?" without guessing. A deadline with no
# attributable author is the state R13 exists to remove — the origin incident's containers
# were held open by a writer nobody could name.
REGISTRY_FIELD_STAY_WRITER: Final = "stay_writer"

# THIS PROCESS ADOPTED THIS RECORD FROM THE LEGACY PREFIX (R22 dual-read window). Written only by
# `_adopt_a_pre_cutover_record`, read only by `delete_registry`, and gone in release B with the
# rest of the legacy arm.
#
# It exists because the legacy prefix is the one namespace with NO environment segment, so
# `bial:sandbox:registry:{user}` means different containers in different deployments that share a
# Redis instance. `delete_registry` deleted it unconditionally: a process reaping its own session
# also deleted whatever another environment had under that key — leaving the owning environment a
# running container with no record, which is exactly the orphan class ADR-0029 exists to collect,
# manufactured by R22's own cleanup. The adoption path already refuses to delete on read for this
# reason; this marker extends the same rule to the one place that still deletes.
#
# Durable rather than in-process, because the delete happens in a later session — often a later
# process — than the adoption.
REGISTRY_FIELD_ADOPTED_FROM_LEGACY: Final = "adopted_from_legacy"

# The complete frozen field set (a completeness/disjointness anchor for tests and
# for SESSION-API's hydration of the registry hash).
REGISTRY_FIELDS: Final = frozenset(
    {
        REGISTRY_FIELD_APP_NAME,
        REGISTRY_FIELD_FQDN,
        REGISTRY_FIELD_TOKEN_REF,
        REGISTRY_FIELD_CREATED_AT,
        REGISTRY_FIELD_STATE,
        REGISTRY_FIELD_PREVIEW_STAY_UNTIL,
        REGISTRY_FIELD_STAY_WRITER,
        REGISTRY_FIELD_ADOPTED_FROM_LEGACY,
    }
)

# The two lifecycle values the reaper writes to REGISTRY_FIELD_STATE (C5): `ready`
# is the normal live state; `ending` is the durable mark-ending marker set FIRST in
# the reaper ordering (mark-ending → teardown → release lock) so a concurrent
# attach sees a dying container and does not reconnect.
REGISTRY_STATE_READY: Final = "ready"
REGISTRY_STATE_ENDING: Final = "ending"
