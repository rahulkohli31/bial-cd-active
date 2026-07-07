"""app_files table + app_file_status enum

Revision ID: 0012_app_files
Revises: 0011_data_records
Create Date: 2026-07-06

Per-app file metadata (Plan B U6, R25). App-scoped (FK app_registry.id, CASCADE);
the blob lives at `apps/{app_id}/{id}` in the object store. `status` is a native PG
enum (ADR-0008) driving the pending→ready two-store choreography. Chains off
0011_data_records (Plan B's linear sub-chain). Hand-finalized from an autogenerate
starting point (ADR-0013).
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0012_app_files"
down_revision: str | None = "0011_data_records"
branch_labels: str | None = None
depends_on: str | None = None

app_file_status = postgresql.ENUM(
    "pending",
    "ready",
    name="app_file_status",
    create_type=False,
)


def upgrade() -> None:
    app_file_status.create(op.get_bind(), checkfirst=True)
    op.create_table(
        "app_files",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("app_id", sa.Uuid(), nullable=False),
        sa.Column("collection", sa.String(length=64), server_default="default", nullable=False),
        sa.Column("filename", sa.String(length=200), nullable=False),
        sa.Column("content_type", sa.String(length=255), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("blob_key", sa.String(length=512), nullable=False),
        sa.Column("status", app_file_status, server_default="pending", nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column(
            "created_in_draft", sa.Boolean(), server_default=sa.text("false"), nullable=False
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
        sa.ForeignKeyConstraint(["app_id"], ["app_registry.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_app_files_app_id"), "app_files", ["app_id"], unique=False)
    op.create_index(
        "ix_app_files_app_collection", "app_files", ["app_id", "collection"], unique=False
    )
    op.create_index("ix_app_files_app_status", "app_files", ["app_id", "status"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_app_files_app_status", table_name="app_files")
    op.drop_index("ix_app_files_app_collection", table_name="app_files")
    op.drop_index(op.f("ix_app_files_app_id"), table_name="app_files")
    op.drop_table("app_files")
    app_file_status.drop(op.get_bind(), checkfirst=True)
