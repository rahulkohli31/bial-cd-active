"""Resolve the project a create/provision should land in (U5, KD-2/KD-4).

Every app and conversation needs a project (R2). Two shared callers — app `provision`
and conversation `append_message`'s lazy-create branch — need the same rule:

  * An **explicit** `project_id` must be owned by the caller, else a non-leaking 404 (a
    cross-user or missing project is indistinguishable, ADR-0004).
  * A **missing** `project_id` lands in the caller's lazily-created **Default** project —
    the transitional regression guard that keeps already-shipped create callers working
    until the SPA starts sending `project_id` (KD-2: there is NO backfill, so the row is
    minted on demand the first time a caller needs it).

Commit-less: it flushes a newly-minted Default so its id is available, but the caller owns
the commit. Two concurrent no-`project_id` creates may each mint a Default (there is no
`(user_id, name)` uniqueness); both are valid homes, so this transitional race is benign.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.errors import AppApiError
from src.db.models.project import Project

# The transitional fallback home (Q3: a normal, renamable/deletable project, not a
# protected system row). Matched by name so a returning caller reuses the same one.
DEFAULT_PROJECT_NAME = "Default"


async def resolve_project_for_write(
    db: AsyncSession, user_id: uuid.UUID, project_id: uuid.UUID | None
) -> Project:
    """Return the caller-owned project a create/provision should attach to (see module doc)."""
    if project_id is not None:
        project = await db.get(Project, project_id)
        if project is None or project.user_id != user_id:
            raise AppApiError(404, "Project not found.")
        return project

    existing = await db.scalar(
        sa.select(Project)
        .where(Project.user_id == user_id, Project.name == DEFAULT_PROJECT_NAME)
        .order_by(Project.id.asc())
        .limit(1)
    )
    if existing is not None:
        return existing
    default = Project(user_id=user_id, name=DEFAULT_PROJECT_NAME)
    db.add(default)
    await db.flush()
    await db.refresh(default)
    return default
