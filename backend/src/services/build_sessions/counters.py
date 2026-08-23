"""Writing the counters down (U25, R32).

ONE FUNCTION, AND IT NEVER RAISES. Every call site is on a path that is doing something else —
finishing a turn, refusing a claim, restoring a workspace — and a counter that can fail the thing
it is counting is worse than no counter at all. That is not a hypothetical: this whole plan exists
because a platform lied about an app, and a metric that turns into a second incident is exactly
the wrong lesson to draw from it.

IT OWNS ITS OWN SESSION. Callers hold a session scoped to work that may still roll back; a count
is a historical fact about something that HAPPENED and must not disappear because the surrounding
transaction did. Same reasoning `workers/reclamation.py::_record_pass` uses for pass history.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

import structlog

from src.db.base import async_session_factory
from src.db.models.harness_counter import HarnessCount, HarnessCounter

_log = structlog.get_logger()


async def count(
    counter: HarnessCounter | str,
    *,
    value: int = 1,
    app_id: uuid.UUID | None = None,
    build_id: uuid.UUID | None = None,
    served_head: str | None = None,
) -> None:
    """Record one counted outcome. Swallows everything.

    `counter` takes a bare string as readily as a member, and that is the point of the open
    vocabulary: the companion plan's tool-boundary counters need no change here and no migration
    to become readable."""
    name = counter.value if isinstance(counter, HarnessCounter) else counter
    try:
        async with async_session_factory() as db:
            db.add(
                HarnessCount(
                    name=name,
                    value=value,
                    app_id=app_id,
                    build_id=build_id,
                    occurred_at=datetime.now(UTC),
                    served_head=served_head,
                )
            )
            await db.commit()
    except Exception:  # noqa: BLE001 - see the module docstring: a counter must never raise
        # BROAD ON PURPOSE, and it is the one place in this codebase where that is right. The
        # narrow alternative is a list of database and driver exceptions that has to stay
        # complete; get it wrong once and a counter takes down a turn. `.claude/rules/fail-first`
        # forbids swallowing an error the caller could act on — the caller here cannot act on
        # this one, and the log line is what an operator acts on instead.
        _log.warning("harness_counter_not_recorded", counter=name, exc_info=True)
