"""Rollback-safe project cascade delete (U6, KD-3 / KD-3a).

Deleting a project must take its ONE app (its code + files) and ALL its conversations
(every kind, plus their attachments) with it — through the real per-child cleanup, not
a bare DB `ON DELETE CASCADE`, which would orphan object-store blobs (a DB cascade never
reaches the store, KD-3a). This service does the deletes the rollback-safe way:

  1. Enumerate the project's children **owner-scoped** (`WHERE project_id = … AND
     user_id = …`) — that enumeration IS the ownership boundary, because the app-purge
     cores are keyed by id with no `user_id` predicate (KD-3).
  2. GATHER every object-store key to sweep (each app's C4 snapshot bundle + conversation
     attachment blobs + deck-PDF siblings) while the rows still resolve them.
  3. DELETE all rows (apps, conversations, the project) INSIDE the caller's transaction.
  4. Return the gathered keys. The caller commits, then RE-ENUMERATES each app's
     `submissions/{app_id}/` prefix (`resweep_submission_prefixes`) and best-effort sweeps
     the union of both lists.

Because blobs are swept only AFTER the caller commits, a mid-cascade DB error rolls the
whole thing back without having destroyed a single blob a restored row still points at
(the rollback-safety guarantee). We deliberately do NOT call `nuke_app` here: it sweeps
blobs INLINE before dropping the app row, which is the exact ordering KD-3 forbids — so
we gather the app's snapshot key, then delete the row, with the commit boundary in between.

Step 4's re-enumeration is the R8/R12 fix: the step-2 gather necessarily runs BEFORE the
authorizing commit, so a submission bundle written into the prefix in between would be swept
by nothing and reachable by no query (its app row is gone). The post-commit re-walk makes the
sweep list reflect the store as it is at sweep time. It lives in the caller, not here, because
the commit boundary does — this service is commit-less by contract.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass

import sqlalchemy as sa
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models.app_registry import AppRegistry
from src.db.models.conversation import Conversation
from src.db.models.project import Project
from src.services.conversations import gather_and_delete_conversations
from src.services.storage import ObjectStorage, all_keys_under, snapshot_key, submissions_prefix

_log = structlog.get_logger()


@dataclass(frozen=True)
class ProjectCascadeCleanup:
    """The object-store cleanup a committed project-delete sweeps post-commit. Two DIFFERENT stores
    (both handled, KTD-7): `blob_keys` are keys in the platform ObjectStorage — legacy per-app file
    blobs (`apps/{app_id}/…`), each app's C4 snapshot bundle, and conversation-attachment blobs —
    swept via `sweep_blobs`; `app_container_ids` are the app ids whose per-app Blob CONTAINER
    (`app-{app_id}`, C9 §6) is deleted WHOLESALE via `sweep_app_containers`. Frozen, mirroring the
    `ObjectMeta`/`ListPage` value-type idiom."""

    blob_keys: list[str]
    app_container_ids: list[uuid.UUID]


async def delete_project_cascade(
    db: AsyncSession, project: Project, storage: ObjectStorage, *, user_id: uuid.UUID
) -> ProjectCascadeCleanup:
    """Delete a project and every child it owns INSIDE the caller's transaction, returning the
    object-store cleanup to sweep AFTER the caller commits (KD-3). Commit-less and owner-scoped by
    `user_id` — enumeration by `(project_id, user_id)` is the ownership boundary (the app-purge
    cores carry no `user_id` predicate).

    `storage` is used ONLY to ENUMERATE each app's `submissions/{app_id}/` prefix (a paginated
    walk, R23) — never to delete; the sweep stays the caller's post-commit job. A `StorageError`
    during that gather deliberately RAISES so the whole delete rolls back with nothing destroyed
    and the caller retries — swallowing it would commit the row deletes and silently strand
    citizen source (possibly holding a secret) in the store forever.

    This gather runs BEFORE the caller's commit, so it cannot see a bundle written after it.
    The caller closes most of that gap by re-walking the same prefixes AFTER the commit
    (`resweep_submission_prefixes`) and sweeping the union.

    RESIDUAL WINDOW — surfaced, not closed. The re-walk shrinks the race to writes landing
    after it; it does not eliminate it. `submit` puts its bundle BEFORE the guarded UPDATE
    that authorizes it (`api/v1/apps/router.py`) and this delete takes no submit interlock, so
    a bundle written after the re-walk stays under `submissions/{app_id}/` with no row. Nothing
    reclaims it automatically: the reconciling sweep is REPORT-ONLY on that prefix until the
    submission-bundle retention policy (D7) is decided, so the bundle shows up in an operator's
    report and an operator reclaims it. Closure waits on D7. This is the same deliberately
    accepted orphan `apps/router.py` already books when its guarded UPDATE refuses after the
    copy lands — a bounded, reported leak, not a new class of one."""
    blob_keys: list[str] = []

    # Apps (one per project today, but enumerate defensively). Gather each app's C4 snapshot
    # bundle key + every immutable submission bundle under its prefix BEFORE dropping the row,
    # then delete the row (the app's own children cascade at the DB level; only the
    # object-store blobs + the per-app container need sweeping here). The project's OWN
    # PostgreSQL database is a third thing entirely: `DROP DATABASE` cannot run inside a
    # transaction, so the caller reads its handles before this call and salts the earth
    # after committing (ADR-0028).
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
        # The app's C4 snapshot bundle lives in the platform store — sweep its blob.
        blob_keys.append(snapshot_key(app_id))
        # Every retained submission bundle (R23) — a paginated walk, so a prefix past
        # DEFAULT_PAGE_SIZE is fully gathered, and a StorageError raises (see docstring).
        blob_keys.extend(await all_keys_under(storage, submissions_prefix(app_id)))
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
    # The app ids double as the per-app Blob CONTAINER ids to delete wholesale (C9 §6) — a
    # different store than `blob_keys` (the platform store); both swept post-commit (KTD-7).
    # They are plain UUID values (a `select(AppRegistry.id)`, not an ORM attribute), so the
    # caller may read them AFTER its commit without tripping `expire_on_commit` lazy I/O
    # (KD-8) — which is exactly what `resweep_submission_prefixes` needs them for.
    return ProjectCascadeCleanup(blob_keys=blob_keys, app_container_ids=list(app_ids))


async def resweep_submission_prefixes(
    storage: ObjectStorage, app_ids: list[uuid.UUID]
) -> list[str]:
    """Re-enumerate every deleted app's `submissions/{app_id}/` prefix AFTER the caller's
    commit, returning the keys to fold into the post-commit sweep (R8/R12).

    The cascade's own gather runs pre-commit and so cannot see a bundle written between that
    walk and the commit; this second walk does. It is the CALLER's job — and lives outside
    `delete_project_cascade` — because the commit boundary is the caller's (`delete_project`),
    and the cascade is commit-less by contract.

    Best-effort, the exact opposite posture to the pre-commit gather: the rows are already
    committed-deleted, so a raised `StorageError` here would 500 a delete that in fact
    succeeded. Every failure is logged and the remaining prefixes are still walked; the
    pre-commit list the caller already holds is swept regardless. Mirrors `sweep_blobs`'
    deliberately broad guard — transport-level errors escape the `StorageError` hierarchy.

    See `delete_project_cascade` for the residual window this does NOT close."""
    keys: list[str] = []
    for app_id in app_ids:
        try:
            keys.extend(await all_keys_under(storage, submissions_prefix(app_id)))
        except Exception:  # noqa: BLE001 — post-commit best-effort: log, never surface
            _log.warning("post_commit_submission_resweep_failed", app_id=str(app_id))
    return keys
