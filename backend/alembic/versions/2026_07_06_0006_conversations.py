"""conversations + messages tables (+ conversation_kind / message_role enums)

Revision ID: 0006_conversations
Revises: 0005_feedback
Create Date: 2026-07-06

User-scoped chat headers + messages (R14, R4). Conversation `id` is the CLIENT-MINTED id
(a builder chat's id IS the deployed appId — preserved across the migration); messages order
by the client-minted `seq` and store the SPA content `parts[]` in JSONB `parts`. `kind` and
`role` are native PG enums (ADR-0008), owned by this migration (explicit CREATE/DROP TYPE).
Chains off this plan's `0005_feedback` branch; the parallel app-data branch reconciles via
`alembic merge heads` at integration. Hand-finalized (ADR-0013).
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0006_conversations"
down_revision: str | None = "0005_feedback"
branch_labels: str | None = None
depends_on: str | None = None

# Native enums (ADR-0008) — create_type=False so THIS migration owns the lifecycle.
conversation_kind = postgresql.ENUM(
    "planning", "assistant", "builder", name="conversation_kind", create_type=False
)
message_role = postgresql.ENUM("user", "assistant", name="message_role", create_type=False)


def upgrade() -> None:
    conversation_kind.create(op.get_bind(), checkfirst=True)
    message_role.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "conversations",
        # Client-minted id (not the mixin default) — preserved across the migration (R4).
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        # OwnedByUserMixin — the single-tenant ownership boundary (ADR-0004).
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("kind", conversation_kind, nullable=False),
        sa.Column("title", sa.Text(), nullable=True),
        sa.Column("context", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("code", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
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
    op.create_index(op.f("ix_conversations_user_id"), "conversations", ["user_id"], unique=False)

    op.create_table(
        "messages",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("role", message_role, nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("schema_version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("parts", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
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
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_messages_user_id"), "messages", ["user_id"], unique=False)
    op.create_index(
        op.f("ix_messages_conversation_id"), "messages", ["conversation_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_messages_conversation_id"), table_name="messages")
    op.drop_index(op.f("ix_messages_user_id"), table_name="messages")
    op.drop_table("messages")
    op.drop_index(op.f("ix_conversations_user_id"), table_name="conversations")
    op.drop_table("conversations")
    message_role.drop(op.get_bind(), checkfirst=True)
    conversation_kind.drop(op.get_bind(), checkfirst=True)
