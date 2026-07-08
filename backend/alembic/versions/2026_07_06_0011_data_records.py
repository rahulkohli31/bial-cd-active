"""data_records table

Revision ID: 0011_data_records
Revises: 0010_app_registry
Create Date: 2026-07-06

The per-app data-service store (Plan B U4, R22/R24). App-scoped (FK app_registry.id,
CASCADE) — every query MUST carry `WHERE app_id`; a dropped predicate is a cross-app
leak. `search_text` is the derived free-text projection matched by substring ILIKE
(U5). Chains off 0010_app_registry (Plan B's own linear sub-chain off 0003).
Hand-finalized from an autogenerate starting point (ADR-0013).
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0011_data_records"
down_revision: str | None = "0010_app_registry"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "data_records",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("app_id", sa.Uuid(), nullable=False),
        sa.Column("collection", sa.String(length=64), server_default="default", nullable=False),
        sa.Column("data", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column(
            "created_in_draft", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("bytes", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("search_text", sa.Text(), nullable=True),
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
    op.create_index(op.f("ix_data_records_app_id"), "data_records", ["app_id"], unique=False)
    # The hot read path: records for one app in one collection.
    op.create_index(
        "ix_data_records_app_collection", "data_records", ["app_id", "collection"], unique=False
    )
    # The list/search default orderings (newest-first by created/updated) per app.
    op.create_index(
        "ix_data_records_app_created", "data_records", ["app_id", "created_at"], unique=False
    )
    op.create_index(
        "ix_data_records_app_updated", "data_records", ["app_id", "updated_at"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_data_records_app_updated", table_name="data_records")
    op.drop_index("ix_data_records_app_created", table_name="data_records")
    op.drop_index("ix_data_records_app_collection", table_name="data_records")
    op.drop_index(op.f("ix_data_records_app_id"), table_name="data_records")
    op.drop_table("data_records")
