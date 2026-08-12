"""record when a published app's container was taken down (unpublish, #113)

Revision ID: 0027_deployment_unpublished_at
Revises: 0026_deployment_classification
Create Date: 2026-08-12

There was no way to take a published app down short of destroying the citizen's
project or app entirely — the manual-runbook `disable` lever cannot apply (it is
guarded on `status == approved`, and a self-deployed app stays `draft`), and the
sandbox reaper cannot reach it either (it reads the Redis sandbox registry, which
publish deliberately never writes to). Since published apps have no authentication
of their own, the first "take it down now" request needs a real answer.

A NULLABLE TIMESTAMP, NOT A NEW `DeploymentStatus`. `DeploymentStatus` is three
states, deliberately (`running` / `succeeded` / `failed`) — the docstring on the
model says so, and the partial unique index `uq_deployments_one_in_flight` is
built on `status = 'running'`. Adding a fourth status would change what that
index covers, which is a real schema decision, not a label change. "Is this
app live right now" and "how did the last deploy attempt end" are two
different axes: a `succeeded` deployment can be currently published or
currently taken down, and that is exactly what this column answers without
touching the first axis at all.

Lives on the deployment ROW, not a column on `app_registry` — the same
reasoning as `classification`/`classification_score` in 0026. The most recent
`succeeded` row for an app IS the one currently serving traffic (or not, if
this column is set), so recording "taken down" anywhere else would be a second
copy of a fact this table already owns. It is also what makes republish free:
a later successful deploy is a NEW row with this column NULL, and the "latest
succeeded" query naturally picks it up — nothing about unpublishing has to be
undone, because nothing about it constrains a future row.

Nullable, and never back-filled — every existing row reads as "still published
(or never was)", which is true of history recorded before this column existed.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0027_deployment_unpublished_at"
down_revision: str | None = "0026_deployment_classification"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "deployments",
        sa.Column("unpublished_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("deployments", "unpublished_at")
