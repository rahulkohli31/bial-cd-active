"""one row per version the citizen saved

Revision ID: 0044_app_versions
Revises: 0043_pending_teardown
Create Date: 2026-09-18

The list of saved versions and the two facts a git bundle cannot carry: the citizen's own
description, and the time the version was saved. The save time was previously read off the object
store's ``last_modified``, which works only while there is exactly one saved copy.

Purely additive. Apps that are already live need no backfill — their live entry resolves from the
deployment record they already have, so a row here is only ever minted by a Save or a rollback
from this point on.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0044_app_versions"
down_revision: str = "0043_pending_teardown"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "app_versions",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("app_id", sa.Uuid(), nullable=False),
        sa.Column("saved_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("description", sa.String(length=120), nullable=True),
        sa.Column("head_sha", sa.String(length=40), nullable=False),
        sa.Column("blob_key", sa.Text(), nullable=False),
        sa.Column("restored_from_id", sa.Uuid(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["app_id"], ["app_registry.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        # SET NULL rather than CASCADE: a version is never deleted by the list, but if one ever
        # goes with its app the lineage pointer must not take an unrelated sibling with it.
        sa.ForeignKeyConstraint(["restored_from_id"], ["app_versions.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_app_versions_user_id"), "app_versions", ["user_id"])
    op.create_index(op.f("ix_app_versions_app_id"), "app_versions", ["app_id"])
    # Every read is "the most recent few for this app", so the index carries the sort.
    op.create_index(
        "ix_app_versions_app_saved_at",
        "app_versions",
        ["app_id", sa.text("saved_at DESC")],
    )


def downgrade() -> None:
    op.drop_index("ix_app_versions_app_saved_at", table_name="app_versions")
    op.drop_index(op.f("ix_app_versions_app_id"), table_name="app_versions")
    op.drop_index(op.f("ix_app_versions_user_id"), table_name="app_versions")
    op.drop_table("app_versions")
