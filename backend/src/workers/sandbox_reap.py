"""The scheduled sandbox sweep on the worker, every five minutes: `sweep_all` over the registry,
then `sweep_owed_teardowns` over the deletions this platform still owes.

TWO INPUTS, BECAUSE THE REGISTRY CANNOT SEE THE SECOND POPULATION. A container whose deletion is
owed has had its registry record cleared — that is what hands the citizen their workspace back
after a failure of ours — so the scan reaches everything except exactly the containers that
already went wrong once. The owed row is the only thing that still names one.

TWO GUARDS BIND THIS PATH AND NEITHER IS OPTIONAL, because this is where almost all of the
deleting happens: `_owning_app_ids` names the slot each container's tree is written back to, and
without it a sweep destroys unsaved work; `may_destroy_on_this_control_plane` keeps the unattended
timer off every non-production control plane.

THE PER-PASS CEILING IS THE ONE GUARD THIS PATH DELIBERATELY DOES NOT TAKE. A bounded sweep that
never finishes its list would leave the same users unreconciled on every tick.
"""

from __future__ import annotations

import uuid
from typing import Final

import structlog

from src.broker import broker
from src.config import settings

_log = structlog.get_logger()

SANDBOX_REAP_TASK_NAME: Final = "sandbox_reap"
SANDBOX_REAP_SCHEDULE_ID: Final = "sandbox-reap-every-5m"
SANDBOX_REAP_CRON: Final = "*/5 * * * *"


def _off_duty_because() -> str | None:
    """Why this sweep must not run, or `None` when it may."""
    if settings.redis is None:
        # The sweep enumerates from the registry, so with no coordination store it would report
        # a zero it has not earned rather than an answer.
        return "unconfigured"
    if settings.sandbox is None or not settings.sandbox.sweep_enabled:
        return "flag_off"
    # This gates the SCHEDULED sweep only: `POST /v1/build-sessions/internal/reap` still runs
    # `sweep_all` on any control plane, and reconcile-on-start still collects a developer's own
    # stale sandbox at their next build, so an off-production deployment is not left unreaped.
    from src.services.build_sessions.destroy import may_destroy_on_this_control_plane

    if not may_destroy_on_this_control_plane(str(settings.ENVIRONMENT)):
        return "off_production"
    return None


@broker.task(
    task_name=SANDBOX_REAP_TASK_NAME,
    schedule=[{"cron": SANDBOX_REAP_CRON, "schedule_id": SANDBOX_REAP_SCHEDULE_ID}],
)
async def reap_abandoned_sandboxes() -> None:
    """Reconcile every registered user whose session has stopped beating.

    A worker may never certify a session dead — that assertion rests on being the only replica,
    which no background process can establish. `test_no_worker_module_may_certify_death` pins it.
    """
    off_duty = _off_duty_because()
    if off_duty is not None:
        _log.info("sandbox_reap_pass_disabled", reason=off_duty)
        return

    from src.services.build_sessions.reaper import sweep_all
    from src.services.build_sessions.shutdown import sweep_owed_teardowns
    from src.services.redis import get_redis
    from src.services.sandbox import SandboxNotConfiguredError, get_sandbox

    try:
        # `live_users` is an in-process set and means nothing in a second process; what spares a
        # build in flight here is the wall-clock liveness lease instead.
        result = await sweep_all(
            get_redis(),
            get_sandbox(),
            live_users=set(),
            app_ids_by_name=await _owning_app_ids(),
        )
        # THE REGISTRY IS NO LONGER THE ONLY INPUT. A container the platform owes a deletion for
        # has had its registry record cleared — that is what gives the citizen their workspace
        # back — so the scan above cannot see it at all. The owed row is the only thing that
        # still names it, and this is the pass that acts on one.
        owed = await sweep_owed_teardowns(get_redis(), get_sandbox())
    except SandboxNotConfiguredError:
        _log.info("sandbox_reap_pass_disabled", reason="unconfigured")
        return

    if result.failed or owed.failed:
        _log.warning(
            "sandbox_reap_pass_partial",
            reaped=result.reaped,
            failed=result.failed,
            owed_settled=owed.settled,
            owed_still_outstanding=owed.still_owed,
            owed_failed=owed.failed,
        )
    else:
        _log.info(
            "sandbox_reap_pass_completed",
            reaped=result.reaped,
            owed_settled=owed.settled,
            owed_still_outstanding=owed.still_owed,
        )


async def _owning_app_ids() -> dict[str, uuid.UUID]:
    """`owning_app_ids` on a session of this pass's own, since a scheduled task has no request
    to borrow one from. A raise here fails the pass; the next tick, five minutes out, retries."""
    from src.db.base import async_session_factory
    from src.services.build_sessions.inventory import owning_app_ids

    async with async_session_factory() as db:
        return await owning_app_ids(db)
