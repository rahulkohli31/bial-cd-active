"""The scheduled half: when the pass runs, when it stands down, and what it leaves on record.

THE GATE IS THE SUBJECT OF THIS FILE, not the deletion — `tests/services/conversations/
test_retention.py` owns what a pass condemns. What is asserted here is that the pass is REACHABLE:
that its own declines cannot seal it shut, that a second worker cannot run it concurrently, and
that an operator reading the pass table can tell a disabled worker from a dead one.
"""

from __future__ import annotations

import contextlib
import datetime as dt
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import sqlalchemy as sa

from src.db.models.worker_pass import PassOutcome, WorkerPass
from src.workers import conversation_retention as retention
from src.workers.conversation_retention import (
    RETENTION_CRON,
    RETENTION_INTERVAL,
    RETENTION_SCHEDULE_ID,
    RETENTION_TASK_NAME,
)

_BACKEND_ROOT = Path(__file__).resolve().parent.parent.parent


@pytest.fixture
def records_land_here(db_session, monkeypatch: pytest.MonkeyPatch):
    """Point `_record_pass` and the marker read at the test's own session.

    The pass writes its record on a session of its own — it must land even when the pass it
    describes has just failed — so a test that wants to read one back has to bind that factory.
    """
    from src.db import base as db_base

    class _NoCommitSession:
        def __init__(self, inner) -> None:
            self._inner = inner

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

        def __getattr__(self, name: str):
            return getattr(self._inner, name)

        async def commit(self) -> None:
            await self._inner.flush()

    monkeypatch.setattr(db_base, "async_session_factory", lambda: _NoCommitSession(db_session))
    return db_session


async def _passes(db) -> list[WorkerPass]:
    return list(
        (
            await db.scalars(
                sa.select(WorkerPass)
                .where(WorkerPass.task_name == RETENTION_TASK_NAME)
                .order_by(WorkerPass.finished_at.asc())
            )
        ).all()
    )


def _switch(monkeypatch: pytest.MonkeyPatch, *, on: bool) -> None:
    """Answer the worker profile's flag without building a worker profile."""
    monkeypatch.setattr(
        retention, "_worker_settings", lambda: _Profile(enabled=on, days=7, per_pass=500)
    )


class _Profile:
    def __init__(self, *, enabled: bool, days: int, per_pass: int) -> None:
        self.conversation_retention_enabled = enabled
        self.conversation_retention_days = days
        self.conversation_retention_per_pass = per_pass


@contextlib.asynccontextmanager
async def _lock(taken: bool) -> AsyncIterator[bool]:
    yield taken


# --- the flag -----------------------------------------------------------------------------


