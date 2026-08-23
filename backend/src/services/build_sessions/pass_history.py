"""Is the scheduled worker alive, and did the reap it performed secure anything? (U11, R20; U5.)

THE ONLY HONEST DETECTOR OF A DEAD WORKER IS SILENCE. Every alarm the reclamation pass raises is
emitted *by the pass* — the fleet-count warning, the store-fault error, the candidate lines. A
crashlooping scheduler emits none of them, which reads exactly like a healthy quiet fleet. That is
the origin incident's epistemic failure relocated one layer out, and it is why the pass writes a
record on every outcome and why this module reads the ABSENCE of one as the alarm.

Read from Postgres, never Redis: under any eviction policy a Redis marker is evictable, so a
staleness alarm keyed on one would fire spuriously *and* its absence would be indistinguishable
from a real outage.

WHY A WRITER LIVES HERE TOO. U5 taught the reaper to take a durable copy before it reclaims, and
the failure mode it inherits is the same epistemic one: a container whose copy cannot be taken is
SPARED, and a spared container is indistinguishable from a fleet with nothing in it. It bills
forever and nothing says so — which is the state ASM30 found the platform already in, at the two
`confirm_durable_copy` call sites that only ever logged. So the attempt writes a `worker_passes`
row on every outcome, exactly as the pass above does, and for exactly the same reason: the row is
the only thing an operator can look for that does not depend on the failing component to speak up.
"""

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

    RAISED AT IMPORT, and that is the point. Every other shape of minute field is silently wrong
    rather than absent: a bare `0` parsed to a zero-minute cadence, which made `STALE_AFTER` zero
    and every pass — including one that finished a second ago — read as stale, so the only alarm
    that can detect a dead worker would fire constantly and be tuned out. A list (`0,30`) or a
    range raised a bare `ValueError` from `int()`, naming nothing. Failing here names the cron and
    stops the process, which is the correct end for a scheduling constant that cannot be honoured.
    """


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

    NEVER-RAN IS STALE. A `None` last-pass is not "no news is good news" — it is a fresh
    deployment whose worker has not started, or one whose worker has never successfully completed
    a single pass. Different causes, same consequence: nothing is watching the fleet.

    ANY OUTCOME COUNTS AS A PASS, including `declined` and `failed`. The question this answers is
    "is the worker running", not "is it happy" — a pass that fails every tick is a different
    problem from a worker that is not there, and conflating them would hide the second behind the
    first."""
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
# U5 — the copy the reaper takes before it reclaims (ADR-0029 §7).
# ─────────────────────────────────────────────────────────────────────────────────────────

#: The `task_name` a copy-before-reclaim row carries, and it is DELIBERATELY NOT
#: `RECLAMATION_TASK_NAME`. `reclamation_pass_freshness` above reads the single newest row for
#: that name and pronounces the scheduler alive on the strength of it — so filing a per-container
#: copy attempt under the pass's own name would let a worker that died hours ago go on looking
#: healthy for as long as anything else kept reaping. Two questions, two names.
DURABLE_COPY_TASK_NAME: Final = "sandbox_durable_copy"


class CopyAttempt(enum.StrEnum):
    """What one reap's attempt to secure a container's work before destroying it came to.

    Five outcomes rather than a bare success/failure pair, because the three sparing arms fail
    for reasons an operator has to act on DIFFERENTLY: an unreachable container needs somebody to
    look at the container, a refused promotion needs somebody to look at the diverted bundle, and
    a raised write needs somebody to look at the store. Collapsing them would produce a row that
    says a container was spared and nothing about what to do next.
    """

    #: The gate was already satisfied — the durable copy is current, so there was nothing to take
    #: before reclaiming. The zero-candidate case, and it is recorded for the same reason a
    #: zero-candidate pass is: a quiet fleet and a dead process are otherwise one observation.
    NOTHING_TO_COPY = "nothing_to_copy"
    #: ADR-0029 §7 kept: a fresh copy landed in the recovery slot, and the container may go.
    COPIED = "copied"
    #: There was nothing to copy FROM. The container would not attach, or the record no longer
    #: names the container we are judging — in which case the tree we could reach belongs to
    #: somebody else's build and must never be bundled into this app's slot.
    UNREACHABLE = "unreachable"
    #: U3's guard would not promote this tree over the copy on record: the lineage is broken or
    #: unreadable. The bundle is preserved under `divert_key`, the existing copy is untouched,
    #: and the container is spared — a refusal is never a licence to destroy.
    REFUSED = "refused"
    #: The bundle, the read-back or the upload itself raised. Nothing was established, so nothing
    #: is destroyed.
    FAILED = "failed"
    #: A copy landed, but it is THE FIRST ONE — there was nothing on record to compare it against,
    #: so U3's lineage guard never ran. Fine at a turn boundary, where the container is alive and
    #: the tree is the citizen's; NOT a licence to destroy, because a reverted container has
    #: exactly this shape and the copy we just took would be the reverted tree.
    UNGUARDED = "unguarded"
    #: The gate was satisfied by its unreadable-container FALLBACK — a parseable bundle stood in
    #: because the container could not answer — so the destroy proceeded without any comparison
    #: having run. Recorded distinctly because "already current" would be a claim nobody made.
    UNVERIFIED_FALLBACK = "unverified_fallback"


