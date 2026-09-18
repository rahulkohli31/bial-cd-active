"""Is the scheduled worker alive, and did the reap it performed secure anything?

THE ONLY HONEST DETECTOR OF A DEAD WORKER IS SILENCE: every alarm the reclamation pass raises
is emitted *by the pass*, so a crashlooping worker emits none and reads like a healthy quiet
fleet. The pass writes a record on every outcome; this module reads the ABSENCE of one as the
alarm.

WHY A WRITER LIVES HERE TOO: the reaper's write-back shares the failure mode — a container
whose tree cannot be written back is SPARED, indistinguishable from an empty fleet, and bills
forever silently. So this writes a `worker_passes` row on every outcome too, for the same reason:
it's the only thing an operator can look for that does not depend on the failing component to
speak up."""

from __future__ import annotations

import datetime as dt
import enum
from typing import Final

import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models.worker_pass import PassOutcome, WorkerPass
from src.workers.reclamation import RECLAMATION_CRON, RECLAMATION_TASK_NAME

_log = structlog.get_logger()

#: How many scheduled intervals may pass before silence counts as a fault. Three, matching the
#: head-room every other liveness signal in this system uses (`HEARTBEAT_TTL` over
#: `HEARTBEAT_CADENCE`, `LIVENESS_LEASE_TTL` over its renewal cadence): two missed passes are a
#: slow ARM enumeration or a revision roll, three is a worker that has stopped.
STALE_AFTER_INTERVALS = 3


class UnschedulableCadenceError(RuntimeError):
    """The reclamation cron is not a plain `*/N` minute step, so no staleness window follows.

    RAISED AT IMPORT, and that is the point: every other shape of minute field failed silently
    wrong rather than absent (a bare `0` made `STALE_AFTER` zero, so every pass — including one
    that finished a second ago — read as stale, and the only dead-worker alarm would fire
    constantly and be tuned out; a list or range raised a bare, unnamed `ValueError`). Failing
    here names the cron and stops the process — the correct end for a scheduling constant that
    cannot be honoured."""


def _minutes_between_passes(cron: str) -> int:
    """The minute step of a `*/N` cron field. Anything else is refused rather than guessed."""
    minute_field = cron.split()[0] if cron.split() else ""
    step = minute_field.removeprefix("*/")
    if step == minute_field or not step.isdigit() or int(step) < 1:
        raise UnschedulableCadenceError(
            f"the reclamation cron {cron!r} is not a '*/N' minute step, so the staleness window "
            "for a dead worker cannot be derived from it"
        )
    return int(step)


#: Derived from the cron rather than restated, so changing the cadence cannot leave the staleness
#: window pointing at the old one. `*/15 * * * *` ⇒ 15 minutes.
_MINUTES_PER_PASS = _minutes_between_passes(RECLAMATION_CRON)
STALE_AFTER = dt.timedelta(minutes=_MINUTES_PER_PASS * STALE_AFTER_INTERVALS)


async def reclamation_pass_freshness(db: AsyncSession) -> tuple[dt.datetime | None, bool]:
    """`(when the last pass finished, is that stale)`.

    NEVER-RAN IS STALE: a `None` last-pass is not "no news is good news" — it is a fresh
    deployment or a worker that has never completed a pass. Same consequence either way:
    nothing is watching the fleet.
    ANY OUTCOME COUNTS AS A PASS, including `declined`/`failed` — this answers "is the worker
    running", not "is it happy", since conflating the two would hide a crashing worker behind
    a merely unhappy one."""
    row = await db.execute(
        sa.select(WorkerPass.finished_at)
        .where(WorkerPass.task_name == RECLAMATION_TASK_NAME)
        .order_by(WorkerPass.finished_at.desc())
        .limit(1)
    )
    last = row.scalar_one_or_none()
    if last is None:
        return None, True
    if last.tzinfo is None:  # a naive column value; compare in UTC rather than crash
        last = last.replace(tzinfo=dt.UTC)
    return last, (dt.datetime.now(dt.UTC) - last) > STALE_AFTER


# ─────────────────────────────────────────────────────────────────────────────────────────
# the copy the reaper takes before it reclaims.
# ─────────────────────────────────────────────────────────────────────────────────────────

#: The `task_name` a copy-before-reclaim row carries, and it is DELIBERATELY NOT
#: `RECLAMATION_TASK_NAME`. `reclamation_pass_freshness` above reads the single newest row for
#: that name and pronounces the scheduler alive on the strength of it — so filing a per-container
#: copy attempt under the pass's own name would let a worker that died hours ago go on looking
#: healthy for as long as anything else kept reaping. Two questions, two names.
DURABLE_COPY_TASK_NAME: Final = "sandbox_durable_copy"


