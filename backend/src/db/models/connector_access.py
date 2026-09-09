"""The `connector_access_requests` table — one row per time a person asked to reach a connector.

ACCESS BELONGS TO THE PERSON, NOT TO A PROJECT (R2). The grant keys on `user_id` alone: an
administrator answers once, about a human being, and every project that person owns — including the
ones they have not made yet — inherits the answer. A project never asks for permission; it only
answers "switched on here?" and "how far back?", which is what `project_connectors` stores.

AN APPEND-ONLY LEDGER, NOT A STATE ROW. The obvious alternative is one mutable row per
`(user_id, connector_key)` carrying the current state. It is rejected: the decision is auditable
history — who decided, when, and in whose words — and a citizen may ask, cancel and ask again,
which a single row would either overwrite or refuse. So each ask is its own row and the person's
CURRENT state is derived from the rows (U3), by the rule "the most recent NON-cancelled row". Naive
"latest row wins" is wrong twice over: somebody who asks and then cancels would read `Cancelled`,
which is not one of the four person states at all, and somebody whose entire history is cancelled
would read it forever instead of returning to `Never asked`.

THE PARTIAL UNIQUE INDEX IS THE CONCURRENCY GUARD, and it is what makes the cancel route's phrase
"the caller's pending row" unambiguous: at most one `pending` row per `(user_id, connector_key)`,
with approved / declined / cancelled rows accumulating freely underneath it. A plain
`UniqueConstraint` on the pair would forbid the second ask entirely; no constraint at all would let
a double-submitted dialog put two identical requests in the administrator's queue.

`declined` IS TERMINAL FOR THIS PASS. `Ask again` is not built (owner ruling, 2026-09-08), and this
pass gives an administrator no way to grant access directly either — so a decline has no path back,
server-side as well as in the UI. Say that to the client rather than letting them find it in the
first mis-click.

THERE IS NO `withdrawn` MEMBER. Person state 5 on the `ConnectorStates` board — the connector's
owner revoking the platform's access — is out of this pass (origin Q2), nothing here could set it,
and an unreachable label would cost an `ALTER TYPE` to remove later. Its absence is deliberate, not
an oversight.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from enum import StrEnum

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base
from src.db.mixins import OwnedByUserMixin, TimestampMixin, UUIDv7PrimaryKeyMixin

# The stored width of a `connector_key`. Both connector tables use it, and it is declared HERE
# rather than on the registry in `src/core/connectors.py` — the semantically better home — because
# U2 gives that module `from src.services.usage import ist_today`, and `src.services.usage`
# re-exports from `src.db.models.token_usage`; a model importing the registry would close a
# models → core → services → models loop. `tests/db/test_connector_models.py` asserts every
# registry key fits this width, so the two cannot drift apart unnoticed.
MAX_CONNECTOR_KEY = 32


class ConnectorRequestStatus(StrEnum):
    """Where one request stands. Values are the native PG enum labels — lowercase, stable, and
    safe to put straight into an API response.

    `cancelled` is the citizen withdrawing their own ask before anybody decided; it is NOT a
    decision and carries no decider, no `decided_at` and no remark. See the module docblock for
    why there is no `withdrawn`."""

    PENDING = "pending"
    APPROVED = "approved"
    DECLINED = "declined"
    CANCELLED = "cancelled"


# The native PG enum type, shared by the model column and the Alembic migration.
# `values_callable` is mandatory (ADR-0008): without it SQLAlchemy would send the enum member
# NAMES (`PENDING`) rather than the values (`pending`), and the labels in the database would not
# be the strings this codebase reads and writes everywhere else.
# `create_type=False`: migration 0039 owns CREATE/DROP TYPE explicitly, so a downgrade actually
# drops it — dropping a table does not drop its type.
connector_request_status_enum = sa.Enum(
    ConnectorRequestStatus,
    name="connector_request_status",
    values_callable=lambda enum: [member.value for member in enum],
    create_type=False,
)


class ConnectorAccessRequest(UUIDv7PrimaryKeyMixin, OwnedByUserMixin, TimestampMixin, Base):
    __tablename__ = "connector_access_requests"

    __table_args__ = (
        # AT MOST ONE OPEN ASK per person per connector. Partial (not a `UniqueConstraint`)
        # because the uniqueness holds only while the row is `pending` — see the module docblock.
        # A partial index cannot be named as an `ON CONSTRAINT` target, so U3's insert infers
        # against it with `index_elements=[...] , index_where=...`.
        #
        # THE PREDICATE IS A LITERAL AND MUST STAY ONE. `status = 'pending'` written as a bound
        # parameter anywhere the planner has to match this index would stop matching it from the
        # sixth execution on a pooled connection, once Postgres switches to a generic plan — the
        # same trap `ix_deployments_success_collapse` documents.
        sa.Index(
            "uq_connector_access_requests_one_pending",
            "user_id",
            "connector_key",
            unique=True,
            postgresql_where=sa.text("status = 'pending'"),
        ),
        # The read axis for every citizen-facing query: "this person's rows for this connector,
        # newest first". `OwnedByUserMixin` already indexes `user_id` alone, which serves the
        # Integrations dialog's all-connectors read; this composite serves the per-connector one.
        sa.Index(
            "ix_connector_access_requests_user_connector",
            "user_id",
            "connector_key",
        ),
    )

    # WHICH connector, as a key rather than a boolean or a foreign key (R15). There is no
    # `connectors` table to point at — `src/core/connectors.py` is the catalogue — so an unknown
    # key is caught at the route (404), never by the database.
    connector_key: Mapped[str] = mapped_column(sa.String(MAX_CONNECTOR_KEY), nullable=False)

    status: Mapped[ConnectorRequestStatus] = mapped_column(
        connector_request_status_enum,
        server_default=ConnectorRequestStatus.PENDING.value,
        nullable=False,
    )

    # WHY THE PERSON SAYS THEY NEED IT. NOT NULL and required at the API (5-50 words, the same
    # `clean_stated_reason` rule the project-deletion reason uses) because it is the whole of
    # what the administrator has to decide on — the decide dialog leads with it. `Text`, not a
    # bounded `String`: the schema layer owns the length rule, and a column cap would turn a
    # rejected paste into a 500 instead of a 422.
    requester_remarks: Mapped[str] = mapped_column(sa.Text, nullable=False)

    # WHO DECIDED. `ON DELETE SET NULL`, not CASCADE: the trail outlives the actor, exactly as the
    # audit model reasons. An administrator leaving BIAL must not delete the record that somebody
    # else's access was granted — the citizen keeps their grant, and the row keeps its date and its
    # remark with an unnamed decider. NULL is also the ordinary state of every pending and
    # cancelled row, so a reader must never treat NULL here as "the decider was deleted".
    decided_by_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid,
        sa.ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    decided_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)

    # THE ADMINISTRATOR'S WORDS, WRITTEN ONLY ON A DECLINE, and shown back to the citizen verbatim
    # on their Integrations row — the whole of what a refused person is owed, since `Ask again` is
    # not built. An APPROVAL stores nothing here (R10): the `AdminReview` board draws a permanent
    # `REQUIRED` pill on `YOUR REMARKS` and a sentence saying approving needs one too, and both
    # come off — approval is a click, decline reveals the box. So NULL on an `approved` row is
    # correct, not a missing write. Rendered as plain text on every surface, never through a
    # markdown component: one user writes it and another reads it.
    decision_remarks: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
