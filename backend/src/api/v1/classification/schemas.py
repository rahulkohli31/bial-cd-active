"""Response bodies for the classification review surface (U7).

ONE shape for both verbs: the ensure route and the poll route answer with the same
`ClassificationReviewResponse`, so the dialog renders one thing however it got there.

THE PER-QUESTION PROJECTION IS DELIBERATELY TWO FIELDS (`verdict`, `reason`). The stored
`verdicts` document also carries `agreed_with_scan`, `downgraded_from_yes` and the compact
scan block — the administrator's dispute presentation, which is U13's concern and not the
citizen's — and the sibling `evidence` document carries cited locations (R4/OD-B).
Building the wire model field-by-field from exactly two keys is what makes "nothing from
evidence, for any verdict" structural rather than a filter someone has to remember when
the stored shape grows a field.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from typing import Any, Final, Literal

from src.schemas import CamelModel
from src.services.deploy.classification import CLASSIFICATION_KEYS

# The citizen-facing status the routes present. Distinct from the row's three-state
# `ClassificationReviewStatus` on purpose: `nothing_to_review` (R21) and `not_reviewed`
# are states of the APP, not of any stored row — and an aged-out RUNNING row is presented
# as `failed` (bucket `review_abandoned`), never as still-in-flight.
PresentedReviewStatus = Literal[
    "nothing_to_review", "not_reviewed", "running", "complete", "failed"
]

# The same sentence the Tier A floor stores for the questions it leaves unanswered
# (`service._FLOOR_UNANSWERED_REASON`), owned here as this surface's own copy: U7 maps
# stored states to citizen sentences, and importing another module's private constant
# would couple the wire copy to the runner's internals.
_UNANSWERED_REASON: Final = (
    "The automatic check could not finish, so this question needs your own answer."
)


class QuestionReview(CamelModel):
    """One question's citizen-safe projection. `unanswered` is a real verdict, distinct
    from `no` (R5): the review answers only where it has evidence, and an unanswered
    question is decided by the citizen alone."""

    verdict: Literal["yes", "no", "unanswered"]
    reason: str


class ReviewAnswers(CamelModel):
    """The six questions, declared in questionnaire order — the same fields (and the
    same camelCase wire keys) as `DataClassificationAnswers`, so the portal's answer
    state and this projection line up key-for-key."""

    credentials_secrets: QuestionReview
    health_data: QuestionReview
    personal_information: QuestionReview
    financial_data: QuestionReview
    confidential_business_data: QuestionReview
    public_data: QuestionReview

    @classmethod
    def of(cls, questions: Mapping[str, Any]) -> ReviewAnswers:
        """Project the stored `verdicts["questions"]` document onto the wire shape.

        Keyed through `CLASSIFICATION_KEYS` with subscription (the
        `classification_flags` discipline) so a reworded questionnaire fails loudly here
        rather than silently dropping a question, and built from exactly `verdict` and
        `reason` so nothing else the document carries can ride along."""
        return cls.model_validate(
            {
                key: {"verdict": questions[key]["verdict"], "reason": questions[key]["reason"]}
                for key in CLASSIFICATION_KEYS
            }
        )

    @classmethod
    def all_unanswered(cls) -> ReviewAnswers:
        """R19's presentation of a failure with no Tier A floor: six unanswered
        questions handed to the citizen, never readable as six No's. Materialized at
        the presentation seam — the STORE keeps `verdicts` NULL on a floorless failure
        precisely so a failure is never stored as an answer set."""
        return cls.model_validate(
            {
                key: {"verdict": "unanswered", "reason": _UNANSWERED_REASON}
                for key in CLASSIFICATION_KEYS
            }
        )


class ClassificationReviewResponse(CamelModel):
    """What the dialog knows: the version on record, what the review said about it (or
    why it could not say), and nothing an administrator sees that a citizen must not.

    TWO STAMPS, deliberately. `head_sha` is the CURRENT saved version (read from the
    snapshot blob's metadata at answer time) and `reviewed_sha` is the version the
    stored review examined. They differ exactly when a Save landed after the review —
    U11 filters by the stamp it asked for, so a second tab's newer review can never
    paint answers for a version this dialog never named."""

    status: PresentedReviewStatus
    # The current saved version and when it was saved — the "version X, saved at Y"
    # line the dialog leads with. Both None in the nothing-to-review state.
    head_sha: str | None = None
    saved_at: datetime | None = None
    # The version the stored review examined (the row's stamp).
    reviewed_sha: str | None = None
    # Present on `complete`, and on `failed` (the Tier A floor when one stands, six
    # unanswered questions otherwise). None while running and in the no-review states.
    verdicts: ReviewAnswers | None = None
    # The failure taxonomy, only when `status == "failed"`: the stable machine bucket,
    # the citizen sentence for it, and whether asking again can help (the taxonomy's
    # retry column, AND'ed with the service's three-runs-per-version cap).
    failure_code: str | None = None
    failure_message: str | None = None
    retryable: bool | None = None
