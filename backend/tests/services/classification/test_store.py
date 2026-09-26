"""The classification review store: claim-or-return, and the version/attempt-guarded
terminal write. Three properties carry the design, each pinned by a test:

* a stored COMPLETE answer for the SAME version is returned, never re-run — this store's
  whole reason to exist;
* a stored answer for an OLDER commit or older class definitions is never returned as the newer
  one's answer — the row is replaced wholesale and the attempt counter resets;
* a run settles only its OWN claim — the row survives being taken over, so a zombie runner's
  `review_id` still points at a live row, and only the commit, fingerprint and attempt stop its
  late write.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import sqlalchemy as sa

from src.db.models.classification_review import ClassificationReview, ClassificationReviewStatus
from src.services.classification import store
from tests.factories import AppRegistryFactory, UserFactory

# Two commits, forty hex chars each — the shape the bundle-header parse guarantees.
_V1 = "a" * 40
_V2 = "b" * 40
# Two class-definition fingerprints, sha256 hex.
_F1 = "1" * 64
_F2 = "2" * 64

# A complete answer set, reasons included — the shape the review runner stores.
_VERDICTS: dict[str, Any] = {
    "classes": {
        "credentials_keys": {"verdict": "yes", "reason": "The app stores a sign-in secret."},
        "pii": {"verdict": "no", "reason": "No identity documents are handled."},
        "public_data": {"verdict": "yes", "reason": "The app shows public timetables."},
    }
}

# The internal half — locations the owner and the administrator never see.
_EVIDENCE: dict[str, Any] = {
    "classes": {"credentials_keys": [{"path": "src/lib/auth.ts", "kind": "hardcoded-value"}]},
    "scan_hits": [],
}


async def _app(db):
    user = await UserFactory.create(db)
    app = await AppRegistryFactory.create(db, user_id=user.id)
    return user, app


async def _claimed(
    db, *, app_id: uuid.UUID, user_id: uuid.UUID, head_sha: str, fingerprint: str = _F1
) -> store.ReviewRecord:
    """Claim, asserting this caller won the run. Tests that need a live run to complete
    narrow the outcome once here instead of restating the assertion at every call site."""
    outcome = await store.claim(
        db, app_id=app_id, user_id=user_id, head_sha=head_sha, fingerprint=fingerprint
    )
    assert outcome.claimed is True
    return outcome.review


async def _row_count(db, *, app_id: uuid.UUID) -> int:
    count: int | None = await db.scalar(
        sa.select(sa.func.count())
        .select_from(ClassificationReview)
        .where(ClassificationReview.app_id == app_id)
    )
    return int(count or 0)


# --- the claim ---------------------------------------------------------------------


async def test_a_first_claim_creates_the_row_running_and_stamped(db_session) -> None:
    user, app = await _app(db_session)

    outcome = await store.claim(
        db_session, app_id=app.id, user_id=user.id, head_sha=_V1, fingerprint=_F1
    )

    assert outcome.claimed is True
    record = outcome.review
    assert record.status is ClassificationReviewStatus.RUNNING
    assert record.head_sha == _V1
    assert record.attempt == 1
    assert record.app_id == app.id
    assert record.user_id == user.id
    # Everything the run fills in later starts empty.
    assert record.verdicts is None
    assert record.evidence is None
    assert record.answers_complete is None
    assert record.failure_code is None
    assert record.finished_at is None
    assert record.started_at is not None


async def test_a_stored_complete_row_for_the_same_version_is_returned_not_rerun(
    db_session,
) -> None:
    """The store must not mark the row RUNNING again for an unchanged version — this is why
    re-open reads back the same verdicts instead of re-triggering a run."""
    user, app = await _app(db_session)
    first = await _claimed(db_session, app_id=app.id, user_id=user.id, head_sha=_V1)
    assert await store.succeed(
        db_session,
        review_id=first.review_id,
        head_sha=_V1,
        fingerprint=_F1,
        attempt=first.attempt,
        verdicts=_VERDICTS,
        evidence=_EVIDENCE,
        answers_complete=True,
    )

    again = await store.claim(
        db_session, app_id=app.id, user_id=user.id, head_sha=_V1, fingerprint=_F1
    )

    assert again.claimed is False
    assert again.review.status is ClassificationReviewStatus.COMPLETE
    assert again.review.head_sha == _V1
    assert again.review.verdicts == _VERDICTS
    assert again.review.attempt == first.attempt  # untouched, not re-counted


async def test_a_claim_while_the_same_version_is_running_does_not_double_the_run(
    db_session,
) -> None:
    """Two dialog opens race: exactly one claims, the other reads the in-flight row.
    If both claimed, two detached runners would write over each other's terminal state."""
    user, app = await _app(db_session)
    running = await _claimed(db_session, app_id=app.id, user_id=user.id, head_sha=_V1)

    second = await store.claim(
        db_session, app_id=app.id, user_id=user.id, head_sha=_V1, fingerprint=_F1
    )

    assert second.claimed is False
    assert second.review.status is ClassificationReviewStatus.RUNNING
    assert second.review.attempt == running.attempt  # not incremented — nothing was claimed
    assert second.review.review_id == running.review_id


