"""add the sandbox_starts table: one row per sandbox start, with its stage timings

Revision ID: 0052_sandbox_starts
Revises: 0051_name_untitled_chats
Create Date: 2026-10-05

See `db/models/sandbox_start.py` for what a row records. Its two foreign keys take a lock on
`users` and `app_registry`, which nearly every request reads and writes, so each half bounds its
lock wait. The downgrade drops the table and its four enum types.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0052_sandbox_starts"
down_revision: str | None = "0051_name_untitled_chats"
branch_labels: str | None = None
depends_on: str | None = None

# create_type=False so this migration owns each type: created in upgrade, dropped in downgrade.
sandbox_start_kind = postgresql.ENUM(
    "reopen",
    "new_project",
    "chat",
    "switch",
    "shared_view",
    name="sandbox_start_kind",
    create_type=False,
)
sandbox_project_type = postgresql.ENUM(
    "plain", "connector", name="sandbox_project_type", create_type=False
)
sandbox_start_miss = postgresql.ENUM(
    "no_ready",
    "unhealthy",
    "claim_failed",
    "size_zero",
    name="sandbox_start_miss",
    create_type=False,
)
sandbox_start_outcome = postgresql.ENUM(
    "served", "failed", name="sandbox_start_outcome", create_type=False
)
_TYPES = (sandbox_start_kind, sandbox_project_type, sandbox_start_miss, sandbox_start_outcome)


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    for enum_type in _TYPES:
        enum_type.create(op.get_bind(), checkfirst=True)
    op.create_table(
        "sandbox_starts",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        # OwnedByUserMixin — the single-tenant ownership boundary.
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("app_id", sa.Uuid(), nullable=False),
        sa.Column("kind", sandbox_start_kind, nullable=False),
        sa.Column("project_type", sandbox_project_type, nullable=False),
        sa.Column("claimed", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("miss_reason", sandbox_start_miss, nullable=True),
        sa.Column("ready_count", sa.Integer(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("outcome", sandbox_start_outcome, nullable=True),
        sa.Column("admission_ms", sa.Integer(), nullable=True),
        sa.Column("settings_ms", sa.Integer(), nullable=True),
        sa.Column("create_ms", sa.Integer(), nullable=True),
        sa.Column("dev_start_ms", sa.Integer(), nullable=True),
        sa.Column("first_page_ms", sa.Integer(), nullable=True),
        sa.Column("browser_visible_ms", sa.Integer(), nullable=True),
        sa.Column(
            "sub_steps",
            postgresql.JSONB(astext_type=sa.Text()),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("reinstalled", sa.Boolean(), nullable=True),
        sa.ForeignKeyConstraint(["app_id"], ["app_registry.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_sandbox_starts_user_id"), "sandbox_starts", ["user_id"], unique=False)
    op.create_index(op.f("ix_sandbox_starts_app_id"), "sandbox_starts", ["app_id"], unique=False)
    op.create_index(
        op.f("ix_sandbox_starts_started_at"), "sandbox_starts", ["started_at"], unique=False
    )
    op.execute("SET LOCAL lock_timeout = DEFAULT")


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.drop_index(op.f("ix_sandbox_starts_started_at"), table_name="sandbox_starts")
    op.drop_index(op.f("ix_sandbox_starts_app_id"), table_name="sandbox_starts")
    op.drop_index(op.f("ix_sandbox_starts_user_id"), table_name="sandbox_starts")
    op.drop_table("sandbox_starts")
    for enum_type in reversed(_TYPES):
        enum_type.drop(op.get_bind(), checkfirst=True)
    op.execute("SET LOCAL lock_timeout = DEFAULT")
