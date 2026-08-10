"""The data-classification policy.

These tests pin the POLICY, not the plumbing: the weights, the two thresholds, and the fact
that they are independent of one another. That last point is the one worth a test — the two
constants look interchangeable, and collapsing them (or deriving one from the other) is a
change that passes every route test while quietly altering who can deploy.

Post-issue-#115: the deploy gate runs LOW score = safe = auto-deploy, HIGH score = needs a
human. `AUTO_DEPLOY_MAX_SCORE = 0` means only a fully-clean declaration (nothing sensitive
checked) ever auto-deploys — any weighted category at all routes to a human, regardless of
how small its weight is.
"""

from __future__ import annotations

from src.services.deploy.classification import (
    AUTO_DEPLOY_MAX_SCORE,
    CLASSIFICATION_KEYS,
    DATA_CLASSIFICATION_QUESTIONS,
    NOTES_REQUIRED_AT,
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


def test_the_deploy_gate_is_stricter_than_the_notes_gate() -> None:
    """Confidential Business Data alone (15) doesn't cross the notes-required threshold (25)
    — the citizen isn't obliged to explain it — but it still isn't safe enough to auto-deploy.

    This is the case worth pinning: the two gates are NOT "notes required == needs a human,
    no notes == auto-deploy". `AUTO_DEPLOY_MAX_SCORE` (0) is stricter than `NOTES_REQUIRED_AT`
    (25) by construction, so every declaration below the notes threshold can still fail the
    deploy gate. A future change that derives one constant from the other, or treats
    "notes not required" as a signal it's safe to auto-deploy, breaks here."""
    flags = _flags(confidential_business_data=True)
    assert total_weight(flags) == 15
    assert not notes_required(flags)
    assert not qualifies_for_deploy(flags)
    assert AUTO_DEPLOY_MAX_SCORE < NOTES_REQUIRED_AT


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
    an old deployment row unopenable the moment the questionnaire grows a seventh question."""
    assert total_weight({"credentials_secrets": True}) == 40


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
