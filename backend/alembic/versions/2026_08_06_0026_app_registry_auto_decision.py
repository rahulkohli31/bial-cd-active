"""app_registry auto decision — the V4 Part 2 score-gated auto-approve/reject marker

Revision ID: 0026_app_registry_auto_decision
Revises: 0025_app_registry_data_classification
Create Date: 2026-08-06

Adds `app_registry.decided_automatically` (boolean, not null, default false): a durable,
row-level record of whether the current approve/reject decision was made by `submit`'s
score gate (V4 Part 2 — see `apps/router.py::submit`) rather than a human admin via the
`approve`/`reject` endpoints. Default `false` leaves every existing row (all human-decided,
or never decided) unchanged; `submit` sets it `true` on every decision it makes from here on.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0026_app_registry_auto_decision"
down_revision: str | None = "0025_app_registry_data_classification"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "app_registry",
        sa.Column(
            "decided_automatically",
            sa.Boolean(),
            server_default=sa.text("false"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_column("app_registry", "decided_automatically")
