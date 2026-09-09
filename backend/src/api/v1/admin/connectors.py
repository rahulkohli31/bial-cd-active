"""The administrator's connector queue and the two decisions behind it.

A THIRD ADMIN ROUTER, IN ITS OWN MODULE. `admin/router.py` is ~2,500 lines and carries two mount
points already; a fourth domain inside it would be found by scrolling. Its schemas nevertheless
stay in `admin/schemas.py` with the other two surfaces' — see the section comment there. Nothing
is imported from `admin/router.py`: `_transition` is module-private, so the guarded-update SHAPE
is copied here rather than shared, and that file's own `enable` route deliberately bypasses it
with a hand-written UPDATE, which makes it a pattern rather than a law.

THIS IS THE ONE CONNECTOR SURFACE THAT READS ACROSS USERS, and it says so out loud. Every
statement in `api/v1/connectors/` carries `user_id == user.id`; every statement here carries no
such predicate on purpose, because the queue's whole job is to show one administrator what other
people have asked for. The gate is `CurrentSuperadmin` — declared on every route in this module,
with no exception — and the two writes are audited. That is the trade ADR-0004 and ADR-0005 name:
reaching across owners is an explicit, role-gated, audited admin action.

BOTH DECISION ROUTES DECLARE `RequireCsrf`, DELIBERATELY DIVERGING FROM `admin/router.py`, which
declares it zero times. CSRF is opt-in per route in this codebase (`api/deps_csrf.py`: "a route
opts IN via `RequireCsrf`"), so following the neighbouring admin router's precedent would ship
approve and decline behind nothing but a session cookie and `SameSite=Lax` — which that module's
own docstring calls "a second line, not the only one". A forged approval of a data-access grant
is not a defect worth inheriting; it costs one line, and the portal's `authFetch` already sends
the header on every mutating call. The codebase-wide gap this reveals is not this pass's to close.

WHAT THE AUDIT ROWS CARRY, AND WHAT THEY DO NOT. `connector:approve` / `connector:approve:self` /
`connector:decline` on `resource_type="connector_request"`, `resource_id` the request id, and an
IDS-ONLY `detail` naming the connector key and the subject user. THE DECLINE REMARK IS NOT IN
THERE, and that is not the general "no record contents" rule — `db/models/audit.py` explicitly
allows text the ACTOR authored about the act, which is how `app:delete` stores its reason. The
reason that exception does not apply here is narrower and better: `app:delete` destroys the thing
its reason describes, so the audit row is the only durable home those words have. A decline remark
already has one — `connector_access_requests.decision_remarks`, which this queue reads back and
which the citizen's own state response quotes to them verbatim. A copy here would be a second
emitter of one sentence, free to drift the day either side is edited.

R18: nothing in this module names the connector. The catalogue is iterated
(`src/core/connectors.py`), the key is a value, and the display name rides the wire.
"""

from __future__ import annotations

import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from typing import Annotated, Any, Final

import sqlalchemy as sa
from fastapi import APIRouter, Query, status
from pydantic import TypeAdapter
from sqlalchemy import Row

from src.api.deps import DbSession
from src.api.deps_csrf import RequireCsrf
from src.api.deps_rbac import CurrentSuperadmin
from src.api.v1.admin.schemas import (
    ConnectorDecisionResponse,
    ConnectorDeclineRequest,
    ConnectorRequestListResponse,
    ConnectorRequestRow,
    ConnectorWaitingCountResponse,
)
from src.api.v1.connectors.schemas import ConsentLine
from src.api.v1.pagination import SearchQuery, clean_search
from src.core.connectors import CONNECTORS
from src.core.errors import AppApiError
from src.db.models.connector_access import ConnectorAccessRequest, ConnectorRequestStatus
from src.db.models.project import Project
from src.db.models.project_connector import ProjectConnector
from src.db.models.user import User
from src.schemas import ADMIN_AUTH, AUTH_401, DetailBody, ErrorEnvelope, error_responses
from src.services.audit.log import append_audit

router = APIRouter(prefix="/admin/connector-requests", tags=["admin"])

# How many queue rows one listing returns. Declared HERE rather than imported from
# `admin/router.py`: that constant is documented as "how many registry rows one listing returns"
# and belongs to the app-registry table. Two listings sharing one number by accident is how a
# later change to one queue silently repages the other. Same value, same sentinel-row shape, and
# — like the app registry — REPORTED rather than hidden, because nothing bounds how many people
# may ask for a connector and a queue that quietly dropped its tail would leave somebody waiting
# forever behind a screen that reads caught-up.
LISTING_CAP = 200

