"""Which connectors a project may ACTUALLY read — the one fact the prompt and the toolset share.

WHY THIS FILE EXISTS SEPARATELY FROM `test_access_state.py`. That file drives the PERSON rule (the
most recent non-cancelled row). This one drives the PROJECT rule: switch AND approval AND
ownership, all three, resolved together. They are different questions with different failure
modes, and this one's failure mode is the serious one — a project that reads a connector it was
never granted, or a prompt that announces a connected system whose tool was never registered.

★ THE MUTANTS THIS FILE IS BUILT AROUND. Each is a one-line edit to
`connected_systems_for_project`, and each turns exactly the named test red:

  drop `Project.user_id == user_id` from the join predicate
      -> `test_another_persons_project_is_not_this_persons_to_read`
  return the system regardless of `window.effectively_on`
      -> `test_a_pending_owner_reads_nothing` and `test_a_switched_off_project_reads_nothing`
  drop the `window is None` arm
      -> `test_a_project_that_never_switched_it_on_reads_nothing_and_is_not_an_error`

Rows are seeded DIRECTLY rather than through the routes, for the reason `test_access_state.py`
gives: a rule that holds only because today's routes are disciplined breaks the first time
somebody adds a route.
"""

from __future__ import annotations

import uuid
from datetime import date, timedelta
from typing import Any

from src.core.connectors import CONNECTORS
from src.db.models.connector_access import ConnectorAccessRequest, ConnectorRequestStatus
from src.db.models.project_connector import ConnectorWindowKind, ProjectConnector
from src.services.connectors.access import connected_systems_for_project
from tests.factories import ProjectFactory, UserFactory

_KEY = next(iter(CONNECTORS))
_REMARKS = "I build the stand and turnaround boards and they need on-block times."


async def _approved(db: Any, user_id: uuid.UUID, status: ConnectorRequestStatus) -> None:
    db.add(
        ConnectorAccessRequest(
            user_id=user_id,
            connector_key=_KEY,
            status=status,
            requester_remarks=_REMARKS,
        )
    )
    await db.flush()


async def _switched_on(
    db: Any, project_id: uuid.UUID, *, enabled: bool = True, **overrides: Any
) -> ProjectConnector:
    data: dict[str, Any] = {
        "project_id": project_id,
        "connector_key": _KEY,
        "enabled": enabled,
        "window_kind": ConnectorWindowKind.RELATIVE,
        "window_days": 7,
    }
    data.update(overrides)
    row = ProjectConnector(**data)
    db.add(row)
    await db.flush()
    return row


async def test_approved_and_switched_on_is_the_one_case_that_reads(db_session) -> None:
    """Both halves present, and what comes back carries the registry entry AND the window — the
    prompt needs the first to name it and the tool needs the second to fail first on a system that
    is not on."""
    user = await UserFactory.create(db_session)
    project = await ProjectFactory.create(db_session, user_id=user.id)
    await _approved(db_session, user.id, ConnectorRequestStatus.APPROVED)
    await _switched_on(db_session, project.id)

    systems = await connected_systems_for_project(
        db_session, user_id=user.id, project_id=project.id
    )
    assert [system.key for system in systems] == [_KEY]
    assert systems[0].connector is CONNECTORS[_KEY]
    assert systems[0].window.effectively_on is True
    assert systems[0].window.days == 7


async def test_a_pending_owner_reads_nothing(db_session) -> None:
    """The switch is on and the administrator has not answered. `effectively_on` is the
    conjunction, and a project that reads on the switch alone is a project reading data nobody
    approved."""
    user = await UserFactory.create(db_session)
    project = await ProjectFactory.create(db_session, user_id=user.id)
    await _approved(db_session, user.id, ConnectorRequestStatus.PENDING)
    await _switched_on(db_session, project.id)

    assert (
        await connected_systems_for_project(db_session, user_id=user.id, project_id=project.id)
        == ()
    )


async def test_a_declined_owner_reads_nothing(db_session) -> None:
    """★ THE CASE THE WHOLE REGISTRATION GATE EXISTS FOR. The platform has a flow whose purpose
    is to say no to this person, and an administrator has used it. A tool surface that ignored
    that answer would not be thrifty; it would be wrong."""
    user = await UserFactory.create(db_session)
    project = await ProjectFactory.create(db_session, user_id=user.id)
    await _approved(db_session, user.id, ConnectorRequestStatus.DECLINED)
    await _switched_on(db_session, project.id)

    assert (
        await connected_systems_for_project(db_session, user_id=user.id, project_id=project.id)
        == ()
    )


