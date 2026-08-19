"""The per-question merge truth table (U6, R9/R5/P8) — written FIRST, before merge.py.

Every cell of the plan's merge table gets a named test. The module under test is PURE —
no database, no model, no I/O — so the table can be pinned exhaustively, which is the
point: the merge is the one genuinely new decision in the feature, and a mock-based test
would prove nothing about it.

Two upstream mappings the table relies on are documented by their own tests here rather
than hidden in the service: a Yes with invalid evidence was ALREADY downgraded to
unanswered before storage (R4 — the merge never sees it), and "no review at all",
"review still running" and "review never returned" all reach the merge as the SAME
absent verdict (`review_verdict=None`) — the gate's ladder rule 4 is what routes those,
not the merge.
"""

from __future__ import annotations

import itertools

import pytest

from src.services.classification.merge import (
    DisagreementKind,
    MergedQuestion,
    QuestionMergeInput,
    ScanSignal,
    merge_question,
    merge_questions,
)
from src.services.classification.schema import Verdict
from src.services.deploy.classification import DATA_CLASSIFICATION_QUESTIONS

# A weighted question and the weight-zero one, straight from the policy table so the
# tests cannot drift from the questionnaire.
_WEIGHTS = {key: weight for key, _label, weight in DATA_CLASSIFICATION_QUESTIONS}
_CREDENTIALS_WEIGHT = _WEIGHTS["credentials_secrets"]
_PUBLIC_WEIGHT = _WEIGHTS["public_data"]
assert _PUBLIC_WEIGHT == 0  # noqa: S101 — a test-module invariant, not runtime validation


def _merge(
    *,
    review: Verdict | None,
    citizen_yes: bool,
    scan: ScanSignal = ScanSignal.NONE,
    weight: int = _CREDENTIALS_WEIGHT,
    key: str = "credentials_secrets",
) -> MergedQuestion:
    return merge_question(
        QuestionMergeInput(
            key=key,
            weight=weight,
            citizen_yes=citizen_yes,
            review_verdict=review,
            scan=scan,
        )
    )


# ---------------------------------------------------------------------------------------
# Row 1: review Yes (evidence valid) | citizen either | — | effective Yes
# ---------------------------------------------------------------------------------------


def test_review_yes_citizen_yes_is_yes_nothing_recorded() -> None:
    merged = _merge(review=Verdict.YES, citizen_yes=True)
    assert merged.effective_yes is True
    assert merged.weighted_yes is True
    assert merged.recorded == ()


def test_review_yes_over_citizen_no_is_yes_and_the_disagreement_is_recorded() -> None:
    merged = _merge(review=Verdict.YES, citizen_yes=False)
    assert merged.effective_yes is True
    assert merged.recorded == (DisagreementKind.REVIEW_YES_OVER_CITIZEN_NO,)


# ---------------------------------------------------------------------------------------
# Row 2: review Yes (evidence invalid) → treated as Unanswered — UPSTREAM (R4).
# ---------------------------------------------------------------------------------------


def test_invalid_evidence_yes_arrives_here_as_unanswered_and_citizen_decides() -> None:
    # The service downgrades an unevidenced Yes BEFORE storage; the merge only ever
    # sees the post-downgrade `unanswered`, which hands the question to the citizen.
    assert _merge(review=Verdict.UNANSWERED, citizen_yes=False).effective_yes is False
    assert _merge(review=Verdict.UNANSWERED, citizen_yes=True).effective_yes is True


# ---------------------------------------------------------------------------------------
# Rows 3-4: review No | citizen Yes → Yes (recorded); citizen No → No
# ---------------------------------------------------------------------------------------


def test_citizen_yes_over_review_no_is_yes_and_the_disagreement_is_recorded() -> None:
    merged = _merge(review=Verdict.NO, citizen_yes=True)
    assert merged.effective_yes is True
    assert merged.recorded == (DisagreementKind.CITIZEN_YES_OVER_REVIEW_NO,)


def test_review_no_citizen_no_is_no() -> None:
    merged = _merge(review=Verdict.NO, citizen_yes=False)
    assert merged.effective_yes is False
    assert merged.weighted_yes is False
    assert merged.recorded == ()


# ---------------------------------------------------------------------------------------
# Row 5: review Unanswered | citizen decides alone (R5) — no disagreement to record
# ---------------------------------------------------------------------------------------


def test_review_unanswered_citizen_yes_decides_yes() -> None:
    merged = _merge(review=Verdict.UNANSWERED, citizen_yes=True)
    assert merged.effective_yes is True
    assert merged.recorded == ()


