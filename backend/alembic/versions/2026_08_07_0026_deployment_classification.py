"""record the data-classification declaration that authorised each deploy

Revision ID: 0026_deployment_classification
Revises: 0025_deployments
Create Date: 2026-08-07

One-click publish is now gated on a six-question data-classification questionnaire the
citizen answers at deploy time. The score decides — at or above the threshold the deploy
proceeds with no human in the loop — which makes the answers the only thing standing
between a generated app and production. They have to be durable.

Two columns rather than one:

* `classification` — the declaration itself, JSONB. Keyed by the questionnaire's own field
  names so the schema, the score, and this row cannot drift apart. JSONB rather than six
  booleans because the questionnaire is expected to be reworded and reweighted, and a
  column-per-question shape makes every wording change a schema conversation.
* `classification_score` — the total that actually authorised the deploy, stored rather
  than recomputed. The weights are policy and policy changes; recomputing later would
  report what TODAY's table says about an OLD declaration, silently rewriting the reason a
  past deploy was allowed.

PER DEPLOY, NOT PER APP. The obvious alternative is a column on `app_registry`, and it is
wrong here: the AI agent edits the app between deploys, so a declaration attached to the app
keeps describing a version that is no longer running. "What was this build claimed to
handle" is only answerable if the answer travels with the build.

Both nullable, and deliberately not back-filled. A row minted before the gate existed has no
declaration; `NULL` reads as "never asked", which is true. An all-False default would read
as "declared to handle nothing" — a claim nobody made, recorded as if they had.

A refused deploy writes NOTHING. There is no row here for an attempt that failed the gate,
because the gate is checked before the claim: refusing is not an event with a deployment to
attach to. The refusal is audited (`audit_log`, action `deploy`) rather than half-recorded
as a deployment that never was.

Hand-finalized (ADR-0013).
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0026_deployment_classification"
down_revision: str | None = "0025_deployments"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "deployments",
        sa.Column("classification", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.add_column("deployments", sa.Column("classification_score", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("deployments", "classification_score")
    op.drop_column("deployments", "classification")
