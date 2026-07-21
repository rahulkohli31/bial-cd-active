"""link attachments to their conversation (R10 / U9)

Revision ID: 0021_attachment_conv_link
Revises: 0020_drop_app_registry_name
Create Date: 2026-07-21

`attachments` had NO link to anything but a client-minted token buried in a message's
`parts` JSONB, so a file uploaded and never sent was reachable by no delete path and
consumed the owner's quota forever (R10). This adds a nullable `conversation_id` FK,
populated at upload time, so an orphan reclaimer has a candidate set and the row has a
referential backstop.

The FK is `ON DELETE SET NULL`, deliberately NOT the `ON DELETE CASCADE` that
`conversations.project_id` uses: under CASCADE, deleting a conversation would destroy
the attachment ROW while its object-store blob survives — manufacturing exactly the
permanent orphan U9 exists to prevent. Blob-aware cleanup stays with the
conversation-delete service (`services/conversations/delete.py`); this FK is only a
row-integrity backstop that NULLs a dangling link.

Nullable is required: existing rows have no conversation, and backfilling the link from
`parts` JSONB is a separate exercise. A NULL link means *legacy*, never *never-sent* —
the reclaimer's eligibility is decided by the `parts` reference scan, not by NULL.

DESTRUCTIVE + SCHEMA-ONLY ROUND-TRIP: `downgrade` drops the FK, index, and column
(structure only — it reconstructs no link data). Hand-finalized (ADR-0013).
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0021_attachment_conv_link"
down_revision: str | None = "0020_drop_app_registry_name"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("attachments", sa.Column("conversation_id", sa.Uuid(), nullable=True))
    op.create_index(
        op.f("ix_attachments_conversation_id"), "attachments", ["conversation_id"], unique=False
    )
    op.create_foreign_key(
        "attachments_conversation_id_fkey",
        "attachments",
        "conversations",
        ["conversation_id"],
        ["id"],
        ondelete="SET NULL",
    )


def downgrade() -> None:
    op.drop_constraint("attachments_conversation_id_fkey", "attachments", type_="foreignkey")
    op.drop_index(op.f("ix_attachments_conversation_id"), table_name="attachments")
    op.drop_column("attachments", "conversation_id")
