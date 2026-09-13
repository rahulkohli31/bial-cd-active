"""The reclamation pass reports and destroys nothing.

THE ASSERTION THIS FILE EXISTS FOR is that no ARM delete is reachable from a pass. Everything else
here is observability, and observability has one job: make a DEAD WORKER distinguishable from a
quiet fleet. Every alarm the pass raises is emitted by the pass, so a crashlooping scheduler emits
nothing and looks exactly like a healthy idle system — which is the origin incident's failure
moved one layer out. The pass record is the only thing that breaks the tie, so it is written on
every outcome, including the boring ones and the failed ones.
"""

from __future__ import annotations

import datetime as dt
import re
import subprocess
import sys
import uuid
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import pytest
import redis.asyncio as aioredis
import sqlalchemy as sa
import structlog.testing
from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import settings
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
from src.services.sandbox.config import SandboxConfig
from tests.fakes import a_fleet_member
from tests.subprocess_env import child_env

USER = uuid.uuid4()
APP = uuid.uuid4()
#: An obviously-fake Azure subscription id — this repo is PUBLIC and has no secret scanning, and
#: the tests below print it into log assertions.
_FAKE_SUB = "00000000-0000-0000-0000-000000000000"


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
    """Two orphans, both candidates, zero ARM deletes. The destroy arm sits behind a second
    flag, off by default, and while that flag is off this is the whole safety posture of the
    feature."""
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


@pytest.mark.parametrize("busy_first", [True, False])
async def test_a_second_record_naming_the_same_container_cannot_unclaim_it(
    fake_redis: aioredis.Redis, busy_first: bool
) -> None:
    """TWO RECORDS, ONE NAME — the claim map is keyed by name, and a plain assignment let the
    scan's LAST writer win: an unrelated user's empty record erased a live builder's claim, and
    a container holding a lock, heartbeat AND liveness lease was staged for destruction — every
    other gate here fails toward sparing, this one failed toward destroying. BOTH ORDERS, because
    the defect is invisible in one of them: scan order must never decide whether a live build
    survives.

    MUTATION-CHECK: restore `claims[name] = await _claim_of(...)` and `busy_first=True` goes
    red while `busy_first=False` stays green — the shape that let this ship."""
    from src.services.redis import registry_key
    from src.services.redis.keys import REGISTRY_FIELD_APP_NAME

    bystander = uuid.uuid4()

    async def _seed_the_live_build() -> None:
        await fake_redis.hset(registry_key(USER), mapping={REGISTRY_FIELD_APP_NAME: "sbx-busy"})
        await fake_redis.set(f"bial:development:sandbox:lock:{USER}", "tok", ex=900)
        await fake_redis.set(f"bial:development:sandbox:heartbeat:{USER}", "now", ex=90)

    async def _seed_the_bystander() -> None:
        # Names the same container and holds nothing: no lock, no heartbeat, no stay, no lease.
        await fake_redis.hset(
            registry_key(bystander), mapping={REGISTRY_FIELD_APP_NAME: "sbx-busy"}
        )

    order = (
        (_seed_the_live_build, _seed_the_bystander)
        if busy_first
        else (_seed_the_bystander, _seed_the_live_build)
    )
    for seed in order:
        await seed()

    report = await pass_mod.run_reclamation_pass(control_plane=_Fleet([_orphan("sbx-busy")]))

    assert report.spared == 1
    assert report.candidates == ()


def test_the_cadence_constant_describes_the_cron_it_claims_to() -> None:
    """`PASS_CADENCE` READ FIVE MINUTES WHILE THE PASS RAN EVERY FIFTEEN.

    Five was the *sweep's* cadence (`SANDBOX_REAP_CRON`), a different worker — so
    `MINIMUM_STAGING_AGE`, derived from "the cadence", enforced a third of the full interval
    the two-independent-reads rule actually needs between the staging and destroying reads.
    Pinned here, not restated, because `reclaim.py` (a pure leaf) and the cron (the worker's)
    cannot import each other.

    Mutation-check: set `PASS_CADENCE` back to 5 minutes and this goes red."""
    from src.services.build_sessions.pass_history import _minutes_between_passes
    from src.services.build_sessions.reclaim import MINIMUM_STAGING_AGE, PASS_CADENCE
    from src.workers.reclamation import RECLAMATION_CRON

    assert PASS_CADENCE == dt.timedelta(minutes=_minutes_between_passes(RECLAMATION_CRON))
    assert MINIMUM_STAGING_AGE == PASS_CADENCE


