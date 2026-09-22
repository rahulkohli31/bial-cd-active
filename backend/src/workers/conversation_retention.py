"""The weekly conversation-retention pass — removes nothing until its flag is switched on.

IT GATES ON ITS OWN RECORDED HISTORY, NOT ON THE SCHEDULER. The scheduler keeps its last-run state
in memory and deliberately skips its first tick after start, so a weekly cron on a platform that
redeploys more often than weekly may never fire at all. The pass record table is already a durable
last-run marker whose task name is free text, so the cron ticks often and the week is enforced by a
read.

THE MARKER READ FILTERS ON SUCCESS, AND THAT IS THE WHOLE GATE RATHER THAN A DETAIL. Declines and
failures are recorded under this same task name — and the flag-off arm declines on every tick
before the feature is ever switched on — so a gate reading the newest row of ANY outcome seals
itself shut: the first declining tick becomes the newest run, the window test can never pass again,
and the pass deletes nothing forever while writing healthy-looking rows.
"""

from __future__ import annotations

import asyncio
import contextlib
import datetime as dt
from typing import Final

import structlog

from src.broker import broker
from src.config import resolve_settings
from src.settings.worker import WorkerSettings

_log = structlog.get_logger()

#: The task's own name, and the `task_name` its pass records carry.
RETENTION_TASK_NAME: Final = "conversation_retention"
RETENTION_SCHEDULE_ID: Final = "conversation-retention-daily"

#: DAILY, NOT WEEKLY, and the window is enforced by the marker read rather than by this line. A
#: weekly cron would have one chance a week to coincide with a running worker; a daily tick that
#: declines six times out of seven costs one SELECT each time and cannot be missed by a deploy.
RETENTION_CRON: Final = "17 3 * * *"

#: How long a SUCCESSFUL pass suppresses the next one. It equals the retention window at the
#: shipped defaults and answers a different question — the window decides what is condemned, this
#: decides how often the question is asked — so an operator may widen either one alone.
RETENTION_INTERVAL: Final = dt.timedelta(days=7)

PASS_COMPLETED_EVENT: Final = "conversation_retention_pass_completed"

#: `WorkerPass.detail` is `String(512)`.
_DETAIL_LIMIT: Final = 512

#: The advisory-lock key. A constant, because the lock protects "a retention pass", not a row.
_LOCK_KEY: Final = 0x43_4F_4E_56_01  # "CONV" + 01, an arbitrary but stable 64-bit constant


def _worker_settings() -> WorkerSettings:
    """This process's worker profile, for the fields only a worker declares.

    `src.config.settings` is typed `ApiSettings` even inside a worker — a deliberate trade the
    config module documents — so a worker-only field is read through the real profile instead.
    Resolved PER CALL, never at module scope: the profile builds on first access, and building it
    at import time is what breaks the worker.
    """
    profile = resolve_settings()
    if not isinstance(profile, WorkerSettings):
        raise RuntimeError("the conversation-retention pass runs only in the worker role")
    return profile


def _off_duty_because() -> str | None:
    """Why this tick will not run, or `None` if it may."""
    if not _worker_settings().conversation_retention_enabled:
        return "flag_off"
    return None


@broker.task(
    task_name=RETENTION_TASK_NAME,
    schedule=[{"cron": RETENTION_CRON, "schedule_id": RETENTION_SCHEDULE_ID}],
)
async def sweep_idle_conversations() -> None:
    """One retention pass. THE FLAG GATE COMES FIRST, before any heavy import — the same contract
    every other scheduled task here sets, so a disabled task costs only structlog, the broker and
    the settings profile.

    SETTINGS ARE READ INSIDE THE FUNCTION, never at module scope: the settings object resolves its
    role lazily and an eager read at import breaks the worker.

    NOTHING IS SWALLOWED: a raise is recorded as a failed pass and re-raised, and a cancellation
    records itself before it propagates, so neither is mistaken for a pass that never ran.
    """
    off_duty = _off_duty_because()
    if off_duty is not None:
        _log.info("conversation_retention_pass_disabled", reason=off_duty)
        await _record_pass(outcome="declined", counts={}, detail=off_duty)
        return

    due_at = await _due_since()
    if due_at is not None:
        _log.info("conversation_retention_pass_not_due", last_success=due_at.isoformat())
        await _record_pass(outcome="declined", counts={}, detail="not due")
        return

    from src.services.build_sessions.destroy import single_flight_lock

    async with single_flight_lock(_LOCK_KEY) as took_the_lock:
        if not took_the_lock:
            # A second worker is mid-pass. Deleting concurrently with it buys nothing and risks
            # two transactions racing the same rows, so this tick logs and stands down.
            _log.info("conversation_retention_pass_locked_out")
            await _record_pass(outcome="declined", counts={}, detail="another pass holds the lock")
            return
        try:
            await _run_one_pass()
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("conversation_retention_pass_failed")
            await _record_pass(
                outcome="failed", counts={}, detail="the pass raised; see the traceback"
            )
            raise