async def test_a_claim_for_a_newer_version_replaces_the_row_wholesale(db_session) -> None:
    """A new claim replaces what was there, whatever it was — verdicts, failure,
    usage, the lot. A stored answer for an older commit is not history, it is a stale
    answer waiting to be mistaken for a current one."""
    user, app = await _app(db_session)
    first = await _claimed(db_session, app_id=app.id, user_id=user.id, head_sha=_V1)
    assert await store.succeed(
        db_session,
        review_id=first.review_id,
        head_sha=_V1,
        fingerprint=_F1,
        attempt=first.attempt,
        verdicts=_VERDICTS,
        evidence=_EVIDENCE,
        answers_complete=True,
        input_tokens=1000,
        output_tokens=200,
    )

    newer = await store.claim(
        db_session, app_id=app.id, user_id=user.id, head_sha=_V2, fingerprint=_F1
    )

    assert newer.claimed is True
    record = newer.review
    assert record.status is ClassificationReviewStatus.RUNNING
    assert record.head_sha == _V2
    assert record.attempt == 1  # the counter belongs to the stamped version
    # The old answers are NOT returned as the answer for the new version.
    assert record.verdicts is None
    assert record.evidence is None
    assert record.answers_complete is None
    assert record.input_tokens == 0
    assert record.output_tokens == 0
    # And it is a replacement, not a second row.
    assert await _row_count(db_session, app_id=app.id) == 1


async def test_a_failed_row_can_be_reclaimed_for_the_same_version(db_session) -> None:
    """Ask again without re-saving: the app did not change, the citizen asks
    again, and the attempt counter — the spend bound's raw material — increments."""
    user, app = await _app(db_session)
    first = await _claimed(db_session, app_id=app.id, user_id=user.id, head_sha=_V1)
    assert await store.fail(
        db_session,
        review_id=first.review_id,
        head_sha=_V1,
        fingerprint=_F1,
        attempt=first.attempt,
        code="review_failed",
        detail="The automatic check couldn't run.",
        input_tokens=5000,
    )

    retry = await store.claim(
        db_session, app_id=app.id, user_id=user.id, head_sha=_V1, fingerprint=_F1
    )

    assert retry.claimed is True
    record = retry.review
    assert record.status is ClassificationReviewStatus.RUNNING
    assert record.head_sha == _V1
    assert record.attempt == 2
    # The predecessor's failure and spend do not read as the CURRENT run's state.
    assert record.failure_code is None
    assert record.failure_detail is None
    assert record.finished_at is None
    assert record.input_tokens == 0


async def test_the_attempt_counter_is_faithful_past_three(db_session) -> None:
    """The cap of three is SERVICE-layer policy. The store counts, and only counts —
    a store that silently stopped at three would hide exactly the spend the cap's
    enforcement needs to see."""
    user, app = await _app(db_session)

    for expected_attempt in (1, 2, 3, 4):
        record = await _claimed(db_session, app_id=app.id, user_id=user.id, head_sha=_V1)
        assert record.attempt == expected_attempt
        assert await store.fail(
            db_session,
            review_id=record.review_id,
            head_sha=_V1,
            fingerprint=_F1,
            attempt=record.attempt,
            code="review_failed",
        )


