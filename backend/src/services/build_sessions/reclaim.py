"""The confidence-tier classifier — fleet + spare-list in, tiered verdicts out (U10, ADR-0029 §3).

PURE AND I/O-FREE, the same shape as `appdb/reconcile.py::classify_databases` and for the same
reason: this function is the safety argument for every destructive unit downstream, so every
dangerous combination has to be provable against a synthetic fleet holding all of them at once,
with no Azure, no Redis and no database in the way. The caller gathers the evidence; this decides.

HOW LONG AN UNCLAIMED CONTAINER WAITS IS SET BY HOW MANY INDEPENDENT SIGNALS CONCUR (R5), not by a
single duration. A lone age threshold defends only against "created but not yet recorded" — a
window the provisioning retry policy already bounds at ~20 minutes — and longer thresholds buy no
protection against a lost or wrong store while costing detection latency in the one signal that
would have caught both past ghosts.

A FACT YOU CANNOT READ DOES NOT BECOME TRUE BY WAITING (R4). Every path out of here that stands for
a signal which could not be read leads to `ESCALATE`. None of them defaults to destroy. A timeout
is not a death certificate.
"""

from __future__ import annotations

import datetime as dt
import enum
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from src.services.sandbox.base import KIND_BUILD_SANDBOX, FleetMember

# --- the clocks -------------------------------------------------------------------------
#
# Plain module constants like their C3-frozen neighbours: these are protocol, not deployment
# config. Every one of them is a *floor* on how long the platform waits before touching somebody
# else's container, so raising one is always safe and lowering one needs the ADR reopened.

#: A container younger than this is never a candidate, whatever else is true of it. `_start_locked`
#: takes the per-user lock BEFORE provisioning the container that writes the registry hash, so a
#: four-second-old sandbox legitimately presents as an unregistered orphan. Sized to the
#: provisioning retry policy's own ceiling rather than guessed.
PROVISIONING_GRACE = dt.timedelta(minutes=20)

#: Two independent reads, a full interval apart (ADR-0029 §5). One pass's opinion is not evidence;
#: the `bial-reclaim-staged-at` tag is how the second pass learns the first one happened.
STAGING_INTERVAL = dt.timedelta(minutes=15)

#: The scheduled cadence. Load-bearing in four places at once: the staging interval below, the
#: effective lifetime of an abandoned container, the unit of U11's staleness threshold, and the
#: floor under `last_pass_at`.
PASS_CADENCE = dt.timedelta(minutes=5)

#: A staging tag younger than one full cadence does not authorise anything — see the comment at
#: the check itself. Equal to the cadence rather than to `STAGING_INTERVAL` so that tightening the
#: cadence tightens this too, and the two can never drift into "staged on any earlier pass".
MINIMUM_STAGING_AGE = PASS_CADENCE

#: Every signal concurs — ours, unclaimed, no app record, already staged.
HIGH_CONFIDENCE_AGE = dt.timedelta(hours=1)

#: One fewer signal: a matching app record means a real builder's real app whose ownership record
#: alone is gone. Fewer concurring signals ⇒ a longer wait.
REAL_APP_AGE = dt.timedelta(hours=4)

# --- the store-fault guard ---------------------------------------------------------------

#: Below this, "the registry knows about a small fraction of the fleet" is not a proportion, it is
#: a single orphan. Refusing to judge here would mean the very first ghost this system was built
#: for escalated to a human instead of being collected.
STORE_FAULT_MIN_FLEET = 4

#: The registry should account for a decent share of a live fleet. It legitimately will not account
#: for all of it — genuine orphans are the point — so this is deliberately generous.
STORE_FAULT_MIN_CLAIM_RATIO = 0.5


class Verdict(enum.StrEnum):
    """What this pass will do about one container."""

    SPARE = "spare"  # in use, too young, or waiting out its staging interval
    STAGE = "stage"  # first sighting as a candidate; mark it and look again next pass
    DESTROY = "destroy"  # every gate passed — subject to the durable-copy check downstream
    ESCALATE = "escalate"  # a human must decide; never destroyed by a timer
    NOT_OURS = "not_ours"  # positively identified as somebody else's


class Tier(enum.StrEnum):
    """Which row of ADR-0029 §3 this container matched."""

    IN_USE = "in_use"
    HIGH_CONFIDENCE = "high_confidence"
    REAL_APP_MISSING_RECORD = "real_app_missing_record"
    CLAIMED_BUT_EXPIRED = "claimed_but_expired"
    UNREADABLE = "unreadable"
    NOT_OURS = "not_ours"


