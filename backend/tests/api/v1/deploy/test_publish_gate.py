"""The publish gate — the precedence ladder, through the real route.

The refusals and the approved copy first, then the configured decision: a hard block, an
unfinished review, a standing rejection and a score over the threshold each route with the
owner's note, and anything else publishes. Every decision stores the declaration on the app row
and in the `publish_gate` audit row.

`wire` binds the REAL classification review service (no override), and `_seed_review` writes
rows through the real store in the runner's exact document shape, stamped with the live class
definitions — so a mock returning what it was fed cannot green these tests. The configuration
itself is the seeded one, edited in place where a scenario needs another policy.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa
from fastapi import FastAPI

from src.api.deps import storage_or_none_dependency
from src.api.v1.deploy.deps import deploy_service_or_none
from src.db.models.app_registry import AppRegistry, AppStatus
from src.db.models.audit import AuditLog
from src.db.models.classification_config import (
    ClassificationClass,
    ClassificationKind,
    ClassificationPolicy,
)
from src.db.models.classification_review import ClassificationReview
from src.services.classification import store as review_store
from src.services.classification.config import load_live_config
from src.services.classification.constants import REVIEW_WALL_CLOCK_CEILING_S
from src.services.deploy.service import DeployNotPossibleError, StartedDeploy
from src.services.storage import snapshot_key, submission_key
from tests.api.v1.build_sessions.conftest import auth_headers
from tests.factories import AppRegistryFactory, UserFactory
from tests.fakes import FakeStorage, a_git_bundle

_DEPLOY = "/v1/projects/{pid}/deploy"
_SHA = "ab" * 20
_OLDER_SHA = "cd" * 20
_NOTE = "It shows the public flight board to the gate staff."
_SAVED_AT = datetime(2026, 9, 26, 11, 45, tzinfo=UTC)


# --- wiring --------------------------------------------------------------------------


class _RecordingPipeline:
    """The deploy service, recording instead of reaching Azure: the commit the gate decided
    about, and the bundle it named when that is not the saved snapshot."""

    def __init__(self, *, refuse: DeployNotPossibleError | None = None) -> None:
        self.started: list[dict[str, Any]] = []
        self._refuse = refuse

    async def start(
        self,
        db: object,
        *,
        user_id: uuid.UUID,
        app_id: uuid.UUID,
        project_id: uuid.UUID,
        conversation_id: uuid.UUID | None,
        expected_commit_sha: str | None = None,
        bundle_key: str | None = None,
    ) -> StartedDeploy:
        if self._refuse is not None:
            raise self._refuse
        self.started.append(
            {
                "app_id": app_id,
                "conversation_id": conversation_id,
                "expected_commit_sha": expected_commit_sha,
                "bundle_key": bundle_key,
            }
        )
        return StartedDeploy(deployment_id=uuid.uuid4(), app_id=app_id)


class _Wiring:
    def __init__(self, app: FastAPI, store: FakeStorage, pipeline: _RecordingPipeline) -> None:
        self.app = app
        self.store = store
        self.pipeline = pipeline


@pytest.fixture
def wire(app: FastAPI) -> _Wiring:
    store = FakeStorage()
    pipeline = _RecordingPipeline()
    app.dependency_overrides[storage_or_none_dependency] = lambda: store
    app.dependency_overrides[deploy_service_or_none] = lambda: pipeline
    return _Wiring(app, store, pipeline)


def _body(
    *, commit: str = _SHA, note: str | None = None, answers: dict[str, bool] | None = None
) -> dict[str, Any]:
    """A request about `commit` — by default the version `_owner_with_saved_app` saved."""
    body: dict[str, Any] = {"commitSha": commit}
    if answers is not None:
        body["answers"] = answers
    if note is not None:
        body["note"] = note
    return body


async def _owner_with_saved_app(db, store: FakeStorage, *, sha: str = _SHA, **overrides):
    user = await UserFactory.create(db)
    app_row = await AppRegistryFactory.create(db, user_id=user.id, **overrides)
    store.objects[snapshot_key(app_row.id)] = a_git_bundle(sha)
    store.meta[snapshot_key(app_row.id)] = {"head_sha": sha}
    store.mtimes[snapshot_key(app_row.id)] = _SAVED_AT
    return user, app_row


async def _seed_review(
    db,
    *,
    app_id: uuid.UUID,
    user_id: uuid.UUID,
    sha: str = _SHA,
    status: str = "complete",
    answers_complete: bool = True,
    yes: tuple[str, ...] = (),
    fingerprint: str | None = None,
    attempts: int = 1,
) -> None:
    """Write one review row through the REAL store, in the runner's shape, against the live class
    definitions. `status="running"` leaves the claim as it lands; `attempts` failed runs reach
    the attempt cap the way the runner would."""
    config = await load_live_config(db)
    fingerprint = fingerprint or config.fingerprint
    for _ in range(attempts):
        outcome = await review_store.claim(
            db, app_id=app_id, user_id=user_id, head_sha=sha, fingerprint=fingerprint
        )
        assert outcome.claimed
        if status == "running":
            return
        if status == "failed":
            await review_store.fail(
                db,
                review_id=outcome.review.review_id,
                head_sha=sha,
                fingerprint=fingerprint,
                attempt=outcome.review.attempt,
                code="review_failed",
            )
            continue
        await review_store.succeed(
            db,
            review_id=outcome.review.review_id,
            head_sha=sha,
            fingerprint=fingerprint,
            attempt=outcome.review.attempt,
            verdicts={
                "classes": {
                    entry.key: {
                        "verdict": "yes" if entry.key in yes else "no",
                        "reason": f"What the reviewer found about {entry.key}.",
                    }
                    for entry in config.classes
                }
            },
            evidence={"classes": {}, "scan_hits": []},
            answers_complete=answers_complete,
        )


async def _set_policy(db, **values: object) -> None:
    await db.execute(sa.update(ClassificationPolicy).values(**values))


async def _set_class(db, key: str, **values: object) -> None:
    await db.execute(
        sa.update(ClassificationClass).where(ClassificationClass.key == key).values(**values)
    )


async def _gate_rows(db, app_id: uuid.UUID) -> list[AuditLog]:
    return list(
        (
            await db.execute(
                sa.select(AuditLog)
                .where(
                    AuditLog.resource_type == "app",
                    AuditLog.resource_id == str(app_id),
                    AuditLog.action == "publish_gate",
                )
                .order_by(AuditLog.created_at)
            )
        )
        .scalars()
        .all()
    )


async def _review_finished_at(db, app_id: uuid.UUID) -> datetime:
    query = sa.select(ClassificationReview.finished_at).where(
        ClassificationReview.app_id == app_id
    )
    finished = (await db.execute(query)).scalar_one()
    assert finished is not None
    return finished


async def _declaration(db, app_id: uuid.UUID) -> dict[str, Any]:
    fresh = await db.get(AppRegistry, app_id, populate_existing=True)
    assert fresh is not None
    assert fresh.declaration is not None
    return fresh.declaration


async def _post(client, user, app_row, body: dict[str, Any]):
    return await client.post(
        _DEPLOY.format(pid=app_row.project_id), headers=auth_headers(user), json=body
    )


# --- publish -------------------------------------------------------------------------


async def test_every_class_no_publishes_with_no_note_and_stores_the_declaration(
    wire, client, db_session
) -> None:
    """At the seeded threshold of 100 a clean feedback form goes live with no administrator, and
    the auto-published app's row carries its declaration exactly as a routed one would."""
    user, app_row = await _owner_with_saved_app(db_session, wire.store)
    await _seed_review(db_session, app_id=app_row.id, user_id=user.id)

    resp = await _post(client, user, app_row, _body())

    assert resp.status_code == 202
    assert resp.json()["outcome"] == "started"
    (started,) = wire.pipeline.started
    assert started["expected_commit_sha"] == _SHA
    assert started["bundle_key"] is None
    declaration = await _declaration(db_session, app_row.id)
    assert declaration["version"] == 2
    assert declaration["commit"] == _SHA
    assert declaration["savedAt"] == _SAVED_AT.isoformat()
    checked_at = await _review_finished_at(db_session, app_row.id)
    assert declaration["review"]["checkedAt"] == checked_at.isoformat()
    assert (declaration["outcome"], declaration["reason"]) == ("published", None)
    assert (declaration["reviewerScore"], declaration["score"]) == (0, 0)
    assert declaration["policy"] == {"threshold": 100, "ownersCanChangeAnswers": True}
    assert {entry["key"] for entry in declaration["classes"]} >= {"pii", "public_data"}
    fresh = await db_session.get(AppRegistry, app_row.id, populate_existing=True)
    assert fresh is not None and fresh.status is AppStatus.DRAFT  # never entered the queue
    (row,) = await _gate_rows(db_session, app_row.id)
    assert row.detail is not None
    assert (row.detail["decision"], row.detail["rule"]) == ("published", "all_clear")
    assert row.detail["declaration"] == declaration


