"""The F1 sweep, on the worker (U15).

WHAT THIS IS. `sweep_all` → `reconcile_user(honor_stay=True)` → `reap_user` is the ONLY scheduled
reclamation the platform has ever had, and it does almost all of the deleting. Its sole timer call
site was a `while True` in `main.py`'s lifespan; this is that loop, ported onto the scheduler
**before** the loop is deleted — the same order U6 used, so there is never a window with no sweep.

WHY PORTING IT UNCHANGED WOULD HAVE BEEN WRONG. Every safety property this plan added — the
durable-copy precondition, staging, the ceiling, re-validation — was written for orphan
collection. The claimed-but-expired path (F1) is where the deletions actually happen, so a port
that skipped the gates would have left the code that does the work subject to none of the new
protection, while the report-only pass carefully guarded the rare case.

THE FOUR-STEP ORDERING IS PRESERVED because out of process both halves of it matter:
`mark_registry_ending` (guards a concurrent attach) → `teardown` → `delete_registry` →
`reap_lock` LAST. In-process the reaper and every start shared an event loop; they no longer do.
"""

from __future__ import annotations

from typing import Final

import structlog

from src.broker import broker
from src.config import settings

_log = structlog.get_logger()

SANDBOX_REAP_TASK_NAME: Final = "sandbox_reap"
SANDBOX_REAP_SCHEDULE_ID: Final = "sandbox-reap-every-5m"

#: Five minutes — the cadence `SWEEP_INTERVAL_SECONDS` has run at since v1.6.5, so moving the work
#: to the worker changes WHERE it runs and nothing else. The cadence is load-bearing in four
#: places at once (see `reclaim.PASS_CADENCE`), which is why it is not rounded off here.
SANDBOX_REAP_CRON: Final = "*/5 * * * *"


def _off_duty_because() -> str | None:
    """Why this sweep must not run, or `None` when it may."""
    if settings.redis is None:
        # The sweep enumerates from the registry; with no coordination store there is nothing to
        # enumerate, and a sweep that "found nothing" would be a lie rather than an answer.
        return "unconfigured"
    if settings.sandbox is None or not settings.sandbox.reclaim_enabled:
        return "flag_off"
    return None


@broker.task(
    task_name=SANDBOX_REAP_TASK_NAME,
    schedule=[{"cron": SANDBOX_REAP_CRON, "schedule_id": SANDBOX_REAP_SCHEDULE_ID}],
)
async def reap_abandoned_sandboxes() -> None:
    """Reconcile every registered user whose session has stopped beating.

    THE FLAG GATE COMES FIRST, before a single heavy import — the contract `deploy_reconcile` set
    and `reclamation` follows.

    A WORKER MAY NEVER PASS `certified_dead=True`. That flag is a caller assertion whose third
    premise is "this is the only replica", and no background process can establish it — a worker
    passing it would reap live builds while their owners watched. Enforced by a test that scans
    `src/workers/` recursively; this module is inside that scan."""
    off_duty = _off_duty_because()
    if off_duty is not None:
        _log.info("sandbox_reap_pass_disabled", reason=off_duty)
        return

    from src.services.build_sessions.reaper import sweep_all
    from src.services.redis import get_redis
    from src.services.sandbox import SandboxNotConfiguredError, get_sandbox

    try:
        # `live_users` is EMPTY here, and that is correct rather than a gap: it is an in-process
        # set that means nothing in a second process. What replaces it is the R10 wall-clock
        # liveness lease (U12), which `reconcile_user` reads before the lock/heartbeat pair — the
        # signal that made running this sweep outside the API process possible at all.
        result = await sweep_all(get_redis(), get_sandbox(), live_users=set())
    except SandboxNotConfiguredError:
        _log.info("sandbox_reap_pass_disabled", reason="unconfigured")
        return

    # REAPED-NOTHING-BUT-FAILED-SOME is the systemic shape worth waking to, and it used to log
    # nothing at all — a sweep where every user threw returns reaped=0 and reads exactly like a
    # sweep with nothing to do, while containers accumulate and bill.
    if result.failed:
        _log.warning("sandbox_reap_pass_partial", reaped=result.reaped, failed=result.failed)
    else:
        _log.info("sandbox_reap_pass_completed", reaped=result.reaped)
