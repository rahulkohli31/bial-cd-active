"""The worker process: `python -m src.worker_main` (ADR-0011 §1-§2).

ONE process runs BOTH taskiq roles — the receiver that executes tasks and the scheduler loop
that enqueues cron ticks — as two supervised asyncio tasks. A scheduler without a receiver would
enqueue work nothing consumes, behind a container that looks healthy, so the receiver is the
mandatory role and the scheduler rides along.

WHY THIS EXISTS INSTEAD OF `taskiq worker` / `taskiq scheduler`
---------------------------------------------------------------
Both CLI-based designs were tried on paper and both are broken:

1. **`taskiq worker` + a `WORKER_STARTUP` handler that starts the scheduler is fatally
   recursive.** `run_scheduler` calls `TaskiqScheduler.startup()`, which calls
   `broker.startup()`, which fires `WORKER_STARTUP` when `is_worker_process` — so the handler
   re-fires the thing that spawned it, without bound. It also reconfigures the root logger and,
   on cancellation, shuts down the co-resident receiver's connection pool.

2. **`taskiq worker` cannot give a single-scheduler guarantee anyway.** Its worker-count argument
   defaults to **2**, so the CLI forks two children, each firing `WORKER_STARTUP`. An earlier
   plan draft relied on `minReplicas = maxReplicas = 1` for that guarantee; the replica pin says
   nothing about child processes inside one replica.

Owning the entrypoint makes "exactly one scheduler" true BY CONSTRUCTION. One interpreter also
halves the memory the container is sized against, and a fatal error exits the process so ACA
restarts the container visibly, rather than a child crash-looping inside a container that never
exits.

WHAT THE REPLICA PIN DOES AND DOES NOT BUY. Even with one scheduler per process and one replica,
ACA drains the old revision while the new one starts, so **two schedulers exist during every
deploy window**. Taskiq has no leader election of any kind. The pin is a defence, never an
exclusivity guarantee — which is why every scheduled pass must be idempotent and single-flighted
in its own right (ADR-0029 §9).
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
from src.scheduler import schedule_sources, scheduler

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
#
# The first passenger is deploy reconciliation (U6), chosen because it CANNOT destroy anything:
# it settles a database row against a read-only view of ARM. Everything else bound for this
# scheduler can delete an Azure resource, and none of that may run out-of-process until the R10
# liveness lease lands (U12).
_TASK_MODULES: tuple[str, ...] = (
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
    """Run the receiver, restarting it with backoff if it ever returns or raises.

    Two library behaviours make this wrapper load-bearing rather than defensive. The receiver's
    internal loop catches broad exceptions with no backoff, so a broken Redis spins hot; and a
    bare `create_task` failure is silent, which would leave a worker that consumes nothing
    forever while its container reports healthy.
    """
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

    `skip_first_run=True` is MANDATORY and the API helper does not expose it. Cron last-run state
    is an in-memory dict that is never persisted, so on restart the first iteration evaluates the
    cron against the current minute and fires if it matches — at a five-minute cadence that is a
    1-in-5 chance per restart, i.e. most revision rolls. For a destructive pass, a spurious extra
    run at deploy time is exactly the wrong failure.

    `SchedulerLoop(scheduler).run(...)`, never `scheduler.startup()`: startup would call
    `broker.startup()` a second time and re-fire the worker-startup event.
    """
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

    Extracted from `main()` so a test can assert the once-only property directly. That property
    is the whole reason this entrypoint exists rather than the taskiq CLI: `broker.startup()`
    fires the worker-startup event, and any design where a startup handler re-enters it (a
    `WORKER_STARTUP` hook that calls `run_scheduler`, or `scheduler.startup()`) recurses without
    bound.
    """
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
