"""Which connectors one project may actually read, resolved once for a turn."""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.connectors import CONNECTORS, ConnectedSystem, resolve_window
from src.db.models.project import Project
from src.db.models.project_connector import ProjectConnector


async def connected_systems_for_project(
    db: AsyncSession, *, user_id: uuid.UUID, project_id: uuid.UUID
) -> tuple[ConnectedSystem, ...]:
    """Every connector this project may ACTUALLY read, resolved once for a turn. `()` is the
    ordinary case and is never an error.

    ONE FACT, RESOLVED ONCE, TWO CONSUMERS. The turn's prompt names what is connected and the
    turn's tool surface registers the schema tool for the same set (`services/agent/toolsets.py`),
    both off this one return value. A second resolution is a second answer waiting to disagree
    with the first, and the disagreement that matters is the one where the prompt says a system is
    connected and the tool to read it was never registered.

    WHETHER A CONNECTOR READS IS READ OFF `resolve_window`, NEVER DECIDED AGAIN HERE — the same
    value the settings row shows the citizen and the same one `build_connector_env` gates the data
    plane on.

    SCOPED BY THE OWNING `user_id` THROUGH A JOIN ON `projects`, in the same WHERE clause.
    `project_connectors` carries no user column of its own, so `projects` is its ownership anchor
    and the predicate IS the isolation boundary (ADR-0004). The routers have already checked
    ownership upstream and that changes nothing: a predicate that is only correct while a
    caller's earlier check stays true is not a boundary, it is a coincidence.

    Nothing is caught. A database failure propagates and fails the turn rather than arriving at
    the prompt looking like "no connectors", which is what "off" and "broken" must never share.
    """
    stored = {
        row.connector_key: row
        for row in await db.scalars(
            sa.select(ProjectConnector)
            .join(Project, Project.id == ProjectConnector.project_id)
            .where(
                ProjectConnector.project_id == project_id,
                Project.user_id == user_id,
            )
        )
    }

    systems: list[ConnectedSystem] = []
    for key, connector in CONNECTORS.items():
        # `None` means this connector was never switched on for this project — a different fact
        # from `enabled = false`, and the same answer here.
        window = resolve_window(connector, stored.get(key))
        if window is None or not window.effectively_on:
            continue
        systems.append(ConnectedSystem(key=key, connector=connector, window=window))
    return tuple(systems)