async def test_a_switched_off_project_reads_nothing(db_session) -> None:
    """Approved for the person, off for this project. The other half of the conjunction, asserted
    separately from the first — a check that only ever saw them fail together would pass on an
    implementation that read either one."""
    user = await UserFactory.create(db_session)
    project = await ProjectFactory.create(db_session, user_id=user.id)
    await _approved(db_session, user.id, ConnectorRequestStatus.APPROVED)
    await _switched_on(db_session, project.id, enabled=False)

    assert (
        await connected_systems_for_project(db_session, user_id=user.id, project_id=project.id)
        == ()
    )


async def test_a_project_that_never_switched_it_on_reads_nothing_and_is_not_an_error(
    db_session,
) -> None:
    """No `project_connectors` row at all — the ordinary case for nearly every project on the
    platform, and it must be silence rather than a raise. `resolve_window` returns `None` here,
    which is a legitimately-absent result and a different fact from `enabled = false`."""
    user = await UserFactory.create(db_session)
    project = await ProjectFactory.create(db_session, user_id=user.id)
    await _approved(db_session, user.id, ConnectorRequestStatus.APPROVED)

    assert (
        await connected_systems_for_project(db_session, user_id=user.id, project_id=project.id)
        == ()
    )


async def test_another_persons_project_is_not_this_persons_to_read(db_session) -> None:
    """★ THE ISOLATION CASE (ADR-0004), asserted explicitly rather than inherited from the
    router's upstream ownership check.

    `project_connectors` has no user column, so `projects` is its ownership anchor and the join
    predicate IS the boundary. The routers do check ownership first, and that changes nothing: a
    predicate that is correct only while a caller's earlier check stays true is not a boundary.
    Both people are approved and both projects are switched on here, so the ONLY thing separating
    them is the `user_id` in the WHERE clause."""
    owner = await UserFactory.create(db_session, email="owner@rvaiglobal.com")
    other = await UserFactory.create(db_session, email="other@rvaiglobal.com")
    theirs = await ProjectFactory.create(db_session, user_id=owner.id)
    await _approved(db_session, owner.id, ConnectorRequestStatus.APPROVED)
    await _approved(db_session, other.id, ConnectorRequestStatus.APPROVED)
    await _switched_on(db_session, theirs.id)

    assert (
        await connected_systems_for_project(db_session, user_id=other.id, project_id=theirs.id)
        == ()
    )
    assert (
        await connected_systems_for_project(db_session, user_id=owner.id, project_id=theirs.id)
        != ()
    )


async def test_one_projects_switch_does_not_answer_for_another(db_session) -> None:
    """The same person owns both. The switch belongs to the PROJECT — a departures board and a
    six-month trend want different amounts of history and the same citizen owns both — so an
    implementation that resolved per person would light up a project nobody switched on."""
    user = await UserFactory.create(db_session)
    switched_on = await ProjectFactory.create(db_session, user_id=user.id, name="Stand board")
    untouched = await ProjectFactory.create(db_session, user_id=user.id, name="Visitor log")
    await _approved(db_session, user.id, ConnectorRequestStatus.APPROVED)
    await _switched_on(db_session, switched_on.id)

    assert (
        await connected_systems_for_project(db_session, user_id=user.id, project_id=switched_on.id)
        != ()
    )
    assert (
        await connected_systems_for_project(db_session, user_id=user.id, project_id=untouched.id)
        == ()
    )


async def test_an_aged_out_absolute_window_still_reads(db_session) -> None:
    """A window whose dates are older than the connector keeps is still a project that MAY read —
    `resolve_window` slides it forward rather than switching it off. Worth asserting here because
    the tempting shortcut for this helper is "return the row if the dates look current", and that
    would take the tool away from a citizen whose picked range simply aged."""
    user = await UserFactory.create(db_session)
    project = await ProjectFactory.create(db_session, user_id=user.id)
    await _approved(db_session, user.id, ConnectorRequestStatus.APPROVED)
    long_ago = date(2026, 1, 1)
    await _switched_on(
        db_session,
        project.id,
        window_kind=ConnectorWindowKind.ABSOLUTE,
        window_days=None,
        window_start=long_ago,
        window_end=long_ago + timedelta(days=2),
    )

    systems = await connected_systems_for_project(
        db_session, user_id=user.id, project_id=project.id
    )
    assert [system.key for system in systems] == [_KEY]
    assert systems[0].window.days == 3
