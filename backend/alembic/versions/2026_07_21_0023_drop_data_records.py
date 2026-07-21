"""drop data_records + clear_data_tokens and the app_registry counter columns

Revision ID: 0023_drop_data_records
Revises: 0022_project_database_registry
Create Date: 2026-07-21

The end of the shared data plane. Every app's data now lives in the project's OWN
PostgreSQL database (ADR-0028, registry added in 0022), so the shared `data_records`
JSONB table — and the `WHERE app_id` predicate that was its only isolation control —
has no reader and no writer left. `clear_data_tokens` existed solely to gate the
admin clear-data op over that table and goes with it. The four `app_registry` counter
columns (`data_count` / `data_bytes` / `file_count` / `file_bytes`) were the per-app
quota ledger for the same plane, and for the `app_files` model retired in 0017.

Consumers were removed FIRST, in the change that ships this revision: the records
router and its schemas, the `X-App-Key` chain, the app-serving quota helper, the
`the_purge` governance op, the admin data-summary / clear-data endpoints, and the ORM
models (so `Base.metadata` and the schema never disagree).

DESTRUCTIVE: `upgrade` deletes real rows and real columns. A pre-drop safety gate —
a row-count check, or an export of `data_records` per `app_id` for any app whose owner
still needs its contents — MUST run in the target environment BEFORE this is applied
there. `downgrade` recreates the STRUCTURE (both tables, their indexes, and the four
columns with their zero defaults) but NOT the data: restored counters read 0 and the
restored tables are empty.

RELEASE ORDER IS EXPAND/CONTRACT REVERSED. Deploy the reader-free image FIRST, then run
this migration out of band. Applying it while the previous image still serves is a
CONTROL-PLANE-WIDE 500, not a records-API 500: the admin app-listing projection SELECTs
`data_count`/`data_bytes` on every admin page load. See
`docs/engineering/deployment/DEPLOYMENT-FACTS.md`.

Mirrors 0011_data_records and 0013_clear_data_token in reverse (their upgrades are this
downgrade, their downgrades are this upgrade), plus the four columns 0010_app_registry
created. Those three revisions stay exactly where they are — 0012 and 0014 name 0011 and
0013 as `down_revision`. Hand-finalized (ADR-0013).
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0023_drop_data_records"
down_revision: str | None = "0022_project_database_registry"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # Indexes before their tables, tables before the columns on a table that survives.
    op.drop_index(op.f("ix_clear_data_tokens_app_id"), table_name="clear_data_tokens")
    op.drop_index(op.f("ix_clear_data_tokens_token"), table_name="clear_data_tokens")
    op.drop_table("clear_data_tokens")

    op.drop_index("ix_data_records_app_updated", table_name="data_records")
    op.drop_index("ix_data_records_app_created", table_name="data_records")
    op.drop_index("ix_data_records_app_collection", table_name="data_records")
    op.drop_index(op.f("ix_data_records_app_id"), table_name="data_records")
    op.drop_table("data_records")

    op.drop_column("app_registry", "file_bytes")
    op.drop_column("app_registry", "file_count")
    op.drop_column("app_registry", "data_bytes")
    op.drop_column("app_registry", "data_count")


def downgrade() -> None:
    # Exact reverse: columns, then each table, then its indexes. Structure only — the rows
    # and the counter values are gone for good.
    op.add_column(
        "app_registry",
        sa.Column("data_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.add_column(
        "app_registry",
        sa.Column("data_bytes", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
    )
    op.add_column(
        "app_registry",
        sa.Column("file_count", sa.Integer(), server_default=sa.text("0"), nullable=False),
    )
    op.add_column(
        "app_registry",
        sa.Column("file_bytes", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
    )

    op.create_table(
        "data_records",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("app_id", sa.Uuid(), nullable=False),
        sa.Column("collection", sa.String(length=64), server_default="default", nullable=False),
        sa.Column("data", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_by", sa.Uuid(), nullable=True),
        sa.Column(
            "created_in_draft", sa.Boolean(), server_default=sa.text("false"), nullable=False
        ),
        sa.Column("bytes", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("search_text", sa.Text(), nullable=True),
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
        sa.ForeignKeyConstraint(["app_id"], ["app_registry.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_data_records_app_id"), "data_records", ["app_id"], unique=False)
    op.create_index(
        "ix_data_records_app_collection", "data_records", ["app_id", "collection"], unique=False
    )
    op.create_index(
        "ix_data_records_app_created", "data_records", ["app_id", "created_at"], unique=False
    )
    op.create_index(
        "ix_data_records_app_updated", "data_records", ["app_id", "updated_at"], unique=False
    )

    op.create_table(
        "clear_data_tokens",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("token", sa.String(length=64), nullable=False),
        sa.Column("app_id", sa.Uuid(), nullable=False),
        sa.Column("actor_id", sa.Uuid(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.ForeignKeyConstraint(["app_id"], ["app_registry.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["actor_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_clear_data_tokens_token"), "clear_data_tokens", ["token"], unique=True
    )
    op.create_index(
        op.f("ix_clear_data_tokens_app_id"), "clear_data_tokens", ["app_id"], unique=False
    )