#: How each outcome reads to the operator endpoint: the native enum it stores under, and the one
#: sentence the `detail` column carries. Kept as a table rather than as branches at the write, so
#: adding an outcome cannot ship a row with no explanation in it.
#:
#: NOT ONE OF THESE SENTENCES NAMES A CONTAINER OR AN APP, and that is the `WorkerPass.detail`
#: contract rather than an oversight: a sandbox name embeds 28 hex characters of its app's uuid,
#: this column is read by an admin endpoint, and the identity belongs in the structlog line the
#: reaper emits beside the row (C10 §3.6). What the row is for is "this is happening, it is not
#: getting better, and here is which half to look at".
_ATTEMPT_MEANING: Final[dict[CopyAttempt, tuple[PassOutcome, str]]] = {
    CopyAttempt.NOTHING_TO_COPY: (
        PassOutcome.OK,
        "the durable copy was already current; nothing to take before reclaiming",
    ),
    CopyAttempt.COPIED: (
        PassOutcome.OK,
        "a recovery copy was taken before the container was reclaimed",
    ),
    CopyAttempt.UNREACHABLE: (
        PassOutcome.DECLINED,
        "the container we judged could not be reached, so no copy could be taken; spared",
    ),
    CopyAttempt.REFUSED: (
        PassOutcome.DECLINED,
        "the working tree is not a descendant of the copy on record; diverted and spared",
    ),
    CopyAttempt.FAILED: (
        PassOutcome.FAILED,
        "the recovery write raised; see the traceback on the reaper's log line. Spared",
    ),
    CopyAttempt.UNVERIFIED_FALLBACK: (
        PassOutcome.DECLINED,
        "the container could not be read, so a standing bundle stood in for the comparison; "
        "reclaimed without verifying currency",
    ),
    CopyAttempt.UNGUARDED: (
        PassOutcome.DECLINED,
        "the copy taken was the first on record, so no lineage guard ran; spared rather than "
        "destroyed on the strength of an unverified tree",
    ),
}

#: The arms that leave a container standing. Read once here rather than re-derived at the write,
#: because "which outcomes spared something" is the only question this row is ever asked.
_SPARED: Final = frozenset({CopyAttempt.UNREACHABLE, CopyAttempt.REFUSED, CopyAttempt.FAILED})


async def record_durable_copy_attempt(attempt: CopyAttempt) -> None:
    """Write the row for ONE container's copy-before-reclaim attempt. Never raises.

    ON EVERY OUTCOME, including the boring one. A reap that found the copy already current writes
    too — otherwise the only rows in the table are the unhappy ones, and an operator reading an
    empty result cannot tell "nothing went wrong" from "nothing ran at all". That is the same
    inference `workers/reclamation._record_pass` protects, one level down.

    ITS OWN SESSION, and the factory is imported INSIDE the function rather than at module scope.
    That is not style: `tests/conftest.py` REBINDS `src.db.base.async_session_factory` onto a
    NullPool engine before any consumer can bind it by value, because pytest-asyncio runs each
    test on its own loop and a pooled asyncpg connection belongs to the loop that opened it. A
    module-level `from ... import async_session_factory` captures the pooled original and hands
    every test a connection from the wrong loop.

    BOOKKEEPING NEVER FAILS THE REAP. A reap that correctly spared a container must not raise
    because its record could not be written — but it is logged with a traceback rather than
    swallowed, because the whole value of this table is that its silence means something, and a
    quiet write failure would make a working reaper look like one that stopped."""
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
