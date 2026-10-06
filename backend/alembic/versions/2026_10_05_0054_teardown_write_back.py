"""add pending_teardowns.write_back: whether a container's tree goes back before it does

Revision ID: 0054_teardown_write_back
Revises: 0052_sandbox_starts
Create Date: 2026-10-05

A start that replaces whatever holds a person's slot owes its delete instead of waiting on it, and
that container's tree is a dead session's or already set aside, so the routine must not write it
back. A shared view is never written back: its existing rows are marked so here, and the reader
knows one an older process writes afterwards by its name. Every other row keeps the write-back it
always had through the server default. The downgrade drops the column.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0054_teardown_write_back"
down_revision: str | None = "0052_sandbox_starts"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column(
        "pending_teardowns",
        sa.Column("write_back", sa.Boolean(), nullable=False, server_default=sa.true()),
    )
    op.execute("UPDATE pending_teardowns SET write_back = false WHERE app_name LIKE 'shr-%'")
    op.execute("SET LOCAL lock_timeout = DEFAULT")


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("pending_teardowns", "write_back")
    op.execute("SET LOCAL lock_timeout = DEFAULT")
