"""`GET /v1/connectors` — the Integrations dialog's whole payload.

WHAT THIS FILE PINS. One entry per REGISTRY connector rather than per grant (a connector you
have never asked about is present, in `neverAsked`), the four person states with exactly the
fields each one needs, and the two isolation claims: another citizen's state never leaks into
your list, and their stated reason never appears in your response.

The state rule itself is proved against `current_access` directly in
`tests/services/connectors/test_access_state.py`; the cancelled-history case is repeated HERE
through the route because the route is what a citizen actually meets.
"""

from __future__ import annotations

from datetime import datetime

from src.core.connectors import CONNECTORS
from src.db.models.connector_access import ConnectorRequestStatus
from src.db.models.project_connector import ConnectorWindowKind, ProjectConnector
from tests.api.v1.connectors.conftest import (
    CONNECTORS_URL,
    DECLINE_REMARKS,
    KEY,
    REMARKS,
    auth_headers,
    seed_decision,
    seed_request,
)
from tests.factories import ProjectFactory, UserFactory


async def _entries(client, user) -> list[dict]:
    resp = await client.get(CONNECTORS_URL, headers=auth_headers(user))
    assert resp.status_code == 200, resp.text
    return resp.json()["connectors"]


async def _only(client, user) -> dict:
    entries = await _entries(client, user)
    assert len(entries) == len(CONNECTORS)
    return entries[0]


async def _switch_on(db, user_id, *, enabled: bool = True) -> None:
    project = await ProjectFactory.create(db, user_id)
    db.add(
        ProjectConnector(
            project_id=project.id,
            connector_key=KEY,
            enabled=enabled,
            window_kind=ConnectorWindowKind.RELATIVE,
            window_days=30,
        )
    )
    await db.flush()


# --- the list itself ------------------------------------------------------------


async def test_the_list_is_the_registry_not_the_grants(client, db_session) -> None:
    """A citizen who has never asked about anything still sees every connector.

    This list IS what Integrations offers — `Data BIAL already holds` — so an empty answer for
    a new joiner would leave the dialog with nothing to ask about."""
    user = await UserFactory.create(db_session)

    entries = await _entries(client, user)

    assert [entry["key"] for entry in entries] == list(CONNECTORS)
    entry = entries[0]
    assert entry["displayName"] == CONNECTORS[KEY].display_name
    assert entry["subtitle"] == CONNECTORS[KEY].subtitle
    assert entry["state"] == "neverAsked"


async def test_the_ask_panel_copy_travels_instead_of_being_reconstructed(
    client, db_session
) -> None:
    """★ R18. The two sentences `AskAccess` cannot derive from a name ride the wire: what the
    system holds, and what an approval gives you.

    THIS IS THE ASSERTION THAT KEEPS "ADD A SECOND CONNECTOR = A REGISTRY ENTRY" TRUE. Both
    values are compared against the registry rather than against literals — the byte-exactness
    against the board is pinned once, in `tests/db/test_connector_models.py` — so this test is
    about the PLUMBING and stays right when the copy is revised. Delete the plumbing and the
    browser has to carry one connector's dataset facts in a component, which is precisely the
    change R18 forbids.

    The lead and the body arrive SEPARATE. The panel bolds the lead against the body's grey, and
    the approver's `Read access to the Flight Fact Report.` proves a joined string cannot be
    split back reliably (its body starts lowercase, mid-sentence)."""
    user = await UserFactory.create(db_session)
    connector = CONNECTORS[KEY]

    entry = await _only(client, user)

    assert entry["askSubtitle"] == connector.ask_subtitle
    # And it is not the row's label wearing a different name.
    assert entry["askSubtitle"] != entry["subtitle"]
    assert entry["consentLinesRequester"] == [
        {"lead": line.lead, "body": line.body} for line in connector.consent_lines_requester
    ]
    assert len(entry["consentLinesRequester"]) == 3


