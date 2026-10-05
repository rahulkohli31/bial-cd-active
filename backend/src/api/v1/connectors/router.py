"""What each project reads from the connector catalogue: the switch, and the days.

Hangs off `/v1/projects/{project_id}/connectors`. The project's switch alone decides whether it
reads a connector — nobody else is asked — and a change reaches the app the next time its
container starts.

THE OWNERSHIP CHECK A REVIEWER SHOULD BE ABLE TO MAKE BY READING TOP TO BOTTOM. There is no
cross-user read among these routes. `project_connectors` deliberately carries no `user_id` of its
own — `projects` is its ownership anchor — so every statement reaches it through a join on
`projects` whose `user_id` predicate sits in the same WHERE clause, and both routes prove
ownership with a `SELECT ... WHERE id = :id AND user_id = :me` before they read or write anything.
A project somebody else owns and a project that does not exist get the SAME 404: a 403 would
confirm the row exists, which is precisely the probe the 404 refuses to answer.

NO WRITE HERE IS AUDITED, and that is a decision rather than an omission. The citizen is acting
on their OWN project, and `project_connectors` is its own record of what was set — `updated_at`
dates it — so an audit entry would be a second copy of the row it describes.

Errors use the data-plane `{"error": {"message", "code"}}` envelope (`AppApiError`), not the auth
endpoints' `{"detail": ...}`; the SPA already branches on `error.code`.
"""

from __future__ import annotations

import uuid
from datetime import date
from typing import Final

import redis.asyncio as aioredis
import sqlalchemy as sa
from fastapi import APIRouter, status
from redis.exceptions import RedisError
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import SQLAlchemyError

from src.api.deps import CurrentUser, DbSession
from src.api.deps_csrf import RequireCsrf
from src.api.v1.connectors.schemas import (
    AbsoluteWindowChoice,
    ConnectorWindow,
    ProjectConnectorEntry,
    ProjectConnectorListResponse,
    ProjectConnectorUpdate,
    RelativeWindowChoice,
    StoredWindow,
)
from src.api.v1.live_build import the_live_session_is_this_app
from src.core.connectors import CONNECTORS, Connector, resolve_window
from src.core.errors import AppApiError
from src.db.models.project import Project
from src.db.models.project_connector import ConnectorWindowKind, ProjectConnector
from src.schemas import AUTH_401, ErrorEnvelope, error_responses
from src.services.build_sessions.locks import (
    liveness_lease_is_held,
    lock_is_held,
    read_registry,
    read_starting_marker,
)
from src.services.build_sessions.manager import existing_app_id
from src.services.redis import get_redis
from src.services.redis.client import RedisNotConfiguredError
from src.services.redis.keys import REGISTRY_FIELD_APP_NAME

router = APIRouter(prefix="/projects/{project_id}/connectors", tags=["connectors"])

_NO_SUCH_CONNECTOR = "That connector is not available."
_PROJECT_NOT_FOUND = "Application not found."

# A CONTAINER RECEIVES ITS ENVIRONMENT EXACTLY ONCE, AT BIRTH, so a connector switched on
# while a build or a conversation is running would leave the rail saying "on" over a container
# that cannot reach anything — and the attach arm, which is the steady state, forwards no
# environment at all. Rather than reconciling that state, the owner ruled it out of existence:
# the settings are locked while a session is live. No retrofit, no forced container swap, no
# half-configured state to test for.
#
# THE REFUSAL IS SERVER-SIDE BECAUSE A GREYED-OUT CONTROL IS NOT A GUARD. The message names what
# the citizen has to do, because they can actually do it — the workspace has a Stop control, and
# a turn ends on its own. It names the PROJECT as well: this refuses only for the project whose
# own session is live, and the switch is reachable from a list where nothing else on screen says
# which of a person's projects is the one to go and finish.
_SESSION_IS_LIVE = (
    "“{project}” has a chat or a build running. Finish or stop it, then change what {name} "
    "reads — an app that is already running keeps the settings it started with."
)

# The residual, stated once so nobody reads the lock as tighter than it is: this refuses a change
# while a TURN is live, which is what the platform can observe from another process. A container
# that is alive but idle between turns still keeps the settings it was born with, so a change made
# in that window reaches the app on its NEXT birth rather than immediately. That is the same
# birth-only rule every other injected credential follows, and the rail already tells the truth
# about what is stored; what the lock prevents is the state where a citizen watches a running
# build and is told it is reading data it demonstrably cannot reach.


