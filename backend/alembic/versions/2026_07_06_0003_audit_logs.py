"""audit_logs table

Revision ID: 0003_audit_logs
Revises: 0002_auth_tables
Create Date: 2026-07-06

Append-only accountability (R9, ADR-0005): WHO did WHAT to WHICH resource, never
the record contents. `actor_id` is a NULLABLE FK with ON DELETE SET NULL so the
trail outlives a deleted actor (distinct from OwnedByUserMixin's NOT-NULL CASCADE).
`action` is an open `String`, not a native enum — the gated-action vocabulary grows
per domain, so a new action must not need an `ALTER TYPE`. Hand-finalized from an
autogenerate starting point (ADR-0013). No enums this revision, so no DROP TYPE.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0003_audit_logs"
down_revision: str | None = "0002_auth_tables"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "audit_logs",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        # Nullable + ON DELETE SET NULL (below) — the accountability trail survives
        # the actor's deletion; NULL also covers a system/anonymous actor.
        sa.Column("actor_id", sa.Uuid(), nullable=True),
        sa.Column("action", sa.String(length=64), nullable=False),
        sa.Column("resource_type", sa.String(length=64), nullable=False),
        sa.Column("resource_id", sa.String(length=255), nullable=True),
        sa.Column("detail", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
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
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_audit_logs_actor_id"), "audit_logs", ["actor_id"], unique=False)
    # Composite index for the "audit rows queryable by resource" read path.
    op.create_index(
        "ix_audit_logs_resource", "audit_logs", ["resource_type", "resource_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_audit_logs_resource", table_name="audit_logs")
    op.drop_index(op.f("ix_audit_logs_actor_id"), table_name="audit_logs")
    op.drop_table("audit_logs")