# The two tables `AdminQueue` draws, as the row statuses behind them. `cancelled` is in NEITHER,
# and that is why the states are named rather than filtered as `!= pending`: a citizen who
# withdrew their ask is not waiting on anybody and had no decision made about them, so the row
# belongs on no administrator's screen. It survives in the ledger as history.
_WAITING: Final = "waiting"
_QUEUE_STATES: Final[Mapping[str, tuple[ConnectorRequestStatus, ...]]] = {
    _WAITING: (ConnectorRequestStatus.PENDING,),
    "decided": (ConnectorRequestStatus.APPROVED, ConnectorRequestStatus.DECLINED),
}

# What a later refusal calls a decision that already happened. Keyed by the STORED status, not by
# the one being attempted: the administrator who lost the race needs to be told what the winner
# actually did, which may be the opposite of what they were about to do.
_ALREADY: Final[Mapping[ConnectorRequestStatus, str]] = {
    ConnectorRequestStatus.APPROVED: "approved",
    ConnectorRequestStatus.DECLINED: "declined",
}

_INVALID_STATE = "Ask for `waiting` or `decided`."
_NO_SUCH_CONNECTOR = "That connector is not available."
_REQUEST_NOT_FOUND = "Request not found."
_REQUEST_CANCELLED = "This request was cancelled before you decided it."
_NOT_WAITING = "This request is no longer waiting on a decision."
_AN_ADMINISTRATOR = "An administrator"

# The 409 the loser of a two-administrator race gets. The message names WHO and WHAT; the WHEN
# rides `error.detail` as an instant, because this codebase formats no human-readable date on the
# server (there is not one `strftime` in `src/` outside an ISO-8601 serializer) and the console
# rendering this already formats every date on the screen behind it. A sentence built here plus a
# field rendered there would be two emitters of one timestamp. `AppApiError.detail` exists for
# exactly this: a refusal that withholds what it already measured makes the caller guess.
_ALREADY_DECIDED = "{who} has already {what} this request."

# The instant in that 409's `detail`, spelled by the SAME serializer every other timestamp in
# this feature crosses the wire through. `datetime.isoformat()` is JSON-ready too and would give
# `+00:00` where every response body gives `Z` — two spellings of one kind of value on one
# feature's wire, which is a client-side date parser away from being a bug report.
_INSTANT: Final = TypeAdapter(datetime)


def _display(name: str | None, email: str) -> str:
    """A person's handle for the screen: their display name, or the work email behind it.

    `users.display_name` IS NULLABLE, and the substitution is a rule rather than a formatting
    choice — the same one `services/connectors/access.PersonAccess` already applies to a
    decider's name. Done on the server so no panel writes a second fallback and no cell renders
    an empty string beside an authorization decision."""
    return name or email


def _connector_name(connector_key: str) -> str:
    """The catalogue's name for a stored key.

    FALLS BACK TO THE KEY ITSELF for a row naming a connector the registry no longer offers.
    There is no removal path today, so this is unreachable — but the alternative on the day
    somebody retires an entry is a `KeyError` on the one screen an administrator opens to find
    out what is waiting, and a lowercase key on screen is a better failure than a 500."""
    connector = CONNECTORS.get(connector_key)
    return connector.display_name if connector is not None else connector_key


def _consent_lines(connector_key: str) -> list[ConsentLine]:
    """`WHAT APPROVING GIVES THEM`, as wire objects, in registry order.

    A COPY OF THE ORDER AND NOTHING ELSE — the citizen router's `_consent_lines` beside its own
    panel, with the OTHER tuple. No filtering, no joining, no re-voicing: these are R1-binding
    consent sentences pinned byte-exact in `tests/db/test_connector_models.py`, and the dialog
    that renders them is a renderer.

    THE APPROVER'S SET, NEVER THE REQUESTER'S. They are third person and only this one names the
    day cap; handing the citizen's second-person tuple to an administrator would ship copy about
    what "you build" to somebody who is not building anything. (Quoting either set here would put
    one connector's name in this module and break R18's grep — the sentences live on the entry.)

    AN EMPTY LIST FOR A KEY THE REGISTRY NO LONGER OFFERS, and — unlike `_connector_name` above
    — the browser treats that as a contract break rather than degrading. The two differ because
    the values differ in kind: a lowercase key where a display name should be is legible, while
    a consent box with a heading and no consent under it asks somebody to approve a data grant
    without saying what it grants. The portal's strict parse throws and the queue shows its
    error-and-retry state, which is the honest one."""
    connector = CONNECTORS.get(connector_key)
    if connector is None:
        return []
    return [
        ConsentLine(lead=line.lead, body=line.body) for line in connector.consent_lines_approver
    ]


