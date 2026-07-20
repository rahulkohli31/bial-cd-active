"""Frozen Redis key namespace for the sandbox lifecycle (contract C5).

These key strings are a **byte-stable cross-track contract**. Every family is
rendered here as one pure builder so no track ever hand-writes a key string —
drift in a prefix is a cross-track break (a lock written under one format is
invisible to a reaper reading another). The builders take `uuid.UUID`, never
`str`: a canonical UUID cannot contain `:` in a way that crosses a segment, so the
`user_id` axis can never forge a different family — the type IS the boundary.

Three provably-disjoint families share the reserved product prefix
`bial:sandbox:`; the third segment (`lock` / `heartbeat` / `registry`) is the
family discriminator (C5):

    bial:sandbox:lock:{user_id}        string  — one-per-user lock (SET NX EX)
    bial:sandbox:heartbeat:{user_id}   string  — idle timer (presence = active)
    bial:sandbox:registry:{user_id}    hash    — {app_name, fqdn, token_ref, created_at,
                                                  state, preview_stay_until?}

Single-replica deployment ⇒ there is intentionally **NO** `:channel` family:
build progress is an in-process asyncio channel (C7), not Redis pub/sub.
"""

from __future__ import annotations

import uuid
from typing import Final

# The reserved product prefix shared by every sandbox key family. The trailing
# colon keeps the family discriminator its own segment.
KEY_PREFIX: Final = "bial:sandbox:"


def lock_key(user_id: uuid.UUID) -> str:
    """`bial:sandbox:lock:{user_id}` — the one-per-user lock (C5). Held via
    `SET … NX EX`; `NX` is the enforcement point for C2's one-sandbox-per-user
    rule. Released LAST (compare-and-delete) in the C4 / reaper ordering."""
    return f"{KEY_PREFIX}lock:{user_id}"


def heartbeat_key(user_id: uuid.UUID) -> str:
    """`bial:sandbox:heartbeat:{user_id}` — the idle timer (C5). Rewritten with a
    fresh expiry on each activity; **expiry = idle** (eligible for reaper teardown,
    which snapshots first per C4)."""
    return f"{KEY_PREFIX}heartbeat:{user_id}"


def registry_key(user_id: uuid.UUID) -> str:
    """`bial:sandbox:registry:{user_id}` — the sandbox record hash (C5). Read on
    `attach_existing` (C2) to reconnect. Fields are the `REGISTRY_FIELD_*`
    constants below; `state` is the reaper's durable mark-ending marker."""
    return f"{KEY_PREFIX}registry:{user_id}"


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
    }
)

# The two lifecycle values the reaper writes to REGISTRY_FIELD_STATE (C5): `ready`
# is the normal live state; `ending` is the durable mark-ending marker set FIRST in
# the reaper ordering (mark-ending → teardown → release lock) so a concurrent
# attach sees a dying container and does not reconnect.
REGISTRY_STATE_READY: Final = "ready"
REGISTRY_STATE_ENDING: Final = "ending"