def test_review_unanswered_citizen_no_decides_no() -> None:
    merged = _merge(review=Verdict.UNANSWERED, citizen_yes=False)
    assert merged.effective_yes is False
    assert merged.recorded == ()


# ---------------------------------------------------------------------------------------
# Rows 6-7: no review at all / review still running → citizen's answer.
# Both reach the merge as `review_verdict=None`; ROUTING those states is ladder rule 4's
# job (U9), never the merge's.
# ---------------------------------------------------------------------------------------


def test_no_review_at_all_citizen_decides() -> None:
    assert _merge(review=None, citizen_yes=True).effective_yes is True
    assert _merge(review=None, citizen_yes=False).effective_yes is False
    assert _merge(review=None, citizen_yes=False).recorded == ()


def test_review_still_running_maps_to_the_same_absent_verdict() -> None:
    # Documented mapping: the caller renders a RUNNING row as "no completed verdict"
    # (`None`); the merge treats it exactly like an absent review, and rule 4 routes.
    merged = _merge(review=None, citizen_yes=False)
    assert merged.effective_yes is False
    assert merged.recorded == ()


# ---------------------------------------------------------------------------------------
# Row 8: any | any | Tier A hit, review ran → the REVIEW'S verdict decides (P8); an
# overrule is recorded as a dispute. The scan never overrides a review that ran — the
# citizen merge still applies on top, exactly as on every other row.
# ---------------------------------------------------------------------------------------


def test_tier_a_with_review_yes_is_yes_and_no_dispute() -> None:
    merged = _merge(review=Verdict.YES, citizen_yes=True, scan=ScanSignal.TIER_A)
    assert merged.effective_yes is True
    assert merged.recorded == ()


def test_tier_a_overrule_is_recorded_as_a_dispute() -> None:
    # The model was SHOWN a Tier A hit and still said No: its No stands against the
    # scan (P8 — the review decides), but the disagreement must reach an administrator.
    merged = _merge(review=Verdict.NO, citizen_yes=False, scan=ScanSignal.TIER_A)
    assert merged.effective_yes is False  # the review's verdict, merged with citizen No
    assert merged.recorded == (DisagreementKind.TIER_A_OVERRULE,)


def test_tier_a_overrule_with_citizen_yes_records_both() -> None:
    merged = _merge(review=Verdict.NO, citizen_yes=True, scan=ScanSignal.TIER_A)
    assert merged.effective_yes is True  # stricter-of with the citizen still applies
    assert set(merged.recorded) == {
        DisagreementKind.TIER_A_OVERRULE,
        DisagreementKind.CITIZEN_YES_OVER_REVIEW_NO,
    }


def test_tier_a_with_review_unanswered_citizen_decides_and_no_dispute() -> None:
    # An overrule is the model saying NO to a shown Tier A hit; an honest abstention is
    # R5's defined state — the citizen decides, and no dispute is recorded.
    merged = _merge(review=Verdict.UNANSWERED, citizen_yes=False, scan=ScanSignal.TIER_A)
    assert merged.effective_yes is False
    assert merged.recorded == ()


# ---------------------------------------------------------------------------------------
# Row 9: no review at all | Tier A hit → Yes — the scan IS the answer when the model
# never returned (P8's second obligation), recorded as standing in.
# ---------------------------------------------------------------------------------------


def test_tier_a_with_no_review_is_yes_the_scan_stands_in() -> None:
    merged = _merge(review=None, citizen_yes=False, scan=ScanSignal.TIER_A)
    assert merged.effective_yes is True
    assert merged.weighted_yes is True
    assert merged.recorded == (DisagreementKind.SCAN_STOOD_IN,)


def test_tier_a_floor_stands_even_when_the_citizen_says_yes() -> None:
    merged = _merge(review=None, citizen_yes=True, scan=ScanSignal.TIER_A)
    assert merged.effective_yes is True
    assert merged.recorded == (DisagreementKind.SCAN_STOOD_IN,)


# ---------------------------------------------------------------------------------------
# Row 10: any | any | Tier B hit → the review's verdict; a lead, not a finding — NOTHING
# is recorded on an overrule, and there is no floor when the model never returned.
# ---------------------------------------------------------------------------------------


def test_tier_b_overrule_records_nothing() -> None:
    merged = _merge(review=Verdict.NO, citizen_yes=False, scan=ScanSignal.TIER_B)
    assert merged.effective_yes is False
    assert merged.recorded == ()


def test_tier_b_with_no_review_has_no_floor() -> None:
    merged = _merge(review=None, citizen_yes=False, scan=ScanSignal.TIER_B)
    assert merged.effective_yes is False
    assert merged.recorded == ()


