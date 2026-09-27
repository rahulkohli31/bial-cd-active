"""Drop the manual go-live route — approval is the only way a reviewed app goes live

Revision ID: 0048_drop_manual_go_live
Revises: 0047_drop_connector_access
Create Date: 2026-09-26

`app_registry` loses the four columns that existed only for an administrator taking an approved
app live by hand: `approval_route`, which recorded whether a submission entered through that route
or through publishing, and `deployed_submission_id`, `deployed_at` and `deployed_url`, the
hand-recorded deployment and its address. The `approval_route` type goes with its column.

The labels and the shapes are written out as literals, never imported — a migration is a historical
record (ADR-0008) — and the type's lifecycle is explicit because dropping a column does not drop
its enum type.

`downgrade` recreates the columns empty: the hand-recorded addresses are gone at upgrade. An app
recorded as live that way publishes through the platform instead: its approved copy on the owner's
one button when one exists, otherwise a fresh send for review.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0048_drop_manual_go_live"
down_revision: str | None = "0047_drop_connector_access"
branch_labels: str | None = None
depends_on: str | None = None

approval_route = postgresql.ENUM(
    "runbook",
    "self_publish",
    name="approval_route",
    create_type=False,
)


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_column("app_registry", "approval_route")
    op.drop_column("app_registry", "deployed_url")
    op.drop_column("app_registry", "deployed_at")
    op.drop_column("app_registry", "deployed_submission_id")
    op.execute("SET LOCAL lock_timeout = DEFAULT")
    approval_route.drop(op.get_bind(), checkfirst=True)


def downgrade() -> None:
    approval_route.create(op.get_bind(), checkfirst=True)
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.add_column("app_registry", sa.Column("deployed_submission_id", sa.Uuid(), nullable=True))
    op.add_column(
        "app_registry", sa.Column("deployed_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("app_registry", sa.Column("deployed_url", sa.String(length=2083), nullable=True))
    op.add_column("app_registry", sa.Column("approval_route", approval_route, nullable=True))
    op.execute("SET LOCAL lock_timeout = DEFAULT")
