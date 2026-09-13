"""The Azure-side sandbox inventory — the view the reaper does not have.

WHY THIS EXISTS. `sweep_all` enumerates from Redis, one pass per registered user, so it can only
ever collect a container it already has a record of. A sandbox whose registry entry is gone is
invisible to it FOREVER: the Redis it was registered in was flushed or replaced (every local dev
stack is a different instance), or the container predates the registry hash, or a `reap_user`
teardown failed after `delete_registry` — the one ordering where the record goes first. None of
those are exotic: one container in the dev subscription ran for twelve days at ~$78/month, found
only because a human went looking. For REGISTERED sandboxes the sweep collects automatically; this
module is the other half.

AND WHY IT STAMPS AN AGE AZURE ALREADY REPORTS. Azure publishes `createdAt` and
`systemData.createdAt` for every container and this platform trusts neither: their behaviour
across a delete-and-recreate under the SAME name is undocumented, so neither says how old THIS
container is. Every container provisioned here carries its own creation tag, stamped at birth and
read back off the ARM record — so reading an age costs an ARM listing and a hot path does without.
Backfill covers containers predating that stamping and errs toward WAITING: the age it writes is
`now`, so one reads as brand new and serves its full tier clock. Believing an Azure timestamp
would hand a nineteen-day-old ghost an instant death sentence on evidence already judged untrusted.

REPORT-ONLY, deliberately: an inventory cannot tell "orphaned" from "provisioned four seconds ago
by a start that has not written its registry hash yet". `_start_locked` takes the lock BEFORE it
provisions the container that writes the registry, so that window reads exactly like an orphan.
Telling an operator the names is enough to act; deleting on a guess is not.
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
from src.db.models.project_share import ProjectShare
from src.services.build_sessions.locks import read_registry
from src.services.redis import registry_scan_patterns
from src.services.redis.keys import REGISTRY_FIELD_APP_NAME
from src.services.sandbox.base import (
    KIND_BUILD_SANDBOX,
    KIND_SHARED_SANDBOX,
    TAG_APP_ID,
    TAG_BACKFILLED_AT,
    TAG_CONTROL_PLANE,
    TAG_CREATED_AT,
    TAG_KIND,
    TAG_USER_ID,
    FleetMember,
    SandboxError,
    control_plane_segment,
)

_log = structlog.get_logger()


@runtime_checkable
class FleetLister(Protocol):
    """The one capability this module needs from a control plane. A Protocol rather than the
    concrete `AcaControlPlane` so a test needs no Azure client and a future substrate satisfies
    it by shape — deliberately not added to the frozen `SandboxClient` ABC.

    ONE ENUMERATION FOR EVERY QUESTION, not a names-only and a names→tags pair walking the same
    ARM pages: no two callers can disagree about the fleet. `FleetMember.tags` normalizes to
    `{}` for a container with no `tags` key: absent from the list means it does not exist, `{}`
    means it exists with no identity — the backfill below depends on that difference."""

    async def list_sandbox_fleet(self) -> list[FleetMember]: ...


@runtime_checkable
class FleetTagger(FleetLister, Protocol):
    """`FleetLister` plus the write half: stamp identity tags onto a container.

    TWO PROTOCOLS, NOT ONE: `take_sandbox_inventory` needs only to enumerate, and demanding a
    *stamper* for a read-only report would 503 every listing-only fake for no gain. Extending
    `FleetLister` keeps the direction that is actually true — a client that can stamp can always
    list. `stamp_tags` is a MERGE, a promise the IMPLEMENTATION keeps rather than ARM:
    `Microsoft.App` replaces the whole tag map on PATCH, so the real client reads before it writes,
    and a substrate that cannot merge would destroy the identity the classifier judges by."""

    async def stamp_tags(self, *, name: str, tags: dict[str, str]) -> None: ...


@runtime_checkable
class FleetDestroyer(FleetTagger, Protocol):
    """`FleetTagger` plus the one capability only a DESTROY path needs: re-reading a single
    container's tags immediately before acting on it.

    THREE PROTOCOLS, NOT TWO, for the same reason there are two rather than one: the backfill never
    re-reads, and widening `FleetTagger` to demand it would 503 nine of its tests. `get_app_tags`
    returns `None` when ARM says the container does not exist — different from `{}` (exists, no
    identity), and the destroy path depends on the difference: absent means the delete already
    landed; untagged means somebody rewrote the resource and it is no longer ours to judge."""

    async def get_app_tags(self, *, name: str) -> dict[str, str] | None: ...


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

    Scans through `registry_scan_patterns()` and reads through `read_registry`, which is how the
    sweep does it. Reading the registry any second way would let this function and the sweep
    disagree about what is registered, and a container reported here as `unregistered` is one an
    operator is being told nothing tracks."""
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
    live = {member.name for member in await control_plane.list_sandbox_fleet()}
    registered = await _registered_app_names(redis)
    return SandboxInventory(
        live=tuple(sorted(live)),
        registered=tuple(sorted(registered)),
        unregistered=tuple(sorted(live - registered)),
        registered_missing=tuple(sorted(registered - live)),
    )


