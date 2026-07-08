"""app_registry table + app_status enum

Revision ID: 0010_app_registry
Revises: 0003_audit_logs
Create Date: 2026-07-06

The app-lifecycle spine of the deployed-app data plane (Plan B U1, R18/R4). The
row `id` IS the appId (identifier preservation, AE3 — the backfill inserts a
migrated app with its existing appId as the PK, no legacy_id). `app_key` is a
publishable `secrets.token_urlsafe` label (ADR-0006). `status` is a native PG enum
(ADR-0008), owned by this migration (CREATE/DROP TYPE explicit). `conversation_id`
is a soft link (no FK) to Plan A's concurrently-developed `conversations` table.

Plan B migrations use a distinct 0010+ revision band so they never collide with
Plan A's concurrent chain; both branch off 0003_audit_logs and are reconciled
(alembic merge) at integration. Hand-finalized from an autogenerate starting
point (ADR-0013).
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0010_app_registry"
down_revision: str | None = "0003_audit_logs"
branch_labels: str | None = None
depends_on: str | None = None

# The native app_status enum (ADR-0008). create_type=False so THIS migration owns
# the lifecycle: explicit .create() in upgrade, .drop() in downgrade.
app_status = postgresql.ENUM(
    "draft",
    "pending",
    "approved",
    "rejected",
    "disabled",
    name="app_status",
    create_type=False,
)


def upgrade() -> None:
    app_status.create(op.get_bind(), checkfirst=True)
    op.create_table(
        "app_registry",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        # OwnedByUserMixin — the single-tenant ownership boundary (ADR-0004).
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("app_key", sa.String(length=64), nullable=False),
        # Soft link to the builder conversation (Plan A) — no FK by design.
        sa.Column("conversation_id", sa.Uuid(), nullable=True),
        sa.Column("name", sa.String(length=120), server_default="", nullable=False),
        sa.Column("login_required", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "status",
            app_status,
            server_default="draft",
            nullable=False,
        ),
        sa.Column("data_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("data_bytes", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("file_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("file_bytes", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("source_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("approved_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("approved_by", sa.Uuid(), nullable=True),
        sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rejection_note", sa.String(length=1000), nullable=True),
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
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "user_id", "conversation_id", name="uq_app_registry_owner_conversation"
        ),
    )
    op.create_index(op.f("ix_app_registry_user_id"), "app_registry", ["user_id"], unique=False)
    op.create_index(op.f("ix_app_registry_app_key"), "app_registry", ["app_key"], unique=True)
    op.create_index(
        op.f("ix_app_registry_conversation_id"), "app_registry", ["conversation_id"], unique=False
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_app_registry_conversation_id"), table_name="app_registry")
    op.drop_index(op.f("ix_app_registry_app_key"), table_name="app_registry")
    op.drop_index(op.f("ix_app_registry_user_id"), table_name="app_registry")
    op.drop_table("app_registry")
    app_status.drop(op.get_bind(), checkfirst=True)