async def test_the_owners_correction_publishes_under_the_threshold(
    wire, client, db_session
) -> None:
    """Owners may change answers, threshold 50: the reviewer's three scored Yes answers make 60,
    and the owner's Integrations No makes 40."""
    await _set_policy(db_session, threshold=50)
    user, app_row = await _owner_with_saved_app(db_session, wire.store)
    await _seed_review(
        db_session,
        app_id=app_row.id,
        user_id=user.id,
        yes=("confidential_business_data", "ai_usage", "integrations"),
    )

    resp = await _post(client, user, app_row, _body(answers={"integrations": False}))

    assert resp.status_code == 202
    declaration = await _declaration(db_session, app_row.id)
    assert (declaration["reviewerScore"], declaration["score"]) == (60, 40)
    assert declaration["ownerAnswers"] == {"integrations": False}
    assert declaration["reviewerAnswers"]["integrations"] is True


async def test_no_active_scored_weight_publishes_when_there_is_no_hard_block(
    wire, client, db_session
) -> None:
    await _set_policy(db_session, threshold=0)
    await db_session.execute(
        sa.update(ClassificationClass)
        .where(ClassificationClass.kind == ClassificationKind.SCORED)
        .values(weight=0)
    )
    user, app_row = await _owner_with_saved_app(db_session, wire.store)
    await _seed_review(db_session, app_id=app_row.id, user_id=user.id, yes=("ai_usage",))

    resp = await _post(client, user, app_row, _body())

    assert resp.status_code == 202
    assert (await _declaration(db_session, app_row.id))["score"] == 0