# --- the tag backfill ----------------------------------------------------------------
#
# Every container provisioned since identity stamping shipped carries its identity from birth.
# This is the other half: the containers that already exist. Until they are stamped, the whole
# fleet is un-judgeable without Redis, which is why running this is a RELEASE PREREQUISITE and not
# a follow-up — the destroy flag must not be flipped while the fleet still reports untagged
# sandboxes.


@dataclass(frozen=True)
class TagBackfillReport:
    """What one backfill pass did, in buckets that SUM to `scanned`.

    `scanned == already_tagged + stamped + skipped_no_row + failed`. `skipped_no_row` containers
    WERE stamped (`kind` + `backfilled_at`, no owner) because no app row matched their name; they
    report forever and nothing destroys them. `unowned` DOES NOT PARTICIPATE IN THE SUM — it says
    what the FLEET IS, not what this pass did. Without it the escalate-forever population vanishes
    after its first stamping, and `already_tagged == scanned`, the endpoint's only clean-fleet
    signal, reads alike for an identified fleet and an unadjudicated one."""

    scanned: int
    already_tagged: int
    stamped: int
    skipped_no_row: int
    failed: int
    unowned: int


@dataclass(frozen=True)
class _KnownContainer:
    """One name this platform could have produced, and what a backfill should stamp it as.

    `user_id` means whatever `TAG_USER_ID` means for `kind` — the OWNER for a build sandbox,
    the RECIPIENT for a shared view (`shared_sandbox_tags`' own rule, restated here rather than
    left implicit in a bare tuple, which is exactly what let this dict go a whole feature
    without a `shr-` entry: nothing about `tuple[UUID, UUID]` said which fleet a name belonged
    to, so nobody who used it needed to answer that question)."""

    app_id: uuid.UUID
    user_id: uuid.UUID
    kind: str


async def _app_names_to_owners(db: AsyncSession) -> dict[str, _KnownContainer]:
    """Map every name this platform could have PRODUCED back to who it belongs to — both fleets
    this one Redis-per-user slot can ever hold (#198): a build sandbox (`sbx-`, keyed by its
    owner) and a colleague's shared view (`shr-`, keyed by the RECIPIENT — same rule
    `shared_sandbox_tags` follows, since a `shr-` container's per-slot occupancy and revocable
    access are the recipient's, not the project owner's).

    FORWARD-MATCHED, never reverse-parsed, on both arms: `app_name_for`/`shr_name_for` each keep
    only 28 of 32 hex characters, so deriving every known name and comparing is exact, while
    parsing an owner out of either is a guess that could promote an unproven container into the
    destroy-eligible tiers. FLEET-WIDE ON PURPOSE — neither query here is scoped by `user_id`,
    because the question is "does ANY user (or ANY share) own this"; between them they read two
    identifier columns plus one junction row, no user data, superadmin-only. `app_name_for`/
    `shr_name_for` are imported in-function to keep `manager`'s heavy imports out of the worker.

    A project shared with several colleagues produces one `shr-` entry per recipient, all keyed
    off the SAME app id — a fan-out `_owning_app_ids` (this function's one non-reclaim consumer)
    already tolerates, since it only ever reads the app id back out, never the name."""
    from src.services.build_sessions.manager import app_name_for, shr_name_for

    rows = (await db.execute(sa.select(AppRegistry.id, AppRegistry.user_id))).all()
    known: dict[str, _KnownContainer] = {
        app_name_for(app_id): _KnownContainer(
            app_id=app_id, user_id=user_id, kind=KIND_BUILD_SANDBOX
        )
        for app_id, user_id in rows
    }
    share_rows = (
        await db.execute(
            sa.select(AppRegistry.id, ProjectShare.shared_with_user_id).join(
                ProjectShare, ProjectShare.project_id == AppRegistry.project_id
            )
        )
    ).all()
    for app_id, recipient_id in share_rows:
        known[shr_name_for(app_id, recipient_id)] = _KnownContainer(
            app_id=app_id, user_id=recipient_id, kind=KIND_SHARED_SANDBOX
        )
    return known


