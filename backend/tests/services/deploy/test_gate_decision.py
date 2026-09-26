"""The publish gate's decision as pure functions: the score, the decision table, which answers
count, when a stored review is current, and the declaration every decision stores.

The score is also pinned against the fixture the portal's score mirror reads, so the dialog and
the server cannot disagree about a number.
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from src.db.models.classification_config import ClassificationKind
from src.db.models.classification_review import ClassificationReviewStatus
from src.services.classification.config import LiveClass, LiveConfig
from src.services.classification.service import ReviewReadout
from src.services.classification.store import ReviewRecord
from src.services.deploy.gate import (
    DECLARATION_VERSION,
    ReviewAtHead,
    RouteReason,
    decide,
    declaration_document,
    review_at_head,
    score,
)

FIXTURE = (
    Path(__file__).resolve().parents[3].parent
    / "portal"
    / "src"
    / "utils"
    / "__fixtures__"
    / "classification-score-cases.json"
)

_SHA = "ab" * 20


def _class(key: str, kind: str, weight: int | None) -> LiveClass:
    return LiveClass(
        key=key,
        title=key.replace("_", " ").capitalize(),
        description=f"Yes if the app handles {key}. No: a calculator.",
        kind=ClassificationKind(kind),
        weight=weight,
    )


_LAUNCH = (
    _class("ai_usage", "scored", 20),
    _class("confidential_business_data", "scored", 20),
    _class("credentials_keys", "scored", 20),
    _class("financial_data", "hard_block", None),
    _class("integrations", "scored", 20),
    _class("pii", "hard_block", None),
    _class("public_data", "scored", 20),
)


def _config(
    classes: tuple[LiveClass, ...] = _LAUNCH, *, threshold: int = 100, owners: bool = True
) -> LiveConfig:
    return LiveConfig(threshold=threshold, owners_can_change_answers=owners, classes=classes)


def _current(config: LiveConfig, **yes: bool) -> ReviewAtHead:
    return ReviewAtHead(
        current=True,
        status="complete",
        failure_code=None,
        answers={entry.key: yes.get(entry.key, False) for entry in config.classes},
        reasons={entry.key: f"Why {entry.key}." for entry in config.classes},
    )


_UNFINISHED = ReviewAtHead(
    current=False, status="failed", failure_code="review_failed", answers={}, reasons={}
)


# --- parity with the portal's score mirror -------------------------------------------------


def _fixture_cases() -> list[dict[str, Any]]:
    cases: list[dict[str, Any]] = json.loads(FIXTURE.read_text(encoding="utf-8"))["cases"]
    return cases


@pytest.mark.parametrize("case", _fixture_cases(), ids=lambda case: case["name"])
def test_the_server_reproduces_every_row_of_the_shared_score_fixture(case: dict[str, Any]) -> None:
    config = _config(
        tuple(_class(entry["key"], entry["kind"], entry["weight"]) for entry in case["classes"]),
        owners=case["ownersCanChangeAnswers"],
    )
    review = ReviewAtHead(
        current=True,
        status="complete",
        failure_code=None,
        answers=case["reviewerAnswers"],
        reasons={},
    )

    decision = decide(
        config=config, review=review, owner_answers=case["ownerAnswers"], rejection_standing=False
    )

    assert decision.reviewer_score == case["reviewerScore"]
    assert decision.final_score == case["score"]


def test_the_fixture_carries_the_rounding_and_empty_weight_rows() -> None:
    names = [case["name"] for case in _fixture_cases()]
    assert "12.5 rounds up to 13" in names
    assert "12.49 rounds down to 12" in names
    assert "a total scored weight of 0 scores 0" in names
    assert "every class No scores 0" in names


def test_rounding_is_half_up_on_exact_halves() -> None:
    halves = (_class("a", "scored", 1), _class("b", "scored", 1))
    assert score({"a": True}, halves) == 50
    eighth = (_class("a", "scored", 1), _class("b", "scored", 7))
    assert score({"a": True}, eighth) == 13
    three_eighths = (_class("a", "scored", 3), _class("b", "scored", 5))
    assert score({"a": True}, three_eighths) == 38  # 37.5


# --- the decision table -------------------------------------------------------------------


def test_every_class_no_publishes_at_the_seeded_threshold() -> None:
    config = _config()

    decision = decide(
        config=config, review=_current(config), owner_answers={}, rejection_standing=False
    )

    assert decision.reason is None
    assert decision.final_score == 0


def test_a_hard_block_answered_yes_routes_whatever_the_score() -> None:
    config = _config()

    decision = decide(
        config=config,
        review=_current(config, pii=True),
        owner_answers={},
        rejection_standing=False,
    )

    assert decision.reason is RouteReason.HARD_BLOCK
    assert decision.final_score == 0


def test_a_class_made_a_hard_block_routes_on_the_existing_answer() -> None:
    before = _config()
    review = _current(before, ai_usage=True)
    after = _config(
        tuple(
            _class("ai_usage", "hard_block", None) if entry.key == "ai_usage" else entry
            for entry in _LAUNCH
        )
    )

    assert after.fingerprint == before.fingerprint
    assert (
        decide(config=before, review=review, owner_answers={}, rejection_standing=False).reason
        is None
    )
    assert (
        decide(config=after, review=review, owner_answers={}, rejection_standing=False).reason
        is RouteReason.HARD_BLOCK
    )


def test_an_unfinished_review_routes_even_at_score_zero() -> None:
    decision = decide(
        config=_config(), review=_UNFINISHED, owner_answers={}, rejection_standing=False
    )

    assert decision.reason is RouteReason.REVIEW_UNFINISHED
    assert decision.reviewer_score is None
    assert decision.final_score is None
    assert decision.owner_answers is None


def test_a_standing_rejection_routes_when_everything_else_is_clear() -> None:
    config = _config()

    decision = decide(
        config=config, review=_current(config), owner_answers={}, rejection_standing=True
    )

    assert decision.reason is RouteReason.REJECTION_STANDING


def test_a_hard_block_outranks_a_standing_rejection() -> None:
    config = _config()

    decision = decide(
        config=config, review=_current(config, pii=True), owner_answers={}, rejection_standing=True
    )

    assert decision.reason is RouteReason.HARD_BLOCK


def test_the_owners_correction_brings_the_score_under_the_threshold() -> None:
    config = _config(threshold=50)
    review = _current(config, confidential_business_data=True, ai_usage=True, integrations=True)

    reviewer_only = decide(
        config=config, review=review, owner_answers={}, rejection_standing=False
    )
    corrected = decide(
        config=config,
        review=review,
        owner_answers={"integrations": False},
        rejection_standing=False,
    )

    assert (reviewer_only.reason, reviewer_only.final_score) == (RouteReason.OVER_THRESHOLD, 60)
    assert (corrected.reason, corrected.reviewer_score, corrected.final_score) == (None, 60, 40)
    assert corrected.owner_answers == {"integrations": False}


def test_with_owners_locked_out_their_answers_are_ignored_and_not_recorded() -> None:
    config = _config(threshold=50, owners=False)
    review = _current(config, confidential_business_data=True, ai_usage=True, integrations=True)

    decision = decide(
        config=config,
        review=review,
        owner_answers={"integrations": False, "ai_usage": False},
        rejection_standing=False,
    )

    assert decision.reason is RouteReason.OVER_THRESHOLD
    assert decision.final_score == 60
    assert decision.owner_answers is None


def test_a_score_equal_to_the_threshold_publishes() -> None:
    config = _config(threshold=40)
    review = _current(config, ai_usage=True, integrations=True)

    decision = decide(config=config, review=review, owner_answers={}, rejection_standing=False)

    assert decision.final_score == 40
    assert decision.reason is None


def test_an_owner_answer_for_a_hard_block_is_ignored() -> None:
    config = _config()

    decision = decide(
        config=config,
        review=_current(config, pii=True),
        owner_answers={"pii": False},
        rejection_standing=False,
    )

    assert decision.reason is RouteReason.HARD_BLOCK
    assert decision.owner_answers == {}


def test_a_lowered_threshold_applies_to_the_next_send() -> None:
    before = _config(threshold=100)
    after = _config(threshold=10)
    review = _current(before, public_data=True)

    assert (
        decide(config=before, review=review, owner_answers={}, rejection_standing=False).reason
        is None
    )
    assert (
        decide(config=after, review=review, owner_answers={}, rejection_standing=False).reason
        is RouteReason.OVER_THRESHOLD
    )


def test_no_active_scored_weight_publishes_without_a_hard_block() -> None:
    config = _config(
        (_class("pii", "hard_block", None), _class("ai_usage", "scored", 0)), threshold=0
    )

    decision = decide(
        config=config,
        review=_current(config, ai_usage=True),
        owner_answers={},
        rejection_standing=False,
    )

    assert decision.final_score == 0
    assert decision.reason is None


# --- when a stored review is current ------------------------------------------------------


def _readout(
    config: LiveConfig,
    *,
    head_sha: str = _SHA,
    fingerprint: str | None = None,
    status: ClassificationReviewStatus = ClassificationReviewStatus.COMPLETE,
    answers_complete: bool | None = True,
    aged_out: bool = False,
    verdicts: dict[str, Any] | None = None,
) -> ReviewReadout:
    now = datetime.now(UTC)
    stored = verdicts
    if stored is None:
        stored = {
            "classes": {
                entry.key: {"verdict": "yes" if entry.key == "pii" else "no", "reason": "Why."}
                for entry in config.classes
            }
        }
    record = ReviewRecord(
        review_id=uuid.uuid7(),
        app_id=uuid.uuid7(),
        user_id=uuid.uuid7(),
        head_sha=head_sha,
        definitions_fingerprint=config.fingerprint if fingerprint is None else fingerprint,
        status=status,
        attempt=1,
        verdicts=stored,
        evidence={},
        answers_complete=answers_complete,
        failure_code=None,
        failure_detail=None,
        started_at=now,
        finished_at=now,
        input_tokens=0,
        output_tokens=0,
        cache_read_tokens=0,
        cache_write_tokens=0,
    )
    return ReviewReadout(review=record, aged_out=aged_out)


def test_a_complete_review_of_this_commit_under_these_definitions_is_current() -> None:
    config = _config()

    review = review_at_head(_readout(config), head_sha=_SHA, config=config)

    assert review.current is True
    assert review.answers["pii"] is True
    assert review.answers["ai_usage"] is False
    assert review.reasons["pii"] == "Why."


@pytest.mark.parametrize(
    "overrides",
    [
        {"head_sha": "cd" * 20},
        {"fingerprint": "0" * 64},
        {"status": ClassificationReviewStatus.RUNNING},
        {"status": ClassificationReviewStatus.FAILED},
        {"answers_complete": False},
        {"aged_out": True},
        {"verdicts": {"questions": {"credentials_secrets": {"verdict": "no", "reason": "x"}}}},
    ],
    ids=[
        "another commit",
        "older class definitions",
        "still running",
        "failed",
        "flagged incomplete",
        "aged out",
        "the older six-question shape",
    ],
)
def test_any_other_review_is_not_current_and_carries_no_answers(overrides: dict[str, Any]) -> None:
    config = _config()

    review = review_at_head(_readout(config, **overrides), head_sha=_SHA, config=config)

    assert review.current is False
    assert review.answers == {}
    assert review.status is not None


def test_a_review_missing_a_live_class_is_not_current() -> None:
    config = _config()
    partial = {"classes": {"pii": {"verdict": "no", "reason": "Why."}}}

    review = review_at_head(_readout(config, verdicts=partial), head_sha=_SHA, config=config)

    assert review.current is False


def test_no_review_at_all_is_not_current() -> None:
    review = review_at_head(None, head_sha=_SHA, config=_config())

    assert review == ReviewAtHead(
        current=False, status=None, failure_code=None, answers={}, reasons={}
    )


# --- the declaration ----------------------------------------------------------------------


def test_the_declaration_records_the_decision_and_everything_it_was_made_under() -> None:
    config = _config(threshold=50)
    review = _current(config, confidential_business_data=True, ai_usage=True, integrations=True)
    decision = decide(
        config=config,
        review=review,
        owner_answers={"integrations": False},
        rejection_standing=False,
    )
    decided_at = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)

    document = declaration_document(
        head_sha=_SHA,
        decided_at=decided_at,
        config=config,
        review=review,
        decision=decision,
        note=None,
    )

    assert document["version"] == DECLARATION_VERSION == 2
    assert document["commit"] == _SHA
    assert document["decidedAt"] == decided_at.isoformat()
    assert document["policy"] == {"threshold": 50, "ownersCanChangeAnswers": True}
    assert document["classes"][0] == {
        "key": "ai_usage",
        "title": "Ai usage",
        "kind": "scored",
        "weight": 20,
    }
    assert all("description" not in entry for entry in document["classes"])
    assert document["reviewerAnswers"]["integrations"] is True
    assert document["reviewerReasons"]["integrations"] == "Why integrations."
    assert document["ownerAnswers"] == {"integrations": False}
    assert (document["reviewerScore"], document["score"]) == (60, 40)
    assert (document["outcome"], document["reason"], document["note"]) == ("published", None, None)
    assert document["review"] == {"current": True, "status": "complete", "failureCode": None}


def test_an_unfinished_review_declares_no_answers_and_no_scores() -> None:
    decision = decide(
        config=_config(), review=_UNFINISHED, owner_answers={}, rejection_standing=False
    )

    document = declaration_document(
        head_sha=_SHA,
        decided_at=datetime.now(UTC),
        config=_config(),
        review=_UNFINISHED,
        decision=decision,
        note="Please look at this one.",
    )

    assert document["outcome"] == "routed"
    assert document["reason"] == "review_unfinished"
    assert document["reviewerAnswers"] is None
    assert document["ownerAnswers"] is None
    assert document["score"] is None
    assert document["review"]["failureCode"] == "review_failed"
    assert document["note"] == "Please look at this one."
