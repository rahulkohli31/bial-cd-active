"""The destructive half: single-flight, re-validated, ceilinged, ordered.

Runs only when BOTH reclamation flags are on: `reclaim_enabled` gets a report,
`reclaim_destroy` lets a pass act.

WHY THIS EXISTS — FOUR PROTECTIONS, each covering a failure the others do not:

1. Single-flight via a Postgres advisory lock, NOT Redis (`locks.py` says why Redis
   isn't trusted to hold one) — ACA revision overlap means two schedulers can exist
   during a deploy.
2. Re-validation immediately before each DELETE, of both the TAGS and the CLAIM:
   `app_name_for` is deterministic, so a trailing `delete_registry` can otherwise wipe
   a record written by a start that landed after the enumeration snapshot. The claim
   covers what a tag re-read cannot — a resumed builder leaves tags untouched and
   changes only the lock/heartbeat/stay/liveness lease.
3. A per-pass ceiling, bounding blast radius AND runtime: ACA's SIGTERM grace is ~30s
   and `asyncio.wait(timeout=)` does not cancel on timeout.
4. A dev allowlist — nothing on a development control plane is deleted, full stop.

Teardown order is FOUR steps, not two: `mark_registry_ending` → `teardown` →
`delete_registry` → `reap_lock` LAST — "Azure before Redis record" is a property of
that sequence, not a substitute for it.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass

import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

from src.services.build_sessions.reclaim import ContainerVerdict, RegistryClaim
from src.services.sandbox.base import TAG_RECLAIM_STAGED_AT, identity_from_tags

#: What re-validation hands back: the container's CURRENT tags, or `None` when ARM says it is
#: gone. Typed as a Protocol rather than `object` so the callable-ness is checked rather than
#: suppressed — a `type: ignore[operator]` on the one line that issues a teardown is exactly the
#: suppression a reviewer should refuse.
Revalidate = Callable[[str], Awaitable[Mapping[str, str] | None]]
#: The OTHER half of re-validation: this container's spare-list entry as of right now, or `None`
#: when nothing claims it. Tags answer "is this still the resource we judged"; a claim answers
#: "has its owner come back", and a resumed builder changes the second without touching the first.
ClaimNow = Callable[[str], Awaitable[RegistryClaim | None]]
#: A teardown REPORTS whether it deleted anything. "I was asked" and "it is gone" are different
#: observations: a refusal — a durable-copy gate sparing the container, an ARM delete that would
#: not take — must not be counted as a destruction in the pass record.
Teardown = Callable[[str], Awaitable[bool]]

_log = structlog.get_logger()

#: A per-pass destroy ceiling. Sized so a pass finishes well inside ACA's ~30s SIGTERM grace even
#: when every delete is a slow LRO, and so a misjudgement costs at most this many containers
#: before a human sees the next report.
DESTROY_CEILING = 5

#: The advisory-lock key. A constant, because the lock protects "a reclamation pass", not a row —
#: two passes must not overlap regardless of which containers they are looking at.
_PASS_LOCK_KEY = 0x5A_4E_44_42_01  # "SNDB" + 01, an arbitrary but stable 64-bit constant

#: The event an operator greps for when passes stop happening. A sustained run of these means a
#: pass is wedged holding the lock, not that the system is idle.
PASS_SKIPPED_LOCKED_EVENT = "reclaim.pass.skipped_locked"


@dataclass(frozen=True)
class DestroyOutcome:
    """What the destructive half of one pass actually did."""

    destroyed: tuple[str, ...]
    #: Candidates the ceiling stopped us reaching. Reported rather than dropped: an operator
    #: seeing a remainder every pass is seeing a fleet growing faster than it is reclaimed.
    remaining: int
    #: Candidates that were re-validated and turned out to have changed since enumeration —
    #: either the resource is no longer the one we judged, or its owner came back.
    aborted: tuple[str, ...]
    #: Candidates the teardown itself declined: the durable-copy gate spared them, or ARM refused.
    #: Its own bucket rather than folded into `aborted`, because "we changed our mind" and "we
    #: tried and it did not die" are different facts and only the second one is likely to repeat.
    refused: tuple[str, ...]
    skipped_locked: bool


_lock_engine: AsyncEngine | None = None


def _the_lock_engine() -> AsyncEngine:
    """The pass lock's OWN engine — AUTOCOMMIT, `NullPool`, built on first use.
    NOT THE APPLICATION POOL: `pg_try_advisory_lock` is SESSION-scoped, so riding the
    shared pool risks the connection being released mid-pass (commit, rollback, expiry) —
    the lock vanishes while the destroy loop keeps deleting believing it's alone, exactly
    the overlap the lock exists to prevent. `NullPool` gives the lock a connection whose
    lifetime is the pass alone, so even a hard crash frees it via session end. AUTOCOMMIT
    because there's no transaction here to speak of. LAZY, for the reason `appdb/engine.py`
    documents: an eagerly-built engine binds to whichever event loop imported it."""
    global _lock_engine
    if _lock_engine is None:
        from src.config import settings
        from src.db.base import attach_entra_token

        _lock_engine = create_async_engine(
            settings.DATABASE_URL.get_secret_value(),
            isolation_level="AUTOCOMMIT",
            poolclass=NullPool,
            # Same no-parameters-in-logs rule as `db/base.py`. This engine runs the
            # reclamation pass unattended, so anything it renders into an exception goes
            # straight to an operator log with nobody reading the response.
            hide_parameters=True,
        )
        # MIRRORS `db/base.py` ON THE CREDENTIAL, NOT ON THE WHOLE CONFIGURATION — the rest
        # of this engine is deliberately different (AUTOCOMMIT, `NullPool`), for the reasons
        # above. What it copies is the token attach: a deployment authenticating with an
        # Entra token needs this engine to get one too, or single-flight would fail closed on
        # connect and every pass would report itself locked out by a lock nobody holds.
        if settings.DB_AUTH_MODE == "entra":
            attach_entra_token(_lock_engine)
    return _lock_engine


@asynccontextmanager
async def single_flight_lock(key: int) -> AsyncIterator[bool]:
    """Hold `key`'s advisory lock for the body, on a connection of its own. Yields whether we
    took it.

    THE KEY IS THE CALLER'S, because more than one scheduled pass needs this shape and they must
    not share a lock: two passes doing unrelated destructive work would otherwise stand each other
    down for no reason. What is shared is the ENGINE — one AUTOCOMMIT `NullPool` connection source
    for every lock, because the hazard it answers (a pooled connection recycled mid-pass, silently
    dropping the lock) is the same one whatever the pass.

    The explicit unlock is belt-and-braces over the connection close — with `NullPool` the
    close alone would free it, and saying so twice costs one statement."""
    async with _the_lock_engine().connect() as conn:
        took = bool((await conn.execute(sa.select(sa.func.pg_try_advisory_lock(key)))).scalar())
        try:
            yield took
        finally:
            if took:
                await conn.execute(sa.select(sa.func.pg_advisory_unlock(key)))


def may_destroy_on_this_control_plane(environment: str) -> bool:
    """THE DEV ALLOWLIST. Production only, and no argument gets around it.

    The dev subscription is a test bed holding containers that people are actively using to
    validate this very feature; deleting one because a classifier said so would destroy the
    evidence. `reclaim_destroy` is a flag an operator can flip anywhere — this is the thing that
    makes flipping it in development harmless."""
    return environment == "production"


async def destroy_candidates(
    candidates: tuple[ContainerVerdict, ...],
    *,
    revalidate: Revalidate,
    claim_now: ClaimNow,
    teardown: Teardown,
    environment: str,
) -> DestroyOutcome:
    """Destroy at most `DESTROY_CEILING` confirmed candidates, in order, re-validating each.
    `revalidate(name)` returns current tags or `None` if ARM says gone; `claim_now(name)`
    returns the current spare-list claim or `None`; `teardown(name)` reports whether it
    deleted anything — all three injected so the chain is drivable against a fake in a test.
    RE-VALIDATION IS TWO READS, NOT ONE: tags answer "is this still the resource judged",
    the claim answers "did its owner come back". A resumed staged container leaves tags
    unchanged, so the claim is rebuilt here through the same `spares_the_container`
    predicate the classifier used."""
    if not may_destroy_on_this_control_plane(environment):
        _log.info(
            "reclaim.destroy.refused_off_production",
            environment=environment,
            candidates=len(candidates),
        )
        return DestroyOutcome((), len(candidates), (), (), False)

    async with single_flight_lock(_PASS_LOCK_KEY) as took_the_lock:
        if not took_the_lock:
            _log.warning(PASS_SKIPPED_LOCKED_EVENT, candidates=len(candidates))
            return DestroyOutcome((), len(candidates), (), (), True)

        destroyed: list[str] = []
        aborted: list[str] = []
        refused: list[str] = []
        for index, candidate in enumerate(candidates):
            if len(destroyed) >= DESTROY_CEILING:
                # THE PASS ENDS AND REPORTS THE REMAINDER. It does not keep going "just for the
                # last one": the ceiling is a runtime bound as much as a blast-radius bound, and
                # a pass killed by SIGTERM mid-delete is the thing it exists to prevent.
                return DestroyOutcome(
                    tuple(destroyed),
                    len(candidates) - index,
                    tuple(aborted),
                    tuple(refused),
                    False,
                )
            if not await _still_the_same_container(candidate, revalidate=revalidate):
                aborted.append(candidate.name)
                continue
            if await _somebody_came_back(candidate, claim_now=claim_now):
                aborted.append(candidate.name)
                continue
            if await teardown(candidate.name):
                destroyed.append(candidate.name)
            else:
                refused.append(candidate.name)
        return DestroyOutcome(tuple(destroyed), 0, tuple(aborted), tuple(refused), False)


async def _still_the_same_container(
    candidate: ContainerVerdict, *, revalidate: Revalidate
) -> bool:
    """Re-read the container's tags and abort on ANY change since enumeration.
    The window is small, the consequence is not: `app_name_for` is deterministic, so
    between enumeration and delete a builder can start a fresh build into the very name
    this pass is about to destroy. Deleting it would take the new container, and the
    trailing registry clear would then wipe the record of a container that no longer
    exists — manufacturing exactly the orphan class this system was built to collect.
    ABSENT is not an abort: already gone is the outcome wanted, and a second delete of an
    absent resource is a 204 no-op."""
    current = await revalidate(candidate.name)
    if current is None:
        return True  # already gone; the ordered teardown is idempotent
    identity = identity_from_tags(current)
    if identity.reclaim_staged_at is None:
        # The staging tag is gone, which means something rewrote this container's tags since the
        # snapshot — a restore, a re-provision, or an operator. Not ours to destroy any more.
        _log.info("reclaim.destroy.aborted_staging_tag_gone", app_name=candidate.name)
        return False
    return True


async def _somebody_came_back(candidate: ContainerVerdict, *, claim_now: ClaimNow) -> bool:
    """Re-read the SPARE-LIST for this container and abort if its owner is holding it again.
    THE TAGS ARE UNCHANGED IN THE CASE THIS CATCHES — why the check above can't stand in
    for this one: a staged container stays fully attachable on purpose, and coming back
    writes a lock/heartbeat/stay/liveness lease, none of which are ARM tags, so the
    classifier's opinion can go stale in the one direction that matters.
    The predicate is `RegistryClaim.spares_the_container`, unchanged and unduplicated: one
    pure rule evaluated once at classification and again here, so there is no second
    spelling to fall out of sync on the path where being wrong costs an afternoon."""
    claim = await claim_now(candidate.name)
    if claim is None:
        return False  # nothing claims it — the state the classifier judged it in
    if claim.spares_the_container:
        _log.info("reclaim.destroy.aborted_claim_reappeared", app_name=candidate.name)
        return True
    return False


def staging_tags(now: dt.datetime) -> dict[str, str]:
    """The tag one pass stamps so the NEXT pass can see it happened.

    Deliberately NOT spelled, named or parseable as `ending`: `attach_existing` refuses a sandbox
    whose registry state reads `ending` *before it probes*, so a staged container must still be
    fully attachable. A citizen coming back to a staged sandbox gets it back — that is what clears
    the staging tag and spares it."""
    return {TAG_RECLAIM_STAGED_AT: now.isoformat()}