async def test_the_ask_panel_copy_is_the_same_in_every_state(client, db_session) -> None:
    """MUTANT: narrow either field to a state and this goes red.

    These are REGISTRY facts, not per-caller facts, and the rest of `ConnectorEntry` is the
    opposite — `approvedByName` on a declined row would be a second answer to "who decided". A
    reader following that pattern one field too far would null the copy in exactly the state the
    ask panel is reachable from, and the citizen would meet an empty consent box."""
    connector = CONNECTORS[KEY]
    admin = await UserFactory.create(
        db_session, email="rahul.menon@rvaiglobal.com", display_name="Rahul Menon"
    )
    never_asked = await UserFactory.create(db_session, email="new.joiner@rvaiglobal.com")
    pending = await UserFactory.create(db_session, email="waiting@rvaiglobal.com")
    await seed_request(db_session, pending.id, ConnectorRequestStatus.PENDING)
    approved = await UserFactory.create(db_session, email="granted@rvaiglobal.com")
    await seed_decision(db_session, approved.id, ConnectorRequestStatus.APPROVED, admin)
    declined = await UserFactory.create(db_session, email="refused@rvaiglobal.com")
    await seed_decision(
        db_session,
        declined.id,
        ConnectorRequestStatus.DECLINED,
        admin,
        decision_remarks=DECLINE_REMARKS,
    )

    entries = [await _only(client, user) for user in (never_asked, pending, approved, declined)]

    # Liveness: the four really are four different states, so "the copy is identical" is a claim
    # about four answers rather than four repeats of the same one.
    assert [entry["state"] for entry in entries] == [
        "neverAsked",
        "pending",
        "approved",
        "declined",
    ]
    expected = (
        connector.ask_subtitle,
        [{"lead": line.lead, "body": line.body} for line in connector.consent_lines_requester],
    )
    assert [(entry["askSubtitle"], entry["consentLinesRequester"]) for entry in entries] == [
        expected
    ] * 4


async def test_the_payload_is_camel_case_on_the_wire(client, db_session) -> None:
    """`CamelModel`, asserted once so a future field cannot arrive snake_case unnoticed."""
    user = await UserFactory.create(db_session)

    entry = await _only(client, user)

    assert "displayName" in entry
    assert "display_name" not in entry


async def test_an_unauthenticated_read_is_refused(client) -> None:
    assert (await client.get(CONNECTORS_URL)).status_code == 401


# --- the four states ------------------------------------------------------------


async def test_never_asked_carries_no_dates_and_no_count(client, db_session) -> None:
    """Every state-specific field is null, `onProjectCount` included. `0` would be a claim
    about projects this person cannot switch anything on in."""
    user = await UserFactory.create(db_session)

    entry = await _only(client, user)

    assert entry["state"] == "neverAsked"
    assert entry["askedAt"] is None
    assert entry["approvedAt"] is None
    assert entry["approvedByName"] is None
    assert entry["onProjectCount"] is None
    assert entry["decidedAt"] is None
    assert entry["decidedByName"] is None
    assert entry["decisionRemarks"] is None


async def test_pending_carries_the_asked_timestamp_and_nothing_decided(client, db_session) -> None:
    user = await UserFactory.create(db_session)
    row = await seed_request(db_session, user.id, ConnectorRequestStatus.PENDING)

    entry = await _only(client, user)

    assert entry["state"] == "pending"
    assert entry["askedAt"] is not None
    assert datetime.fromisoformat(entry["askedAt"]) == row.created_at
    assert entry["decidedAt"] is None
    assert entry["approvedAt"] is None


async def test_approved_names_the_administrator_and_the_date(client, db_session) -> None:
    """`Approved for you 2 Sep · Rahul Menon` — both halves of the board's sentence."""
    user = await UserFactory.create(db_session)
    admin = await UserFactory.create(
        db_session, email="rahul.menon@rvaiglobal.com", display_name="Rahul Menon"
    )
    row = await seed_decision(db_session, user.id, ConnectorRequestStatus.APPROVED, admin)

    entry = await _only(client, user)

    assert entry["state"] == "approved"
    assert entry["approvedByName"] == "Rahul Menon"
    assert row.decided_at is not None
    assert datetime.fromisoformat(entry["approvedAt"]) == row.decided_at
    # The decline half of the payload stays empty: one decision, one sentence.
    assert entry["decidedAt"] is None
    assert entry["decisionRemarks"] is None


async def test_an_approver_with_no_display_name_is_named_by_their_email(
    client, db_session
) -> None:
    """★ `users.display_name` IS NULLABLE. Without the fallback the citizen reads
    `Approved for you 2 Sep · ` and is told nobody granted their access — a plain address is
    strictly better than a dangling separator."""
    user = await UserFactory.create(db_session)
    admin = await UserFactory.create(
        db_session, email="ops.admin@rvaiglobal.com", display_name=None
    )
    await seed_decision(db_session, user.id, ConnectorRequestStatus.APPROVED, admin)

    entry = await _only(client, user)

    assert entry["approvedByName"] == "ops.admin@rvaiglobal.com"


async def test_declined_carries_the_administrators_remark_verbatim(client, db_session) -> None:
    """The whole of what a refused person is owed, since `Ask again` is not built (D10)."""
    user = await UserFactory.create(db_session)
    admin = await UserFactory.create(db_session, display_name="Rahul Menon")
    row = await seed_decision(
        db_session,
        user.id,
        ConnectorRequestStatus.DECLINED,
        admin,
        decision_remarks=DECLINE_REMARKS,
    )

    entry = await _only(client, user)

    assert entry["state"] == "declined"
    assert entry["decisionRemarks"] == DECLINE_REMARKS
    assert entry["decidedByName"] == "Rahul Menon"
    assert row.decided_at is not None
    assert datetime.fromisoformat(entry["decidedAt"]) == row.decided_at
    # The approval half stays empty — a client reading `approvedAt` must not find a decline in it.
    assert entry["approvedAt"] is None
    assert entry["approvedByName"] is None


