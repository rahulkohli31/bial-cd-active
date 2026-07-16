"""users.approved_at — the pending-approval marker

Revision ID: 0017_user_approved_at
Revises: 0016_user_suspended_at
Create Date: 2026-07-14

Adds the nullable `approved_at` timestamp to `users`: NULL means the account is
awaiting a super-admin's approval; a timestamp means it was approved at that
instant. Mirrors `suspended_at`'s own nullable-timestamp idiom (0016) but is an
independent, orthogonal marker — a suspended account and a never-approved
account are different states, both derived (never stored) via `User.status()`.

Every row that exists before this ships is backfilled to approved (using its
own `created_at`) so rollout never locks out an existing user — only accounts
created AFTER this migration start out pending.

Chains off `0016_user_suspended_at` to keep the single migration head
(`tests/test_alembic_single_head.py`).
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0017_user_approved_at"
down_revision: str | None = "0016_user_suspended_at"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True))
    op.execute("UPDATE users SET approved_at = created_at WHERE approved_at IS NULL")


def downgrade() -> None:
    op.drop_column("users", "approved_at")
