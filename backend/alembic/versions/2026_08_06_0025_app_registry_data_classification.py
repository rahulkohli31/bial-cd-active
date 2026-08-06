"""app_registry data classification — the V4 submit-time questionnaire

Revision ID: 0025_data_classification
Revises: 0024_messages_native_reset
Create Date: 2026-08-06

Adds `app_registry.data_classification` (JSONB, nullable): the six-question
Yes/No data-classification questionnaire + optional notes, recorded atomically
with `status`/`source_submission_id`/`source_commit_sha`/`submitted_at` on every
submit (see `apps/router.py::submit`). NULL until the first submit; a
pre-migration submission stays NULL forever (never backfilled) — the app-side
distinguishes "no answers on file" (NULL) from "answered, all No" (a dict of six
`false` values), so this column is additive-only and touches no existing row.

Storage shape (one JSONB blob vs. six typed columns) is still open with
engineering leadership; swapping to six columns later only touches this
migration, the `data_classification` column definition, and the read/write call
sites — `DATA_CLASSIFICATION_QUESTIONS` and `DataClassificationAnswers` are
unaffected either way.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0025_data_classification"
down_revision: str | None = "0024_messages_native_reset"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "app_registry",
        sa.Column("data_classification", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("app_registry", "data_classification")