async def test_a_history_of_nothing_but_cancels_reads_never_asked(client, db_session) -> None:
    """★ THE RULE, THROUGH THE ROUTE. Under "latest row wins" this citizen's dialog would show
    a state that does not exist, forever, with no way back to `Request access`."""
    user = await UserFactory.create(db_session)
    await seed_request(db_session, user.id, ConnectorRequestStatus.CANCELLED)
    await seed_request(db_session, user.id, ConnectorRequestStatus.CANCELLED)

    entry = await _only(client, user)

    assert entry["state"] == "neverAsked"
    assert entry["askedAt"] is None


# --- `On in N projects` ---------------------------------------------------------


async def test_the_project_count_counts_only_switched_on_projects(client, db_session) -> None:
    user = await UserFactory.create(db_session)
    admin = await UserFactory.create(db_session, display_name="Rahul Menon")
    await seed_decision(db_session, user.id, ConnectorRequestStatus.APPROVED, admin)
    await _switch_on(db_session, user.id)
    await _switch_on(db_session, user.id)
    # A project that has the connector's row but the switch DOWN: `enabled = false` means
    # "switched off, and the days you picked are still here", not "on".
    await _switch_on(db_session, user.id, enabled=False)
    # And a project with no connector row at all.
    await ProjectFactory.create(db_session, user.id)

    entry = await _only(client, user)

    assert entry["onProjectCount"] == 2


async def test_the_project_count_is_zero_not_null_for_an_approved_person(
    client, db_session
) -> None:
    """`On in 0 projects` is a true sentence about somebody who has access and has not used it
    yet; `null` there would make the portal guess."""
    user = await UserFactory.create(db_session)
    admin = await UserFactory.create(db_session, display_name="Rahul Menon")
    await seed_decision(db_session, user.id, ConnectorRequestStatus.APPROVED, admin)

    entry = await _only(client, user)

    assert entry["onProjectCount"] == 0


async def test_the_project_count_never_counts_somebody_elses_projects(client, db_session) -> None:
    """`project_connectors` carries NO `user_id` — `projects` is its ownership anchor — so this
    count reaches its scope through a join. Drop that predicate and every approved citizen is
    told the connector is on in every project on the platform."""
    asha = await UserFactory.create(db_session, email="asha@rvaiglobal.com")
    ravi = await UserFactory.create(db_session, email="ravi@rvaiglobal.com")
    admin = await UserFactory.create(db_session, display_name="Rahul Menon")
    await seed_decision(db_session, asha.id, ConnectorRequestStatus.APPROVED, admin)
    await seed_decision(db_session, ravi.id, ConnectorRequestStatus.APPROVED, admin)
    await _switch_on(db_session, ravi.id)
    await _switch_on(db_session, ravi.id)

    assert (await _only(client, asha))["onProjectCount"] == 0
    assert (await _only(client, ravi))["onProjectCount"] == 2


# --- isolation ------------------------------------------------------------------


async def test_one_citizens_state_never_appears_in_anothers_list(client, db_session) -> None:
    """Two people, every state on one of them, nothing on the other — and the reason one of
    them wrote must not travel: `requester_remarks` is not on this payload at all, and the
    administrator's words belong only to the person they were written about."""
    asha = await UserFactory.create(db_session, email="asha@rvaiglobal.com")
    ravi = await UserFactory.create(db_session, email="ravi@rvaiglobal.com")
    admin = await UserFactory.create(db_session, display_name="Rahul Menon")
    await seed_decision(
        db_session,
        asha.id,
        ConnectorRequestStatus.DECLINED,
        admin,
        decision_remarks=DECLINE_REMARKS,
        requester_remarks=REMARKS,
    )

    asha_entry = await _only(client, asha)
    ravi_resp = await client.get(CONNECTORS_URL, headers=auth_headers(ravi))

    assert asha_entry["state"] == "declined"
    assert ravi_resp.json()["connectors"][0]["state"] == "neverAsked"
    assert DECLINE_REMARKS not in ravi_resp.text
    assert REMARKS not in ravi_resp.text
    # The citizen's own stated reason is not on this surface either — it is written for an
    # administrator, and the admin queue (U5) is where it is read.
    assert REMARKS not in (await client.get(CONNECTORS_URL, headers=auth_headers(asha))).text