@pytest.mark.parametrize("cron", ["0 * * * *", "*/0 * * * *", "0,30 * * * *", "", "* * * * *"])
def test_a_cron_no_staleness_window_follows_from_is_refused(cron: str) -> None:
    """A ZERO-MINUTE CADENCE IS THE DANGEROUS ONE, not the unparseable one.

    `int("0")` succeeded and produced a staleness window of zero, so every pass — including one
    that finished a second ago — read as stale. The single alarm capable of detecting a dead
    worker would have fired on every check and been tuned out, which is worse than not having it.
    The list and range forms merely raised a bare `ValueError` naming nothing."""
    from src.services.build_sessions.pass_history import (
        UnschedulableCadenceError,
        _minutes_between_passes,
    )

    with pytest.raises(UnschedulableCadenceError):
        _minutes_between_passes(cron)


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


# --- the destroy flag's precondition, made legible without a write ----------------


async def test_an_untagged_container_is_counted_without_a_write(
    fake_redis: aioredis.Redis,
) -> None:
    """The destroy flag's precondition, read off the same enumeration the pass already did.

    MUTATION-CHECK: hard-code `untagged=0` in `run_reclamation_pass` and this goes red."""
    fleet = _Fleet([a_fleet_member("sbx-ghost"), _orphan("sbx-a")])

    report = await pass_mod.run_reclamation_pass(control_plane=fleet)

    assert report.untagged == 1


async def test_a_fully_tagged_fleet_reports_zero_untagged(fake_redis: aioredis.Redis) -> None:
    report = await pass_mod.run_reclamation_pass(
        control_plane=_Fleet([_orphan("sbx-a"), _orphan("sbx-b")])
    )

    assert report.untagged == 0


async def test_a_store_fault_still_counts_what_the_fleet_listing_saw(
    fake_redis: aioredis.Redis,
) -> None:
    """`untagged` reads the ARM listing, not the coordination store — so a fleet too thin on
    registry claims to trust for verdicts still answers the one question that never depended on
    Redis, while the fault it cannot ignore is still raised."""
    fleet = _Fleet(
        [
            a_fleet_member("sbx-ghost-a"),
            a_fleet_member("sbx-ghost-b"),
            _orphan("sbx-a"),
            _orphan("sbx-b"),
        ]
    )

    report = await pass_mod.run_reclamation_pass(control_plane=fleet)

    assert report.store_fault is True
    assert report.untagged == 2


# --- the pass record: the only thing that can detect a dead worker ----------------


async def _passes(db: AsyncSession) -> list[WorkerPass]:
    rows = await db.execute(sa.select(WorkerPass))
    return list(rows.scalars())


async def test_a_zero_candidate_pass_still_writes_a_record(
    record_pass_writes_here: _RecordPassHarness,
) -> None:
    """THE LOAD-BEARING ONE. A healthy quiet fleet and a dead worker are the same observation
    unless the quiet pass leaves a trace. Skip this write and the staleness alarm fires on every
    idle night, which trains an operator to ignore it — and then it fires for real.

    IT USED TO MONKEYPATCH `_record_pass` AND THEN CALL IT, so the assertion ran against a copy
    of the insert written in this file. Deleting the production function entirely left it green,
    which is the exact opposite of load-bearing. It now calls the real one, against a factory
    pointed at this test's connection."""
    from src.workers.reclamation import _record_pass

    await _record_pass(outcome="ok", counts={"scanned": 0}, detail=None)

    rows = await _passes(record_pass_writes_here.db)
    assert len(rows) == 1
    assert rows[0].outcome is PassOutcome.OK
    assert rows[0].counts == {"scanned": 0}
    # AND IT WAS COMMITTED. Without this, autoflush makes a `_record_pass` that never commits
    # look identical in here to one that does — while in production the session closes, rolls
    # back, and the only detector of a dead worker silently writes nothing.
    assert all(getattr(s, "committed", False) for s in record_pass_writes_here.sessions)


@dataclass
class _RecordPassHarness:
    """What the fixture hands back: the connection the rows land on, and every session the
    production factory was asked for — so a test can assert the COMMIT as well as the row."""

    db: AsyncSession
    sessions: Sequence[object]


