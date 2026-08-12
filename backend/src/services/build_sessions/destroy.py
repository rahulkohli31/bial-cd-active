"""The destructive half: single-flight, re-validated, ceilinged, ordered (U15, R7/R8/R19).

Everything here runs only when BOTH flags are on. `reclaim_enabled` gets you a report;
`reclaim_destroy` is what lets a pass act, and it must not be flipped until the C10 backfill
reports zero untagged sandboxes.

FOUR PROTECTIONS, AND EACH ONE COVERS A FAILURE THE OTHERS DO NOT:

1. **Single-flight via a Postgres advisory lock.** ACA revision overlap means two schedulers can
   exist during a deploy, so a second pass can start while one is running. Deliberately NOT a
   Redis lock: U5, U10 and U11 all exist because Redis is the store this work distrusts, its
   `maxmemory-policy` is unverified, and under any `allkeys-*` policy a lock key can be evicted
   mid-pass — silently undoing single-flight inside the destructive chain.
2. **Re-validation immediately before each DELETE — of the TAGS *and* of the CLAIM.**
   `app_name_for(app_id)` is deterministic, so a reclaimed container's name is the name the next
   start provisions into. In-process, the reaper and every start shared one event loop; out of
   process they do not. A trailing `delete_registry` can otherwise wipe a record written by a
   start that happened *after* the enumeration snapshot — manufacturing exactly the orphan class
   this plan collects. The claim is the half a tag re-read cannot cover: a builder who RESUMES a
   staged container leaves its tags untouched (staged containers stay attachable by design) and
   changes only the lock, heartbeat, stay or liveness lease.
3. **A per-pass ceiling.** A bounded blast radius, and a bounded runtime: ACA sends SIGTERM with a
   ~30s grace and `asyncio.wait(..., timeout=)` does not cancel on timeout, so a pass that
   overran would be killed mid-flight holding whatever it held.
4. **A dev allowlist.** Nothing on a development control plane is deleted, full stop. Dry-run is
   the default and this is the belt to that pair of braces.

THE FOUR-STEP TEARDOWN ORDERING IS FOUR STEPS, NOT TWO: `mark_registry_ending` (guards a
concurrent attach) → `teardown` → `delete_registry` → `reap_lock` LAST. Out of process, both the
dropped mark-ending guard and the never-released per-user lock matter. "Azure before Redis record"
is a property of that sequence, not a replacement for it.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass

import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

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
#: A teardown REPORTS whether it deleted anything. It used to return `None`, so "I was asked" and
#: "it is gone" were the same observation and a refusal — a durable-copy gate sparing the
#: container, an ARM delete that would not take — was counted as a destruction in the pass record.
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


async def _take_the_pass_lock(db: AsyncSession) -> bool:
    """`pg_try_advisory_lock` — non-blocking, released on connection close.

    NO TTL TO TUNE AND NOTHING TO EVICT, which is the entire reason it is here rather than in
    Redis. Note the connection must be held for the duration of the pass: an advisory lock lives
    on the session that took it, so returning the connection to the pool releases it."""
    row = await db.execute(sa.select(sa.func.pg_try_advisory_lock(_PASS_LOCK_KEY)))
    return bool(row.scalar())


async def _release_the_pass_lock(db: AsyncSession) -> None:
    await db.execute(sa.select(sa.func.pg_advisory_unlock(_PASS_LOCK_KEY)))


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
    db: AsyncSession,
    revalidate: Revalidate,
    claim_now: ClaimNow,
    teardown: Teardown,
    environment: str,
) -> DestroyOutcome:
    """Destroy at most `DESTROY_CEILING` confirmed candidates, in order, re-validating each.

    `revalidate(name)` returns the container's CURRENT tags, or `None` if ARM says it is gone.
    `claim_now(name)` returns the coordination store's CURRENT claim on it, or `None` when
    nothing claims it. `teardown(name)` performs the ordered reap and reports whether it deleted
    anything. All three are injected so the whole destructive chain is drivable against a fake in
    a test — the one place where "a green suite proves nothing" would be least acceptable.

    RE-VALIDATION IS TWO READS, NOT ONE, and neither substitutes for the other. Tags answer "is
    this still the resource the classifier judged"; the claim answers "did its owner come back
    while we were walking the list". A builder who resumes a staged container between enumeration
    and delete leaves the tags exactly as they were — a tag-only recheck waves them straight
    through — so the claim is rebuilt here, at delete time, and run through the same pure
    `spares_the_container` predicate the classifier used."""
    if not may_destroy_on_this_control_plane(environment):
        _log.info(
            "reclaim.destroy.refused_off_production",
            environment=environment,
            candidates=len(candidates),
        )
        return DestroyOutcome((), len(candidates), (), (), False)

    if not await _take_the_pass_lock(db):
        _log.warning(PASS_SKIPPED_LOCKED_EVENT, candidates=len(candidates))
        return DestroyOutcome((), len(candidates), (), (), True)

    destroyed: list[str] = []
    aborted: list[str] = []
    refused: list[str] = []
    try:
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
    finally:
        await _release_the_pass_lock(db)


async def _still_the_same_container(
    candidate: ContainerVerdict, *, revalidate: Revalidate
) -> bool:
    """Re-read the container's tags and abort on ANY change since enumeration.

    The window this closes is small and the consequence is not: `app_name_for` is deterministic,
    so between enumeration and delete a builder can start a fresh build that provisions into the
    very name this pass is about to destroy. Deleting it would take the new container; the
    trailing registry clear would then wipe the record of a container that no longer exists,
    manufacturing an orphan of exactly the kind this system was built to collect.

    A container ARM reports ABSENT is not an abort — it is already gone, which is the outcome we
    wanted, and a second delete of an absent resource is a 204 no-op."""
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

    THE TAGS ARE UNCHANGED IN THE CASE THIS CATCHES, which is why the check above cannot stand in
    for this one. A staged container stays fully attachable on purpose — `attach_existing` refuses
    anything reading `ending`, so a citizen coming back must be able to reach it — and coming back
    writes a lock, a heartbeat, a stay or an R10 lease, none of which are ARM tags. Between
    enumeration and this line the classifier's opinion can therefore go stale in the one direction
    that matters, with every tag still saying exactly what it said when we judged it.

    The predicate is `RegistryClaim.spares_the_container`, unchanged and unduplicated: one pure
    rule for "is somebody using this", evaluated once at classification and again here. A second
    spelling of it would be a second thing to keep in sync with the first, on the path where being
    wrong costs somebody their afternoon."""
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
