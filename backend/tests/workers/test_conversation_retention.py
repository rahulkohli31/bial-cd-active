"""The scheduled half: when the pass runs, when it stands down, and what it leaves on record.

`tests/services/conversations/test_retention.py` owns what a pass condemns. What is asserted here
is that the pass is REACHABLE — that its own declines cannot seal it shut, that a second worker
cannot run it concurrently, and that an operator reading the pass table can tell a disabled worker
from a dead one — and, in one test, that the whole select-delete-commit-sweep-record pipeline runs
with nothing stubbed between its ends, since every other test here replaces it.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
import uuid
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa

from src.db import base as db_base
from src.db.models.attachment import Attachment
from src.db.models.conversation import ChatKind, Conversation
from src.db.models.worker_pass import PassOutcome, WorkerPass
from src.workers import conversation_retention as retention
from src.workers.conversation_retention import (
    RETENTION_CRON,
    RETENTION_INTERVAL,
    RETENTION_SCHEDULE_ID,
    RETENTION_TASK_NAME,
)
from tests.factories import ConversationFactory, UserFactory

_BACKEND_ROOT = Path(__file__).resolve().parent.parent.parent


class _NoCommitSession:
    """The test's own session, handed to code that opens one of its own.

    `refuses_bookkeeping` fails the pass-record write while leaving the deletion's own session
    working, which is the only way in to that branch from outside the module.
    """

    def __init__(self, inner, *, refuses_bookkeeping: bool = False) -> None:
        self._inner = inner
        self._refuses_bookkeeping = refuses_bookkeeping

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None

    def __getattr__(self, name: str):
        return getattr(self._inner, name)

    def add(self, instance: Any, *args: Any, **kwargs: Any) -> None:
        if self._refuses_bookkeeping and isinstance(instance, WorkerPass):
            raise RuntimeError("the pass record would not write")
        self._inner.add(instance, *args, **kwargs)

    async def commit(self) -> None:
        await self._inner.flush()


@pytest.fixture
def records_land_here(db_session, monkeypatch: pytest.MonkeyPatch):
    """Point `_record_pass` and the marker read at the test's own session.

    The pass writes its record on a session of its own — it must land even when the pass it
    describes has just failed — so a test that wants to read one back has to bind that factory.
    """
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


async def _an_idle_chat_holding(db, *blob_keys: str) -> uuid.UUID:
    """A conversation nobody has touched for a month, holding one uploaded file per key.

    The backdate follows the inserts, and has to: the row is created carrying `now()`, so a
    column written before that is simply overwritten.
    """
    user = await UserFactory.create(db)
    conversation = await ConversationFactory.create(db, user.id, kind=ChatKind.GENERIC)
    for key in blob_keys:
        db.add(
            Attachment(
                user_id=user.id,
                conversation_id=conversation.id,
                attachment_id=f"att-{uuid.uuid4().hex[:12]}",
                name="roster.pdf",
                media_type="application/pdf",
                size=512,
                storage_key=key,
            )
        )
    await db.execute(
        sa.update(Conversation)
        .where(Conversation.id == conversation.id)
        .values(updated_at=dt.datetime.now(dt.UTC) - dt.timedelta(days=30))
    )
    await db.flush()
    return conversation.id


def _switch(monkeypatch: pytest.MonkeyPatch, *, on: bool, destroy: bool = True) -> None:
    """Answer the worker profile's retention fields without building a worker profile."""
    monkeypatch.setattr(
        retention,
        "_worker_settings",
        lambda: _Profile(enabled=on, destroy=destroy, days=7, per_pass=500),
    )


class _Profile:
    """Stands in for the retention fields of a worker profile."""

    def __init__(self, *, enabled: bool, destroy: bool, days: int, per_pass: int) -> None:
        self.conversation_retention_enabled = enabled
        self.conversation_retention_destroy = destroy
        self.conversation_retention_days = days
        self.conversation_retention_per_pass = per_pass


@contextlib.asynccontextmanager
async def _lock(taken: bool) -> AsyncIterator[bool]:
    yield taken


def _the_store_refuses(monkeypatch: pytest.MonkeyPatch, store, key: str) -> None:
    """Fail one key's delete, leaving the rest of the sweep to succeed."""
    deletes = store.delete

    async def _delete(refused: str) -> None:
        if refused == key:
            raise RuntimeError("the store would not delete it")
        await deletes(refused)

    monkeypatch.setattr(store, "delete", _delete)


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


# --- the pipeline itself --------------------------------------------------------------------


