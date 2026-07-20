"""drop the app_registry.name column (#48 / F5)

Revision ID: 0020_drop_app_registry_name
Revises: 0019_app_registry_deployed_url
Create Date: 2026-07-19

The per-app `name` was never populated on the provision path (both upserts rely on
its `server_default=""`), so the admin registry rendered "(untitled)" for every app.
The display name now comes from the owning `projects.name` (re-sourced in the admin
projection), and the column goes away. No index/constraint/enum is involved — a plain
single-column drop, mirroring 0019's own `op.drop_column`/`op.add_column` shape.

DESTRUCTIVE + SCHEMA-ONLY ROUND-TRIP: `downgrade` recreates the column's STRUCTURE
(`String(120) NOT NULL server_default=""`), not its data. Any admin-set name is gone on
the drop and is NOT reconstructed on downgrade. A pre-drop count on release/phase2
confirmed zero rows had a non-empty name, so the drop loses nothing in practice.

Hand-finalized (ADR-0013).
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0020_drop_app_registry_name"
down_revision: str | None = "0019_app_registry_deployed_url"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.drop_column("app_registry", "name")


def downgrade() -> None:
    op.add_column(
        "app_registry",
        sa.Column("name", sa.String(length=120), server_default="", nullable=False),
    )