@dataclass(frozen=True)
class RegistryClaim:
    """What the coordination store says about one registered container.

    REGISTRATION ALONE IS NOT ON THIS LIST, and that absence is the point. The naive spare set —
    "every app name the registry knows" — is wrong twice over: `_pardon_the_container` deliberately
    keeps the registry entry after a turn completes, and `preview_stay_until` is a hash field
    rather than a TTL'd key. A pardoned-then-abandoned container would therefore sit in that set
    forever, which is most of what this whole system exists to collect."""

    lock_held: bool
    heartbeat_alive: bool
    stay_current: bool
    lease_held: bool

    @property
    def spares_the_container(self) -> bool:
        """`(lock held AND heartbeat alive) OR stay current OR liveness lease held`.

        The lock and the heartbeat are ONE signal, not two: a held lock whose owner stopped
        breathing is the crashed builder the reaper exists for.

        MUTATION-CHECKED. Reverting this to `True` — "registered ⇒ spared" — silently disables
        essentially all reclamation while every other test stays green, which is exactly the kind
        of regression that ships."""
        return (self.lock_held and self.heartbeat_alive) or self.stay_current or self.lease_held

    def combined_with(self, other: RegistryClaim) -> RegistryClaim:
        """One container, two records naming it: the record that SPARES wins.

        The claim map is keyed by CONTAINER NAME and the signals are keyed by USER, so two
        records can name one container — a stale entry, or a crossed one. A plain assignment
        makes the scan's last writer win, and an unrelated user's empty record then erases a live
        builder's claim. Observed against a real fleet: a container holding a lock, a live
        heartbeat and a valid liveness lease was classified `claimed_but_expired` and staged.

        NOT A FIELD-WISE `or`. OR-ing would let one record's lock and another record's heartbeat
        add up to a liveness nobody actually holds — a claim invented by the merge rather than
        made by any record. Preferring the sparing record keeps every claim something a real
        record really asserted, and points the ambiguity the same way every other gate here
        points it: toward sparing.

        When both spare or neither does, `self` stands. They can then differ only in which
        signals are set, and both land in the same tier either way."""
        return other if other.spares_the_container and not self.spares_the_container else self


@dataclass(frozen=True)
class ContainerVerdict:
    """One container's outcome, with the evidence behind it.

    `reason` is written for a human reading a report-only pass at 2am, not for a log parser. The
    tier says which rule matched; the reason says why this container matched it."""

    name: str
    tier: Tier
    verdict: Verdict
    reason: str


@dataclass(frozen=True)
class ReclamationPlan:
    """What one pass would do, and — when the coordination store looks wrong — what it refused to.

    THE BUCKETS SUM: `scanned == spared + staged + destroy + escalate + not_ours`. Not tidiness. A
    container that silently vanishes from the accounting is a container nobody is deciding about,
    which is how the first ghost survived nineteen days."""

    verdicts: tuple[ContainerVerdict, ...]
    store_fault: bool
    by_name: Mapping[str, ContainerVerdict] = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "by_name", {v.name: v for v in self.verdicts})

    def _count(self, verdict: Verdict) -> int:
        return sum(1 for v in self.verdicts if v.verdict is verdict)

    @property
    def scanned(self) -> int:
        return len(self.verdicts)

    @property
    def spared(self) -> int:
        return self._count(Verdict.SPARE)

    @property
    def staged(self) -> int:
        return self._count(Verdict.STAGE)

    @property
    def destroy(self) -> int:
        return self._count(Verdict.DESTROY)

    @property
    def escalate(self) -> int:
        return self._count(Verdict.ESCALATE)

    @property
    def not_ours(self) -> int:
        return self._count(Verdict.NOT_OURS)


def _the_registry_looks_wrong(fleet_size: int, claim_count: int) -> bool:
    """R6, and deliberately past its literal wording.

    R6 asks for "empty spare-list against a live fleet", which catches a FLUSHED Redis. It does not
    catch partial loss, and partial loss is the more dangerous shape: a live build presenting as
    registered-but-lapsed routes into the fifth tier with every individual signal reading normal,
    so a binary guard sees nothing wrong and stages it mid-build.

    Proportional, therefore — the registry should account for a decent share of a live fleet — with
    a floor under the fleet size, because a proportion needs a denominator and one unregistered
    container is an orphan rather than evidence about Redis.

    WRONG IN THE SAFE DIRECTION BY CONSTRUCTION: a false positive escalates to a human, and a
    human is what the escalate tier is for.

    HONEST LIMIT, worth stating rather than implying: this cannot catch the eviction shape where
    the registry hash SURVIVES and only the lock, stay and lease evict (the registry is the one key
    family with no TTL, so under `volatile-*` it is the last to go). Counts cannot tell that apart
    from a quiet fleet where nobody happens to be building. What defends against it is the rest
    of the chain — the staging interval, the durable-copy gate, the per-pass ceiling — not this."""
    if fleet_size < STORE_FAULT_MIN_FLEET:
        return False
    return claim_count < math.ceil(fleet_size * STORE_FAULT_MIN_CLAIM_RATIO)


