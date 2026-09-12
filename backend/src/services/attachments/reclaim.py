"""Reclaim never-sent attachment uploads so they stop consuming the owner's quota.

WHY THIS EXISTS
An upload is reachable only by a client-minted token buried in a message's payload JSONB, so
a file uploaded and never sent has no delete path and consumes the owner's 50 MB quota forever.
This service closes that gap: it deletes one user's orphaned uploads and their object-store
blobs, with its own service-layer test, and runs in prod via the operator reconcile sweep once
per owning user (`admin/router.py::_reclaim_orphans_for_all_users`) — so the leak is actually
reclaimed, not merely reclaimable.

Eligibility, exactly (both required): (a) referenced by NO sent message — the
`_referenced_attachment_ids` scan over `Message.payload`, REUSED from the conversation cascade
so the two can never drift; and (b) older than `NEVER_SENT_RECLAIM_WINDOW`, so a file attached
seconds ago and still mid-composition is spared. A NULL `conversation_id` is NEVER itself an
eligibility signal — it means *legacy*, not *never-sent*, and every legacy row is already past
the window, so (a) alone still decides it.

Every query here is scoped by `user_id`: `attachment_id` is a client-minted token unique only
per owner, so an unscoped reference scan would let a colliding token in another user's message
shield this user's orphan from reclamation — the exact cross-user leak the scoping closes.
"""

from __future__ import annotations

import datetime
import uuid
from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models.attachment import Attachment
from src.db.models.message import Message

# REUSED, not reimplemented (drift guard): the reclaimer resolves sent references and derives blob
# keys through the SAME helpers the conversation cascade uses, so a payload shape the cascade
# honours is honoured here too.
from src.services.conversations.delete import _blob_keys_for, _referenced_attachment_ids
from src.services.storage import ObjectStorage, sweep_blobs

# A window that errs long. Too short deletes a file the user is still composing;
# too long costs a few hours of stored bytes. 48h is a large multiple of any realistic
# upload-then-send interval, and consistent with the reconciler's long-erring grace.
NEVER_SENT_RECLAIM_WINDOW = datetime.timedelta(hours=48)


@dataclass(frozen=True)
class AttachmentReclaimResult:
    """What one reclamation pass over a user did. Counts only — no key list: a report
    never leaks the internal object layout."""

    reclaimed: int  # orphan rows deleted
    freed_bytes: int  # SUM(size) reclaimed — the quota this pass gave back
    swept_keys: int  # object keys swept, one per attachment


async def reclaim_orphaned_attachments(
    db: AsyncSession,
    storage: ObjectStorage,
    *,
    user_id: uuid.UUID,
    now: datetime.datetime | None = None,
) -> AttachmentReclaimResult:
    """Delete a user's orphaned uploads (no sent message references them, and they are past the
    window) and best-effort sweep their blobs; return the counts.

    Commit + sweep are owned here (the whole operation, not a step in a larger cascade).
    Rollback-safe: blob keys and freed bytes are captured BEFORE the delete/commit —
    `expire_on_commit` would make a post-commit read raise `MissingGreenlet` — then blobs are
    swept AFTER the commit, so a rolled-back delete never destroys a blob a restored row still
    points at. `now` is injectable for tests."""
    cutoff = (now or datetime.datetime.now(datetime.UTC)) - NEVER_SENT_RECLAIM_WINDOW

    # (a) every attachmentId this user has referenced in ANY of their sent messages (native
    # payload ref markers). Scoped by `user_id` on this half too — a colliding token in
    # another user's message must not count.
    payload_rows = (
        (await db.execute(sa.select(Message.payload).where(Message.user_id == user_id)))
        .scalars()
        .all()
    )
    referenced_ids = _referenced_attachment_ids(payload_rows)

    # Candidate set = this user's uploads past the window (b). The reference scan (a) below
    # decides which are orphaned; age alone never deletes a file some message still references.
    candidates = (
        (
            await db.execute(
                sa.select(Attachment).where(
                    Attachment.user_id == user_id,
                    Attachment.created_at < cutoff,
                )
            )
        )
        .scalars()
        .all()
    )
    orphans = [att for att in candidates if att.attachment_id not in referenced_ids]
    if not orphans:
        return AttachmentReclaimResult(reclaimed=0, freed_bytes=0, swept_keys=0)

    # Capture everything read post-commit BEFORE the delete/commit.
    blob_keys = _blob_keys_for(orphans)
    freed_bytes = sum(att.size for att in orphans)
    reclaimed = len(orphans)
    for att in orphans:
        await db.delete(att)
    await db.commit()

    await sweep_blobs(storage, blob_keys)
    return AttachmentReclaimResult(
        reclaimed=reclaimed, freed_bytes=freed_bytes, swept_keys=len(blob_keys)
    )
