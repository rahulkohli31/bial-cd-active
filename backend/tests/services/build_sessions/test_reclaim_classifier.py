"""U10 — the confidence-tier classifier (R4, R5, R6).

WRITTEN BEFORE THE IMPLEMENTATION, deliberately: this function IS the safety argument for every
destructive unit downstream, so the tier table from ADR-0029 §3 is spelled out as tests first and
the code is written to satisfy them.

The classifier is pure and I/O-free — the same shape as `appdb/reconcile.py::classify_databases`,
and for the same reason. Every dangerous combination can then be proven against a synthetic fleet
that holds all of them at once, with no Azure, no Redis and no database in the way.

THE TWO ASSERTIONS THAT MATTER MOST, both mutation-checked:

* `test_a_registered_container_whose_signals_have_all_lapsed_is_a_candidate` — reverting the spare
  set to "registered ⇒ spared" silently disables ~all reclamation while every other test here
  stays green.
* `test_a_partially_lost_spare_list_trips_the_store_fault_guard` — the eviction shape. Reverting
  the guard to empty-only leaves a live build routed into staging with every signal reading normal.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

from src.services.build_sessions.reclaim import (
    MINIMUM_STAGING_AGE,
    PROVISIONING_GRACE,
    ReclamationPlan,
    RegistryClaim,
    Tier,
    Verdict,
    classify_fleet,
)
from src.services.sandbox.base import (
    KIND_BUILD_SANDBOX,
    TAG_APP_ID,
    TAG_CONTROL_PLANE,
    TAG_CREATED_AT,
    TAG_KIND,
    TAG_RECLAIM_STAGED_AT,
    TAG_USER_ID,
    FleetMember,
    control_plane_segment,
)
from tests.fakes import a_fleet_member

NOW = dt.datetime(2026, 8, 11, 12, 0, tzinfo=dt.UTC)
#: A staging tag old enough to authorise the second read. Deliberately past the minimum
#: rather than exactly on it: a boundary-exact fixture turns any future tightening of the
#: interval into a suite-wide failure that says nothing about what actually broke.
STAGED_LONG_ENOUGH = MINIMUM_STAGING_AGE * 2
USER = uuid.uuid4()
APP = uuid.uuid4()


def _tags(
    *,
    age: dt.timedelta = dt.timedelta(hours=6),
    staged: dt.timedelta | None = None,
    control_plane: str | None = None,
    owned: bool = True,
) -> dict[str, str]:
    """A fully-identified sandbox's tags, aged `age` ago. The defaults describe the container this
    system exists to collect: ours, old enough to judge, and nobody's business but ours."""
    tags = {
        TAG_KIND: KIND_BUILD_SANDBOX,
        TAG_CREATED_AT: (NOW - age).isoformat(),
        TAG_CONTROL_PLANE: control_plane or control_plane_segment(),
    }
    if owned:
        tags[TAG_USER_ID] = str(USER)
        tags[TAG_APP_ID] = str(APP)
    if staged is not None:
        tags[TAG_RECLAIM_STAGED_AT] = (NOW - staged).isoformat()
    return tags


def _claim(
    *,
    lock: bool = False,
    heartbeat: bool = False,
    stay: bool = False,
    lease: bool = False,
) -> RegistryClaim:
    """What the coordination store says about one registered container. Every signal defaults to
    LAPSED, because "registered" on its own is exactly the thing that must not spare anything."""
    return RegistryClaim(
        lock_held=lock, heartbeat_alive=heartbeat, stay_current=stay, lease_held=lease
    )


def _judge(
    fleet: list[FleetMember],
    *,
    claims: dict[str, RegistryClaim] | None = None,
    known_apps: frozenset[str] | None = frozenset(),
    now: dt.datetime = NOW,
) -> ReclamationPlan:
    return classify_fleet(fleet, claims=claims or {}, known_app_names=known_apps, now=now)


def _healthy_padding(count: int = 6) -> tuple[list[FleetMember], dict[str, RegistryClaim]]:
    """A fleet large enough to talk about proportions, all of it plainly alive.

    Needed because the store-fault guard is about the RATIO of registry claims to live containers,
    so a one-container fleet cannot express "the registry lost most of what it knew"."""
    members = [a_fleet_member(f"sbx-live{i}", tags=_tags()) for i in range(count)]
    claims = {f"sbx-live{i}": _claim(lock=True, heartbeat=True) for i in range(count)}
    return members, claims


# --- the tier table (ADR-0029 §3) -------------------------------------------------


