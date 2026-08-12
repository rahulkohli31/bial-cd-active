"""projects table + one-app-per-project wiring (no data migration)

Revision ID: 0015_projects
Revises: 0014_merge_plan_ab_heads
Create Date: 2026-07-09

Stands up `projects` (the parent container, R1/R2) and wires the one-app-per-project
model (KD-4) against an **empty DB** — this is a PURE-SCHEMA revision with NO data pass
(KD-2). Existing local Postgres data is disposable and dropped; the deployed PoC still
runs on Cosmos, and the real Cosmos→Postgres data migration (which will preserve
appId/appKey/conversation ids and map legacy data into one-app-per-project) is a
SEPARATE future script, not this revision. So `project_id` is added NOT NULL directly
— no nullable→backfill→NOT NULL dance, no Default/General backfill.

Changes:
  * create `projects` (mixin columns; `user_id` FK CASCADE).
  * add NOT NULL `project_id` FK (→ projects.id, ON DELETE CASCADE) to `app_registry`
    and `conversations`. The DB cascade is a row-integrity backstop only — blob-aware
    cleanup runs through the U6 service (KD-3a).
  * add `app_registry.current_code` (JSONB) — the project's live code source of truth
    for continuity (KD-9).
  * swap `uq_app_registry_owner_conversation` (user_id, conversation_id) for
    `uq_app_registry_project` (project_id) — one app per project (KD-4).

Hand-finalized from an autogenerate starting point (ADR-0013). Chains off the single
merged head so `tests/test_alembic_single_head.py` stays green.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0015_projects"
down_revision: str | None = "0014_merge_plan_ab_heads"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # 1. The parent container. Mixin columns mirror every other owned table.
    op.create_table(
        "projects",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        # OwnedByUserMixin — the single-tenant ownership boundary (ADR-0004).
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=120), nullable=False),
        # Optional shared chat context (R15/R16); capped + normalized at the write
        # boundary (KD-8), so the column itself is a plain nullable TEXT.
        sa.Column("description", sa.Text(), nullable=True),
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
    op.create_index(op.f("ix_projects_user_id"), "projects", ["user_id"], unique=False)

    # 2. app_registry: add the project parent (NOT NULL, empty DB — KD-2), swap the
    #    uniqueness to one-app-per-project (KD-4), and add the current-code home (KD-9).
    op.add_column("app_registry", sa.Column("project_id", sa.Uuid(), nullable=False))
    op.add_column(
        "app_registry",
        sa.Column("current_code", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.drop_constraint("uq_app_registry_owner_conversation", "app_registry", type_="unique")
    # `uq_app_registry_project`'s unique index already covers project_id lookups —
    # no separate non-unique index.
    op.create_unique_constraint("uq_app_registry_project", "app_registry", ["project_id"])
    op.create_foreign_key(
        "app_registry_project_id_fkey",
        "app_registry",
        "projects",
        ["project_id"],
        ["id"],
        ondelete="CASCADE",
    )

    # 3. conversations: add the project parent (NOT NULL, empty DB — KD-2).
    op.add_column("conversations", sa.Column("project_id", sa.Uuid(), nullable=False))
    op.create_index(
        op.f("ix_conversations_project_id"), "conversations", ["project_id"], unique=False
    )
    op.create_foreign_key(
        "conversations_project_id_fkey",
        "conversations",
        "projects",
        ["project_id"],
        ["id"],
        ondelete="CASCADE",
    )


def downgrade() -> None:
    """Schema-shape rollback — safe only PRE-DIVERGENCE.

    Recreating `uq_app_registry_owner_conversation` assumes no two apps share a
    (user_id, conversation_id). The NEW schema makes that shape legal — the same
    conversation may head one app per project (proven by
    `tests/db/test_projects_migration.py::test_old_owner_conversation_uniqueness_dropped`)
    — so once such rows exist, the constraint recreation below aborts with a
    UniqueViolation (transactionally: the whole downgrade rolls back cleanly, nothing
    half-applied). Manually resolve conflicting (user_id, conversation_id) groups
    before downgrading a diverged database.
    """
    # conversations: drop the project parent.
    op.drop_constraint("conversations_project_id_fkey", "conversations", type_="foreignkey")
    op.drop_index(op.f("ix_conversations_project_id"), table_name="conversations")
    op.drop_column("conversations", "project_id")

    # app_registry: restore the old owner-conversation uniqueness, drop the new wiring.
    op.drop_constraint("app_registry_project_id_fkey", "app_registry", type_="foreignkey")
    op.drop_constraint("uq_app_registry_project", "app_registry", type_="unique")
    op.create_unique_constraint(
        "uq_app_registry_owner_conversation",
        "app_registry",
        ["user_id", "conversation_id"],
    )
    op.drop_column("app_registry", "current_code")
    op.drop_column("app_registry", "project_id")

    # Finally the container itself.
    op.drop_index(op.f("ix_projects_user_id"), table_name="projects")
    op.drop_table("projects")
