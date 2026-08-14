"""record when a published app's container was taken down (unpublish, #113)

Revision ID: 0028_deployment_unpublished_at
Revises: 0027_worker_passes
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
row for an app is the one whose container is current (or not, if this column
is set), so recording "taken down" anywhere else would be a second copy of a
fact this table already owns. It is also what makes republish free: a later
successful deploy is a NEW row with this column NULL, and the "latest row"
query naturally picks it up — nothing about unpublishing has to be undone,
because nothing about it constrains a future row.

NEWEST ROW, NOT NEWEST SUCCEEDED ROW, and the difference is load-bearing. The
deploy pipeline creates the container app at step 5 and only THEN awaits the
revision, so an attempt that settles FAILED at step 6 can still name a
container that exists, is externally addressable, holds the app's database URL
and Blob SAS, and bills. That is why the kill-switch resolves through
`store.latest_for_app` rather than `store.last_successful`: a stamp on a FAILED
row is meaningful and says exactly what it looks like — THIS is the attempt
whose container was torn down. An older row keeps whatever value it had when it
was current and is not maintained afterwards.

Nullable, and never back-filled — every existing row reads as "still published
(or never was)", which is true of history recorded before this column existed.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0028_deployment_unpublished_at"
down_revision: str | None = "0027_worker_passes"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.add_column(
        "deployments",
        sa.Column("unpublished_at", sa.DateTime(timezone=True), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("deployments", "unpublished_at")
