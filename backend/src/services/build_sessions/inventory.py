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

import datetime as dt
import uuid
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import redis.asyncio as aioredis
import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models.app_registry import AppRegistry
from src.services.build_sessions.locks import read_registry
from src.services.redis import registry_scan_patterns
from src.services.redis.keys import REGISTRY_FIELD_APP_NAME
from src.services.sandbox.base import (
    KIND_BUILD_SANDBOX,
    TAG_APP_ID,
    TAG_BACKFILLED_AT,
    TAG_CONTROL_PLANE,
    TAG_CREATED_AT,
    TAG_KIND,
    TAG_USER_ID,
    SandboxError,
    control_plane_segment,
)

_log = structlog.get_logger()


@runtime_checkable
class FleetLister(Protocol):
    """The one capability this module needs from a control plane. A Protocol rather than the
    concrete `AcaControlPlane` so a test needs no Azure client, and so a future substrate
    (ACA Sandboxes, say) satisfies it by shape. Deliberately NOT added to the `SandboxClient`
    ABC, which is a frozen cross-track contract (C2) — the capability lives on the concrete
    client and the route checks for it at runtime."""

    async def list_sandbox_app_names(self) -> list[str]: ...


@runtime_checkable
class FleetTagger(FleetLister, Protocol):
    """`FleetLister` plus the C10 identity half: read the tags a container carries, and stamp tags
    onto one.

    TWO PROTOCOLS, NOT ONE, deliberately. `take_sandbox_inventory` above needs only to enumerate,
    and demanding a *stamper* for a read-only report would over-constrain a substrate that can list
    but not write — and would turn every existing listing-only fake into a 503 for no gain. Because
    this extends `FleetLister`, a client that can stamp can always list, which is the direction
    that is actually true.

    `list_sandbox_app_tags` maps name -> tags, with an EMPTY DICT for a container ARM returned with
    no `tags` key at all. Absence FROM the mapping means the container does not exist; an empty
    mapping VALUE means it exists carrying no identity. Those are different answers, and the
    backfill below depends on the difference.

    `stamp_tags` is a MERGE (ARM `PATCH`): it adds and overwrites the keys given and leaves every
    other tag alone."""

    async def list_sandbox_app_tags(self) -> dict[str, dict[str, str]]: ...

    async def stamp_tags(self, *, name: str, tags: dict[str, str]) -> None: ...


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


# --- the C10 tag backfill ----------------------------------------------------------------
#
# Everything created from U8 onward carries its identity from birth. This is the other half: the
# containers that already exist. Until they are stamped, the whole fleet is un-judgeable without
# Redis, which is why running this is a RELEASE PREREQUISITE and not a follow-up — the destroy flag
# must not be flipped while the fleet still reports untagged sandboxes (C10 §3.5).


@dataclass(frozen=True)
class TagBackfillReport:
    """What one backfill pass did, in buckets that SUM.

    `scanned == already_tagged + stamped + skipped_no_row + failed`, the `appdb/reconcile.py`
    shape: a report whose buckets do not add up is a report nobody can check, and an operator is
    about to decide whether the fleet is ready for a destructive flag on the strength of it.

    `skipped_no_row` is the bucket that matters most and its name understates it: those containers
    WERE stamped — with `kind` and `backfilled_at` and nothing else — because no app row matched
    their name. They carry no owner, and they will be reported forever and destroyed by nothing."""

    scanned: int
    already_tagged: int
    stamped: int
    skipped_no_row: int
    failed: int


async def _app_names_to_owners(db: AsyncSession) -> dict[str, tuple[uuid.UUID, uuid.UUID]]:
    """Map every app's DERIVED sandbox name back to `(app_id, user_id)`.

    FORWARD-MATCHED, never reverse-parsed, and that is the whole safety argument. `app_name_for`
    produces `sbx-` + `app_id.hex[:28]` — 28 of 32 hex characters, truncated to fit ACA's 32-char
    name limit — so a sandbox name does NOT identify its app. Deriving the name for each known app
    and comparing is exact; parsing an owner out of a name is a guess, and a guess here promotes an
    unproven container into the destroy-eligible tiers.

    FLEET-WIDE ON PURPOSE. Every other query in this codebase is scoped by `user_id`; this one
    cannot be, because the question is "does ANY user own this container" and a per-user scope
    would answer "no" for every container belonging to somebody else — turning every other
    citizen's live sandbox into an unowned orphan. It reads two identifier columns and no user
    data, and it is reachable only from a superadmin fleet endpoint, the same posture as the other
    reconcilers.

    `app_name_for` is imported in-function because it lives in `manager`, which pulls in the api
    schema package and pydantic_ai; a module-level import would drag both into every consumer of
    this module — including the out-of-process worker that has no business loading them."""
    from src.services.build_sessions.manager import app_name_for

    rows = (await db.execute(sa.select(AppRegistry.id, AppRegistry.user_id))).all()
    return {app_name_for(app_id): (app_id, user_id) for app_id, user_id in rows}