async def _due_since() -> dt.datetime | None:
    """`None` when the pass is due; otherwise the successful run that is holding it back.

    ONLY `ok` ROWS COUNT. See this module's docstring: a gate that read the newest row of any
    outcome would be pushed forward by its own declines and never open again.
    """
    import sqlalchemy as sa

    from src.db.base import async_session_factory
    from src.db.models.worker_pass import PassOutcome, WorkerPass

    async with async_session_factory() as db:
        newest = await db.scalar(
            sa.select(sa.func.max(WorkerPass.finished_at)).where(
                WorkerPass.task_name == RETENTION_TASK_NAME,
                WorkerPass.outcome == PassOutcome.OK,
            )
        )
    if newest is None:
        return None
    return None if dt.datetime.now(dt.UTC) - newest >= RETENTION_INTERVAL else newest


async def _run_one_pass() -> None:
    """Select, delete, commit, sweep — in that order, which is the order that cannot strand a
    blob whose row survived.

    THE COMMIT IS THE POINT OF NO RETURN, so both sides of it hold against cancellation: before
    it the transaction rolls back and the pass records that it was interrupted; after it the
    sweep and the record run shielded, because that record is the only thing holding the next
    tick back and a batch deleted without one is condemned a second time."""
    from src.db.base import async_session_factory
    from src.services.conversations.retention import (
        condemned_conversations,
        gather_and_delete,
        idle_before,
    )

    profile = _worker_settings()
    cutoff = idle_before(
        dt.datetime.now(dt.UTC), window=dt.timedelta(days=profile.conversation_retention_days)
    )
    limit = profile.conversation_retention_per_pass

    try:
        async with async_session_factory() as db:
            candidates, outstanding = await condemned_conversations(db, cutoff=cutoff, limit=limit)
            sweep = await gather_and_delete(db, conversation_ids=candidates, cutoff=cutoff)
            await db.commit()
    except asyncio.CancelledError:
        _log.warning("conversation_retention_pass_cancelled")
        await _record_pass(
            outcome="failed", counts={}, detail="cancelled before the delete committed"
        )
        raise

    tail = asyncio.ensure_future(
        _sweep_and_record(
            sweep.blob_keys,
            condemned=len(candidates),
            removed=sweep.removed,
            outstanding=outstanding,
        )
    )
    try:
        await asyncio.shield(tail)
    except asyncio.CancelledError:
        # The shield leaves the tail running; this waits it out instead of handing an unwritten
        # record to a task the loop is about to tear down.
        with contextlib.suppress(asyncio.CancelledError):
            await asyncio.wait({tail}, timeout=_TAIL_GRACE_S)
        if not tail.done():
            _log.error("conversation_retention_pass_record_owed", removed=sweep.removed)
        raise


#: How long a cancelled pass waits for its shielded tail. Well inside the worker's own shutdown
#: grace, so the record lands before the container goes.
_TAIL_GRACE_S: Final = 10.0


async def _sweep_and_record(
    blob_keys: tuple[str, ...], *, condemned: int, removed: int, outstanding: int
) -> None:
    """Everything the pass owes after its commit: drop the blobs, then write the pass record.

    IT DOES NOT RAISE. The rows are already gone, so a store that is unreachable must still leave
    an `ok` row naming what it could not delete — without one the gate sees no successful run and
    the next tick condemns a second batch."""
    from src.services.storage import get_storage, sweep_blobs

    try:
        survived = await sweep_blobs(get_storage(), list(blob_keys), concurrency=_SWEEP_WIDTH)
    except Exception:
        _log.exception("conversation_retention_blob_sweep_failed")
        survived = list(blob_keys)

    counts = {
        "condemned": condemned,
        "removed": removed,
        "blobs": len(blob_keys),
        "blobs_failed": len(survived),
        "outstanding": outstanding,
    }
    _log.info(PASS_COMPLETED_EVENT, **counts)
    await _record_pass(outcome="ok", counts=counts, detail=_stranded_blobs_detail(survived))


#: Wider than the interactive delete's, which is tuned for one citizen pressing one button. This
#: sweep is bulk and post-commit: raising it only shortens the pass, never changes its outcome.
_SWEEP_WIDTH: Final = 24


def _stranded_blobs_detail(survived: list[str]) -> str | None:
    """What the record says about blobs the store would not delete.

    NOTHING RETAKES THESE. No code reads `detail` back, so the line is a report for a human, not a
    queue — and the count leads it because `_DETAIL_LIMIT` truncates the key list, and a truncated
    list that hid its own length would read as a smaller leak than it is."""
    if not survived:
        return None
    return f"{len(survived)} blobs left in the store, never retried: {', '.join(survived)}"


async def _record_pass(*, outcome: str, counts: dict[str, int], detail: str | None) -> None:
    """Write the pass record. EVERY outcome, including the boring ones — and the gate above reads
    only the successful ones, so a decline is observable without being load-bearing.

    ITS OWN SESSION, not the pass's: it runs outside any request and must land even when the pass
    it describes has just failed."""
    from src.db.base import async_session_factory
    from src.db.models.worker_pass import PassOutcome, WorkerPass

    try:
        async with async_session_factory() as db:
            db.add(
                WorkerPass(
                    task_name=RETENTION_TASK_NAME,
                    outcome=PassOutcome(outcome),
                    finished_at=dt.datetime.now(dt.UTC),
                    counts=counts,
                    detail=detail[:_DETAIL_LIMIT] if detail else None,
                )
            )
            await db.commit()
    except Exception:
        # A pass whose WORK succeeded must not be reported as failed because its bookkeeping did.
        # Loud, though: the gate above reads this table, so a silent failure here would make the
        # pass due again immediately.
        _log.exception("conversation_retention_pass_record_failed", outcome=outcome)