def test_high_confidence_orphan_is_destroyed_at_one_hour() -> None:
    """*Covers AE1.* Carries our identity, absent from the spare set, no matching app record,
    staged on an earlier pass, past its hour. Every signal concurs, so the wait is short."""
    live, claims = _healthy_padding()
    doomed = a_fleet_member(
        "sbx-ghost", tags=_tags(age=dt.timedelta(hours=2), staged=STAGED_LONG_ENOUGH)
    )

    plan = _judge([*live, doomed], claims=claims)

    verdict = plan.by_name["sbx-ghost"]
    assert verdict.tier is Tier.HIGH_CONFIDENCE
    assert verdict.verdict is Verdict.DESTROY


def test_an_untagged_container_escalates_forever_however_old_it_is() -> None:
    """*Covers AE2.* Predates identity stamping, so nothing about it can be verified. Age is not
    evidence — the nineteen-day ghost was exactly this shape, and guessing would have been guessing
    about somebody's unsaved work."""
    live, claims = _healthy_padding()
    ancient = a_fleet_member("sbx-prehistoric", tags={})

    plan = _judge([*live, ancient], claims=claims)

    verdict = plan.by_name["sbx-prehistoric"]
    assert verdict.tier is Tier.UNREADABLE
    assert verdict.verdict is Verdict.ESCALATE


@pytest.mark.parametrize(
    ("age", "expected"),
    [(dt.timedelta(hours=2), Verdict.SPARE), (dt.timedelta(hours=5), Verdict.DESTROY)],
    ids=["two-hours-spared", "five-hours-destroyed"],
)
def test_an_unclaimed_container_with_a_real_app_record_waits_longer(
    age: dt.timedelta, expected: Verdict
) -> None:
    """*Covers AE3.* A matching app record means a real builder's real app whose ownership record
    alone is gone — one fewer independent signal concurring, so a longer wait before it is touched
    (four hours, against the high-confidence hour)."""
    live, claims = _healthy_padding()
    member = a_fleet_member("sbx-realapp", tags=_tags(age=age, staged=STAGED_LONG_ENOUGH))

    plan = _judge([*live, member], claims=claims, known_apps=frozenset({"sbx-realapp"}))

    verdict = plan.by_name["sbx-realapp"]
    assert verdict.tier is Tier.REAL_APP_MISSING_RECORD
    assert verdict.verdict is expected


def test_a_registered_container_whose_signals_have_all_lapsed_is_a_candidate() -> None:
    """*The fifth tier (F1), and the mutation-check that guards it.*

    Registered, but lock, stay and liveness lease have ALL lapsed. `_pardon_the_container` keeps
    the registry entry after a turn completes and `preview_stay_until` is a hash field rather than
    a TTL'd key, so a pardoned-then-abandoned container sits in the registry forever. This is the
    path that produces essentially all of the cost saving.

    MUTATION: revert the spare set to "registered ⇒ spared" and this goes red while every other
    test in this file stays green — which is precisely why it is worth writing."""
    live, claims = _healthy_padding()
    abandoned = a_fleet_member("sbx-pardoned", tags=_tags(staged=STAGED_LONG_ENOUGH))
    claims["sbx-pardoned"] = _claim()  # registered; every signal lapsed

    plan = _judge([*live, abandoned], claims=claims)

    verdict = plan.by_name["sbx-pardoned"]
    assert verdict.tier is Tier.CLAIMED_BUT_EXPIRED
    assert verdict.verdict is Verdict.DESTROY


# --- what spares a container ------------------------------------------------------


def test_a_build_ninety_seconds_in_is_spared_by_its_liveness_lease() -> None:
    """*Covers AE6.* The heartbeat is seeded once per turn against a 90-second TTL, so from ~90s
    into any build the lease is the ONLY thing a process that is not running the build can see."""
    live, claims = _healthy_padding()
    building = a_fleet_member("sbx-building", tags=_tags(age=dt.timedelta(minutes=90)))
    claims["sbx-building"] = _claim(lease=True)

    plan = _judge([*live, building], claims=claims)

    assert plan.by_name["sbx-building"].verdict is Verdict.SPARE


def test_a_current_stay_of_execution_spares_a_container_whose_lock_has_lapsed() -> None:
    """A relaunched preview holds no lock and runs no turn; the stay is the whole of its claim."""
    live, claims = _healthy_padding()
    previewing = a_fleet_member("sbx-preview", tags=_tags())
    claims["sbx-preview"] = _claim(stay=True)

    plan = _judge([*live, previewing], claims=claims)

    assert plan.by_name["sbx-preview"].verdict is Verdict.SPARE


