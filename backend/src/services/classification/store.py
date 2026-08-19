"""Row operations on `classification_reviews` — the claim-or-return, and the terminal write.

The claim is the interesting one, and it is a different animal from the deploy store's:
that claim serializes ATTEMPTS (append-only rows, a partial index as the one-in-flight
guard), while this one settles which single row an app carries and whether the caller is
the one who must now go run a review. Three outcomes, resolved in Postgres so a control
plane restart or a concurrent dialog cannot double-run:

* no row, or a row stamped a DIFFERENT version → the row is created / replaced wholesale,
  marked running, attempt reset to 1 — the caller claimed a run (R6a: a new claim replaces
  what was there, whatever it was);
* a FAILED row for the SAME version → re-claimed, attempt incremented — R19's "ask again
  without re-saving". The store counts faithfully and caps nothing: the three-runs-per-
  version ceiling is service policy, and policy enforced in two places is policy enforced
  in neither;
* a RUNNING or COMPLETE row for the SAME version → returned untouched, `claimed=False`
  (R6: re-opening the form for an unchanged version returns the stored answers without
  running again; a run already in flight is never doubled).

THE TERMINAL WRITES ARE GUARDED ON THE VERSION AND THE ATTEMPT, NOT JUST ON `running` —
and that is the load-bearing difference from `deploy/store._finish`. A deployment attempt
is its own row, so `id + status` identifies it; here the row SURVIVES being taken over (a
newer claim rewrites it in place), so a zombie runner's `review_id` still points at a live
row. Its stale `head_sha` (the version moved) or stale `attempt` (a re-claim of the same
version) is what makes its late write touch zero rows instead of dressing a new claim in
an old run's verdicts.

Every write commits its own work: the runner is a detached task that outlives its request
(the deploy service's shape), so it opens short sessions of its own rather than borrowing
one it does not own. And every function returns plain scalars or a frozen dataclass,
NEVER a live ORM instance across the commit boundary — the repo-documented MissingGreenlet
hazard (prefer-returning-over-refresh).
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


def _record(row: Row[Any]) -> ReviewRecord:
    """One committed row → the frozen record. Positional against `_RECORD_COLUMNS`."""
    return ReviewRecord(*row)


def _fresh_run_values(*, head_sha: str, user_id: uuid.UUID) -> dict[str, Any]:
    """Everything a (re)claimed row is reset to — the wholesale overwrite, minus
    `attempt`, which is the one column whose next value depends on WHY the claim won
    (1 on a version change, +1 on a same-version retry).

    `started_at` is renewed because the wall-clock ceiling is measured from it, and the
    verdict/failure/usage fields are cleared because the durable history lives in the
    per-run audit records (R6a), not here — a re-claimed row carrying its predecessor's
    failure text would read as the CURRENT run's state, which it is not."""
    return {
        "user_id": user_id,
        "head_sha": head_sha,
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
) -> ClaimOutcome:
    """Claim a review run for `app_id` at `head_sha`, or get the stored row back. Commits.

    Two passes at most, mirroring the deploy claim's bounded retry: the only way the
    first pass resolves to nothing is a concurrent claim replacing the row for another
    version in the gap between our writes and our read — a window one retry closes and
    an unbounded loop against a hot row would spin in."""
    for _ in range(2):
        claimed = await _try_claim(db, app_id=app_id, user_id=user_id, head_sha=head_sha)
        if claimed is not None:
            return ClaimOutcome(claimed=True, review=claimed)

        stored = await get_for_app(db, app_id=app_id)
        if stored is not None and stored.head_sha == head_sha:
            return ClaimOutcome(claimed=False, review=stored)

        _log.warning(
            "classification_review_claim_contended",
            app_id=str(app_id),
            head_sha=head_sha,
        )

    # Two full passes lost: either the app is being deleted under us (the CASCADE
    # removed the row between insert-conflict and read) or something is rewriting the
    # row faster than we can claim it. Refuse loudly rather than answer with a row
    # stamped a version nobody asked about — security-adjacent checks fail closed.
    raise RuntimeError(
        f"could not claim a classification review for app {app_id}: "
        "the row is being rewritten concurrently"
    )


