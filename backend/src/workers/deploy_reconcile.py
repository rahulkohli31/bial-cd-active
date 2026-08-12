"""Deploy reconciliation on the scheduler — the chassis's first passenger (U6, ADR-0011).

WHY THIS WORKLOAD FIRST. It cannot destroy anything. `reconcile_stalled_deployments` settles a
database ROW and at most promotes it, and the type it is handed (`PublishedAppReader`) declares
no delete method at all — so a wrong answer here costs a row that reads `failed` instead of
`running`, never a container app. Everything else queued for this scheduler can delete an Azure
resource, and none of that may run out-of-process until the R10 liveness lease lands (U12).

AND ITS LIVENESS SIGNAL IS ALREADY OUT-OF-PROCESS. Staleness is `deployments.heartbeat_at`, a
shared database column read under a `status = 'running'` guard — not an in-process set like the
sandbox reaper's live-session shield — so moving this pass into another container changes nothing
about what it can see. That is precisely why it is safe today and the sandbox sweep is not.

WHAT IS DELIBERATELY NOT HERE
-----------------------------
`main.py`'s `_reconcile_interrupted_deploys` is a BOOT-PATH ONE-SHOT, not a loop. It settles a
deploy that straddled a restart *before the first request is served*, which no cron can do — a
five-minute tick would leave the citizen looking at a Deploy button that 409s in the meantime. It
survives untouched. Its periodic twin `_reconcile_deploys_periodically` is what this module
replaces, and that one is removed in **U7, not here**: until the worker is actually provisioned
in Azure this replacement exists only in the repo, and deleting a live reconciler one unit ahead
of its deployed replacement is the same mistake in deploy form that the plan forbids in file
form. The two are easy to confuse and easy to delete symmetrically; they are not symmetric.

RUNNING BOTH AT ONCE IS SAFE, which is what makes that overlap affordable. Every terminal write
goes through `store._finish`'s `WHERE status = 'running'` guard and returns its rowcount, so of
two racing reconcilers exactly one settles a given row and the other learns it lost. The pass is
idempotent for the same reason two schedulers during an ACA revision roll are survivable
(ADR-0029 §9).

IMPORT DISCIPLINE. Module scope imports structlog, the broker, the settings front door and
taskiq's own cron predicate — nothing else. The ORM, the ARM SDK and the reconciler itself are
imported INSIDE the task body, after the flag gate, so a disabled task costs an import of the
chassis and nothing more (`src/workers/__init__.py`).
"""

from __future__ import annotations

import structlog

from src.broker import broker
from src.config import settings

_log = structlog.get_logger()

# Distinguishable event names: an operator (or an Azure Monitor rule) greps for exactly these,
# so they are constants rather than inline literals. Deliberately distinct from `main.py`'s
# `deploy_startup_reconcile`, so "the scheduler ran a pass" and "a process booted" never blur
# into one another in the log stream.
DEPLOY_RECONCILE_DONE_EVENT = "deploy_reconcile_pass_done"
DEPLOY_RECONCILE_DISABLED_EVENT = "deploy_reconcile_pass_disabled"

# The task's stable wire name. It travels inside every queued message and is the key the
# receiver looks the executor up by, so renaming it strands anything already on the stream.
DEPLOY_RECONCILE_TASK_NAME = "deploy.reconcile_stalled"

# Pinned, not minted. `LabelScheduleSource` generates a fresh `uuid4().hex` for any schedule
# entry that does not carry one, per process start — which makes the id useless as a correlation
# key in logs and useless as a dedupe key for anything downstream.
DEPLOY_RECONCILE_SCHEDULE_ID = "deploy-reconcile-every-five-minutes"


def _honoured_by_the_clock(expression: str) -> str:
    """Return `expression`, or refuse to import.

    Taskiq validates a cron NOWHERE at decoration time: `@broker.task(schedule=[...])` stores the
    label verbatim, and a bad expression surfaces only as a warning logged once a second inside
    the scheduler loop, forever, while the task never fires. A reconciler that silently never
    runs is the exact failure this plan exists to stop being surprised by — so it is asserted at
    IMPORT, which is worker startup: `worker_main.startup()` imports every task module before the
    receiver and the clock begin.

    Two properties, not one. Checked with `is_cron_task_now` — the very predicate the scheduler
    loop calls — rather than a second cron library that could disagree with the one that actually
    decides. And checked for FIRING rather than merely for parsing, because the underlying
    evaluator accepts `99 * * * *` without complaint and then matches no minute ever; "it parses"
    is not the property worth asserting. Any cadence of an hour or finer must match at least one
    minute in the next sixty.
    """
    from datetime import UTC, datetime, timedelta

    from taskiq.cli.scheduler.run import CronValueError, is_cron_task_now

    minute = datetime.now(UTC).replace(second=0, microsecond=0)
    try:
        fires = any(
            is_cron_task_now(expression, minute + timedelta(minutes=ahead)) for ahead in range(60)
        )
    except CronValueError as exc:
        raise ValueError(
            f"the deploy-reconcile cron {expression!r} does not parse, so the scheduler would "
            f"log a warning every second and never fire the task"
        ) from exc
    if not fires:
        raise ValueError(
            f"the deploy-reconcile cron {expression!r} parses but matches no minute in the next "
            f"hour, so the scheduler would never fire the task"
        )
    return expression


