"""Owner-scoped project resolution (R2, ADR-0004).

Project-first is the product model: every app and conversation belongs to exactly
one project the caller owns, so `project_id` is REQUIRED at every create seam —
there is no fallback project. A missing and a cross-user id are the same
non-leaking 404 (indistinguishable, ADR-0004). Shared by the projects CRUD router,
app `provision`, and conversation `append_message`'s create branch.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from src.core.errors import AppApiError
from src.db.models.project import Project


async def owned_project_or_404(
    db: AsyncSession, user_id: uuid.UUID, project_id: uuid.UUID
) -> Project:
    """Load a project scoped to its owner, or fail closed with a non-leaking 404."""
    project = await db.get(Project, project_id)
    if project is None or project.user_id != user_id:
        raise AppApiError(404, "Project not found.")
    return project
