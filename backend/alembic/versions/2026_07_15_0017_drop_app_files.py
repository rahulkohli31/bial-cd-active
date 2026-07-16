"""drop app_files table + app_file_status enum

Revision ID: 0017_drop_app_files
Revises: 0016_user_suspended_at
Create Date: 2026-07-15

Retire the per-app file model (OPEN-SANDBOX). Every consumer was removed first —
files_router/parse_router serving (U10), admin governance + project-delete off
`AppFile` (U9), the `Storage` relocation (U8) — so this drop breaks nothing on read.
`app_files` has one outbound FK (app_registry.id CASCADE) and NO inbound FKs, so the
drop is self-contained; it owns the native PG enum `app_file_status` (ADR-0008).

DESTRUCTIVE: `upgrade` deletes real rows. A pre-drop safety gate (row-count / empty
check, or an export of the rows + their `blob_key`s for a deferred blob-GC) MUST run in
the target environment BEFORE this is applied there — consumer removal proves nothing
breaks on read, but only that gate protects the rows themselves. `downgrade` recreates the
STRUCTURE (table + enum + indexes) but NOT the data.

Mirrors 0012_app_files in reverse (its downgrade is this upgrade, its upgrade is this
downgrade). Hand-finalized (ADR-0013).
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0017_drop_app_files"
down_revision: str | None = "0016_user_suspended_at"
branch_labels: str | None = None
depends_on: str | None = None

# The native PG enum app_files owns — copied verbatim from 0012 (create_type=False so
# `.drop(checkfirst=True)` targets the existing type without Alembic auto-managing it).
app_file_status = postgresql.ENUM(
    "pending",
    "ready",
    name="app_file_status",
    create_type=False,
)


def upgrade() -> None:
    op.drop_index("ix_app_files_app_status", table_name="app_files")
    op.drop_index("ix_app_files_app_collection", table_name="app_files")
    op.drop_index(op.f("ix_app_files_app_id"), table_name="app_files")
    op.drop_table("app_files")
    app_file_status.drop(op.get_bind(), checkfirst=True)


def downgrade() -> None:
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
