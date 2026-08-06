"""app_registry last_deploy_error — the V4 Part 3 auto-deploy failure marker

Revision ID: 0027_last_deploy_error
Revises: 0026_app_registry_auto_decision
Create Date: 2026-08-06

Adds `app_registry.last_deploy_error` (varchar(1000), nullable): why the most
recent auto-deploy attempt failed, if it did (`services/deploy/provision.py`).
NULL when there has never been a failed attempt, or the last attempt succeeded.
Additive-only — no existing row's meaning changes.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0027_last_deploy_error"
down_revision: str | None = "0026_app_registry_auto_decision"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "app_registry",
        sa.Column("last_deploy_error", sa.String(length=1000), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("app_registry", "last_deploy_error")
