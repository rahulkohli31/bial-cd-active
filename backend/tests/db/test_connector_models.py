"""The connector table and the connector registry, against the REAL migrated schema.

WHAT THIS FILE IS FOR. Two of the guarantees here are ones only the DATABASE can make — the
one-row-per-project-per-connector constraint and the window-shape CHECK — so they are asserted by
making Postgres refuse the write, inside the per-test transaction the `db_session` fixture rolls
back. Every refusal is asserted on the CONSTRAINT NAME, never on a bare `IntegrityError`: a NOT
NULL violation and a CHECK violation raise the same class, so "it raised" is not evidence the
constraint under test is the one that fired.

DELIBERATELY NO MIGRATION ROUND-TRIP TEST, and this is a considered omission rather than a gap. The
same upgrade → downgrade → upgrade is performed BY HAND in the unit's verification, and the
automated form permanently burns `pg_attribute` slots on the shared `citizen_one_test` database (a
dropped column's attnum is never reused, ~1600 per table ever) — which is why it would have to wear
`@pytest.mark.destructive_migration`, which is why it would be deselected from the default lane,
which is why it would never run again after the day it was written. Two guards, one hazard; keep
the cheaper one. The tables reaching this file at all is itself evidence the upgrade ran.

THE REGISTRY SECTION IS A MUTANT TRAP. `CONNECTORS` must hold exactly one entry. The boards draw a
second, greyed `[ANOTHER BIAL SYSTEM]` placeholder that the owner ruled out on 2026-09-08, and the
feature renders its connector list by ITERATING the registry — so a second entry, added for any
reason, puts that placeholder back on screen. These tests go red when it appears."""

from __future__ import annotations

import dataclasses
import uuid
from datetime import date
from typing import Any

import pytest
import sqlalchemy as sa
from sqlalchemy.exc import DBAPIError, IntegrityError

from src.core.connectors import CONNECTORS, Connector
from src.db.models.project_connector import (
    MAX_CONNECTOR_KEY,
    ConnectorWindowKind,
    ProjectConnector,
)
from tests.factories import ProjectFactory, UserFactory

_ONE_PER_PROJECT = "uq_project_connectors_project_connector"
_WINDOW_SHAPE = "ck_project_connectors_window_shape"

_DICE = "dice"


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


# --- the schema the upgrade leaves ------------------------------------------------


async def test_the_project_table_is_the_only_connector_table_left(db_session) -> None:
    """The switch is the whole of connector access, so there is no per-person ledger and no
    status type for one. The positive half is asserted first: a query that could not see the
    schema at all would report every name missing and pass the absence half for nothing."""
    present = await db_session.execute(
        sa.text(
            "SELECT to_regclass('project_connectors') IS NOT NULL, "
            "EXISTS (SELECT 1 FROM pg_type WHERE typname = 'connector_window_kind'), "
            "to_regclass('connector_access_requests') IS NOT NULL, "
            "EXISTS (SELECT 1 FROM pg_type WHERE typname = 'connector_request_status')"
        )
    )
    table, window_kind, ledger, request_status = present.one()

    assert (table, window_kind) == (True, True)
    assert (ledger, request_status) == (False, False)


# --- the row shapes ---------------------------------------------------------------


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
    project = await ProjectFactory.create(db_session, user_id=user.id)

    inserted = (
        await db_session.execute(
            sa.text(
                "INSERT INTO project_connectors "
                "(project_id, connector_key, window_kind, window_days) "
                "VALUES (:project_id, :key, 'relative', 7) "
                "RETURNING id, enabled, created_at, updated_at"
            ),
            {"project_id": project.id, "key": _DICE},
        )
    ).one()

    assert inserted.id.version == 7
    assert inserted.enabled is False
    assert inserted.created_at is not None
    assert inserted.updated_at is not None


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


# --- the native enum --------------------------------------------------------------


async def test_an_unknown_window_kind_is_refused_by_the_database(db_session) -> None:
    """THE point of a native PG enum over a `varchar` + application discipline: `'rolling'` is not
    a label, and the database says so at the write rather than at the read six weeks later."""
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


# --- the registry -----------------------------------------------------------------


def test_the_registry_holds_exactly_one_connector() -> None:
    """MUTANT: add a second entry and this goes red — deliberately.

    The greyed `[ANOTHER BIAL SYSTEM]` row the boards draw is not built (owner ruling, 2026-09-08),
    and the connector list is rendered by iterating `CONNECTORS`. So a second entry, added for any
    reason at all, is the placeholder returning to the screen. The key is asserted too: "one
    entry" alone would pass on a renamed one.
    """
    assert set(CONNECTORS) == {"dice"}


def test_the_entry_carries_the_board_literals() -> None:
    """`ConnectorStates` state 1 draws the row this renders: the name and the one-line subtitle."""
    dice = CONNECTORS["dice"]
    assert dice.display_name == "Flight Fact Data"
    assert dice.subtitle == "Airport operations"
    assert dice.max_window_days == 30


def test_a_connector_carries_exactly_these_fields() -> None:
    """The field set is the contract, and the notable absence is `available`.

    There is no known-but-unusable key: anything outside the registry is unknown and 404s, the same
    guard for no field and no branch. Asserted as an exact set so both an added flag and a quietly
    dropped field go red.
    """
    assert {field.name for field in dataclasses.fields(Connector)} == {
        "display_name",
        "subtitle",
        "data_noun",
        "max_window_days",
        "freshness_lag_days",
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
    assert CONNECTORS["dice"].display_name == "Flight Fact Data"
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
