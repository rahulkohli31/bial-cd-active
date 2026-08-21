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
`TIER_A_OVERRULE`, and an absent review with a Tier A hit makes credentials Yes with
`SCAN_STOOD_IN` recorded (the scan is the answer when the model is unavailable). A Tier B
signal records NOTHING, ever — it is a lead handed to the review, expected to be mostly
noise.

A DISCARDED OR OVERRULED AGENT SIGNAL ROUTES; IT DOES NOT FALL THROUGH TO THE CITIZEN.
Two cells used to, and both published unattended on the citizen's own No:

  * a Tier A hit the review overruled (`TIER_A_OVERRULE`), and
  * a review Yes R4 discarded for citing locations that do not exist
    (`UNEVIDENCED_YES_ROUTED`).

Recording a dispute was the whole of P8's compensation for making the scan non-binding —
but the record renders only on the administrator's review screen, which is only ever
opened for an app that ROUTED. On an app nothing else routes, the note was written to a
page nobody would open. The rule the merge now holds everywhere: EITHER SIDE MAY RAISE A
FLAG AND NEITHER MAY LOWER THE OTHER'S — a review Yes already stood over a citizen No,
and these two close the cases where the agent's own signal was quietly discarded instead.

This is a deliberate narrowing of P8's "the review decides" and of R4's fall-through to
R5; both are recorded as amendments in the plan's Review Log rather than smuggled in.
Public Data is untouched: weight zero still routes nothing, by design (ASM22).
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
    """The review was SHOWN a Tier A hit and still answered No. The overrule ROUTES: the
    review's No is no longer the last word on its own, because a recorded dispute that
    only ever renders on an already-routed app reaches nobody on the apps where it is
    the only signal there is."""

    UNEVIDENCED_YES_ROUTED = "unevidenced_yes_routed"
    """The review answered Yes and R4 discarded it — every location it cited was absent
    from the reviewed code. The discard stands as a VERDICT (the citizen still answers
    the question), but it routes: the agent raised a flag, and a flag raised on
    hallucinated evidence is the last state that should buy LESS scrutiny."""

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
    #: R4 discarded a Yes here — every location it cited was absent from the reviewed
    #: code, so `review_verdict` arrives as UNANSWERED. The merge cannot recover this
    #: from the verdict alone (an honest abstention looks identical), so the runner's
    #: stored `downgraded_from_yes` is carried in rather than re-derived.
    downgraded_from_yes: bool = False


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
    #: True when the ONLY thing that made this a Yes is a dispute the citizen cannot see
    #: — a Tier A hit the review overruled, or a Yes R4 discarded. Both are server-side
    #: facts: the form shows the review's answers, and on these the review's answer is No
    #: (or absent). It ROUTES all the same; what it must not do is oblige an explanation,
    #: because the citizen would be asked to account for something no surface has told
    #: them about and that they do not believe is there (R10 asks them to explain their
    #: OWN declaration). The administrator gets the dispute copy instead.
    disputed_only: bool = False


@dataclass(frozen=True)
class MergeOutcome:
    """The whole answer set merged. `any_weighted_yes` IS ladder rule 6's predicate."""

    questions: tuple[MergedQuestion, ...] = field(default=())

    @property
    def any_weighted_yes(self) -> bool:
        return any(question.weighted_yes for question in self.questions)

    @property
    def explanation_owed(self) -> bool:
        """R10's predicate, which is NOT `any_weighted_yes`. Routing and the explanation
        obligation used to be one condition because every weighted Yes came from a source
        the citizen could see — their own answer, or a review verdict the form shows them.
        A dispute-only Yes is neither: the form shows the review answering No, so a
        citizen asked to explain it has been handed a refusal about a fact no surface has
        told them. It still ROUTES; the administrator is the one who reads the dispute."""
        return any(
            question.weighted_yes and not question.disputed_only for question in self.questions
        )


def merge_question(question: QuestionMergeInput) -> MergedQuestion:
    """Resolve one cell of the truth table. Read top-to-bottom against the plan's merge
    table: absent verdict (floor or citizen), then Yes (stands), then No (stricter-of
    with the citizen, plus the Tier A dispute), then Unanswered (citizen alone, R5)."""
    recorded: list[DisagreementKind] = []
    verdict = question.review_verdict
    # Set only where a Yes is produced by a signal the citizen has no surface for; any
    # path that also carries their own Yes or a review Yes clears it below.
    disputed_only = False

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
        # The review still DECIDES against the scan (P8 is intact — its No is the stored
        # verdict). What changed is that an overruled Tier A hit now ROUTES rather than
        # merely being recorded: the dispute was only ever rendered on the administrator's
        # screen, which is only ever opened for an app that ROUTED, so on the one app
        # where the scan is the sole remaining signal the record reached nobody. The scan
        # is the half of this gate a citizen cannot talk out of — the review reads their
        # source raw — so a disagreement about it is exactly what a human should see.
        if question.scan is ScanSignal.TIER_A:
            recorded.append(DisagreementKind.TIER_A_OVERRULE)
            effective = True
            # Their own Yes is something they can be asked to explain; the dispute alone
            # is not — the form showed them the review answering No.
            disputed_only = not question.citizen_yes
        else:
            effective = question.citizen_yes
        if question.citizen_yes:
            recorded.append(DisagreementKind.CITIZEN_YES_OVER_REVIEW_NO)
    else:  # Verdict.UNANSWERED
        # R5: the citizen's answer is the only one on record — an honest abstention is
        # not a disagreement, and (unlike a No) not an overrule of anything.
        #
        # ONE EXCEPTION, and it is not an abstention at all: a Yes R4 discarded for
        # citing locations that do not exist. R4 rightly refuses to treat it as evidence,
        # but "not evidence" and "not a signal" are different things, and collapsing them
        # let the agent raise a flag and the app publish anyway. It routes and the
        # discard is recorded; the citizen's answer is still what goes on record as the
        # verdict for the question.
        if question.downgraded_from_yes:
            recorded.append(DisagreementKind.UNEVIDENCED_YES_ROUTED)
            effective = True
            disputed_only = not question.citizen_yes
        else:
            effective = question.citizen_yes

    return MergedQuestion(
        key=question.key,
        effective_yes=effective,
        weighted_yes=effective and question.weight > 0,
        recorded=tuple(recorded),
        disputed_only=disputed_only,
    )


def merge_questions(questions: Sequence[QuestionMergeInput]) -> MergeOutcome:
    """Merge a whole answer set, preserving the caller's question order."""
    return MergeOutcome(questions=tuple(merge_question(question) for question in questions))