async def test_a_reclaim_renews_the_wall_clock_start(db_session) -> None:
    """The ceiling is measured from `started_at`, so a re-claim that kept the old start
    would be born aged-out — every failed-then-retried review would abort instantly."""
    user, app = await _app(db_session)
    first = await _claimed(db_session, app_id=app.id, user_id=user.id, head_sha=_V1)
    assert await store.fail(
        db_session,
        review_id=first.review_id,
        head_sha=_V1,
        fingerprint=_F1,
        attempt=1,
        code="review_failed",
    )
    long_ago = datetime.now(UTC) - timedelta(seconds=10_000)
    await db_session.execute(
        sa.update(ClassificationReview)
        .where(ClassificationReview.id == first.review_id)
        .values(started_at=long_ago)
    )

    retry = await store.claim(
        db_session, app_id=app.id, user_id=user.id, head_sha=_V1, fingerprint=_F1
    )

    assert retry.claimed is True
    assert retry.review.started_at > long_ago


# --- the terminal writes -------------------------------------------------------------


async def test_a_row_settles_exactly_once(db_session) -> None:
    """A second terminal write after the row settled must not overwrite what is now on
    record — same discipline as the deploy store's `_finish`."""
    user, app = await _app(db_session)
    record = await _claimed(db_session, app_id=app.id, user_id=user.id, head_sha=_V1)

    first = await store.succeed(
        db_session,
        review_id=record.review_id,
        head_sha=_V1,
        fingerprint=_F1,
        attempt=record.attempt,
        verdicts=_VERDICTS,
        evidence=_EVIDENCE,
        answers_complete=True,
    )
    late = await store.fail(
        db_session,
        review_id=record.review_id,
        head_sha=_V1,
        fingerprint=_F1,
        attempt=record.attempt,
        code="too_late",
    )

    assert first is True
    assert late is False
    settled = await store.get_for_app(db_session, app_id=app.id)
    assert settled is not None
    assert settled.status is ClassificationReviewStatus.COMPLETE
    assert settled.verdicts == _VERDICTS
    assert settled.failure_code is None


async def test_completing_a_row_a_newer_version_took_over_writes_nothing(db_session) -> None:
    """THE guard this store exists for. The row SURVIVES the takeover (same `review_id`!),
    so only the version stamp in the WHERE clause stops the older run's late completion
    from dressing the new claim in old verdicts."""
    user, app = await _app(db_session)
    old_run = await _claimed(db_session, app_id=app.id, user_id=user.id, head_sha=_V1)
    newer = await store.claim(
        db_session, app_id=app.id, user_id=user.id, head_sha=_V2, fingerprint=_F1
    )
    assert newer.claimed is True
    assert newer.review.review_id == old_run.review_id  # the trap: the id still matches

    late = await store.succeed(
        db_session,
        review_id=old_run.review_id,
        head_sha=_V1,
        fingerprint=_F1,
        attempt=old_run.attempt,
        verdicts=_VERDICTS,
        evidence=_EVIDENCE,
        answers_complete=True,
    )

    assert late is False
    row = await store.get_for_app(db_session, app_id=app.id)
    assert row is not None
    assert row.status is ClassificationReviewStatus.RUNNING
    assert row.head_sha == _V2
    assert row.verdicts is None


async def test_a_zombie_from_a_superseded_attempt_writes_nothing(db_session) -> None:
    """Same version, new attempt: a runner from attempt 1 that comes back after the
    citizen re-asked must not settle attempt 2's run — the `attempt` predicate is what
    stops it, since id, version and RUNNING status all still match."""
    user, app = await _app(db_session)
    first = await _claimed(db_session, app_id=app.id, user_id=user.id, head_sha=_V1)
    assert await store.fail(
        db_session,
        review_id=first.review_id,
        head_sha=_V1,
        fingerprint=_F1,
        attempt=1,
        code="review_failed",
    )
    retry = await store.claim(
        db_session, app_id=app.id, user_id=user.id, head_sha=_V1, fingerprint=_F1
    )
    assert retry.claimed is True and retry.review.attempt == 2

    zombie = await store.fail(
        db_session,
        review_id=first.review_id,
        head_sha=_V1,
        fingerprint=_F1,
        attempt=1,
        code="zombie",
    )

    assert zombie is False
    row = await store.get_for_app(db_session, app_id=app.id)
    assert row is not None
    assert row.status is ClassificationReviewStatus.RUNNING
    assert row.attempt == 2
    assert row.failure_code is None