def _backfill_tags(owner: _KnownContainer | None) -> dict[str, str]:
    """The tags to merge onto one pre-existing container.

    Two shapes, the difference being the escalate-never-destroy invariant made concrete. Owner
    recovered: full identity, `created_at` set to NOW with a `backfilled_at` marker saying that age
    is synthetic. No matching app row: `kind` and `backfilled_at` and NOTHING ELSE —
    escalate-forever by construction, reported every pass and destroyed by none. Filling in a
    plausible owner is the one change that would silently make it destroy-eligible.

    UNMATCHED STAMPS `KIND_BUILD_SANDBOX`, NEVER `KIND_SHARED_SANDBOX` — a container this pass
    cannot name is, definitionally, not one `_app_names_to_owners` could resolve to either fleet,
    so the unmatched arm has no real "which kind" answer to give. `KIND_BUILD_SANDBOX` is the
    reclaimer's only DESTROY-eligible kind, which is what makes it the correct default here: an
    unowned container this platform cannot explain is exactly the population idle-reclaim exists
    to collect, and `KIND_SHARED_SANDBOX` would instead escalate it forever on a guess."""
    stamped_at = dt.datetime.now(dt.UTC).isoformat()
    if owner is None:
        return {TAG_KIND: KIND_BUILD_SANDBOX, TAG_BACKFILLED_AT: stamped_at}
    return {
        TAG_KIND: owner.kind,
        TAG_USER_ID: str(owner.user_id),
        TAG_APP_ID: str(owner.app_id),
        TAG_CONTROL_PLANE: control_plane_segment(),
        TAG_CREATED_AT: stamped_at,
        TAG_BACKFILLED_AT: stamped_at,
    }


async def backfill_sandbox_tags(db: AsyncSession, control_plane: FleetTagger) -> TagBackfillReport:
    """Stamp identity onto every sandbox container that predates identity stamping.

    Idempotent: a container already carrying `bial-kind` is counted and LEFT ALONE — re-stamping
    would reset the whole fleet's age clock on every run, making idle-reclaim reclaim nothing
    forever while every test stayed green. ONE CONTAINER'S FAILURE DOES NOT FAIL THE PASS (counted
    in `failed`, retried next run) — but AN ENUMERATION FAILURE PROPAGATES: a half-listed fleet
    reporting "nothing left to stamp" is the exact false green the destroy flag is gated on."""
    live = {member.name: member.tags for member in await control_plane.list_sandbox_fleet()}
    owners = await _app_names_to_owners(db)
    # END THE READ TRANSACTION BEFORE THE ARM LOOP. `owners` is already materialised as plain
    # UUIDs, so nothing below needs the session — and what follows is an unbounded serial walk of
    # PATCHes, each pollable to `_LRO_CEILING_SECONDS`. Holding the request's connection
    # idle-in-transaction across all of that starves a small pool for as long as the sweep runs,
    # for no gain at all. The three sibling reconcilers never do external writes under an open
    # session; this is the first endpoint that could, so it explicitly does not.
    #
    # `commit`, not `rollback`, for a transaction that only read: both end it and hand the
    # connection back, but rollback would also discard anything the caller had written before
    # calling us — which is real in the test harness, where the whole test runs inside one
    # transaction, and would be a live foot-gun for any future caller that seeds and then
    # backfills. Ending a read this way costs nothing and cannot destroy anybody's work.
    await db.commit()

    already_tagged = stamped = skipped_no_row = failed = unowned = 0
    for name in sorted(live):
        if live[name].get(TAG_KIND):
            already_tagged += 1
            # A container stamped by an EARLIER pass and still carrying no owner. Counting it
            # only when this pass did the stamping is what made the escalate-forever population
            # vanish on every re-run.
            if not live[name].get(TAG_USER_ID):
                unowned += 1
            continue
        owner = owners.get(name)
        try:
            await control_plane.stamp_tags(name=name, tags=_backfill_tags(owner))
        except SandboxError:
            # The name reaches this log line and nowhere else — the report is counts — so this
            # is an operator's only record of WHICH container refused.
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
            unowned += 1
        else:
            stamped += 1

    return TagBackfillReport(
        scanned=len(live),
        already_tagged=already_tagged,
        stamped=stamped,
        skipped_no_row=skipped_no_row,
        failed=failed,
        unowned=unowned,
    )
