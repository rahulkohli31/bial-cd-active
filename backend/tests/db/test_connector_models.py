"""The two connector tables and the connector registry, against the REAL migrated schema.

WHAT THIS FILE IS FOR. Three of the four things this unit ships are guarantees only the DATABASE
can make — the one-open-ask-per-person partial unique index, the one-row-per-project-per-connector
constraint, and the window-shape CHECK — so they are asserted by making Postgres refuse the write,
inside the per-test transaction the `db_session` fixture rolls back. Every refusal is asserted on
the CONSTRAINT NAME, never on a bare `IntegrityError`: a NOT NULL violation and a CHECK violation
raise the same class, so "it raised" is not evidence the constraint under test is the one that
fired.

DELIBERATELY NO MIGRATION ROUND-TRIP TEST, and this is a considered omission rather than a gap. The
same upgrade → downgrade → upgrade is performed BY HAND in the unit's verification, and the
automated form permanently burns `pg_attribute` slots on the shared `citizen_one_test` database (a
dropped column's attnum is never reused, ~1600 per table ever) — which is why it would have to wear
`@pytest.mark.destructive_migration`, which is why it would be deselected from the default lane,
which is why it would never run again after the day it was written. Two guards, one hazard; keep
the cheaper one. The tables reaching this file at all is itself evidence the upgrade ran.

THE REGISTRY SECTION IS A MUTANT TRAP. `CONNECTORS` must hold exactly one entry. The boards draw a
second, greyed `[ANOTHER BIAL SYSTEM]` placeholder that the owner ruled out on 2026-09-08, and the
whole feature renders its connector lists by ITERATING the registry — so a second entry, added for
any reason, puts that placeholder back on three screens at once. These tests go red when it
appears."""

from __future__ import annotations

import dataclasses
import uuid
from datetime import date
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError, IntegrityError

from src.core.connectors import CONNECTORS, Connector, ConsentLine
from src.db.models.connector_access import (
    MAX_CONNECTOR_KEY,
    ConnectorAccessRequest,
    ConnectorRequestStatus,
)
from src.db.models.project_connector import ConnectorWindowKind, ProjectConnector
from tests.factories import ProjectFactory, UserFactory

_ONE_PENDING = "uq_connector_access_requests_one_pending"
_ONE_PER_PROJECT = "uq_project_connectors_project_connector"
_WINDOW_SHAPE = "ck_project_connectors_window_shape"

# A real key and a second, fictional one. The DATABASE does not validate keys — the registry does,
# at the route — so a second key is available here to prove a constraint spans the PAIR rather than
# the user or the project alone.
_DICE = "dice"
_OTHER = "another"

_REMARKS = "I build the stand and turnaround boards and they need on-block times."


def _request(user_id: uuid.UUID, **overrides: Any) -> ConnectorAccessRequest:
    data: dict[str, Any] = {
        "user_id": user_id,
        "connector_key": _DICE,
        "requester_remarks": _REMARKS,
    }
    data.update(overrides)
    return ConnectorAccessRequest(**data)


def _project_connector(project_id: uuid.UUID, **overrides: Any) -> ProjectConnector:
    """A switched-on row on the default window (`Last 30 days`), unless overridden."""
    data: dict[str, Any] = {
        "project_id": project_id,
        "connector_key": _DICE,
        "enabled": True,
        "window_kind": ConnectorWindowKind.RELATIVE,
        "window_days": 30,
    }
    data.update(overrides)
    return ProjectConnector(**data)


# --- the row shapes ---------------------------------------------------------------


async def test_a_request_persists_with_its_server_defaults(db_session) -> None:
    """A fresh ask is `pending`, undecided, and stamped — every one of those from the DDL rather
    than from Python, which is what makes the model and the migration agreeing observable."""
    user = await UserFactory.create(db_session)

    row = _request(user.id)
    db_session.add(row)
    await db_session.flush()
    await db_session.refresh(row)

    assert row.status is ConnectorRequestStatus.PENDING
    assert row.connector_key == _DICE
    assert row.requester_remarks == _REMARKS
    # Undecided is three NULLs, not a sentinel — and a reader must not confuse this with a
    # decided row whose administrator account was later deleted.
    assert row.decided_by_id is None
    assert row.decided_at is None
    assert row.decision_remarks is None
    assert row.id.version == 7
    assert row.created_at is not None
    assert row.updated_at is not None


