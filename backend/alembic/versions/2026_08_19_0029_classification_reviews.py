"""add the classification_reviews table for the pre-publish AI review

Revision ID: 0029_classification_reviews
Revises: 0028_deployment_unpublished_at
Create Date: 2026-08-19

The pre-publish review reads an app's last saved code and pre-fills the six
data-classification questions; this table stores that result — ONE row per app,
upserted, stamped with the commit it read (`head_sha`). The opposite shape from
`deployments` (0025), deliberately: a deployment attempt is append-only history
because a failed attempt must not overwrite the record of the version serving
traffic, while a review is only ever a claim about the CURRENT saved version — a
stored answer for an older commit is a stale answer waiting to be mistaken for a
current one, so the row is overwritten wholesale when the version moves and the
stamp is what makes staleness detectable (R6). The durable history lives in the
per-run and per-publish audit records, not here (R6a).

`uq_classification_reviews_app` is both the one-row-per-app invariant and the
claim's `ON CONFLICT` inference target: the fresh-insert race is settled in
Postgres because the control plane restarts mid-run and two dialog opens can
race — the same reasoning that put `uq_deployments_one_in_flight` in the
database rather than in-process.

`attempt` counts the runs claimed for the stamped version (reset on a version
change, incremented on a same-version retry). It exists because the review
bypasses the citizen's daily token gate, so its real spend bound is the
service-layer cap of three model runs per version — a cap that can only be
enforced against a counter the store keeps faithfully.

The verdict/evidence pair is JSONB for the same reason `deployments.classification`
is (0026): the questionnaire is expected to be reworded and reweighted, and a shape
needing a migration per question would make that a schema conversation every time.

Hand-finalized (ADR-0013).
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0029_classification_reviews"
down_revision: str | None = "0028_deployment_unpublished_at"
branch_labels: str | None = None
depends_on: str | None = None

# The native classification_review_status enum (ADR-0008). create_type=False so THIS
# migration owns the lifecycle: explicit .create() in upgrade, .drop() in downgrade.
classification_review_status = postgresql.ENUM(
    "running",
    "complete",
    "failed",
    name="classification_review_status",
    create_type=False,
)


def upgrade() -> None:
    classification_review_status.create(op.get_bind(), checkfirst=True)
    op.create_table(
        "classification_reviews",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        # OwnedByUserMixin — the single-tenant ownership boundary (ADR-0004).
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("app_id", sa.Uuid(), nullable=False),
        # The version stamp: the commit the review read. NOT NULL — a row without a
        # stamp is exactly the un-datable answer this table exists to prevent.
        sa.Column("head_sha", sa.String(length=40), nullable=False),
        sa.Column(
            "status",
            classification_review_status,
            server_default="running",
            nullable=False,
        ),
        sa.Column("attempt", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("verdicts", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("evidence", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("answers_complete", sa.Boolean(), nullable=True),
        sa.Column("failure_code", sa.String(length=64), nullable=True),
        sa.Column("failure_detail", sa.Text(), nullable=True),
        sa.Column(
            "started_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        # The four raw token classes, kept split and never re-folded (the documented
        # cache double-count regression) — same discipline as token_usage (0004).
        sa.Column("input_tokens", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column("output_tokens", sa.BigInteger(), server_default=sa.text("0"), nullable=False),
        sa.Column(
            "cache_read_tokens", sa.BigInteger(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "cache_write_tokens", sa.BigInteger(), server_default=sa.text("0"), nullable=False
        ),
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
        # ONE ROW PER APP — the whole design, and the claim's conflict target.
        sa.UniqueConstraint("app_id", name="uq_classification_reviews_app"),
    )
    op.create_index(
        op.f("ix_classification_reviews_user_id"),
        "classification_reviews",
        ["user_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_classification_reviews_user_id"), table_name="classification_reviews")
    op.drop_table("classification_reviews")
    classification_review_status.drop(op.get_bind(), checkfirst=True)