# One listing row as the join hands it over: the request, then its ASKER's two name columns.
_QueueRow = Row[tuple[ConnectorAccessRequest, str | None, str]]


async def _decider_names(db: DbSession, decider_ids: set[uuid.UUID]) -> dict[uuid.UUID, str]:
    """Each deciding administrator's handle, by id — one query for the page, never per row.

    A SECOND LOOKUP RATHER THAN A SECOND JOIN. The two people on a decided row are the same table
    twice: the asker (`user_id`, an inner join) and the decider (`decided_by_id`, which would
    have to be an OUTER one). A single statement would need an aliased self-join whose two
    `users` entries a reader has to keep apart at every projection, to save one round trip per
    page. This costs a dict.

    A MISSING KEY CANNOT HAPPEN, and that is the FK's doing rather than this function's: a
    decider who is deleted takes `decided_by_id` to NULL (`ON DELETE SET NULL`), so a row that
    still names an id still has a user behind it. Rows with no decider are never asked about."""
    if not decider_ids:
        return {}
    rows = await db.execute(
        sa.select(User.id, User.display_name, User.email).where(User.id.in_(decider_ids))
    )
    return {row_id: _display(name, email) for row_id, name, email in rows}


async def _enabled_project_counts(
    db: DbSession, pairs: Sequence[tuple[uuid.UUID, str]]
) -> dict[tuple[uuid.UUID, str], int]:
    """`USING IT IN`, for the whole page in ONE query — how many of each person's projects have
    that connector switched on.

    KEYED BY `(user_id, connector_key)`, NOT BY USER. One person may hold decisions on several
    connectors once the catalogue has more than one entry, and a per-user key would add their
    counts together — the number under `USING IT IN` would then be right only while the registry
    has one entry, which is exactly the kind of claim that rots without going red.

    THE `user_id` PREDICATE IS ON `projects`, WHICH IS `project_connectors`' OWNERSHIP ANCHOR
    (that table deliberately carries no `user_id` of its own). Grouping by it is what keeps a
    second person's enabled projects out of this person's count — this is a cross-user read, so
    the grouping key IS the isolation here, and dropping it would total the whole platform's
    switches onto every row.

    COUNTING `enabled` IS COUNTING EFFECTIVE STATE HERE, and only here: the column is projected
    onto APPROVED rows only, so `enabled AND the owner is approved` (R12/R13,
    `core.connectors.resolve_window`) has its second conjunct already true for everything
    counted. It is not a licence to read `enabled` and call it "on" anywhere else."""
    if not pairs:
        return {}
    user_ids = {user_id for user_id, _key in pairs}
    keys = {key for _user_id, key in pairs}
    rows = await db.execute(
        sa.select(Project.user_id, ProjectConnector.connector_key, sa.func.count())
        .select_from(ProjectConnector)
        .join(Project, Project.id == ProjectConnector.project_id)
        .where(
            Project.user_id.in_(user_ids),
            ProjectConnector.connector_key.in_(keys),
            ProjectConnector.enabled.is_(True),
        )
        .group_by(Project.user_id, ProjectConnector.connector_key)
    )
    return {(user_id, key): int(count) for user_id, key, count in rows}