def test_a_lock_without_a_live_heartbeat_does_not_spare() -> None:
    """The two are one signal, not two. A held lock whose owner stopped breathing is the crashed
    builder the reaper exists for."""
    live, claims = _healthy_padding()
    crashed = a_fleet_member("sbx-crashed", tags=_tags(staged=STAGED_LONG_ENOUGH))
    claims["sbx-crashed"] = _claim(lock=True, heartbeat=False)

    plan = _judge([*live, crashed], claims=claims)

    assert plan.by_name["sbx-crashed"].verdict is Verdict.DESTROY


def test_a_container_mid_provision_is_not_a_candidate() -> None:
    """`_start_locked` takes the lock BEFORE it provisions the container that writes the registry
    hash, so a four-second-old container legitimately looks exactly like an orphan. That window is
    the reason this whole module is report-only for a release."""
    live, claims = _healthy_padding()
    newborn = a_fleet_member("sbx-newborn", tags=_tags(age=dt.timedelta(seconds=4)))

    plan = _judge([*live, newborn], claims=claims)

    verdict = plan.by_name["sbx-newborn"]
    assert verdict.verdict is Verdict.SPARE
    assert verdict.reason == "still inside the provisioning grace"


# --- staging: two independent reads, a full interval apart ------------------------


def test_a_first_sighting_is_staged_never_destroyed() -> None:
    """ADR-0029 §5. One read is an opinion; the tag is how the second pass learns the first one
    happened, and the interval between them is what makes them independent."""
    live, claims = _healthy_padding()
    fresh = a_fleet_member("sbx-firstlook", tags=_tags())

    plan = _judge([*live, fresh], claims=claims)

    assert plan.by_name["sbx-firstlook"].verdict is Verdict.STAGE


def test_a_staging_mark_younger_than_the_minimum_age_still_waits() -> None:
    """Staged one minute ago is not two independent reads; it is one read and a rounding error.

    The bound is MINIMUM_STAGING_AGE (one full cadence), not "any earlier pass" — reclamation is
    operator-triggerable, so two back-to-back manual invocations would otherwise satisfy the
    two-reads rule with zero elapsed time between them."""
    live, claims = _healthy_padding()
    hasty = a_fleet_member("sbx-hasty", tags=_tags(staged=MINIMUM_STAGING_AGE / 2))

    plan = _judge([*live, hasty], claims=claims)

    assert plan.by_name["sbx-hasty"].verdict is Verdict.SPARE


def test_a_staged_container_that_came_back_to_life_is_spared_not_destroyed() -> None:
    """The staging mark is evidence, not a sentence. A builder who returns between two passes
    outranks whatever the earlier pass believed."""
    live, claims = _healthy_padding()
    revived = a_fleet_member("sbx-revived", tags=_tags(staged=STAGED_LONG_ENOUGH))
    claims["sbx-revived"] = _claim(lease=True)

    plan = _judge([*live, revived], claims=claims)

    assert plan.by_name["sbx-revived"].verdict is Verdict.SPARE


# --- an unreadable signal escalates; it never expires into a decision --------------


def test_a_container_from_another_control_plane_is_never_ours_to_sentence() -> None:
    """R22. Reading somebody else's container is fine; sentencing it is not."""
    live, claims = _healthy_padding()
    theirs = a_fleet_member("sbx-theirs", tags=_tags(control_plane="some-other-plane"))

    plan = _judge([*live, theirs], claims=claims)

    verdict = plan.by_name["sbx-theirs"]
    assert verdict.tier is Tier.UNREADABLE
    assert verdict.verdict is Verdict.ESCALATE


def test_an_unavailable_product_database_escalates_the_entire_fleet() -> None:
    """`known_app_names=None` is "could not ask", not "no apps exist". The difference decides
    whether a real builder's app is a high-confidence orphan or a four-hour one, and a fact you
    cannot read does not become true by waiting."""
    live, claims = _healthy_padding()
    doomed = a_fleet_member("sbx-ghost", tags=_tags(staged=STAGED_LONG_ENOUGH))

    plan = _judge([*live, doomed], claims=claims, known_apps=None)

    assert plan.escalate == plan.scanned
    assert plan.destroy == plan.staged == 0
    assert all(v.tier is Tier.UNREADABLE for v in plan.by_name.values())


