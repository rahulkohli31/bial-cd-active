"""Removing a conversation nobody has added to in a while — the policy's half, set-based.

IT DOES NOT REUSE THE INTERACTIVE DELETE. `delete.py` answers "this person asked to delete this
chat": it is scoped to one owner because an attachment's client-minted token is unique only per
owner, and it discovers attachments by scanning what the stored messages REFERENCE. This answers
"a policy condemns these chats, whoever they belong to" — every owner in one set of statements,
and attachments reached by `attachments.conversation_id`, the foreign key the upload door stamps
whether or not the file is ever sent. So this finds a file uploaded to a chat and never sent,
which a payload scan structurally cannot see, and it needs no owner predicate to do it.

THE ORDER IS DELETE, COMMIT, SWEEP: rows deleted inside the caller's transaction, the caller
commits, then blobs swept best-effort. Inverting it destroys blobs a rolled-back delete would
have kept.
"""

from __future__ import annotations

import datetime as dt
import uuid
from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models.attachment import Attachment
from src.db.models.conversation import Conversation


@dataclass(frozen=True)
class RetentionSweep:
    """What one pass removed: the conversations that are gone, and the blobs they left behind."""

    conversation_ids: tuple[uuid.UUID, ...] = ()
    blob_keys: tuple[str, ...] = ()

    @property
    def removed(self) -> int:
        return len(self.conversation_ids)


def idle_before(now: dt.datetime, *, window: dt.timedelta) -> dt.datetime:
    """The cutoff a conversation must be older than to be condemned.

    ITS OWN PREDICATE, shared with nothing that shows a citizen anything. A display may not forgive
    what this forgives: being a day out on a list is a cosmetic wrong, and being a day out here
    deletes somebody's conversation.
    """
    return now - window


async def condemned_conversations(
    db: AsyncSession, *, cutoff: dt.datetime, limit: int
) -> tuple[tuple[uuid.UUID, ...], int]:
    """The conversation ids idle past `cutoff`, oldest first and capped, plus how many remain.

    EVERY KIND AND EVERY OWNER. A generic chat, a plan chat and a build chat are all conversations
    nobody has added to, and the policy is about the conversation rather than about what it was
    for.

    `updated_at` IS MAINTAINED BY A TRIGGER on message insert as well as by the header edits, so it
    reads as "last touched by a person" — a conversation that never received a message keeps its
    creation time, which is the fallback this query wants rather than a gap in it.
    """
    idle = sa.select(Conversation.id).where(Conversation.updated_at < cutoff)
    remaining = (await db.scalar(sa.select(sa.func.count()).select_from(idle.subquery()))) or 0
    rows = (await db.scalars(idle.order_by(Conversation.updated_at.asc()).limit(limit))).all()
    return tuple(rows), max(remaining - len(rows), 0)


async def gather_and_delete(
    db: AsyncSession, *, conversation_ids: tuple[uuid.UUID, ...], cutoff: dt.datetime
) -> RetentionSweep:
    """Delete the condemned rows inside the caller's transaction; return the keys to sweep.

    THE VERDICT IS RE-TAKEN HERE AND HELD UNDER A ROW LOCK, which is what makes both deletes safe:
    at READ COMMITTED a citizen who sends a message between the verdict and the deletes must keep
    their conversation, and locking — rather than re-testing inside each delete — is also what
    stops the attachment delete from stripping a spared conversation's rows.

    MESSAGES CASCADE in the database; attachment ROWS do not — their foreign key is
    `ON DELETE SET NULL`, which is right for the interactive path and would leave this one's rows
    behind holding blobs nothing points at. So they are deleted explicitly, first.
    """
    if not conversation_ids:
        return RetentionSweep()

    still_idle = sa.and_(Conversation.id.in_(conversation_ids), Conversation.updated_at < cutoff)
    doomed = (
        await db.scalars(sa.select(Conversation.id).where(still_idle).with_for_update())
    ).all()
    if not doomed:
        return RetentionSweep()

    keys = (
        await db.scalars(
            sa.delete(Attachment)
            .where(Attachment.conversation_id.in_(doomed))
            .returning(Attachment.storage_key)
        )
    ).all()
    removed = (
        await db.scalars(
            sa.delete(Conversation).where(Conversation.id.in_(doomed)).returning(Conversation.id)
        )
    ).all()
    return RetentionSweep(conversation_ids=tuple(removed), blob_keys=tuple(keys))
