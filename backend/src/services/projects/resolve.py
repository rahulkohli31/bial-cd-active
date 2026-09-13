"""Project access resolution — owner-scoped, and (as of #198) the one place a share widens it.

Project-first is the product model: every app and conversation belongs to exactly
one project the caller owns, so `project_id` is REQUIRED at every create seam —
there is no fallback project. A missing and a cross-user id are the same
non-leaking 404. Shared by the projects CRUD router,
app `provision`, and conversation `append_message`'s create branch.

`resolve_project_access` (#198 R11) IS THE ONLY PLACE A USER-SCOPE PREDICATE MAY BE WIDENED
beyond strict ownership — the platform's first legitimate exception to ADR-0004's "every query
is scoped by a single user_id" rule. `owned_project_or_404` stays the binary owner-or-404 every
existing caller already gets — it is now a one-line wrapper around the tri-state resolver below,
so none of its ~20 existing call sites (build sessions, classification, deploy, conversations,
turns — every one of them a MUTATING action) changed behaviour by one bit. Only a caller that
explicitly wants to admit a shared viewer calls `resolve_project_access` directly, and as of
this slice that is `get_project` alone — a read. Nothing here relaxes a mutation; requirement 6
("Can use", never edit) is enforced by simply never widening a write path's own
`owned_project_or_404` call.
"""

from __future__ import annotations

import enum
import uuid
from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.errors import AppApiError
from src.db.models.project import Project
from src.db.models.project_share import ProjectShare


class ProjectAccess(enum.Enum):
    """OWNER takes precedence over SHARED by construction: `resolve_project_access` checks
    ownership FIRST and only asks about a share when that fails, so a builder can never be
    handed the restricted recipient view of their own project (#198 R11)."""

    OWNER = "owner"
    SHARED = "shared"


@dataclass(frozen=True)
class ResolvedProject:
    project: Project
    access: ProjectAccess


async def resolve_project_access(
    db: AsyncSession, user_id: uuid.UUID, project_id: uuid.UUID
) -> ResolvedProject:
    """Load a project the caller may see — as its owner, or as a share recipient — or fail
    closed with a non-leaking 404 (a project that exists but is neither owned nor shared reads
    identically to one that does not exist at all, same as `owned_project_or_404` always has).
    """
    project = await db.get(Project, project_id)
    if project is None:
        raise AppApiError(404, "Project not found.")
    if project.user_id == user_id:
        return ResolvedProject(project=project, access=ProjectAccess.OWNER)
    is_shared = await db.scalar(
        sa.select(
            sa.exists().where(
                ProjectShare.project_id == project_id,
                ProjectShare.shared_with_user_id == user_id,
            )
        )
    )
    if is_shared:
        return ResolvedProject(project=project, access=ProjectAccess.SHARED)
    raise AppApiError(404, "Project not found.")


async def owned_project_or_404(
    db: AsyncSession, user_id: uuid.UUID, project_id: uuid.UUID
) -> Project:
    """Load a project scoped to its owner, or fail closed with a non-leaking 404. A share never
    satisfies this — every existing caller of this function is a mutating action (build a
    session, submit for review, deploy, append a turn, patch, delete), and #198 grants a
    recipient "Can use", never a change to the project (R6)."""
    resolved = await resolve_project_access(db, user_id, project_id)
    if resolved.access is not ProjectAccess.OWNER:
        raise AppApiError(404, "Project not found.")
    return resolved.project
