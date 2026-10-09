"""Row operations on `classification_reviews` — the claim-or-return, and the terminal write.

WHY THIS EXISTS
This claim differs from the deploy store's: that one serializes ATTEMPTS as append-only
rows behind a partial index; this one settles which single row an app carries. A review's
version is the pair (commit, class-definition fingerprint). Three outcomes, resolved in
Postgres so a restart or a concurrent dialog cannot double-run:

* no row, or a different pair → replaced wholesale, attempt reset to 1;
* a FAILED row, same pair → re-claimed, attempt incremented (the dialog can ask again
  without re-saving; the three-runs-per-pair ceiling is service policy, enforced in
  exactly one place);
* a RUNNING or COMPLETE row, same pair → returned untouched, `claimed=False` (an
  unchanged pair returns the stored answers without re-running; a live run is never
  doubled).

Terminal writes are guarded on the pair AND the attempt, not just `running`: unlike
`deploy/store._finish`, this row SURVIVES a takeover (a newer claim rewrites it in
place, possibly at the same commit and attempt 1), so a stale commit, fingerprint or
attempt is what makes a zombie runner's late write touch zero rows instead of overwriting
a newer claim's verdicts.

Every write commits on its own — the runner is a detached task with its own session.
Every function returns plain scalars or a frozen dataclass, never a live ORM instance
across the commit boundary, to avoid a MissingGreenlet after commit.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Final

import sqlalchemy as sa
import structlog
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.engine import Row
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models.classification_review import ClassificationReview, ClassificationReviewStatus

_log = structlog.get_logger()

# Every column a `ReviewRecord` carries, in the record's field order. One tuple so the
# claim's three RETURNING clauses and the read cannot drift apart.
_RECORD_COLUMNS: Final = (
    ClassificationReview.id,
    ClassificationReview.app_id,
    ClassificationReview.user_id,
    ClassificationReview.head_sha,
    ClassificationReview.definitions_fingerprint,
    ClassificationReview.status,
    ClassificationReview.attempt,
    ClassificationReview.verdicts,
    ClassificationReview.evidence,
    ClassificationReview.answers_complete,
    ClassificationReview.failure_code,
    ClassificationReview.failure_detail,
    ClassificationReview.started_at,
    ClassificationReview.finished_at,
    ClassificationReview.input_tokens,
    ClassificationReview.output_tokens,
    ClassificationReview.cache_read_tokens,
    ClassificationReview.cache_write_tokens,
)


@dataclass(frozen=True)
class ReviewRecord:
    """A committed row, frozen. Safe to hold across any await — it is plain data, not a
    session-bound instance that lazy-loads (and MissingGreenlets) after the commit."""

    review_id: uuid.UUID
    app_id: uuid.UUID
    user_id: uuid.UUID
    head_sha: str
    definitions_fingerprint: str | None
    status: ClassificationReviewStatus
    attempt: int
    verdicts: dict[str, Any] | None
    evidence: dict[str, Any] | None
    answers_complete: bool | None
    failure_code: str | None
    failure_detail: str | None
    started_at: datetime
    finished_at: datetime | None
    input_tokens: int
    output_tokens: int
    cache_read_tokens: int
    cache_write_tokens: int


@dataclass(frozen=True)
class ClaimOutcome:
    """What a claim resolved to. `claimed=True` means THIS caller now owns a run and must
    perform it (the record is the fresh running row, `attempt` telling it which run it
    is); `claimed=False` means the stored record IS the answer — complete for this
    version, or a run already in flight that must not be doubled."""

    claimed: bool
    review: ReviewRecord


def _record(row: Row[*tuple[Any, ...]]) -> ReviewRecord:
    """One committed row → the frozen record. Positional against `_RECORD_COLUMNS`."""
    return ReviewRecord(*row)


def _fresh_run_values(*, head_sha: str, fingerprint: str, user_id: uuid.UUID) -> dict[str, Any]:
    """Everything a (re)claimed row is reset to — the wholesale overwrite, minus
    `attempt`, which is the one column whose next value depends on WHY the claim won
    (1 on a version change, +1 on a same-version retry).

    `started_at` is renewed because the wall-clock ceiling is measured from it, and the
    verdict/failure/usage fields are cleared because the durable history lives in the
    per-run audit records, not here — a re-claimed row carrying its predecessor's
    failure text would read as the CURRENT run's state, which it is not."""
    return {
        "user_id": user_id,
        "head_sha": head_sha,
        "definitions_fingerprint": fingerprint,
        "status": ClassificationReviewStatus.RUNNING,
        "verdicts": None,
        "evidence": None,
        "answers_complete": None,
        "failure_code": None,
        "failure_detail": None,
        "started_at": sa.func.now(),
        "finished_at": None,
        "input_tokens": 0,
        "output_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
    }