def _row(
    queued: _QueueRow,
    decider_names: Mapping[uuid.UUID, str],
    counted: Mapping[tuple[uuid.UUID, str], int],
) -> ConnectorRequestRow:
    """One queue row, assembled in the ONE place either table's shape is decided."""
    request, display_name, email = queued
    approved = request.status is ConnectorRequestStatus.APPROVED
    return ConnectorRequestRow(
        id=request.id,
        user_id=request.user_id,
        display_name=_display(display_name, email),
        email=email,
        connector_key=request.connector_key,
        connector_display_name=_connector_name(request.connector_key),
        consent_lines_approver=_consent_lines(request.connector_key),
        requester_remarks=request.requester_remarks,
        asked_at=request.created_at,
        status=request.status,
        decided_at=request.decided_at,
        decided_by_id=request.decided_by_id,
        # `None` for a waiting row and for a decision whose administrator has since been
        # deleted; those two are told apart by `status`, never by this field.
        decided_by_name=(
            None if request.decided_by_id is None else decider_names.get(request.decided_by_id)
        ),
        decision_remarks=request.decision_remarks,
        # `0` is a real answer — an approved person who has not switched it on anywhere yet —
        # and `None` means the column is not asked on this row. The board draws an em dash for
        # a decline, which is a different statement from "none".
        using_it_in=(
            counted.get((request.user_id, request.connector_key), 0) if approved else None
        ),
    )


@router.get(
    "",
    responses=error_responses(
        # One entry, two refusals, told apart by `error.code`: an unknown `state`
        # (`invalid_state`) and a connector that is not in the catalogue (`unknown_connector`).
        # Both are rejected rather than ignored — a filter silently dropped is a queue that
        # reads as empty when it is not.
        (400, ErrorEnvelope, "Unknown `state` or `connector` filter"),
        (422, ErrorEnvelope, "Over-long or unrepresentable `q`"),
        *ADMIN_AUTH,
    ),
)
async def list_connector_requests(
    admin: CurrentSuperadmin,
    db: DbSession,
    state: Annotated[str, Query()],
    connector: Annotated[str | None, Query()] = None,
    q: SearchQuery = None,
) -> ConnectorRequestListResponse:
    """Who has asked to reach a connector, and what was decided.

    `state` is required and selects the table. `waiting` returns the requests nobody has answered
    yet, OLDEST FIRST — the review-queue order the app registry already uses, so the person who
    has waited longest is on top. `decided` returns approvals and declines, newest decision
    first. A request the citizen withdrew appears in neither: nobody is waiting on it and nobody
    decided it.

    `connector` narrows to one catalogue key; `q` matches a person's name or work email,
    case-insensitively. Every row carries the asker's name and work email, the connector's
    display name, their remarks in full, and when they asked. A decided row adds who decided,
    when, their remark if they declined, and `usingItIn` — how many of that person's projects
    have this connector switched on, `null` on a decline.

    Capped at 200 rows. `truncated` is `true` when there are more, and the answer to it is `q`,
    not a page number."""
    statuses = _QUEUE_STATES.get(state)
    if statuses is None:
        raise AppApiError(status.HTTP_400_BAD_REQUEST, _INVALID_STATE, code="invalid_state")
    if connector is not None and connector not in CONNECTORS:
        # The registry is the catalogue (R15), so an unknown key is caught here and never by the
        # database. A 400 rather than the citizen routes' 404: there the key is a path segment
        # naming the resource being acted on, here it is a filter over a listing that exists.
        # The CODE is deliberately the same word, so the portal reads one vocabulary.
        raise AppApiError(
            status.HTTP_400_BAD_REQUEST, _NO_SUCH_CONNECTOR, code="unknown_connector"
        )
    search = clean_search(q)

    # THE CROSS-USER JOIN. No `user_id` predicate, on purpose: this is the admin surface, and its
    # subject is other people's requests. `join`, not `outerjoin` — `user_id` is a NOT NULL,
    # ON DELETE CASCADE FK, so a request without its asker cannot exist.
    query = (
        sa.select(ConnectorAccessRequest, User.display_name, User.email)
        .join(User, User.id == ConnectorAccessRequest.user_id)
        # ADR-0008: the status predicate goes through the mapped ORM column, so SQLAlchemy types
        # the binds as the native enum — asyncpg will not cast `varchar` to an enum implicitly.
        .where(ConnectorAccessRequest.status.in_(statuses))
    )
    if connector is not None:
        query = query.where(ConnectorAccessRequest.connector_key == connector)
    if search is not None:
        query = query.where(
            sa.or_(
                User.email.icontains(search, autoescape=True),
                User.display_name.icontains(search, autoescape=True),
            )
        )

    # THE TIE-BREAK ON `id` IS LOAD-BEARING, not decoration. `created_at` defaults to `now()`,
    # which in PostgreSQL is the TRANSACTION's timestamp — several rows written in one
    # transaction share it exactly, and the queue would then order them arbitrarily. `id` is a
    # UUIDv7, so it sorts by creation and settles the tie in the direction the timestamp meant.
    order: tuple[sa.UnaryExpression[Any], ...]
    if state == _WAITING:
        order = (ConnectorAccessRequest.created_at.asc(), ConnectorAccessRequest.id.asc())
    else:
        # `nullslast` because DESC puts NULLs FIRST in PostgreSQL. A decided row with no
        # `decided_at` cannot be written through this API, but if one ever existed it would sit
        # at the top of the administrator's screen with a blank `WHEN`.
        order = (
            ConnectorAccessRequest.decided_at.desc().nullslast(),
            ConnectorAccessRequest.id.desc(),
        )

    # One past the cap: the extra row is never projected, it only answers "is there more?"
    # without a second COUNT query.
    rows = (await db.execute(query.order_by(*order).limit(LISTING_CAP + 1))).all()
    truncated = len(rows) > LISTING_CAP
    page: list[_QueueRow] = list(rows[:LISTING_CAP])

    decider_names = await _decider_names(
        db, {request.decided_by_id for request, _n, _e in page if request.decided_by_id}
    )
    # Counted only for the rows that render the column: a declined row draws an em dash, so
    # tallying its projects would be work whose answer is thrown away.
    counted = await _enabled_project_counts(
        db,
        [
            (request.user_id, request.connector_key)
            for request, _n, _e in page
            if request.status is ConnectorRequestStatus.APPROVED
        ],
    )

    return ConnectorRequestListResponse(
        requests=[_row(queued, decider_names, counted) for queued in page],
        truncated=truncated,
    )


