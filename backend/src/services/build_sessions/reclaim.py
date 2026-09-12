"""The confidence-tier classifier — fleet + spare-list in, tiered verdicts out.

WHY THIS EXISTS
Pure and I/O-free, like `appdb/reconcile.py::classify_databases`: this is the safety argument for
every destructive unit downstream, so every dangerous combination must be provable against a
synthetic fleet holding all of them at once, with no Azure, Redis, or database in the way. The
caller gathers evidence; this decides.

How long an unclaimed container waits is set by how many signals concur, not a single duration — a
lone age threshold only defends "created but not yet recorded" (a window the provisioning retry
policy already bounds at ~20 minutes), not a lost or wrong store.

A fact you cannot read does not become true by waiting. Every unreadable signal leads to
`ESCALATE`, never DESTROY — a timeout is not a death certificate.

One workspace per user, and nobody takes it by force. The registry is keyed by user, so a live
container belonging to another of that user's projects is why theirs is not up; reclaiming it is
the citizen's call, never the platform's — a relaunch or turn that would displace it refuses with a
409 (`manager.py::SandboxReclaimBlockedError`), and the portal offers to save the incumbent's work
instead. The scheduled pass below applies the same principle in reverse: a container any signal
still claims is spared, never collected.
"""

from __future__ import annotations

import datetime as dt
import enum
import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

from src.services.sandbox.base import KIND_BUILD_SANDBOX, KIND_SHARED_SANDBOX, FleetMember

# --- the clocks -------------------------------------------------------------------------
#
# Plain module constants like their frozen neighbours: these are protocol, not deployment
# config. Every one of them is a *floor* on how long the platform waits before touching somebody
# else's container, so raising one is always safe and lowering one is a decision to reopen.

#: A container younger than this is never a candidate, whatever else is true of it. A sandbox a
#: few seconds old legitimately presents as an unregistered orphan — `inventory.py` documents that
#: window — so this is sized to the provisioning retry policy's own ceiling rather than guessed.
PROVISIONING_GRACE = dt.timedelta(minutes=20)

#: THE RECLAMATION PASS'S OWN CADENCE. Five minutes is the cadence of the *sweep*
#: (`SANDBOX_REAP_CRON`) — a different worker doing different work. The reclamation pass runs on
#: `RECLAMATION_CRON`, every fifteen.
#:
#: Load-bearing in three places at once: the minimum staging age, the effective lifetime of an
#: abandoned container, and the unit of the staleness threshold. `pass_history` derives its
#: window from the cron string directly and a test pins the two together, so this cannot drift
#: back out of agreement without something going red.
PASS_CADENCE = dt.timedelta(minutes=15)