async def test_a_project_row_persists_with_its_window(db_session) -> None:
    """The per-project half: a switch and the days, anchored on the project rather than on a
    user_id of its own (`projects` is the ownership anchor)."""
    user = await UserFactory.create(db_session)
    project = await ProjectFactory.create(db_session, user_id=user.id)

    row = _project_connector(project.id)
    db_session.add(row)
    await db_session.flush()
    await db_session.refresh(row)

    assert row.enabled is True
    assert row.window_kind is ConnectorWindowKind.RELATIVE
    assert row.window_days == 30
    assert row.window_start is None
    assert row.window_end is None
    assert row.id.version == 7
    assert row.created_at is not None
    assert row.updated_at is not None


async def test_a_project_row_defaults_to_switched_off(db_session) -> None:
    """`enabled` has a server default of false. A row written by anything other than an explicit
    switch-on is OFF — the safe direction for a data-access control."""
    user = await UserFactory.create(db_session)
    project = await ProjectFactory.create(db_session, user_id=user.id)

    row = ProjectConnector(
        project_id=project.id,
        connector_key=_DICE,
        window_kind=ConnectorWindowKind.RELATIVE,
        window_days=7,
    )
    db_session.add(row)
    await db_session.flush()
    await db_session.refresh(row)

    assert row.enabled is False


async def test_a_raw_insert_takes_its_key_and_stamps_from_the_server(db_session) -> None:
    """The ORM's `default=uuid.uuid7` hides the SERVER default, so the mixin's `uuidv7()` and the
    `now()` stamps are only ever exercised by a statement that supplies neither — a migration
    backfill, a psql session during an incident, or the seed of a future backfill."""
    user = await UserFactory.create(db_session)

    inserted = (
        await db_session.execute(
            sa.text(
                "INSERT INTO connector_access_requests "
                "(user_id, connector_key, requester_remarks) "
                "VALUES (:user_id, :key, :remarks) "
                "RETURNING id, status, created_at, updated_at"
            ),
            {"user_id": user.id, "key": _DICE, "remarks": _REMARKS},
        )
    ).one()

    assert inserted.id.version == 7
    assert inserted.status == ConnectorRequestStatus.PENDING.value
    assert inserted.created_at is not None
    assert inserted.updated_at is not None


# --- at most one open ask ---------------------------------------------------------


async def test_a_second_pending_ask_for_the_same_connector_is_refused(db_session) -> None:
    """THE guard this table's shape exists for: one open ask per person per connector, so the
    cancel route's "the caller's pending row" is unambiguous and a double-submitted dialog cannot
    put two identical rows in the administrator's queue."""
    user = await UserFactory.create(db_session)
    db_session.add(_request(user.id))
    await db_session.flush()

    with pytest.raises(IntegrityError) as caught:
        async with db_session.begin_nested():
            db_session.add(_request(user.id))
            await db_session.flush()
    assert _ONE_PENDING in str(caught.value)


async def test_a_pending_ask_may_sit_beside_a_settled_one(db_session) -> None:
    """The index is PARTIAL, and this is the half that proves it. A cancelled row and a live
    pending row for the same pair coexist — a plain UNIQUE on the pair would forbid this and make
    a second ask impossible for the rest of that person's life."""
    user = await UserFactory.create(db_session)

    db_session.add(_request(user.id, status=ConnectorRequestStatus.CANCELLED))
    await db_session.flush()
    db_session.add(_request(user.id))
    await db_session.flush()

    live = await db_session.scalar(
        sa.select(sa.func.count())
        .select_from(ConnectorAccessRequest)
        .where(ConnectorAccessRequest.user_id == user.id)
    )
    assert live == 2


