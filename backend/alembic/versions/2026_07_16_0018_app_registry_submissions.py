"""re-shape app_registry onto typed submission columns (APPROVAL)

Revision ID: 0018_app_registry_submissions
Revises: 0017_drop_app_files
Create Date: 2026-07-16

Retire the JSX-era snapshot JSONB (`source_snapshot` / `approved_snapshot`) and
replace it with typed references to immutable per-submission git bundles (D1): a
submission is a *reference* — `(app_id, submission_id)` derives the blob key via
the key builder, so no key is stored. Seven nullable columns land:

  * `source_submission_id` / `source_commit_sha` / `submitted_at` — the submission
    under review (the approve guard's predicate, D5; the queue's order axis, R16).
  * `approved_submission_id` / `approved_commit_sha` — the pinned reviewed artifact.
  * `deployed_submission_id` / `deployed_at` — the manual-runbook marker (D7): a
    marker, NOT a status, so `app_status` and the transition table are untouched.

DESTRUCTIVE: `upgrade` drops both JSONB snapshot columns — the JSX-era compiled
artifacts they hold are discarded and unrecoverable by construction (nothing can
backfill a git bundle from a compiled-JSX blob). It also resets legacy `pending` /
`approved` rows to `draft`: they hold artifacts the new gate cannot approve or
serve, and leaving them APPROVED with a NULL ref manufactures the "approved app
whose artifact does not exist" failure this schema exists to prevent (D13). The
reset deliberately SPARES `disabled` and `rejected`: those are the only statuses
outside ACTIVE_STATUSES (the X-App-Key data plane refuses them with 403), and
`draft` is active with `app_key` unchanged — a blanket reset would silently
re-open a kill-switched or rejected app's data plane. A pre-drop export of
`source_snapshot` / `approved_snapshot` MUST run in the target environment BEFORE
this is applied there. `downgrade` recreates the STRUCTURE (the two JSONB columns)
but NOT the data — the reset statuses also stay reset.

Hand-finalized (ADR-0013).
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0018_app_registry_submissions"
down_revision: str | None = "0017_drop_app_files"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    # 1. The typed submission/approval/deployment references. All nullable: a
    #    freshly provisioned app has none of them.
    op.add_column("app_registry", sa.Column("source_submission_id", sa.Uuid(), nullable=True))
    op.add_column(
        "app_registry", sa.Column("source_commit_sha", sa.String(length=40), nullable=True)
    )
    op.add_column(
        "app_registry", sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("app_registry", sa.Column("approved_submission_id", sa.Uuid(), nullable=True))
    op.add_column(
        "app_registry", sa.Column("approved_commit_sha", sa.String(length=40), nullable=True)
    )
    op.add_column("app_registry", sa.Column("deployed_submission_id", sa.Uuid(), nullable=True))
    op.add_column(
        "app_registry", sa.Column("deployed_at", sa.DateTime(timezone=True), nullable=True)
    )

    # 2. Reset the legacy JSX-era in-flight rows (STATUS-SCOPED — see the
    #    DESTRUCTIVE paragraph for why disabled/rejected are spared). Their new ref
    #    columns were just added, so they are NULL by construction; rebuild +
    #    re-submit is the honest path back for these already-inert rows.
    op.execute("UPDATE app_registry SET status = 'draft' WHERE status IN ('pending', 'approved')")

    # 3. Drop the JSX-era snapshots. `app_status` is NOT touched (D7).
    op.drop_column("app_registry", "source_snapshot")
    op.drop_column("app_registry", "approved_snapshot")


def downgrade() -> None:
    """Schema-shape rollback ONLY: the JSONB columns come back empty (the JSX
    artifacts are unrecoverable by construction) and the pending/approved→draft
    reset is NOT undone (nothing records which rows it touched)."""
    op.add_column(
        "app_registry",
        sa.Column("source_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column(
        "app_registry",
        sa.Column("approved_snapshot", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.drop_column("app_registry", "deployed_at")
    op.drop_column("app_registry", "deployed_submission_id")
    op.drop_column("app_registry", "approved_commit_sha")
    op.drop_column("app_registry", "approved_submission_id")
    op.drop_column("app_registry", "submitted_at")
    op.drop_column("app_registry", "source_commit_sha")
    op.drop_column("app_registry", "source_submission_id")
