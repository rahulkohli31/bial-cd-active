"""add the deployments table for one-click publish

Revision ID: 0025_deployments
Revises: 0024_messages_native_reset
Create Date: 2026-08-06

A citizen can now publish their generated app with one click — no admin approval on that
path. That leaves the control plane with no record of what is actually running: the
`app_registry.deployed_*` columns belong to the manual go-live runbook and their writer
(`mark-deployed`) is guarded on `status == APPROVED`, so a self-deployed app — which stays
`draft` — can never be described by them. Relaxing that guard would dissolve the approval
invariant, so publish gets its own lineage instead.

One row per ATTEMPT, append-only. A failed deploy must not overwrite the record of the
version still serving traffic, which is what makes "what is live" and "what we last tried"
separately answerable — the two questions the crash reconciler and rollback both ask.

`image_digest` is the load-bearing column. It is the reconciler's authorization to act
(after a crash it may promote a row only when ARM reports the same digest live, and it may
never delete a container app it cannot prove it created), and it is the rollback source
(redeploying the previous digest is one ARM call against an image that already exists).

`uq_deployments_one_in_flight` is a PARTIAL unique index, not a constraint: at most one
`running` row per app, while terminal rows accumulate freely. It is claimed by inference
(`ON CONFLICT (app_id) WHERE status = 'running' DO NOTHING`) because a partial index cannot
be an `ON CONSTRAINT` target. The guard lives in Postgres rather than in-process because
the pipeline runs for minutes and the control plane restarts on every platform deploy —
the first repo-wide use of a partial index, and deliberately so.

`ON DELETE CASCADE` from `app_registry` guarantees a row cannot outlive its app. Note the
ordering that forces on the delete paths: `container_app_name` must be read out of this
row BEFORE the delete commits, or the running Azure container becomes an orphan no sweeper
can find.

Hand-finalized (ADR-0013).
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0025_deployments"
down_revision: str | None = "0024_messages_native_reset"
branch_labels: str | None = None
depends_on: str | None = None

# The native deployment_status enum (ADR-0008). create_type=False so THIS migration owns
# the lifecycle: explicit .create() in upgrade, .drop() in downgrade.
deployment_status = postgresql.ENUM(
    "running",
    "succeeded",
    "failed",
    name="deployment_status",
    create_type=False,
)


def upgrade() -> None:
    deployment_status.create(op.get_bind(), checkfirst=True)
    op.create_table(
        "deployments",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        # OwnedByUserMixin — the single-tenant ownership boundary (ADR-0004).
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("app_id", sa.Uuid(), nullable=False),
        sa.Column(
            "status",
            deployment_status,
            server_default="running",
            nullable=False,
        ),
        sa.Column("step", sa.String(length=32), server_default="claimed", nullable=False),
        sa.Column("head_sha", sa.String(length=40), nullable=True),
        sa.Column("image_digest", sa.String(length=80), nullable=True),
        sa.Column("acr_run_id", sa.String(length=64), nullable=True),
        sa.Column("container_app_name", sa.String(length=32), nullable=True),
        sa.Column("revision_name", sa.String(length=64), nullable=True),
        sa.Column("url", sa.String(length=2083), nullable=True),
        sa.Column("failure_code", sa.String(length=64), nullable=True),
        sa.Column("failure_detail", sa.Text(), nullable=True),
        sa.Column(
            "heartbeat_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
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
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(op.f("ix_deployments_user_id"), "deployments", ["user_id"], unique=False)
    op.create_index(op.f("ix_deployments_app_id"), "deployments", ["app_id"], unique=False)
    # The concurrency guard. `postgresql_where` is what makes it partial — without it this
    # would forbid an app from ever being deployed twice.
    op.create_index(
        "uq_deployments_one_in_flight",
        "deployments",
        ["app_id"],
        unique=True,
        postgresql_where=sa.text("status = 'running'"),
    )


def downgrade() -> None:
    op.drop_index("uq_deployments_one_in_flight", table_name="deployments")
    op.drop_index(op.f("ix_deployments_app_id"), table_name="deployments")
    op.drop_index(op.f("ix_deployments_user_id"), table_name="deployments")
    op.drop_table("deployments")
    deployment_status.drop(op.get_bind(), checkfirst=True)
