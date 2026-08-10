"""The data-classification policy.

These tests pin the POLICY, not the plumbing: the weights, the two thresholds, and the fact
that they are independent of one another. That last point is the one worth a test — the two
constants look interchangeable, and collapsing them (or deriving one from the other) is a
change that passes every route test while quietly altering who can deploy.
"""

from __future__ import annotations

from src.services.deploy.classification import (
    AUTO_DEPLOY_AT,
    CLASSIFICATION_KEYS,
    DATA_CLASSIFICATION_QUESTIONS,
    NOTES_REQUIRED_AT,
    declined_categories,
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


def test_an_all_no_declaration_scores_zero_and_cannot_deploy() -> None:
    assert total_weight(_flags()) == 0
    assert not qualifies_for_deploy(_flags())


def test_public_data_is_a_real_answer_that_adds_nothing() -> None:
    """Weighted 0 deliberately — it is an answer, not filler, and must never move a total."""
    assert total_weight(_flags(public_data=True)) == 0


def test_the_two_thresholds_are_independent() -> None:
    """PII + Financial (40) crosses the notes gate and still falls short of the deploy gate.

    This combination is the reason the constants are separate. A future change that derives
    one from the other, or reuses a single number for both, breaks here — which is the point:
    it would silently let every app that must explain itself also deploy itself."""
    flags = _flags(personal_information=True, financial_data=True)
    assert total_weight(flags) == 40
    assert notes_required(flags)
    assert not qualifies_for_deploy(flags)
    assert NOTES_REQUIRED_AT < AUTO_DEPLOY_AT


def test_the_threshold_is_inclusive() -> None:
    """`>=`, not `>`. Health + PII + Confidential is exactly 60; a declaration landing
    precisely ON the threshold deploys."""
    assert total_weight(_flags(health_data=True, personal_information=True)) == 45
    assert not qualifies_for_deploy(_flags(health_data=True, personal_information=True))
    at_threshold = _flags(credentials_secrets=True, personal_information=True)
    assert total_weight(at_threshold) == 60
    assert qualifies_for_deploy(at_threshold)


def test_a_missing_key_counts_as_no_rather_than_raising() -> None:
    """A declaration stored before a question existed must stay readable. Raising would make
    an old deployment row unopenable the moment the questionnaire grows a seventh question."""
    assert total_weight({"credentials_secrets": True}) == 40


def test_declined_categories_omits_the_zero_weight_one() -> None:
    """`Public Data` could never have changed the outcome, so offering it as something to
    reconsider is noise presented as advice."""
    declined = declined_categories(_flags(credentials_secrets=True))
    assert "Public Data" not in declined
    assert "Health Data" in declined
    assert "Credentials / Secrets" not in declined


def test_the_refusal_names_the_score_and_the_threshold() -> None:
    """A refusal without a number is un-actionable — the citizen cannot tell whether they
    were close or nowhere near, so every refusal becomes a question for an administrator."""
    message = refusal_message(_flags(confidential_business_data=True))
    assert "15" in message
    assert str(AUTO_DEPLOY_AT) in message
