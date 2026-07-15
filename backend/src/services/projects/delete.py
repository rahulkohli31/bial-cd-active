"""Rollback-safe project cascade delete (U6, KD-3 / KD-3a).

Deleting a project must take its ONE app (its code + files) and ALL its conversations
(every kind, plus their attachments) with it — through the real per-child cleanup, not
a bare DB `ON DELETE CASCADE`, which would orphan object-store blobs (a DB cascade never
reaches the store, KD-3a). This service does the deletes the rollback-safe way:

  1. Enumerate the project's children **owner-scoped** (`WHERE project_id = … AND
     user_id = …`) — that enumeration IS the ownership boundary, because the app-purge
     cores are keyed by id with no `user_id` predicate (KD-3).
  2. GATHER every object-store key to sweep (app-file blobs + conversation attachment
     blobs + deck-PDF siblings) while the rows still resolve them.
  3. DELETE all rows (apps, conversations, the project) INSIDE the caller's transaction.
  4. Return the gathered keys. The caller commits, then best-effort sweeps the blobs.

Because blobs are swept only AFTER the caller commits, a mid-cascade DB error rolls the
whole thing back without having destroyed a single blob a restored row still points at
(the rollback-safety guarantee). We deliberately do NOT call `nuke_app` here: it sweeps
blobs INLINE before dropping the app row, which is the exact ordering KD-3 forbids — so
we replicate its two halves (gather `AppFile.blob_key`, then delete the row) with the
commit boundary in between.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models.app_file import AppFile
from src.db.models.app_registry import AppRegistry
from src.db.models.conversation import Conversation
from src.db.models.project import Project
from src.services.conversations import gather_and_delete_conversations
from src.services.storage import snapshot_key


async def delete_project_cascade(
    db: AsyncSession, project: Project, *, user_id: uuid.UUID
) -> list[str]:
    """Delete a project and every child it owns INSIDE the caller's transaction, returning
    the object-store keys to sweep AFTER the caller commits (KD-3). Commit-less and
    owner-scoped by `user_id` — enumeration by `(project_id, user_id)` is the ownership
    boundary (the app-purge cores carry no `user_id` predicate)."""
    blob_keys: list[str] = []

    # Apps (one per project today, but enumerate defensively). Gather each app's file
    # blobs BEFORE dropping the row, then delete the row (app_files / data_records /
    # clear_data_tokens cascade at the DB level; only the object-store blobs need sweeping).
    app_ids = (
        (
            await db.execute(
                sa.select(AppRegistry.id).where(
                    AppRegistry.project_id == project.id, AppRegistry.user_id == user_id
                )
            )
        )
        .scalars()
        .all()
    )
    for app_id in app_ids:
        keys = (
            (await db.execute(sa.select(AppFile.blob_key).where(AppFile.app_id == app_id)))
            .scalars()
            .all()
        )
        blob_keys.extend(keys)
        # The app's C4 snapshot bundle lives outside app_files — sweep its blob too.
        blob_keys.append(snapshot_key(app_id))
        await db.execute(
            sa.delete(AppRegistry).where(AppRegistry.id == app_id, AppRegistry.user_id == user_id)
        )

    # Conversations (all kinds), batched: one messages SELECT + one attachments SELECT across
    # the whole project (not a per-conversation N+1). The purge deletes the conversation rows +
    # their messages (DB cascade) + attachment rows, and hands back the attachment/deck-PDF blob
    # keys — still gathered before any delete, so the caller's post-commit sweep stays safe.
    conversation_ids = (
        (
            await db.execute(
                sa.select(Conversation.id).where(
                    Conversation.project_id == project.id, Conversation.user_id == user_id
                )
            )
        )
        .scalars()
        .all()
    )
    blob_keys.extend(await gather_and_delete_conversations(db, conversation_ids, user_id=user_id))

    # Finally the container row itself (children are already gone, so nothing cascades).
    await db.execute(
        sa.delete(Project).where(Project.id == project.id, Project.user_id == user_id)
    )
    return blob_keys
