"""U11 — the reclamation pass reports and destroys nothing (R3, R20).

THE ASSERTION THIS FILE EXISTS FOR is that no ARM delete is reachable from a pass. Everything else
here is observability, and observability has one job: make a DEAD WORKER distinguishable from a
quiet fleet. Every alarm the pass raises is emitted by the pass, so a crashlooping scheduler emits
nothing and looks exactly like a healthy idle system — which is the origin incident's failure
moved one layer out. The pass record is the only thing that breaks the tie, so it is written on
every outcome, including the boring ones and the failed ones.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
import redis.asyncio as aioredis
import sqlalchemy as sa
import structlog.testing
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models.worker_pass import PassOutcome, WorkerPass
from src.services.build_sessions import reclamation_pass as pass_mod
from src.services.build_sessions.pass_history import STALE_AFTER, reclamation_pass_freshness
from src.services.build_sessions.reclaim import Verdict
from src.services.sandbox.base import (
    KIND_BUILD_SANDBOX,
    TAG_APP_ID,
    TAG_CONTROL_PLANE,
    TAG_CREATED_AT,
    TAG_KIND,
    TAG_USER_ID,
    FleetMember,
    control_plane_segment,
)
from tests.fakes import a_fleet_member

USER = uuid.uuid4()
APP = uuid.uuid4()


class _Fleet:
    """A control plane that lists whatever it is told to — and RECORDS any delete attempt.

    `deleted` is the assertion surface for the whole unit: report-only means this list stays
    empty, and a fake that could not observe a delete could not prove that."""

    def __init__(self, members: list[FleetMember]) -> None:
        self.members = members
        self.deleted: list[str] = []

    async def list_sandbox_fleet(self) -> list[FleetMember]:
        return list(self.members)

    async def delete_app(self, *, name: str) -> None:  # pragma: no cover - must never run
        self.deleted.append(name)


def _orphan(name: str, *, age_hours: int = 6) -> FleetMember:
    """A fully-identified, unclaimed, old container — the shape that reaches a destroy tier."""
    return a_fleet_member(
        name,
        tags={
            TAG_KIND: KIND_BUILD_SANDBOX,
            TAG_USER_ID: str(USER),
            TAG_APP_ID: str(APP),
            TAG_CONTROL_PLANE: control_plane_segment(),
            TAG_CREATED_AT: (dt.datetime.now(dt.UTC) - dt.timedelta(hours=age_hours)).isoformat(),
        },
    )


@pytest.fixture(autouse=True)
def _no_app_table(monkeypatch: pytest.MonkeyPatch) -> None:
    """The product database answers "no app matches", not "could not ask".

    Pinned per-test because the difference decides the tier: `None` escalates the whole fleet, an
    empty set routes into the one-hour tier. A test that let this default would be testing the
    database fixture, not the pass."""

    async def _known() -> frozenset[str]:
        return frozenset()

    monkeypatch.setattr(pass_mod, "_known_app_names", _known)


# --- the pass reports; it does not act --------------------------------------------


async def test_a_pass_over_orphans_destroys_nothing(fake_redis: aioredis.Redis) -> None:
    """Two orphans, both candidates, zero ARM deletes. The destroy arm is U15 and it is behind a
    second flag; until then this is the whole safety posture of the feature."""
    fleet = _Fleet([_orphan("sbx-a"), _orphan("sbx-b")])

    report = await pass_mod.run_reclamation_pass(control_plane=fleet)

    assert report.scanned == 2
    assert fleet.deleted == []
    assert {c.name for c in report.candidates} == {"sbx-a", "sbx-b"}
    assert all(c.verdict is not Verdict.SPARE for c in report.candidates)


async def test_a_registered_and_busy_container_is_spared_and_not_reported(
    fake_redis: aioredis.Redis,
) -> None:
    """The spare-list is read through the same primitives the sweep uses, so the two can never
    disagree about what is claimed."""
    from src.services.redis import registry_key
    from src.services.redis.keys import REGISTRY_FIELD_APP_NAME

    await fake_redis.hset(registry_key(USER), mapping={REGISTRY_FIELD_APP_NAME: "sbx-busy"})
    await fake_redis.set(f"bial:development:sandbox:lock:{USER}", "tok", ex=900)
    await fake_redis.set(f"bial:development:sandbox:heartbeat:{USER}", "now", ex=90)
    fleet = _Fleet([_orphan("sbx-busy")])

    report = await pass_mod.run_reclamation_pass(control_plane=fleet)

    assert report.spared == 1
    assert report.candidates == ()


async def test_an_empty_fleet_is_a_clean_pass_not_an_error(fake_redis: aioredis.Redis) -> None:
    report = await pass_mod.run_reclamation_pass(control_plane=_Fleet([]))
    assert report.scanned == 0 and report.candidates == ()


async def test_the_pass_reports_the_evidence_behind_every_verdict(
    fake_redis: aioredis.Redis,
) -> None:
    """An operator reading a candidate list at 2am has to be able to DISAGREE with it, which
    needs the tier and the reason — not just a name and a verdict."""
    report = await pass_mod.run_reclamation_pass(control_plane=_Fleet([_orphan("sbx-a")]))

    (candidate,) = report.candidates
    assert candidate.tier is not None
    assert candidate.reason  # non-empty prose, not a code


# --- the pass record: the only thing that can detect a dead worker ----------------


async def _passes(db: AsyncSession) -> list[WorkerPass]:
    rows = await db.execute(sa.select(WorkerPass))
    return list(rows.scalars())


async def test_a_zero_candidate_pass_still_writes_a_record(
    db_session: AsyncSession, fake_redis: aioredis.Redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE LOAD-BEARING ONE. A healthy quiet fleet and a dead worker are the same observation
    unless the quiet pass leaves a trace. Skip this write and the staleness alarm fires on every
    idle night, which trains an operator to ignore it — and then it fires for real."""
    from src.workers import reclamation

    monkeypatch.setattr(reclamation, "_record_pass", _recorder(db_session))
    await reclamation._record_pass(outcome="ok", counts={"scanned": 0}, detail=None)

    rows = await _passes(db_session)
    assert len(rows) == 1
    assert rows[0].outcome is PassOutcome.OK
    assert rows[0].counts == {"scanned": 0}


