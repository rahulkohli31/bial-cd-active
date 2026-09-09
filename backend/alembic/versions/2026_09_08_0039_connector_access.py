"""connector access requests + per-project connector settings

Revision ID: 0039_connector_access
Revises: 0038_app_previous_status
Create Date: 2026-09-08

WHY THIS EXISTS: the two state machines the `ConnectorStates` board draws. Access belongs to the
PERSON — `connector_access_requests`, keyed on `user_id`, one row per ask, decided once and
covering every project that person owns. The days belong to the PROJECT — `project_connectors`, one
row per project that has ever switched a connector on, carrying its switch and the window it reads.

NO `connectors` TABLE (R15, origin Q18). There is exactly one connector and its catalogue is
`src/core/connectors.py`. A table would store display strings the boards own, need seeding in every
environment, and eventually want an admin CRUD screen for rows nobody may add. `connector_key` is a
plain `varchar(32)`; an unknown key is refused at the route, not by a foreign key.

GENERIC BY NAME, DICE ONLY BY VALUE (R18): nothing named here says DICE. It is the first of several
integrations, so a second one should be a registry entry plus its board copy — not this migration
again.

`uq_connector_access_requests_one_pending` IS PARTIAL ON PURPOSE: at most one OPEN ask per person
per connector, while approved / declined / cancelled rows accumulate freely underneath. A plain
UNIQUE on the pair would forbid a second ask forever; no constraint at all would let a
double-submitted dialog put two identical rows in the administrator's queue. It is also the
insert's `ON CONFLICT` inference target — a partial index cannot be an `ON CONSTRAINT` target, so
the predicate must be repeated at the call site, spelled as a LITERAL exactly as it is here.

`ck_project_connectors_window_shape` IS ONE CONSTRAINT OVER BOTH ARMS so a violation names the
shape that is wrong. `window_kind` carries its own NOT NULL as well, and that is not redundant: a
NULL kind makes both disjuncts UNKNOWN, and a CHECK constraint PASSES on UNKNOWN.

BOTH ENUM TYPES ARE CREATED AND DROPPED EXPLICITLY (`create_type=False` + `checkfirst=True`), and
the labels are written out as literals rather than imported from the Python enums — dropping a
table does not drop its type, and a migration that imports today's model breaks the day that model
changes (ADR-0008). `connector_request_status` deliberately has NO `withdrawn` label: nothing in
this pass could set it, and an unreachable label would cost an `ALTER TYPE` to remove.

NO BACKFILL, AND NOTHING TO UNDO *BEFORE GO-LIVE*: both tables are new, so `downgrade` drops them
and their types. After go-live that same `downgrade` is destructive and there is no way back —
`connector_access_requests` is the only record of who was granted or refused this data and by
whom, and the `connector:approve` / `connector:decline` audit rows written beside it carry request
ids, so dropping the table leaves those rows pointing at nothing. Treat a downgrade past this
revision on a live database as a data-loss operation that needs a dump first, not a routine
rollback step. Hand-finalized.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0039_connector_access"
down_revision: str | None = "0038_app_previous_status"
branch_labels: str | None = None
depends_on: str | None = None

# The native connector_request_status enum. Labels spelled out, never imported from
# `src.db.models.connector_access` — a migration is a historical record and must not change
# meaning when the model does. create_type=False so THIS migration owns the lifecycle.
connector_request_status = postgresql.ENUM(
    "pending",
    "approved",
    "declined",
    "cancelled",
    name="connector_request_status",
    create_type=False,
)

# The native connector_window_kind enum, same convention.
connector_window_kind = postgresql.ENUM(
    "relative",
    "absolute",
    name="connector_window_kind",
    create_type=False,
)

# The window shape rule, as one expression. Kept as a module constant so the DDL below and any
# future reader see exactly the string the constraint carries.
WINDOW_SHAPE_CHECK = (
    "(window_kind = 'relative' AND window_days IS NOT NULL "
    "AND window_start IS NULL AND window_end IS NULL) "
    "OR "
    "(window_kind = 'absolute' AND window_days IS NULL "
    "AND window_start IS NOT NULL AND window_end IS NOT NULL)"
)


def upgrade() -> None:
    connector_request_status.create(op.get_bind(), checkfirst=True)
    connector_window_kind.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "connector_access_requests",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        # OwnedByUserMixin — the single-tenant ownership boundary. Access is the PERSON's.
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column("connector_key", sa.String(length=32), nullable=False),
        sa.Column(
            "status",
            connector_request_status,
            server_default="pending",
            nullable=False,
        ),
        # The whole of what the administrator decides on, so NOT NULL. Text: the 5-50 word rule
        # lives at the schema layer, and a column cap would turn a rejected paste into a 500.
        sa.Column("requester_remarks", sa.Text(), nullable=False),
        # SET NULL, not CASCADE: the trail outlives the actor. An administrator's account being
        # deleted must not delete the record that somebody else was granted access.
        sa.Column("decided_by_id", sa.Uuid(), nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        # Written only on a decline (R10 removed the board's approve-remark), shown back to the
        # citizen verbatim.
        sa.Column("decision_remarks", sa.Text(), nullable=True),
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
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["decided_by_id"], ["users.id"], ondelete="SET NULL"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_connector_access_requests_user_id"),
        "connector_access_requests",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        "ix_connector_access_requests_user_connector",
        "connector_access_requests",
        ["user_id", "connector_key"],
        unique=False,
    )
    # THE guard: at most one open ask per person per connector. `postgresql_where` is what makes it
    # partial — without it a declined citizen could never ask again.
    op.create_index(
        "uq_connector_access_requests_one_pending",
        "connector_access_requests",
        ["user_id", "connector_key"],
        unique=True,
        postgresql_where=sa.text("status = 'pending'"),
    )

    op.create_table(
        "project_connectors",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        # NO user_id: `projects` is the ownership anchor and every user-facing query reaches this
        # table through a join on it (the `project_databases` precedent).
        sa.Column("project_id", sa.Uuid(), nullable=False),
        sa.Column("connector_key", sa.String(length=32), nullable=False),
        # Off unless something explicitly switched it on — the safe direction for a data control.
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        # NOT NULL with no default: every writer supplies a window, and a DDL default would be a
        # second place deciding what "no window" means.
        sa.Column("window_kind", connector_window_kind, nullable=False),
        sa.Column("window_days", sa.SmallInteger(), nullable=True),
        # Calendar days, not instants — the citizen picks days in an Asia/Kolkata grid. Stored
        # UNCLAMPED; the floor and the ceiling are applied on the read, in one place.
        sa.Column("window_start", sa.Date(), nullable=True),
        sa.Column("window_end", sa.Date(), nullable=True),
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
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        # One row per project per connector, and the upsert's ON CONFLICT inference target.
        sa.UniqueConstraint(
            "project_id",
            "connector_key",
            name="uq_project_connectors_project_connector",
        ),
        sa.CheckConstraint(WINDOW_SHAPE_CHECK, name="ck_project_connectors_window_shape"),
    )


def downgrade() -> None:
    op.drop_table("project_connectors")
    op.drop_index(
        "uq_connector_access_requests_one_pending",
        table_name="connector_access_requests",
    )
    op.drop_index(
        "ix_connector_access_requests_user_connector",
        table_name="connector_access_requests",
    )
    op.drop_index(
        op.f("ix_connector_access_requests_user_id"),
        table_name="connector_access_requests",
    )
    op.drop_table("connector_access_requests")
    # DROP THE TYPES TOO. Dropping a table does not drop its enum type, and a leftover type makes
    # the next upgrade fail on `type already exists` — on somebody else's machine, weeks later.
    connector_window_kind.drop(op.get_bind(), checkfirst=True)
    connector_request_status.drop(op.get_bind(), checkfirst=True)