async def _try_claim(
    db: AsyncSession,
    *,
    app_id: uuid.UUID,
    user_id: uuid.UUID,
    head_sha: str,
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
            .values(app_id=app_id, user_id=user_id, head_sha=head_sha)
            .on_conflict_do_nothing(index_elements=[ClassificationReview.app_id])
            .returning(*_RECORD_COLUMNS)
        )
    ).one_or_none()
    if inserted is not None:
        await db.commit()
        return _record(inserted)

    # 2. The version moved → replace the row WHOLESALE, whatever its status. This is
    #    the write that retires a stale COMPLETE answer, a stale failure, AND an older
    #    version's still-running run (its late completion is disarmed by the terminal
    #    guards below). Attempt resets: the counter belongs to the stamped version.
    replaced = (
        await db.execute(
            sa.update(ClassificationReview)
            .where(
                ClassificationReview.app_id == app_id,
                ClassificationReview.head_sha != head_sha,
            )
            .values(attempt=1, **_fresh_run_values(head_sha=head_sha, user_id=user_id))
            .returning(*_RECORD_COLUMNS)
        )
    ).one_or_none()
    if replaced is not None:
        await db.commit()
        return _record(replaced)

    # 3. Same version, FAILED → re-claim it (R19: ask again without re-saving). The
    #    status predicate is the race guard — of two concurrent retries, the second
    #    finds the row RUNNING and falls through to the stored-row read.
    reclaimed = (
        await db.execute(
            sa.update(ClassificationReview)
            .where(
                ClassificationReview.app_id == app_id,
                ClassificationReview.head_sha == head_sha,
                ClassificationReview.status == ClassificationReviewStatus.FAILED,
            )
            .values(
                attempt=ClassificationReview.attempt + 1,
                **_fresh_run_values(head_sha=head_sha, user_id=user_id),
            )
            .returning(*_RECORD_COLUMNS)
        )
    ).one_or_none()
    await db.commit()
    if reclaimed is not None:
        return _record(reclaimed)

    # Same version, RUNNING or COMPLETE: nothing to claim — the caller reads the row.
    return None


async def succeed(
    db: AsyncSession,
    *,
    review_id: uuid.UUID,
    head_sha: str,
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
    attempt: int,
    code: str,
    detail: str | None = None,
    input_tokens: int = 0,
    output_tokens: int = 0,
    cache_read_tokens: int = 0,
    cache_write_tokens: int = 0,
) -> bool:
    """Write the terminal FAILED — the bucket, its (already-redacted) detail, and what
    the run spent learning nothing. True iff this call was the one that settled the row.

    A failure is stored ON the row (stamped with the version it attempted, R6a) so the
    form can say what happened and the gate can route — but it is stored as a BUCKET,
    never as an answer set: `verdicts` stays NULL, because "the check couldn't run" must
    never be readable as six No's (R19)."""
    return await _finish(
        db,
        review_id=review_id,
        head_sha=head_sha,
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
    attempt: int,
    status: ClassificationReviewStatus,
    **fields: Any,
) -> bool:
    """The single terminal write, guarded so a run settles exactly its OWN claim.

    All four predicates earn their place: `id` names the row, `status == running` stops
    a second terminal write, `head_sha` stops a runner whose version was replaced under
    it (the row survives a takeover here, unlike a deployment row), and `attempt` stops
    a zombie from an earlier same-version run settling the retry that superseded it."""
    result = await db.execute(
        sa.update(ClassificationReview)
        .where(
            ClassificationReview.id == review_id,
            ClassificationReview.status == ClassificationReviewStatus.RUNNING,
            ClassificationReview.head_sha == head_sha,
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

    The record carries its `head_sha` and the CALLER compares it to the version in hand —
    a stored row for an older version must never be read as the answer for a newer one
    (R6), and the store cannot know which version the caller is asking about."""
    row = (
        await db.execute(sa.select(*_RECORD_COLUMNS).where(ClassificationReview.app_id == app_id))
    ).one_or_none()
    return None if row is None else _record(row)
