"""Removing a conversation nobody has added to in a while — the policy's half, set-based.

IT DOES NOT REUSE THE INTERACTIVE DELETE, and the duplication is honest rather than an oversight.
`delete.py` answers "this person asked to delete this chat": it is scoped to one owner because an
attachment's client-minted token is unique only per owner, and it discovers attachments by scanning
what the stored messages REFERENCE. This answers "a policy condemns these chats, whoever they
belong to" — every owner in one set of statements, and attachments found by the foreign key the
upload door stamps. That second difference is not a shortcut: the key is stamped whether or not the
file is ever sent, so this finds a file uploaded to a chat and never sent, which a payload scan
structurally cannot see.

OWNER SCOPING SURVIVES WITHOUT A LOOP. It is needed in exactly one place — resolving a
client-minted identifier out of a message payload — and there is no such resolution here: every
attachment is reached by `attachments.conversation_id`, a real indexed foreign key, so the
predicate that makes the interactive path safe has nothing to be unsafe about.

THE ORDER IS THE ESTABLISHED ONE: rows deleted inside the caller's transaction, the caller commits,
then blobs are swept best-effort. Inverting it destroys blobs that a rolled-back delete would have
kept.
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
    """What one pass condemned, and what it could not finish.

    `outstanding` is the candidates the cap left behind, reported rather than dropped: the first
    enabled run's candidate set is the whole historical backlog, not a weekly increment, so a
    number here is the difference between "the backlog is draining" and "the pass is stuck".
    """

    conversation_ids: tuple[uuid.UUID, ...] = ()
    blob_keys: tuple[str, ...] = ()
    outstanding: int = 0

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
    rows = (
        await db.scalars(
            sa.select(Conversation.id)
            .where(Conversation.updated_at < cutoff)
            .order_by(Conversation.updated_at.asc())
            .limit(limit)
        )
    ).all()
    return tuple(rows), max(remaining - len(rows), 0)


async def gather_and_delete(
    db: AsyncSession, *, conversation_ids: tuple[uuid.UUID, ...], cutoff: dt.datetime
) -> RetentionSweep:
    """Delete the condemned rows inside the caller's transaction; return the keys to sweep.

    THE IDLENESS IS RE-ASKED AT DELETE TIME, in the delete's own predicate rather than trusting the
    id list. Selection and deletion are separated by the attachment read, and a citizen who sends a
    message in that window must keep their conversation — so the `updated_at` test travels with the
    ids and the trigger's write is what takes a live chat back out of the set.

    MESSAGES CASCADE in the database; attachment ROWS do not — their foreign key is
    `ON DELETE SET NULL`, which is right for the interactive path and would leave this one's rows
    behind holding blobs nothing points at. So they are deleted explicitly, first.
    """
    if not conversation_ids:
        return RetentionSweep()

    still_idle = sa.and_(Conversation.id.in_(conversation_ids), Conversation.updated_at < cutoff)
    doomed = (await db.scalars(sa.select(Conversation.id).where(still_idle))).all()
    if not doomed:
        return RetentionSweep()

    keys = (
        await db.scalars(
            sa.select(Attachment.storage_key).where(Attachment.conversation_id.in_(doomed))
        )
    ).all()

    await db.execute(sa.delete(Attachment).where(Attachment.conversation_id.in_(doomed)))
    await db.execute(sa.delete(Conversation).where(Conversation.id.in_(doomed)))
    return RetentionSweep(conversation_ids=tuple(doomed), blob_keys=tuple(keys))
