"""The durable record of a reap's copy-before-destroy attempt.

WHY A WRITER LIVES HERE: the reaper's write-back can fail invisibly — a container whose tree
cannot be written back is SPARED, indistinguishable from an empty fleet, and bills forever
silently. So this writes a `worker_passes` row on every outcome, the same protection every
scheduled pass in this system gives itself: it's the only thing an operator can look for that
does not depend on the failing component to speak up."""

from __future__ import annotations

import datetime as dt
import enum
from typing import Final

import structlog

from src.db.models.worker_pass import PassOutcome, WorkerPass

_log = structlog.get_logger()

#: The `task_name` a copy-before-destroy row carries. Its own name, not any scheduled pass's —
#: filing a per-container copy attempt under a pass's own task name would blur "did the pass
#: run" together with "did this one container's copy work", two different questions an operator
#: needs to ask separately.
DURABLE_COPY_TASK_NAME: Final = "sandbox_durable_copy"


class CopyAttempt(enum.StrEnum):
    """What one reap's attempt to secure a container's work before destroying it came to.

    Several outcomes rather than a bare success/failure pair, because the sparing arms fail for
    reasons an operator has to act on DIFFERENTLY: an unreachable container needs somebody to
    look at the container, and a raised write needs somebody to look at the store. Collapsing
    them would produce a row that says a container was spared and nothing about what to do next.
    """

    #: Nothing was written back, and nothing could or needed to be: the container still held the
    #: untouched starter template; or it could not be attached at all but a saved bundle already
    #: stands for this app; or its workspace had lost its repository, which no write-back can
    #: save. Recorded for the same reason a zero-candidate pass is: a quiet fleet and a dead
    #: process are otherwise one observation.
    NOTHING_TO_COPY = "nothing_to_copy"
    #: The tree landed in the saved copy, and the container may go.
    COPIED = "copied"
    #: There was nothing to copy FROM and no saved bundle to stand in. The container would not
    #: attach, or the record no longer names the container we are judging — in which case the tree
    #: we could reach belongs to somebody else's build and must never be bundled into this app's
    #: slot.
    UNREACHABLE = "unreachable"
    #: The bundle, the read-back or the upload itself raised. Nothing was established, so nothing
    #: is destroyed.
    FAILED = "failed"


#: How each outcome reads to the operator endpoint: the native enum it stores under, and the one
#: sentence the `detail` column carries. Kept as a table rather than as branches at the write, so
#: adding an outcome cannot ship a row with no explanation in it.
#:
#: NOT ONE OF THESE SENTENCES NAMES A CONTAINER OR AN APP, and that is the `WorkerPass.detail`
#: contract rather than an oversight: the identity belongs in the structlog line the reaper emits
#: beside the row, never in a column an admin endpoint hands out. What the row is for is "this is
#: happening, it is not getting better, and here is which half to look at".
_ATTEMPT_MEANING: Final[dict[CopyAttempt, tuple[PassOutcome, str]]] = {
    CopyAttempt.NOTHING_TO_COPY: (
        PassOutcome.OK,
        "there was nothing to write back before reclaiming, or the workspace had lost its "
        "repository",
    ),
    CopyAttempt.COPIED: (
        PassOutcome.OK,
        "the tree was written back before the container was reclaimed",
    ),
    CopyAttempt.UNREACHABLE: (
        PassOutcome.DECLINED,
        "the container we judged could not be reached, so nothing could be written back; spared",
    ),
    CopyAttempt.FAILED: (
        PassOutcome.FAILED,
        "the write-back raised; see the traceback on the reaper's log line. Spared",
    ),
}

#: The arms that leave a container standing. Read once here rather than re-derived at the write,
#: because "which outcomes spared something" is the only question this row is ever asked.
_SPARED: Final = frozenset({CopyAttempt.UNREACHABLE, CopyAttempt.FAILED})


async def record_durable_copy_attempt(attempt: CopyAttempt) -> None:
    """Write the row for ONE container's copy-before-reclaim attempt. Never raises.

    ON EVERY OUTCOME, including "already current" — else empty rows can't be told from a worker
    that never ran (same inference `_record_pass` protects, one level down).
    Imports its session factory INSIDE the function — `tests/conftest.py` rebinds it onto a
    per-test engine, and a module-level import would hand tests a connection from the wrong loop.
    NEVER FAILS THE REAP: logged, not swallowed — a silent write failure would make a working
    reaper look stopped."""
    from src.db.base import async_session_factory

    try:
        # INSIDE the try, so the docstring's "never raises" is literally true. An
        # unmapped member is a bug, but a bug that aborts a REAP is worse than one that loses
        # a row — and this sits on a destroy path where an escaping exception ends the whole
        # user's sweep.
        outcome, detail = _ATTEMPT_MEANING[attempt]
        async with async_session_factory() as db:
            db.add(
                WorkerPass(
                    task_name=DURABLE_COPY_TASK_NAME,
                    outcome=outcome,
                    finished_at=dt.datetime.now(dt.UTC),
                    counts={
                        "copied": 1 if attempt is CopyAttempt.COPIED else 0,
                        "spared": 1 if attempt in _SPARED else 0,
                    },
                    detail=detail,
                )
            )
            await db.commit()
    except Exception:
        _log.exception("durable_copy_attempt_record_failed", attempt=attempt.value)