async def test_two_people_may_each_hold_a_pending_ask(db_session) -> None:
    """The pair, not the connector: one person asking must never lock out another."""
    first = await UserFactory.create(db_session)
    second = await UserFactory.create(db_session, azure_oid=f"oid-{uuid.uuid4()}")

    db_session.add(_request(first.id))
    db_session.add(_request(second.id))
    await db_session.flush()


async def test_one_person_may_hold_a_pending_ask_per_connector(db_session) -> None:
    """…and the other half of the pair: the guard spans `(user_id, connector_key)`, so a person
    waiting on one connector is not blocked from asking for a second."""
    user = await UserFactory.create(db_session)

    db_session.add(_request(user.id, connector_key=_DICE))
    db_session.add(_request(user.id, connector_key=_OTHER))
    await db_session.flush()


# --- one row per project per connector --------------------------------------------


async def test_a_second_row_for_the_same_project_and_connector_is_refused(db_session) -> None:
    """The invariant AND the upsert's `ON CONFLICT` inference target: two switch presses racing
    from two tabs must land on one row, not two rows disagreeing about the window."""
    user = await UserFactory.create(db_session)
    project = await ProjectFactory.create(db_session, user_id=user.id)
    db_session.add(_project_connector(project.id))
    await db_session.flush()

    with pytest.raises(IntegrityError) as caught:
        async with db_session.begin_nested():
            db_session.add(_project_connector(project.id, enabled=False))
            await db_session.flush()
    assert _ONE_PER_PROJECT in str(caught.value)


async def test_each_project_carries_its_own_row(db_session) -> None:
    """Per project, not per person: switching a connector on in one project must not touch
    another, because the days are the project's choice."""
    user = await UserFactory.create(db_session)
    first = await ProjectFactory.create(db_session, user_id=user.id)
    second = await ProjectFactory.create(db_session, user_id=user.id, name="Second Project")

    db_session.add(_project_connector(first.id))
    db_session.add(_project_connector(second.id, window_days=7))
    await db_session.flush()


# --- the window shape -------------------------------------------------------------


@pytest.mark.parametrize(
    ("label", "columns"),
    [
        (
            "a relative window carrying an absolute start",
            {
                "window_kind": ConnectorWindowKind.RELATIVE,
                "window_days": 7,
                "window_start": date(2026, 9, 1),
            },
        ),
        (
            "an absolute window carrying a day count",
            {
                "window_kind": ConnectorWindowKind.ABSOLUTE,
                "window_days": 7,
                "window_start": date(2026, 9, 1),
                "window_end": date(2026, 9, 8),
            },
        ),
        (
            "a relative window with no day count",
            {
                "window_kind": ConnectorWindowKind.RELATIVE,
                "window_days": None,
            },
        ),
        (
            "an absolute window with only one end",
            {
                "window_kind": ConnectorWindowKind.ABSOLUTE,
                "window_days": None,
                "window_start": date(2026, 9, 1),
            },
        ),
    ],
)
async def test_a_mismatched_window_is_refused(db_session, label: str, columns: dict) -> None:
    """Exactly the right columns for the kind, enforced by the database.

    This is the constraint the upsert's `DO UPDATE` has to respect: the tempting per-column
    `COALESCE(EXCLUDED.x, existing.x)` leaves a relative window landing on a stored absolute row
    with days, start AND end all populated — the first case below, reached from live code.

    Asserted on the CONSTRAINT NAME. `window_days IS NULL` on a relative row would raise
    `IntegrityError` from a NOT NULL constraint too, and that would be a different, weaker table
    passing this test.
    """
    user = await UserFactory.create(db_session)
    project = await ProjectFactory.create(db_session, user_id=user.id)

    with pytest.raises(IntegrityError) as caught:
        async with db_session.begin_nested():
            db_session.add(_project_connector(project.id, **columns))
            await db_session.flush()
    assert _WINDOW_SHAPE in str(caught.value), label


