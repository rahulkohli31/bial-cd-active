"""messages native reset — drop legacy conversations/messages data, rebuild `messages` native

Revision ID: 0024_messages_native_reset
Revises: 0023_drop_data_records
Create Date: 2026-07-23

THE DESTRUCTIVE RESET (plan 2026-07-22-002, R11 — user decision: pilot-phase, dev-local
conversation data only; nothing is migrated). The unified-chat rebuild replaces the SPA-driven
parts-shaped transcript with NATIVE pydantic-ai batches, and the two shapes share nothing worth
carrying, so this revision:

  * DELETEs every conversation row (their messages cascade; `attachments.conversation_id`
    is `ON DELETE SET NULL`, so upload links are severed — orphaned uploads age into the
    never-sent reclaimer's sweep, which also removes their blobs through the proper
    blob-aware path a migration cannot run);
  * DROPs the legacy `messages` table and its `message_role` enum outright;
  * RECREATEs `messages` in the native-batch shape (`entry_kind`/`visibility`/`mode`
    row-classification columns, native JSONB `payload`, system `meta`, server-owned `seq`);
  * adds the server-owned sticky `conversations.mode` (native enum, default `plan`) and
    drops the legacy builder `code` snapshot column (code truth lives in
    `app_registry.current_code` and the build snapshots).

Apps, snapshots, projects, usage, approvals, users are UNTOUCHED. `downgrade` is structure-only
(the legacy shape returns EMPTY — this is a one-way door for data, both ways, by design).
Consumers were reshaped in the same change that ships this revision (append/read endpoints
retired, the store/outcome/attachment readers moved to the native shape) so `Base.metadata` and
the schema never disagree. Hand-finalized (ADR-0013).
"""

from __future__ import annotations

import datetime

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0024_messages_native_reset"
down_revision: str | None = "0023_drop_data_records"
branch_labels: str | None = None
depends_on: str | None = None

# Native enums (ADR-0008) — create_type=False so THIS migration owns each lifecycle.
message_role = postgresql.ENUM("user", "assistant", name="message_role", create_type=False)
conversation_mode = postgresql.ENUM(
    "ask", "plan", "write", name="conversation_mode", create_type=False
)
message_entry_kind = postgresql.ENUM(
    "turn", "step", "system_event", "mode_switch", name="message_entry_kind", create_type=False
)
message_visibility = postgresql.ENUM(
    "visible", "hidden", name="message_visibility", create_type=False
)


def _timestamps() -> tuple[sa.Column[datetime.datetime], sa.Column[datetime.datetime]]:
    # Fresh Column objects per call: a Column binds to the first table that uses it, and the
    # round-trip test runs downgrade + upgrade in ONE process — shared columns would raise.
    return (
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
    )


def upgrade() -> None:
    # The big bang: every legacy conversation goes (messages cascade at the DB level;
    # attachment links sever to NULL via their own FK). DELETE rather than TRUNCATE so the
    # referential actions actually fire.
    op.execute(sa.text("DELETE FROM conversations"))

    # Legacy messages table + its enum, gone for good.
    op.drop_index(op.f("ix_messages_conversation_id"), table_name="messages")
    op.drop_index(op.f("ix_messages_user_id"), table_name="messages")
    op.drop_table("messages")
    message_role.drop(op.get_bind(), checkfirst=True)

    # The unified chat's enums.
    conversation_mode.create(op.get_bind(), checkfirst=True)
    message_entry_kind.create(op.get_bind(), checkfirst=True)
    message_visibility.create(op.get_bind(), checkfirst=True)

    # `messages`, reborn: one row per persisted NATIVE batch (see `db/models/message.py`).
    op.create_table(
        "messages",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("schema_version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("entry_kind", message_entry_kind, nullable=False),
        sa.Column(
            "visibility",
            message_visibility,
            server_default=sa.text("'visible'::message_visibility"),
            nullable=False,
        ),
        sa.Column("mode", conversation_mode, nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("meta", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        *_timestamps(),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        # `seq` is the sole per-conversation ordering key — unique so a two-writer collision
        # is a caught IntegrityError the store retries, never silent ordering corruption.
        sa.UniqueConstraint("conversation_id", "seq", name="uq_messages_conversation_seq"),
    )
    op.create_index(op.f("ix_messages_user_id"), "messages", ["user_id"], unique=False)
    op.create_index(
        op.f("ix_messages_conversation_id"), "messages", ["conversation_id"], unique=False
    )

    # The sticky server-owned chat mode; and the legacy builder code snapshot dies.
    op.add_column(
        "conversations",
        sa.Column(
            "mode",
            conversation_mode,
            server_default=sa.text("'plan'::conversation_mode"),
            nullable=False,
        ),
    )
    op.drop_column("conversations", "code")


def downgrade() -> None:
    # Structure only — the native rows are gone for good, exactly as the legacy rows were on
    # the way up. Exact reverse order.
    op.add_column(
        "conversations",
        sa.Column("code", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.drop_column("conversations", "mode")

    op.drop_index(op.f("ix_messages_conversation_id"), table_name="messages")
    op.drop_index(op.f("ix_messages_user_id"), table_name="messages")
    op.drop_table("messages")
    message_visibility.drop(op.get_bind(), checkfirst=True)
    message_entry_kind.drop(op.get_bind(), checkfirst=True)
    conversation_mode.drop(op.get_bind(), checkfirst=True)

    message_role.create(op.get_bind(), checkfirst=True)
    op.create_table(
        "messages",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("role", message_role, nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("schema_version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("parts", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        *_timestamps(),
        sa.ForeignKeyConstraint(["conversation_id"], ["conversations.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("conversation_id", "seq", name="uq_messages_conversation_seq"),
    )
    op.create_index(op.f("ix_messages_user_id"), "messages", ["user_id"], unique=False)
    op.create_index(
        op.f("ix_messages_conversation_id"), "messages", ["conversation_id"], unique=False
    )