async def test_a_refused_claim_leaves_no_decision_on_the_app_row(app, client, db_session) -> None:
    store = FakeStorage()
    pipeline = _RecordingPipeline(
        refuse=DeployNotPossibleError("Already deploying.", code="deploy_in_flight")
    )
    app.dependency_overrides[storage_or_none_dependency] = lambda: store
    app.dependency_overrides[deploy_service_or_none] = lambda: pipeline
    user, app_row = await _owner_with_saved_app(db_session, store)
    await _seed_review(db_session, app_id=app_row.id, user_id=user.id)

    resp = await _post(client, user, app_row, _body())

    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "deploy_in_flight"
    fresh = await db_session.get(AppRegistry, app_row.id, populate_existing=True)
    assert fresh is not None
    assert fresh.declaration is None
    assert await _gate_rows(db_session, app_row.id) == []


# --- a hard block --------------------------------------------------------------------


async def test_a_hard_block_needs_a_note_and_writes_nothing_without_one(
    wire, client, db_session
) -> None:
    user, app_row = await _owner_with_saved_app(db_session, wire.store)
    await _seed_review(db_session, app_id=app_row.id, user_id=user.id, yes=("pii",))

    resp = await _post(client, user, app_row, _body())

    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "note_required"
    assert error["detail"] == {"reason": "hard_block"}
    assert wire.pipeline.started == []
    fresh = await db_session.get(AppRegistry, app_row.id, populate_existing=True)
    assert fresh is not None
    assert fresh.status is AppStatus.DRAFT
    assert fresh.declaration is None
    assert await _gate_rows(db_session, app_row.id) == []