async def test_a_window_with_no_kind_at_all_is_refused(db_session) -> None:
    """`window_kind`'s own NOT NULL is not redundant with the CHECK, and this is why: a NULL kind
    makes both of the CHECK's disjuncts UNKNOWN, and a CHECK constraint PASSES on UNKNOWN. Drop the
    NOT NULL and a row with no kind and no window would be accepted."""
    user = await UserFactory.create(db_session)
    project = await ProjectFactory.create(db_session, user_id=user.id)

    with pytest.raises(IntegrityError) as caught:
        async with db_session.begin_nested():
            db_session.add(
                ProjectConnector(
                    project_id=project.id,
                    connector_key=_DICE,
                    window_kind=None,
                    window_days=None,
                )
            )
            await db_session.flush()
    assert "window_kind" in str(caught.value)


# --- the native enums -------------------------------------------------------------


async def test_a_wrong_case_status_is_refused_by_the_database(db_session) -> None:
    """THE point of a native PG enum over a `varchar` + application discipline: `'Pending'` is not
    a label, and the database says so at the write rather than at the read six weeks later."""
    user = await UserFactory.create(db_session)

    with pytest.raises(DBAPIError) as caught:
        async with db_session.begin_nested():
            await db_session.execute(
                sa.text(
                    "INSERT INTO connector_access_requests "
                    "(user_id, connector_key, status, requester_remarks) "
                    "VALUES (:user_id, :key, 'Pending', :remarks)"
                ),
                {"user_id": user.id, "key": _DICE, "remarks": _REMARKS},
            )
    assert "connector_request_status" in str(caught.value)


async def test_an_unknown_window_kind_is_refused_by_the_database(db_session) -> None:
    """The same guarantee on the other enum — a `varchar` column would have taken `'rolling'`."""
    user = await UserFactory.create(db_session)
    project = await ProjectFactory.create(db_session, user_id=user.id)

    with pytest.raises(DBAPIError) as caught:
        async with db_session.begin_nested():
            await db_session.execute(
                sa.text(
                    "INSERT INTO project_connectors "
                    "(project_id, connector_key, window_kind, window_days) "
                    "VALUES (:project_id, :key, 'rolling', 7)"
                ),
                {"project_id": project.id, "key": _DICE},
            )
    assert "connector_window_kind" in str(caught.value)


def test_the_request_status_labels_are_exactly_the_four_states() -> None:
    """FOUR labels, and no `withdrawn`. Person state 5 on the `ConnectorStates` board is out of
    this pass (origin Q2): nothing here could set it, and an unreachable label would cost an
    `ALTER TYPE` to remove. Asserted as an exact set so both a missing state and a speculative
    extra one go red."""
    assert {member.value for member in ConnectorRequestStatus} == {
        "pending",
        "approved",
        "declined",
        "cancelled",
    }


def test_the_window_kind_labels_are_exactly_the_two_shapes() -> None:
    assert {member.value for member in ConnectorWindowKind} == {"relative", "absolute"}


# --- cascades ---------------------------------------------------------------------


async def test_deleting_a_project_cascades_its_connector_rows(db_session) -> None:
    """A deleted project can never leave a switch pointing at a project id nothing resolves."""
    user = await UserFactory.create(db_session)
    project = await ProjectFactory.create(db_session, user_id=user.id)
    db_session.add(_project_connector(project.id))
    await db_session.flush()

    await db_session.execute(sa.text("DELETE FROM projects WHERE id = :i"), {"i": project.id})

    survivors = await db_session.scalar(
        sa.select(sa.func.count())
        .select_from(ProjectConnector)
        .where(ProjectConnector.project_id == project.id)
    )
    assert survivors == 0


async def test_deleting_a_person_cascades_their_own_requests(db_session) -> None:
    """`OwnedByUserMixin`'s CASCADE: the asks are the person's, and they go with the person."""
    user = await UserFactory.create(db_session)
    db_session.add(_request(user.id))
    await db_session.flush()

    await db_session.execute(sa.text("DELETE FROM users WHERE id = :i"), {"i": user.id})

    survivors = await db_session.scalar(
        sa.select(sa.func.count())
        .select_from(ConnectorAccessRequest)
        .where(ConnectorAccessRequest.user_id == user.id)
    )
    assert survivors == 0


