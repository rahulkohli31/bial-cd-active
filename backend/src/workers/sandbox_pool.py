"""The pool of ready sandboxes, held at its size every minute (`build_sessions/pool_pass.py`).

Unlike the sweep it runs in every environment: it deletes only containers the pool's own ledger
holds, never one a registry names. Each tick logs one line, at a size of zero too, so a silent
minute means the pass is not running. Each pass takes an advisory lock, so the two schedulers of
a deploy run one pass between them.
"""

from __future__ import annotations

from typing import Final

from src.broker import broker

SANDBOX_POOL_TASK_NAME: Final = "sandbox_pool"
SANDBOX_POOL_SCHEDULE_ID: Final = "sandbox-pool-every-minute"
SANDBOX_POOL_CRON: Final = "* * * * *"


@broker.task(
    task_name=SANDBOX_POOL_TASK_NAME,
    schedule=[{"cron": SANDBOX_POOL_CRON, "schedule_id": SANDBOX_POOL_SCHEDULE_ID}],
)
async def keep_the_pool_at_its_size() -> None:
    """One pass over the pool, or one line saying why there was none."""
    from src.services.build_sessions.pool_pass import run_pool_pass

    await run_pool_pass()
