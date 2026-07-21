"""Reclaim never-sent attachment uploads so they stop consuming the owner's quota (R10 / U9).

An upload is reachable only by a client-minted token buried in a message's `parts` JSONB, so
a file that is uploaded and never sent is referenced by no delete path and consumes the owner's
50 MB quota forever. This is the missing sweep: a user-scoped service that deletes a user's
ORPHANED uploads — and their object-store blobs — and is directly testable (its own
service-layer test, ADR-0010). It is now driven in prod by the operator reconcile sweep, once per
owning user (`admin/router.py::_reclaim_orphans_for_all_users`, U10), so the leak it fixes is
actually reclaimed and not merely reclaimable.

Eligibility, exactly (both required):
  (a) referenced by NO sent message — the `_referenced_attachment_ids` scan over `Message.parts`,
      REUSED from the conversation cascade so the reclaimer and the cascade can never drift; and
  (b) older than `NEVER_SENT_RECLAIM_WINDOW` — a file attached seconds ago and not yet sent is
      still needed (mid-composition), so age is half the rule.

A NULL `conversation_id` is NEVER on its own an eligibility signal: NULL means *legacy*, not
*never-sent*, and every legacy row is by definition past the window, so criterion (a) decides
it exactly as it decides a linked row. The column narrows the candidate set / backstops the row;
it does not answer (a).

BOTH halves are user-scoped (ADR-0004). `attachment_id` is a client-minted token unique only
per owner (`uq_attachments_owner_attachment`), so a reference scan across ALL users' messages
would let a colliding token in user B's message shield user A's orphan from reclamation — the
exact cross-user leak the scoping closes.
"""

from __future__ import annotations

import datetime
import uuid
from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models.attachment import Attachment
from src.db.models.message import Message

# REUSED, not reimplemented (drift guard): the reclaimer resolves sent references and derives
# blob keys (incl. each deck's `{key}.pdf` sibling) through the SAME helpers the conversation
# cascade uses, so a `parts` shape the cascade honours is honoured here too.
from src.services.conversations.delete import _blob_keys_for, _referenced_attachment_ids
from src.services.storage import ObjectStorage, sweep_blobs

# The err-long window (KD-7 posture). Too short deletes a file the user is still composing;
# too long costs a few hours of stored bytes. 48h is a large multiple of any realistic
# upload-then-send interval, and consistent with the reconciler's err-long grace.
NEVER_SENT_RECLAIM_WINDOW = datetime.timedelta(hours=48)


@dataclass(frozen=True)
class AttachmentReclaimResult:
    """What one reclamation pass over a user did. Counts only — no key list (R13 posture:
    a report never leaks the internal object layout)."""

    reclaimed: int  # orphan rows deleted
    freed_bytes: int  # SUM(size) reclaimed — the quota this pass gave back
    swept_keys: int  # object keys swept (each storage_key, plus a deck's `.pdf` sibling)


async def reclaim_orphaned_attachments(
    db: AsyncSession,
    storage: ObjectStorage,
    *,
    user_id: uuid.UUID,
    now: datetime.datetime | None = None,
) -> AttachmentReclaimResult:
    """Delete a user's orphaned uploads (referenced by no sent message AND past the window)
    and best-effort sweep their blobs; return the counts.

    Commit + sweep are owned here (this is the whole operation, not a step inside a larger
    cascade). Rollback-safe (KD-8): blob keys and freed bytes are captured BEFORE the delete
    and commit — `expire_on_commit` would make a post-commit attribute read raise
    `MissingGreenlet` — then the blobs are swept AFTER the commit, so a rolled-back delete
    never destroys a blob a restored row still points at. `now` is injectable for tests.
    """
    cutoff = (now or datetime.datetime.now(datetime.UTC)) - NEVER_SENT_RECLAIM_WINDOW

    # (a) every attachmentId this user has referenced in ANY of their sent messages. Scoped by
    # `user_id` on this half too — a colliding token in another user's message must not count.
    parts_rows = (
        (await db.execute(sa.select(Message.parts).where(Message.user_id == user_id)))
        .scalars()
        .all()
    )
    referenced_ids = _referenced_attachment_ids(parts_rows)

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

    # Capture everything read post-commit BEFORE the delete/commit (KD-8).
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
