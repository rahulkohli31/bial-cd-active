"""project_shares — the platform's first junction table (#198)

Revision ID: 0041_project_shares
Revises: 0040_description_embedding
Create Date: 2026-09-11

WHY THIS EXISTS: sharing a project with a colleague needs a genuine many-to-many, and nothing
in the schema expresses one today — every existing table is a single-owner row under
`OwnedByUserMixin`. See `db/models/project_share.py` for the full design note, in particular
why there is no `shared_by_user_id` column (it would always equal `projects.user_id`) and why
both FKs cascade at the DB level (a share row has no object-store footprint to sweep, unlike
`delete_project_cascade`'s explicit app/conversation deletes).

Revision id kept short deliberately — `alembic_version.version_num` is a `VARCHAR(32)`, and
`0041_project_shares` (20 chars) has room to spare; see `0030_approval_route_declaration.py`'s
own comment about a prior revision that did not.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "0041_project_shares"
down_revision: str | Sequence[str] | None = "0040_description_embedding"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "project_shares",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("shared_with_user_id", sa.Uuid(), nullable=False),
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
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["shared_with_user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        # ONE ROW PER (project, recipient) — the idempotent-re-share invariant and the
        # upsert's ON CONFLICT inference target.
        sa.UniqueConstraint(
            "project_id", "shared_with_user_id", name="uq_project_shares_project_recipient"
        ),
    )
    op.create_index(
        op.f("ix_project_shares_project_id"), "project_shares", ["project_id"], unique=False
    )
    op.create_index(
        op.f("ix_project_shares_shared_with_user_id"),
        "project_shares",
        ["shared_with_user_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_project_shares_shared_with_user_id"), table_name="project_shares")
    op.drop_index(op.f("ix_project_shares_project_id"), table_name="project_shares")
    op.drop_table("project_shares")
