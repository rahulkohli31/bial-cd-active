"""The seam between a build starting and a window's files reaching Redis.

ONE FUNCTION THE BUILD PATH CALLS, and it answers the whole question for itself: is a lake
configured, is this connector switched on for this project, has its owner been approved, which
dates does it read, which files are those, and are they already held. Every one of those has an
answer meaning "do nothing", and every one of them returns quietly.

IT OPENS ITS OWN SESSION AND OWNS ITS OWN LIFETIME. It runs as a detached task fired after a
container is born, so by the time it reads the database the request that started it may be long
finished — borrowing that request's session would use it after `get_db` had already rolled it
back. Same rule the deploy pipeline follows.

IT NEVER BLOCKS A BUILD AND NEVER FAILS ONE. Nothing on this platform reads what it writes (the
generated app reads the lake directly, with its own identity), so a citizen must never wait on it
and must never lose a build to it. Every failure is logged and swallowed — the one deliberate
exception to this tree's "configured and broken always raises" rule, stated here and in
`transfer.py` so it reads as a decision rather than a missing `raise`.

WHY IT FIRES ON A BIRTH AND NOT ON EVERY TURN. A container gets its environment exactly once, at
birth; the attach arm is the steady state and forwards none. Firing here on every turn would list
the lake once per message for a copy nobody reads.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Final

import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.connectors import CONNECTORS, ResolvedWindow, resolve_window
from src.db.models.project import Project
from src.db.models.project_connector import ProjectConnector
from src.services.connectors.access import current_access
from src.services.lake.client import get_lake
from src.services.lake.transfer import TransferReport, transfer_window_or_log
from src.services.lake.window import select_files
from src.services.redis.client import RedisNotConfiguredError, get_redis_bytes

_log = structlog.get_logger()

# Every detached copy in flight, held so the event loop cannot garbage-collect a running task —
# `asyncio.create_task` keeps only a weak reference. Same shape the deploy service uses.
_in_flight: Final[set[asyncio.Task[None]]] = set()


async def _window_for(
    db: AsyncSession, *, user_id: uuid.UUID, project_id: uuid.UUID, connector_key: str
) -> ProjectConnector | None:
    """This project's stored connector row, or `None` when it was never switched on.

    Scoped by the owning `user_id` through a join on `projects` in the SAME `WHERE` clause —
    `project_connectors` carries no user column of its own, so `projects` is its ownership anchor
    and the predicate IS the isolation boundary."""
    row: ProjectConnector | None = await db.scalar(
        sa.select(ProjectConnector)
        .join(Project, Project.id == ProjectConnector.project_id)
        .where(
            ProjectConnector.project_id == project_id,
            ProjectConnector.connector_key == connector_key,
            Project.user_id == user_id,
        )
    )
    return row


async def plan_window_copies(
    db: AsyncSession, *, user_id: uuid.UUID, project_id: uuid.UUID
) -> tuple[tuple[str, ResolvedWindow], ...]:
    """Which connectors this project reads right now, and the dates each one reads.

    THE DATABASE HALF, HELD APART FROM THE NETWORK HALF ON PURPOSE — this is the whole reason
    the function exists rather than being three lines inside the loop below. Everything here is
    a `SELECT` and finishes in milliseconds. Everything in `run_window_copies` is a container
    listing plus up to a full window of blob downloads, and the measured figure for one window
    is ~6.7 s from outside the region. A pooled connection held across that second half is
    indistinguishable from a busy one, and the control plane's pool is twenty wide: a background
    copy nobody reads would be starving the request path that citizens are waiting on. The
    deploy pipeline splits at exactly this seam and for exactly this reason.

    Returns `()` for every "do nothing" answer, so a caller has one shape to handle."""
    if get_lake() is None:
        return ()  # no lake configured — the supported dev/test posture
    plans: list[tuple[str, ResolvedWindow]] = []
    for connector_key, connector in CONNECTORS.items():
        access = await current_access(db, user_id=user_id, connector_key=connector_key)
        stored = await _window_for(
            db, user_id=user_id, project_id=project_id, connector_key=connector_key
        )
        window = resolve_window(connector, stored, access.request_status)
        # `effectively_on` IS the conjunction (the switch AND the approval), read off the resolver
        # rather than spelled again here. A second place that decides whether a connector reads is
        # a second place that can disagree with the rail the citizen is looking at.
        if window is None or not window.effectively_on:
            continue
        plans.append((connector_key, window))
    return tuple(plans)


async def run_window_copies(
    plans: tuple[tuple[str, ResolvedWindow], ...], *, project_id: uuid.UUID
) -> TransferReport | None:
    """Copy each planned window into Redis. Never raises. TOUCHES NO DATABASE — see above.

    Returns the LAST connector's report, or `None` when nothing was copied — for logs and tests.
    There is one connector in the registry today; iterating is what keeps a second one from
    needing a change here."""
    if not plans:
        return None
    lake = get_lake()
    if lake is None:
        return None  # no lake configured — the supported dev/test posture
    try:
        redis = get_redis_bytes()
    except RedisNotConfiguredError:
        return None  # no Redis configured — likewise; a build still provisions

    report: TransferReport | None = None
    for connector_key, window in plans:
        connector = CONNECTORS[connector_key]
        listing = await lake.list_files()
        selection = select_files(listing, window, max_files=connector.max_window_days)
        report = await transfer_window_or_log(lake, redis, selection)
        if report is not None:
            _log.info(
                "lake_window_copied",
                connector=connector_key,
                project_id=str(project_id),
                copied=report.copied,
                bytes_copied=report.bytes_copied,
                already_held=report.already_held,
                # The days inside the window the upstream load left as zero-byte stubs. "The lake
                # was quiet" and "the lake was broken" are different sentences, and with no
                # top-up this number is the only trace of the second.
                unreadable_days=report.skipped_stubs,
                # Files the lake refused or could not serve on this attempt — a different
                # sentence from "the upstream load wrote a zero-byte file", and worth its own
                # number because one of the two means the platform's own access has changed.
                unreadable_files=report.unreadable,
                # Should always be zero. A non-zero value means the lake grew a second file for
                # a date — an `archive/` or `backup/` folder under the prefix — and the ceiling
                # that assumes one file per date has stopped meaning what it says.
                duplicate_files=selection.duplicates,
                evicted=report.evicted,
                # Files the window selected and the budget refused. Without it a workspace that
                # is quietly missing its oldest days looks identical in the log to one that got
                # everything, and the ceiling is the difference.
                too_large_to_hold=report.too_large_to_hold,
            )
    return report


async def copy_window_for_project(
    db: AsyncSession, *, user_id: uuid.UUID, project_id: uuid.UUID
) -> TransferReport | None:
    """Both halves, in order, against a session the caller owns. Never raises.

    THE HALVES COMPOSED, NOT A THIRD IMPLEMENTATION. Production does not call this — it calls
    `_copy_in_its_own_session`, which runs the two halves with the session CLOSED in between.
    This exists so the whole decision can be asserted end to end against one session."""
    plans = await plan_window_copies(db, user_id=user_id, project_id=project_id)
    return await run_window_copies(plans, project_id=project_id)


async def _copy_in_its_own_session(user_id: uuid.UUID, project_id: uuid.UUID) -> None:
    from src.db.base import async_session_factory

    try:
        async with async_session_factory() as session:
            plans = await plan_window_copies(session, user_id=user_id, project_id=project_id)
        # THE SESSION IS CLOSED BEFORE A SINGLE BYTE IS DOWNLOADED, and the dedent above is the
        # entire point — see `plan_window_copies` for why holding it across the transfer would
        # starve the request path. Nothing below reads the database, so nothing below needs it.
        await run_window_copies(plans, project_id=project_id)
    except Exception:
        # The outermost catch on a detached task. `transfer_window_or_log` already swallows the
        # lake's and Redis's own failures; this covers the rest — a database blip, a listing that
        # raised — and it is broad on purpose: an exception escaping here would surface as an
        # un-retrieved task exception at garbage-collection time, attributed to nothing.
        _log.exception("lake_window_copy_failed", user_id=str(user_id), project_id=str(project_id))


def schedule_window_copy(user_id: uuid.UUID, project_id: uuid.UUID) -> None:
    """Fire the copy and return immediately. Never raises, never awaits, never blocks a build.

    Called from the sandbox BIRTH arms only — see the module docblock for why not on attach."""
    task = asyncio.create_task(
        _copy_in_its_own_session(user_id, project_id), name="lake-window-copy"
    )
    _in_flight.add(task)
    task.add_done_callback(_in_flight.discard)