async def claim(
    db: AsyncSession,
    *,
    app_id: uuid.UUID,
    user_id: uuid.UUID,
    head_sha: str,
    fingerprint: str,
) -> ClaimOutcome:
    """Claim a review run for `app_id` at (`head_sha`, `fingerprint`), or get the stored row
    back. Commits.

    Two passes at most, mirroring the deploy claim's bounded retry: the only way the
    first pass resolves to nothing is a concurrent claim replacing the row for another
    version in the gap between our writes and our read — a window one retry closes and
    an unbounded loop against a hot row would spin in."""
    for _ in range(2):
        claimed = await _try_claim(
            db, app_id=app_id, user_id=user_id, head_sha=head_sha, fingerprint=fingerprint
        )
        if claimed is not None:
            return ClaimOutcome(claimed=True, review=claimed)

        stored = await get_for_app(db, app_id=app_id)
        if stored is not None and is_for(stored, head_sha=head_sha, fingerprint=fingerprint):
            return ClaimOutcome(claimed=False, review=stored)

        _log.warning(
            "classification_review_claim_contended",
            app_id=str(app_id),
            head_sha=head_sha,
            fingerprint=fingerprint,
        )

    # Two full passes lost: either the app is being deleted under us (the CASCADE
    # removed the row between insert-conflict and read) or something is rewriting the
    # row faster than we can claim it. Refuse loudly rather than answer with a row
    # stamped a version nobody asked about — security-adjacent checks fail closed.
    raise RuntimeError(
        f"could not claim a classification review for app {app_id}: "
        "the row is being rewritten concurrently"
    )


def is_for(record: ReviewRecord, *, head_sha: str, fingerprint: str) -> bool:
    """Whether `record` is the review of this commit under these class definitions."""
    return record.head_sha == head_sha and record.definitions_fingerprint == fingerprint


async def _try_claim(
    db: AsyncSession,
    *,
    app_id: uuid.UUID,
    user_id: uuid.UUID,
    head_sha: str,
    fingerprint: str,
) -> ReviewRecord | None:
    """One pass over the three ways a claim can win. At most one statement mutates;
    all three land in a single committed transaction.

    Statement order is the common case first (no row yet), then the version change,
    then the same-version retry. Each UPDATE's predicate is what makes the pass
    race-safe: two concurrent claimants for the same transition both match at most one
    row, and the loser's predicate is falsified by the winner's write."""
    # 1. No row yet → the fresh insert wins it. `ON CONFLICT DO NOTHING` against the
    #    one-row-per-app constraint: a conflict just means the row exists and the
    #    UPDATE arms decide.
    inserted = (
        await db.execute(
            pg_insert(ClassificationReview)
            .values(
                app_id=app_id,
                user_id=user_id,
                head_sha=head_sha,
                definitions_fingerprint=fingerprint,
            )
            .on_conflict_do_nothing(index_elements=[ClassificationReview.app_id])
            .returning(*_RECORD_COLUMNS)
        )
    ).one_or_none()
    if inserted is not None:
        await db.commit()
        return _record(inserted)

    # 2. The commit or the class definitions moved → replace the row WHOLESALE, whatever its
    #    status. This is the write that retires a stale COMPLETE answer, a stale failure, AND
    #    an older pair's still-running run (its late completion is disarmed by the terminal
    #    guards below). Attempt resets: the counter belongs to the pair. A legacy row's NULL
    #    fingerprint counts as moved.
    replaced = (
        await db.execute(
            sa.update(ClassificationReview)
            .where(
                ClassificationReview.app_id == app_id,
                sa.or_(
                    ClassificationReview.head_sha != head_sha,
                    ClassificationReview.definitions_fingerprint.is_distinct_from(fingerprint),
                ),
            )
            .values(
                attempt=1,
                **_fresh_run_values(head_sha=head_sha, fingerprint=fingerprint, user_id=user_id),
            )
            .returning(*_RECORD_COLUMNS)
        )
    ).one_or_none()
    if replaced is not None:
        await db.commit()
        return _record(replaced)

    # 3. Same pair, FAILED → re-claim it (ask again without re-saving). The
    #    status predicate is the race guard — of two concurrent retries, the second
    #    finds the row RUNNING and falls through to the stored-row read.
    reclaimed = (
        await db.execute(
            sa.update(ClassificationReview)
            .where(
                ClassificationReview.app_id == app_id,
                ClassificationReview.head_sha == head_sha,
                ClassificationReview.definitions_fingerprint == fingerprint,
                ClassificationReview.status == ClassificationReviewStatus.FAILED,
            )
            .values(
                attempt=ClassificationReview.attempt + 1,
                **_fresh_run_values(head_sha=head_sha, fingerprint=fingerprint, user_id=user_id),
            )
            .returning(*_RECORD_COLUMNS)
        )
    ).one_or_none()
    await db.commit()
    if reclaimed is not None:
        return _record(reclaimed)

    # Same pair, RUNNING or COMPLETE: nothing to claim — the caller reads the row.
    return None