class CopyAttempt(enum.StrEnum):
    """What one reap's attempt to secure a container's work before destroying it came to.

    Several outcomes rather than a bare success/failure pair, because the sparing arms fail for
    reasons an operator has to act on DIFFERENTLY: an unreachable container needs somebody to
    look at the container, and a raised write needs somebody to look at the store. Collapsing
    them would produce a row that says a container was spared and nothing about what to do next.
    """

    #: Nothing was written back, and nothing needed to be: the container still held the untouched
    #: starter template, or it could not be attached at all but a saved bundle already stands for
    #: this app. Recorded for the same reason a zero-candidate pass is: a quiet fleet and a dead
    #: process are otherwise one observation.
    NOTHING_TO_COPY = "nothing_to_copy"
    #: The tree landed in the saved copy, and the container may go.
    COPIED = "copied"
    #: There was nothing to copy FROM and no saved bundle to stand in. The container would not
    #: attach, or the record no longer names the container we are judging — in which case the tree
    #: we could reach belongs to somebody else's build and must never be bundled into this app's
    #: slot.
    UNREACHABLE = "unreachable"
    #: The bundle, the read-back or the upload itself raised. Nothing was established, so nothing
    #: is destroyed.
    FAILED = "failed"


#: How each outcome reads to the operator endpoint: the native enum it stores under, and the one
#: sentence the `detail` column carries. Kept as a table rather than as branches at the write, so
#: adding an outcome cannot ship a row with no explanation in it.
#:
#: NOT ONE OF THESE SENTENCES NAMES A CONTAINER OR AN APP, and that is the `WorkerPass.detail`
#: contract rather than an oversight: the identity belongs in the structlog line the reaper emits
#: beside the row, never in a column an admin endpoint hands out. What the row is for is "this is
#: happening, it is not getting better, and here is which half to look at".
_ATTEMPT_MEANING: Final[dict[CopyAttempt, tuple[PassOutcome, str]]] = {
    CopyAttempt.NOTHING_TO_COPY: (
        PassOutcome.OK,
        "there was nothing to write back before reclaiming",
    ),
    CopyAttempt.COPIED: (
        PassOutcome.OK,
        "the tree was written back before the container was reclaimed",
    ),
    CopyAttempt.UNREACHABLE: (
        PassOutcome.DECLINED,
        "the container we judged could not be reached, so nothing could be written back; spared",
    ),
    CopyAttempt.FAILED: (
        PassOutcome.FAILED,
        "the write-back raised; see the traceback on the reaper's log line. Spared",
    ),
}

#: The arms that leave a container standing. Read once here rather than re-derived at the write,
#: because "which outcomes spared something" is the only question this row is ever asked.
_SPARED: Final = frozenset({CopyAttempt.UNREACHABLE, CopyAttempt.FAILED})


async def record_durable_copy_attempt(attempt: CopyAttempt) -> None:
    """Write the row for ONE container's copy-before-reclaim attempt. Never raises.

    ON EVERY OUTCOME, including "already current" — else empty rows can't be told from a worker
    that never ran (same inference `_record_pass` protects, one level down).
    Imports its session factory INSIDE the function — `tests/conftest.py` rebinds it onto a
    per-test engine, and a module-level import would hand tests a connection from the wrong loop.
    NEVER FAILS THE REAP: logged, not swallowed — a silent write failure would make a working
    reaper look stopped."""
    from src.db.base import async_session_factory

    try:
        # INSIDE the try, so the docstring's "never raises" is literally true. An
        # unmapped member is a bug, but a bug that aborts a REAP is worse than one that loses
        # a row — and this sits on a destroy path where an escaping exception ends the whole
        # user's sweep.
        outcome, detail = _ATTEMPT_MEANING[attempt]
        async with async_session_factory() as db:
            db.add(
                WorkerPass(
                    task_name=DURABLE_COPY_TASK_NAME,
                    outcome=outcome,
                    finished_at=dt.datetime.now(dt.UTC),
                    counts={
                        "copied": 1 if attempt is CopyAttempt.COPIED else 0,
                        "spared": 1 if attempt in _SPARED else 0,
                    },
                    detail=detail,
                )
            )
            await db.commit()
    except Exception:
        _log.exception("durable_copy_attempt_record_failed", attempt=attempt.value)
