"""The citizen's connectors: what the registry offers, where they stand, what each project reads.

TWO ROUTERS, TWO MOUNT POINTS, ONE DOMAIN. `router` hangs off `/v1/connectors` and answers for
the PERSON — the catalogue, their state, asking, withdrawing the ask, and the drill-down list of
their own projects. `project_router` hangs off `/v1/projects/{project_id}/connectors` and answers
for the PROJECT — which connectors it reads and over which days. `ConnectorStates` draws the two
as separate state machines and says why: an administrator answers once, about a person, and after
that a project only ever answers "switched on here?" and "how far back?".

THE OWNERSHIP CHECK A REVIEWER SHOULD BE ABLE TO MAKE BY READING TOP TO BOTTOM. There is no
cross-user read among these six routes. Every statement that touches `connector_access_requests`
carries `user_id == user.id` in its WHERE clause, and every statement that touches
`project_connectors` reaches it through a join on `projects` whose `user_id` predicate is in the
same clause (that table deliberately carries no `user_id` of its own — `projects` is its
ownership anchor). The only rows-free read is the registry itself, which is a module constant,
identical for everybody, and holds no user data.

NO WRITE HERE IS AUDITED, and that is a decision rather than an omission (R11/origin R9). The
citizen is acting on their OWN row: an audit entry would carry the same actor and the same
timestamp `connector_access_requests` already holds, so it would be a second copy of the row it
describes. The project switch is the same argument — `project_connectors` is its own record of
who set what, and `updated_at` dates it. Approve and decline — one person acting on another — DO
write one, in U5.

Errors use the data-plane `{"error": {"message", "code"}}` envelope (`AppApiError`), not the auth
endpoints' `{"detail": ...}`; the SPA already branches on `error.code`.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Final

import sqlalchemy as sa
from fastapi import APIRouter, status
from sqlalchemy.dialects.postgresql import insert as pg_insert

from src.api.deps import CurrentUser, DbSession
from src.api.deps_csrf import RequireCsrf
from src.api.v1.connectors.schemas import (
    AbsoluteWindowChoice,
    AccessRequestBody,
    ConnectorEntry,
    ConnectorListResponse,
    ConnectorProjectEntry,
    ConnectorProjectListResponse,
    ConnectorWindow,
    ConsentLine,
    ProjectConnectorEntry,
    ProjectConnectorListResponse,
    ProjectConnectorUpdate,
    RelativeWindowChoice,
    StoredWindow,
)
from src.core.connectors import CONNECTORS, Connector, resolve_window
from src.core.errors import AppApiError
from src.db.models.connector_access import ConnectorAccessRequest, ConnectorRequestStatus
from src.db.models.project import Project
from src.db.models.project_connector import ConnectorWindowKind, ProjectConnector
from src.schemas import AUTH_401, ErrorEnvelope, error_responses
from src.services.connectors import ConnectorPersonState, PersonAccess, current_access

router = APIRouter(prefix="/connectors", tags=["connectors"])

_NO_SUCH_CONNECTOR = "That connector is not available."
_ALREADY_ASKED = "You have already asked for access to this. An administrator is looking at it."
_ALREADY_DECIDED = "An administrator has already answered this request."
_NOTHING_TO_CANCEL = "There is no waiting request to cancel."

# A state that must have a row behind it arrived without one. Unreachable while `current_access`
# is the only producer of a `PersonAccess`; raised rather than papered over because the
# alternative is an entry that claims `approved` and renders a blank date and a blank name.
_STATE_WITHOUT_ITS_ROW = "a decided connector state arrived with no request row behind it"


def _known_connector(connector_key: str) -> Connector:
    """The registry is the catalogue (R15) — there is no `connectors` table, so an unknown key is
    caught HERE and never by the database. Membership first, then subscript: a `.get()` returning
    `None` would put the absent case and the present case on the same line."""
    if connector_key not in CONNECTORS:
        raise AppApiError(status.HTTP_404_NOT_FOUND, _NO_SUCH_CONNECTOR, code="unknown_connector")
    return CONNECTORS[connector_key]


async def _on_project_count(db: DbSession, user_id: uuid.UUID, connector_key: str) -> int:
    """How many of this person's projects have the connector switched on — `On in 2 projects ›`.

    Scoped through `projects`, which is `project_connectors`' ownership anchor: the `user_id`
    predicate is on the join target, and dropping it would count every citizen's projects.

    COUNTING `enabled` IS COUNTING EFFECTIVE STATE HERE, and only here. Effective on is
    `enabled AND the owner is approved` (R12/R13, `core.connectors.resolve_window`), and this
    number is rendered on the APPROVED row only — the second conjunct is already true for every
    row it counts. It is not a licence to read `enabled` and call it "on" anywhere else."""
    counted = await db.scalar(
        sa.select(sa.func.count())
        .select_from(ProjectConnector)
        .join(Project, Project.id == ProjectConnector.project_id)
        .where(
            Project.user_id == user_id,
            ProjectConnector.connector_key == connector_key,
            ProjectConnector.enabled.is_(True),
        )
    )
    return int(counted or 0)


def _consent_lines(connector: Connector) -> list[ConsentLine]:
    """The registry's requester consent tuple, as wire objects, in board order.

    A COPY OF THE ORDER AND NOTHING ELSE. No filtering, no joining, no re-voicing: the panel that
    renders these is a renderer, and the sentences are R1-binding consent copy pinned byte-exact
    in `tests/db/test_connector_models.py`. The approver's set stays where it is — it is third
    person and it names the day cap, and it belongs to the admin queue, not to this list."""
    return [
        ConsentLine(lead=line.lead, body=line.body) for line in connector.consent_lines_requester
    ]


async def _entry(
    db: DbSession, user_id: uuid.UUID, connector_key: str, connector: Connector
) -> ConnectorEntry:
    """One connector as this person sees it — the ONE place an entry is assembled.

    Both writes report their result through this rather than asserting the state they intended:
    the answer is a derivation over the person's remaining rows (`current_access`), and a route
    hard-coding `pending` after an insert or `neverAsked` after a cancel would be a second copy
    of that rule, correct only for as long as nobody adds a fifth status."""
    access = await current_access(db, user_id=user_id, connector_key=connector_key)
    state = access.state
    if state is ConnectorPersonState.NEVER_ASKED:
        return ConnectorEntry(
            key=connector_key,
            display_name=connector.display_name,
            subtitle=connector.subtitle,
            ask_subtitle=connector.ask_subtitle,
            consent_lines_requester=_consent_lines(connector),
            state=state,
        )

    row = access.request
    if row is None:
        raise ValueError(_STATE_WITHOUT_ITS_ROW)

    asked_at = row.created_at if state is ConnectorPersonState.PENDING else None
    approved = state is ConnectorPersonState.APPROVED
    declined = state is ConnectorPersonState.DECLINED
    return ConnectorEntry(
        key=connector_key,
        display_name=connector.display_name,
        subtitle=connector.subtitle,
        # Registry facts, identical in all four states — see `ConnectorEntry`'s docblock for why
        # they are NOT narrowed to the state the ask panel happens to be reachable from.
        ask_subtitle=connector.ask_subtitle,
        consent_lines_requester=_consent_lines(connector),
        state=state,
        asked_at=asked_at,
        approved_at=row.decided_at if approved else None,
        approved_by_name=access.decided_by_name if approved else None,
        on_project_count=(
            await _on_project_count(db, user_id, connector_key) if approved else None
        ),
        decided_at=row.decided_at if declined else None,
        decided_by_name=access.decided_by_name if declined else None,
        decision_remarks=row.decision_remarks if declined else None,
    )


@router.get("", responses=error_responses(AUTH_401))
async def list_connectors(user: CurrentUser, db: DbSession) -> ConnectorListResponse:
    """Every system this platform can connect to, and where you stand with each one.

    One entry per catalogue connector, always — a connector you have never asked about is
    present with the state `neverAsked`, because this list is the whole of what Integrations
    offers, not a list of your grants. Each entry carries only the fields its own state needs:
    `askedAt` while you wait, `approvedAt` / `approvedByName` / `onProjectCount` once an
    administrator has said yes, and `decidedAt` / `decidedByName` / `decisionRemarks` if they
    said no. Access is granted to a PERSON, so one answer covers every project you own,
    including the ones you have not made yet."""
    return ConnectorListResponse(
        connectors=[
            await _entry(db, user.id, key, connector) for key, connector in CONNECTORS.items()
        ]
    )


@router.post(
    "/{connector_key}/request",
    status_code=status.HTTP_201_CREATED,
    dependencies=[RequireCsrf],
    responses=error_responses(
        AUTH_401,
        (403, ErrorEnvelope, "CSRF check failed"),
        (404, ErrorEnvelope, "No such connector"),
        (409, ErrorEnvelope, "Already waiting on an administrator, or already answered"),
    ),
)
async def request_access(
    connector_key: str, body: AccessRequestBody, user: CurrentUser, db: DbSession
) -> ConnectorEntry:
    """Ask an administrator for access to a connector, for yourself.

    `remarks` is required and is the whole of what the administrator has to go on: 5 to 50 words,
    the same rule the platform applies to every reason it asks you to state.

    Refused with `409 already_pending` if you are already waiting on an administrator, and with
    `409 already_decided` if one has already answered — a decline is FINAL for now, and an
    approval you already hold is not improved by asking again. Returns the connector in its new
    state."""
    connector = _known_connector(connector_key)

    # THE HONEST-COPY PRE-CHECK; the insert below is the real gate for the pending case.
    # `approved` is refused for a reason beyond tidiness: a second pending row would become the
    # most recent non-cancelled row, so the person's state would fall back to `pending` and the
    # resolver would switch their connector OFF in every project until an administrator acted.
    access = await current_access(db, user_id=user.id, connector_key=connector_key)
    if access.state in (ConnectorPersonState.APPROVED, ConnectorPersonState.DECLINED):
        raise AppApiError(status.HTTP_409_CONFLICT, _ALREADY_DECIDED, code="already_decided")
    if access.state is ConnectorPersonState.PENDING:
        raise AppApiError(status.HTTP_409_CONFLICT, _ALREADY_ASKED, code="already_pending")

    # ON CONFLICT DO NOTHING, INFERRED AGAINST THE PARTIAL PENDING INDEX. A plain
    # check-then-insert loses the race that the portal makes ordinary rather than exotic:
    # `ComposerBox.tsx` records that `aria-disabled` "says so; it does not do so", so `Ask an
    # administrator` stays clickable while the first request is in flight, both clicks clear the
    # pre-check above, and the second violates `uq_connector_access_requests_one_pending` — a 500
    # where this route promises a 409.
    #
    # `index_elements` + `index_where` rather than `constraint=`: a PARTIAL index cannot be named
    # as an `ON CONSTRAINT` target. The predicate stays the LITERAL `status = 'pending'` the
    # model's `postgresql_where` uses — written as a bound parameter it would stop matching the
    # index from the sixth execution on a pooled connection, once Postgres switches to a generic
    # plan.
    inserted = await db.execute(
        pg_insert(ConnectorAccessRequest)
        .values(
            user_id=user.id,
            connector_key=connector_key,
            status=ConnectorRequestStatus.PENDING,
            requester_remarks=body.remarks,
        )
        .on_conflict_do_nothing(
            index_elements=["user_id", "connector_key"],
            index_where=sa.text("status = 'pending'"),
        )
        .returning(ConnectorAccessRequest.id)
    )
    if inserted.first() is None:
        # The citizen's other click landed between the pre-check and here — same person, two
        # requests in flight. Same refusal and same code as the pre-check: they asked twice and
        # are waiting once, which is the outcome they wanted.
        raise AppApiError(status.HTTP_409_CONFLICT, _ALREADY_ASKED, code="already_pending")

    await db.commit()
    return await _entry(db, user.id, connector_key, connector)


@router.post(
    "/{connector_key}/cancel",
    dependencies=[RequireCsrf],
    responses=error_responses(
        AUTH_401,
        (403, ErrorEnvelope, "CSRF check failed"),
        (404, ErrorEnvelope, "No such connector"),
        (409, ErrorEnvelope, "Nothing is waiting on an administrator"),
    ),
)
async def cancel_access_request(
    connector_key: str, user: CurrentUser, db: DbSession
) -> ConnectorEntry:
    """Withdraw your own waiting request for a connector.

    Only a request that is still waiting can be withdrawn — one an administrator has already
    answered is refused with `409 nothing_pending`, never silently accepted. Cancelling leaves
    the row as history and returns you to `neverAsked`, free to ask again. Returns the connector
    in its new state."""
    connector = _known_connector(connector_key)

    # THE GUARDED UPDATE IS THE WHOLE GATE — no pre-check, no read-then-write. Zero rows means
    # there was nothing pending (or an administrator decided it a moment ago), and that is a
    # refusal rather than a no-op: a `Cancel` that reports success while the request sails on
    # into the queue is the one outcome worse than an error. `user_id` is in the predicate, so a
    # crafted request naming somebody else's connector cancels nothing.
    #
    # ADR-0008: both status values go through the mapped ORM column, which types the binds as the
    # native enum — asyncpg will not cast `varchar` to an enum implicitly.
    cancelled = await db.execute(
        sa.update(ConnectorAccessRequest)
        .where(
            ConnectorAccessRequest.user_id == user.id,
            ConnectorAccessRequest.connector_key == connector_key,
            ConnectorAccessRequest.status == ConnectorRequestStatus.PENDING,
        )
        .values(status=ConnectorRequestStatus.CANCELLED)
        .returning(ConnectorAccessRequest.id)
    )
    if cancelled.first() is None:
        raise AppApiError(status.HTTP_409_CONFLICT, _NOTHING_TO_CANCEL, code="nothing_pending")

    await db.commit()
    return await _entry(db, user.id, connector_key, connector)


# ================================================================================
# THE PROJECT'S SWITCH AND ITS DAYS
# ================================================================================
#
# A SECOND ROUTER IN THE SAME MODULE, and the mount points are why. Everything above hangs off
# `/v1/connectors` because access belongs to the PERSON. Two of the three routes below hang off
# `/v1/projects/{project_id}/connectors` instead, because the switch and the days belong to the
# PROJECT (`ConnectorStates`: `Access is yours. The days are the project's.`). One `APIRouter`
# cannot carry two prefixes, and splitting the file would put one domain's five routes and its
# shared `_known_connector` in two places. The third — the drill-down list of the caller's own
# projects — stays on `router`: it is reached from the person's connector row, keys on nothing
# but `user_id`, and names no project in its path.
#
# THE OWNERSHIP CLAIM EXTENDS UNCHANGED. `project_connectors` carries no `user_id` of its own —
# `projects` is its ownership anchor — so every statement below reaches it through a join on
# `projects` whose `user_id` predicate sits in the same WHERE clause, and the two project-scoped
# routes prove ownership with a `SELECT ... WHERE id = :id AND user_id = :me` before they read or
# write anything. A project somebody else owns and a project that does not exist get the SAME
# 404: a 403 would confirm the row exists, which is precisely the probe the 404 refuses to
# answer.

project_router = APIRouter(prefix="/projects/{project_id}/connectors", tags=["connectors"])

# How many of the caller's projects one drill-down returns. NOTHING BOUNDS A CITIZEN'S PROJECT
# COUNT — `projects/router.py` pages its own listing at 25 and no per-user cap exists anywhere in
# this tree — so "a citizen's project list is short" is an assumption, not a fact. Same cap and
# same sentinel-row shape as the admin registry listing, and reported rather than hidden:
# `ConnectorProjectListResponse.truncated` says when it bit. Declared here rather than imported
# from the admin router, which is a superadmin surface a citizen route should not depend on.
LISTING_CAP = 200

# The three presets the `DateRange` popover draws, in the board's order. They are the OFFER, not
# the rule: `_offered_days` filters them against the connector's own retention before any of them
# reaches a row. See that function for why the filter is not decoration.
_BOARD_PRESET_DAYS: Final = (7, 14, 30)

_PROJECT_NOT_FOUND = "Project not found."
_NEEDS_APPROVAL = "You do not have access to {name} yet. Ask for it under Integrations."

# `resolve_window` answers `None` for exactly one input — a missing row — so a `None` beside a row
# that is demonstrably present means the resolver grew a second absent case. Raised rather than
# rendered as "off": a project whose switch is up would silently read nothing, with the rail
# drawing the state-b sentence over a connector the citizen had already switched on.
_RESOLVER_LOST_ITS_ROW = (
    "resolve_window answered None for a project_connectors row that is present — "
    "its only None case is an absent row"
)


def _offered_days(connector: Connector) -> tuple[int, ...]:
    """The `Last N days` presets THIS connector may be set to.

    DERIVED FROM `max_window_days`, NEVER HARD-CODED, and this is a correctness rule rather than
    generality for its own sake. `resolve_window` reports `clamped = false` for every relative
    window BY DEFINITION — a day count has no stored date pair to differ from — and it will not
    clamp one, because clamping a preset would make that flag lie. The premise holding that up is
    that every preset the write side offers is inside the connector's own retention. Offer
    `Last 30 days` on a connector that keeps seven and the premise breaks: the row resolves to a
    thirty-day span reaching three weeks past anything the connector holds, and says it was not
    clamped. So the filter is here, at the write, which is where the resolver's docstring says
    the choice belongs.

    A connector whose retention is shorter than the smallest preset offers exactly its retention
    — an empty offer would leave the citizen a switch and no range to switch it to."""
    within_retention = tuple(
        days for days in _BOARD_PRESET_DAYS if days <= connector.max_window_days
    )
    return within_retention or (connector.max_window_days,)


def _unsupported_window_message(connector: Connector) -> str:
    """The refusal a hand-crafted `days` gets, naming what it could have sent instead.

    The popover only ever offers `_offered_days`, so nobody meets this through the product — but
    a refusal that names the alternatives is what makes a 422 debuggable at the seam, and the
    list is built from the same function the offer is."""
    offered = [str(days) for days in _offered_days(connector)]
    listed = offered[0] if len(offered) == 1 else f"{', '.join(offered[:-1])} or {offered[-1]}"
    return f"Pick one of the ranges {connector.display_name} offers: {listed} days."


async def _owned_project_or_404(db: DbSession, project_id: uuid.UUID, user_id: uuid.UUID) -> None:
    """Prove the caller owns this project, or fail closed with the non-leaking 404.

    A PROOF, NOT A LOAD. Neither route wants a column off `projects`, and putting `user_id` in
    the WHERE clause — rather than loading the row and comparing afterwards — is what the
    single-tenant rule asks for: the predicate IS the isolation boundary, and a predicate cannot
    be forgotten between the load and the check."""
    owned = await db.scalar(
        sa.select(Project.id).where(Project.id == project_id, Project.user_id == user_id)
    )
    if owned is None:
        raise AppApiError(status.HTTP_404_NOT_FOUND, _PROJECT_NOT_FOUND, code="project_not_found")


def _resolved(
    connector: Connector,
    stored: ProjectConnector | None,
    owner_access_state: ConnectorRequestStatus | None,
) -> tuple[bool, ConnectorWindow | None]:
    """`(effectivelyOn, window)` for one project's row — the ONE place either is assembled.

    NO ROW IS NOT `enabled = false`. A project this connector was never switched on in has no
    window to render and nothing to read, so the pair is a literal `False` and `None`. That
    literal is deliberate and must stay literal: the moment it is written as
    `stored is not None and stored.enabled and approved` there are two homes for the on-ness
    conjunction — this line and `resolve_window` — and the two will drift. Every OTHER path here
    takes `effectively_on` off the resolver without restating it."""
    if stored is None:
        return False, None
    window = resolve_window(connector, stored, owner_access_state)
    if window is None:
        raise ValueError(_RESOLVER_LOST_ITS_ROW)
    return window.effectively_on, ConnectorWindow(
        kind=window.kind,
        start=window.start,
        end=window.end,
        days=window.days,
        clamped=window.clamped,
        earliest_date=window.earliest,
        latest_date=window.latest,
        # The pair as the citizen picked it, so a client can say what moved when `clamped` is
        # true. Read off the row BEFORE any commit — never across one.
        stored=StoredWindow(
            days=stored.window_days, start=stored.window_start, end=stored.window_end
        ),
    )


def _project_entry(
    connector_key: str,
    connector: Connector,
    access: PersonAccess,
    stored: ProjectConnector | None,
) -> ProjectConnectorEntry:
    """One DATA row: where the person stands, where this project's switch stands, and the days."""
    effectively_on, window = _resolved(connector, stored, access.request_status)
    asked = access.request if access.state is ConnectorPersonState.PENDING else None
    return ProjectConnectorEntry(
        key=connector_key,
        display_name=connector.display_name,
        data_noun=connector.data_noun,
        state=access.state,
        asked_at=None if asked is None else asked.created_at,
        enabled=stored is not None and stored.enabled,
        effectively_on=effectively_on,
        window=window,
    )


