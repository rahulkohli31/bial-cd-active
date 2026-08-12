"""Owner-scoped lookups the deploy routes need.

Two facts, one query, and both are read WITHOUT minting anything.
`resolve_app_for_project` — the build path's resolver — UPSERTS a draft app row, which is
correct when a build is about to start and wrong here: a Deploy on a project with no app
must answer "there is nothing to deploy", not quietly create one and then fail on the
missing snapshot.

`user_id` is in the predicate rather than checked afterwards. A dropped ownership predicate
is a cross-user leak, and the shape that never drops it is the one where it lives in the
WHERE clause.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models.app_registry import AppRegistry


@dataclass(frozen=True)
class DeployTarget:
    """What a deploy needs to know about the app before it starts."""

    app_id: uuid.UUID
    # The thread the outcome is written to. A SOFT pointer at the last builder session, so
    # it can legitimately be absent — an app built through an API-only path has no thread,
    # and that is a deploy with no chat message, not a failed deploy.
    conversation_id: uuid.UUID | None


async def deploy_target(
    db: AsyncSession, *, user_id: uuid.UUID, project_id: uuid.UUID
) -> DeployTarget | None:
    """The project's app and its conversation, or `None` when it has no app yet."""
    row = (
        await db.execute(
            sa.select(AppRegistry.id, AppRegistry.conversation_id).where(
                AppRegistry.project_id == project_id,
                AppRegistry.user_id == user_id,
            )
        )
    ).one_or_none()
    if row is None:
        return None
    return DeployTarget(app_id=row.id, conversation_id=row.conversation_id)