def _judge_one(
    member: FleetMember,
    *,
    claim: RegistryClaim | None,
    known_app_names: frozenset[str] | None,
    now: dt.datetime,
) -> ContainerVerdict:
    identity = member.identity

    # Positively somebody else's. Distinct from "carries no identity" — that is the orphan
    # population and it escalates; this is a published app or a co-tenant workload and it is simply
    # not our business.
    if identity.kind is not None and identity.kind != KIND_BUILD_SANDBOX:
        return ContainerVerdict(
            member.name, Tier.NOT_OURS, Verdict.NOT_OURS, "not a build sandbox"
        )

    # The product database could not be read. `None` is "could not ask", not "no apps exist", and
    # the difference decides whether a real builder's app waits one hour or four.
    if known_app_names is None:
        return ContainerVerdict(
            member.name, Tier.UNREADABLE, Verdict.ESCALATE, "the product database was unreadable"
        )

    # Missing owner, app or age — or stamped by a different control plane (R22). Every one of those
    # is a signal that could not be read, and none of them expires into a decision.
    if identity.escalate_only:
        return ContainerVerdict(
            member.name,
            Tier.UNREADABLE,
            Verdict.ESCALATE,
            "carries no identity this control plane can judge",
        )

    if claim is not None and claim.spares_the_container:
        return ContainerVerdict(member.name, Tier.IN_USE, Verdict.SPARE, "in use")

    # `escalate_only` already proved this is not None; the assert is for the type checker and costs
    # nothing at runtime that a comment would not.
    created_at = identity.created_at
    assert created_at is not None  # noqa: S101 - narrowed by `escalate_only` above
    age = now - created_at
    if age < PROVISIONING_GRACE:
        return ContainerVerdict(
            member.name, Tier.IN_USE, Verdict.SPARE, "still inside the provisioning grace"
        )

    if claim is not None:
        tier, threshold = Tier.CLAIMED_BUT_EXPIRED, PROVISIONING_GRACE
    elif member.name in known_app_names:
        tier, threshold = Tier.REAL_APP_MISSING_RECORD, REAL_APP_AGE
    else:
        tier, threshold = Tier.HIGH_CONFIDENCE, HIGH_CONFIDENCE_AGE

    if age < threshold:
        return ContainerVerdict(member.name, tier, Verdict.SPARE, "too young for its tier")

    staged_at = identity.reclaim_staged_at
    if staged_at is None:
        return ContainerVerdict(member.name, tier, Verdict.STAGE, "first sighting as a candidate")
    if now - staged_at < MINIMUM_STAGING_AGE:
        # A MINIMUM AGE, not merely "staged on some earlier pass". Reclamation is
        # operator-triggerable, so two back-to-back manual invocations would otherwise satisfy the
        # two-independent-reads rule with zero elapsed time between them — which is two readings
        # of one instant, i.e. one reading. Sized to a full cadence interval: anything smaller
        # would constrain only manual runs and do nothing at all on the scheduled path.
        return ContainerVerdict(
            member.name, tier, Verdict.SPARE, "waiting out the minimum staging age"
        )
    return ContainerVerdict(
        member.name, tier, Verdict.DESTROY, "staged on an earlier pass and idle since"
    )


def classify_fleet(
    fleet: Iterable[FleetMember],
    *,
    claims: Mapping[str, RegistryClaim],
    known_app_names: frozenset[str] | None,
    now: dt.datetime,
) -> ReclamationPlan:
    """Bucket every enumerated container.

    `claims` maps app name → what the coordination store says. A name ABSENT from it is
    unregistered; a name present with every signal lapsed is the fifth tier (F1), which runs
    through the identical durable-copy → staging → ceiling → destroy chain rather than sitting
    outside the gates. Porting F1 forward *outside* them would have left the code that does almost
    all of the deleting subject to none of the new safety.

    `known_app_names` is the set of container names with a matching app record, or `None` when the
    product database could not be read — in which case the whole fleet escalates."""
    members = list(fleet)
    store_fault = _the_registry_looks_wrong(len(members), len(claims))
    if store_fault:
        # NOTHING IS TOUCHED ON A PASS THAT DOES NOT TRUST ITS OWN INPUTS. Reported, not silent:
        # the alarm is the output here, and the caller raises it.
        return ReclamationPlan(
            verdicts=tuple(
                ContainerVerdict(
                    m.name,
                    Tier.UNREADABLE,
                    Verdict.ESCALATE,
                    "the coordination store accounts for too little of the live fleet",
                )
                for m in members
            ),
            store_fault=True,
        )
    return ReclamationPlan(
        verdicts=tuple(
            _judge_one(m, claim=claims.get(m.name), known_app_names=known_app_names, now=now)
            for m in members
        ),
        store_fault=False,
    )