@router.get("/counts", responses=error_responses(*ADMIN_AUTH))
async def connector_request_counts(
    admin: CurrentSuperadmin, db: DbSession
) -> ConnectorWaitingCountResponse:
    """How many people are waiting on a decision — the Integrations tab badge's source.

    One indexed count: no join, no row projection, no per-person project tally. A DEDICATED route
    rather than `len(requests)` off the listing, for the reason the app registry's `/counts`
    gives: that listing projects up to 200 rows, joins `users` twice and counts every approved
    person's enabled projects, and a badge polling it would pay all of that on a cadence — and
    pay MORE as the queue it reports on grows, which is exactly backwards.

    An empty queue is `{"waiting": 0}`, never a 404: "nothing is waiting" and "we did not ask"
    must not render as the same pixel."""
    waiting = await db.scalar(
        sa.select(sa.func.count())
        .select_from(ConnectorAccessRequest)
        .where(ConnectorAccessRequest.status == ConnectorRequestStatus.PENDING)
    )
    return ConnectorWaitingCountResponse(waiting=int(waiting or 0))


_DECISION_RESPONSES = error_responses(
    AUTH_401,
    # ONE ENTRY, TWO REFUSALS, TWO BODY SHAPES — `error_responses` rejects a duplicate status, so
    # the pair is documented in prose. The superadmin gate raises a bare `HTTPException`
    # (`{"detail"}`, the model named here); the CSRF gate raises `AppApiError`
    # (`{"error": {"code": "csrf_failed"}}`). A client tells them apart by the body it gets.
    (403, DetailBody, "Super-admin privileges required, or the CSRF check failed (`csrf_failed`)"),
    (404, ErrorEnvelope, "No such request"),
    (
        409,
        ErrorEnvelope,
        "Another administrator decided it first (`already_decided`), the citizen withdrew it "
        "(`request_cancelled`), or it is otherwise no longer waiting (`not_waiting`)",
    ),
)


