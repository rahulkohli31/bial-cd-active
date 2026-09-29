"""users.has_signed_in — whether the person has ever signed in

Revision ID: 0050_user_has_signed_in
Revises: 0049_classification_config
Create Date: 2026-09-29

A non-null boolean whose server default, true, is also the backfill: every existing row came from
a sign-in. Only a user created from the directory before their first sign-in carries false. A
constant default makes the add a catalog change with no table rewrite, but it still takes an
ACCESS EXCLUSIVE lock on `users`, which every request reads, so each half bounds its lock wait.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0050_user_has_signed_in"
down_revision: str | None = "0049_classification_config"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column(
        "users",
        sa.Column("has_signed_in", sa.Boolean(), server_default=sa.text("true"), nullable=False),
    )
    op.execute("SET LOCAL lock_timeout = DEFAULT")


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("users", "has_signed_in")
    op.execute("SET LOCAL lock_timeout = DEFAULT")