async def test_deleting_the_decider_keeps_the_decision_and_unnames_them(db_session) -> None:
    """`ON DELETE SET NULL` on `decided_by_id` — the trail outlives the actor. An administrator
    leaving BIAL must not delete the record that somebody ELSE was granted access; the citizen
    keeps their grant, and the row keeps its date and its status with an unnamed decider.

    The positive half is the point: it would be trivially easy to satisfy "the decider is NULL" by
    cascading the whole row away, so the row's survival is asserted first.
    """
    citizen = await UserFactory.create(db_session)
    admin = await UserFactory.create(db_session, azure_oid=f"oid-{uuid.uuid4()}")
    row = _request(
        citizen.id,
        status=ConnectorRequestStatus.APPROVED,
        decided_by_id=admin.id,
        decided_at=sa.func.now(),
    )
    db_session.add(row)
    await db_session.flush()
    request_id = row.id

    await db_session.execute(sa.text("DELETE FROM users WHERE id = :i"), {"i": admin.id})

    survived = (
        await db_session.execute(
            sa.select(
                ConnectorAccessRequest.status,
                ConnectorAccessRequest.decided_at,
                ConnectorAccessRequest.decided_by_id,
            ).where(ConnectorAccessRequest.id == request_id)
        )
    ).one_or_none()
    assert survived is not None, "the decision row must outlive the administrator who made it"
    assert survived.status is ConnectorRequestStatus.APPROVED
    assert survived.decided_at is not None
    assert survived.decided_by_id is None


# --- the registry -----------------------------------------------------------------


def test_the_registry_holds_exactly_one_connector() -> None:
    """MUTANT: add a second entry and this goes red — deliberately.

    The greyed `[ANOTHER BIAL SYSTEM]` row the boards draw is not built (owner ruling, 2026-09-08),
    and every connector list in this feature is rendered by iterating `CONNECTORS`. So a second
    entry, added for any reason at all, is the placeholder returning to three screens at once. The
    key is asserted too: "one entry" alone would pass on a renamed one.
    """
    assert set(CONNECTORS) == {"dice"}


def test_the_entry_carries_the_board_literals() -> None:
    """`ConnectorStates` state 1 draws the row this renders: the name and the one-line subtitle."""
    dice = CONNECTORS["dice"]
    assert dice.display_name == "DICE"
    assert dice.subtitle == "Airport operations"
    assert dice.max_window_days == 30


def test_the_ask_subtitle_is_the_ask_board_verbatim() -> None:
    """`AskAccess`'s own sentence under its title, byte-exact — typographic apostrophe and em
    dash included, because the panel renders this string and nothing reconstructs it.

    ASSERTED SEPARATELY FROM `subtitle` because they are two different sentences about the same
    connector: the row's four-word label, and the whole sentence the ask panel opens with. A
    reader who assumed one was a truncation of the other would delete the wrong one."""
    assert CONNECTORS["dice"].ask_subtitle == (
        "DICE is BIAL’s airport operations data. An administrator decides who may read it — "
        "you are asking once, for yourself."
    )
    assert CONNECTORS["dice"].ask_subtitle != CONNECTORS["dice"].subtitle


def test_the_requester_consent_lines_are_the_ask_dialog_verbatim() -> None:
    """`AskAccess`'s `WHAT AN APPROVAL GIVES YOU`, in the second person, shipped whole. This is
    consent copy — R1 makes it binding in substance — so a summarised rewrite goes red here rather
    than in a browser."""
    assert CONNECTORS["dice"].consent_lines_requester == (
        ConsentLine(
            lead="Read-only.",
            body="Nothing you build can change DICE data.",
        ),
        ConsentLine(
            lead="One dataset.",
            body=(
                "The Flight Fact Report — flight schedules, gates, stands and status. "
                "Nothing else in DICE."
            ),
        ),
        ConsentLine(
            lead="Every project you own.",
            body=(
                "Including ones you have not made yet. You switch it on per project, "
                "and pick the days each one reads."
            ),
        ),
    )