async def _refusal(db: DbSession, request_id: uuid.UUID) -> AppApiError:
    """Say WHY the guarded update moved nothing. Returns the error for the caller to raise.

    RETURNS RATHER THAN RAISES so `raise await _refusal(...)` reads as a raise at the call site
    and narrows the row type for every checker in the gate — an `async def` that only ever raises
    is not something all four of them agree is unreachable.

    Read in the SAME TRANSACTION as the update that found nothing, so the answer is the state
    that refused it rather than a state that has moved on since."""
    stale = (
        await db.execute(
            sa.select(
                ConnectorAccessRequest.status,
                ConnectorAccessRequest.decided_at,
                User.display_name,
                User.email,
            )
            # OUTER, because the decider may have been deleted since (`ON DELETE SET NULL`) and
            # every pending or cancelled row has no decider at all. An inner join would turn
            # both of those into a 404 saying the request does not exist.
            .outerjoin(User, User.id == ConnectorAccessRequest.decided_by_id)
            .where(ConnectorAccessRequest.id == request_id)
        )
    ).first()
    if stale is None:
        return AppApiError(status.HTTP_404_NOT_FOUND, _REQUEST_NOT_FOUND, code="request_not_found")

    already = _ALREADY.get(stale.status)
    if already is not None and stale.decided_at is not None:
        # NAME WHO, AND CARRY WHEN. `decidedByName` is `null` only when the deciding
        # administrator's account is gone, and the sentence then reads "An administrator" rather
        # than leaving a hole where a person should be.
        who = None if stale.email is None else _display(stale.display_name, stale.email)
        return AppApiError(
            status.HTTP_409_CONFLICT,
            _ALREADY_DECIDED.format(who=who or _AN_ADMINISTRATOR, what=already),
            code="already_decided",
            detail={
                "status": stale.status.value,
                "decidedByName": who,
                "decidedAt": _INSTANT.dump_python(stale.decided_at, mode="json"),
            },
        )

    if stale.status is ConnectorRequestStatus.CANCELLED:
        # THE CASE THE DECIDED READ CANNOT SERVE. A citizen who withdraws between the
        # administrator's render and their click leaves a row whose decision fields are BOTH
        # null, so the sentence above would name nobody at no time at all. No `detail` either:
        # there is nothing measured to hand over. NOT called "withdrawn" — see `_decide`.
        return AppApiError(status.HTTP_409_CONFLICT, _REQUEST_CANCELLED, code="request_cancelled")

    # Still `pending` after an UPDATE that matched nothing is not reachable through this API —
    # PostgreSQL serialises the two writers and the loser re-reads the winner's status. It is
    # answered rather than assumed away because the alternative, on the day a fifth status or a
    # second writer appears, is a decided-looking 200 over a row nothing moved.
    return AppApiError(status.HTTP_409_CONFLICT, _NOT_WAITING, code="not_waiting")


async def _decide(
    db: DbSession,
    admin: User,
    request_id: uuid.UUID,
    *,
    target: ConnectorRequestStatus,
    remarks: str | None,
) -> ConnectorDecisionResponse:
    """Move ONE waiting request to `target`, audit it, and commit both together.

    THE GUARDED UPDATE IS THE WHOLE GATE. `status = 'pending'` sits in the WHERE clause, not in a
    read before it: two administrators opening the same queue and clicking within a second of
    each other is ordinary rather than exotic, and a read-then-write would let the second
    overwrite the first's decision, their remark and their name. Zero rows updated is a refusal,
    never a no-op that reports success. This is `admin/router.py::_transition`'s shape, copied
    rather than imported: that helper is module-private, keyed to `STATUS_TRANSITIONS[AppStatus]`,
    and its own `enable` route bypasses it — a pattern to follow, not a function to call.

    THE ZERO-ROW BRANCH HAS THREE OUTCOMES, NOT ONE, and telling them apart needs a follow-up
    SELECT in the SAME TRANSACTION: a guarded UPDATE reports how many rows moved, never why none
    did. The administrator who lost a race is told who won it and when; the one whose citizen
    withdrew in the meantime is told THAT instead, because the decision fields on a cancelled row
    are both NULL and the race sentence would render a nameless decider at no time. The third arm
    exists so a status this function does not know about cannot pass as a success or as somebody
    else's decision. See `_refusal`.

    THE WORD `withdrawn` APPEARS NOWHERE IN THIS PATH, deliberately. It is reserved for the person
    state `ConnectorStates` draws fifth — the connector's owner revoking the platform's access —
    which this pass keeps out of the enum, the resolver and the scope table. A wire-facing
    `withdrawn` code for the unrelated act of a citizen cancelling their own ask would hand the
    next reviewer grepping for that state a false hit.

    `append_audit` FLUSHES AND DOES NOT COMMIT, so the accountability row and the state change
    share one transaction and the single `db.commit()` below: a decision whose record could not
    be written is rolled back with it, and there is never a trail entry for a decision that did
    not happen."""
    now = datetime.now(UTC)
    decided = (
        await db.execute(
            sa.update(ConnectorAccessRequest)
            .where(
                ConnectorAccessRequest.id == request_id,
                # ADR-0008 again: through the mapped column, so the bind is the native enum.
                ConnectorAccessRequest.status == ConnectorRequestStatus.PENDING,
            )
            .values(
                status=target,
                decided_by_id=admin.id,
                decided_at=now,
                # Stated even on an approval, where it writes the NULL the column already holds:
                # "an approval stores no remark" (R10) is a decision, and a decision invisible in
                # the code is one a later reader undoes by accident.
                decision_remarks=remarks,
            )
            # Core `.returning(...)` scalars, because every value below is read AFTER the commit
            # and an ORM attribute must never be read across one.
            .returning(
                ConnectorAccessRequest.user_id,
                ConnectorAccessRequest.connector_key,
                ConnectorAccessRequest.decided_at,
            )
        )
    ).first()
    if decided is None:
        raise await _refusal(db, request_id)

    # A SUPER-ADMIN APPROVING THEIR OWN REQUEST IS RECORDED DISTINGUISHABLY, NOT FORBIDDEN. RBAC
    # has two computed roles and no concept of a second approver, and ADR-0005 records the
    # missing separation of duties at this gate as an accepted consequence — so the answer is a
    # trail that can be QUERIED, not a refusal that would leave an administrator unable to reach
    # the data they administer. `approve:self` follows the app registry's own precedent, and the
    # action vocabulary is an open `String(64)`, so no migration is involved.
    #
    # ONLY APPROVE HAS THE VARIANT, exactly as the app registry has `approve:self` and no
    # `reject:self`. Refusing yourself grants nothing, so there is nothing for a reviewer to
    # query — and an action word that exists only to be symmetrical is one more thing whose
    # meaning has to be looked up.
    approving = target is ConnectorRequestStatus.APPROVED
    action = "connector:approve" if approving else "connector:decline"
    if approving and decided.user_id == admin.id:
        action += ":self"

    await append_audit(
        db,
        actor_id=admin.id,
        action=action,
        resource_type="connector_request",
        resource_id=str(request_id),
        # IDS AND A KEY ONLY. The remark stays on the request row — see the module docblock for
        # why this is not the call `app:delete` made about its own reason.
        detail={"connectorKey": decided.connector_key, "userId": str(decided.user_id)},
    )
    await db.commit()
    return ConnectorDecisionResponse(
        request_id=request_id,
        user_id=decided.user_id,
        connector_key=decided.connector_key,
        status=target,
        decided_at=decided.decided_at,
    )


