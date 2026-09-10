"""The connector half of a build session's injected environment — and the gate on it.

One small function per capability, each returning `{}` when its substrate is unconfigured and
raising only when a *configured* substrate genuinely fails: the shape `appstorage.py` and
`appdb_env.py` already keep, and the reason a developer machine with no lake still builds apps.

THIS IS THE SECURITY BOUNDARY OF THE WHOLE DATA-PLANE PASS. Three conditions, all of them
required: a lake is configured, the connector is switched on for THIS project, and the project
owner's access has been approved by an administrator. The answer decides both the coordinates and
the managed identity — see `services/lake/env.py::identity_resource_id_for_env` for why those
cannot be separated.

THE ON-NESS CONJUNCTION IS READ OFF `resolve_window`, NEVER SPELLED AGAIN HERE. `effectively_on`
is the switch AND the approval, and it is the same value the rail shows the citizen. A second
place that decides whether a connector reads is a second place that can disagree with what the
citizen was told.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.connectors import CONNECTORS, resolve_window
from src.db.models.project import Project
from src.db.models.project_connector import ProjectConnector
from src.services.connectors.access import current_access
from src.services.lake.env import lake_env_for


async def build_connector_env(
    db: AsyncSession, *, user_id: uuid.UUID, project_id: uuid.UUID
) -> dict[str, str]:
    """Every approved, switched-on connector's coordinates for this project. `{}` when there are
    none — which is the ordinary case and is never an error.

    Scoped by the owning `user_id` through a join on `projects` in the SAME `WHERE` clause:
    `project_connectors` carries no user column of its own, so `projects` is its ownership anchor
    and the predicate IS the isolation boundary (ADR-0004). A dropped `user_id` here would hand
    one citizen's build the coordinates another citizen's project was granted.

    Nothing is caught. A database failure propagates and fails the start, exactly as
    `provision_app_database` lets a configured substrate's failure propagate — "off" and "broken"
    must not arrive at the caller looking the same. There is no Azure call on this path: it reads
    settings and one row, so a configured lake has nothing else to fail at.
    """
    stored_rows = await db.scalars(
        sa.select(ProjectConnector)
        .join(Project, Project.id == ProjectConnector.project_id)
        .where(
            ProjectConnector.project_id == project_id,
            Project.user_id == user_id,
        )
    )
    rows = {row.connector_key: row for row in stored_rows}

    env: dict[str, str] = {}
    for connector_key, connector in CONNECTORS.items():
        access = await current_access(db, user_id=user_id, connector_key=connector_key)
        # `resolve_window` returns None for a project this connector was never switched on in —
        # a different fact from `enabled = false`, and both mean the same thing here.
        window = resolve_window(connector, rows.get(connector_key), access.request_status)
        if window is None or not window.effectively_on:
            continue
        env |= lake_env_for(connector_key)
    return env
