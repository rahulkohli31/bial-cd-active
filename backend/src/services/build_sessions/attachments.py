"""R3 — materialize a conversation's attachments into the build agent's prompt (native store).

Binary attachments live in messages as `{kind: "bial-attachment-ref", attachment_id}` markers
(U4 externalizes `BinaryContent` at the persist seam — bytes never sit in a row). This module is
the build path's read-back: the markers appended since the last build STARTED are resolved to
`BinaryContent` for `agent.iter`, owner-scoped through the attachments table.

Inline text/office content needs NO materialization here anymore: in the native shape the fenced
text lives INSIDE the user prompt's content strings (the producer inlines it at persist time —
U5/U7), so it reaches the model as ordinary history. Only binaries make the round trip through
the object store.

TODO(U5): once producers write native turns, re-verify the boundary semantics against a real
mid-build turn (the `startedSeq` capture in `SessionManager.start` is unchanged).

**Why the build path may read messages from the DB at all.** Issue #28's "the server never reads
messages from the DB" is a rule about the CHAT RELAY (`/v1/claude`), which is a stateless relay.
It is NOT a rule about the build path — attachments travel by conversation REFERENCE (an optional
`conversationId` on the start body) precisely so the bytes don't have to make a second trip
through the browser.

**Every failure RAISES** `BuildAttachmentError`, which the router turns into a 422 naming the
file. A clear "we couldn't read your file" beats a build that pretends it read it (the
silent-drop bug this module exists to prevent).

**Fail-first, before provisioning.** The whole resolution runs at the very top of
`SessionManager.start` — before the per-user lock, before the sandbox — so a rejected attachment
costs the user nothing (no container, no quota, no lock to compensate).
"""

from __future__ import annotations

import uuid
from typing import Any

import sqlalchemy as sa
from pydantic_ai import BinaryContent
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models.attachment import Attachment
from src.db.models.conversation import Conversation
from src.db.models.message import Message, MessageEntryKind
from src.services.build_sessions.outcome import EMPTY_TRANSCRIPT
from src.services.extract.office import PPTX_MEDIA_TYPE
from src.services.media.magic import bytes_match_declared
from src.services.messages.store import ATTACHMENT_REF_KIND
from src.services.storage import (
    ObjectStorage,
    StorageError,
    StorageNotFoundError,
    assert_owned,
    get_storage,
)


class BuildAttachmentError(Exception):
    """An attachment could not be materialized → the router's 422. The message is USER-FACING
    (it names the file) and carries no internal detail (`.claude/rules/security.md`)."""


class ConversationNotFoundError(Exception):
    """The referenced conversation is not the caller's, or does not belong to the start's
    project → the router's non-leaking 404 (ADR-0004). A cross-user id and a cross-project id
    are both indistinguishable from a missing one: grounding a build in another project's files
    must not even be detectable, let alone possible."""


def _boundary(rows: list[Message]) -> int:
    """The EXCLUSIVE seq bound this build collects after: the last build's START marker.

    THE BOUNDARY IS TEMPORAL, NOT POSITIONAL, and the difference is a silent data-loss bug. The
    outcome row is allocated at build END, so it lands AFTER any turn the user sent while the
    build was running — and the composer stays live during a build on purpose. Starting the
    collection after the outcome ROW would put every mid-build turn permanently BEHIND the
    boundary: attach a spreadsheet while a build runs and no later build ever sees it, with no
    error on either side. `meta.startedSeq` — the head at that build's start — is the honest
    bound; a row without one (defensive: the writer always stamps it when the start captured
    one) can only offer its own position.

    `EMPTY_TRANSCRIPT` (-1) when the thread holds no outcome at all: a first build collects
    everything, which is right.
    """
    boundary = EMPTY_TRANSCRIPT
    for row in rows:
        if row.entry_kind is not MessageEntryKind.SYSTEM_EVENT:
            continue
        meta = row.meta if isinstance(row.meta, dict) else None
        if meta is None or "sessionId" not in meta:
            continue  # a lifecycle entry that is not a build outcome
        started = meta.get("startedSeq")
        # bool is an int subclass — a JSON `true` here is malformed, not a marker.
        if isinstance(started, int) and not isinstance(started, bool):
            boundary = started
        else:
            boundary = row.seq
    return boundary


