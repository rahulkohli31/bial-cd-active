"""Rollback-safe project cascade delete.

WHY THIS EXISTS

Deleting a project must take its ONE app (code + files) and ALL its conversations (every
kind, plus their attachments) with it through the real per-child cleanup, not a bare DB
`ON DELETE CASCADE` — a DB cascade never reaches the object store, so it would orphan every
blob. The ordering here is the whole point:

  1. Enumerate the project's children **owner-scoped** (`WHERE project_id = … AND
     user_id = …`) — that enumeration IS the ownership boundary, because the app-purge
     cores are keyed by id with no `user_id` predicate.
  2. GATHER every object-store key to sweep (each app's snapshot bundle + conversation
     attachment blobs) while the rows still resolve them.
  3. DELETE all rows (apps, conversations, the project) INSIDE the caller's transaction.
  4. Return the gathered keys; the caller commits, re-enumerates each app's
     `submissions/{app_id}/` prefix (`resweep_submission_prefixes`), and sweeps the union.

Blobs are swept only AFTER the caller commits, so a mid-cascade DB error rolls back without
having destroyed a blob a restored row still points at. `nuke_app` is deliberately NOT used:
it sweeps blobs INLINE before dropping the app row, the exact ordering this service avoids.

Step 4's re-enumeration exists because the step-2 gather necessarily runs BEFORE the authorizing
commit: a submission bundle written into the prefix in between would be swept by nothing and
reachable by no query, its app row gone. The post-commit re-walk makes the sweep list reflect the
store as it is at sweep time, and it lives in the caller because the commit boundary does — this
service is commit-less by contract."""

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
from src.services.deploy.registry_delete import app_ids_that_could_have_an_image
from src.services.storage import (
    ObjectStorage,
    all_keys_under,
    recovery_key,
    snapshot_key,
    submissions_prefix,
)

_log = structlog.get_logger()


@dataclass(frozen=True)
class ProjectCascadeCleanup:
    """The object-store cleanup a committed project-delete sweeps post-commit. Two DIFFERENT
    stores, both swept: `blob_keys` are keys in the platform ObjectStorage — legacy per-app file
    blobs (`apps/{app_id}/…`), each app's snapshot bundle, and conversation-attachment blobs —
    swept via `sweep_blobs`; `app_container_ids` are the app ids whose per-app Blob CONTAINER
    (`app-{app_id}`) is deleted WHOLESALE via `sweep_app_containers`. Frozen, mirroring the
    `ObjectMeta`/`ListPage` value-type idiom."""

    blob_keys: list[str]
    app_container_ids: list[uuid.UUID]
    # THE SUBSET THAT COULD HAVE AN IMAGE IN THE CONTAINER REGISTRY — the apps with a deployment
    # row. Captured here rather than derived by the caller because `deployments` cascades with
    # the app, so after the commit the answer is empty for everything. See
    # `deploy/registry_delete.app_ids_that_could_have_an_image` for why the sweep is narrowed
    # at all when a delete of an absent repository already succeeds.
    built_app_ids: list[uuid.UUID]


async def delete_project_cascade(
    db: AsyncSession, project: Project, storage: ObjectStorage, *, user_id: uuid.UUID
) -> ProjectCascadeCleanup:
    """Delete a project and every child it owns inside the caller's transaction; return the
    object-store cleanup to sweep after the caller commits. Commit-less and owner-scoped by
    `user_id` — enumeration by `(project_id, user_id)` IS the ownership boundary, because the
    app-purge cores carry no `user_id` predicate. `storage` only enumerates each app's
    submission-bundle prefix (a paginated walk) — never deletes; a `StorageError` there RAISES so
    the whole delete rolls back with nothing destroyed and the caller retries, where swallowing it
    would commit the row deletes and strand citizen source — possibly holding a secret — in the
    store forever.

    RESIDUAL WINDOW — surfaced, not closed. The caller's post-commit re-walk narrows the race to
    writes landing after it; it does not eliminate it. `submit` puts its bundle BEFORE the guarded
    UPDATE that authorizes it (`api/v1/apps/router.py`) and this delete takes no submit interlock,
    so a bundle written after the re-walk sits under its prefix with no owning row. Nothing
    reclaims it automatically: the reconciling sweep is REPORT-ONLY on that prefix until the
    retention policy (D7) is decided, so it reaches an operator's report and an operator reclaims
    it — the same bounded, reported leak `apps/router.py` already books when its guarded UPDATE
    refuses after the copy lands, not a new class of one."""
    blob_keys: list[str] = []

    # Apps (one per project today, but enumerate defensively). Gather each app's snapshot
    # bundle key + every immutable submission bundle under its prefix BEFORE dropping the row,
    # then delete the row (the app's own children cascade at the DB level; only the
    # object-store blobs + the per-app container need sweeping here). The project's OWN
    # PostgreSQL database is a third thing entirely: `DROP DATABASE` cannot run inside a
    # transaction, so the caller reads its handles before this call and salts the earth
    # after committing.
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
    # BEFORE the rows go: `deployments` cascades with the app, so this question has to be asked
    # while there is still something to ask it about.
    built_app_ids = await app_ids_that_could_have_an_image(db, app_ids)
    for app_id in app_ids:
        # The app's snapshot bundle lives in the platform store — sweep its blob.
        blob_keys.append(snapshot_key(app_id))
        # ...and its crash-recovery twin. Both hold the app's full source tree, so missing this
        # one leaves a deleted project's entire codebase in Blob forever: no owning row, nothing
        # that lists it, and invisible to the operator reconciler unless `recovery/` is one of
        # its known roots. Deleting a project must not leave the code behind.
        blob_keys.append(recovery_key(app_id))
        # Every retained submission bundle — a paginated walk, so a prefix past
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
    # The app ids double as the per-app Blob CONTAINER ids to delete wholesale — a
    # different store than `blob_keys` (the platform store); both swept post-commit.
    # They are plain UUID values (a `select(AppRegistry.id)`, not an ORM attribute), so the
    # caller may read them AFTER its commit without tripping `expire_on_commit` lazy I/O
    # — which is exactly what `resweep_submission_prefixes` needs them for.
    return ProjectCascadeCleanup(
        blob_keys=blob_keys, app_container_ids=list(app_ids), built_app_ids=built_app_ids
    )


async def resweep_submission_prefixes(
    storage: ObjectStorage, app_ids: list[uuid.UUID]
) -> list[str]:
    """Re-enumerate every deleted app's `submissions/{app_id}/` prefix AFTER the caller's
    commit, returning the keys to fold into the post-commit sweep.

    The cascade's own gather runs pre-commit and so cannot see a bundle written between that
    walk and the commit; this second walk does. See `delete_project_cascade` for the residual
    window this does NOT close."""
    # This lives in the CALLER, not in `delete_project_cascade`, because the commit boundary
    # is the caller's (`delete_project`) and the cascade is commit-less by contract.
    #
    # Best-effort, the exact opposite posture to the pre-commit gather: the rows are already
    # committed-deleted, so a raised `StorageError` here would 500 a delete that in fact
    # succeeded. Every failure is logged and the remaining prefixes are still walked; the
    # pre-commit list the caller already holds is swept regardless. The guard is deliberately
    # broad, mirroring `sweep_blobs` — transport-level errors escape the `StorageError` hierarchy.
    keys: list[str] = []
    for app_id in app_ids:
        try:
            keys.extend(await all_keys_under(storage, submissions_prefix(app_id)))
        except Exception:  # noqa: BLE001 — post-commit best-effort: log, never surface
            _log.warning("post_commit_submission_resweep_failed", app_id=str(app_id))
    return keys