# Every five minutes on the wall clock — the cadence the in-process loop has run at since v1.6.5
# (`main.py::DEPLOY_RECONCILE_INTERVAL_SECONDS`), so moving the work to the worker changes WHERE
# it runs and nothing else.
#
# CRON, NEVER `interval`. `is_interval_task_now` returns True whenever `last_run is None`, and
# last-run state is an in-memory dict the scheduler never persists — so an interval task fires on
# EVERY process start, and `skip_first_run` does not suppress it. At ACA revision-roll frequency
# that is an unasked-for pass per deploy, forever.
DEPLOY_RECONCILE_CRON = _honoured_by_the_clock("*/5 * * * *")


def _off_duty_because() -> str | None:
    """Why this pass must not run, or `None` when it may.

    Two conditions, reported separately because they mean different things to whoever reads the
    log line. `unconfigured` is "this deployment does not publish apps at all" — the ordinary
    dev, test and not-yet-granted-the-registry-role posture. `flag_off` is "it does, and an
    operator has the timer switched off", which is the state every environment ships in until U7
    has watched a scheduled pass run in Azure.
    """
    deploy = settings.deploy
    if deploy is None:
        return "unconfigured"
    if not deploy.reconcile_enabled:
        return "flag_off"
    return None


@broker.task(
    task_name=DEPLOY_RECONCILE_TASK_NAME,
    # A LIST of dicts. A bare dict raises inside `LabelScheduleSource.startup()`, and an entry
    # missing all of {cron, interval, time} is silently skipped — a schedule that looks present
    # and never fires.
    schedule=[{"cron": DEPLOY_RECONCILE_CRON, "schedule_id": DEPLOY_RECONCILE_SCHEDULE_ID}],
)
async def reconcile_stalled_deploys() -> None:
    """Settle every deployment row whose pipeline stopped beating, and say how many.

    THE FLAG GATE COMES FIRST, before a single heavy import — that ordering is the contract, not
    an optimization. A disabled task must cost structlog, the broker and the settings profile and
    nothing else, so that adding a passenger to this scheduler never taxes a deployment that has
    not turned it on.

    NOTHING IS SWALLOWED. Unlike the boot-path one-shot — where a raise would turn an ARM blip
    into a container that refuses to start, so it catches broadly on purpose — a raise here is
    caught by the receiver, logged with a traceback, and re-driven by the next tick five minutes
    later. Swallowing would buy nothing and hide everything.

    A row ARM could not answer for is NOT counted as reconciled. `reconcile_stalled_deployments`
    catches the transient case per row and leaves that row exactly as it was, so it reappears in
    the next pass's work list — the one answer that must never collapse into "gone", because
    collapsing it eventually marks a live app failed.

    Counts only in the log line (`.claude/rules/security.md`): `resolved` is a number, never a
    deployment id and never an app name. Every completed pass logs, including a pass that
    resolved nothing — the presence of the event is the liveness signal an out-of-process worker
    is judged by, and "quiet" would be indistinguishable from "dead".
    """
    off_duty = _off_duty_because()
    if off_duty is not None:
        _log.info(DEPLOY_RECONCILE_DISABLED_EVENT, reason=off_duty)
        return

    from src.db.base import async_session_factory
    from src.services.deploy.aca_publish import DeployNotConfiguredError, get_published_apps
    from src.services.deploy.reconcile import reconcile_stalled_deployments

    try:
        published_apps = get_published_apps()
    except DeployNotConfiguredError:
        # Belt and braces against the gate above: `get_published_apps` reads `settings.deploy`
        # itself, and the two reads are not one atomic look. A defined state, not a failure.
        _log.info(DEPLOY_RECONCILE_DISABLED_EVENT, reason="unconfigured")
        return

    resolved = await reconcile_stalled_deployments(async_session_factory, published_apps)
    _log.info(DEPLOY_RECONCILE_DONE_EVENT, resolved=resolved)
