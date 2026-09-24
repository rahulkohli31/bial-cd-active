"""The worker process: `python -m src.worker_main`.

ONE process runs BOTH taskiq roles — the receiver that executes tasks and the scheduler loop that
enqueues cron ticks — as two supervised asyncio tasks. A receiverless scheduler would enqueue work
nothing consumes behind a healthy-looking container, so the receiver is mandatory and the scheduler
rides along.

WHAT IS ON A TIMER: deploy reconciliation and the sandbox sweep every five minutes, the fleet
reclamation pass every fifteen (report-only — its destroy flag is off everywhere today).
Everything else that sweeps is OPERATOR-INVOKED, run only when a superadmin calls it; `main.py`'s
boot one-shot is neither — it settles a deploy before cron can run.

WHY THIS EXISTS instead of `taskiq worker` / `taskiq scheduler`: both were tried and both are
broken — a `WORKER_STARTUP` handler that starts the scheduler recurses without bound
(`run_scheduler` → `TaskiqScheduler.startup()` → `broker.startup()` → re-fires `WORKER_STARTUP`),
and `taskiq worker`'s worker-count (default 2) forks children that fire it too, past any replica
pin. Owning the entrypoint makes "one scheduler" true BY CONSTRUCTION, and a fatal error exits
visibly instead of crash-looping.

Even with one scheduler per process and one replica, ACA drains the old revision while the new one
starts, so TWO SCHEDULERS EXIST DURING EVERY DEPLOY WINDOW — taskiq has no leader election. The
replica pin is a defence, not an exclusivity guarantee, which is why every scheduled pass must be
idempotent and single-flighted in its own right.
"""

from __future__ import annotations

import asyncio
import contextlib
import signal
import sys

import structlog
from taskiq.acks import AcknowledgeType
from taskiq.api import run_receiver_task
from taskiq.cli.scheduler.run import SchedulerLoop

from src.broker import broker
from src.config import settings
from src.core.log_config import configure_logging
from src.scheduler import schedule_sources, scheduler

configure_logging(production=settings.is_production)
_log = structlog.get_logger()

# How long to wait before retrying after the receiver dies. `run_receiver_task`'s own loop
# catches broad exceptions with NO backoff, so a permanently-broken Redis becomes a hot spin.
_RECEIVER_RESTART_BACKOFF_S: float = 5.0

# ACA sends SIGTERM with roughly a 30-second grace period before SIGKILL.
_SHUTDOWN_GRACE_S: float = 25.0

# Task modules are imported for their DECORATION side effect: `@broker.task` registers an
# executor, and a task module that is never imported is a queue whose messages are enqueued and
# never consumed. Each module keeps its own heavy imports inside the task body, after the flag
# gate, so listing one here costs an import of structlog and the broker and nothing else.
_TASK_MODULES: tuple[str, ...] = (
    "src.workers.conversation_retention",
    "src.workers.deploy_reconcile",
    "src.workers.reclamation",
    "src.workers.sandbox_reap",
)


def _import_task_modules() -> None:
    import importlib

    for module in _TASK_MODULES:
        importlib.import_module(module)
        _log.info("taskiq_task_module_registered", module=module)


async def _run_receiver_forever() -> None:
    """Run the receiver, restarting it with backoff if it ever returns or raises."""
    while True:
        try:
            await run_receiver_task(
                broker,
                run_startup=False,  # the broker is started exactly once, below
                # Acknowledge on RECEIPT, never after execution. These tasks delete Azure
                # resources: at-least-once delivery buys nothing (a lost pass is re-driven by the
                # next cron tick) and costs a concurrent second reconciler. taskiq-redis has no
                # delivery-count cap and no dead-letter, so a message that crashes the worker
                # would otherwise be an unbounded destructive-retry loop.
                ack_time=AcknowledgeType.WHEN_RECEIVED,
            )
            _log.error("taskiq_receiver_returned", detail="receiver exited; restarting")
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("taskiq_receiver_crashed")
        await asyncio.sleep(_RECEIVER_RESTART_BACKOFF_S)


async def _run_scheduler_forever() -> None:
    """Drive the scheduler loop directly rather than through `taskiq.api.run_scheduler_task`.

    `skip_first_run=True` is MANDATORY (the API helper hides it): cron last-run state is an
    in-memory dict, never persisted, so a restart's first tick fires on a 1-in-5 chance at this
    five-minute cadence — a spurious destructive run at deploy time, exactly the wrong failure.

    `SchedulerLoop(scheduler).run(...)`, never `scheduler.startup()`, which would call
    `broker.startup()` a second time and re-fire the worker-startup event."""
    while True:
        try:
            await SchedulerLoop(scheduler).run(skip_first_run=True)
            _log.error("taskiq_scheduler_returned", detail="scheduler loop exited; restarting")
        except asyncio.CancelledError:
            raise
        except Exception:
            _log.exception("taskiq_scheduler_crashed")
        await asyncio.sleep(_RECEIVER_RESTART_BACKOFF_S)


def _on_task_done(task: asyncio.Task[None]) -> None:
    """Make a dead supervisor visible. Without this, a task that raises out of `create_task` is
    swallowed and the worker keeps running while doing nothing."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        _log.error("taskiq_worker_task_died", task=task.get_name(), error=repr(exc))


async def startup() -> None:
    """Register task modules and bring the broker up — EXACTLY ONE `broker.startup()` call.

    Extracted from `main()` so a test can assert the once-only property directly."""
    _import_task_modules()

    # Mark this process as a worker BEFORE starting the broker: `AsyncBroker.startup()` branches
    # on the flag to decide whether to fire worker-startup events and to begin consuming.
    broker.is_worker_process = True

    # EXACTLY ONCE. Everything downstream (`run_receiver_task(run_startup=False)`, the scheduler
    # loop rather than `scheduler.startup()`) exists to keep this the only call.
    await broker.startup()
    for source in schedule_sources:
        await source.startup()

    _log.info(
        "taskiq_worker_started",
        broker=type(broker).__name__,
        task_modules=list(_TASK_MODULES),
        detail="one process: receiver + scheduler loop",
    )


async def main() -> None:
    await startup()

    receiver = asyncio.create_task(_run_receiver_forever(), name="taskiq-receiver")
    clock = asyncio.create_task(_run_scheduler_forever(), name="taskiq-scheduler")
    for task in (receiver, clock):
        task.add_done_callback(_on_task_done)

    stopping = asyncio.Event()

    def _request_stop(signame: str) -> None:
        _log.info("taskiq_worker_signal", signal=signame)
        stopping.set()

    loop = asyncio.get_running_loop()
    for signame in ("SIGTERM", "SIGINT"):
        with contextlib.suppress(NotImplementedError):  # not available on Windows
            loop.add_signal_handler(getattr(signal, signame), _request_stop, signame)

    await stopping.wait()

    # Stop the clock first so nothing new is enqueued, then let the receiver drain within the
    # grace window. The scheduler is outside taskiq's own drain and would keep enqueuing until
    # cancelled; those messages would execute late on the successor replica, which is harmless
    # only because every scheduled pass is idempotent.
    clock.cancel()
    with contextlib.suppress(asyncio.CancelledError):
        await clock

    receiver.cancel()
    with contextlib.suppress(asyncio.CancelledError, TimeoutError):
        async with asyncio.timeout(_SHUTDOWN_GRACE_S):
            await receiver

    await broker.shutdown()
    _log.info("taskiq_worker_stopped")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:  # pragma: no cover - interactive only
        sys.exit(0)