@project_router.get(
    "",
    responses=error_responses(AUTH_401, (404, ErrorEnvelope, "Project not found")),
)
async def list_project_connectors(
    project_id: uuid.UUID, user: CurrentUser, db: DbSession
) -> ProjectConnectorListResponse:
    """Every connector this project could read, what it reads today, and why it does not.

    One entry per catalogue connector, always. Each carries where YOU stand with that connector
    (`state`, and `askedAt` while an administrator has your request), this project's own switch
    (`enabled`), whether it actually reads (`effectivelyOn` — the switch AND your approval), and
    the days it reads (`window`).

    `window` is `null` for a connector this project has never switched on. When it is present it
    is RESOLVED for today: `start`, `end` and `days` are what the app can actually see, a preset
    is re-counted from today on every read, and a fixed range that has aged past what the
    connector keeps — or that points at the future — comes back moved, with `clamped` saying so
    and `stored` carrying the pair you picked. `earliestDate` and `latestDate` are the two ends
    the connector will serve today; a date picker greys against both and computes neither.

    An unknown project, and a project belonging to somebody else, are the same 404."""
    await _owned_project_or_404(db, project_id, user.id)

    # `populate_existing`: this router's upsert is an INSERT, so — unlike an ORM-enabled UPDATE
    # — it does not synchronise the session's identity map. A read that follows a write in the
    # SAME session must take the database's values, never a stale in-session copy of the row.
    # The drill-down read says the same thing for the same reason.
    rows = (
        await db.execute(
            sa.select(ProjectConnector)
            .join(Project, Project.id == ProjectConnector.project_id)
            .where(Project.user_id == user.id, ProjectConnector.project_id == project_id)
            .execution_options(populate_existing=True)
        )
    ).scalars()
    stored_by_key = {row.connector_key: row for row in rows}

    return ProjectConnectorListResponse(
        connectors=[
            _project_entry(
                key,
                connector,
                await current_access(db, user_id=user.id, connector_key=key),
                stored_by_key.get(key),
            )
            for key, connector in CONNECTORS.items()
        ]
    )


