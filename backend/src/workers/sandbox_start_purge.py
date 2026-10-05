"""The hourly purge of sandbox start rows older than ninety days.

A row names who started which app and when, so it is kept only as long as its timings are worth
reading. The delete is by age alone, so a second scheduler during a deploy, or a tick missed
across a restart, costs nothing: the next pass deletes whatever is then past the line.
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Final, cast

import structlog

from src.broker import broker

_log = structlog.get_logger()

SANDBOX_START_PURGE_TASK_NAME: Final = "sandbox_start_purge"
SANDBOX_START_PURGE_SCHEDULE_ID: Final = "sandbox-start-purge-hourly"
SANDBOX_START_PURGE_CRON: Final = "41 * * * *"


@broker.task(
    task_name=SANDBOX_START_PURGE_TASK_NAME,
    schedule=[{"cron": SANDBOX_START_PURGE_CRON, "schedule_id": SANDBOX_START_PURGE_SCHEDULE_ID}],
)
async def purge_old_sandbox_starts() -> None:
    """Delete every start row whose start is past the retention window."""
    import sqlalchemy as sa

    from src.db.base import async_session_factory
    from src.db.models.sandbox_start import SANDBOX_START_RETENTION, SandboxStart

    cutoff = dt.datetime.now(dt.UTC) - SANDBOX_START_RETENTION
    async with async_session_factory() as db:
        deleted = cast(
            "sa.CursorResult[Any]",
            await db.execute(sa.delete(SandboxStart).where(SandboxStart.started_at < cutoff)),
        )
        await db.commit()
    _log.info("sandbox_start_purge_pass_completed", removed=deleted.rowcount)
