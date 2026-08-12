"""users.suspended_at — the local suspension marker

Revision ID: 0016_user_suspended_at
Revises: 0015_projects
Create Date: 2026-07-09

Adds the nullable `suspended_at` timestamp to `users` (R10–R13, KD-6): NULL means
active; a timestamp means a super-admin blocked the account platform-side at that
instant. Nullable-with-no-default is the correct shape (every existing and new row
starts active), so this is a plain additive column — trivially reversible.

Chains off `0015_projects` to keep the single migration head
(`tests/test_alembic_single_head.py`).
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0016_user_suspended_at"
down_revision: str | None = "0015_projects"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("suspended_at", sa.DateTime(timezone=True), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "suspended_at")
