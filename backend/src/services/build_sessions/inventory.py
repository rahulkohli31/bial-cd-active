"""The Azure-side sandbox inventory — the view the reaper does not have (#83 follow-up).

WHY THIS EXISTS. `sweep_all` enumerates from Redis: `scan_iter` over the registry namespace
(`bial:{env}:sandbox:registry:*`), one pass per registered user. That is the right shape for its
job — reconciling users whose session died — but it means the sweep can only ever collect a
container it already has a record of. A sandbox whose registry entry is gone is invisible to it
FOREVER:

* the Redis it was registered in was flushed, or replaced, or is a different instance entirely
  (every local dev stack is a different instance);
* the container predates the registry hash;
* a `reap_user` teardown failed after `delete_registry` — the one ordering where the record
  goes before the container.

None of those are exotic. One container in the dev subscription ran for twelve days, ~$78/month,
found only because a human went looking. The reaper's changelog entry says a sweep "now collects
it automatically", and for REGISTERED sandboxes it does; this module is the other half.

REPORT-ONLY, deliberately, and for the same reason `reconcile_orphaned_app_databases` is: an
inventory cannot tell "orphaned" from "provisioned four seconds ago by a start that has not
written its registry hash yet". `_start_locked` takes the lock BEFORE it provisions the container
that writes the registry, so that window reads exactly like an orphan — the same ambiguity
`live_build._the_live_session_is_this_app` fails closed on. Telling an operator the names is
enough to act; deleting on a guess is not something to hand a sweep.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import redis.asyncio as aioredis

from src.services.build_sessions.locks import read_registry
from src.services.redis import registry_scan_patterns
from src.services.redis.keys import REGISTRY_FIELD_APP_NAME


@runtime_checkable
class FleetLister(Protocol):
    """The one capability this module needs from a control plane. A Protocol rather than the
    concrete `AcaControlPlane` so a test needs no Azure client, and so a future substrate
    (ACA Sandboxes, say) satisfies it by shape. Deliberately NOT added to the `SandboxClient`
    ABC, which is a frozen cross-track contract (C2) — the capability lives on the concrete
    client and the route checks for it at runtime."""

    async def list_sandbox_app_names(self) -> list[str]: ...


@dataclass(frozen=True)
class SandboxInventory:
    """What ARM has, what the registry claims, and the gap between them.

    `unregistered` is THE LEAK: containers Azure is billing for that nothing tracks, so no sweep
    will ever reach them. `registered_missing` is the opposite and far less urgent — a registry
    entry whose container is already gone, which the next `reconcile_user` clears on its own."""

    live: tuple[str, ...]
    registered: tuple[str, ...]
    unregistered: tuple[str, ...]
    registered_missing: tuple[str, ...]


async def _registered_app_names(redis: aioredis.Redis) -> set[str]:
    """Every app name the sandbox registry currently claims is live.

    Walks the same patterns `sweep_all` does — `registry_scan_patterns()`, which during the R22
    dual-read window is the environment-scoped prefix AND the legacy one (C5) — and reads through
    the same `read_registry`, dual-read and all. Both halves are on purpose: reading the registry
    a second way would let this function and the sweep disagree, and it exists precisely to be
    trusted about what the sweep can and cannot see. A container reported here as `unregistered`
    is one an operator is being told nothing tracks."""
    names: set[str] = set()
    seen: set[uuid.UUID] = set()
    for pattern in registry_scan_patterns():
        async for raw_key in redis.scan_iter(match=pattern):
            try:
                user_uuid = uuid.UUID(str(raw_key).rsplit(":", 1)[-1])
            except ValueError:
                continue  # a key we did not write; not ours to interpret
            if user_uuid in seen:  # the same user under both prefixes — one read is enough
                continue
            seen.add(user_uuid)
            reg = await read_registry(redis, user_uuid)
            app_name = (reg or {}).get(REGISTRY_FIELD_APP_NAME)
            if app_name:
                names.add(app_name)
    return names


async def take_sandbox_inventory(
    redis: aioredis.Redis, control_plane: FleetLister
) -> SandboxInventory:
    """Diff the sandbox containers ARM knows about against the ones the registry claims.

    A listing failure PROPAGATES. A partial inventory that read as "no orphans" would be the
    worst possible output — it is the exact answer that gets a billing container forgotten for
    another twelve days."""
    live = set(await control_plane.list_sandbox_app_names())
    registered = await _registered_app_names(redis)
    return SandboxInventory(
        live=tuple(sorted(live)),
        registered=tuple(sorted(registered)),
        unregistered=tuple(sorted(live - registered)),
        registered_missing=tuple(sorted(registered - live)),
    )
