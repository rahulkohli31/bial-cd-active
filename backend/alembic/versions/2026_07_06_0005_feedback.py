"""feedback table

Revision ID: 0005_feedback
Revises: 0004_usage
Create Date: 2026-07-06

Citizen feedback submissions (R30). One row per message: author (`user_id`), the trimmed
`message` (TEXT; the 4000-byte cap is enforced at the API boundary), and a sanitized
same-origin `page` path (or empty). No rating — feedback is message + page only. Chains off
this plan's `0004_usage` branch; the parallel `app_registry` branch reconciles via
`alembic merge heads` at integration. Hand-finalized (ADR-0013); no enums, so no DROP TYPE.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0005_feedback"
down_revision: str | None = "0004_usage"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "feedback",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        # OwnedByUserMixin — the author; ON DELETE CASCADE (a deleted user's feedback goes).
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("page", sa.String(length=256), server_default="", nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_feedback_user_id"), "feedback", ["user_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_feedback_user_id"), table_name="feedback")
    op.drop_table("feedback")