async def test_a_hard_block_with_a_note_routes_carrying_the_note_and_the_reason(
    wire, client, db_session
) -> None:
    user, app_row = await _owner_with_saved_app(db_session, wire.store)
    await _seed_review(db_session, app_id=app_row.id, user_id=user.id, yes=("pii",))

    resp = await _post(client, user, app_row, _body(note=_NOTE))

    assert resp.status_code == 200
    body = resp.json()
    assert body["outcome"] == "routed_for_review"
    assert body["commitSha"] == _SHA
    assert wire.pipeline.started == []
    fresh = await db_session.get(AppRegistry, app_row.id, populate_existing=True)
    assert fresh is not None and fresh.status is AppStatus.PENDING
    declaration = await _declaration(db_session, app_row.id)
    assert (declaration["outcome"], declaration["reason"]) == ("routed", "hard_block")
    assert declaration["note"] == _NOTE
    assert declaration["savedAt"] == _SAVED_AT.isoformat()
    checked_at = await _review_finished_at(db_session, app_row.id)
    assert declaration["review"]["checkedAt"] == checked_at.isoformat()
    assert declaration["reviewerAnswers"]["pii"] is True
    assert declaration["reviewerReasons"]["pii"] == "What the reviewer found about pii."
    (row,) = await _gate_rows(db_session, app_row.id)
    assert row.detail is not None
    assert (row.detail["decision"], row.detail["rule"]) == ("routed", "hard_block")
    assert row.detail["declaration"] == declaration


async def test_an_owner_answer_cannot_clear_a_hard_block(wire, client, db_session) -> None:
    user, app_row = await _owner_with_saved_app(db_session, wire.store)
    await _seed_review(db_session, app_id=app_row.id, user_id=user.id, yes=("pii",))

    resp = await _post(client, user, app_row, _body(answers={"pii": False}))

    assert resp.status_code == 422
    assert resp.json()["error"]["detail"] == {"reason": "hard_block"}


async def test_a_class_made_a_hard_block_routes_the_next_send_without_a_new_review(
    wire, client, db_session
) -> None:
    """A kind change leaves stored reviews current: the existing AI usage Yes now routes. An app
    decided before the change keeps the declaration it was decided under."""
    earlier_owner, earlier = await _owner_with_saved_app(db_session, wire.store)
    await _seed_review(db_session, app_id=earlier.id, user_id=earlier_owner.id, yes=("pii",))
    assert (await _post(client, earlier_owner, earlier, _body(note=_NOTE))).status_code == 200
    before = await _declaration(db_session, earlier.id)

    user, app_row = await _owner_with_saved_app(db_session, wire.store)
    await _seed_review(db_session, app_id=app_row.id, user_id=user.id, yes=("ai_usage",))
    await _set_class(db_session, "ai_usage", kind=ClassificationKind.HARD_BLOCK, weight=None)

    refused = await _post(client, user, app_row, _body())
    routed = await _post(client, user, app_row, _body(note=_NOTE))

    assert refused.json()["error"]["detail"] == {"reason": "hard_block"}
    assert routed.status_code == 200
    declaration = await _declaration(db_session, app_row.id)
    assert declaration["review"]["current"] is True
    ai_usage = next(entry for entry in declaration["classes"] if entry["key"] == "ai_usage")
    assert ai_usage == {
        "key": "ai_usage",
        "title": "AI usage",
        "kind": "hard_block",
        "weight": None,
    }
    assert await _declaration(db_session, earlier.id) == before
    earlier_ai = next(entry for entry in before["classes"] if entry["key"] == "ai_usage")
    assert earlier_ai["kind"] == "scored"


# --- over the threshold --------------------------------------------------------------


async def test_owners_locked_out_are_scored_on_the_reviewers_answers(
    wire, client, db_session
) -> None:
    """Owners cannot change answers, threshold 50, reviewer score 60. Answers the client sends
    anyway are ignored and not recorded."""
    await _set_policy(db_session, threshold=50, owners_can_change_answers=False)
    user, app_row = await _owner_with_saved_app(db_session, wire.store)
    await _seed_review(
        db_session,
        app_id=app_row.id,
        user_id=user.id,
        yes=("confidential_business_data", "ai_usage", "integrations"),
    )
    forged = {"integrations": False, "ai_usage": False}

    refused = await _post(client, user, app_row, _body(answers=forged))
    routed = await _post(client, user, app_row, _body(answers=forged, note=_NOTE))

    assert refused.status_code == 422
    assert refused.json()["error"] == {
        "code": "note_required",
        "message": refused.json()["error"]["message"],
        "detail": {"reason": "over_threshold"},
    }
    assert routed.status_code == 200
    declaration = await _declaration(db_session, app_row.id)
    assert declaration["reason"] == "over_threshold"
    assert declaration["ownerAnswers"] is None
    assert (declaration["reviewerScore"], declaration["score"]) == (60, 60)
    assert declaration["policy"] == {"threshold": 50, "ownersCanChangeAnswers": False}


