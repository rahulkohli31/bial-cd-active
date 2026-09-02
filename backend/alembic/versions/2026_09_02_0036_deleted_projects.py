"""deleted_projects — a tombstone per deletion, not a soft-delete flag (#158 §13.3)

The ask was `is_deleted` + `remark` on `projects`. This is a separate table instead, and the
model's docstring carries the argument: the dialog tells the citizen nothing is recoverable
and the server means it (`salt_the_earth` force-drops the project's database), so a
surviving row is a record ABOUT the deletion rather than a project that can come back. A
flag on `projects` would also put `WHERE is_deleted = false` on every read of that table
forever, and missing one resurrects a deleted project.

No foreign key to `projects`: the row it would reference is gone by the time this is
written. `project_id` is stored so an administrator can correlate with audit rows and
deployment history that still name it.

Revision ID: 0036_deleted_projects
Revises: 0035_chat_kind
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID as PGUUID

from alembic import op

revision: str = "0036_deleted_projects"
down_revision: str | None = "0035_chat_kind"
branch_labels: str | None = None
depends_on: str | None = None

_TABLE = "deleted_projects"


def upgrade() -> None:
    op.create_table(
        _TABLE,
        sa.Column("id", PGUUID(as_uuid=True), primary_key=True),
        # Deliberately NOT a ForeignKey — see the module docstring.
        sa.Column("project_id", PGUUID(as_uuid=True), nullable=False),
        sa.Column("project_name", sa.String(120), nullable=False),
        sa.Column("owner_id", PGUUID(as_uuid=True), nullable=False),
        sa.Column("owner_email", sa.String(320), nullable=False),
        sa.Column("deleted_by", PGUUID(as_uuid=True), nullable=False),
        # `deleted_by` in words, stamped from the session — never from the request body.
        # 320 so neither source (`users.display_name` at 256, `users.email` at 320) can
        # ever need truncating. See the model's docstring.
        #
        # NOT NULL with no server_default, which is only free because this column ships in
        # the CREATE rather than in a later ALTER: the table is created empty in the same
        # statement, so there is nothing to backfill.
        sa.Column("deleted_by_name", sa.String(320), nullable=False),
        sa.Column(
            "deleted_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("remark", sa.Text(), nullable=False),
        sa.Column("chats_deleted", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("had_app", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("had_database", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    # The two ways an administrator reads this: "what did this project's deletion say" and
    # "what has this person deleted". Declared on the model too, so `--autogenerate` stays
    # empty (the drift #147 was caught by).
    op.create_index("ix_deleted_projects_project_id", _TABLE, ["project_id"])
    op.create_index("ix_deleted_projects_owner_id", _TABLE, ["owner_id"])


def downgrade() -> None:
    op.drop_index("ix_deleted_projects_owner_id", table_name=_TABLE)
    op.drop_index("ix_deleted_projects_project_id", table_name=_TABLE)
    op.drop_table(_TABLE)
