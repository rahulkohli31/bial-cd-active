"""add pending_teardowns.kind: a build sandbox or a shared view

Revision ID: 0053_pending_teardown_kind
Revises: 0052_sandbox_starts
Create Date: 2026-10-05

The owed-teardown routine writes a build sandbox back before it destroys it and never writes back
a shared view, and a row's name no longer has to say which. Nullable: the upgrade fills every
existing row from its name prefix, and a row a process older than the column writes afterwards
is read the same way. The downgrade drops the column and its enum type.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0053_pending_teardown_kind"
down_revision: str | None = "0052_sandbox_starts"
branch_labels: str | None = None
depends_on: str | None = None

# create_type=False so this migration owns the type: created in upgrade, dropped in downgrade.
pending_teardown_kind = postgresql.ENUM(
    "build", "shared", name="pending_teardown_kind", create_type=False
)


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    pending_teardown_kind.create(op.get_bind(), checkfirst=True)
    op.add_column("pending_teardowns", sa.Column("kind", pending_teardown_kind, nullable=True))
    op.execute(
        "UPDATE pending_teardowns SET kind = CASE WHEN app_name LIKE 'shr-%' "
        "THEN 'shared'::pending_teardown_kind ELSE 'build'::pending_teardown_kind END"
    )
    op.execute("SET LOCAL lock_timeout = DEFAULT")


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("pending_teardowns", "kind")
    pending_teardown_kind.drop(op.get_bind(), checkfirst=True)
    op.execute("SET LOCAL lock_timeout = DEFAULT")