async def test_a_lowered_threshold_applies_to_the_next_send_and_the_review_stays_current(
    wire, client, db_session
) -> None:
    user, app_row = await _owner_with_saved_app(db_session, wire.store)
    await _seed_review(db_session, app_id=app_row.id, user_id=user.id, yes=("public_data",))
    await _set_policy(db_session, threshold=10)

    resp = await _post(client, user, app_row, _body(note=_NOTE))

    assert resp.status_code == 200
    declaration = await _declaration(db_session, app_row.id)
    assert declaration["reason"] == "over_threshold"
    assert declaration["review"]["current"] is True
    assert declaration["score"] == 20


# --- an unfinished review ------------------------------------------------------------


async def test_no_review_at_all_routes_as_unfinished(wire, client, db_session) -> None:
    user, app_row = await _owner_with_saved_app(db_session, wire.store)

    refused = await _post(client, user, app_row, _body())
    routed = await _post(client, user, app_row, _body(note=_NOTE))

    assert refused.status_code == 422
    assert refused.json()["error"]["detail"] == {"reason": "review_unfinished"}
    assert routed.status_code == 200
    declaration = await _declaration(db_session, app_row.id)
    assert declaration["reason"] == "review_unfinished"
    assert declaration["review"] == {
        "current": False,
        "status": None,
        "failureCode": None,
        "checkedAt": None,
    }
    assert declaration["savedAt"] == _SAVED_AT.isoformat()
    assert declaration["reviewerAnswers"] is None
    assert declaration["score"] is None


async def test_three_failed_reviews_route_every_send_even_at_score_zero(
    wire, client, db_session
) -> None:
    user, app_row = await _owner_with_saved_app(db_session, wire.store)
    await _seed_review(db_session, app_id=app_row.id, user_id=user.id, status="failed", attempts=3)

    refused = await _post(client, user, app_row, _body())
    routed = await _post(client, user, app_row, _body(note=_NOTE))

    assert refused.json()["error"]["detail"] == {"reason": "review_unfinished"}
    assert routed.status_code == 200
    declaration = await _declaration(db_session, app_row.id)
    assert declaration["review"] == {
        "current": False,
        "status": "failed",
        "failureCode": "review_failed",
        "checkedAt": None,
    }


async def test_a_class_added_after_the_review_makes_the_send_unfinished(
    wire, client, db_session
) -> None:
    user, app_row = await _owner_with_saved_app(db_session, wire.store)
    await _seed_review(db_session, app_id=app_row.id, user_id=user.id)
    db_session.add(
        ClassificationClass(
            key="file_uploads",
            title="File uploads",
            description="Yes if the app accepts uploaded files. No: a calculator.",
            kind=ClassificationKind.SCORED,
            weight=20,
        )
    )
    await db_session.flush()

    refused = await _post(client, user, app_row, _body())
    routed = await _post(client, user, app_row, _body(note=_NOTE))

    assert refused.status_code == 422
    assert refused.json()["error"]["detail"] == {"reason": "review_unfinished"}
    assert wire.pipeline.started == []
    assert routed.status_code == 200
    assert (await _declaration(db_session, app_row.id))["reason"] == "review_unfinished"


@pytest.mark.parametrize(
    "seed",
    [
        {"status": "running"},
        {"answers_complete": False},
        {"sha": _OLDER_SHA},
        {"fingerprint": "0" * 64},
    ],
    ids=["still running", "flagged incomplete", "an older commit", "older class definitions"],
)
async def test_a_review_that_is_not_current_routes_as_unfinished(
    wire, client, db_session, seed: dict[str, Any]
) -> None:
    user, app_row = await _owner_with_saved_app(db_session, wire.store)
    await _seed_review(db_session, app_id=app_row.id, user_id=user.id, **seed)

    resp = await _post(client, user, app_row, _body())

    assert resp.status_code == 422
    assert resp.json()["error"]["detail"] == {"reason": "review_unfinished"}
    assert wire.pipeline.started == []