@router.post("/{request_id}/approve", dependencies=[RequireCsrf], responses=_DECISION_RESPONSES)
async def approve_connector_request(
    request_id: uuid.UUID, admin: CurrentSuperadmin, db: DbSession
) -> ConnectorDecisionResponse:
    """Give this person access to the connector they asked for.

    NO BODY AND NO REMARK. An approval is a click: nothing is stored beyond who decided and when,
    because an approval remark would be readable nowhere — the citizen is never shown one, the
    audit row carries ids, and the decided table has no remarks column. One decision covers every
    project that person owns, including the ones they have not made yet.

    Only a request still waiting can be approved. `409 already_decided` names the administrator
    who answered first and carries the instant they did in `error.detail`; `409 request_cancelled`
    says the citizen withdrew the ask between your screen and your click. An unknown request is a
    404. Writes one `connector:approve` audit row — `connector:approve:self` when you are
    approving your own request, which is allowed and recorded rather than refused."""
    return await _decide(
        db, admin, request_id, target=ConnectorRequestStatus.APPROVED, remarks=None
    )


@router.post("/{request_id}/decline", dependencies=[RequireCsrf], responses=_DECISION_RESPONSES)
async def decline_connector_request(
    request_id: uuid.UUID,
    body: ConnectorDeclineRequest,
    admin: CurrentSuperadmin,
    db: DbSession,
) -> ConnectorDecisionResponse:
    """Refuse this person's request, in words they will read.

    `remarks` is required: 5 to 50 words, the same rule the platform applies to every reason it
    asks somebody to state, and it reaches the citizen VERBATIM on their own Integrations row. It
    is the whole of what a refused person is told — a decline is final for now, and asking again
    is not offered — so a blank or one-word refusal is not accepted.

    Only a request still waiting can be declined; the two 409s and the 404 are the approve
    route's. Writes one `connector:decline` audit row carrying ids only: the words live on the
    request row, which is where both this queue and the citizen read them from."""
    return await _decide(
        db, admin, request_id, target=ConnectorRequestStatus.DECLINED, remarks=body.remarks
    )
