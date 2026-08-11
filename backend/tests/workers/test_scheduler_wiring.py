"""The worker entrypoint's structural guarantees (U4, ADR-0011 §2).

`test_the_worker_starts_the_broker_exactly_once` is the most important test in this unit. Two
CLI-based designs for this worker were considered and BOTH were fatally recursive or unsafe:

1. `taskiq worker` plus a `WORKER_STARTUP` handler that starts the scheduler — `run_scheduler`
   calls `TaskiqScheduler.startup()`, which calls `broker.startup()`, which fires
   `WORKER_STARTUP` when `is_worker_process`. The handler re-fires the thing that spawned it,
   without bound.
2. `taskiq worker` for single-scheduler safety — its worker-count argument defaults to **2**, so
   the CLI forks two children and each fires `WORKER_STARTUP`. An earlier plan draft relied on
   `minReplicas = maxReplicas = 1` for that guarantee; a replica pin says nothing about child
   processes inside one replica.

Owning the entrypoint makes "one broker startup, one scheduler" true by construction. This file
is what stops a future refactor from quietly reintroducing either shape.
"""

from __future__ import annotations

import os
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path

from taskiq import TaskiqEvents
from taskiq.schedule_sources import LabelScheduleSource

import src.worker_main as worker_main
from src.broker import broker
from src.scheduler import schedule_sources, scheduler

_BACKEND_ROOT = Path(__file__).resolve().parent.parent.parent


def _code_without_docstring(func: Callable[..., object]) -> str:
    """The function's executable source, with its docstring removed.

    Necessary because these docstrings deliberately NAME the calls they forbid, in order to
    explain why they are forbidden — so a naive substring search over `inspect.getsource` finds
    the explanation and reports a violation that does not exist. Parsed rather than string-
    stripped: `inspect.getdoc` normalizes indentation, so it does not match the raw source text.
    """
    import ast
    import inspect
    import textwrap

    tree = ast.parse(textwrap.dedent(inspect.getsource(func)))
    definition = tree.body[0]
    assert isinstance(definition, (ast.FunctionDef, ast.AsyncFunctionDef))
    statements = definition.body
    if (
        statements
        and isinstance(statements[0], ast.Expr)
        and isinstance(statements[0].value, ast.Constant)
    ):
        statements = statements[1:]
    return "\n".join(ast.unparse(node) for node in statements)


async def test_the_worker_starts_the_broker_exactly_once() -> None:
    """A counting worker-startup handler must fire exactly once.

    MUTATION CHECK (performed by hand when U4 landed): adding `await scheduler.startup()` to
    `worker_main.startup()` makes this count climb without bound, because that call re-enters
    `broker.startup()` and re-fires the event. Reinstating it must turn this test red.
    """
    calls: list[str] = []

    @broker.on_event(TaskiqEvents.WORKER_STARTUP)
    async def _count(_: object) -> None:
        calls.append("startup")

    try:
        await worker_main.startup()
    finally:
        broker.is_worker_process = False

    assert calls == ["startup"], (
        f"broker startup fired {len(calls)} times — a startup handler is re-entering "
        f"broker.startup(); that recursion is unbounded in production"
    )


def test_exactly_one_scheduler_source_is_configured() -> None:
    """One clock. `LabelScheduleSource` creates ZERO Redis keys, which is why it is used rather
    than `RedisScheduleSource` — that adds a `schedule:*` key family and issues a multi-key read
    that breaks under an OSS clustering policy."""
    assert len(schedule_sources) == 1
    assert isinstance(schedule_sources[0], LabelScheduleSource)
    assert list(scheduler.sources) == list(schedule_sources)


def test_the_scheduler_loop_is_driven_with_first_run_skipped() -> None:
    """`skip_first_run=True` is MANDATORY and `taskiq.api.run_scheduler_task` does not expose it,
    which is why the loop is driven directly.

    Cron last-run state is an in-memory dict that is never persisted, so on restart the first
    iteration evaluates the cron against the CURRENT minute and fires if it matches — at a
    five-minute cadence that is a 1-in-5 chance per restart, i.e. most revision rolls. For a
    destructive pass, a spurious extra run at deploy time is exactly the wrong failure mode.

    Asserted on the source text because the flag is passed to a library call this test cannot
    observe without running the loop; the string is short and unambiguous.
    """
    body = _code_without_docstring(worker_main._run_scheduler_forever)

    assert "skip_first_run=True" in body, "the scheduler must not fire on the restart minute"
    assert "scheduler.startup()" not in body, (
        "scheduler.startup() re-enters broker.startup() and re-fires WORKER_STARTUP — drive "
        "SchedulerLoop directly instead"
    )


def test_the_receiver_never_redelivers_a_destructive_message() -> None:
    """`ack_time=WHEN_RECEIVED` acknowledges BEFORE execution — the explicit never-redeliver
    setting. taskiq-redis has no delivery-count cap and no dead-letter, so a message that crashes
    the worker would otherwise be an unbounded destructive-retry loop. A lost pass costs nothing:
    the next cron tick re-drives it five minutes later."""
    body = _code_without_docstring(worker_main._run_receiver_forever)
    assert "AcknowledgeType.WHEN_RECEIVED" in body
    assert "run_startup=False" in body, (
        "the receiver must not start the broker — worker_main.startup() already did, exactly once"
    )


def test_the_entrypoint_builds_no_web_app_and_loads_no_model_client() -> None:
    """The worker must not pay for the things it will NEVER use.

    Scoped deliberately, and the scope was set by measurement rather than aspiration. Importing
    the entrypoint does pull in SQLAlchemy and `azure.core`, because `src.config` reads the
    capability configs and both `services/appdb/__init__.py` and `services/sandbox/__init__.py`
    re-export modules that import them. That is pre-existing (the old `src/config.py` imported
    the same two config modules) and it is not worth restructuring: the worker needs the ORM for
    the app table and the advisory lock, and the ARM SDK to enumerate and delete — so the first
    pass loads both within seconds regardless.

    What must NEVER appear is the other kind of weight: `src.main` (the whole route tree, built
    for a process that serves no requests) and `pydantic_ai` (the model client, in a process that
    runs no model). Those are the ones a careless import would add silently.
    """
    result = subprocess.run(  # noqa: S603
        [
            sys.executable,
            "-B",
            "-c",
            "import sys, src.worker_main;"
            " never = [m for m in ('src.main', 'pydantic_ai', 'uvicorn') if m in sys.modules];"
            " print('NEVER:' + ','.join(never))",
        ],
        cwd=_BACKEND_ROOT,
        env={"PATH": os.environ["PATH"], "ENV_FILE": ".env.test"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, f"stdout: {result.stdout}\nstderr: {result.stderr}"
    assert "NEVER:" in result.stdout, result.stdout
    pulled_in = result.stdout.split("NEVER:")[1].strip()
    assert pulled_in == "", (
        f"the worker entrypoint pulled in {pulled_in} — a process that serves no requests and "
        f"runs no model must not import the app or the model client."
    )


def test_every_registered_task_module_is_importable() -> None:
    """Taskiq imports only the broker module. A task module that is never imported is never
    registered, so its messages are enqueued and silently never consumed — the failure looks
    exactly like a dead worker. Deploy reconciliation boarded as the first passenger in U6; this
    walks whatever is registered, so every later passenger is covered without an edit.
    """
    import importlib

    for module in worker_main._TASK_MODULES:
        importlib.import_module(module)
