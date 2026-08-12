"""Gather the evidence one reclamation pass needs, and hand it to the classifier (U11).

THE SPLIT IS THE TESTABILITY ARGUMENT. `reclaim.py` decides and touches nothing; this module
touches everything and decides nothing. Every safety property lives in the pure half, where it can
be proven against a synthetic fleet holding every dangerous combination at once; this half is the
plumbing that feeds it, and it can be exercised with fakes.

THREE SOURCES, AND THEY DISAGREE ON PURPOSE:

* **Azure** is the fleet of record (ADR-0029). What it says exists, exists.
* **Redis** is a spare-list ONLY — never an inventory. A container missing from it is not thereby
  an orphan; it is a container with no claim on it.
* **Postgres** answers "does an app row match this container", which decides whether an unclaimed
  container waits one hour or four.

`None` from the product database means COULD NOT ASK, and the classifier escalates the whole fleet
on it. That is not paranoia: the alternative reads a database outage as "no app matches any
container", which is the shape that turns every real builder's app into a high-confidence orphan.
"""

from __future__ import annotations

import datetime as dt
import uuid
from collections.abc import Mapping
from dataclasses import dataclass

import redis.asyncio as aioredis
import structlog

from src.services.build_sessions import locks
from src.services.build_sessions.inventory import FleetLister
from src.services.build_sessions.reclaim import (
    ContainerVerdict,
    RegistryClaim,
    Verdict,
    classify_fleet,
)
from src.services.redis import get_redis, registry_scan_patterns
from src.services.redis.keys import REGISTRY_FIELD_APP_NAME

_log = structlog.get_logger()


@dataclass(frozen=True)
class PassReport:
    """What one pass saw and what it would do."""

    scanned: int
    spared: int
    staged: int
    destroy: int
    escalate: int
    not_ours: int
    store_fault: bool
    #: Everything an operator has to look at: the destroy candidates AND the escalations. Sparing
    #: is the boring majority and is reported as a count only.
    candidates: tuple[ContainerVerdict, ...]
    #: `app_name -> (user_id, app_id)`, read off the ARM tags. Carried because the destroy arm
    #: cannot act without it: the ordered teardown is keyed by USER, the durable-copy gate by APP.
    #: A container with incomplete identity never appears here — and never reaches a destroy tier
    #: either, so the two absences agree by construction.
    owners: Mapping[str, tuple[uuid.UUID, uuid.UUID]]


async def _registry_claims() -> dict[str, RegistryClaim]:
    """What the coordination store says about each container it knows.

    Walks the same patterns `sweep_all` does — during the R22 dual-read window that is the
    environment-scoped prefix AND the legacy one — and reads through the same `read_registry`, so
    this and the sweep can never disagree about what is registered.

    REGISTRATION IS NOT A CLAIM. Every signal is read explicitly, because "the registry knows this
    name" is exactly the thing that must not spare a container: `_pardon_the_container` keeps the
    entry after a turn completes, so a pardoned-then-abandoned sandbox sits here forever."""
    redis = get_redis()
    claims: dict[str, RegistryClaim] = {}
    seen: set[uuid.UUID] = set()
    for pattern in registry_scan_patterns():
        async for raw_key in redis.scan_iter(match=pattern):
            try:
                user = uuid.UUID(str(raw_key).rsplit(":", 1)[-1])
            except ValueError:
                continue  # a key we did not write; not ours to interpret
            if user in seen:  # the same user under both prefixes — one read is enough
                continue
            seen.add(user)
            reg = await locks.read_registry(redis, user) or {}
            name = reg.get(REGISTRY_FIELD_APP_NAME)
            if not name:
                continue
            claims[name] = await _claim_of(redis, user)
    return claims


async def _claim_of(redis: aioredis.Redis, user: uuid.UUID) -> RegistryClaim:
    """The four signals, read explicitly. One spelling, so the enumeration read and the delete-time
    re-read below can never disagree about what "claimed" means."""
    return RegistryClaim(
        lock_held=await locks.lock_is_held(redis, user),
        heartbeat_alive=await locks.heartbeat_is_alive(redis, user),
        stay_current=await locks.stay_of_execution_is_current(redis, user),
        lease_held=await locks.liveness_lease_is_held(redis, user),
    )


async def claim_for_container(
    redis: aioredis.Redis, user_id: uuid.UUID, *, app_name: str
) -> RegistryClaim | None:
    """What the store says about ONE container right now, or `None` when nothing claims it.

    THE NAME CHECK IS THE POINT, not a formality. The claim keys are container names but the
    signals are keyed by USER, so reading a user's lock and calling it a claim on `app_name` is
    only true while their registry still names `app_name`. Once it names a fresh container, those
    signals describe THAT one — and reading them as a claim on the orphan would spare the very
    container the pass exists to collect, every pass, forever.

    Used by the destroy arm to re-run the classifier's spare-list read immediately before the
    delete, against a store that has had a whole staging interval to change its mind."""
    reg = await locks.read_registry(redis, user_id) or {}
    if reg.get(REGISTRY_FIELD_APP_NAME) != app_name:
        return None
    return await _claim_of(redis, user_id)


async def _known_app_names() -> frozenset[str] | None:
    """Container names with a matching app row, or `None` when the database could not be read.

    The `None` is load-bearing and is why this does not simply return an empty set on failure. An
    empty set means "asked, and nothing matched", which routes every container into the
    high-confidence one-hour tier. `None` means "could not ask", and escalates the fleet."""
    from src.db.base import async_session_factory
    from src.services.build_sessions.inventory import _app_names_to_owners

    try:
        async with async_session_factory() as db:
            return frozenset(await _app_names_to_owners(db))
    except Exception:
        _log.exception("reclamation_could_not_read_the_app_table")
        return None


async def run_reclamation_pass(*, control_plane: FleetLister | None = None) -> PassReport:
    """Enumerate, gather, classify. Touches no container.

    `control_plane` is injectable so a test can drive the whole pass against a fake fleet; the
    default builds the real ACA client from settings."""
    if control_plane is None:
        from src.config import settings
        from src.services.sandbox.aca import create_aca_control_plane

        if settings.sandbox is None:  # pragma: no cover - the caller's flag gate precedes this
            raise RuntimeError("reclamation ran with no sandbox configuration")
        control_plane = create_aca_control_plane(settings.sandbox)

    fleet = await control_plane.list_sandbox_fleet()
    claims = await _registry_claims()
    known = await _known_app_names()

    plan = classify_fleet(fleet, claims=claims, known_app_names=known, now=dt.datetime.now(dt.UTC))
    return PassReport(
        scanned=plan.scanned,
        spared=plan.spared,
        staged=plan.staged,
        destroy=plan.destroy,
        escalate=plan.escalate,
        not_ours=plan.not_ours,
        store_fault=plan.store_fault,
        candidates=tuple(
            v
            for v in plan.verdicts
            if v.verdict in (Verdict.DESTROY, Verdict.STAGE, Verdict.ESCALATE)
        ),
        owners={
            m.name: (m.identity.user_id, m.identity.app_id)
            for m in fleet
            if m.identity.user_id is not None and m.identity.app_id is not None
        },
    )
