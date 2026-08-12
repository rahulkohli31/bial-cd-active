"""auth tables (users, refresh_tokens)

Revision ID: 0002_auth_tables
Revises: 0001_enable_extensions
Create Date: 2026-07-02

The first real tables in the control-plane. `users` fulfils the promise
`OwnedByUserMixin` already FKs; `refresh_tokens` carries the rotation/reuse-detection
state denormalized onto each row (family_id + used_at + revoked + absolute_expires_at
— KD-7, no separate token_families table). Hand-finalized from an autogenerate
starting point (ADR-0013). `users` is created before `refresh_tokens` for the FK;
`downgrade()` drops them in reverse. No enums this phase (role/RBAC deferred), so no
DROP TYPE.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0002_auth_tables"
down_revision: str | None = "0001_enable_extensions"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("azure_oid", sa.String(length=64), nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("upn", sa.String(length=320), nullable=True),
        sa.Column("display_name", sa.String(length=256), nullable=True),
        sa.Column("token_version", sa.Integer(), server_default=sa.text("0"), nullable=False),
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
        sa.PrimaryKeyConstraint("id"),
    )
    # Unique index on the Entra Object ID — the upsert key (a returning sign-in
    # resolves the same row; a concurrent first sign-in can't create a duplicate).
    op.create_index(op.f("ix_users_azure_oid"), "users", ["azure_oid"], unique=True)

    op.create_table(
        "refresh_tokens",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        # OwnedByUserMixin — the single-tenant ownership boundary (ADR-0004), FK to
        # users.id with ON DELETE CASCADE (a deleted user's tokens go with it).
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("family_id", sa.Uuid(), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("absolute_expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("revoked", sa.Boolean(), server_default=sa.text("false"), nullable=False),
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
    )
    op.create_index(
        op.f("ix_refresh_tokens_family_id"), "refresh_tokens", ["family_id"], unique=False
    )
    op.create_index(
        op.f("ix_refresh_tokens_token_hash"), "refresh_tokens", ["token_hash"], unique=True
    )
    op.create_index(op.f("ix_refresh_tokens_user_id"), "refresh_tokens", ["user_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_refresh_tokens_user_id"), table_name="refresh_tokens")
    op.drop_index(op.f("ix_refresh_tokens_token_hash"), table_name="refresh_tokens")
    op.drop_index(op.f("ix_refresh_tokens_family_id"), table_name="refresh_tokens")
    op.drop_table("refresh_tokens")
    op.drop_index(op.f("ix_users_azure_oid"), table_name="users")
    op.drop_table("users")