def _known_connector(connector_key: str) -> Connector:
    """The registry is the catalogue — there is no `connectors` table, so an unknown key is
    caught HERE and never by the database. Membership first, then subscript: a `.get()` returning
    `None` would put the absent case and the present case on the same line."""
    if connector_key not in CONNECTORS:
        raise AppApiError(status.HTTP_404_NOT_FOUND, _NO_SUCH_CONNECTOR, code="unknown_connector")
    return CONNECTORS[connector_key]


# The three presets the `DateRange` popover draws, in the board's order. They are the OFFER, not
# the rule: `_offered_days` filters them against the connector's own retention before any of them
# reaches a row. See that function for why the filter is not decoration.
_BOARD_PRESET_DAYS: Final = (7, 14, 30)

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


async def _the_live_session_is_this_project(
    db: DbSession,
    redis: aioredis.Redis,
    user_id: uuid.UUID,
    project_id: uuid.UUID,
    starting_project_id: uuid.UUID | None,
) -> bool:
    """Is the session the signals above report the one running inside THIS project?

    THE THREE SIGNALS CARRY TWO KINDS OF IDENTITY. The starting marker holds a PROJECT id
    outright and is written under the lock, so while one stands it IS the live session's
    identity — and it is the only one that can answer during a cold start, before a registry
    entry exists. The lock and the lease carry none: theirs comes from the registry hash's app
    name, which `the_live_session_is_this_app` compares and fails closed on.

    A PROJECT NOTHING WAS EVER BUILT IN HAS NO APP ROW, and so no container of its own — a
    registry naming an app is naming somebody else's work. A registry naming NOTHING is
    ambiguity rather than innocence, and refuses here for the reason it refuses there."""
    if starting_project_id is not None:
        return starting_project_id == project_id
    # Read, never mint: `resolve_app_for_project` upserts, and a settings write that minted a
    # draft app would leave one behind for a project nobody has ever built in.
    app_id = await existing_app_id(db, user_id, project_id)
    if app_id is None:
        registry = await read_registry(redis, user_id)
        return not (registry or {}).get(REGISTRY_FIELD_APP_NAME, "")
    return await the_live_session_is_this_app(redis, user_id, app_id)


async def _refuse_while_a_session_is_live(
    db: DbSession,
    user_id: uuid.UUID,
    project_id: uuid.UUID,
    project_name: str,
    connector: Connector,
) -> None:
    """Refuse a settings change while this project has a turn in flight.

    THREE SIGNALS, ANY OF WHICH MEANS LIVE, because they cover the whole of a session's shape:
    the one-per-user LOCK is held for the duration of a turn; the liveness LEASE is the one signal
    readable from another process and outlives a lock a crashed builder left standing; and the
    STARTING marker covers the window between "a start was asked for" and "the lock was taken".
    Reading only the lock would let a change land during a cold start, which is precisely the
    window in which the container's environment is being assembled.

    ALL THREE ARE KEYED ON THE PERSON, because a citizen gets one workspace — so a build anywhere
    lights all three. Only the project that session is running in has an environment a change
    could contradict, so the refusal is narrowed to it and the citizen's other projects stay
    settable while it runs.

    FAILS CLOSED. A Redis error refuses the change rather than allowing it — the same posture
    `acquire_lock` takes, and the consistent one: if the platform cannot tell whether a session is
    live, it also cannot start one, so refusing here denies nothing that would otherwise work.

    NO REDIS AT ALL means no sandbox coordination, which means no live session to protect. That is
    a supported dev/test posture and the answer is simply "not live"."""
    try:
        redis = get_redis()
    except RedisNotConfiguredError:
        return
    try:
        starting_project_id = await read_starting_marker(redis, user_id)
        live_here = (
            await lock_is_held(redis, user_id)
            or await liveness_lease_is_held(redis, user_id)
            or starting_project_id is not None
        ) and await _the_live_session_is_this_project(
            db, redis, user_id, project_id, starting_project_id
        )
    except (RedisError, SQLAlchemyError) as exc:
        # BOTH stores, because the question now needs both: the three signals come from Redis
        # and the project's app id from Postgres. A database fault reaching the caller as a 500
        # would still block the write, but it would break the fail-closed CONTRACT this
        # docstring states and hand the citizen an error with nothing to do about it.
        raise AppApiError(
            status.HTTP_409_CONFLICT,
            _SESSION_IS_LIVE.format(project=project_name, name=connector.display_name),
            code="session_is_live",
        ) from exc
    if live_here:
        raise AppApiError(
            status.HTTP_409_CONFLICT,
            _SESSION_IS_LIVE.format(project=project_name, name=connector.display_name),
            code="session_is_live",
        )