async def test_with_the_flag_off_the_pass_declines_and_says_so(
    records_land_here, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The flag ships OFF, so this is what every tick does until somebody deliberately enables
    it — and it is RECORDED, because "off" read as silence is indistinguishable from a worker
    that has stopped."""
    _switch(monkeypatch, on=False)

    await retention.sweep_idle_conversations()

    rows = await _passes(records_land_here)
    assert [row.outcome for row in rows] == [PassOutcome.DECLINED]
    assert rows[0].detail == "flag_off"


async def test_with_the_flag_off_nothing_is_deleted(
    records_land_here,  # noqa: ARG001 — the pass records on every arm, including this one
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Mutation receipt: remove the flag gate and this goes red — the pass would reach its own
    deletion on a deployment that never asked for one."""
    _switch(monkeypatch, on=False)
    ran = False

    async def _should_not_run() -> None:
        nonlocal ran
        ran = True

    monkeypatch.setattr(retention, "_run_one_pass", _should_not_run)

    await retention.sweep_idle_conversations()

    assert ran is False


# --- the marker gate ----------------------------------------------------------------------


async def test_with_no_recorded_run_at_all_the_pass_is_due(
    records_land_here, monkeypatch: pytest.MonkeyPatch
) -> None:
    _switch(monkeypatch, on=True)
    monkeypatch.setattr(
        "src.services.build_sessions.destroy.single_flight_lock", lambda _key: _lock(True)
    )
    ran = False

    async def _pass() -> None:
        nonlocal ran
        ran = True

    monkeypatch.setattr(retention, "_run_one_pass", _pass)

    await retention.sweep_idle_conversations()

    # The pass itself writes the `ok` record, and it is stubbed here — so what this asserts is
    # that the gate OPENED: nothing declined, and the work was reached.
    assert ran is True
    assert await _passes(records_land_here) == []


async def test_with_a_recent_successful_run_the_pass_declines(
    records_land_here, monkeypatch: pytest.MonkeyPatch
) -> None:
    _switch(monkeypatch, on=True)
    records_land_here.add(
        WorkerPass(
            task_name=RETENTION_TASK_NAME,
            outcome=PassOutcome.OK,
            finished_at=dt.datetime.now(dt.UTC) - dt.timedelta(days=1),
            counts={},
        )
    )
    await records_land_here.flush()

    ran = False

    async def _pass() -> None:
        nonlocal ran
        ran = True

    monkeypatch.setattr(retention, "_run_one_pass", _pass)

    await retention.sweep_idle_conversations()

    assert ran is False
    assert [row.outcome for row in await _passes(records_land_here)][-1] is PassOutcome.DECLINED


async def test_two_declining_ticks_do_not_push_the_marker_forward(
    records_land_here, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ THE GATE THAT SEALS ITSELF SHUT, and the reason the marker read filters on success.

    Declines and failures are recorded under the same task name. A gate reading the newest row of
    ANY outcome would be pushed forward by its own flag-off tick — and the flag-off arm runs on
    every tick before the feature is ever enabled — so the window test could never pass again and
    the pass would delete nothing, forever, while writing healthy-looking rows.

    Mutation receipt: drop the `outcome == OK` filter from `_due_since` and this goes red."""
    _switch(monkeypatch, on=False)
    await retention.sweep_idle_conversations()  # tick one: flag off

    _switch(monkeypatch, on=True)
    monkeypatch.setattr(
        "src.services.build_sessions.destroy.single_flight_lock", lambda _key: _lock(False)
    )
    await retention.sweep_idle_conversations()  # tick two: the lock is held elsewhere

    ran = False

    async def _pass() -> None:
        nonlocal ran
        ran = True

    monkeypatch.setattr(retention, "_run_one_pass", _pass)
    monkeypatch.setattr(
        "src.services.build_sessions.destroy.single_flight_lock", lambda _key: _lock(True)
    )
    await retention.sweep_idle_conversations()  # tick three: due, and free

    assert ran is True, "two declines sealed the gate shut"
    # The two declines are on record — which is the point: they are visible AND they are not
    # what the gate reads. The third tick's own `ok` row is written by `_run_one_pass`, stubbed
    # here, so the gate opening is what the assertion above measures.
    outcomes = [row.outcome for row in await _passes(records_land_here)]
    assert outcomes == [PassOutcome.DECLINED, PassOutcome.DECLINED]


# --- single flight ------------------------------------------------------------------------


async def test_a_second_concurrent_pass_takes_no_lock_deletes_nothing_and_records_a_decline(
    records_land_here, monkeypatch: pytest.MonkeyPatch
) -> None:
    _switch(monkeypatch, on=True)
    monkeypatch.setattr(
        "src.services.build_sessions.destroy.single_flight_lock", lambda _key: _lock(False)
    )
    ran = False

    async def _pass() -> None:
        nonlocal ran
        ran = True

    monkeypatch.setattr(retention, "_run_one_pass", _pass)

    await retention.sweep_idle_conversations()

    assert ran is False
    rows = await _passes(records_land_here)
    assert rows[-1].outcome is PassOutcome.DECLINED
    assert "lock" in (rows[-1].detail or "")


async def test_a_failure_mid_pass_is_recorded_before_it_propagates(
    records_land_here, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A pass that raises every tick leaves no `ok` row — indistinguishable from a worker that
    never runs unless the failure itself is recorded."""
    _switch(monkeypatch, on=True)
    monkeypatch.setattr(
        "src.services.build_sessions.destroy.single_flight_lock", lambda _key: _lock(True)
    )

    async def _boom() -> None:
        raise RuntimeError("the pass fell over")

    monkeypatch.setattr(retention, "_run_one_pass", _boom)

    with pytest.raises(RuntimeError, match="fell over"):
        await retention.sweep_idle_conversations()

    rows = await _passes(records_land_here)
    assert rows[-1].outcome is PassOutcome.FAILED


# --- the wiring -----------------------------------------------------------------------------


def test_the_worker_entrypoint_imports_this_task_module() -> None:
    """A task module that is never imported is a schedule nothing registers and a queue nothing
    consumes — a worker that looks healthy and does none of this."""
    from src.worker_main import _TASK_MODULES

    assert "src.workers.conversation_retention" in _TASK_MODULES


def test_the_scheduler_registers_exactly_one_schedule_for_this_task() -> None:
    schedules = retention.sweep_idle_conversations.labels.get("schedule") or []
    assert [entry["schedule_id"] for entry in schedules] == [RETENTION_SCHEDULE_ID]
    assert schedules[0]["cron"] == RETENTION_CRON


def test_the_cron_ticks_more_often_than_the_window_it_enforces() -> None:
    """★ The cron is not the gate, and it must not be mistaken for one. The scheduler keeps its
    last-run state in memory and skips its first tick after start, so a weekly cron on a platform
    that redeploys more often than weekly may never fire. A daily tick that declines six days out
    of seven costs one SELECT each time and cannot be missed by a deploy."""
    assert RETENTION_CRON.split()[2:] == ["*", "*", "*"], "the cron must fire every day"
    assert RETENTION_INTERVAL >= dt.timedelta(days=7)


def test_the_example_environment_file_carries_the_new_settings() -> None:
    """The flag is REQUIRED, so a deployment that does not carry it refuses to boot — and the
    sample is where an operator finds out before that happens."""
    sample = (_BACKEND_ROOT / ".env.worker.example").read_text(encoding="utf-8")
    assert "CONVERSATION_RETENTION_ENABLED=false" in sample
    assert "CONVERSATION_RETENTION_DAYS=" in sample
    assert "CONVERSATION_RETENTION_PER_PASS=" in sample
