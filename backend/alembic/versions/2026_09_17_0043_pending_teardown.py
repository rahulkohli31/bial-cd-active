"""add the pending_teardowns table for the outgoing-container debt

Revision ID: 0043_pending_teardown
Revises: 0042_merge_shares_connectors
Create Date: 2026-09-17

WHY THIS EXISTS: see `db/models/pending_teardown.py` for the full design note. In short — a
switch overwrites the per-user registry with the incoming container on the same request that
stops using the outgoing one, so nothing in Redis can still reach it. This table is the durable
claim that can: one row per owed deletion, unique on the container name, discriminated from a
same-named recreate by `instance_ref` (the registry's own `created_at`, not the ARM resource id,
which is deterministic from the name alone and so cannot tell two containers apart).

`app_id` and `project_id` carry no ForeignKey — the row must outlive the app or project it names,
the same reasoning `deleted_projects` (0036) already applies to its own `project_id`.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0043_pending_teardown"
down_revision: str | None = "0042_merge_shares_connectors"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "pending_teardowns",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        # OwnedByUserMixin — the single-tenant ownership boundary.
        sa.Column("user_id", sa.Uuid(), nullable=False),
        # No ForeignKey — see the module docstring.
        sa.Column("app_id", sa.Uuid(), nullable=False),
        sa.Column("app_name", sa.String(length=32), nullable=False),
        # No ForeignKey — see the module docstring.
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("instance_ref", sa.DateTime(timezone=True), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=True),
        sa.Column("attempts", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("claimed_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
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
        # The concurrency claim's own `ON CONFLICT` inference target — at most one owed
        # deletion may exist per container name at a time.
        sa.UniqueConstraint("app_name", name="uq_pending_teardowns_app_name"),
    )
    op.create_index(
        op.f("ix_pending_teardowns_user_id"), "pending_teardowns", ["user_id"], unique=False
    )
    op.create_index(
        op.f("ix_pending_teardowns_app_id"), "pending_teardowns", ["app_id"], unique=False
    )
    op.create_index(
        op.f("ix_pending_teardowns_project_id"), "pending_teardowns", ["project_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_pending_teardowns_project_id"), table_name="pending_teardowns")
    op.drop_index(op.f("ix_pending_teardowns_app_id"), table_name="pending_teardowns")
    op.drop_index(op.f("ix_pending_teardowns_user_id"), table_name="pending_teardowns")
    op.drop_table("pending_teardowns")
