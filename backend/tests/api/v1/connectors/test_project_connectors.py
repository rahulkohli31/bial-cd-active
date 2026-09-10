"""`GET /v1/projects/{id}/connectors` and `PUT .../{key}` — the rail's DATA section, and the one
write that sets both the switch and the days.

SIX CLAIMS THIS FILE EXISTS FOR:

1. **No row is not `enabled = false`.** A project this connector was never switched on in reads
   `effectivelyOn: false` and `window: null` — written as literals, never as a second spelling of
   `enabled AND approved`. That conjunction has exactly one home, `core.connectors.resolve_window`,
   and a second one would drift.
2. **The upsert BRANCHES.** Omitting `window` keeps the stored one; sending one replaces it
   outright. The per-column `COALESCE(EXCLUDED.x, x)` that suggests itself is refused by
   `ck_project_connectors_window_shape` the first time a preset lands on a stored date pair, and
   `test_enabled_only_leaves_an_absolute_window_whole` is the row that proves it.
3. **The row survives a switch-off.** Switching back on returns the range the citizen picked, not
   the default — `test_switching_off_keeps_the_window_and_on_returns_it` goes red if the row is
   deleted instead.
4. **Both bounds are on the wire.** `earliestDate` and `latestDate` come from the server because a
   browser in Bangalore and a server in UTC are 5½ hours apart; a portal written against a contract
   without them would compute its own calendar bounds.
5. **R12 is enforced at the server.** Waiting, declined and never-asked are all refused with a 403
   and no row, and the same person's rail read shows the state that explains why.
6. **Stored state is not effective state.** An approval flips an already-enabled row to effectively
   on with NOTHING written to `project_connectors` — the split is real, not a collapsed column.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta

import pytest
import sqlalchemy as sa
from redis.exceptions import RedisError

from src.api.v1.connectors import router
from src.core.connectors import CONNECTORS
from src.db.models.connector_access import ConnectorRequestStatus
from src.db.models.project_connector import ConnectorWindowKind, ProjectConnector
from src.services.build_sessions.locks import (
    acquire_lock,
    release_lock_as_holder,
    renew_liveness_lease,
    write_starting_marker,
)
from src.services.redis.keys import REGISTRY_STATE_READY, registry_key
from src.services.usage import ist_today
from tests.api.v1.connectors.conftest import (
    DECLINE_REMARKS,
    KEY,
    UNKNOWN_KEY,
    auth_headers,
    seed_decision,
    seed_request,
)
from tests.factories import ProjectFactory, UserFactory

_CONNECTOR = CONNECTORS[KEY]


def _ceiling() -> date:
    """The newest day this connector holds, right now — today minus its own freshness lag.

    Computed rather than written down because these tests run on whatever day they run on. The
    ARITHMETIC is pinned by literals in `tests/core/test_connector_window.py`; what this file
    proves is that whatever the resolver decided reaches the wire, and that it is never today —
    which is asserted against `ist_today()` directly, without going through this helper."""
    return ist_today() - timedelta(days=_CONNECTOR.freshness_lag_days)


def _rail(project_id: uuid.UUID) -> str:
    return f"/v1/projects/{project_id}/connectors"


def _switch(project_id: uuid.UUID, key: str = KEY) -> str:
    return f"/v1/projects/{project_id}/connectors/{key}"


async def _approved(db) -> tuple:
    """A citizen an administrator has already said yes to, and a project of theirs."""
    user = await UserFactory.create(db)
    admin = await UserFactory.create(db, email="rahul.menon@rvaiglobal.com")
    await seed_decision(db, user.id, ConnectorRequestStatus.APPROVED, admin)
    project = await ProjectFactory.create(db, user.id)
    return user, project


async def _put(client, user, project_id, body: dict, *, key: str = KEY, csrf: bool = True):
    return await client.put(
        _switch(project_id, key), headers=auth_headers(user, with_csrf=csrf), json=body
    )


async def _entry(client, user, project_id) -> dict:
    resp = await client.get(_rail(project_id), headers=auth_headers(user))
    assert resp.status_code == 200, resp.text
    entries = resp.json()["connectors"]
    assert len(entries) == len(CONNECTORS)
    return entries[0]


async def _stored_rows(db, project_id: uuid.UUID) -> list:
    """The row as the DATABASE holds it. Column selects, never entities: an `ON CONFLICT DO
    UPDATE` is an INSERT, so it does not synchronise the session's identity map, and an entity
    read here could hand back a stale in-session copy of exactly the row under test."""
    return list(
        (
            await db.execute(
                sa.select(
                    ProjectConnector.enabled,
                    ProjectConnector.window_kind,
                    ProjectConnector.window_days,
                    ProjectConnector.window_start,
                    ProjectConnector.window_end,
                ).where(ProjectConnector.project_id == project_id)
            )
        ).all()
    )


def _refusal(resp) -> tuple[str, str]:
    """The `{"error": {...}}` envelope's code and message — the shape the SPA branches on."""
    error = resp.json()["error"]
    return error["code"], error["message"]