def test_the_approver_consent_lines_are_the_decide_dialog_verbatim() -> None:
    """`AdminReview`'s `WHAT APPROVING GIVES THEM`, in the THIRD person. A separate set on purpose:
    the two panels differ in voice and in content, and folding them together would ship
    `Nothing you build can change DICE data` to the approver while dropping the thirty-day promise
    from their panel entirely."""
    assert CONNECTORS["dice"].consent_lines_approver == (
        ConsentLine(
            lead="Read access to the Flight Fact Report.",
            body="and nothing else in DICE.",
        ),
        ConsentLine(
            lead="Every project they own.",
            body="including ones they have not made yet. They switch it on per project.",
        ),
        ConsentLine(
            lead="Up to 30 days of history while they build.",
            body=(
                "each project picks its own range; a published app reads the dates its users pick."
            ),
        ),
    )


def test_the_approver_panel_names_the_same_cap_the_resolver_will_enforce() -> None:
    """TWO EMITTERS OF ONE FACT. The approver's third line states the window cap in prose and
    `max_window_days` states it as the number the resolver clamps to — so an entry whose cap is
    changed without its copy would have an administrator approving a promise the product does not
    keep. Assert the number out of the sentence rather than the sentence as a whole: this is a
    check on the FACT, and the wording around it is already pinned above."""
    for key, connector in CONNECTORS.items():
        lead = connector.consent_lines_approver[2].lead
        numbers = [word for word in lead.replace(".", " ").split() if word.isdigit()]
        assert numbers == [str(connector.max_window_days)], (
            f"{key}: the approver's consent line says {numbers}, "
            f"but max_window_days is {connector.max_window_days}"
        )


def test_a_connector_carries_exactly_these_fields() -> None:
    """The field set is the contract, and the notable absence is `available`.

    An earlier draft carried an availability flag so the ask and switch-on routes could refuse a
    write against the greyed placeholder — a KNOWN key a hand-crafted call could name. With the
    placeholder gone there is no known-but-unusable key: anything outside the registry is unknown
    and 404s, the same guard for no field and no branch. Asserted as an exact set so both a
    re-added flag and a quietly dropped field go red.
    """
    assert {field.name for field in dataclasses.fields(Connector)} == {
        "display_name",
        "subtitle",
        "ask_subtitle",
        "data_noun",
        "max_window_days",
        "consent_lines_requester",
        "consent_lines_approver",
    }


def test_the_registry_cannot_be_extended_at_runtime() -> None:
    """A `MappingProxyType`, not a dict. The surfaces that iterate this are the product's whole
    connector list, and "the list is whatever somebody put in the dict at import time" is not a
    reviewable claim. Paired with a positive read so a broken import cannot pass as a refusal.

    THE `Any` HOP IS THE TEST, not a way around one. All four type gates already refuse the typed
    form outright — that is the compile-time half of the guarantee, and it is why the assignment
    cannot simply be written here. This asserts the RUNTIME half, which is the one that survives an
    untyped call site, a `getattr`, or a plugin loader.
    """
    assert CONNECTORS["dice"].display_name == "DICE"
    smuggler: Any = CONNECTORS
    with pytest.raises(TypeError):
        smuggler["smuggled"] = CONNECTORS["dice"]


def test_an_entry_cannot_be_mutated_in_place() -> None:
    """Frozen dataclasses. The registry is read at import and never written; a typo'd assignment
    should fail loudly rather than silently redefine a connector for the life of the process. Same
    `Any` hop, same reason, as the test above."""
    assert CONNECTORS["dice"].max_window_days == 30
    entry: Any = CONNECTORS["dice"]
    with pytest.raises(dataclasses.FrozenInstanceError):
        entry.max_window_days = 90
    assert CONNECTORS["dice"].max_window_days == 30


def test_every_registry_key_fits_the_stored_column() -> None:
    """The registry decides what a connector key IS; `connector_key` is where it lands. They live
    in different modules on purpose (the registry gains an import that would close a
    models → core → services → models loop), so this is the seam that keeps them agreeing."""
    assert CONNECTORS
    for key in CONNECTORS:
        assert 0 < len(key) <= MAX_CONNECTOR_KEY
        assert key == key.lower()