async def succeed(
    db: AsyncSession,
    *,
    review_id: uuid.UUID,
    head_sha: str,
    fingerprint: str,
    attempt: int,
    verdicts: dict[str, Any],
    evidence: dict[str, Any],
    answers_complete: bool,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> bool:
    """Write the terminal COMPLETE. True iff this call was the one that settled the row —
    False means a newer claim took over (the version moved, or the attempt was
    superseded) and NOTHING was written; the late runner's result is simply dropped."""
    return await _finish(
        db,
        review_id=review_id,
        head_sha=head_sha,
        fingerprint=fingerprint,
        attempt=attempt,
        status=ClassificationReviewStatus.COMPLETE,
        verdicts=verdicts,
        evidence=evidence,
        answers_complete=answers_complete,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
    )


async def fail(
    db: AsyncSession,
    *,
    review_id: uuid.UUID,
    head_sha: str,
    fingerprint: str,
    attempt: int,
    code: str,
    detail: str | None = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> bool:
    """Write the terminal FAILED, its (already-redacted) detail, and what the run spent
    learning nothing. True iff this call settled the row.

    Stored ON the row as a BUCKET, never an answer set: `verdicts` stays NULL, because
    "the check couldn't run" must never be readable as a set of No's."""
    return await _finish(
        db,
        review_id=review_id,
        head_sha=head_sha,
        fingerprint=fingerprint,
        attempt=attempt,
        status=ClassificationReviewStatus.FAILED,
        failure_code=code,
        failure_detail=detail,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_tokens=cache_read_tokens,
        cache_write_tokens=cache_write_tokens,
    )


async def _finish(
    db: AsyncSession,
    *,
    review_id: uuid.UUID,
    head_sha: str,
    fingerprint: str,
    attempt: int,
    status: ClassificationReviewStatus,
    **fields: Any,
) -> bool:
    """The single terminal write, guarded so a run settles exactly its OWN claim.

    Every predicate earns its place: `id` names the row, `status == running` stops a second
    terminal write, the commit and the fingerprint stop a runner whose pair was replaced under
    it (the row survives a takeover here, unlike a deployment row, and a fingerprint takeover
    keeps the commit and resets the attempt to 1), and `attempt` stops a zombie from an earlier
    run of the same pair settling the retry that superseded it."""
    result = await db.execute(
        sa.update(ClassificationReview)
        .where(
            ClassificationReview.id == review_id,
            ClassificationReview.status == ClassificationReviewStatus.RUNNING,
            ClassificationReview.head_sha == head_sha,
            ClassificationReview.definitions_fingerprint == fingerprint,
            ClassificationReview.attempt == attempt,
        )
        .values(status=status, finished_at=sa.func.now(), **fields)
    )
    await db.commit()
    settled = bool(_rows_touched(result))
    if not settled:
        _log.info(
            "classification_review_late_write_dropped",
            review_id=str(review_id),
            head_sha=head_sha,
            attempt=attempt,
            outcome=status.value,
        )
    return settled


def _rows_touched(result: object) -> int:
    """How many rows an UPDATE actually changed. `AsyncSession.execute` is declared to
    return the general `Result`, which has no `rowcount` — only the `CursorResult` a DML
    statement really yields does. One narrow accessor, same as the deploy store's."""
    return int(getattr(result, "rowcount", 0) or 0)


# --- reads -------------------------------------------------------------------------


async def get_for_app(db: AsyncSession, *, app_id: uuid.UUID) -> ReviewRecord | None:
    """The app's one review row, frozen, or None if no review was ever claimed.

    The record carries its commit and fingerprint and the CALLER compares them to the pair in
    hand (`is_for`) — a stored row for an older pair must never be read as the answer for a
    newer one, and the store cannot know which pair the caller is asking about."""
    row = (
        await db.execute(sa.select(*_RECORD_COLUMNS).where(ClassificationReview.app_id == app_id))
    ).one_or_none()
    return None if row is None else _record(row)
