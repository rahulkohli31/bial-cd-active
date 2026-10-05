"""add the sandbox_pool table: the ledger of containers made ahead of time for the pool

Revision ID: 0055_sandbox_pool
Revises: 0054_teardown_write_back
Create Date: 2026-10-05

See `db/models/sandbox_pool.py` for what a row records. A new table with no foreign keys, so it
locks nothing an existing request touches. The downgrade drops the table and its enum type.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0055_sandbox_pool"
down_revision: str | None = "0054_teardown_write_back"
branch_labels: str | None = None
depends_on: str | None = None

# create_type=False so this migration owns the type: created in upgrade, dropped in downgrade.
sandbox_pool_state = postgresql.ENUM(
    "filling", "ready", "claimed", "retiring", name="sandbox_pool_state", create_type=False
)


def upgrade() -> None:
    sandbox_pool_state.create(op.get_bind(), checkfirst=True)
    op.create_table(
        "sandbox_pool",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("name", sa.String(length=32), nullable=False),
        sa.Column("fqdn", sa.Text(), nullable=True),
        sa.Column("image_ref", sa.Text(), nullable=False),
        sa.Column("state", sandbox_pool_state, nullable=False),
        sa.Column("state_changed_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_sandbox_pool_name"),
        sa.CheckConstraint("name ~ '^sbx-[0-9a-f]{28}$'", name="ck_sandbox_pool_name_shape"),
    )


def downgrade() -> None:
    op.drop_table("sandbox_pool")
    sandbox_pool_state.drop(op.get_bind(), checkfirst=True)
