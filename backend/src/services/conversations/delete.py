"""The commit-less core of a conversation delete (KD-3), shared by the conversation
DELETE endpoint and the project cascade-delete service (U6).

`delete_conversation` used to sweep object-store blobs INLINE and then delete rows
before committing — so a commit failure left blobs destroyed while their rows rolled
back (orphaned rows pointing at deleted blobs). This core splits the two halves:
delete the attachment + conversation ROWS inside the caller's transaction and RETURN
the object-store keys to sweep; the caller commits and only THEN best-effort sweeps
the blobs. A rolled-back transaction therefore never destroys a blob a restored row
still points at (the rollback-safety guarantee, KD-3). Two call sites (the endpoint
and the cascade) earn this its own service home (ADR-0010).
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models.attachment import Attachment
from src.db.models.conversation import Conversation
from src.db.models.message import Message
from src.services.extract.office import PPTX_MEDIA_TYPE


async def gather_and_delete_conversation(
    db: AsyncSession, conversation: Conversation, *, user_id: uuid.UUID
) -> list[str]:
    """Delete a conversation, its messages (DB `ON DELETE CASCADE`), and its attachment
    ROWS inside the caller's transaction; return the object-store keys (attachment blobs
    plus each deck attachment's derived `{key}.pdf` sibling) to sweep AFTER the caller
    commits. Commit-less: the caller owns both the commit and the post-commit blob sweep.
    Owner-scoped by `user_id` (ADR-0004) — attachments hang off `user_id`, not the
    conversation, so the caller must pass the owning user id, not trust the row."""
    messages = (
        (
            await db.execute(
                sa.select(Message).where(
                    Message.conversation_id == conversation.id, Message.user_id == user_id
                )
            )
        )
        .scalars()
        .all()
    )
    attachment_ids: set[str] = set()
    for message in messages:
        for part in message.parts or []:
            if isinstance(part, dict) and part.get("type") == "file":
                att_id = part.get("attachmentId")
                if isinstance(att_id, str):
                    attachment_ids.add(att_id)
    # NOTE: a deck part's internal Files-API `pdfFileId` release is deferred with the
    # Foundry hosting-mode decision (Azure-hosted Foundry has no Files API to release
    # against; wire it here if Anthropic-hosted mode is confirmed — U13/ADR-0026).

    blob_keys: list[str] = []
    if attachment_ids:
        attachments = (
            (
                await db.execute(
                    sa.select(Attachment).where(
                        Attachment.user_id == user_id,
                        Attachment.attachment_id.in_(attachment_ids),
                    )
                )
            )
            .scalars()
            .all()
        )
        for attachment in attachments:
            blob_keys.append(attachment.storage_key)
            # A deck attachment also wrote a derived `{key}.pdf` sibling — sweep it too.
            if attachment.media_type == PPTX_MEDIA_TYPE:
                blob_keys.append(attachment.storage_key + ".pdf")
            await db.delete(attachment)

    # Delete the conversation; its messages cascade (FK ON DELETE CASCADE).
    await db.delete(conversation)
    return blob_keys
