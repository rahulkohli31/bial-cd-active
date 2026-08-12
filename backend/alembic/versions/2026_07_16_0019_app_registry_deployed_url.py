"""record the deployed app's URL on app_registry (PILOT R5)

Revision ID: 0019_app_registry_deployed_url
Revises: 0018_app_registry_submissions
Create Date: 2026-07-16

One nullable column — `deployed_url` — completing the manual-runbook marker that
0018 landed (`deployed_submission_id` / `deployed_at`). The superadmin pastes the
address the go-live runbook produced when they mark the app deployed, and the owner
finally gets a "Live" link instead of "an approved app is deployed by the platform
team". Deployed URL is DATA, not automation: nothing here derives, probes, or
verifies the address.

Nullable by construction: every existing row predates the column, and an app can be
marked deployed without a URL (the field is optional and the endpoint was already in
use — back-compat). `String(2083)` mirrors `MAX_DEPLOYED_URL` / pydantic `HttpUrl`'s
own `max_length`, so a URL that parses at the schema boundary always fits here.

`downgrade` drops the column — the recorded URLs go with it, and they are re-typed
from the runbook, never reconstructed. NOT destructive to any other column.

Hand-finalized (ADR-0013).
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0019_app_registry_deployed_url"
down_revision: str | None = "0018_app_registry_submissions"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column("app_registry", sa.Column("deployed_url", sa.String(length=2083), nullable=True))


def downgrade() -> None:
    op.drop_column("app_registry", "deployed_url")