async def test_an_aged_out_review_routes_as_unfinished(wire, client, db_session) -> None:
    user, app_row = await _owner_with_saved_app(db_session, wire.store)
    await _seed_review(db_session, app_id=app_row.id, user_id=user.id, status="running")
    stale = datetime.now(UTC) - timedelta(seconds=REVIEW_WALL_CLOCK_CEILING_S + 60)
    await db_session.execute(
        sa.update(ClassificationReview)
        .where(ClassificationReview.app_id == app_row.id)
        .values(started_at=stale)
    )

    resp = await _post(client, user, app_row, _body(note=_NOTE))

    assert resp.status_code == 200
    assert (await _declaration(db_session, app_row.id))["reason"] == "review_unfinished"


# --- a standing rejection ------------------------------------------------------------


async def test_a_rejected_app_routes_even_with_a_clean_review(wire, client, db_session) -> None:
    user, app_row = await _owner_with_saved_app(
        db_session,
        wire.store,
        status=AppStatus.REJECTED,
        rejection_note="Please remove the staff phone list.",
        # `reject` writes both; the gate reads this one.
        rejection_standing=True,
    )
    await _seed_review(db_session, app_id=app_row.id, user_id=user.id)

    refused = await _post(client, user, app_row, _body())
    routed = await _post(client, user, app_row, _body(note=_NOTE))

    assert refused.json()["error"]["detail"] == {"reason": "rejection_standing"}
    assert routed.status_code == 200
    (row,) = await _gate_rows(db_session, app_row.id)
    assert row.detail is not None
    assert row.detail["rule"] == "rejection_standing"


async def test_a_rejection_survives_the_publish_then_withdraw_round_trip(
    wire, client, db_session
) -> None:
    """Publishing a REJECTED app writes PENDING, withdrawing writes DRAFT, and by the third call
    the status has forgotten the refusal — the gate still routes. Walks the real routes: the bug
    lived in the seam between two handlers that were each correct alone."""
    user, app_row = await _owner_with_saved_app(
        db_session,
        wire.store,
        status=AppStatus.REJECTED,
        rejection_note="Please remove the staff phone list.",
        rejection_standing=True,
    )
    await _seed_review(db_session, app_id=app_row.id, user_id=user.id)

    first = await _post(client, user, app_row, _body(note=_NOTE))
    assert first.status_code == 200
    withdrawn = await client.post(f"/v1/apps/{app_row.id}/withdraw", headers=auth_headers(user))
    assert withdrawn.status_code == 200
    await db_session.refresh(app_row)
    assert app_row.status is AppStatus.DRAFT
    assert app_row.rejection_standing is True

    second = await _post(client, user, app_row, _body(note=_NOTE))

    assert second.status_code == 200
    assert second.json()["outcome"] == "routed_for_review"
    assert wire.pipeline.started == []
    rules = [row.detail["rule"] for row in await _gate_rows(db_session, app_row.id) if row.detail]
    assert rules == ["rejection_standing", "rejection_standing"]


async def test_an_approval_is_what_lifts_a_standing_rejection(wire, client, db_session) -> None:
    user, app_row = await _owner_with_saved_app(
        db_session,
        wire.store,
        status=AppStatus.REJECTED,
        rejection_note="Please remove the staff phone list.",
        rejection_standing=True,
    )
    await _seed_review(db_session, app_id=app_row.id, user_id=user.id)
    app_row.rejection_standing = False  # what `approve` writes
    app_row.status = AppStatus.DRAFT
    await db_session.commit()

    resp = await _post(client, user, app_row, _body())

    assert resp.status_code == 202
    assert resp.json()["outcome"] == "started"


# --- the owner's answers --------------------------------------------------------------


async def test_an_answer_for_a_class_that_is_not_active_is_refused(
    wire, client, db_session
) -> None:
    user, app_row = await _owner_with_saved_app(db_session, wire.store)
    await _seed_review(db_session, app_id=app_row.id, user_id=user.id)

    resp = await _post(client, user, app_row, _body(answers={"health_data": False}))

    assert resp.status_code == 422
    error = resp.json()["error"]
    assert error["code"] == "unknown_class"
    assert error["detail"] == {"keys": ["health_data"]}
    assert wire.pipeline.started == []


async def test_a_browser_supplied_review_cannot_influence_the_decision(
    wire, client, db_session
) -> None:
    """The gate reads the STORED review, and the request schema has no review field. Extra body
    keys are dropped at the boundary, so a caller cannot answer for the platform."""
    user, app_row = await _owner_with_saved_app(db_session, wire.store)
    await _seed_review(db_session, app_id=app_row.id, user_id=user.id, yes=("pii",))

    body = _body()
    body["review"] = {"classes": {"pii": {"verdict": "no"}}}
    body["reviewerAnswers"] = {"pii": False}
    body["score"] = 0

    resp = await _post(client, user, app_row, body)

    assert resp.status_code == 422
    assert resp.json()["error"]["detail"] == {"reason": "hard_block"}
    assert wire.pipeline.started == []