def _collect_ref_ids(node: Any, ordered: list[str], seen: set[str]) -> None:
    """Walk a payload tree for attachment ref markers, preserving first-seen order."""
    if isinstance(node, list):
        for item in node:
            _collect_ref_ids(item, ordered, seen)
    elif isinstance(node, dict):
        if node.get("kind") == ATTACHMENT_REF_KIND:
            ref = node.get("attachment_id")
            if isinstance(ref, str) and ref not in seen:
                seen.add(ref)
                ordered.append(ref)
        for value in node.values():
            _collect_ref_ids(value, ordered, seen)


def _collect_attachment_ids(rows: list[Message]) -> list[str]:
    """The attachment ids referenced by every TURN appended since the last build STARTED
    (or the thread start), in thread order, deduped.

    Deduped on the attachment id — the identity the `attachments` table is keyed on with
    `user_id`. Re-attaching the same file across turns therefore reaches the model once. Only
    `turn` rows are scanned: `step`/`system_event` payloads are the agent's own output and a
    marker row carries no files.
    """
    boundary = _boundary(rows)
    ordered: list[str] = []
    seen: set[str] = set()
    for row in rows:
        if row.seq <= boundary or row.entry_kind is not MessageEntryKind.TURN:
            continue
        _collect_ref_ids(row.payload, ordered, seen)
    return ordered


async def _binary_from_ref(
    db: AsyncSession,
    storage: ObjectStorage,
    user_id: uuid.UUID,
    attachment_id: str,
) -> BinaryContent:
    """An attachment reference → `BinaryContent`, rehydrated from Blob.

    The storage key + media type come from the `attachments` TABLE, never from the payload: the
    row is looked up by (`user_id`, `attachmentId`) — owner-scoped by construction (ADR-0004) —
    exactly as `attachments/router.py::_load_owned` does. The magic-byte gate the upload path
    applied is re-asserted, so a swapped blob can't ride a stale row.
    """
    row = await db.scalar(
        sa.select(Attachment).where(
            Attachment.user_id == user_id, Attachment.attachment_id == attachment_id
        )
    )
    if row is None:
        raise BuildAttachmentError(
            "An attached file is no longer available. Attach it again and retry the build."
        )
    name = row.name or "the attached file"
    if row.media_type == PPTX_MEDIA_TYPE:
        raise BuildAttachmentError(
            f'PowerPoint decks are not supported in builds yet, so "{name}" cannot be used. '
            "Remove it and try again."
        )
    # Defense in depth: the query already scoped by user_id; re-assert the key is in scope.
    assert_owned(row.storage_key, user_id)
    try:
        data = await storage.get(row.storage_key)
    except StorageNotFoundError:
        raise BuildAttachmentError(
            f'"{name}" is no longer available. Attach it again and retry the build.'
        ) from None
    except StorageError:
        raise BuildAttachmentError(
            f'Could not read "{name}" right now. Please try again in a moment.'
        ) from None
    if not bytes_match_declared(row.media_type, data):
        raise BuildAttachmentError(
            f'"{name}" does not match its file type and cannot be used in a build. '
            "Attach it again and retry."
        )
    return BinaryContent(data=data, media_type=row.media_type, identifier=row.attachment_id)


async def resolve_build_attachments(
    db: AsyncSession,
    user_id: uuid.UUID,
    project_id: uuid.UUID,
    conversation_id: uuid.UUID,
) -> list[str | BinaryContent]:
    """The conversation's binary attachments as pydantic-ai content, in thread order.

    Owner-scoped AND project-scoped: the conversation must be the caller's AND belong to the
    start's project, else `ConversationNotFoundError` (a build must not be grounded in another
    project's files). Any rehydration/validation failure raises `BuildAttachmentError`.
    """
    conversation = await db.scalar(
        sa.select(Conversation).where(
            Conversation.id == conversation_id,
            Conversation.user_id == user_id,
            Conversation.project_id == project_id,
        )
    )
    if conversation is None:
        raise ConversationNotFoundError(str(conversation_id))
    rows = list(
        await db.scalars(
            sa.select(Message)
            .where(Message.conversation_id == conversation_id, Message.user_id == user_id)
            .order_by(Message.seq)
        )
    )
    attachment_ids = _collect_attachment_ids(rows)
    if not attachment_ids:
        return []

    try:
        storage = get_storage()
    except StorageError:
        raise BuildAttachmentError(
            "Could not read the attached files right now. Please try again in a moment."
        ) from None
    content: list[str | BinaryContent] = []
    for attachment_id in attachment_ids:
        content.append(await _binary_from_ref(db, storage, user_id, attachment_id))
    return content
