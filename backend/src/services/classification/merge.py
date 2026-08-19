"""The per-question merge (U6, R9/R5/P8) — three sources, one effective answer.

A PURE module by design: no database, no model, no I/O — so the truth table in the plan's
design section can be pinned exhaustively (`test_merge.py` names every cell). This is the
one genuinely new decision surface in the feature, and it is deliberately small enough to
read against that table line by line.

THE API IS SHAPED FOR U9's gate, its second consumer. The publish request builds one
`QuestionMergeInput` per questionnaire key — the citizen's declared answer, the stored
review verdict, the scan signal (credentials only), and the policy weight from
`deploy/classification.DATA_CLASSIFICATION_QUESTIONS` — and reads back per-question
effective answers plus `any_weighted_yes`, which is exactly ladder rule 6 ("any weighted
category merges to Yes → ROUTE"). The recorded disagreement kinds are what U9 persists
into the routing declaration and what the administrator's view leads with (R15).

Input conventions the callers must honour (each pinned by a named test):

* `review_verdict=None` means NO COMPLETED REVIEW VERDICT IS ON RECORD for the question —
  the review never ran, is still running, or failed. The merge hands those to the
  citizen's answer (R5's shape); ROUTING them is ladder rule 4's job, never the merge's.
* A Yes whose evidence did not validate was already downgraded to `unanswered` upstream
  (R4, in the runner) — the merge never sees an unevidenced Yes.
* `scan` is meaningful for `credentials_secrets` only; every other question passes
  `ScanSignal.NONE`.

The two P8 obligations live here: a review No against a shown Tier A hit records
`TIER_A_OVERRULE` (an overrule nobody can see is the same as having no scan), and an
absent review with a Tier A hit makes credentials Yes with `SCAN_STOOD_IN` recorded (the
scan is the answer when the model is unavailable). A Tier B signal records NOTHING, ever
— it is a lead handed to the review, expected to be mostly noise.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from enum import StrEnum

from src.services.classification.schema import Verdict


class ScanSignal(StrEnum):
    """The credential scan's state for one question. `TIER_A` is a value-shaped,
    high-confidence hit (the floor and the dispute trigger); `TIER_B` is a
    credential-shaped name — a lead only. Everything but credentials is `NONE`."""

    NONE = "none"
    TIER_B = "tier_b"
    TIER_A = "tier_a"


class DisagreementKind(StrEnum):
    """What the merge asks its caller to put on record (R8/R15/P8). These are the
    strings U9 persists into the routing declaration, so renaming one is a data
    migration, not a refactor."""

    REVIEW_YES_OVER_CITIZEN_NO = "review_yes_over_citizen_no"
    """The review found something the citizen declared absent — the Yes stands."""

    CITIZEN_YES_OVER_REVIEW_NO = "citizen_yes_over_review_no"
    """The citizen declared something the review did not find — the Yes stands."""

    TIER_A_OVERRULE = "tier_a_overrule"
    """The review was SHOWN a Tier A hit and still answered No. Its No is the verdict
    (P8 — the review decides), but the administrator must see the dispute."""

    SCAN_STOOD_IN = "scan_stood_in"
    """No review verdict ever landed and a Tier A hit stood in as the credentials
    answer — origin's floor for when the model is unavailable."""


@dataclass(frozen=True)
class QuestionMergeInput:
    """One question's three sources plus its policy weight, ready to merge."""

    key: str
    weight: int
    citizen_yes: bool
    review_verdict: Verdict | None
    scan: ScanSignal = ScanSignal.NONE


@dataclass(frozen=True)
class MergedQuestion:
    """One question's merged outcome. `effective_yes` is the answer of record;
    `weighted_yes` is its routing contribution (always False at weight zero — Public
    Data merges the same way and never routes anything); `recorded` is what the caller
    must persist, in a deterministic order."""

    key: str
    effective_yes: bool
    weighted_yes: bool
    recorded: tuple[DisagreementKind, ...]


@dataclass(frozen=True)
class MergeOutcome:
    """The whole answer set merged. `any_weighted_yes` IS ladder rule 6's predicate."""

    questions: tuple[MergedQuestion, ...] = field(default=())

    @property
    def any_weighted_yes(self) -> bool:
        return any(question.weighted_yes for question in self.questions)


def merge_question(question: QuestionMergeInput) -> MergedQuestion:
    """Resolve one cell of the truth table. Read top-to-bottom against the plan's merge
    table: absent verdict (floor or citizen), then Yes (stands), then No (stricter-of
    with the citizen, plus the Tier A dispute), then Unanswered (citizen alone, R5)."""
    recorded: list[DisagreementKind] = []
    verdict = question.review_verdict

    if verdict is None:
        # No completed review verdict on record. Tier A is the one signal strong enough
        # to answer on its own (P8's floor); everything else is the citizen's word, and
        # ladder rule 4 routes the absent-review state regardless.
        if question.scan is ScanSignal.TIER_A:
            effective = True
            recorded.append(DisagreementKind.SCAN_STOOD_IN)
        else:
            effective = question.citizen_yes
    elif verdict is Verdict.YES:
        # A review Yes always stands — its evidence was validated before storage (R4).
        effective = True
        if not question.citizen_yes:
            recorded.append(DisagreementKind.REVIEW_YES_OVER_CITIZEN_NO)
    elif verdict is Verdict.NO:
        # The review decides against the scan (P8) — but an overruled Tier A hit is put
        # on record for the administrator. The citizen's Yes still wins stricter-of.
        if question.scan is ScanSignal.TIER_A:
            recorded.append(DisagreementKind.TIER_A_OVERRULE)
        effective = question.citizen_yes
        if question.citizen_yes:
            recorded.append(DisagreementKind.CITIZEN_YES_OVER_REVIEW_NO)
    else:  # Verdict.UNANSWERED
        # R5: the citizen's answer is the only one on record — an honest abstention is
        # not a disagreement, and (unlike a No) not an overrule of anything.
        effective = question.citizen_yes

    return MergedQuestion(
        key=question.key,
        effective_yes=effective,
        weighted_yes=effective and question.weight > 0,
        recorded=tuple(recorded),
    )


def merge_questions(questions: Sequence[QuestionMergeInput]) -> MergeOutcome:
    """Merge a whole answer set, preserving the caller's question order."""
    return MergeOutcome(questions=tuple(merge_question(question) for question in questions))