def _field_errors(resp) -> list[tuple[str, tuple]]:
    """One `(type, loc)` per Pydantic complaint. Asserted on the TYPE, never on 422 alone: a
    missing field and a bad discriminator are different bugs at the client."""
    return [(item["type"], tuple(item["loc"])) for item in resp.json()["detail"]]


# --- switching on ---------------------------------------------------------------


async def test_switching_on_with_no_window_reads_the_widest_range_offered(
    client, db_session
) -> None:
    """★ The happy path, and the server-side default. The client sends one field; the days come
    back resolved to concrete dates so nothing in the browser has to know what `Last 30 days`
    means or when today is."""
    user, project = await _approved(db_session)

    resp = await _put(client, user, project.id, {"enabled": True})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["key"] == KEY
    assert body["displayName"] == _CONNECTOR.display_name
    assert body["state"] == "approved"
    assert body["enabled"] is True
    assert body["effectivelyOn"] is True

    window = body["window"]
    assert window["kind"] == "relative"
    assert window["days"] == _CONNECTOR.max_window_days
    # THE DEFAULT PRESET ENDS AT THE CONNECTOR'S CEILING, NOT TODAY. DICE extracts overnight, so
    # the newest day it holds is yesterday — and the preset path is where every citizen meets
    # that rule, because it is what switching on with no window gives them.
    assert window["end"] == _ceiling().isoformat()
    assert window["clamped"] is False
    # The default is the WIDEST range the connector offers, and it is stored as the preset the
    # citizen would have picked — not as a date pair frozen on the day they switched it on.
    assert window["stored"] == {"days": _CONNECTOR.max_window_days, "start": None, "end": None}


async def test_the_rail_read_returns_what_the_write_returned(client, db_session) -> None:
    """The write's body and the read's entry are assembled by the same function, so a client can
    render either without branching on which call it made."""
    user, project = await _approved(db_session)

    written = (await _put(client, user, project.id, {"enabled": True})).json()
    read = await _entry(client, user, project.id)

    assert read == written


async def test_a_project_with_no_row_is_off_with_no_window(client, db_session) -> None:
    """★ NO ROW IS NOT `enabled = false`, and `effectivelyOn` is a literal here.

    An approved citizen who has never switched this connector on in this project gets the board's
    state b — the switch down, nothing reading, and no window to render. If `effectivelyOn` were
    ever written as `enabled and approved` at this boundary there would be two homes for that
    conjunction; this row is the one that has no resolver behind it at all."""
    user, project = await _approved(db_session)

    entry = await _entry(client, user, project.id)

    assert entry["state"] == "approved"
    assert entry["enabled"] is False
    assert entry["effectivelyOn"] is False
    assert entry["window"] is None
    assert entry["askedAt"] is None
    assert await _stored_rows(db_session, project.id) == []


async def test_the_rail_lists_every_registry_connector(client, db_session) -> None:
    """One entry per catalogue entry, in registry order — the rail renders this array whole and
    counts it, so a connector missing from it is a row that silently disappears."""
    user, project = await _approved(db_session)

    resp = await client.get(_rail(project.id), headers=auth_headers(user))

    assert [entry["key"] for entry in resp.json()["connectors"]] == list(CONNECTORS)


async def test_the_payload_is_camel_case_on_the_wire(client, db_session) -> None:
    user, project = await _approved(db_session)
    await _put(client, user, project.id, {"enabled": True})

    entry = await _entry(client, user, project.id)

    assert "effectivelyOn" in entry and "effectively_on" not in entry
    assert "displayName" in entry and "display_name" not in entry
    assert "earliestDate" in entry["window"] and "earliest_date" not in entry["window"]


# --- the window survives the switch ---------------------------------------------


async def test_switching_off_keeps_the_window_and_on_returns_it(client, db_session) -> None:
    """★ MUTANT: delete the row on switch-off and this goes red.

    Approval is not spent by switching off, and neither is the range the citizen picked. The row
    stays with its window intact, `effectivelyOn` drops to false, and switching back on returns
    `Last 7 days` rather than silently re-picking the default."""
    user, project = await _approved(db_session)
    await _put(
        client, user, project.id, {"enabled": True, "window": {"kind": "relative", "days": 7}}
    )

    off = (await _put(client, user, project.id, {"enabled": False})).json()

    assert off["enabled"] is False
    assert off["effectivelyOn"] is False
    # The window is still ON THE WIRE while the switch is down — the rail hides the chip, but the
    # popover has to open on the range the citizen chose.
    assert off["window"]["days"] == 7
    assert await _stored_rows(db_session, project.id) == [
        (False, ConnectorWindowKind.RELATIVE, 7, None, None)
    ]

    back_on = (await _put(client, user, project.id, {"enabled": True})).json()

    assert back_on["enabled"] is True
    assert back_on["window"]["days"] == 7
    assert back_on["window"]["stored"]["days"] == 7


async def test_enabled_only_leaves_an_absolute_window_whole(client, db_session) -> None:
    """★ MUTANT: implement the upsert with per-column `COALESCE(EXCLUDED.x, x)` and the CHECK
    constraint rejects this write.

    An omitted window's INSERT arm still has to carry a `relative` default for the fresh-row case.
    Merged column by column onto a stored `absolute` row that default leaves `window_days`,
    `window_start` and `window_end` all populated at once —
    `ck_project_connectors_window_shape` refuses it, and the citizen's switch 500s. The `DO
    UPDATE` therefore branches: no window, no window columns."""
    user, project = await _approved(db_session)
    picked_start, picked_end = date(2026, 9, 1), date(2026, 9, 3)
    await _put(
        client,
        user,
        project.id,
        {
            "enabled": True,
            "window": {
                "kind": "absolute",
                "start": picked_start.isoformat(),
                "end": picked_end.isoformat(),
            },
        },
    )

    resp = await _put(client, user, project.id, {"enabled": False})

    assert resp.status_code == 200, resp.text
    assert await _stored_rows(db_session, project.id) == [
        (False, ConnectorWindowKind.ABSOLUTE, None, picked_start, picked_end)
    ]


async def test_sending_a_preset_replaces_a_stored_date_pair_outright(client, db_session) -> None:
    """The other half of the branch: a supplied window REPLACES, so a `relative` choice must not
    leave the old dates behind it — which is the same CHECK violation from the other direction."""
    user, project = await _approved(db_session)
    await _put(
        client,
        user,
        project.id,
        {
            "enabled": True,
            "window": {"kind": "absolute", "start": "2026-09-01", "end": "2026-09-03"},
        },
    )

    resp = await _put(
        client, user, project.id, {"enabled": True, "window": {"kind": "relative", "days": 14}}
    )

    assert resp.status_code == 200, resp.text
    assert await _stored_rows(db_session, project.id) == [
        (True, ConnectorWindowKind.RELATIVE, 14, None, None)
    ]


async def test_two_projects_of_one_owner_hold_their_own_windows(client, db_session) -> None:
    """The DAYS belong to the PROJECT — a departures board and a six-month trend want different
    history and the same person owns both. Writing one must not reach the other."""
    user, first = await _approved(db_session)
    second = await ProjectFactory.create(db_session, user.id)
    await _put(
        client, user, first.id, {"enabled": True, "window": {"kind": "relative", "days": 7}}
    )
    await _put(
        client,
        user,
        second.id,
        {
            "enabled": True,
            "window": {"kind": "absolute", "start": "2026-09-01", "end": "2026-09-03"},
        },
    )

    await _put(
        client, user, first.id, {"enabled": True, "window": {"kind": "relative", "days": 14}}
    )

    assert (await _entry(client, user, first.id))["window"]["stored"]["days"] == 14
    assert await _stored_rows(db_session, second.id) == [
        (True, ConnectorWindowKind.ABSOLUTE, None, date(2026, 9, 1), date(2026, 9, 3))
    ]


# --- the bounds, and the clamp --------------------------------------------------


async def test_the_read_carries_both_bounds(client, db_session) -> None:
    """★ THE FIELDS THE CALENDAR GREYS AGAINST. Without them the portal has to derive its own
    floor and ceiling from a browser clock 5½ hours away from the server's."""
    user, project = await _approved(db_session)
    await _put(client, user, project.id, {"enabled": True})

    window = (await _entry(client, user, project.id))["window"]

    # THE ONE ASSERTION THAT DOES NOT RECOMPUTE THE SERVER'S OWN EXPRESSION. Everything else in
    # this test mirrors `resolve_window`, so a matching off-by-one on both sides would stay
    # green; this compares the wire against the reading day itself. R3 is a claim about what
    # reaches the browser, and the browser is where the calendar greys its dates.
    assert window["latestDate"] < ist_today().isoformat(), (
        "the picker must never be offered today: the lake is a day behind"
    )
    assert window["latestDate"] == _ceiling().isoformat()
    assert (
        window["earliestDate"]
        == (_ceiling() - timedelta(days=_CONNECTOR.max_window_days - 1)).isoformat()
    )


async def test_an_aged_out_range_reads_clamped_and_the_row_is_not_rewritten(
    client, db_session
) -> None:
    """★ A window ages out on its own. The stored pair is what the citizen picked, forever; the
    bounds are applied on every READ, and `clamped` is how the chip says the dates moved. Read a
    second time, the row is still exactly as it was written — nothing is ever written back."""
    user, project = await _approved(db_session)
    long_ago_start, long_ago_end = date(2026, 1, 5), date(2026, 1, 8)
    await _put(
        client,
        user,
        project.id,
        {
            "enabled": True,
            "window": {
                "kind": "absolute",
                "start": long_ago_start.isoformat(),
                "end": long_ago_end.isoformat(),
            },
        },
    )

    window = (await _entry(client, user, project.id))["window"]

    assert window["clamped"] is True
    assert window["end"] == _ceiling().isoformat()
    # The pick slides forward at the SAME LENGTH rather than widening to the cap.
    assert window["days"] == (long_ago_end - long_ago_start).days + 1
    assert window["stored"] == {
        "days": None,
        "start": long_ago_start.isoformat(),
        "end": long_ago_end.isoformat(),
    }
    # A second read finds the row untouched: the clamp is a read-time rule, not a migration.
    assert (await _entry(client, user, project.id))["window"] == window
    assert await _stored_rows(db_session, project.id) == [
        (True, ConnectorWindowKind.ABSOLUTE, None, long_ago_start, long_ago_end)
    ]


# --- R12: the write refuses everybody an administrator has not approved ---------


@pytest.mark.parametrize(
    ("history", "person_state", "board_state"),
    [
        (None, "neverAsked", "c"),
        (ConnectorRequestStatus.PENDING, "pending", "d"),
        (ConnectorRequestStatus.DECLINED, "declined", "c"),
    ],
)
async def test_only_an_approved_person_may_switch_a_connector_on(
    client, db_session, history, person_state, board_state
) -> None:
    """★ R12, ENFORCED AT THE SERVER. The switch is only drawn for an approved person, which is
    exactly why the refusal cannot live in the form. Nothing is written, and the same person's
    rail read shows the state that explains it — `ConnectorStates` c (`You do not have access to
    … yet`) or d (`waiting on an administrator`)."""
    user = await UserFactory.create(db_session)
    if history is ConnectorRequestStatus.DECLINED:
        admin = await UserFactory.create(db_session, email="rahul.menon@rvaiglobal.com")
        await seed_decision(db_session, user.id, history, admin, decision_remarks=DECLINE_REMARKS)
    elif history is not None:
        await seed_request(db_session, user.id, history)
    project = await ProjectFactory.create(db_session, user.id)

    resp = await _put(client, user, project.id, {"enabled": True})

    assert resp.status_code == 403, resp.text
    code, message = _refusal(resp)
    assert code == "access_not_approved"
    assert _CONNECTOR.display_name in message
    assert await _stored_rows(db_session, project.id) == []

    entry = await _entry(client, user, project.id)
    assert entry["state"] == person_state
    assert entry["enabled"] is False
    assert entry["effectivelyOn"] is False
    assert entry["window"] is None
    # State d is the only one that draws a date, and it is the date they asked on.
    assert (entry["askedAt"] is not None) is (board_state == "d")


async def test_a_write_without_the_csrf_header_is_refused(client, db_session) -> None:
    """The shape a cross-site form post arrives in: the cookie rides along, the header does not."""
    user, project = await _approved(db_session)

    resp = await _put(client, user, project.id, {"enabled": True}, csrf=False)

    assert resp.status_code == 403
    assert await _stored_rows(db_session, project.id) == []


async def test_an_unauthenticated_read_is_refused(client) -> None:
    assert (await client.get(_rail(uuid.uuid4()))).status_code == 401


# --- the refusals ---------------------------------------------------------------


async def test_an_unknown_connector_is_a_404_and_writes_nothing(client, db_session) -> None:
    """The registry is the catalogue — there is no `connectors` table — so an unknown key is
    caught at the route and never by the database."""
    user, project = await _approved(db_session)

    resp = await _put(client, user, project.id, {"enabled": True}, key=UNKNOWN_KEY)

    assert resp.status_code == 404
    assert _refusal(resp)[0] == "unknown_connector"
    assert await _stored_rows(db_session, project.id) == []


async def test_a_range_the_connector_does_not_offer_is_refused(client, db_session) -> None:
    """`days` is checked against the set derived from the connector's own retention, not against
    a hard-coded `{7, 14, 30}` — and the refusal names what could have been sent instead."""
    user, project = await _approved(db_session)

    resp = await _put(
        client, user, project.id, {"enabled": True, "window": {"kind": "relative", "days": 5}}
    )

    assert resp.status_code == 422, resp.text
    code, message = _refusal(resp)
    assert code == "unsupported_window"
    assert "30" in message
    assert await _stored_rows(db_session, project.id) == []


async def test_a_backwards_date_pair_is_refused(client, db_session) -> None:
    """`window_start <= window_end` is THIS boundary's guarantee: the resolver takes it as given
    and never re-checks it, so an inverted pair must not get past here."""
    user, project = await _approved(db_session)

    resp = await _put(
        client,
        user,
        project.id,
        {
            "enabled": True,
            "window": {"kind": "absolute", "start": "2026-09-30", "end": "2026-09-01"},
        },
    )

    assert resp.status_code == 422, resp.text
    assert [type_ for type_, _loc in _field_errors(resp)] == ["value_error"]
    assert resp.json()["detail"][0]["msg"] == "The first date must be on or before the last."
    assert await _stored_rows(db_session, project.id) == []


@pytest.mark.parametrize(
    ("window", "expected_type", "expected_field"),
    [
        # An `absolute` kind carrying a day count: the dates it claims to be are simply absent.
        ({"kind": "absolute", "days": 7}, "missing", "start"),
        # A `relative` kind carrying dates: same failure from the other side.
        ({"kind": "relative", "start": "2026-09-01", "end": "2026-09-03"}, "missing", "days"),
        # A kind that is neither. The discriminator refuses the WINDOW as a whole rather than
        # reporting both arms' missing fields at once, which is what makes the message
        # actionable — `input_tag 'whenever' found using 'kind' does not match any of the
        # expected tags`.
        ({"kind": "whenever", "days": 7}, "union_tag_invalid", "window"),
    ],
)
async def test_a_window_whose_kind_and_fields_disagree_is_refused(
    client, db_session, window, expected_type, expected_field
) -> None:
    user, project = await _approved(db_session)

    resp = await _put(client, user, project.id, {"enabled": True, "window": window})

    assert resp.status_code == 422, resp.text
    types = [type_ for type_, _loc in _field_errors(resp)]
    fields = [loc[-1] for _type, loc in _field_errors(resp)]
    assert expected_type in types
    assert expected_field in fields
    assert await _stored_rows(db_session, project.id) == []


async def test_another_citizens_project_is_a_404_on_both_the_read_and_the_write(
    client, db_session
) -> None:
    """★ A project somebody else owns and a project that does not exist answer identically. A 403
    would confirm the row exists, which is precisely the probe the 404 refuses to answer."""
    asha, asha_project = await _approved(db_session)
    ravi = await UserFactory.create(db_session, email="ravi@rvaiglobal.com")
    admin = await UserFactory.create(db_session, email="admin@rvaiglobal.com")
    await seed_decision(db_session, ravi.id, ConnectorRequestStatus.APPROVED, admin)

    read = await client.get(_rail(asha_project.id), headers=auth_headers(ravi))
    write = await _put(client, ravi, asha_project.id, {"enabled": True})
    missing = await client.get(_rail(uuid.uuid4()), headers=auth_headers(ravi))

    assert read.status_code == write.status_code == missing.status_code == 404
    assert _refusal(read) == _refusal(write) == _refusal(missing)
    assert _refusal(read)[0] == "project_not_found"
    assert await _stored_rows(db_session, asha_project.id) == []


# --- stored state is not effective state ----------------------------------------


async def test_approving_a_person_switches_their_rows_on_without_writing_to_them(
    client, db_session
) -> None:
    """★ THE STORED/EFFECTIVE SPLIT, PROVED. A project whose switch is up while its owner waits
    reads `enabled: true` and `effectivelyOn: false`. The administrator's approval — a write to
    the person's ledger and to nothing else — flips it on. If `enabled` were the collapsed
    "is it on" column, granting access would have to walk every row the person owns.

    `ctid` is the proof, and it is the only honest one available: `now()` is the TRANSACTION's
    timestamp in PostgreSQL, so an `updated_at` comparison inside one test transaction cannot
    tell a rewritten row from an untouched one. An UPDATE always writes a new tuple version, so
    an unchanged `ctid` means the row was not written at all — not even with the same values."""
    user = await UserFactory.create(db_session)
    admin = await UserFactory.create(db_session, email="rahul.menon@rvaiglobal.com")
    await seed_request(db_session, user.id, ConnectorRequestStatus.PENDING)
    project = await ProjectFactory.create(db_session, user.id)
    db_session.add(
        ProjectConnector(
            project_id=project.id,
            connector_key=KEY,
            enabled=True,
            window_kind=ConnectorWindowKind.RELATIVE,
            window_days=7,
        )
    )
    await db_session.flush()

    async def _tuple_version() -> str:
        return str(
            await db_session.scalar(
                sa.text("SELECT ctid::text FROM project_connectors WHERE project_id = :p"),
                {"p": project.id},
            )
        )

    before_ctid = await _tuple_version()
    waiting = await _entry(client, user, project.id)
    # The approval, and nothing else: an append to the person's ledger (U5 writes this row).
    await seed_decision(db_session, user.id, ConnectorRequestStatus.APPROVED, admin)
    approved = await _entry(client, user, project.id)

    assert waiting["state"] == "pending"
    assert waiting["enabled"] is True
    assert waiting["effectivelyOn"] is False
    assert approved["state"] == "approved"
    assert approved["enabled"] is True
    assert approved["effectivelyOn"] is True
    assert approved["window"]["days"] == 7
    assert await _tuple_version() == before_ctid


# --- R11a: the settings are locked while a session is live -------------------------------------
#
# A CONTAINER RECEIVES ITS ENVIRONMENT EXACTLY ONCE, AT BIRTH. The attach arm — which is the
# steady state for every message after the first — forwards none, so a connector switched on
# mid-build would leave the rail saying "on" over a container that cannot reach anything. Rather
# than reconciling that state, it is prevented: the write is refused while a turn is in flight.
#
# ASSERTED AT THE API, not in the portal. A control that is only greyed out in the browser is not
# a guard — the route is reachable with a cookie and a CSRF token, which is exactly what a
# citizen's own second tab has.


async def test_the_switch_is_refused_while_a_build_or_chat_is_running(
    client, db_session, fake_redis
) -> None:
    """★ Refused server-side, with a message that names what the citizen can actually do."""
    user, project = await _approved(db_session)
    await acquire_lock(fake_redis, user.id)

    resp = await _put(client, user, project.id, {"enabled": True})

    assert resp.status_code == 409, resp.text
    body = resp.json()["error"]
    assert body["code"] == "session_is_live"
    assert _CONNECTOR.display_name in body["message"]
    # NOTHING WAS WRITTEN — a refusal that had already upserted the row would be worse than no
    # refusal at all, because the rail would then show the new setting over the old container.
    assert await _stored_rows(db_session, project.id) == []


async def test_the_window_is_refused_while_a_session_is_live_too(
    client, db_session, fake_redis
) -> None:
    """The switch and the days are locked together: both ride the same container environment,
    and a window changed mid-build is the same broken promise as a switch flipped mid-build."""
    user, project = await _approved(db_session)
    await _put(client, user, project.id, {"enabled": True})
    await acquire_lock(fake_redis, user.id)

    resp = await _put(
        client, user, project.id, {"enabled": True, "window": {"kind": "relative", "days": 7}}
    )

    assert resp.status_code == 409
    assert (await _entry(client, user, project.id))["window"]["stored"]["days"] == (
        _CONNECTOR.max_window_days
    ), "the stored window is exactly as it was before the refused write"


@pytest.mark.parametrize(
    "signal", ["lease", "starting"], ids=["liveness-lease", "start-in-flight"]
)
async def test_the_other_two_live_signals_refuse_as_well(
    client, db_session, fake_redis, signal: str
) -> None:
    """★ THREE SIGNALS, NOT ONE, and each covers a window the others do not.

    The lock is held for a turn. The LEASE is the one signal readable from another process and
    outlives a lock a crashed builder left standing. The STARTING marker covers the gap between
    "a start was asked for" and "the lock was taken" — which is precisely the window in which the
    container's environment is being assembled, and therefore the worst possible moment to accept
    a change."""
    user, project = await _approved(db_session)
    if signal == "lease":
        # A lease is only written against a container that exists — `renew_liveness_lease`
        # refuses otherwise, and logs that the build is unprotected. So the registry hash comes
        # first, which is also the order production creates them in.
        await fake_redis.hset(
            registry_key(user.id),
            mapping={"app_name": "sbx-abc", "state": REGISTRY_STATE_READY},
        )
        assert await renew_liveness_lease(fake_redis, user.id) is True
    else:
        await write_starting_marker(fake_redis, user.id, project.id)

    resp = await _put(client, user, project.id, {"enabled": True})

    assert resp.status_code == 409
    assert resp.json()["error"]["code"] == "session_is_live"


async def test_a_redis_that_answers_badly_refuses_the_change_rather_than_allowing_it(
    client, db_session, fake_redis, monkeypatch
) -> None:
    """★ THE FAIL-CLOSED ARM, WHICH IS A DIFFERENT ARM FROM "NO REDIS AT ALL". No Redis
    configured means no sandbox coordination, so there is no live session to protect and the write
    is allowed — that is the supported dev posture. A CONFIGURED Redis that raises is the opposite
    situation: the platform cannot tell whether a session is live, and a guess in that state is
    the half-configured container the whole R11a lock exists to prevent.

    Refusing costs nothing real, which is why this is the right posture rather than a cautious
    one: if Redis cannot answer, `acquire_lock` cannot take a lock either, so no build the refusal
    blocks could have started anyway.

    Turning the `raise` in that `except RedisError` into a `return` makes this test red and every
    other test in this file stay green — which is the only reason it is worth writing."""
    user, project = await _approved(db_session)

    async def _redis_that_is_having_a_bad_day(*args, **kwargs):
        raise RedisError("connection reset by peer")

    # Patched on the FIRST of the three liveness reads: the three are `or`-ed, so a failure in any
    # one of them has to refuse — short-circuiting past a broken check would be the bug.
    monkeypatch.setattr(router, "lock_is_held", _redis_that_is_having_a_bad_day)

    resp = await _put(client, user, project.id, {"enabled": True})

    assert resp.status_code == 409, resp.text
    assert resp.json()["error"]["code"] == "session_is_live"
    # And nothing was written, exactly as for a genuinely live session.
    assert await _stored_rows(db_session, project.id) == []


async def test_once_the_session_ends_the_switch_is_settable_again(
    client, db_session, fake_redis
) -> None:
    """The lock is a PAUSE, not a permanent refusal. The next container born carries the change,
    which is the whole point of preventing the half-configured state rather than reconciling it."""
    user, project = await _approved(db_session)
    token = await acquire_lock(fake_redis, user.id)
    assert token is not None
    assert (await _put(client, user, project.id, {"enabled": True})).status_code == 409

    await release_lock_as_holder(fake_redis, user.id, token)

    assert (await _put(client, user, project.id, {"enabled": True})).status_code == 200


async def test_an_unapproved_citizen_still_gets_the_403_not_the_lock_message(
    client, db_session, fake_redis
) -> None:
    """★ ORDERING. The approval check runs FIRST, so somebody who was never allowed to change
    this setting is told that — rather than being told to stop a build for a control they could
    not have used anyway."""
    user = await UserFactory.create(db_session, email="pending@rvaiglobal.com")
    project = await ProjectFactory.create(db_session, user_id=user.id)
    await seed_request(db_session, user.id, ConnectorRequestStatus.PENDING)
    await db_session.flush()
    await acquire_lock(fake_redis, user.id)

    resp = await _put(client, user, project.id, {"enabled": True})

    assert resp.status_code == 403
    assert resp.json()["error"]["code"] == "access_not_approved"


async def test_with_no_redis_at_all_there_is_no_session_to_protect(client, db_session) -> None:
    """No Redis means no sandbox coordination, which means no live session — a supported dev/test
    posture, and the answer is simply "not live". Binds no `fake_redis` fixture on purpose: with
    one bound this branch is unreachable by construction."""
    user, project = await _approved(db_session)

    assert (await _put(client, user, project.id, {"enabled": True})).status_code == 200