def _backfill_tags(owner: tuple[uuid.UUID, uuid.UUID] | None) -> dict[str, str]:
    """The tags to merge onto one pre-existing container (C10 §3.1, §3.2).

    Two shapes, and the difference is the escalate-never-destroy invariant made concrete:

    * **Owner recovered** — the full identity, with `created_at` set to NOW and a `backfilled_at`
      marker beside it saying so. `now` errs toward WAITING: the container reads as brand new and
      must serve its whole tier clock before it is eligible for anything. Azure's
      `systemData.createdAt` is deliberately not used — it is the field R2 exists to distrust, and
      believing it here would hand a nineteen-day-old ghost an instant death sentence on evidence
      the platform has already decided is untrustworthy.
    * **No matching app row** — `kind` and `backfilled_at` and NOTHING ELSE. No owner, no app, no
      control plane. That container is escalate-forever by construction: `SandboxIdentity.
      escalate_only` is true for it and stays true, so it is reported on every pass and destroyed
      by none of them. Filling in a plausible owner here is the single change that would silently
      make it destroy-eligible, which is why it is mutation-checked.
    """
    stamped_at = dt.datetime.now(dt.UTC).isoformat()
    if owner is None:
        return {TAG_KIND: KIND_BUILD_SANDBOX, TAG_BACKFILLED_AT: stamped_at}
    app_id, user_id = owner
    return {
        TAG_KIND: KIND_BUILD_SANDBOX,
        TAG_USER_ID: str(user_id),
        TAG_APP_ID: str(app_id),
        TAG_CONTROL_PLANE: control_plane_segment(),
        TAG_CREATED_AT: stamped_at,
        TAG_BACKFILLED_AT: stamped_at,
    }


async def backfill_sandbox_tags(db: AsyncSession, control_plane: FleetTagger) -> TagBackfillReport:
    """Stamp C10 identity onto every sandbox container that predates identity stamping.

    Idempotent: a container already carrying `bial-kind` is counted and LEFT ALONE. Re-stamping
    would overwrite a real `bial-created-at` with `now` on every run, resetting the age clock of
    the entire fleet each time an operator pressed the button — which would make the feature that
    reclaims idle containers reclaim nothing, forever, while every test stayed green.

    ONE CONTAINER'S FAILURE DOES NOT FAIL THE PASS. A refused PATCH is counted in `failed` and the
    sweep moves on, because the operation is idempotent and the next run retries it; aborting on
    the first failure would leave the fleet part-stamped with no report of what remains.

    AN ENUMERATION FAILURE IS DIFFERENT AND PROPAGATES. A half-listed fleet reporting "nothing left
    to stamp" is the exact false green that the destroy flag is gated on (C10 §3.5)."""
    live = await control_plane.list_sandbox_app_tags()
    owners = await _app_names_to_owners(db)

    already_tagged = stamped = skipped_no_row = failed = 0
    for name in sorted(live):
        if live[name].get(TAG_KIND):
            already_tagged += 1
            continue
        owner = owners.get(name)
        try:
            await control_plane.stamp_tags(name=name, tags=_backfill_tags(owner))
        except SandboxError:
            # Counts only in the report; the name goes to the log, never to the audit row (C10
            # §3.6).
            _log.warning("sandbox_tag_backfill_failed", app_name=name, exc_info=True)
            failed += 1
            continue
        if owner is None:
            _log.warning(
                "sandbox_tag_backfill_found_no_owner",
                app_name=name,
                detail=(
                    "stamped kind + backfilled_at only; no app row matches this name, so the "
                    "container stays escalate-only forever rather than being guessed an owner"
                ),
            )
            skipped_no_row += 1
        else:
            stamped += 1

    return TagBackfillReport(
        scanned=len(live),
        already_tagged=already_tagged,
        stamped=stamped,
        skipped_no_row=skipped_no_row,
        failed=failed,
    )
