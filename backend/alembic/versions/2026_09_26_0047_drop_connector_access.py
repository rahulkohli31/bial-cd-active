"""Drop the per-person connector access ledger — the project's switch is the whole of access

Revision ID: 0047_drop_connector_access
Revises: 0046_conversation_updated_at
Create Date: 2026-09-26

`connector_access_requests` recorded who asked to read a connector and what an administrator
decided. A project's own switch now decides alone, so the ledger, its indexes and the
`connector_request_status` type go. `project_connectors` and `connector_window_kind` — the switch
and the days — are untouched.

The labels and the shape are written out as literals, never imported — a migration is a
historical record (ADR-0008) — and the type's lifecycle is explicit because dropping a table does
not drop its enum type.

`downgrade` recreates the structure, empty: the rows are gone at upgrade. Treat an upgrade past
this revision on a live database as a data-loss operation, and dump the table first if its history
matters.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0047_drop_connector_access"
down_revision: str | None = "0046_conversation_updated_at"
branch_labels: str | None = None
depends_on: str | None = None

connector_request_status = postgresql.ENUM(
    "pending",
    "approved",
    "declined",
    "cancelled",
    name="connector_request_status",
    create_type=False,
)


def upgrade() -> None:
    # The table's own indexes go with it. Dropping its foreign keys also locks `users`.
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_table("connector_access_requests")
    op.execute("SET LOCAL lock_timeout = DEFAULT")
    connector_request_status.drop(op.get_bind(), checkfirst=True)


def downgrade() -> None:
    connector_request_status.create(op.get_bind(), checkfirst=True)
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.create_table(
        "connector_access_requests",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("connector_key", sa.String(length=32), nullable=False),
        sa.Column(
            "status",
            connector_request_status,
            server_default="pending",
            nullable=False,
        ),
        sa.Column("requester_remarks", sa.Text(), nullable=False),
        sa.Column("decided_by_id", sa.Uuid(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_remarks", sa.Text(), nullable=True),
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
        sa.ForeignKeyConstraint(["decided_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.execute("SET LOCAL lock_timeout = DEFAULT")
    op.create_index(
        op.f("ix_connector_access_requests_user_id"),
        "connector_access_requests",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        "ix_connector_access_requests_user_connector",
        "connector_access_requests",
        ["user_id", "connector_key"],
        unique=False,
    )
    # The predicate stays a literal, exactly as 0039 wrote it.
    op.create_index(
        "uq_connector_access_requests_one_pending",
        "connector_access_requests",
        ["user_id", "connector_key"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )
