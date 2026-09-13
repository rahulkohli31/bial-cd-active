"""`GET /v1/connectors/{key}/projects` — the drill-down behind an approved connector row.

WHAT THIS FILE PINS. The list is the citizen's PROJECTS, not their switches: a project this
connector was never switched on in is present, off, with no window, because offering it a switch
is the whole point of opening the panel. Zero projects is a 200 and an empty list. The read is
capped at `LISTING_CAP` and SAYS so.

THE CAP IS NOT DECORATION. Nothing in this tree bounds a citizen's project count —
`projects/router.py` pages its own listing at 25 and no per-user cap exists — so the earlier
premise that "a citizen's project list is short" was an assumption. The cap test below is the
row that keeps the sentinel honest.

The resolved window's own arithmetic is proved once, against `resolve_window` directly, in
`tests/core/test_connector_window.py`; what is asserted here is that this route carries it —
including both calendar bounds, which is the contract the portal's date grid is written against.
"""

from __future__ import annotations

from datetime import date, timedelta

from src.api.v1.connectors.router import LISTING_CAP
from src.core.connectors import CONNECTORS
from src.db.models.connector_access import ConnectorRequestStatus
from src.db.models.project_connector import ConnectorWindowKind, ProjectConnector
from src.services.usage import ist_today
from tests.api.v1.connectors.conftest import (
    CONNECTORS_URL,
    KEY,
    UNKNOWN_KEY,
    auth_headers,
    seed_decision,
    seed_request,
)
from tests.factories import ProjectFactory, UserFactory

_URL = f"{CONNECTORS_URL}/{KEY}/projects"
_CONNECTOR = CONNECTORS[KEY]


async def _approved_citizen(db):
    user = await UserFactory.create(db)
    admin = await UserFactory.create(db, email="rahul.menon@rvaiglobal.com")
    await seed_decision(db, user.id, ConnectorRequestStatus.APPROVED, admin)
    return user


async def _switch_on(db, project_id, **window) -> ProjectConnector:
    """A `project_connectors` row seeded directly. The panel's own writes are the `PUT`'s
    business (`test_project_connectors.py`); this suite is about what comes back out."""
    row = ProjectConnector(
        project_id=project_id,
        connector_key=KEY,
        enabled=window.pop("enabled", True),
        window_kind=window.pop("kind", ConnectorWindowKind.RELATIVE),
        window_days=window.pop("days", 7),
        window_start=window.pop("start", None),
        window_end=window.pop("end", None),
    )
    db.add(row)
    await db.flush()
    return row


async def _projects(client, user, *, key: str = KEY) -> dict:
    resp = await client.get(f"{CONNECTORS_URL}/{key}/projects", headers=auth_headers(user))
    assert resp.status_code == 200, resp.text
    return resp.json()


# --- the list -------------------------------------------------------------------


async def test_every_project_is_listed_with_its_own_switch_and_days(client, db_session) -> None:
    """★ THE LIST IS THE PROJECTS, NOT THE SWITCHES. `DialogProjects` draws four rows and only
    two of them carry a chip — the other two are exactly the ones the citizen came here to
    switch on."""
    user = await _approved_citizen(db_session)
    oldest = await ProjectFactory.create(db_session, user.id, name="Visitor Log")
    middle = await ProjectFactory.create(db_session, user.id, name="Bay Occupancy — T1")
    newest = await ProjectFactory.create(db_session, user.id, name="Terminal 2 Departures")
    await _switch_on(db_session, middle.id, days=7)
    await _switch_on(
        db_session,
        newest.id,
        kind=ConnectorWindowKind.ABSOLUTE,
        days=None,
        start=date(2026, 9, 1),
        end=date(2026, 9, 3),
    )

    body = await _projects(client, user)

    # UUIDv7 primary keys sort by creation, so newest first — the same order and the same
    # expression the projects listing itself uses.
    assert [row["name"] for row in body["projects"]] == [
        "Terminal 2 Departures",
        "Bay Occupancy — T1",
        "Visitor Log",
    ]
    departures, bay, visitors = body["projects"]
    assert departures["projectId"] == str(newest.id)
    assert departures["enabled"] is True
    assert departures["window"]["kind"] == "absolute"
    assert bay["window"]["kind"] == "relative"
    assert bay["window"]["days"] == 7
    # No row at all: off, and nothing to render a chip from. An em dash, not an empty chip.
    assert visitors["projectId"] == str(oldest.id)
    assert visitors["enabled"] is False
    assert visitors["window"] is None
    assert body["truncated"] is False


async def test_a_project_switched_off_keeps_its_window_on_the_wire(client, db_session) -> None:
    """`enabled = false` means "switched off, and the days you picked are still here". The panel
    hides the chip; the popover still has to open on the range the citizen chose."""
    user = await _approved_citizen(db_session)
    project = await ProjectFactory.create(db_session, user.id)
    await _switch_on(db_session, project.id, enabled=False, days=14)

    row = (await _projects(client, user))["projects"][0]

    assert row["enabled"] is False
    assert row["window"]["days"] == 14