async def test_the_note_is_redacted_before_it_is_stored(wire, client, db_session) -> None:
    user, app_row = await _owner_with_saved_app(db_session, wire.store)
    await _seed_review(db_session, app_id=app_row.id, user_id=user.id, yes=("pii",))

    resp = await _post(
        client,
        user,
        app_row,
        _body(note='It signs in with password = "hunter2plaintext" to the vendor API.'),
    )

    assert resp.status_code == 200
    declaration = await _declaration(db_session, app_row.id)
    assert "hunter2plaintext" not in declaration["note"]
    assert "password" in declaration["note"]


async def test_a_blank_note_is_no_note(wire, client, db_session) -> None:
    user, app_row = await _owner_with_saved_app(db_session, wire.store)
    await _seed_review(db_session, app_id=app_row.id, user_id=user.id, yes=("pii",))

    resp = await _post(client, user, app_row, _body(note="   "))

    assert resp.status_code == 422
    assert resp.json()["error"]["code"] == "note_required"


# --- rule 3: the approved copy ---------------------------------------------------------


def _approved(sha: str, **extra: object) -> dict[str, Any]:
    submission_id = uuid.uuid4()
    return {
        "status": AppStatus.APPROVED,
        "source_submission_id": submission_id,
        "source_commit_sha": sha,
        "approved_submission_id": submission_id,
        "approved_commit_sha": sha,
        # An older, six-question declaration: an approved copy keeps whatever it was approved on.
        "declaration": {"citizen": {"answers": {}, "explanation": "As submitted."}},
        **extra,
    }


async def test_the_approved_commit_publishes_the_approved_copy(wire, client, db_session) -> None:
    """The approval IS the decision for that commit: a hard block, no review and no note change
    nothing, and what ships is the submission copy, pinned to the commit."""
    user, app_row = await _owner_with_saved_app(db_session, wire.store, **_approved(_SHA))
    await _seed_review(db_session, app_id=app_row.id, user_id=user.id, yes=("pii",))

    resp = await _post(client, user, app_row, _body())

    assert resp.status_code == 202
    (started,) = wire.pipeline.started
    assert started["expected_commit_sha"] == _SHA
    assert app_row.approved_submission_id is not None
    assert started["bundle_key"] == submission_key(app_row.id, app_row.approved_submission_id)
    (row,) = await _gate_rows(db_session, app_row.id)
    assert row.detail is not None
    assert (row.detail["decision"], row.detail["rule"]) == ("published", "approved_override")
    assert row.detail["declaration"] == {
        "citizen": {"answers": {}, "explanation": "As submitted."}
    }
    assert await _declaration(db_session, app_row.id) == row.detail["declaration"]


async def test_the_approved_commit_republishes_the_copy_while_newer_work_is_saved(
    wire, client, db_session
) -> None:
    user, app_row = await _owner_with_saved_app(
        db_session, wire.store, sha=_SHA, **_approved(_OLDER_SHA)
    )

    resp = await _post(client, user, app_row, _body(commit=_OLDER_SHA))

    assert resp.status_code == 202
    (started,) = wire.pipeline.started
    assert started["expected_commit_sha"] == _OLDER_SHA
    assert started["bundle_key"] is not None


async def test_the_saved_version_of_an_approved_app_goes_through_the_gate(
    wire, client, db_session
) -> None:
    user, app_row = await _owner_with_saved_app(
        db_session, wire.store, sha=_SHA, **_approved(_OLDER_SHA)
    )

    resp = await _post(client, user, app_row, _body(note=_NOTE))

    assert resp.status_code == 200
    assert resp.json()["outcome"] == "routed_for_review"
    assert wire.pipeline.started == []


async def test_an_approval_with_no_stored_copy_is_not_an_approved_copy(
    wire, client, db_session
) -> None:
    user, app_row = await _owner_with_saved_app(
        db_session,
        wire.store,
        sha=_SHA,
        **_approved(_OLDER_SHA, approved_submission_id=None),
    )

    resp = await _post(client, user, app_row, _body(commit=_OLDER_SHA))

    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "snapshot_moved"
    assert wire.pipeline.started == []


