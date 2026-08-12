"""token_usage + user_limits tables

Revision ID: 0004_usage
Revises: 0003_audit_logs
Create Date: 2026-07-06

The server-authoritative daily-token ledger (R13/R30). `token_usage` holds one row per
(user, IST day) with the four token classes split for observability; the unique
(user_id, usage_date) is the atomic-upsert conflict target. `user_limits` holds sparse
per-user overrides (NULL column = use the global default). Hand-finalized from an
autogenerate starting point (ADR-0013). No enums this revision, so no DROP TYPE.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0004_usage"
down_revision: str | None = "0003_audit_logs"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "token_usage",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        # OwnedByUserMixin — the single-tenant ownership boundary (ADR-0004).
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("usage_date", sa.Date(), nullable=False),
        sa.Column("input_tokens", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("output_tokens", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "cache_read_tokens", sa.BigInteger(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "cache_write_tokens", sa.BigInteger(), server_default=sa.text("0"), nullable=False
        ),
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
        # The atomic-upsert conflict target: one row per user per IST day.
        sa.UniqueConstraint("user_id", "usage_date", name="uq_token_usage_user_date"),
    )
    op.create_index(op.f("ix_token_usage_user_id"), "token_usage", ["user_id"], unique=False)

    op.create_table(
        "user_limits",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        # NULL = use the global default; the daily gate consults this first.
        sa.Column("daily_token_limit", sa.BigInteger(), nullable=True),
        sa.Column("context_soft_limit", sa.Integer(), nullable=True),
        sa.Column("context_hard_limit", sa.Integer(), nullable=True),
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
        # One override row per user.
        sa.UniqueConstraint("user_id", name="uq_user_limits_user"),
    )
    op.create_index(op.f("ix_user_limits_user_id"), "user_limits", ["user_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_user_limits_user_id"), table_name="user_limits")
    op.drop_table("user_limits")
    op.drop_index(op.f("ix_token_usage_user_id"), table_name="token_usage")
    op.drop_table("token_usage")
