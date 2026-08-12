"""The data-classification policy.

These tests pin the POLICY, not the plumbing: the weights and the threshold.

Post-issue-#115: the deploy gate runs LOW score = safe = auto-deploy, HIGH score = needs a
human. `AUTO_DEPLOY_MAX_SCORE = 0` means only a fully-clean declaration (nothing sensitive
checked) ever auto-deploys — any weighted category at all routes to a human, regardless of
how small its weight is.

Post-issue-#117 follow-up: `notes_required()` is TIED to `AUTO_DEPLOY_MAX_SCORE`, not a
separate threshold — every declaration that fails the deploy gate is also obliged to explain
itself, and nothing can fail the gate without being asked why. The two used to be
independent (a since-removed `NOTES_REQUIRED_AT = 25` sat strictly inside the refused
region), which meant a declaration could be compelled to explain itself on a refusal that
threw the explanation away unread. That's the case worth a test now: there is no longer a
band that's refused but never asked to explain, nor one that must explain but isn't refused.
"""

from __future__ import annotations

import pytest

from src.services.deploy.classification import (
    AUTO_DEPLOY_MAX_SCORE,
    CLASSIFICATION_KEYS,
    DATA_CLASSIFICATION_QUESTIONS,
    declared_categories,
    notes_required,
    qualifies_for_deploy,
    refusal_message,
    total_weight,
)


def _flags(**yes: bool) -> dict[str, bool]:
    return {key: yes.get(key, False) for key in CLASSIFICATION_KEYS}


def test_the_questionnaire_is_six_questions_with_the_agreed_weights() -> None:
    """The weights ARE the policy. Changing one changes who can publish without review, so
    it should have to be done here, in a diff a reviewer sees, rather than fall out of a
    refactor."""
    assert dict((key, weight) for key, _label, weight in DATA_CLASSIFICATION_QUESTIONS) == {
        "credentials_secrets": 40,
        "health_data": 25,
        "personal_information": 20,
        "financial_data": 20,
        "confidential_business_data": 15,
        "public_data": 0,
    }


def test_an_all_no_declaration_scores_zero_and_can_auto_deploy() -> None:
    """The one and only shape of answer set safe enough to publish with no human review."""
    assert total_weight(_flags()) == 0
    assert qualifies_for_deploy(_flags())


def test_public_data_is_a_real_answer_that_adds_nothing() -> None:
    """Weighted 0 deliberately — it is an answer, not filler, and must never move a total."""
    assert total_weight(_flags(public_data=True)) == 0
    assert qualifies_for_deploy(_flags(public_data=True))


def test_notes_required_and_needing_a_human_are_now_the_same_condition() -> None:
    """Issue #117 follow-up: `notes_required()` is tied to `AUTO_DEPLOY_MAX_SCORE`, not a
    separate threshold. Confidential Business Data alone (15) — the lowest nonzero weight
    the questionnaire can produce — both fails the deploy gate AND obliges an explanation;
    before this, it fell inside the old NOTES_REQUIRED_AT=25 gap: not obliged to explain
    itself, yet still refused. A future change that re-splits the two thresholds, or lets a
    declaration fail one without the other, breaks here."""
    flags = _flags(confidential_business_data=True)
    assert total_weight(flags) == 15
    assert not qualifies_for_deploy(flags)
    assert notes_required(flags)

    # And the boundary itself: exactly AUTO_DEPLOY_MAX_SCORE requires neither.
    clean = _flags()
    assert total_weight(clean) == AUTO_DEPLOY_MAX_SCORE
    assert qualifies_for_deploy(clean)
    assert not notes_required(clean)


def test_a_declaration_needing_a_human_is_never_left_unable_to_explain_why() -> None:
    """Personal Information + Financial Data (40): a mid-range refusal, pinned separately
    from the boundary case above so a regression that only breaks at the lowest nonzero
    weight doesn't slip through."""
    flags = _flags(personal_information=True, financial_data=True)
    assert total_weight(flags) == 40
    assert not qualifies_for_deploy(flags)
    assert notes_required(flags)


def test_the_threshold_is_inclusive_and_any_weighted_yes_fails_it() -> None:
    """`<=`, not `<`: exactly 0 qualifies. Above 0 — even the smallest single category —
    does not, regardless of how far it is from the old-world "50"."""
    assert qualifies_for_deploy(_flags())
    smallest = _flags(confidential_business_data=True)  # the lowest nonzero weight, 15
    assert total_weight(smallest) == 15
    assert not qualifies_for_deploy(smallest)
    highest = _flags(credentials_secrets=True, personal_information=True)
    assert total_weight(highest) == 60
    assert not qualifies_for_deploy(highest)


def test_a_missing_key_counts_as_no_rather_than_raising() -> None:
    """A declaration stored before a question existed must stay readable. Raising would make
    an old deployment row unopenable the moment the questionnaire grows a seventh question.
    This tolerance belongs to `total_weight` alone — see the next test for why the deploy
    gate itself does NOT extend it."""
    assert total_weight({"credentials_secrets": True}) == 40


def test_qualifies_for_deploy_rejects_an_incomplete_mapping_instead_of_scoring_it() -> None:
    """`total_weight`'s per-key omission tolerance is for reading an OLD stored answer set,
    not for scoring a live declaration — a mapping missing every key scores 0 and would
    otherwise silently clear the auto-deploy gate. Not reachable over HTTP today
    (`DataClassificationAnswers` requires all six booleans), but this module's own docstring
    anticipates other callers and a future seventh question; both would auto-qualify an
    incomplete declaration were this not here — the fail-open shape of the #115 bug itself."""
    with pytest.raises(ValueError, match="incomplete declaration"):
        qualifies_for_deploy({})
    with pytest.raises(ValueError, match="incomplete declaration"):
        qualifies_for_deploy({"credentials_secrets": False})


def test_declared_categories_omits_the_zero_weight_one() -> None:
    """`Public Data` could never have changed the outcome, so surfacing it as part of why
    this app needs a human review would be noise presented as an explanation."""
    declared = declared_categories(_flags(credentials_secrets=True, public_data=True))
    assert "Public Data" not in declared
    assert "Credentials / Secrets" in declared
    assert "Health Data" not in declared


def test_the_refusal_names_the_score_and_what_was_declared() -> None:
    """A refusal without a number or a reason is un-actionable — the citizen cannot tell
    whether this is a mistake in their answers or a genuine "this needs a human" outcome."""
    message = refusal_message(_flags(confidential_business_data=True))
    assert "15" in message
    assert "Confidential Business Data" in message
    # The old wording invited "declare more to get published" — the corrected message must
    # never suggest that declaring MORE sensitive categories is the way to auto-deploy.
    assert "to deploy automatically" not in message
