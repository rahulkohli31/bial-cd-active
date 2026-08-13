"""drop the app_registry.login_required column (issue #92, R22)

Revision ID: 0028_drop_login_required
Revises: 0027_worker_passes
Create Date: 2026-08-13

Whether a generated app needs a sign-in is the app author's decision, expressed by
whether the app's own code has one — not an administrator's switch (R1). The column
was stored, PATCHable, and audited, but nothing ever READ it to gate anything: this
drop is the cleanup half of shipping issue #92's real mechanism (mint + JWKS,
postMessage/launch transport), landing in the SAME phase rather than deferred,
exactly as R22 requires ("leaving it means the next admin-console change quietly
re-wires a control that should not exist").

DESTRUCTIVE + SCHEMA-ONLY ROUND-TRIP: `downgrade` recreates the column's STRUCTURE
(`Boolean NOT NULL server_default=false`), not any admin-set value — mirroring
0020's own `op.drop_column`/`op.add_column` shape. Nothing ever read the column, so
no admin-set `true` was ever load-bearing; the drop loses nothing in practice.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0028_drop_login_required"
down_revision: str | None = "0027_worker_passes"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.drop_column("app_registry", "login_required")


def downgrade() -> None:
    op.add_column(
        "app_registry",
        sa.Column("login_required", sa.Boolean(), server_default=sa.text("false"), nullable=False),
    )