@pytest.fixture
def record_pass_writes_here(db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch):  # noqa: ANN201
    """Point the REAL `_record_pass` at this test's connection instead of a fresh factory one.

    THE TESTS USED TO CALL A COPY OF IT, proving only that the test file could write a row —
    not that the platform can tell a dead worker from a quiet fleet. Deleting `_record_pass`
    outright, or dropping its `await db.commit()`, left them green. The production function
    opens its OWN session on purpose (it must land even when the pass it describes just
    failed), so the factory is rebound to hand back THIS connection's session, with `commit`
    neutered to avoid leaking rows into later tests."""
    import src.db.base as db_base

    class _NoCommitSession:
        """The test's session, minus the one call that would escape the harness transaction.

        `committed` is not bookkeeping — it is the half of the contract this harness would
        otherwise erase. Autoflush means a bare `add()` is visible to the very next SELECT, so a
        `_record_pass` that forgot to commit reads as perfectly healthy in here while writing
        nothing at all in production, where the session closes and rolls back. So the flag is
        recorded and asserted, and `flush` stands in for the real write."""

        def __init__(self, inner: AsyncSession) -> None:
            self._inner = inner
            self.committed = False

        async def __aenter__(self) -> _NoCommitSession:
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

        def add(self, instance: object) -> None:
            self._inner.add(instance)

        async def commit(self) -> None:
            self.committed = True
            await self._inner.flush()

    sessions: list[_NoCommitSession] = []

    def _factory() -> _NoCommitSession:
        sessions.append(_NoCommitSession(db_session))
        return sessions[-1]

    monkeypatch.setattr(db_base, "async_session_factory", _factory)
    return _RecordPassHarness(db=db_session, sessions=sessions)


async def test_a_failed_pass_is_recorded_as_failed_not_lost(
    record_pass_writes_here: _RecordPassHarness,
) -> None:
    """A pass that raises every tick leaves no `ok` row — indistinguishable from a worker that
    never runs unless the failure itself is recorded."""
    from src.workers.reclamation import _record_pass

    await _record_pass(outcome="failed", counts={}, detail="the pass raised")

    (row,) = await _passes(record_pass_writes_here.db)
    assert row.outcome is PassOutcome.FAILED


async def test_a_declined_pass_is_its_own_outcome(
    record_pass_writes_here: _RecordPassHarness,
) -> None:
    """ "Reclamation is switched off" is a thing an operator should be able to SEE, not infer from
    silence — and it is not a failure."""
    from src.workers.reclamation import _record_pass

    await _record_pass(outcome="declined", counts={}, detail="flag_off")

    (row,) = await _passes(record_pass_writes_here.db)
    assert row.outcome is PassOutcome.DECLINED