async def test_the_list_carries_both_calendar_bounds(client, db_session) -> None:
    """★ THE CONTRACT THE DATE GRID IS WRITTEN AGAINST. Both ends travel — the connector holds
    nothing before `earliestDate` and nothing after `latestDate` — so the popover greys dates
    against the server's calendar rather than the browser's, which is 5½ hours away."""
    user = await _approved_citizen(db_session)
    project = await ProjectFactory.create(db_session, user.id)
    await _switch_on(db_session, project.id, days=7)

    window = (await _projects(client, user))["projects"][0]["window"]

    ceiling = ist_today() - timedelta(days=_CONNECTOR.freshness_lag_days)
    # Not today, and asserted against today directly: the drill-down feeds the same calendar the
    # rail does, so a ceiling that drifted back to the reading day here would grey a day the
    # project view had already refused. See `test_project_connectors.py::_ceiling` for why the
    # arithmetic lives in the resolver's own unit table rather than being re-derived per surface.
    assert window["latestDate"] < ist_today().isoformat()
    assert window["latestDate"] == ceiling.isoformat()
    assert (
        window["earliestDate"]
        == (ceiling - timedelta(days=_CONNECTOR.max_window_days - 1)).isoformat()
    )


async def test_an_aged_out_range_reads_clamped_here_too(client, db_session) -> None:
    """The chip on this panel and the chip in the rail are the same component reading the same
    resolved fields, so the clamp has to reach both."""
    user = await _approved_citizen(db_session)
    project = await ProjectFactory.create(db_session, user.id)
    await _switch_on(
        db_session,
        project.id,
        kind=ConnectorWindowKind.ABSOLUTE,
        days=None,
        start=date(2026, 1, 5),
        end=date(2026, 1, 8),
    )

    window = (await _projects(client, user))["projects"][0]["window"]

    assert window["clamped"] is True
    assert (
        window["end"] == (ist_today() - timedelta(days=_CONNECTOR.freshness_lag_days)).isoformat()
    )
    assert window["stored"] == {"days": None, "start": "2026-01-05", "end": "2026-01-08"}


async def test_the_payload_is_camel_case_on_the_wire(client, db_session) -> None:
    user = await _approved_citizen(db_session)
    project = await ProjectFactory.create(db_session, user.id)
    await _switch_on(db_session, project.id)

    row = (await _projects(client, user))["projects"][0]

    assert "projectId" in row and "project_id" not in row
    assert "earliestDate" in row["window"] and "earliest_date" not in row["window"]


# --- the guaranteed states ------------------------------------------------------


async def test_a_citizen_with_no_projects_gets_an_empty_list_and_a_200(client, db_session) -> None:
    """★ A GUARANTEED STATE, NOT AN EDGE CASE. A grant runs forward, so an administrator can
    approve somebody before they have made anything. A 404 here would tell a newly-approved
    citizen their access was broken."""
    user = await _approved_citizen(db_session)

    body = await _projects(client, user)

    assert body == {"projects": [], "truncated": False}


async def test_more_projects_than_the_cap_returns_the_cap_and_says_so(client, db_session) -> None:
    """★ THE SENTINEL ROW. One past the cap answers "is there more?" without a second COUNT, and
    `truncated` is what the panel renders as a line pointing at the search — silent truncation is
    how two surfaces come to disagree with nothing on screen admitting it."""
    user = await _approved_citizen(db_session)
    db_session.add_all(
        ProjectFactory.build(user.id, name=f"Project {index}") for index in range(LISTING_CAP + 1)
    )
    await db_session.flush()

    body = await _projects(client, user)

    assert len(body["projects"]) == LISTING_CAP
    assert body["truncated"] is True


# --- isolation and refusals -----------------------------------------------------


async def test_one_citizens_projects_never_appear_in_anothers_list(client, db_session) -> None:
    """`project_connectors` carries NO `user_id` — `projects` is its ownership anchor — so this
    list reaches its scope through a join. Drop that predicate and every approved citizen is
    handed a switch for every project on the platform."""
    asha = await _approved_citizen(db_session)
    ravi = await UserFactory.create(db_session, email="ravi@rvaiglobal.com")
    admin = await UserFactory.create(db_session, email="admin@rvaiglobal.com")
    await seed_decision(db_session, ravi.id, ConnectorRequestStatus.APPROVED, admin)
    ravis = await ProjectFactory.create(db_session, ravi.id, name="Turnaround Times")
    await _switch_on(db_session, ravis.id)
    await ProjectFactory.create(db_session, asha.id, name="Visitor Log")

    asha_body = await _projects(client, asha)
    ravi_body = await _projects(client, ravi)

    assert [row["name"] for row in asha_body["projects"]] == ["Visitor Log"]
    assert [row["name"] for row in ravi_body["projects"]] == ["Turnaround Times"]
    assert "Turnaround Times" not in str(asha_body)


async def test_a_person_still_waiting_sees_their_projects_and_reads_nothing(
    client, db_session
) -> None:
    """The list is not gated on approval — it is the caller's own projects, and the refusal that
    matters is on the WRITE. What approval changes is what the window means: the same rows come
    back, and `resolve_window` is the only thing that knows they are not reading."""
    user = await UserFactory.create(db_session)
    await seed_request(db_session, user.id, ConnectorRequestStatus.PENDING)
    project = await ProjectFactory.create(db_session, user.id, name="Bay Occupancy — T1")
    await _switch_on(db_session, project.id, days=7)

    body = await _projects(client, user)

    assert [row["name"] for row in body["projects"]] == ["Bay Occupancy — T1"]
    assert body["projects"][0]["enabled"] is True
    assert body["projects"][0]["window"]["days"] == 7


async def test_an_unknown_connector_is_a_404(client, db_session) -> None:
    user = await _approved_citizen(db_session)

    resp = await client.get(f"{CONNECTORS_URL}/{UNKNOWN_KEY}/projects", headers=auth_headers(user))

    assert resp.status_code == 404
    assert resp.json()["error"]["code"] == "unknown_connector"


async def test_an_unauthenticated_read_is_refused(client) -> None:
    assert (await client.get(_URL)).status_code == 401
