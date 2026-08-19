"""a kind dimension on token_usage — review spend is metered, never billed (U15)

Revision ID: 0031_token_usage_kind
Revises: 0030_approval_route_declaration
Create Date: 2026-08-19

`token_usage` was one row per (user, IST day) with no dimension for what the spend
was FOR. The pre-publish classification review needs its spend recorded against the
citizen (attribution — knowing who generates review cost is the point of recording
it) without counting toward the budget their own builds are measured against (a
heavy build day must never make an app unpublishable, and opening the publish
dialog must never silently spend build time — ASM14). One column carries both:

  * `kind` — a native enum (`build` | `review`, ADR-0008), NOT NULL, defaulting to
    `build` because that is the DEFINED meaning of an unspecified kind: every
    writer that predates the dimension was a build writer.
  * the uniqueness moves from `(user_id, usage_date)` to `(user_id, usage_date,
    kind)` — at most one row per kind per day, so `record_usage`'s atomic
    `INSERT … ON CONFLICT … DO UPDATE` fold keeps working unchanged with `kind`
    added to its inference target.

The `kind == 'build'` filter lands in the expression's two READERS (the daily
gate's `_used_today` and the admin roster), never inside `billable_spend()` itself
— that expression carries the fix for two production accounting incidents and is
left untouched.

BACKFILL: every existing row becomes `build`. That is what it has always been —
the review kind did not exist to be recorded.

`downgrade` DELETES `review` rows before restoring the old `(user_id, usage_date)`
uniqueness: the pre-kind schema keys one row per (user, day) and cannot hold a
second same-day row, and folding review spend into the build row would do
retroactively exactly what this migration exists to prevent — bill the citizen for
reviews. Attribution records the old schema cannot represent are dropped with the
schema that cannot represent them.

Hand-finalized (ADR-0013).
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0031_token_usage_kind"
down_revision: str | None = "0030_approval_route_declaration"
branch_labels: str | None = None
depends_on: str | None = None

# The native token_usage_kind enum (ADR-0008). create_type=False so THIS migration owns
# the lifecycle: explicit .create() in upgrade, .drop() in downgrade.
token_usage_kind = postgresql.ENUM(
    "build",
    "review",
    name="token_usage_kind",
    create_type=False,
)

# The U15 backfill, as ONE statement so the test suite can exercise EXACTLY what runs
# here (the shape tests run against the fresh-upgrade DDL; a full downgrade/upgrade
# round-trip stays in the destructive lane). `kind IS NULL` targets precisely the rows
# that predate the dimension — the column is added nullable, backfilled, then pinned
# NOT NULL, so nothing else can be NULL at this point.
BACKFILL_KIND_BUILD = sa.text("UPDATE token_usage SET kind = 'build' WHERE kind IS NULL")

# The downgrade's clearing of the rows the old schema cannot represent (see the module
# docstring: deleted, never folded into build — that would retroactively bill reviews).
DELETE_REVIEW_ROWS = sa.text("DELETE FROM token_usage WHERE kind = 'review'")


def upgrade() -> None:
    token_usage_kind.create(op.get_bind(), checkfirst=True)
    # Nullable first, backfill, then pin NOT NULL — the three-step add keeps the
    # backfill an explicit, testable statement rather than a side effect of a default.
    op.add_column("token_usage", sa.Column("kind", token_usage_kind, nullable=True))
    op.execute(BACKFILL_KIND_BUILD)
    op.alter_column(
        "token_usage",
        "kind",
        existing_type=token_usage_kind,
        nullable=False,
        server_default=sa.text("'build'"),
    )
    # The upsert's conflict target widens to include the kind: at most one row per
    # user per IST day PER KIND.
    op.drop_constraint("uq_token_usage_user_date", "token_usage", type_="unique")
    op.create_unique_constraint(
        "uq_token_usage_user_date_kind", "token_usage", ["user_id", "usage_date", "kind"]
    )


def downgrade() -> None:
    op.execute(DELETE_REVIEW_ROWS)
    op.drop_constraint("uq_token_usage_user_date_kind", "token_usage", type_="unique")
    op.create_unique_constraint(
        "uq_token_usage_user_date", "token_usage", ["user_id", "usage_date"]
    )
    op.drop_column("token_usage", "kind")
    token_usage_kind.drop(op.get_bind(), checkfirst=True)