async def test_a_record_that_cannot_be_written_is_logged_and_never_raised(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE SWALLOW IS DELIBERATE AND HAS TO STAY LOUD.

    A pass whose WORK succeeded must not be reported as failed because its bookkeeping was —
    re-raising would turn a database blip into a crashlooping worker. But the staleness alarm
    reads this table, so a silent swallow makes a healthy worker look dead, and the log line is
    the only thing telling an operator which of the two they are looking at.

    Mutation-check: delete the `_log.exception` and this goes red; delete the `except` and it
    raises instead."""
    import src.db.base as db_base
    from src.workers.reclamation import _record_pass

    def _no_database() -> object:
        raise RuntimeError("the database is unreachable")

    monkeypatch.setattr(db_base, "async_session_factory", _no_database)

    with structlog.testing.capture_logs() as logs:
        await _record_pass(outcome="ok", counts={"scanned": 3}, detail=None)

    assert any(entry.get("event") == "sandbox_reclamation_pass_record_failed" for entry in logs)


# --- staleness ---------------------------------------------------------------------


async def test_never_having_run_reads_as_stale(db_session: AsyncSession) -> None:
    """NOT "no news is good news". A null last-pass is a fresh deployment whose worker never
    started, or one that has never completed a pass — different causes, same consequence: nothing
    is watching the fleet."""
    last, stale = await reclamation_pass_freshness(db_session)

    assert last is None
    assert stale is True


async def test_a_recent_pass_is_fresh(record_pass_writes_here: _RecordPassHarness) -> None:
    from src.workers.reclamation import _record_pass

    await _record_pass(outcome="ok", counts={}, detail=None)

    last, stale = await reclamation_pass_freshness(record_pass_writes_here.db)

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


async def test_a_failing_worker_still_reads_as_alive(
    record_pass_writes_here: _RecordPassHarness,
) -> None:
    """Any outcome counts as a pass. "Is the worker running" and "is the worker happy" are
    different questions, and answering the first with the second would hide a worker that is
    there and broken behind one that is simply gone."""
    from src.workers.reclamation import _record_pass

    await _record_pass(outcome="failed", counts={}, detail="boom")

    _, stale = await reclamation_pass_freshness(record_pass_writes_here.db)

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
            owners={},
            untagged=0,
        )

    monkeypatch.setattr(pass_mod, "run_reclamation_pass", _report)

    with structlog.testing.capture_logs() as logs:
        await reclamation.reclaim_abandoned_sandboxes()

    fired = [entry for entry in logs if entry.get("event") == reclamation.FLEET_THRESHOLD_EVENT]
    assert len(fired) == 1


async def _noop_record(*, outcome: str, counts: dict[str, int], detail: str | None) -> None:
    return None


# --- WHICH FLEET? ------------------------------------------------------------------

#: The `backend/` tree — `tests/workers/` lives two levels under it.
_BACKEND_ROOT = Path(__file__).resolve().parent.parent.parent
_WORKER_SAMPLE = ".env.worker.example"

#: The only strings a `SANDBOX__*` value in the tracked sample may be. Deliberately a closed set
#: of exact literals rather than a "looks fake enough" heuristic: this repo is PUBLIC and has no
#: secret scanning, the worker's `SandboxConfig` is REQUIRED and carries an ACR password, and the
#: failure mode is somebody pasting a working credential in to make the file parse.
_PLACEHOLDERS = frozenset({"REPLACE_ME", "00000000-0000-0000-0000-000000000000", "true", "false"})

_SANDBOX_KEY = re.compile(r"^SANDBOX__[A-Z0-9_]+$")


def _a_fleet_configuration(*, reclaim_enabled: bool) -> SandboxConfig:
    """A structurally valid `SANDBOX__*` block naming an obviously-fake fleet."""
    return SandboxConfig(
        subscription_id=_FAKE_SUB,
        resource_group="rg-not-ours",
        region="REPLACE_ME",
        managed_environment_name="env-not-ours",
        image_ref="REPLACE_ME",
        acr_server="REPLACE_ME",
        acr_username="REPLACE_ME",
        acr_password=SecretStr("REPLACE_ME"),
        reclaim_enabled=reclaim_enabled,
    )


async def _a_quiet_report(**_: object) -> pass_mod.PassReport:
    return pass_mod.PassReport(
        scanned=0,
        spared=0,
        staged=0,
        destroy=0,
        escalate=0,
        not_ours=0,
        store_fault=False,
        candidates=(),
        owners={},
        untagged=0,
    )


async def test_a_running_pass_names_the_fleet_it_enumerated_exactly_once(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE GREPPABLE LINE. `sandbox_fleet_over_threshold` and the pass-completed event both
    describe a fleet without naming one, so neither can settle the question that actually
    matters: is this worker judging OUR containers? The resource group and the managed environment
    answer it, and the subscription id — which is kept out of `WorkerPass.detail` because an
    admin endpoint reads that column into a response — is precise enough to settle it alone.

    ONCE PER PASS, not once per container: a per-container line makes the fleet identity scale
    with the fleet, which is exactly when nobody reads it.

    MUTATION-CHECK: make `_log_the_fleet` a no-op (`return` on its first line) and this goes red.
    """
    from src.workers import reclamation

    monkeypatch.setattr(settings, "sandbox", _a_fleet_configuration(reclaim_enabled=True))
    monkeypatch.setattr(reclamation, "_record_pass", _noop_record)
    monkeypatch.setattr(pass_mod, "run_reclamation_pass", _a_quiet_report)

    with structlog.testing.capture_logs() as logs:
        await reclamation.reclaim_abandoned_sandboxes()

    named = [e for e in logs if e.get("event") == reclamation.FLEET_ENUMERATED_EVENT]
    assert len(named) == 1
    assert named[0]["resource_group"] == "rg-not-ours"
    assert named[0]["managed_environment"] == "env-not-ours"
    assert named[0]["subscription_id"] == _FAKE_SUB


async def test_a_pass_declined_by_the_flag_still_names_its_fleet(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE MISCONFIGURED WORKER, RECONSTRUCTED. It was wrong in both halves at once: the reclaim
    flag was never set on the worker's own env, AND the subscription was one retired two
    rotations earlier. A fleet line emitted only by a pass that RUNS would never have been
    emitted on that deployment — the one it exists for — which is why `_log_the_fleet` is called
    before the flag gate rather than after it.

    MUTATION-CHECK: move the `_log_the_fleet()` call below the `if off_duty is not None: return`
    block and this goes red while the running-pass test above stays green."""
    from src.workers import reclamation

    monkeypatch.setattr(settings, "sandbox", _a_fleet_configuration(reclaim_enabled=False))
    monkeypatch.setattr(reclamation, "_record_pass", _noop_record)

    with structlog.testing.capture_logs() as logs:
        await reclamation.reclaim_abandoned_sandboxes()

    assert any(e.get("reason") == "flag_off" for e in logs), "the pass must still have declined"
    named = [e for e in logs if e.get("event") == reclamation.FLEET_ENUMERATED_EVENT]
    assert len(named) == 1
    assert named[0]["resource_group"] == "rg-not-ours"


# --- the tracked sample -------------------------------------------------------------


def test_the_worker_sample_boots_a_valid_worker_profile() -> None:
    """THE WORKER ROLE, WHICH HAD NO TEMPLATE. A fresh checkout could produce a working API from
    `.env.example`; there was nothing at all for the worker, and `WorkerSettings` makes object
    storage, Redis and ARM access REQUIRED in every environment — so a hand-assembled file fails
    at construction, and the cheapest way out of that is to start trimming safety.

    A SUBPROCESS WITH A SCRUBBED ENVIRONMENT, not an in-process construct: pydantic-settings
    merges the ambient environment on top of the file, so a developer's own `.env.worker` would
    quietly supply anything this sample forgot — the exact drift the test exists to catch."""
    done = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-B",
            "-c",
            "from src.config import resolve_settings;"
            " s = resolve_settings();"
            " print(type(s).__name__, s.sandbox.reclaim_enabled)",
        ],
        cwd=_BACKEND_ROOT,
        env=child_env(ENV_FILE=_WORKER_SAMPLE, BIAL_ROLE="worker"),
        capture_output=True,
        text=True,
        check=False,
    )

    assert done.returncode == 0, f"{_WORKER_SAMPLE} does not boot a worker:\n{done.stderr}"
    # The profile AND the flag: `BIAL_ROLE` is read from the real environment, never from the
    # file, so a sample that booted an ApiSettings would prove nothing about the worker.
    assert done.stdout.split() == ["WorkerSettings", "False"], done.stdout


def test_no_sandbox_value_in_the_worker_sample_looks_real() -> None:
    """THE ONE THAT STOPS A CREDENTIAL REACHING A PUBLIC REPO.

    `SandboxConfig` is REQUIRED for this role and carries `acr_password`, `acr_username` and a
    subscription id, and `.env.example` never had to demonstrate safe placeholders for that block
    because the API's sandbox is optional. So there is nothing here to copy the convention from,
    and the natural move when a sample will not parse is to paste a value that works. This
    repository is public and has nothing scanning it.

    COMMENTED LINES COUNT TOO: a commented-out real credential is a committed credential."""
    lines = (_BACKEND_ROOT / _WORKER_SAMPLE).read_text(encoding="utf-8").splitlines()
    values = {
        key: value
        for key, _, value in (line.lstrip("# ").partition("=") for line in lines)
        if _SANDBOX_KEY.match(key)
    }

    assert values, f"{_WORKER_SAMPLE} documents no SANDBOX__* key at all"
    assert "SANDBOX__RECLAIM_ENABLED" in values, (
        "the flag whose absence from the real file produced `flag_off` must be IN the template"
    )
    not_placeholders = {k: v for k, v in values.items() if v not in _PLACEHOLDERS}
    assert not not_placeholders, (
        f"{_WORKER_SAMPLE} carries SANDBOX__ values that are not placeholders — this repo is "
        f"public and unscanned, so replace each with one of {sorted(_PLACEHOLDERS)}: "
        f"{sorted(not_placeholders)}"
    )


def test_the_worker_sample_is_not_excluded_by_gitignore() -> None:
    """A TEMPLATE NOBODY CAN COMMIT IS THE ABSENCE IT WAS WRITTEN TO FIX. `backend/.gitignore` is
    `.env.*` — which matches this filename — rescued only by the `!.env*.example` negation on the
    next line. Reorder those two, or narrow the negation to `!.env.example`, and the file silently
    stops being trackable while every other test here stays green.

    ASKED OF GIT ITSELF rather than by re-implementing pattern precedence: last-match-wins across
    nested `.gitignore` files is not a rule worth reproducing in a test. `--no-index` keeps the
    answer the same before and after the file is committed."""
    probe = subprocess.run(  # noqa: S603
        ["git", "check-ignore", "--no-index", "-v", "--", f"backend/{_WORKER_SAMPLE}"],
        cwd=_BACKEND_ROOT.parent,
        capture_output=True,
        text=True,
        check=False,
    )

    if probe.returncode == 1:  # no pattern matched at all — trivially trackable
        return
    assert probe.returncode == 0, f"git could not answer: {probe.stderr}"
    # `<source>:<line>:<pattern>\t<path>`. A leading `!` is git saying "explicitly NOT ignored".
    pattern = probe.stdout.split("\t", 1)[0].split(":", 2)[2]
    assert pattern.startswith("!"), (
        f"backend/{_WORKER_SAMPLE} is ignored by {pattern!r} — the template cannot be committed"
    )