async def _owned_project_or_404(db: DbSession, project_id: uuid.UUID, user_id: uuid.UUID) -> str:
    """Prove the caller owns this project and hand back its name, or fail closed with the
    non-leaking 404.

    ONE COLUMN, NOT THE ROW, and putting `user_id` in the WHERE clause — rather than loading the
    row and comparing afterwards — is what the single-tenant rule asks for: the predicate IS the
    isolation boundary, and a predicate cannot be forgotten between the load and the check. The
    name comes back because the live-session refusal says it; `projects.name` is NOT NULL, so an
    absent row is the only way to read `None` here."""
    name: str | None = await db.scalar(
        sa.select(Project.name).where(Project.id == project_id, Project.user_id == user_id)
    )
    if name is None:
        raise AppApiError(status.HTTP_404_NOT_FOUND, _PROJECT_NOT_FOUND, code="project_not_found")
    return name


def _resolved(
    connector: Connector, stored: ProjectConnector | None
) -> tuple[bool, ConnectorWindow | None]:
    """`(effectivelyOn, window)` for one project's row — the ONE place either is assembled.

    NO ROW IS NOT `enabled = false`. A project this connector was never switched on in has no
    window to render and nothing to read, so the pair is a literal `False` and `None`. Every
    OTHER path here takes `effectively_on` off the resolver without restating it."""
    if stored is None:
        return False, None
    window = resolve_window(connector, stored)
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
    connector_key: str, connector: Connector, stored: ProjectConnector | None
) -> ProjectConnectorEntry:
    """One settings row: where this project's switch stands, and the days."""
    effectively_on, window = _resolved(connector, stored)
    return ProjectConnectorEntry(
        key=connector_key,
        display_name=connector.display_name,
        data_noun=connector.data_noun,
        enabled=stored is not None and stored.enabled,
        effectively_on=effectively_on,
        window=window,
    )


@router.get(
    "",
    responses=error_responses(AUTH_401, (404, ErrorEnvelope, "Project not found")),
)
async def list_project_connectors(
    project_id: uuid.UUID, user: CurrentUser, db: DbSession
) -> ProjectConnectorListResponse:
    """Every connector this project could read, and what it reads today.

    One entry per catalogue connector, always. Each carries this project's own switch
    (`enabled`), whether it actually reads (`effectivelyOn`), and the days it reads (`window`).

    `window` is `null` for a connector this project has never switched on. When it is present it
    is RESOLVED for today: `start`, `end` and `days` are what the app can actually see, a preset
    is re-counted from today on every read, and a fixed range that has aged past what the
    connector keeps — or that points at the future — comes back moved, with `clamped` saying so
    and `stored` carrying the pair you picked. `earliestDate` and `latestDate` are the two ends
    the connector will serve today; a date picker greys against both and computes neither.

    An unknown project, and a project belonging to somebody else, are the same 404."""
    await _owned_project_or_404(db, project_id, user.id)

    rows = (
        await db.execute(
            sa.select(ProjectConnector)
            .join(Project, Project.id == ProjectConnector.project_id)
            .where(Project.user_id == user.id, ProjectConnector.project_id == project_id)
        )
    ).scalars()
    stored_by_key = {row.connector_key: row for row in rows}

    return ProjectConnectorListResponse(
        connectors=[
            _project_entry(key, connector, stored_by_key.get(key))
            for key, connector in CONNECTORS.items()
        ]
    )


@router.put(
    "/{connector_key}",
    dependencies=[RequireCsrf],
    responses=error_responses(
        AUTH_401,
        (403, ErrorEnvelope, "CSRF check failed"),
        (404, ErrorEnvelope, "No such connector, or no such project"),
        (409, ErrorEnvelope, "A chat or build is running, so the settings are locked"),
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

    A change reaches the app the next time its container starts. Refused with `404
    project_not_found` for a project you do not own, and `404 unknown_connector` for a connector
    that is not in the catalogue. Refused with `409 session_is_live` while you have a chat or a
    build running: a container is given its environment once, when it starts, so a change made
    mid-session would leave the settings promising data the running app cannot reach.

    A stored range is never bounds-checked on the way in and never rewritten afterwards: it is
    clamped on every READ instead, so it ages out on its own. Returns this project's connector in
    its new state, resolved exactly as the read returns it."""
    connector = _known_connector(connector_key)
    project_name = await _owned_project_or_404(db, project_id, user.id)
    await _refuse_while_a_session_is_live(db, user.id, project_id, project_name, connector)

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
    )
    # `populate_existing` because a Core upsert does not reach the session's cached copy of the
    # row: without it, a later read through this session hands back the switch as it was.
    written = (
        await db.execute(
            upsert.returning(ProjectConnector),
            execution_options={"populate_existing": True},
        )
    ).scalar_one()
    # Assembled BEFORE the commit, so nothing reads an ORM attribute across one.
    entry = _project_entry(connector_key, connector, written)
    await db.commit()
    return entry
