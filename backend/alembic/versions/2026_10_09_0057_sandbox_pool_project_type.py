"""sandbox_pool gains project_type: which pool a container was made for

Revision ID: 0057_sandbox_pool_project_type
Revises: 0056_start_miss_connector
Create Date: 2026-10-09

The column reuses `sandbox_project_type`, which 0052 owns, so this revision neither creates nor
drops the type. The default is `plain` because an older release writes rows without the column,
and every container it makes for the pool is plain. Adding a column with a constant default
rewrites nothing, but takes ACCESS EXCLUSIVE on a table every claim and fill writes, so the lock
wait is bounded.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0057_sandbox_pool_project_type"
down_revision: str | None = "0056_start_miss_connector"
branch_labels: str | None = None
depends_on: str | None = None

sandbox_project_type = postgresql.ENUM(
    "plain", "connector", name="sandbox_project_type", create_type=False
)


def upgrade() -> None:
    op.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
    op.add_column(
        "sandbox_pool",
        sa.Column(
            "project_type",
            sandbox_project_type,
            nullable=False,
            server_default=sa.text("'plain'"),
        ),
    )
    op.execute(sa.text("SET LOCAL lock_timeout = DEFAULT"))


def downgrade() -> None:
    op.execute(sa.text("SET LOCAL lock_timeout = '5s'"))
    op.drop_column("sandbox_pool", "project_type")
    op.execute(sa.text("SET LOCAL lock_timeout = DEFAULT"))