# --- rule 4: the request is about the saved version or nothing -------------------------


async def test_a_commit_that_is_neither_saved_nor_approved_is_refused(
    wire, client, db_session
) -> None:
    user, app_row = await _owner_with_saved_app(db_session, wire.store)
    await _seed_review(db_session, app_id=app_row.id, user_id=user.id, sha=_OLDER_SHA)

    resp = await _post(client, user, app_row, _body(commit=_OLDER_SHA))

    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "snapshot_moved"
    assert wire.pipeline.started == []
    assert await _gate_rows(db_session, app_row.id) == []


async def test_a_bundle_with_no_stamped_commit_can_be_named_by_no_request(
    wire, client, db_session
) -> None:
    user, app_row = await _owner_with_saved_app(db_session, wire.store)
    wire.store.meta[snapshot_key(app_row.id)] = {}

    resp = await _post(client, user, app_row, _body())

    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "snapshot_moved"


# --- rules 1 and 2: the plain refusals ------------------------------------------------


async def test_a_disabled_app_cannot_publish(wire, client, db_session) -> None:
    user, app_row = await _owner_with_saved_app(db_session, wire.store, status=AppStatus.DISABLED)
    await _seed_review(db_session, app_id=app_row.id, user_id=user.id)

    resp = await _post(client, user, app_row, _body())

    assert resp.status_code == 409
    error = resp.json()["error"]
    assert error["code"] == "app_disabled"
    assert "detail" not in error
    assert wire.pipeline.started == []


async def test_a_pending_app_is_refused_with_the_state_the_surfaces_must_render(
    wire, client, db_session
) -> None:
    user, app_row = await _owner_with_saved_app(
        db_session,
        wire.store,
        status=AppStatus.PENDING,
        source_submission_id=uuid.uuid4(),
        source_commit_sha=_OLDER_SHA,
        submitted_at=datetime.now(UTC),
        rejection_note="Earlier round: please drop the ID numbers.",
    )

    resp = await _post(client, user, app_row, _body(note=_NOTE))

    assert resp.status_code == 409
    error = resp.json()["error"]
    assert error["code"] == "waiting_for_review"
    assert error["detail"]["submittedSha"] == _OLDER_SHA
    assert error["detail"]["rejectionNote"] == "Earlier round: please drop the ID numbers."
    assert wire.pipeline.started == []


# --- properties that hold across the rungs -------------------------------------------


async def test_routing_works_with_the_deploy_service_unbound(app, client, db_session) -> None:
    """Routing needs object storage and the queue, never the deploy service."""
    store = FakeStorage()
    app.dependency_overrides[storage_or_none_dependency] = lambda: store
    app.dependency_overrides[deploy_service_or_none] = lambda: None
    user, app_row = await _owner_with_saved_app(db_session, store)
    await _seed_review(db_session, app_id=app_row.id, user_id=user.id, yes=("financial_data",))

    resp = await _post(client, user, app_row, _body(note=_NOTE))

    assert resp.status_code == 200
    assert resp.json()["outcome"] == "routed_for_review"


async def test_publishing_still_503s_when_the_deploy_service_is_unbound(
    app, client, db_session
) -> None:
    store = FakeStorage()
    app.dependency_overrides[storage_or_none_dependency] = lambda: store
    app.dependency_overrides[deploy_service_or_none] = lambda: None
    user, app_row = await _owner_with_saved_app(db_session, store)
    await _seed_review(db_session, app_id=app_row.id, user_id=user.id)

    resp = await _post(client, user, app_row, _body())

    assert resp.status_code == 503
    assert "message" in resp.json()["error"]


async def test_the_gate_is_owner_scoped(wire, client, db_session) -> None:
    """A dropped ownership predicate is a cross-user leak; a stranger's probe is a non-leaking
    404, never a 403 confirming the project exists."""
    _owner, app_row = await _owner_with_saved_app(db_session, wire.store)
    stranger = await UserFactory.create(db_session, email="stranger@rvaiglobal.com")

    resp = await _post(client, stranger, app_row, _body(note=_NOTE))

    assert resp.status_code == 404
    assert wire.pipeline.started == []
