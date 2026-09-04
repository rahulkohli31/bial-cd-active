"""The F1 sweep, on the worker (U15).

WHAT THIS IS. `sweep_all` → `reconcile_user(honor_stay=True)` → `reap_user` is the ONLY scheduled
reclamation the platform has ever had, and it does almost all of the deleting. Its sole timer call
site was a `while True` in `main.py`'s lifespan; this is that loop, ported onto the scheduler
**before** the loop is deleted — the same order U6 used, so there is never a window with no sweep.

WHY PORTING IT UNCHANGED WOULD HAVE BEEN WRONG. Every safety property this plan added — the
durable-copy precondition, staging, the ceiling, re-validation — was written for orphan
collection. The claimed-but-expired path (F1) is where the deletions actually happen, so a port
that skipped the gates would have left the code that does the work subject to none of the new
protection, while the report-only pass carefully guarded the rare case. The first port DID skip
them: it called `sweep_all` with no `app_id`, which is precisely how the U14 durable-copy gate is
opted out of, and off no allowlist at all. Two things close that here and neither is optional —
`_owning_app_ids` resolves the owner so the gate binds, and `may_destroy_on_this_control_plane`
keeps the unattended timer off every non-production control plane. The ceiling is the one guard
this path deliberately does not take: it enumerates only what the registry already knows, and a
bounded sweep that never finishes its list would leave the same users unreconciled every pass.

THE FOUR-STEP ORDERING IS PRESERVED because out of process both halves of it matter:
`mark_registry_ending` (guards a concurrent attach) → `teardown` → `delete_registry` →
`reap_lock` LAST. In-process the reaper and every start shared an event loop; they no longer do.
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
    # `sweep_enabled`, NOT `reclaim_enabled`, and the distinction is load-bearing. This sweep is
    # the pre-existing one — an unflagged lifespan loop before U15 — so its flag ships ON, or the
    # port would have silently stopped all reaping on every deployment that has not opted into
    # the new pass. `reclaim_enabled` gates the NEW reclamation pass and stays off.
    if settings.sandbox is None or not settings.sandbox.sweep_enabled:
        return "flag_off"
    # THE SAME DEV ALLOWLIST THE JANITOR IS BEHIND, and for the same standing reason: the dev
    # subscription is a test bed holding containers people are actively using to validate this
    # feature, and an unattended timer deleting one destroys the evidence. Imported here rather
    # than at module scope so a deployment with reclamation off still pays for nothing — and
    # checked LAST so the two ordinary off states answer before the import happens at all.
    #
    # This gates the SCHEDULED sweep only. `POST /v1/build-sessions/internal/reap` still runs
    # `sweep_all` anywhere: it is superadmin-authenticated, audited, and has a human behind it who
    # can see what they asked for. (Written `/v1/internal/reap` until now, which 404s — the route
    # is mounted on the build-sessions router, and this line is one an operator pastes.)
    # Reconcile-on-start is likewise untouched, so a developer's own stale sandbox is still
    # collected the moment they start their next build.
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
        result = await sweep_all(
            get_redis(),
            get_sandbox(),
            live_users=set(),
            app_ids_by_name=await _owning_app_ids(),
        )
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


async def _owning_app_ids() -> dict[str, uuid.UUID]:
    """Container name → the app that owns it, so this sweep's teardowns are gated by U14.

    WITHOUT THIS THE GATE IS OFF ON THE PATH THAT DOES THE DELETING. `reap_user` only calls
    `confirm_durable_copy` when it is handed an `app_id`, and the sweep handed it nothing — so
    the durable-copy precondition applied to the rare orphan the janitor collects and not at all
    to the claimed-but-expired population, which is where the deletions actually happen.

    FORWARD-MATCHED, never reverse-parsed. `app_name_for` truncates the app id to 28 of its 32
    hex characters, so a sandbox name does not identify its app; the map is built by deriving
    each known app's name and comparing. `_app_names_to_owners` is the same fleet-wide read the
    reclamation pass and the C10 backfill use — reading two identifier columns and no user data.

    A DATABASE THAT WILL NOT ANSWER FAILS THE PASS rather than returning an empty map. Empty
    resolves every container to `None`, which is indistinguishable from "the caller opted out"
    and would silently un-gate the whole sweep at exactly the moment nothing can be verified.
    The raise is logged by the receiver and the next tick, five minutes out, retries."""
    from src.db.base import async_session_factory
    from src.services.build_sessions.inventory import _app_names_to_owners

    async with async_session_factory() as db:
        owners = await _app_names_to_owners(db)
    return {name: app_id for name, (app_id, _user_id) in owners.items()}