@router.get(
    "/{connector_key}/projects",
    responses=error_responses(AUTH_401, (404, ErrorEnvelope, "No such connector")),
)
async def list_connector_projects(
    connector_key: str, user: CurrentUser, db: DbSession
) -> ConnectorProjectListResponse:
    """Every project you own, with this connector's switch and days in each one.

    Newest project first. A project you have never switched this connector on in is present with
    `enabled: false` and a `null` window — the list is your projects, not your switches, because
    switching one on is the whole point of opening it.

    Capped at 200 projects. `truncated` is `true` when you own more than that, and the ones past
    the cap are reachable by narrowing the list rather than by paging.

    Having NO projects is a 200 and an empty list, never a 404 — an administrator can approve
    somebody before they have made anything, and the grant runs forward from there."""
    connector = _known_connector(connector_key)
    # ONE access read for the whole list. Access belongs to the PERSON, so the answer is the same
    # for every row — reading it per project would be N identical queries and N chances for two
    # rows in one response to disagree about whether their owner is approved.
    access = await current_access(db, user_id=user.id, connector_key=connector_key)

    rows = (
        await db.execute(
            sa.select(Project.id, Project.name, ProjectConnector)
            # The connector predicate belongs in the JOIN, not the WHERE: in the WHERE it would
            # turn this outer join back into an inner one and drop every project the citizen has
            # not switched this connector on in — which is most of them, and exactly the rows
            # the panel exists to offer a switch for.
            .outerjoin(
                ProjectConnector,
                sa.and_(
                    ProjectConnector.project_id == Project.id,
                    ProjectConnector.connector_key == connector_key,
                ),
            )
            .where(Project.user_id == user.id)
            # `id` is a UUIDv7, so this is newest-first — the same order and the same expression
            # the projects listing itself uses, so the panel does not reorder somebody's projects
            # depending on which screen they opened them from.
            .order_by(Project.id.desc())
            # One past the cap: the extra row is never projected, it only answers "is there
            # more?" without a second COUNT query.
            .limit(LISTING_CAP + 1)
            .execution_options(populate_existing=True)
        )
    ).all()
    truncated = len(rows) > LISTING_CAP

    return ConnectorProjectListResponse(
        projects=[
            ConnectorProjectEntry(
                project_id=project_id,
                name=name,
                enabled=stored is not None and stored.enabled,
                window=_resolved(connector, stored, access.request_status)[1],
            )
            for project_id, name, stored in rows[:LISTING_CAP]
        ],
        truncated=truncated,
    )