#: Two independent reads, a full interval apart. One pass's opinion is not evidence;
#: the `bial-reclaim-staged-at` tag is how the second pass learns the first one happened.
#:
#: ONE CONSTANT, NOT TWO. This was `MINIMUM_STAGING_AGE = PASS_CADENCE` (5m) sitting beside a
#: `STAGING_INTERVAL` of 15m that nothing in `src/` ever read — so the protocol documented one
#: interval and enforced a third of it, and the only reader of the 15 was the test suite. The
#: enforced number is the real one; the other is deleted rather than reconciled.
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
    """Which confidence tier this container matched."""

    IN_USE = "in_use"
    HIGH_CONFIDENCE = "high_confidence"
    REAL_APP_MISSING_RECORD = "real_app_missing_record"
    CLAIMED_BUT_EXPIRED = "claimed_but_expired"
    UNREADABLE = "unreadable"
    NOT_OURS = "not_ours"
    SHARED_SANDBOX = "shared_sandbox"


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
    starting: bool

    @property
    def spares_the_container(self) -> bool:
        """`(lock held AND heartbeat alive) OR stay current OR liveness lease held OR starting`.
        Lock and heartbeat count as ONE signal: a held lock whose owner stopped breathing is the
        crashed builder this exists to catch. `starting` covers the one narrow window nothing else
        can: after a turn claims the workspace but before the registry hash and heartbeat are
        written, when no lease or stay can exist yet. It is a BOUNDED claim, not a pardon — a
        mandatory wall-clock TTL forces re-evaluation once it expires. MUTATION-CHECKED: reverting
        this to `True` silently disables reclamation while every other test stays green."""
        return (
            (self.lock_held and self.heartbeat_alive)
            or self.stay_current
            or self.lease_held
            or self.starting
        )

    def combined_with(self, other: RegistryClaim) -> RegistryClaim:
        """One container, two records naming it: the record that SPARES wins.

        Claims are keyed by user while the claim map is keyed by container name, so two records
        can name one container (a stale or crossed entry); a plain assignment lets the last
        writer win and an unrelated user's empty record erase a live claim.

        NOT a field-wise `or` — that would invent a liveness no record asserts by combining one's
        lock with another's heartbeat. Ties keep `self`; both land in the same tier regardless."""
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

    THE BUCKETS SUM: `scanned == spared + staged + destroy + escalate + not_ours`. Not tidiness.
    A container that silently vanishes from the accounting is nobody's to decide about."""

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
    """The store-fault guard, deliberately past what a literal reading of the requirement would
    need: "empty spare-list against a live fleet" catches a flushed Redis but not partial loss,
    the more dangerous shape where a live build presenting as registered-but-lapsed reads normal
    on every individual signal. So this is proportional instead (with a floor under fleet size
    for a sane denominator) and wrong in the safe direction — a false positive only escalates to
    a human. It cannot catch the registry hash surviving while lock, stay and lease evict (the
    registry key alone has no TTL); the staging interval, the durable-copy gate and the per-pass
    ceiling catch that instead.

    WATCH THIS ON THE DAY `shr-` (#198) JOINS THE FLEET THIS CLASSIFIER SEES. `claims` is
    sourced from the SAME per-user build-sandbox registry keyspace `sbx-` claims live in
    (`reclamation_pass.py::_registry_claims`); a shared-runtime sandbox with no matching claim
    mechanism of its own would inflate `fleet_size` here without inflating `claim_count`,
    depressing the ratio and escalating the ENTIRE fleet — `sbx-` included — on a population
    this function was never told to expect. Whoever widens `list_sandbox_fleet`'s ARM filter
    (still `sbx-`-only as of #198 slice 2) past this comment must wire a matching claim source
    for `shr-` in the same change, not after."""
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

    # OURS, but not this classifier's business (#198) — distinct from the NOT_OURS branch just
    # below, and checked FIRST so it never falls into it. A shared-runtime sandbox is neither a
    # build sandbox nor a published app nor somebody else's workload; it is this platform's own
    # third lineage, with its own claim/liveness signals (a Slice-3 concern) that this
    # build-sandbox-only classifier does not read. Escalating rather than either destroying it
    # on a policy this classifier has no basis for, or — the actual defect this branch exists to
    # prevent — falling through to NOT_OURS and going invisible to every report this pass makes,
    # the exact blind spot the orphan inventory exists to close for `sbx-` (see `inventory.py`).
    if identity.kind == KIND_SHARED_SANDBOX:
        return ContainerVerdict(
            member.name,
            Tier.SHARED_SANDBOX,
            Verdict.ESCALATE,
            "a shared-runtime sandbox; this classifier does not yet judge this kind",
        )

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

    # Missing owner, app or age — or stamped by a different control plane. Every one of those
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

    `claims` maps app name to what the coordination store says; an absent name is unregistered,
    and a present name with every signal lapsed is the fifth tier — it runs the same
    durable-copy → staging → ceiling → destroy chain, not a separate path.

    `known_app_names` is the set of container names with a matching app record, or `None` when the
    product database could not be read, in which case the whole fleet escalates."""
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