async def test_a_failure_stores_the_bucket_and_the_spend_never_an_answer_set(db_session) -> None:
    """A failure is never stored as the answer — `verdicts` stays NULL so "the check
    couldn't run" can never be read as six No's. The spend still lands: the failing runs
    are the expensive ones, and they are what the attempt cap is bounding."""
    user, app = await _app(db_session)
    record = await _claimed(db_session, app_id=app.id, user_id=user.id, head_sha=_V1)

    assert await store.fail(
        db_session,
        review_id=record.review_id,
        head_sha=_V1,
        fingerprint=_F1,
        attempt=record.attempt,
        code="review_abandoned",
        detail="The check ran past its ceiling.",
        input_tokens=90_000,
        output_tokens=1_000,
        cache_read_tokens=80_000,
        cache_write_tokens=9_000,
    )

    row = await store.get_for_app(db_session, app_id=app.id)
    assert row is not None
    assert row.status is ClassificationReviewStatus.FAILED
    assert row.head_sha == _V1  # stamped with the version it ATTEMPTED
    assert row.failure_code == "review_abandoned"
    assert row.failure_detail == "The check ran past its ceiling."
    assert row.verdicts is None
    assert row.answers_complete is None
    assert row.finished_at is not None
    # The four classes land raw — no folding, no re-adding (the double-count regression).
    assert row.input_tokens == 90_000
    assert row.output_tokens == 1_000
    assert row.cache_read_tokens == 80_000
    assert row.cache_write_tokens == 9_000


async def test_a_partial_answer_set_is_storable_as_complete_but_says_so(db_session) -> None:
    """`answers_complete` is a separate axis from `status`: the run finished, the answers
    are not whole, and the publish gate treats that as failed. The store records both
    truths rather than collapsing them into one column."""
    user, app = await _app(db_session)
    record = await _claimed(db_session, app_id=app.id, user_id=user.id, head_sha=_V1)

    assert await store.succeed(
        db_session,
        review_id=record.review_id,
        head_sha=_V1,
        fingerprint=_F1,
        attempt=record.attempt,
        verdicts={"credentials_secrets": {"answer": "yes", "reason": "A stored secret."}},
        evidence=_EVIDENCE,
        answers_complete=False,
    )

    row = await store.get_for_app(db_session, app_id=app.id)
    assert row is not None
    assert row.status is ClassificationReviewStatus.COMPLETE
    assert row.answers_complete is False


# --- reads ---------------------------------------------------------------------------


async def test_get_for_app_is_none_when_no_review_was_ever_claimed(db_session) -> None:
    _user, app = await _app(db_session)
    assert await store.get_for_app(db_session, app_id=app.id) is None


async def test_two_apps_review_independently(db_session) -> None:
    """One row per APP, not per user: reviewing project A must not disturb project B."""
    user = await UserFactory.create(db_session)
    app_a = await AppRegistryFactory.create(db_session, user_id=user.id)
    app_b = await AppRegistryFactory.create(db_session, user_id=user.id)

    a = await store.claim(
        db_session, app_id=app_a.id, user_id=user.id, head_sha=_V1, fingerprint=_F1
    )
    b = await store.claim(
        db_session, app_id=app_b.id, user_id=user.id, head_sha=_V2, fingerprint=_F1
    )

    assert a.claimed is True and b.claimed is True
    assert a.review.review_id != b.review.review_id


# --- the whole life of a row ----------------------------------------------------------


async def test_claim_complete_claim_newer_read_leaves_one_truthful_row(db_session) -> None:
    """The unit's verification sequence: claim / complete / claim-newer / read leaves
    exactly one row per app whose stamp always matches its contents."""
    user, app = await _app(db_session)

    v1 = await _claimed(db_session, app_id=app.id, user_id=user.id, head_sha=_V1)
    assert await store.succeed(
        db_session,
        review_id=v1.review_id,
        head_sha=_V1,
        fingerprint=_F1,
        attempt=v1.attempt,
        verdicts=_VERDICTS,
        evidence=_EVIDENCE,
        answers_complete=True,
    )

    unchanged = await store.claim(
        db_session, app_id=app.id, user_id=user.id, head_sha=_V1, fingerprint=_F1
    )
    assert unchanged.claimed is False
    assert unchanged.review.verdicts == _VERDICTS

    v2 = await _claimed(db_session, app_id=app.id, user_id=user.id, head_sha=_V2)
    fresh_verdicts = {**_VERDICTS, "public_data": {"answer": "no", "reason": "Nothing public."}}
    assert await store.succeed(
        db_session,
        review_id=v2.review_id,
        head_sha=_V2,
        fingerprint=_F1,
        attempt=v2.attempt,
        verdicts=fresh_verdicts,
        evidence={},
        answers_complete=True,
    )

    row = await store.get_for_app(db_session, app_id=app.id)
    assert row is not None
    assert row.head_sha == _V2
    assert row.verdicts == fresh_verdicts
    assert row.status is ClassificationReviewStatus.COMPLETE
    assert await _row_count(db_session, app_id=app.id) == 1


