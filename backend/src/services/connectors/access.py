"""Where ONE person stands with ONE connector — the state rule, and nothing else.

THE RULE IS "THE MOST RECENT NON-CANCELLED ROW", NOT "THE LATEST ROW". `connector_access_requests`
is an append-only ledger (see its model docblock), so the person's current state is derived from
their rows rather than stored. The naive `ORDER BY created_at DESC LIMIT 1` is wrong twice over,
and both ways are reachable from the shipped UI:

1. Somebody who asks and then presses `Cancel` reads `cancelled` — which is not one of the four
   person states at all, so every surface that switches on the state falls through to nothing.
2. Somebody whose entire history is cancelled reads it FOREVER, instead of returning to
   `neverAsked` and being offered `Request access` again.

`tests/services/connectors/test_access_state.py` drives this function directly for exactly that
case, and `tests/api/v1/connectors/test_person_state.py` drives it again through the route.

WHY THIS IS A MODULE AND NOT AN INLINE QUERY (ADR-0010 wants a reason). Present-tense reuse:
`GET /v1/connectors` reads it, `POST /v1/connectors/{key}/request` refuses a second ask on it,
`POST /v1/connectors/{key}/cancel` reports the resulting state with it, and U4's per-project
switch-on refuses on it as well. The realized testing benefit is the direct test named above —
the cancelled-history case is the whole reason the rule is not one line, and testing it through
HTTP alone would leave the rule provable only by the surface that happens to call it.

FOUR PERSON STATES, NOT FIVE. `ConnectorStates` draws a fifth — `Withdrawn`, the connector's owner
revoking the platform's access — and it is out of this pass: there is no `withdrawn` status, and
nothing here could set one.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Final

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models.connector_access import ConnectorAccessRequest, ConnectorRequestStatus
from src.db.models.user import User


class ConnectorPersonState(StrEnum):
    """The four states `ConnectorStates` draws for THE PERSON, as they cross the wire.

    Values are camelCase because they are wire values, not database labels: `CamelModel` renames
    FIELDS, never the contents of a string enum, so `neverAsked` has to be spelled that way here
    or the portal reads `never_asked` in the one place the rest of the payload is camel.

    Three of the four coincide with `ConnectorRequestStatus` values, and that is convenience, not
    identity: `cancelled` is a row status that is deliberately NOT a person state, and
    `neverAsked` is a person state with no row behind it at all."""

    NEVER_ASKED = "neverAsked"
    PENDING = "pending"
    APPROVED = "approved"
    DECLINED = "declined"


@dataclass(frozen=True, slots=True)
class PersonAccess:
    """Where the caller stands, plus the row that says so — resolved together because every
    caller that wants one wants the other in the same breath.

    `request` is `None` for `neverAsked` ONLY, which includes the person whose whole history is
    cancelled: a cancelled row is not the row behind any state.

    `decided_by_name` rides along because `users.display_name` is NULLABLE and the fallback to
    the email is a rule, not a formatting choice — `Approved for you 2 Sep · ` with nothing after
    the separator is worse than a plain address. Deriving it here, off the same row-selection the
    state came from, is what stops each surface writing `display_name or email` again (the
    project tombstone already had to learn this). It is `None` when nobody has decided yet, and
    also when the administrator who decided has since been deleted — `decided_by_id` is
    `ON DELETE SET NULL`, so the decision outlives its decider and the row keeps its date and its
    remark with the decider unnamed. Those two cases are told apart by `state`, not by this field.
    """

    state: ConnectorPersonState
    request: ConnectorAccessRequest | None
    decided_by_name: str | None

    @property
    def request_status(self) -> ConnectorRequestStatus | None:
        """The stored status behind the state, in the shape `core.connectors.resolve_window`
        takes as `owner_access_state` — `None` for never-asked. The resolver is the one place
        that answers "is this connector on for this project", and it wants the ROW's vocabulary;
        handing it `state` instead would make a caller translate, which is a second rule."""
        return None if self.request is None else self.request.status


# A row status that reached the state mapping despite the query excluding it. Raised rather than
# defaulted: a person silently rendered as `neverAsked` because their cancelled row leaked past
# the predicate would offer `Request access` to somebody who is actually approved.
_CANCELLED_LEAKED: Final = (
    "a cancelled connector request reached the state mapping — "
    "current_access's status predicate should have excluded it"
)


def _person_state(status: ConnectorRequestStatus) -> ConnectorPersonState:
    """Map a SELECTED row's status onto the person state it means. Every member is named, so a
    fifth request status cannot be added without this function refusing to compile."""
    match status:
        case ConnectorRequestStatus.PENDING:
            return ConnectorPersonState.PENDING
        case ConnectorRequestStatus.APPROVED:
            return ConnectorPersonState.APPROVED
        case ConnectorRequestStatus.DECLINED:
            return ConnectorPersonState.DECLINED
        case ConnectorRequestStatus.CANCELLED:
            raise ValueError(_CANCELLED_LEAKED)


async def current_access(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    connector_key: str,
) -> PersonAccess:
    """Where `user_id` stands with `connector_key` right now, by the rule in the module docblock.

    Scoped by `user_id` in the WHERE clause, never by a check made after the row is loaded: this
    row carries one person's stated reason and another person's decision about them, and the
    predicate IS the isolation boundary (ADR-0004).

    `connector_key` is NOT validated here — the registry in `src/core/connectors.py` is the
    catalogue and the ROUTE 404s an unknown key before reaching this. An unknown key that did get
    here simply matches no rows and reads `neverAsked`, which is the truth about it.

    THE TIE-BREAK ON `id` IS LOAD-BEARING. `created_at` defaults to `now()`, which in PostgreSQL
    is the TRANSACTION's timestamp — two rows written in one transaction (a test seeding an
    approval and a later decline, or any future backfill) share it exactly. `id` is a UUIDv7, so
    it sorts by creation and settles the tie in the same direction the timestamp meant to.

    ADR-0008: the status predicate goes through the mapped ORM column, so SQLAlchemy types the
    bind as the native enum — asyncpg will not cast `varchar` to an enum implicitly.
    """
    row = (
        await db.execute(
            sa.select(ConnectorAccessRequest, User.display_name, User.email)
            .outerjoin(User, User.id == ConnectorAccessRequest.decided_by_id)
            .where(
                ConnectorAccessRequest.user_id == user_id,
                ConnectorAccessRequest.connector_key == connector_key,
                ConnectorAccessRequest.status != ConnectorRequestStatus.CANCELLED,
            )
            .order_by(
                ConnectorAccessRequest.created_at.desc(),
                ConnectorAccessRequest.id.desc(),
            )
            .limit(1)
        )
    ).first()

    if row is None:
        return PersonAccess(
            state=ConnectorPersonState.NEVER_ASKED, request=None, decided_by_name=None
        )

    request, display_name, email = row
    return PersonAccess(
        state=_person_state(request.status),
        request=request,
        # The email stands in for a missing display name, and `None` survives only when the
        # outer join matched nothing — nobody has decided, or the decider's account is gone.
        decided_by_name=(display_name or email) if email is not None else None,
    )
