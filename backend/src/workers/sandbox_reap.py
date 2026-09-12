"""The scheduled sandbox sweep: `sweep_all` on the worker, every five minutes.

TWO GATES BIND THIS PATH AND NEITHER IS OPTIONAL, because passing no `app_id` is how the
durable-copy precondition is opted out of and this is where almost all of the deleting happens:
`_owning_app_ids` resolves the owner so the gate binds, and `may_destroy_on_this_control_plane`
keeps the unattended timer off every non-production control plane.

THE PER-PASS CEILING IS THE ONE GUARD THIS PATH DELIBERATELY DOES NOT TAKE. It reaches only what
the registry already has a record of, and a bounded sweep that never finishes its list would
leave the same users unreconciled on every tick.
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
    # `sweep_enabled`, NOT `reclaim_enabled`: this flag gates this sweep and ships ON, while
    # `reclaim_enabled` gates the reclamation pass and is off everywhere. Reading the other one
    # here would stop all reaping on every deployment that has not opted into that pass.
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
    except SandboxNotConfiguredError:
        _log.info("sandbox_reap_pass_disabled", reason="unconfigured")
        return

    if result.failed:
        _log.warning("sandbox_reap_pass_partial", reaped=result.reaped, failed=result.failed)
    else:
        _log.info("sandbox_reap_pass_completed", reaped=result.reaped)


async def _owning_app_ids() -> dict[str, uuid.UUID]:
    """Container name → the app that owns it, which is what binds this sweep's durable-copy gate.

    A DATABASE THAT WILL NOT ANSWER FAILS THE PASS rather than returning an empty map. Empty
    resolves every container to `None`, which is indistinguishable from "the caller opted out"
    and would silently un-gate the whole sweep at exactly the moment nothing can be verified.
    The raise is logged by the receiver and the next tick, five minutes out, retries."""
    from src.db.base import async_session_factory
    from src.services.build_sessions.inventory import _app_names_to_owners

    async with async_session_factory() as db:
        owners = await _app_names_to_owners(db)
    return {name: known.app_id for name, known in owners.items()}
