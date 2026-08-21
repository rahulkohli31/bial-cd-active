"""approval lineage + submitted declaration on app_registry (U4: R15, R17a, P5)

Revision ID: 0030_approval_route_declaration
Revises: 0029_classification_reviews
Create Date: 2026-08-19

Two nullable columns land on `app_registry`:

  * `approval_route` — a native enum (`runbook` | `self_publish`, ADR-0008) recording
    which LINEAGE the current submission entered through. An EXPLICIT column, not a
    derivation tweak (ASM8): `redeploy_needed` derives from two columns a
    self-published app never sets, so without this a self-published app would read
    "Deploy needed" forever and prompt an administrator to run a runbook that must
    not be run (R17a).
  * `declaration` — the JSONB payload the publish flow attaches at submit: both
    answer sets, the per-question differences, and the redacted explanation (R15).
    JSONB for the same reason `deployments.classification` (0026) is: the
    questionnaire is expected to be reworded and reweighted.

BACKFILL (the P5 cutover): every existing row WITH an approval, and every row
sitting in PENDING, is marked `runbook`. The approvals half is the obvious one —
they were granted for an out-of-band code review, a different decision, so they
must never satisfy the gate's self-publish rule. The pending half is the one that
is easy to miss and costs more: a queue item outstanding at release carries no
lineage, so the administrator's approval would leave it NULL, the gate's rule 3
would not be satisfied, and the citizen would need a SECOND approval to publish
once. Marking them `runbook` makes that a NAMED dead end (approve refuses with
copy telling the admin the citizen must re-submit through the publish flow)
rather than a silent loop. Everything else — drafts, and rejected rows that were
never approved — stays NULL and picks up `self_publish` on its first trip through
the publish flow.

`downgrade` just drops the columns + the enum: the backfill is not "undone"
because it has nothing to restore — every value it wrote lives in a column the
downgrade removes.

Hand-finalized (ADR-0013).
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# One char under alembic_version's varchar(32) — "and" did not survive the cap.
revision: str = "0030_approval_route_declaration"
down_revision: str | None = "0029_classification_reviews"
branch_labels: str | None = None
depends_on: str | None = None

# The native approval_route enum (ADR-0008). create_type=False so THIS migration owns
# the lifecycle: explicit .create() in upgrade, .drop() in downgrade.
approval_route = postgresql.ENUM(
    "runbook",
    "self_publish",
    name="approval_route",
    create_type=False,
)

# The P5 cutover, as ONE statement so the test suite can exercise EXACTLY what runs here
# (the non-destructive lane seeds rows via the ORM and executes this — a full
# downgrade/upgrade round-trip on app_registry burns pg_attribute slots forever, so it
# stays out of the default lane). The three disjuncts, deliberately:
#   * `approved_submission_id IS NOT NULL` — every row carrying an approved pin, whatever
#     its status today (a re-submitted-then-rejected app keeps its pin, and that pin is a
#     pre-feature approval).
#   * `status IN ('approved', 'disabled')` — the pin-less legacy remainder: a DISABLED
#     row 0018 spared with a NULL pin (the D13 approved-with-no-artifact state) was still
#     approved once, and `disabled` is only reachable FROM approved.
#   * `status = 'pending'` — the then-outstanding queue items (see the module docstring).
# `approval_route IS NULL` keeps this idempotent and blind to rows the publish flow has
# already stamped.
BACKFILL_RUNBOOK_LINEAGE = sa.text(
    "UPDATE app_registry SET approval_route = 'runbook' "
    "WHERE approval_route IS NULL AND ("
    "approved_submission_id IS NOT NULL "
    "OR status IN ('pending', 'approved', 'disabled'))"
)


def upgrade() -> None:
    approval_route.create(op.get_bind(), checkfirst=True)
    op.add_column(
        "app_registry",
        sa.Column("approval_route", approval_route, nullable=True),
    )
    op.add_column(
        "app_registry",
        sa.Column("declaration", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )
    op.execute(BACKFILL_RUNBOOK_LINEAGE)


def downgrade() -> None:
    op.drop_column("app_registry", "declaration")
    op.drop_column("app_registry", "approval_route")
    approval_route.drop(op.get_bind(), checkfirst=True)
