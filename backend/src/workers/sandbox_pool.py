"""The pool of ready sandboxes, held at its size every minute (`services/sandbox/pool.py`).

Unlike the sweep it runs in every environment: it deletes only containers the pool's own ledger
holds, never one a registry names. Each tick logs one line, at a size of zero too, so a silent
minute means the pass is not running. It shares an advisory lock with the backend's startup pass,
so the two schedulers of a deploy, or a backend starting in the same minute, run one pass.
"""

from __future__ import annotations

from typing import Final

import structlog

from src.broker import broker
from src.config import settings

_log = structlog.get_logger()

SANDBOX_POOL_TASK_NAME: Final = "sandbox_pool"
SANDBOX_POOL_SCHEDULE_ID: Final = "sandbox-pool-every-minute"
SANDBOX_POOL_CRON: Final = "* * * * *"


@broker.task(
    task_name=SANDBOX_POOL_TASK_NAME,
    schedule=[{"cron": SANDBOX_POOL_CRON, "schedule_id": SANDBOX_POOL_SCHEDULE_ID}],
)
async def keep_the_pool_at_its_size() -> None:
    """One pass over the pool, or one line saying why there was none."""
    from src.services.sandbox import get_sandbox
    from src.services.sandbox.pool import PoolKeeper, keep_the_pool_under_the_lock

    config = settings.sandbox
    if config is None:
        _log.info("sandbox_pool_pass_disabled", reason="unconfigured")
        return
    sandbox = get_sandbox()
    if not isinstance(sandbox, PoolKeeper):
        _log.info("sandbox_pool_pass_disabled", reason="no_pool_client")
        return
    await keep_the_pool_under_the_lock(sandbox, config)
