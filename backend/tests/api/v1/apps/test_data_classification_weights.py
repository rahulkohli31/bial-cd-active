"""The V4 data-classification weight table — pinned exactly, so an accidental edit
to `DATA_CLASSIFICATION_QUESTIONS` (order, wording, or weight) is caught immediately
rather than silently drifting from the task sheet."""

from __future__ import annotations

from src.api.v1.apps.schemas import DataClassificationAnswers, total_weight
from src.db.models.app_registry import DATA_CLASSIFICATION_QUESTIONS


def test_question_table_matches_the_task_sheet_exactly() -> None:
    assert DATA_CLASSIFICATION_QUESTIONS == (
        ("credentials_secrets", "Credentials / Secrets", 40),
        ("health_data", "Health Data", 25),
        ("personal_information", "Personal Information (PII)", 20),
        ("financial_data", "Financial Data", 20),
        ("confidential_business_data", "Confidential Business Data", 15),
        ("public_data", "Public Data", 0),
    )


def _answers(**overrides: bool) -> DataClassificationAnswers:
    # Every case here that flags a category needs `notes` to satisfy the ≥25 gate
    # (Credentials/Secrets and Health Data both cross it alone) — supplying a fixed
    # explanation unconditionally is simpler than conditioning it on the weight.
    base = {
        "credentials_secrets": False,
        "health_data": False,
        "personal_information": False,
        "financial_data": False,
        "confidential_business_data": False,
        "public_data": False,
        "notes": "n/a",
    }
    base.update(overrides)
    return DataClassificationAnswers.model_validate(base)


def test_total_weight_all_no_is_zero() -> None:
    assert total_weight(_answers()) == 0


def test_total_weight_sums_flagged_categories_only() -> None:
    answers = _answers(public_data=True, confidential_business_data=True)
    assert total_weight(answers) == 15  # public_data contributes 0


def test_credentials_secrets_alone_crosses_the_notes_threshold() -> None:
    assert total_weight(_answers(credentials_secrets=True)) == 40


def test_health_data_alone_crosses_the_notes_threshold() -> None:
    assert total_weight(_answers(health_data=True)) == 25
