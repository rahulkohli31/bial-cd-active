"""The publish gate's decision: the stored review situated against the version being sent, the
score, the outcome, and the declaration every decision stores.

`deploy/router.py` owns the refusals and the approved-copy republish; everything after them is
decided here, by pure functions over the live configuration, the stored review and the owner's
answers. Scoring always uses the live kinds, weights, threshold and owners' switch.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, Final

from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models.classification_config import ClassificationKind
from src.db.models.classification_review import ClassificationReviewStatus
from src.services.audit.log import append_audit
from src.services.classification.config import LiveClass, LiveConfig
from src.services.classification.service import ReviewReadout
from src.services.classification.store import is_for

GATE_AUDIT_ACTION: Final = "publish_gate"
"""The one audit action every gate outcome writes. See `append_gate_audit`."""

DECLARATION_VERSION: Final = 2
"""The declaration's shape marker. A stored declaration without it is the older six-question
shape, which readers render as sent."""


class RouteReason(StrEnum):
    """Why a send goes to an administrator. Each one requires the owner's note."""

    HARD_BLOCK = "hard_block"
    REVIEW_UNFINISHED = "review_unfinished"
    REJECTION_STANDING = "rejection_standing"
    OVER_THRESHOLD = "over_threshold"


@dataclass(frozen=True)
class ReviewAtHead:
    """The stored review, situated against the commit being sent and the live class definitions.

    `current` is the gate's one question about it: a COMPLETE row that answered every live class,
    for exactly this commit and fingerprint, not aged out. Answers, reasons and the time it
    finished are carried only when it is current; an answer about another version or other
    definitions is never read."""

    current: bool
    status: str | None
    failure_code: str | None
    answers: dict[str, bool]
    reasons: dict[str, str]
    checked_at: datetime | None


def review_at_head(
    readout: ReviewReadout | None, *, head_sha: str, config: LiveConfig
) -> ReviewAtHead:
    if readout is None:
        return ReviewAtHead(
            current=False,
            status=None,
            failure_code=None,
            answers={},
            reasons={},
            checked_at=None,
        )
    record = readout.review
    stored = record.verdicts.get("classes") if record.verdicts is not None else None
    if not (
        isinstance(stored, dict)
        and is_for(record, head_sha=head_sha, fingerprint=config.fingerprint)
        and record.status is ClassificationReviewStatus.COMPLETE
        and record.answers_complete is True
        and not readout.aged_out
        and set(stored) == {entry.key for entry in config.classes}
    ):
        return ReviewAtHead(
            current=False,
            status=record.status.value,
            failure_code=record.failure_code,
            answers={},
            reasons={},
            checked_at=None,
        )
    return ReviewAtHead(
        current=True,
        status=record.status.value,
        failure_code=record.failure_code,
        answers={key: entry["verdict"] == "yes" for key, entry in stored.items()},
        reasons={key: str(entry["reason"]) for key, entry in stored.items()},
        checked_at=record.finished_at,
    )


def score(answers: Mapping[str, bool], classes: Sequence[LiveClass]) -> int:
    """The share of the active scored weight answered Yes, out of 100, rounded half up. Zero when
    the scored weight totals zero. Integer arithmetic throughout, so no rounding edge is left to
    floating point."""
    scored = [entry for entry in classes if entry.kind is ClassificationKind.SCORED]
    total = sum(entry.weight or 0 for entry in scored)
    if total == 0:
        return 0
    yes = sum(entry.weight or 0 for entry in scored if answers.get(entry.key))
    return (200 * yes + total) // (2 * total)


def owner_answers_that_count(
    owner_answers: Mapping[str, bool], *, config: LiveConfig
) -> dict[str, bool] | None:
    """The owner's answers the score uses: their scored-class answers while owners may change
    answers, and None when they may not. Hard-block answers are always the reviewer's."""
    if not config.owners_can_change_answers:
        return None
    scored = {entry.key for entry in config.classes if entry.kind is ClassificationKind.SCORED}
    return {key: value for key, value in owner_answers.items() if key in scored}