async def test_a_pass_run_end_to_end_records_the_counts_and_the_blob_it_could_not_delete(
    records_land_here, fake_storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ Every other test here replaces the pass, so this is the one that runs it: select,
    delete, commit, sweep, record. A store that drops one key is the case the record exists for —
    the rows are already gone, so the pass owes an `ok` row naming what is still out there rather
    than a failure that would condemn the same batch again.

    Mutation receipt: let the blob sweep's failure escape `_sweep_and_record` and this goes red —
    the pass raises and records `failed` for work that succeeded."""
    _switch(monkeypatch, on=True)
    monkeypatch.setattr(
        "src.services.build_sessions.destroy.single_flight_lock", lambda _key: _lock(True)
    )
    doomed = await _an_idle_chat_holding(records_land_here, "att/goes", "att/stays")
    await fake_storage.put("att/goes", b"one")
    await fake_storage.put("att/stays", b"two")
    _the_store_refuses(monkeypatch, fake_storage, "att/stays")

    await retention.sweep_idle_conversations()

    records_land_here.expunge_all()
    assert await records_land_here.get(Conversation, doomed) is None
    assert fake_storage.objects == {"att/stays": b"two"}
    rows = await _passes(records_land_here)
    assert [row.outcome for row in rows] == [PassOutcome.OK]
    assert rows[0].counts == {
        "condemned": 1,
        "removed": 1,
        "blobs": 2,
        "blobs_failed": 1,
        "outstanding": 0,
    }
    assert "att/stays" in (rows[0].detail or "")


async def test_a_pass_cancelled_after_its_commit_still_leaves_its_record(
    records_land_here, fake_storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ The commit is the point of no return, and the record is the only thing holding the next
    tick back — so a cancellation arriving after it must still leave a row. Without one the run
    reads as one that never happened and the batch that is already gone is condemned again.

    Mutation receipt: drop the `asyncio.shield` around the tail and this goes red — the record is
    never written."""
    _switch(monkeypatch, on=True)
    monkeypatch.setattr(
        "src.services.build_sessions.destroy.single_flight_lock", lambda _key: _lock(True)
    )
    doomed = await _an_idle_chat_holding(records_land_here, "att/one")
    in_the_tail = asyncio.Event()
    carry_on = asyncio.Event()
    real_tail = retention._sweep_and_record

    async def _waits_to_be_cancelled(blob_keys: Any, **counts: int) -> None:
        in_the_tail.set()
        await carry_on.wait()
        await real_tail(blob_keys, **counts)

    monkeypatch.setattr(retention, "_sweep_and_record", _waits_to_be_cancelled)

    running = asyncio.ensure_future(retention.sweep_idle_conversations())
    await in_the_tail.wait()
    running.cancel()
    carry_on.set()
    with pytest.raises(asyncio.CancelledError):
        await running

    records_land_here.expunge_all()
    assert await records_land_here.get(Conversation, doomed) is None
    rows = await _passes(records_land_here)
    assert [row.outcome for row in rows] == [PassOutcome.OK]


async def test_a_pass_whose_record_cannot_be_written_keeps_its_deletion_and_does_not_raise(
    records_land_here, fake_storage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ The rows are committed gone before the record is written, so a pass whose bookkeeping
    fails did its work and must not report otherwise. The cost is real and deliberate: with no
    row the gate sees no successful run, which is why the branch logs rather than passes quietly.

    Mutation receipt: let `_record_pass` propagate and this goes red — the pass raises, and the
    caller above it writes a `failed` row for a deletion that happened."""
    _switch(monkeypatch, on=True)
    monkeypatch.setattr(
        "src.services.build_sessions.destroy.single_flight_lock", lambda _key: _lock(True)
    )
    doomed = await _an_idle_chat_holding(records_land_here, "att/one")
    monkeypatch.setattr(
        db_base,
        "async_session_factory",
        lambda: _NoCommitSession(records_land_here, refuses_bookkeeping=True),
    )

    await retention.sweep_idle_conversations()

    records_land_here.expunge_all()
    assert await records_land_here.get(Conversation, doomed) is None
    assert await _passes(records_land_here) == []


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
    """The enabling flag is REQUIRED, so a deployment that does not carry it refuses to boot —
    and the sample is where an operator finds out before that happens. The destroy switch
    defaults off instead, which makes the sample the only place an operator meets it at all."""
    sample = (_BACKEND_ROOT / ".env.worker.example").read_text(encoding="utf-8")
    assert "CONVERSATION_RETENTION_ENABLED=false" in sample
    assert "CONVERSATION_RETENTION_DESTROY=false" in sample
    assert "CONVERSATION_RETENTION_DAYS=" in sample
    assert "CONVERSATION_RETENTION_PER_PASS=" in sample
