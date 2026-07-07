"""clear_data_tokens table

Revision ID: 0013_clear_data_token
Revises: 0012_app_files
Create Date: 2026-07-06

The durable single-use clear-data confirm token (Plan B U8, R29) — replaces Express's
in-memory per-instance Map so the destructive-op gate survives a stateless/multi-worker
backend. App-bound (FK app_registry.id, CASCADE), TTL + single-use redeemed atomically.
Chains off 0012_app_files (Plan B's linear sub-chain). Hand-finalized (ADR-0013).
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0013_clear_data_token"
down_revision: str | None = "0012_app_files"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "clear_data_tokens",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("token", sa.String(length=64), nullable=False),
        sa.Column("app_id", sa.Uuid(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.ForeignKeyConstraint(["app_id"], ["app_registry.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_clear_data_tokens_token"), "clear_data_tokens", ["token"], unique=True
    )
    op.create_index(
        op.f("ix_clear_data_tokens_app_id"), "clear_data_tokens", ["app_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_clear_data_tokens_app_id"), table_name="clear_data_tokens")
    op.drop_index(op.f("ix_clear_data_tokens_token"), table_name="clear_data_tokens")
    op.drop_table("clear_data_tokens")
