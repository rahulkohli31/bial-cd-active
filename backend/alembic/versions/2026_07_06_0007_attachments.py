"""attachments table

Revision ID: 0007_attachments
Revises: 0006_conversations
Create Date: 2026-07-06

One row per uploaded image/PDF (R16, R4). Bytes live in the object store under
`att/{user_id}/{uuid}`; this row is the metadata + per-user quota ledger (summing `size` is
drift-free, unlike Express's counter). The client-minted `attachment_id` token is unique per
owner (idempotent re-upload). Chains off this plan's `0006_conversations` branch; the parallel
app-data branch reconciles via `alembic merge heads` at integration. Hand-finalized (ADR-0013);
no enums, so no DROP TYPE.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0007_attachments"
down_revision: str | None = "0006_conversations"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "attachments",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        # OwnedByUserMixin — the single-tenant ownership boundary (ADR-0004).
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("attachment_id", sa.String(length=128), nullable=False),
        sa.Column("media_type", sa.String(length=255), nullable=False),
        sa.Column("name", sa.String(length=512), server_default="", nullable=False),
        sa.Column("size", sa.BigInteger(), nullable=False),
        sa.Column("storage_key", sa.String(length=512), nullable=False),
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
        # Idempotent re-upload: one row per (owner, client attachment id).
        sa.UniqueConstraint("user_id", "attachment_id", name="uq_attachments_owner_attachment"),
    )
    op.create_index(op.f("ix_attachments_user_id"), "attachments", ["user_id"], unique=False)
    op.create_index(
        op.f("ix_attachments_attachment_id"), "attachments", ["attachment_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_attachments_attachment_id"), table_name="attachments")
    op.drop_index(op.f("ix_attachments_user_id"), table_name="attachments")
    op.drop_table("attachments")
