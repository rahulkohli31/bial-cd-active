"""The `project_connectors` table — one row per project that has ever switched a connector on,
carrying that project's switch and the days it reads.

THE DAYS BELONG TO THE PROJECT (R2). Access is answered once for a person
(`connector_access_requests`); after that a project only ever answers two questions of its own —
is this switched on here, and how far back does it read — and neither needs anybody's permission.
A departures board and a six-month trend want different history, so the window is per project.

WHY THIS EXISTS AS A TABLE. Same shape as `project_databases`, and for the same reasons: absence is
clean (no row = this connector was never switched on here, which is a different fact from "switched
off"), and the row is where a per-project setting lives without widening `projects` with a column
per integration that most projects will never use.

NO `OwnedByUserMixin`, DELIBERATELY. `projects` is the ownership anchor: every user-facing query
reaches this table through a join on it, so the `user_id` predicate lives there and is never
dropped here. Duplicating `user_id` onto this row would create a second copy of the ownership fact
that a project transfer would have to keep in step. This is `project_databases`' precedent, not a
new call.

THE ROW IS KEPT ON SWITCH-OFF. `enabled = false` means "switched off, and the days you picked are
still here"; deleting the row would destroy the stored window that origin R10 requires survive, for
no saving. Switching a connector off is not spending the approval, and switching it back on must
not silently re-pick `Last 30 days` over the range the citizen chose.

STORED STATE IS NOT EFFECTIVE STATE (R12/R13). `enabled = true` on this row means the project's
switch is up — nothing more. Whether the connector actually reads is `enabled AND the owner is
approved`, and that conjunction is computed in exactly ONE place, the resolver in
`src/core/connectors.py`. Nothing may read `enabled` and call it "on".

WINDOWS STORE THEIR KIND, and one CHECK constraint keeps the columns honest. The alternative —
inferring `relative` vs `absolute` from which columns happen to be null — needs the same constraint
to be trustworthy, reads worse at every call site, and still has to put a kind on the wire.
Recorded because the choice should be visible (origin Q6).

THE CHECK IS ALSO WHY THE UPSERT MUST BRANCH. `PUT .../connectors/{key}` takes an optional
`window`: omitted means "keep whatever is stored". The obvious `DO UPDATE SET x =
COALESCE(EXCLUDED.x, x)` per column is WRONG — a relative window landing on a stored absolute row
would leave `window_days`, `window_start` and `window_end` all populated and violate this
constraint. The `DO UPDATE` branches: window supplied, set all four window columns; window omitted,
set `enabled` only.

ASYNCPG WILL NOT CAST `varchar` → enum IMPLICITLY (ADR-0008). Any parameterised statement that
compares or writes `window_kind` must go through the mapped ORM column (which types it) or cast
explicitly. This is the line of ADR-0008 that is easiest to skip and cheapest to honour.
"""

from __future__ import annotations

import uuid
from datetime import date
from enum import StrEnum

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base
from src.db.mixins import TimestampMixin, UUIDv7PrimaryKeyMixin
from src.db.models.connector_access import MAX_CONNECTOR_KEY


class ConnectorWindowKind(StrEnum):
    """How a project's window is expressed. Values are the native PG enum labels.

    `relative` is one of the board's presets — a day count ending today, re-resolved on every read,
    so a project set to `Last 7 days` still reads the last seven days a month later. `absolute` is
    a fixed date pair the citizen picked in the calendar, and it ages: the resolver slides an
    aged-out range forward to a window of the SAME LENGTH ending today rather than widening it to
    the cap."""

    RELATIVE = "relative"
    ABSOLUTE = "absolute"


# The native PG enum type, shared by the model column and the Alembic migration.
# `values_callable` is mandatory (ADR-0008) so the labels stored are the values (`relative`), not
# the member names (`RELATIVE`). `create_type=False`: migration 0039 owns CREATE/DROP TYPE, so the
# downgrade actually removes it — dropping a table does not drop its type.
connector_window_kind_enum = sa.Enum(
    ConnectorWindowKind,
    name="connector_window_kind",
    values_callable=lambda enum: [member.value for member in enum],
    create_type=False,
)

# EXACTLY THE RIGHT COLUMNS NON-NULL FOR THE KIND. Written as one constraint over both arms rather
# than as three smaller ones so a violation names the shape that is wrong, and so a future third
# kind is a visible edit to a single expression instead of a rule spread over the file.
#
# `window_kind` IS NOT NULL SEPARATELY, and that is not redundant with this: a NULL `window_kind`
# makes both disjuncts evaluate to UNKNOWN, and a CHECK constraint PASSES on UNKNOWN. Without the
# column's own NOT NULL, a row with no kind and no window at all would be accepted here.
_WINDOW_SHAPE = (
    "(window_kind = 'relative' AND window_days IS NOT NULL "
    "AND window_start IS NULL AND window_end IS NULL) "
    "OR "
    "(window_kind = 'absolute' AND window_days IS NULL "
    "AND window_start IS NOT NULL AND window_end IS NOT NULL)"
)


class ProjectConnector(UUIDv7PrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "project_connectors"

    __table_args__ = (
        # ONE row per project per connector, forever. This constraint is also the upsert's
        # `ON CONFLICT` inference target, which is what serialises two switch presses racing from
        # two tabs into one row rather than two.
        sa.UniqueConstraint(
            "project_id",
            "connector_key",
            name="uq_project_connectors_project_connector",
        ),
        sa.CheckConstraint(_WINDOW_SHAPE, name="ck_project_connectors_window_shape"),
    )

    # The owning project, and the ownership anchor (see the module docblock). CASCADE so a deleted
    # project can never leave a switch pointing at nothing.
    # No `index=True`: `uq_project_connectors_project_connector`'s unique index leads with
    # `project_id`, so the rail's "this project's connector rows" read is already covered.
    project_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid,
        sa.ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
    )

    # WHICH connector, as a key (R15) — the catalogue is `src/core/connectors.py`, not a table, so
    # there is nothing to point a foreign key at and an unknown key is refused at the route.
    connector_key: Mapped[str] = mapped_column(sa.String(MAX_CONNECTOR_KEY), nullable=False)

    # The project's switch. `false` by default so a row written by anything other than an explicit
    # switch-on is off — the safe direction for a data-access control.
    enabled: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.text("false")
    )

    # THE WINDOW: a kind plus exactly the columns that kind uses, per `ck_..._window_shape`.
    # NOT NULL with NO server default — every writer supplies a window (an omitted one on a fresh
    # row means `Last 30 days`, resolved at the route, not in the DDL). A default here would be a
    # second place that decides what "no window" means.
    window_kind: Mapped[ConnectorWindowKind] = mapped_column(
        connector_window_kind_enum, nullable=False
    )
    # `SmallInteger`: the value is a day count bounded by the connector's own retention cap (a
    # month, on the only connector that exists), so two bytes is three orders of magnitude of
    # headroom. Non-null only on a `relative` row.
    window_days: Mapped[int | None] = mapped_column(sa.SmallInteger, nullable=True)
    # `Date`, not a timestamp: the citizen picks calendar days in an `Asia/Kolkata` grid, and
    # storing an instant would make the stored value depend on the reader's zone. Both non-null
    # only on an `absolute` row. Stored UNCLAMPED — the thirty-day floor and the same-day ceiling
    # are applied on the READ, in one place, so a window ages out on its own instead of a stored
    # value slowly becoming a lie.
    window_start: Mapped[date | None] = mapped_column(sa.Date, nullable=True)
    window_end: Mapped[date | None] = mapped_column(sa.Date, nullable=True)