def test_a_published_app_is_not_ours_and_is_never_counted_as_an_orphan() -> None:
    live, claims = _healthy_padding()
    published = a_fleet_member("sbx-mislabelled", tags={TAG_KIND: "published-app"})

    plan = _judge([*live, published], claims=claims)

    assert plan.by_name["sbx-mislabelled"].verdict is Verdict.NOT_OURS


# --- the store-fault guard (R6, and past its literal wording) ---------------------


def test_an_empty_spare_list_against_a_live_fleet_destroys_nothing() -> None:
    """*Covers AE4.* Eleven live sandboxes and a registry that claims none of them is a story about
    Redis, not about eleven abandoned containers."""
    fleet = [a_fleet_member(f"sbx-{i}", tags=_tags(staged=STAGED_LONG_ENOUGH)) for i in range(11)]

    plan = _judge(fleet, claims={})

    assert plan.store_fault is True
    assert plan.destroy == 0
    assert plan.escalate == 11


def test_a_partially_lost_spare_list_trips_the_store_fault_guard() -> None:
    """THE EVICTION SHAPE, and the more dangerous of the two.

    The registry hash is the only key family with no TTL, so under any `volatile-*` policy the
    lock, the stay and the lease evict FIRST while the registry survives. A live build then reads
    as registered-but-lapsed — the fifth tier — with a non-empty registry, so a binary
    empty-or-not guard sees nothing wrong and routes it into staging and destruction mid-build.

    MUTATION: revert the guard to `not claims` and this goes red while every other test stays
    green. That is the whole reason the guard is proportional rather than binary."""
    fleet = [a_fleet_member(f"sbx-{i}", tags=_tags(staged=STAGED_LONG_ENOUGH)) for i in range(11)]
    claims = {"sbx-0": _claim(lock=True, heartbeat=True), "sbx-1": _claim(lease=True)}

    plan = _judge(fleet, claims=claims)

    assert plan.store_fault is True
    assert plan.destroy == 0


def test_a_healthy_ratio_of_claims_does_not_trip_the_guard() -> None:
    """The guard has to be wrong in the safe direction, not wrong in every direction: a fleet the
    registry mostly accounts for still reclaims its genuine orphans."""
    live, claims = _healthy_padding(count=8)
    doomed = a_fleet_member("sbx-ghost", tags=_tags(staged=STAGED_LONG_ENOUGH))

    plan = _judge([*live, doomed], claims=claims)

    assert plan.store_fault is False
    assert plan.by_name["sbx-ghost"].verdict is Verdict.DESTROY


def test_a_tiny_fleet_cannot_trip_the_guard() -> None:
    """A proportion needs a denominator. One unregistered container is an orphan, not evidence
    about Redis — and treating it as a store fault would mean the very first ghost this system was
    built for escalated instead of being collected."""
    lonely = a_fleet_member("sbx-only", tags=_tags(staged=STAGED_LONG_ENOUGH))

    plan = _judge([lonely], claims={})

    assert plan.store_fault is False
    assert plan.by_name["sbx-only"].verdict is Verdict.DESTROY


# --- the accounting invariant -----------------------------------------------------


def test_every_container_lands_in_exactly_one_bucket() -> None:
    """`scanned == spared + staged + destroy + escalate + not_ours`, stated as an invariant rather
    than hoped for. A container that silently vanished from the accounting is a container nobody
    is deciding about — which is how the first ghost survived nineteen days."""
    live, claims = _healthy_padding()
    fleet = [
        *live,
        a_fleet_member("sbx-untagged", tags={}),
        a_fleet_member("sbx-published", tags={TAG_KIND: "published-app"}),
        a_fleet_member("sbx-ghost", tags=_tags(staged=STAGED_LONG_ENOUGH)),
        a_fleet_member("sbx-fresh", tags=_tags()),
        a_fleet_member("sbx-newborn", tags=_tags(age=dt.timedelta(seconds=4))),
        a_fleet_member("sbx-theirs", tags=_tags(control_plane="elsewhere")),
    ]

    plan = _judge(fleet, claims=claims)

    assert plan.scanned == len(fleet)
    assert plan.scanned == plan.spared + plan.staged + plan.destroy + plan.escalate + plan.not_ours
    assert len(plan.by_name) == plan.scanned


def test_the_provisioning_grace_is_shorter_than_every_destroy_threshold() -> None:
    """Otherwise the grace would be the only threshold that ever fired, and the tier table would
    be decoration."""
    assert PROVISIONING_GRACE < MINIMUM_STAGING_AGE * 2
    assert PROVISIONING_GRACE < dt.timedelta(hours=1)