# --- the class-definition fingerprint ------------------------------------------------------


async def test_a_claim_under_new_class_definitions_replaces_the_row_at_attempt_one(
    db_session,
) -> None:
    """Same commit, different definitions: the stored answers were given to other questions,
    so they are retired and the attempt counter starts again for the new pair."""
    user, app = await _app(db_session)
    first = await _claimed(db_session, app_id=app.id, user_id=user.id, head_sha=_V1)
    for attempt in (1, 2, 3):
        assert await store.fail(
            db_session,
            review_id=first.review_id,
            head_sha=_V1,
            fingerprint=_F1,
            attempt=attempt,
            code="review_failed",
        )
        if attempt < 3:
            await _claimed(db_session, app_id=app.id, user_id=user.id, head_sha=_V1)

    fresh = await store.claim(
        db_session, app_id=app.id, user_id=user.id, head_sha=_V1, fingerprint=_F2
    )

    assert fresh.claimed is True
    assert fresh.review.attempt == 1
    assert fresh.review.head_sha == _V1
    assert fresh.review.definitions_fingerprint == _F2
    assert fresh.review.status is ClassificationReviewStatus.RUNNING
    assert fresh.review.failure_code is None
    assert await _row_count(db_session, app_id=app.id) == 1


async def test_a_complete_row_under_older_definitions_is_not_returned(db_session) -> None:
    user, app = await _app(db_session)
    first = await _claimed(db_session, app_id=app.id, user_id=user.id, head_sha=_V1)
    assert await store.succeed(
        db_session,
        review_id=first.review_id,
        head_sha=_V1,
        fingerprint=_F1,
        attempt=1,
        verdicts=_VERDICTS,
        evidence=_EVIDENCE,
        answers_complete=True,
    )

    fresh = await store.claim(
        db_session, app_id=app.id, user_id=user.id, head_sha=_V1, fingerprint=_F2
    )

    assert fresh.claimed is True
    assert fresh.review.verdicts is None


async def test_a_zombie_from_older_definitions_writes_nothing(db_session) -> None:
    """The takeover keeps the commit and resets the attempt to 1, so id, commit, RUNNING and
    attempt all still match the zombie's claim — only the fingerprint stops its late write."""
    user, app = await _app(db_session)
    old_run = await _claimed(db_session, app_id=app.id, user_id=user.id, head_sha=_V1)
    newer = await store.claim(
        db_session, app_id=app.id, user_id=user.id, head_sha=_V1, fingerprint=_F2
    )
    assert newer.claimed is True
    assert (newer.review.review_id, newer.review.attempt) == (old_run.review_id, 1)

    late = await store.succeed(
        db_session,
        review_id=old_run.review_id,
        head_sha=_V1,
        fingerprint=_F1,
        attempt=1,
        verdicts=_VERDICTS,
        evidence=_EVIDENCE,
        answers_complete=True,
    )

    assert late is False
    row = await store.get_for_app(db_session, app_id=app.id)
    assert row is not None
    assert row.status is ClassificationReviewStatus.RUNNING
    assert row.definitions_fingerprint == _F2
    assert row.verdicts is None


async def test_a_row_from_before_fingerprints_is_replaced_by_the_next_claim(db_session) -> None:
    user, app = await _app(db_session)
    legacy = await _claimed(db_session, app_id=app.id, user_id=user.id, head_sha=_V1)
    await db_session.execute(
        sa.update(ClassificationReview)
        .where(ClassificationReview.id == legacy.review_id)
        .values(status=ClassificationReviewStatus.COMPLETE, definitions_fingerprint=None)
    )
    await db_session.commit()

    fresh = await store.claim(
        db_session, app_id=app.id, user_id=user.id, head_sha=_V1, fingerprint=_F1
    )

    assert fresh.claimed is True
    assert fresh.review.definitions_fingerprint == _F1
    assert fresh.review.attempt == 1
