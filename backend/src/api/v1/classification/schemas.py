"""Response bodies for the classification review surface.

ONE shape for both verbs: the ensure route and the poll route answer with the same
`ClassificationReviewResponse`, so the dialog renders one thing however it got there.

The per-class projection is deliberately two fields (`verdict`, `reason`), built from exactly
those two keys, so nothing from the internal evidence document can ride along. A class is
projected as key, title, kind and weight: its description is the reviewer's instruction and
never reaches an owner.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import datetime
from typing import Any, Literal

from src.db.models.classification_config import ClassificationKind
from src.schemas import CamelModel
from src.services.classification.config import LiveClass, LiveConfig

# The owner-facing status the routes present. Distinct from the row's three-state
# `ClassificationReviewStatus` on purpose: `nothing_to_review` and `not_reviewed`
# are states of the APP, not of any stored row — and an aged-out RUNNING row is presented
# as `failed` (bucket `review_abandoned`), never as still-in-flight.
PresentedReviewStatus = Literal[
    "nothing_to_review", "not_reviewed", "running", "complete", "failed"
]


class ClassReview(CamelModel):
    """One class's owner-safe projection of the reviewer's answer."""

    verdict: Literal["yes", "no"]
    reason: str

    @classmethod
    def all_of(cls, stored: Mapping[str, Any]) -> dict[str, ClassReview]:
        """The stored `verdicts["classes"]` document, projected class by class onto exactly
        `verdict` and `reason`."""
        return {
            key: cls(verdict=entry["verdict"], reason=entry["reason"])
            for key, entry in stored.items()
        }


class ReviewPolicy(CamelModel):
    """The two settings the owner's answers are scored with."""

    threshold: int
    owners_can_change_answers: bool

    @classmethod
    def of(cls, config: LiveConfig) -> ReviewPolicy:
        return cls(
            threshold=config.threshold, owners_can_change_answers=config.owners_can_change_answers
        )


class ReviewClass(CamelModel):
    """One active class, as the dialog renders and scores it."""

    key: str
    title: str
    kind: ClassificationKind
    weight: int | None

    @classmethod
    def in_display_order(cls, classes: Sequence[LiveClass]) -> list[ReviewClass]:
        """Hard blocks first, then scored classes by weight, highest first; ties in the order the
        classes were created, as the admin table breaks them."""
        ordered = sorted(
            classes,
            key=lambda entry: (
                entry.kind is not ClassificationKind.HARD_BLOCK,
                -(entry.weight or 0),
            ),
        )
        return [
            cls(key=entry.key, title=entry.title, kind=entry.kind, weight=entry.weight)
            for entry in ordered
        ]


class ClassificationReviewResponse(CamelModel):
    """What the dialog knows: the live policy and classes it scores with, the version on
    record, what the review said about it (or why it could not say), and nothing an
    administrator sees that an owner must not.

    TWO STAMPS, deliberately. `head_sha` is the CURRENT saved version and `reviewed_sha` is the
    version the stored review examined. `current` is true only when the review examined the saved
    version under the live class definitions; a review that is not current is presented without
    its answers, and the dialog asks for a fresh one."""

    status: PresentedReviewStatus
    policy: ReviewPolicy
    classes: list[ReviewClass]
    # The current saved version and when it was saved. Both None in the nothing-to-review state.
    head_sha: str | None = None
    saved_at: datetime | None = None
    # The version the stored review examined (the row's stamp), and when its run settled.
    reviewed_sha: str | None = None
    checked_at: datetime | None = None
    current: bool = False
    # Keyed by class key. Present only on a current, complete review.
    verdicts: dict[str, ClassReview] | None = None
    # The failure taxonomy, only when `status == "failed"`: the stable machine bucket,
    # the owner sentence for it, and whether asking again can help (the taxonomy's
    # retry column, AND'ed with the service's three-runs-per-version cap).
    failure_code: str | None = None
    failure_message: str | None = None
    retryable: bool | None = None