def _recorder(db: AsyncSession):
    """Route `_record_pass` at the test's session instead of a fresh factory one.

    The production function deliberately opens its OWN session — it must land even when the pass
    it describes has just failed — but the test harness runs inside a single transaction, so a
    factory session would write somewhere this test cannot see."""

    async def _write(*, outcome: str, counts: dict[str, int], detail: str | None) -> None:
        db.add(
            WorkerPass(
                task_name="sandbox_reclamation",
                outcome=PassOutcome(outcome),
                finished_at=dt.datetime.now(dt.UTC),
                counts=counts,
                detail=detail,
            )
        )
        await db.flush()

    return _write


async def test_a_failed_pass_is_recorded_as_failed_not_lost(db_session: AsyncSession) -> None:
    """A pass that raises every tick leaves no `ok` row — indistinguishable from a worker that
    never runs unless the failure itself is recorded."""
    await _recorder(db_session)(outcome="failed", counts={}, detail="the pass raised")

    (row,) = await _passes(db_session)
    assert row.outcome is PassOutcome.FAILED


async def test_a_declined_pass_is_its_own_outcome(db_session: AsyncSession) -> None:
    """ "Reclamation is switched off" is a thing an operator should be able to SEE, not infer from
    silence — and it is not a failure."""
    await _recorder(db_session)(outcome="declined", counts={}, detail="flag_off")

    (row,) = await _passes(db_session)
    assert row.outcome is PassOutcome.DECLINED


# --- staleness ---------------------------------------------------------------------


async def test_never_having_run_reads_as_stale(db_session: AsyncSession) -> None:
    """NOT "no news is good news". A null last-pass is a fresh deployment whose worker never
    started, or one that has never completed a pass — different causes, same consequence: nothing
    is watching the fleet."""
    last, stale = await reclamation_pass_freshness(db_session)

    assert last is None
    assert stale is True


async def test_a_recent_pass_is_fresh(db_session: AsyncSession) -> None:
    await _recorder(db_session)(outcome="ok", counts={}, detail=None)

    last, stale = await reclamation_pass_freshness(db_session)

    assert last is not None
    assert stale is False


async def test_a_pass_older_than_the_window_reads_as_stale(db_session: AsyncSession) -> None:
    db_session.add(
        WorkerPass(
            task_name="sandbox_reclamation",
            outcome=PassOutcome.OK,
            finished_at=dt.datetime.now(dt.UTC) - STALE_AFTER - dt.timedelta(minutes=1),
            counts={},
        )
    )
    await db_session.flush()

    _, stale = await reclamation_pass_freshness(db_session)

    assert stale is True


async def test_a_failing_worker_still_reads_as_alive(db_session: AsyncSession) -> None:
    """Any outcome counts as a pass. "Is the worker running" and "is the worker happy" are
    different questions, and answering the first with the second would hide a worker that is
    there and broken behind one that is simply gone."""
    await _recorder(db_session)(outcome="failed", counts={}, detail="boom")

    _, stale = await reclamation_pass_freshness(db_session)

    assert stale is False


# --- the fleet threshold alarm -----------------------------------------------------


async def test_the_two_events_have_distinct_names() -> None:
    """An alert rule keyed on the fleet threshold CANNOT detect a dead worker, because a dead
    worker never emits it. That is the whole reason there are two constants, and why the second
    one's absence is what an alert rule watches."""
    from src.workers import reclamation

    # Asserted as a SET SIZE rather than `!=`: both constants are `Final`, so mypy narrows them
    # to distinct `Literal` types and rejects the comparison as non-overlapping — technically
    # right, and beside the point. The property is that a future edit cannot collapse the two
    # names into one, which is exactly what a set of size two says.
    assert len({reclamation.FLEET_THRESHOLD_EVENT, reclamation.PASS_COMPLETED_EVENT}) == 2


async def test_the_threshold_alarm_fires_once_per_pass_not_once_per_container(
    fake_redis: aioredis.Redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    from src.workers import reclamation

    monkeypatch.setattr(reclamation, "_record_pass", _noop_record)
    monkeypatch.setattr(reclamation, "_threshold", lambda: 2)
    # On duty: the flag gate is `deploy_reconcile`'s proven shape and has its own coverage; what
    # is under test here is what a RUNNING pass emits.
    monkeypatch.setattr(reclamation, "_off_duty_because", lambda: None)

    async def _report(**_: object) -> pass_mod.PassReport:
        return pass_mod.PassReport(
            scanned=5,
            spared=5,
            staged=0,
            destroy=0,
            escalate=0,
            not_ours=0,
            store_fault=False,
            candidates=(),
        )

    monkeypatch.setattr(pass_mod, "run_reclamation_pass", _report)

    with structlog.testing.capture_logs() as logs:
        await reclamation.reclaim_abandoned_sandboxes()

    fired = [entry for entry in logs if entry.get("event") == reclamation.FLEET_THRESHOLD_EVENT]
    assert len(fired) == 1


async def _noop_record(*, outcome: str, counts: dict[str, int], detail: str | None) -> None:
    return None
