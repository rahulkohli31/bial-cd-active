"""a standing rejection that outlives the status it was read from (P4)

Revision ID: 0032_rejection_standing
Revises: 0031_token_usage_kind
Create Date: 2026-08-20

WHY A COLUMN AND NOT A STATUS READ. Ladder rule 5 — "a rejection is sticky: an
administrator lifts it, a re-roll never" — was deriving that durable policy fact
from `status`, which is mutable lifecycle state. Two ordinary citizen-callable
calls laundered it: pressing Publish on a REJECTED app routes it (the submit
service moves REJECTED -> PENDING and nulls `rejection_note` on the way), and
`POST /apps/{id}/withdraw` is legal from PENDING and writes DRAFT. The row then
carries no trace a rejection ever happened — only the audit log does — and the
next Publish with a clean review goes out unattended, which is precisely what P4
forbids.

`rejection_standing` separates the two questions that were sharing one column:
WHERE is this app in its lifecycle (`status`, which withdraw may legitimately
move) versus HAS A HUMAN REFUSED IT (this flag, which only an administrator
clears). `reject` raises it, `approve` lowers it, and nothing on the citizen's
side touches it.

BACKFILL: every row sitting in `rejected` today. That is the whole of what is
knowable at cutover — a row that was rejected and has since been re-submitted or
withdrawn left no durable trace to recover, which is the very defect this column
exists to stop. Those rows keep today's (wrong) behaviour until an administrator
rejects them again; nothing is made worse, and no false positive is invented by
guessing.

NOT NULL with a constant server default is metadata-only on PG11+ — no table
rewrite, no lock of consequence. The default stays in place deliberately: the ORM
always supplies the value, but a row inserted by a hand-run statement during an
incident should read "not rejected" rather than fail.

NO DOWNGRADE/UPGRADE ROUND-TRIP TEST for this one, following 0030: a round trip
on `app_registry` burns pg_attribute slots on the shared test database forever
(see `docs/solutions/test-failures/alembic-round-trip-tests-burn-attnum-slots-2026-07-20.md`).
The backfill is exercised directly instead.

Hand-finalized (ADR-0013).
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0032_rejection_standing"
down_revision: str | None = "0031_token_usage_kind"
branch_labels: str | None = None
depends_on: str | None = None

# As ONE statement so the test suite can execute EXACTLY what runs here. `status =
# 'rejected'` is the only durable evidence of a refusal that survives to cutover;
# `rejection_standing = false` keeps it idempotent and blind to anything already raised.
BACKFILL_STANDING_REJECTIONS = sa.text(
    "UPDATE app_registry SET rejection_standing = true "
    "WHERE rejection_standing = false AND status = 'rejected'"
)


def upgrade() -> None:
    op.add_column(
        "app_registry",
        sa.Column(
            "rejection_standing",
            sa.Boolean(),
            nullable=False,
            server_default=sa.false(),
        ),
    )
    op.execute(BACKFILL_STANDING_REJECTIONS)


def downgrade() -> None:
    op.drop_column("app_registry", "rejection_standing")