def test_tier_b_with_review_yes_is_yes_like_any_other_row() -> None:
    merged = _merge(review=Verdict.YES, citizen_yes=False, scan=ScanSignal.TIER_B)
    assert merged.effective_yes is True
    assert merged.recorded == (DisagreementKind.REVIEW_YES_OVER_CITIZEN_NO,)


# ---------------------------------------------------------------------------------------
# Public Data: merges the same way, never routes anything — weight zero.
# ---------------------------------------------------------------------------------------


def test_public_data_merges_the_same_way_but_never_weights() -> None:
    merged = _merge(
        review=Verdict.YES,
        citizen_yes=False,
        weight=_PUBLIC_WEIGHT,
        key="public_data",
    )
    # The effective answer and the recorded disagreement are computed exactly as for a
    # weighted question — only the routing contribution is zero.
    assert merged.effective_yes is True
    assert merged.weighted_yes is False
    assert merged.recorded == (DisagreementKind.REVIEW_YES_OVER_CITIZEN_NO,)


# ---------------------------------------------------------------------------------------
# Aggregation — the shape U9's stricter-of gate consumes.
# ---------------------------------------------------------------------------------------


def test_any_weighted_yes_true_when_a_weighted_category_merges_to_yes() -> None:
    outcome = merge_questions(
        [
            QuestionMergeInput(
                key="credentials_secrets",
                weight=_CREDENTIALS_WEIGHT,
                citizen_yes=False,
                review_verdict=Verdict.YES,
            ),
            QuestionMergeInput(
                key="public_data",
                weight=_PUBLIC_WEIGHT,
                citizen_yes=True,
                review_verdict=Verdict.YES,
            ),
        ]
    )
    assert outcome.any_weighted_yes is True
    assert [merged.key for merged in outcome.questions] == ["credentials_secrets", "public_data"]


def test_a_public_data_only_yes_routes_nothing() -> None:
    outcome = merge_questions(
        [
            QuestionMergeInput(
                key="public_data",
                weight=_PUBLIC_WEIGHT,
                citizen_yes=True,
                review_verdict=Verdict.YES,
            ),
            QuestionMergeInput(
                key="credentials_secrets",
                weight=_CREDENTIALS_WEIGHT,
                citizen_yes=False,
                review_verdict=Verdict.NO,
            ),
        ]
    )
    assert outcome.any_weighted_yes is False


# ---------------------------------------------------------------------------------------
# The full cross product — review verdict × citizen × scan × weighted-vs-Public-Data.
# Invariants that must hold in EVERY cell, so no unnamed combination can hide a hole.
# ---------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("review", "citizen_yes", "scan", "weight"),
    list(
        itertools.product(
            [None, Verdict.YES, Verdict.NO, Verdict.UNANSWERED],
            [True, False],
            list(ScanSignal),
            [_CREDENTIALS_WEIGHT, _PUBLIC_WEIGHT],
        )
    ),
)
def test_full_truth_table_invariants(
    review: Verdict | None, citizen_yes: bool, scan: ScanSignal, weight: int
) -> None:
    merged = _merge(review=review, citizen_yes=citizen_yes, scan=scan, weight=weight)

    # Stricter-of: a citizen Yes is never weakened by anything.
    if citizen_yes:
        assert merged.effective_yes is True
    # A review Yes always stands (its evidence was validated upstream).
    if review is Verdict.YES:
        assert merged.effective_yes is True
    # Weight zero never routes, whatever the effective answer.
    if weight == 0:
        assert merged.weighted_yes is False
    else:
        assert merged.weighted_yes is merged.effective_yes
    # The floor fires exactly when the model never returned AND the scan is Tier A.
    floor = review is None and scan is ScanSignal.TIER_A
    assert (DisagreementKind.SCAN_STOOD_IN in merged.recorded) is floor
    if floor:
        assert merged.effective_yes is True
    # A Tier A overrule is exactly a review No against a shown Tier A hit.
    overruled = review is Verdict.NO and scan is ScanSignal.TIER_A
    assert (DisagreementKind.TIER_A_OVERRULE in merged.recorded) is overruled
    # Tier B never records anything of its own — no dispute kind exists for it.
    if scan is ScanSignal.TIER_B:
        assert DisagreementKind.TIER_A_OVERRULE not in merged.recorded
        assert DisagreementKind.SCAN_STOOD_IN not in merged.recorded
    # The two citizen-vs-review disagreements are recorded exactly on their cells.
    assert (DisagreementKind.REVIEW_YES_OVER_CITIZEN_NO in merged.recorded) is (
        review is Verdict.YES and not citizen_yes
    )
    assert (DisagreementKind.CITIZEN_YES_OVER_REVIEW_NO in merged.recorded) is (
        review is Verdict.NO and citizen_yes
    )
