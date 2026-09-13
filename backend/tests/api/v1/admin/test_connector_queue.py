"""`GET /v1/admin/connector-requests` and `.../counts` — the administrator's two tables.

SEVEN CLAIMS THIS FILE EXISTS FOR:

1. **The two tables are two orders, and both are named rather than incidental.** `waiting` is
   oldest-first (the review-queue order the app registry already uses, so the person who has
   waited longest is on top) and `decided` is newest-decision-first. Both are seeded so that the
   correct answer DISAGREES with insertion order — an assertion that passes on whatever order
   PostgreSQL happens to return proves nothing.
2. **A cancelled request is on NEITHER table.** It is not waiting on anybody and nobody decided
   it; the row survives in the ledger as history and belongs on no administrator's screen.
3. **`usingItIn` is one person's count.** This is the one connector surface that reads across
   users, so the grouping key IS the isolation: a second citizen's enabled projects must not
   reach this row, and the test seeds a second citizen for exactly that reason.
4. **Nullable names never reach the wire as holes.** `users.display_name` is nullable and the
   SERVER substitutes the work email — one fallback, not one per panel. The decider's name is
   `null` only when their account is gone, which is a different fact and is asserted as one.
5. **A bad filter is refused, never ignored.** An unknown `state` or `connector` narrows nothing
   silently; a queue that reads as empty because a filter was dropped is the worst outcome here.
6. **An empty queue is a 200 and a zero, not a 404.** "Nothing is waiting" and "we did not ask"
   must not render as the same pixel.
7. **The decide dialog's consent copy rides the row.** The administrator's third-person
   `WHAT APPROVING GIVES THEM` lines are registry facts on the wire, not sentences a component
   reconstructs — R18's claim is that a second connector costs a registry entry and nothing
   else, and the citizen's half of this wire has already been repaired once for exactly that.

The ledger seeds come from the citizen suite's conftest ON PURPOSE: both surfaces read one table,
and a second `seed_request` here would be a second place the row shape is written down.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
import sqlalchemy as sa

from src.api.v1.admin.connectors import LISTING_CAP
from src.core.connectors import CONNECTORS
from src.db.models.audit import AuditLog
from src.db.models.connector_access import ConnectorAccessRequest, ConnectorRequestStatus
from src.db.models.project_connector import ConnectorWindowKind, ProjectConnector
from src.db.models.user import User
from tests.api.v1.connectors.conftest import (
    DECLINE_REMARKS,
    KEY,
    REMARKS,
    auth_headers,
    seed_decision,
    seed_request,
)
from tests.factories import ProjectFactory, UserFactory

QUEUE = "/v1/admin/connector-requests"
COUNTS = f"{QUEUE}/counts"

_CONNECTOR = CONNECTORS[KEY]
_NOW = datetime.now(UTC)


async def _admin(db, **overrides: Any) -> User:
    """A super-admin. `admin@bial.com` is on `.env.test`'s `SUPERADMIN_EMAILS`; the role is
    COMPUTED from that allowlist, so the email is the whole of what makes this user one."""
    return await UserFactory.create(db, email="admin@bial.com", **overrides)


async def _citizen(db, **overrides: Any) -> User:
    return await UserFactory.create(db, email="nobody@rvaiglobal.com", **overrides)


async def _waiting(client, admin: User, **params: str) -> list[dict[str, Any]]:
    resp = await client.get(
        QUEUE, headers=auth_headers(admin), params={"state": "waiting", **params}
    )
    assert resp.status_code == 200, resp.text
    return list(resp.json()["requests"])


async def _decided(client, admin: User, **params: str) -> list[dict[str, Any]]:
    resp = await client.get(
        QUEUE, headers=auth_headers(admin), params={"state": "decided", **params}
    )
    assert resp.status_code == 200, resp.text
    return list(resp.json()["requests"])


async def _switch_on(db, project_id: uuid.UUID, *, enabled: bool = True) -> None:
    """One `project_connectors` row with its switch in the given position, on the widest window
    the connector offers. The days do not matter here; only `enabled` is counted."""
    db.add(
        ProjectConnector(
            project_id=project_id,
            connector_key=KEY,
            enabled=enabled,
            window_kind=ConnectorWindowKind.RELATIVE,
            window_days=_CONNECTOR.max_window_days,
        )
    )
    await db.flush()


# --- the waiting table ----------------------------------------------------------


async def test_the_waiting_table_is_oldest_first_and_carries_the_whole_row(
    client, db_session
) -> None:
    """★ THE HAPPY PATH. Three people waiting, the longest wait on top, each row carrying
    everything `AdminQueue`'s `WAITING ON YOU` draws: who, their work email, the connector's
    display name, their remarks IN FULL, and when they asked.

    SEEDED SO THE RIGHT ANSWER DISAGREES WITH INSERTION ORDER — the rows go in newest, oldest,
    middle. Drop the `order_by` and this goes red rather than passing on whatever PostgreSQL
    returns.
    """
    admin = await _admin(db_session)
    people = {}
    for name, email, age_hours in (
        ("Priya Nair", "priya.nair@bial.com", 1),
        ("Sam Fernandes", "sam.fernandes@bial.com", 3),
        ("Divya Shetty", "divya.shetty@bial.com", 2),
    ):
        person = await UserFactory.create(db_session, display_name=name, email=email)
        people[name] = person
        await seed_request(
            db_session,
            person.id,
            ConnectorRequestStatus.PENDING,
            created_at=_NOW - timedelta(hours=age_hours),
        )

    rows = await _waiting(client, admin)

    assert [row["displayName"] for row in rows] == ["Sam Fernandes", "Divya Shetty", "Priya Nair"]
    first = rows[0]
    assert first["userId"] == str(people["Sam Fernandes"].id)
    # The work email, in place of the board's `department` — that field exists nowhere in this
    # product and there is no directory client behind one.
    assert first["email"] == "sam.fernandes@bial.com"
    assert first["connectorKey"] == KEY
    assert first["connectorDisplayName"] == _CONNECTOR.display_name
    # IN FULL, never truncated to a tooltip: this sentence is the whole of what the
    # administrator decides on.
    assert first["requesterRemarks"] == REMARKS
    assert first["askedAt"] is not None
    assert first["status"] == "pending"
    # Nobody has decided, and the row says so rather than defaulting to the reader.
    assert first["decidedAt"] is None
    assert first["decidedById"] is None
    assert first["decidedByName"] is None
    assert first["decisionRemarks"] is None
    # `null`, not `0`: "we did not count" and "none" are different answers, and only one of them
    # belongs on a row nobody has approved.
    assert first["usingItIn"] is None


async def test_every_row_carries_the_approver_consent_lines_as_objects(client, db_session) -> None:
    """★ THE DECIDE DIALOG'S COPY RIDES THE ROW (R18). `AdminReview`'s `WHAT APPROVING GIVES THEM`
    panel is three consent sentences about ONE connector, and a component that spelled them would
    make "add a second connector" a component change — the exact defect `1935588e` came back to
    repair on the citizen's side of this wire.

    THREE THINGS ARE ASSERTED, AND EACH HAS ITS OWN MUTANT:
    the lines are the registry's APPROVER tuple (hand over the requester's and the third-person
    panel ships second-person copy, and the thirty-day promise disappears); they cross as
    `{lead, body}` objects rather than pre-joined sentences (join them and the browser has to
    guess the bold split at the first full stop, which the first line's lowercase body breaks);
    and they are on a DECIDED row too, not narrowed to `waiting` (narrow them and the field is
    state-conditional copy, which is what `ConnectorEntry`'s docblock argues against).
    """
    admin = await _admin(db_session)
    citizen = await _citizen(db_session)
    await seed_request(db_session, citizen.id, ConnectorRequestStatus.PENDING)
    other = await UserFactory.create(db_session, email="decided@rvaiglobal.com")
    await seed_decision(db_session, other.id, ConnectorRequestStatus.APPROVED, admin)

    expected = [
        {"lead": line.lead, "body": line.body} for line in _CONNECTOR.consent_lines_approver
    ]
    # Guard the guard: an empty registry tuple would make every assertion below vacuously true.
    assert len(expected) == 3

    waiting_row = (await _waiting(client, admin))[0]
    assert waiting_row["consentLinesApprover"] == expected
    # THE APPROVER'S SET, NOT THE CITIZEN'S. The two tuples are different sentences in different
    # voices; only this one names the day cap, and only the citizen's says "you".
    assert waiting_row["consentLinesApprover"] != [
        {"lead": line.lead, "body": line.body} for line in _CONNECTOR.consent_lines_requester
    ]
    assert (await _decided(client, admin))[0]["consentLinesApprover"] == expected


async def test_rows_written_in_one_transaction_still_order_by_their_id(client, db_session) -> None:
    """★ THE TIE-BREAK'S OWN CASE. `created_at` defaults to `now()`, which in PostgreSQL is the
    TRANSACTION's timestamp — several rows written together share it EXACTLY, which is precisely
    what happens when a test (or a backfill) seeds a queue. `id` is a UUIDv7, so it sorts by
    creation and settles the tie in the direction the timestamp meant to.

    Mutation check: drop `ConnectorAccessRequest.id.asc()` and this goes red."""
    admin = await _admin(db_session)
    stamped = _NOW - timedelta(hours=5)
    asked = []
    for index in range(3):
        person = await UserFactory.create(db_session, display_name=f"Person {index}")
        asked.append(
            await seed_request(
                db_session, person.id, ConnectorRequestStatus.PENDING, created_at=stamped
            )
        )

    rows = await _waiting(client, admin)

    assert [row["id"] for row in rows] == [str(row.id) for row in asked]


async def test_a_cancelled_request_is_on_neither_table(client, db_session) -> None:
    """★ THE `cancelled` CLAIM. A citizen who withdrew their ask is not waiting on anybody and
    had no decision made about them. Filtering `!= pending` for the decided table would put
    their row under `ALREADY DECIDED` with a blank decision, a blank date and a blank decider.

    Paired with a liveness assertion — a second, genuinely pending row IS returned — so a
    crashed listing cannot pass this absence."""
    admin = await _admin(db_session)
    quitter = await UserFactory.create(db_session, display_name="Rakesh Iyer")
    waiting = await UserFactory.create(db_session, display_name="Meera Rao")
    await seed_request(db_session, quitter.id, ConnectorRequestStatus.CANCELLED)
    await seed_request(db_session, waiting.id, ConnectorRequestStatus.PENDING)

    assert [row["displayName"] for row in await _waiting(client, admin)] == ["Meera Rao"]
    assert await _decided(client, admin) == []


async def test_the_display_name_falls_back_to_the_work_email(client, db_session) -> None:
    """`users.display_name` is NULLABLE, and the substitution is the SERVER's — the same rule
    `services/connectors/access.PersonAccess` already applies to a decider's name. One fallback,
    written once, so no panel writes a second one and no cell renders an empty string beside an
    authorization decision.

    Mutation check: return the raw column and this reads `None`."""
    admin = await _admin(db_session)
    anonymous = await UserFactory.create(db_session, display_name=None, email="no.name@bial.com")
    await seed_request(db_session, anonymous.id, ConnectorRequestStatus.PENDING)

    row = (await _waiting(client, admin))[0]

    assert row["displayName"] == "no.name@bial.com"
    assert row["email"] == "no.name@bial.com"


# --- the decided table ----------------------------------------------------------


async def test_the_decided_table_is_newest_decision_first(client, db_session) -> None:
    """The other order, and the axis is the DECISION's — `WHEN` is the column the board draws,
    not when they asked. Seeded so the decision order disagrees with the asking order."""
    admin = await _admin(db_session)
    for name, asked_hours, decided_hours in (
        ("Anant Gupta", 50, 2),
        ("Rakesh Iyer", 10, 6),
        ("Meera Rao", 90, 4),
    ):
        person = await UserFactory.create(db_session, display_name=name)
        await seed_request(
            db_session,
            person.id,
            ConnectorRequestStatus.APPROVED,
            created_at=_NOW - timedelta(hours=asked_hours),
            decided_by_id=admin.id,
            decided_at=_NOW - timedelta(hours=decided_hours),
        )

    rows = await _decided(client, admin)

    assert [row["displayName"] for row in rows] == ["Anant Gupta", "Meera Rao", "Rakesh Iyer"]


async def test_a_declined_row_carries_its_remark_and_no_count(client, db_session) -> None:
    """`ALREADY DECIDED` draws a red pill, the administrator's words, and an em dash under
    `USING IT IN`. `null` is what the em dash renders from — `0` would claim we counted."""
    admin = await _admin(db_session, display_name="Rahul Menon")
    person = await UserFactory.create(db_session, display_name="Rakesh Iyer")
    await seed_request(
        db_session,
        person.id,
        ConnectorRequestStatus.DECLINED,
        decided_by_id=admin.id,
        decided_at=_NOW,
        decision_remarks=DECLINE_REMARKS,
    )
    # An enabled project of theirs, so `null` cannot pass merely because there is nothing to
    # count: a declined person may well have switched something on before they were refused.
    project = await ProjectFactory.create(db_session, person.id)
    await _switch_on(db_session, project.id)

    row = (await _decided(client, admin))[0]

    assert row["status"] == "declined"
    assert row["decisionRemarks"] == DECLINE_REMARKS
    assert row["decidedById"] == str(admin.id)
    assert row["decidedByName"] == "Rahul Menon"
    assert row["usingItIn"] is None


async def test_using_it_in_counts_only_this_persons_enabled_projects(client, db_session) -> None:
    """★ THE CROSS-USER CLAIM. This is the one connector surface with no `user_id` predicate, so
    the GROUPING key is the isolation. A second approved citizen with three enabled projects is
    seeded for exactly that reason: drop `Project.user_id` from the group-by and the first
    person's row would total the platform's switches.

    A disabled row is not counted, and an approved person with nothing switched on reads `0` —
    a real answer, not the declined row's `null`."""
    admin = await _admin(db_session)
    mine = await UserFactory.create(db_session, display_name="Anant Gupta")
    theirs = await UserFactory.create(db_session, display_name="Somebody Else")
    idle = await UserFactory.create(db_session, display_name="Not Started Yet")
    for person in (mine, theirs, idle):
        await seed_request(
            db_session,
            person.id,
            ConnectorRequestStatus.APPROVED,
            decided_by_id=admin.id,
            decided_at=_NOW,
        )

    for enabled in (True, True, False):
        project = await ProjectFactory.create(db_session, mine.id)
        await _switch_on(db_session, project.id, enabled=enabled)
    for _ in range(3):
        project = await ProjectFactory.create(db_session, theirs.id)
        await _switch_on(db_session, project.id)
    # A project with no `project_connectors` row at all: never switched on is not `enabled=false`.
    await ProjectFactory.create(db_session, idle.id)

    counted = {row["displayName"]: row["usingItIn"] for row in await _decided(client, admin)}

    assert counted == {"Anant Gupta": 2, "Somebody Else": 3, "Not Started Yet": 0}


async def test_a_decision_outlives_the_administrator_who_made_it(client, db_session) -> None:
    """`decided_by_id` is `ON DELETE SET NULL`: an administrator leaving BIAL must not delete the
    record that somebody else's access was granted. The row keeps its date and its remark with
    the decider unnamed, and `null` here means "nobody to name" — never "we could not find one",
    which is why the two are told apart by `status` and not by this field."""
    admin = await _admin(db_session)
    departing = await UserFactory.create(
        db_session, display_name="Gone Already", email="gone@bial.com"
    )
    person = await UserFactory.create(db_session, display_name="Meera Rao")
    await seed_request(
        db_session,
        person.id,
        ConnectorRequestStatus.APPROVED,
        decided_by_id=departing.id,
        decided_at=_NOW,
    )
    await db_session.delete(departing)
    await db_session.flush()

    row = (await _decided(client, admin))[0]

    assert row["status"] == "approved"
    assert row["decidedAt"] is not None
    assert row["decidedById"] is None
    assert row["decidedByName"] is None


# --- the filters ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("q", "expected"),
    [
        ("priya", ["Priya Nair"]),
        ("PRIYA", ["Priya Nair"]),
        ("NAIR", ["Priya Nair"]),
        ("sam.fernandes@bial.com", ["Sam Fernandes"]),
        ("BIAL.COM", ["Priya Nair", "Sam Fernandes"]),
        ("nobody at all", []),
    ],
    ids=["name", "name-upper", "surname", "whole-email", "email-fragment", "no-match"],
)
async def test_q_matches_a_name_or_a_work_email_case_insensitively(
    client, db_session, q, expected
) -> None:
    """`Search people…` scopes an unbounded cross-user set, so it belongs in the query rather
    than in the browser. Both columns match, and neither match is case-sensitive — an
    administrator typing a surname in lower case is the ordinary case, not the exotic one."""
    admin = await _admin(db_session)
    for index, (name, email) in enumerate(
        (("Priya Nair", "priya.nair@bial.com"), ("Sam Fernandes", "sam.fernandes@bial.com"))
    ):
        person = await UserFactory.create(db_session, display_name=name, email=email)
        await seed_request(
            db_session,
            person.id,
            ConnectorRequestStatus.PENDING,
            created_at=_NOW - timedelta(hours=10 - index),
        )

    assert [row["displayName"] for row in await _waiting(client, admin, q=q)] == expected


async def test_the_connector_filter_narrows_to_one_catalogue_key(client, db_session) -> None:
    admin = await _admin(db_session)
    person = await UserFactory.create(db_session, display_name="Priya Nair")
    await seed_request(db_session, person.id, ConnectorRequestStatus.PENDING)

    assert len(await _waiting(client, admin, connector=KEY)) == 1


@pytest.mark.parametrize(
    ("params", "code"),
    [
        ({"state": "everything"}, "invalid_state"),
        ({"state": "Waiting"}, "invalid_state"),
        ({"state": "waiting", "connector": "no-such-system"}, "unknown_connector"),
    ],
    ids=["unknown-state", "wrong-case-state", "unknown-connector"],
)
async def test_a_filter_that_names_nothing_is_refused_rather_than_ignored(
    client, db_session, params, code
) -> None:
    """★ FAIL CLOSED ON A FILTER. Silently ignoring an unrecognised `state` would return the
    WAITING queue to a screen asking for the decided one — or, worse, every row to a screen
    asking for a connector that is not in the catalogue. Asserted on the `code`, not on the
    status: two refusals share 400 and "it 400'd" cannot tell them apart."""
    admin = await _admin(db_session)
    person = await UserFactory.create(db_session)
    await seed_request(db_session, person.id, ConnectorRequestStatus.PENDING)

    resp = await client.get(QUEUE, headers=auth_headers(admin), params=params)

    assert resp.status_code == 400, resp.text
    assert resp.json()["error"]["code"] == code


async def test_a_missing_state_is_refused(client, db_session) -> None:
    """`state` selects the table AND its order, so there is no honest default. FastAPI answers
    a missing required query parameter itself, with `type: "missing"` naming the field."""
    admin = await _admin(db_session)

    resp = await client.get(QUEUE, headers=auth_headers(admin))

    assert resp.status_code == 422, resp.text
    assert resp.json()["detail"][0]["type"] == "missing"


async def test_an_over_long_q_is_refused_in_the_data_plane_envelope(client, db_session) -> None:
    """The platform's shared `?q=` boundary (`api/v1/pagination.clean_search`), reached through
    this route so the bound is not merely assumed to apply here."""
    admin = await _admin(db_session)

    resp = await client.get(
        QUEUE, headers=auth_headers(admin), params={"state": "waiting", "q": "x" * 201}
    )

    assert resp.status_code == 422, resp.text
    assert resp.json()["error"]["message"] == "q must be at most 200 characters."


# --- the cap --------------------------------------------------------------------


async def test_the_listing_stops_at_the_cap_and_says_so(client, db_session) -> None:
    """The cap is REPORTED, not hidden. Nothing bounds how many people may ask for a connector,
    and a queue that quietly returned a prefix would leave somebody waiting forever behind a
    screen that reads caught-up. `truncated` is what the panel turns into "narrow the search".

    Seeded as DECIDED rows because `uq_connector_access_requests_one_pending` allows a person at
    most one open ask — decided rows accumulate freely underneath it, so the cap can be reached
    with a handful of people rather than 201 of them."""
    admin = await _admin(db_session)
    person = await UserFactory.create(db_session, display_name="Anant Gupta")
    db_session.add_all(
        ConnectorAccessRequest(
            user_id=person.id,
            connector_key=KEY,
            status=ConnectorRequestStatus.APPROVED,
            requester_remarks=REMARKS,
            decided_by_id=admin.id,
            decided_at=_NOW - timedelta(minutes=index),
        )
        for index in range(LISTING_CAP + 1)
    )
    await db_session.flush()

    resp = await client.get(QUEUE, headers=auth_headers(admin), params={"state": "decided"})

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert len(body["requests"]) == LISTING_CAP
    assert body["truncated"] is True


# --- the count ------------------------------------------------------------------


async def test_the_count_is_the_waiting_rows_and_nothing_else(client, db_session) -> None:
    """The badge answers one question: is anybody waiting on me. Decided and cancelled rows are
    not, and a count that included them would show a red badge over a caught-up queue."""
    admin = await _admin(db_session)
    for status_value in (
        ConnectorRequestStatus.PENDING,
        ConnectorRequestStatus.PENDING,
        ConnectorRequestStatus.APPROVED,
        ConnectorRequestStatus.DECLINED,
        ConnectorRequestStatus.CANCELLED,
    ):
        person = await UserFactory.create(db_session)
        await seed_request(db_session, person.id, status_value)

    resp = await client.get(COUNTS, headers=auth_headers(admin))

    assert resp.status_code == 200, resp.text
    assert resp.json() == {"waiting": 2}


async def test_an_empty_queue_is_an_empty_list_and_a_zero(client, db_session) -> None:
    """★ NOT A 404. An administrator with nothing waiting is the steady state most days, and
    both surfaces have to say so positively: an empty list the panel renders as
    `Nobody is waiting on a decision`, and a zero the badge renders as no badge at all."""
    admin = await _admin(db_session)

    listing = await client.get(QUEUE, headers=auth_headers(admin), params={"state": "waiting"})
    counts = await client.get(COUNTS, headers=auth_headers(admin))

    assert listing.status_code == 200, listing.text
    assert listing.json() == {"requests": [], "truncated": False}
    assert counts.status_code == 200, counts.text
    assert counts.json() == {"waiting": 0}


async def test_reading_the_queue_writes_no_audit_row(client, db_session) -> None:
    """R11: the audited pair is approve and decline — one person acting on another. A READ of
    the queue changes nothing, and an audit row per page load would bury the two rows that
    matter under a poll's worth of noise."""
    admin = await _admin(db_session)
    person = await UserFactory.create(db_session)
    await seed_request(db_session, person.id, ConnectorRequestStatus.PENDING)

    assert (
        await client.get(QUEUE, headers=auth_headers(admin), params={"state": "waiting"})
    ).status_code == 200
    assert (await client.get(COUNTS, headers=auth_headers(admin))).status_code == 200

    audited = await db_session.scalar(sa.select(sa.func.count()).select_from(AuditLog))
    assert audited == 0


# --- the gate -------------------------------------------------------------------


@pytest.mark.parametrize("path", ["", "/counts"], ids=["listing", "counts"])
@pytest.mark.parametrize(
    ("email", "allowed"),
    [("nobody@rvaiglobal.com", False), ("admin@bial.com", True)],
    ids=["citizen", "super-admin"],
)
async def test_every_read_here_is_super_admin_only(
    client, db_session, path, email, allowed
) -> None:
    """★ RBAC ACROSS BOTH COMPUTED ROLES, on every route in this module. The queue is the one
    connector surface with no `user_id` predicate, so the gate IS the isolation: a citizen
    reaching it would read every colleague's stated reason for wanting operational data.

    The refusal is asserted on its SENTENCE, not merely on 403 — the CSRF gate answers 403 too,
    and the two must not be able to stand in for each other."""
    caller = await UserFactory.create(db_session, email=email)

    resp = await client.get(
        f"{QUEUE}{path}", headers=auth_headers(caller), params={"state": "waiting"}
    )

    if allowed:
        assert resp.status_code == 200, resp.text
    else:
        assert resp.status_code == 403, resp.text
        assert resp.json()["detail"] == "Super-admin privileges required."


async def test_an_unauthenticated_caller_is_refused(client, db_session) -> None:
    await _citizen(db_session)

    resp = await client.get(QUEUE, params={"state": "waiting"})

    assert resp.status_code == 401, resp.text