@dataclass(frozen=True)
class Decision:
    """One send's outcome: a route reason, or None to publish, and the numbers it rests on."""

    reason: RouteReason | None
    owner_answers: dict[str, bool] | None
    reviewer_score: int | None
    final_score: int | None


def decide(
    *,
    config: LiveConfig,
    review: ReviewAtHead,
    owner_answers: Mapping[str, bool],
    rejection_standing: bool,
) -> Decision:
    """The decision table, in order: an unfinished review (no current answers to read a hard
    block from), a hard block answered Yes, a standing rejection, a score over the threshold;
    otherwise publish. The owner's answers count only against a current review."""
    if not review.current:
        return Decision(
            reason=RouteReason.REVIEW_UNFINISHED,
            owner_answers=None,
            reviewer_score=None,
            final_score=None,
        )
    counted = owner_answers_that_count(owner_answers, config=config)
    final = {**review.answers, **(counted or {})}
    reviewer_score = score(review.answers, config.classes)
    final_score = score(final, config.classes)
    hard_block = any(
        review.answers.get(entry.key)
        for entry in config.classes
        if entry.kind is ClassificationKind.HARD_BLOCK
    )
    reason: RouteReason | None = None
    if hard_block:
        reason = RouteReason.HARD_BLOCK
    elif rejection_standing:
        reason = RouteReason.REJECTION_STANDING
    elif final_score > config.threshold:
        reason = RouteReason.OVER_THRESHOLD
    return Decision(
        reason=reason,
        owner_answers=counted,
        reviewer_score=reviewer_score,
        final_score=final_score,
    )


def declaration_document(
    *,
    head_sha: str,
    saved_at: datetime | None,
    decided_at: datetime,
    config: LiveConfig,
    review: ReviewAtHead,
    decision: Decision,
    note: str | None,
) -> dict[str, Any]:
    """The declaration: what was decided about this commit, and everything it was decided under.

    Stored on the app row and in the `publish_gate` audit row on every decision, so its shape is
    contract. It carries its own snapshot of the policy and the classes, so a class reworded or
    switched off later still reads as it was judged. No evidence location ever enters it."""
    return {
        "version": DECLARATION_VERSION,
        "commit": head_sha,
        "savedAt": saved_at.isoformat() if saved_at is not None else None,
        "decidedAt": decided_at.isoformat(),
        "policy": {
            "threshold": config.threshold,
            "ownersCanChangeAnswers": config.owners_can_change_answers,
        },
        "classes": [
            {
                "key": entry.key,
                "title": entry.title,
                "kind": entry.kind.value,
                "weight": entry.weight,
            }
            for entry in config.classes
        ],
        "review": {
            "current": review.current,
            "status": review.status,
            "failureCode": review.failure_code,
            "checkedAt": review.checked_at.isoformat() if review.checked_at is not None else None,
        },
        "reviewerAnswers": dict(review.answers) if review.current else None,
        "reviewerReasons": dict(review.reasons),
        "ownerAnswers": decision.owner_answers,
        "reviewerScore": decision.reviewer_score,
        "score": decision.final_score,
        "outcome": "published" if decision.reason is None else "routed",
        "reason": decision.reason.value if decision.reason is not None else None,
        "note": note,
    }


async def append_gate_audit(
    db: AsyncSession,
    *,
    actor_id: uuid.UUID,
    email: str | None,
    app_id: uuid.UUID,
    project_id: uuid.UUID,
    decision: str,
    rule: str,
    declaration: dict[str, Any] | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    """ONE audit action for every gate outcome, APP-SCOPED so the admin app trail finds it by
    `resource_id` or `detail->>'appId'`. Commit-less. `email` is denormalised because the actor
    reference is nulled on user removal."""
    detail: dict[str, Any] = {
        "appId": str(app_id),
        "projectId": str(project_id),
        "email": email,
        "decision": decision,
        # WHICH rung answered — "routed because of a hard block" and "routed because there was
        # no review" are different stories.
        "rule": rule,
    }
    if declaration is not None:
        detail["declaration"] = declaration
    if extra:
        detail.update(extra)
    await append_audit(
        db,
        actor_id=actor_id,
        action=GATE_AUDIT_ACTION,
        resource_type="app",
        resource_id=str(app_id),
        detail=detail,
    )
