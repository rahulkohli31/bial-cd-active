"""The compile signal for a tab with NO LIVE TURN (R17/R18).

THE HOLE THIS ROUTE CLOSES. The compile state reaches the portal as a frame on the turn stream,
so its producer stops the moment the turn does. Reload the page after a turn that ended red and
the pane comes up with no signal at all: it initialises uncovered, and the citizen is shown the
framework's full-screen error screen underneath a live-preview label. That is the exact failure
the cover exists to prevent, reachable by pressing F5.

Every unanswerable case is `unknown`, which the pane HOLDS its cover on. Absent must never read
as clean — that is the whole contract of this signal, and it is what each test here pins.
"""

from __future__ import annotations

import uuid

from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from src.services.sandbox.base import CompileReport, CompileState
from tests.api.v1.build_sessions.conftest import auth_headers
from tests.factories import AppRegistryFactory, ProjectFactory, UserFactory

_ROUTE = "/v1/build-sessions/projects/{project_id}/compile-state"


async def test_a_project_with_no_live_container_is_unknown_not_clean(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage
) -> None:
    """Nothing is serving this project, so there is nothing to ask. `clean` here would uncover
    the pane over a dead app on the strength of a question nobody answered."""
    user = await UserFactory.create(db_session, email="u11-nolive@rvaiglobal.com")
    project = await ProjectFactory.create(db_session, user.id)
    await AppRegistryFactory.create(db_session, user_id=user.id, project_id=project.id)

    resp = await client.get(_ROUTE.format(project_id=project.id), headers=auth_headers(user))

    assert resp.status_code == 200
    assert resp.json() == {"state": "unknown"}


async def test_another_users_project_is_404(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage
) -> None:
    """ADR-0004, same as every other route in this file: a cross-user project and a missing one
    are the same non-leaking answer."""
    owner = await UserFactory.create(db_session, email="u11-owner@rvaiglobal.com")
    project = await ProjectFactory.create(db_session, owner.id)
    intruder = await UserFactory.create(db_session, email="u11-intruder@rvaiglobal.com")

    resp = await client.get(_ROUTE.format(project_id=project.id), headers=auth_headers(intruder))

    assert resp.status_code == 404
    # LIVENESS: the route works — the owner gets a real answer for the same project.
    ok = await client.get(_ROUTE.format(project_id=project.id), headers=auth_headers(owner))
    assert ok.status_code == 200


async def test_an_unknown_project_is_404(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage
) -> None:
    user = await UserFactory.create(db_session, email="u11-noproj@rvaiglobal.com")
    resp = await client.get(_ROUTE.format(project_id=uuid.uuid4()), headers=auth_headers(user))
    assert resp.status_code == 404


async def test_a_project_with_no_app_is_unknown(
    client: AsyncClient, db_session: AsyncSession, fake_redis, fake_storage
) -> None:
    """Nobody has built here. The pane has its own vocabulary for that; this route claims
    nothing rather than reporting a healthy compile of an app that does not exist."""
    user = await UserFactory.create(db_session, email="u11-noapp@rvaiglobal.com")
    project = await ProjectFactory.create(db_session, user.id)

    resp = await client.get(_ROUTE.format(project_id=project.id), headers=auth_headers(user))

    assert resp.status_code == 200
    assert resp.json()["state"] == "unknown"


def test_every_compile_state_is_representable_on_the_wire() -> None:
    """The response is the enum's own value, so the four states cross the boundary unchanged —
    including `unknown`, which a caller must be able to receive in order to hold its cover."""
    from src.api.v1.build_sessions.schemas import CompileStateResponse

    for state in CompileState:
        assert CompileStateResponse(state=state).model_dump()["state"] == state.value


def test_a_report_of_any_state_is_carried_through_unchanged() -> None:
    """Guards the mapping the route performs: it returns the container's verdict, and does not
    re-derive, default, or 'helpfully' upgrade it."""
    for state in CompileState:
        assert CompileReport(state=state).state is state