@project_router.put(
    "/{connector_key}",
    dependencies=[RequireCsrf],
    responses=error_responses(
        AUTH_401,
        (403, ErrorEnvelope, "CSRF check failed, or your access is not approved"),
        (404, ErrorEnvelope, "No such connector, or no such project"),
        (422, ErrorEnvelope, "The window is not one this connector offers"),
    ),
)
async def set_project_connector(
    project_id: uuid.UUID,
    connector_key: str,
    body: ProjectConnectorUpdate,
    user: CurrentUser,
    db: DbSession,
) -> ProjectConnectorEntry:
    """Switch a connector on or off for one project, and set the days it reads.

    `enabled` is required and `window` is optional. Omitting `window` KEEPS whatever this project
    already had — so switching off and back on returns the range you chose, not a default — and,
    on a project that has never had this connector switched on, sets the widest range the
    connector offers. Sending a window REPLACES the stored one: `{"kind": "relative", "days": 7}`
    for a preset, `{"kind": "absolute", "start": "2026-09-01", "end": "2026-09-30"}` for a fixed
    range whose first date is on or before its last.

    Refused with `403 access_not_approved` unless an administrator has approved YOUR access to
    this connector — waiting, declined and never-asked all refuse, and nothing is written.
    Refused with `404 project_not_found` for a project you do not own, and `404
    unknown_connector` for a connector that is not in the catalogue.

    A stored range is never bounds-checked on the way in and never rewritten afterwards: it is
    clamped on every READ instead, so it ages out on its own. Returns this project's connector in
    its new state, resolved exactly as the read returns it."""
    connector = _known_connector(connector_key)
    await _owned_project_or_404(db, project_id, user.id)

    # R12 IS ENFORCED HERE, NOT IN THE FORM. The switch is only drawn for an approved person, so
    # nobody meets this through the product — which is the whole reason it has to exist on the
    # server. Read through `current_access` rather than a status comparison of our own: the
    # person's state is a derivation over their remaining rows, and a second copy of that rule
    # would be correct only until somebody cancels and asks again.
    access = await current_access(db, user_id=user.id, connector_key=connector_key)
    if access.state is not ConnectorPersonState.APPROVED:
        raise AppApiError(
            status.HTTP_403_FORBIDDEN,
            _NEEDS_APPROVAL.format(name=connector.display_name),
            code="access_not_approved",
        )

    window = body.window
    if isinstance(window, RelativeWindowChoice) and window.days not in _offered_days(connector):
        raise AppApiError(
            status.HTTP_422_UNPROCESSABLE_CONTENT,
            _unsupported_window_message(connector),
            code="unsupported_window",
        )

    # The four window columns for the INSERT arm. `isinstance` rather than a `.kind` comparison
    # so every type checker in the gate narrows the union the same way.
    window_days: int | None = None
    window_start: date | None = None
    window_end: date | None = None
    if isinstance(window, AbsoluteWindowChoice):
        window_kind = ConnectorWindowKind.ABSOLUTE
        window_start, window_end = window.start, window.end
    else:
        window_kind = ConnectorWindowKind.RELATIVE
        # The widest range the connector offers — `Last 30 days` on the only one in the
        # catalogue. Derived, not written down, for the reason `_offered_days` gives: a default
        # outside a connector's retention would store a window that reports itself unclamped.
        window_days = window.days if window is not None else max(_offered_days(connector))

    insert = pg_insert(ProjectConnector).values(
        project_id=project_id,
        connector_key=connector_key,
        enabled=body.enabled,
        window_kind=window_kind,
        window_days=window_days,
        window_start=window_start,
        window_end=window_end,
    )
    # THE `DO UPDATE` BRANCHES; IT DOES NOT MERGE PER COLUMN. `COALESCE(EXCLUDED.x, x)` is the
    # obvious implementation and it is wrong: a `relative` window landing on a stored `absolute`
    # row would keep the old dates alongside the new day count, leaving all four columns
    # populated and violating `ck_project_connectors_window_shape`. Window supplied — replace all
    # four. Window omitted — the switch moves and the days stay exactly as they were, which is
    # what makes `enabled` a one-field call and what keeps the row on switch-off.
    #
    # `updated_at` is set explicitly: a column `onupdate` is a unit-of-work default and this is a
    # Core INSERT, so the two shipped upserts in this tree both state it and so does this one.
    window_columns = {
        "window_kind": insert.excluded.window_kind,
        "window_days": insert.excluded.window_days,
        "window_start": insert.excluded.window_start,
        "window_end": insert.excluded.window_end,
    }
    upsert = insert.on_conflict_do_update(
        # `project_id, connector_key` infers `uq_project_connectors_project_connector`, which is
        # also what serialises two switch presses racing from two tabs into one row.
        index_elements=[ProjectConnector.project_id, ProjectConnector.connector_key],
        set_={
            "enabled": insert.excluded.enabled,
            "updated_at": sa.func.now(),
            **({} if body.window is None else window_columns),
        },
    ).returning(
        ProjectConnector.enabled,
        ProjectConnector.window_kind,
        ProjectConnector.window_days,
        ProjectConnector.window_start,
        ProjectConnector.window_end,
    )
    written = (await db.execute(upsert)).one()

    # The row as the database now holds it, carried as a TRANSIENT `ProjectConnector` because
    # that is what the resolver reads. Never added to the session: it is a value object for one
    # call, and the persisted row is the one the statement above already wrote.
    stored = ProjectConnector(
        enabled=written.enabled,
        window_kind=written.window_kind,
        window_days=written.window_days,
        window_start=written.window_start,
        window_end=written.window_end,
    )
    # Assembled BEFORE the commit, so nothing reads an ORM attribute across one.
    entry = _project_entry(connector_key, connector, access, stored)
    await db.commit()
    return entry
