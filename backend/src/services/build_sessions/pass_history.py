"""Is the scheduled worker alive? (U11, R20.)

THE ONLY HONEST DETECTOR OF A DEAD WORKER IS SILENCE. Every alarm the reclamation pass raises is
emitted *by the pass* — the fleet-count warning, the store-fault error, the candidate lines. A
crashlooping scheduler emits none of them, which reads exactly like a healthy quiet fleet. That is
the origin incident's epistemic failure relocated one layer out, and it is why the pass writes a
record on every outcome and why this module reads the ABSENCE of one as the alarm.

Read from Postgres, never Redis: under any eviction policy a Redis marker is evictable, so a
staleness alarm keyed on one would fire spuriously *and* its absence would be indistinguishable
from a real outage.
"""

from __future__ import annotations

import datetime as dt

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models.worker_pass import WorkerPass
from src.workers.reclamation import RECLAMATION_CRON, RECLAMATION_TASK_NAME

#: How many scheduled intervals may pass before silence counts as a fault. Three, matching the
#: head-room every other liveness signal in this system uses (`HEARTBEAT_TTL` over
#: `HEARTBEAT_CADENCE`, `LIVENESS_LEASE_TTL` over its renewal cadence): two missed passes are a
#: slow ARM enumeration or a revision roll, three is a worker that has stopped.
STALE_AFTER_INTERVALS = 3

#: Derived from the cron rather than restated, so changing the cadence cannot leave the staleness
#: window pointing at the old one. `*/15 * * * *` ⇒ 15 minutes.
_MINUTES_PER_PASS = int(RECLAMATION_CRON.split()[0].removeprefix("*/"))
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
