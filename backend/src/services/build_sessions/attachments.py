"""R3 — materialize a conversation's attachments into the build agent's prompt.

Files attached in the build composer are persisted by the SPA as `messages.parts` BEFORE
`start` fires (`portal/src/utils/attachmentStore.js::buildUserParts`), but nothing ever read
them back: the build ran text-only and the user's spreadsheet was silently ignored. This module
is the read-back — the seam that turns persisted parts into what `agent.iter` consumes.

**Why the build path may read messages from the DB at all.** Issue #28's "the server never reads
messages from the DB" is a rule about the CHAT RELAY (`/v1/claude`), which is a stateless relay:
the browser assembles the prompt and the server forwards it. It is NOT a rule about the build
path — C3 §2.1 leaves the prompt→BRAIN transport unfrozen, and attachments travel by conversation
REFERENCE (an optional `conversationId` on the start body) precisely so the bytes don't have to
make a second trip through the browser. The three message shapes still hold: DB `parts` (here) →
pydantic-ai content (out).

**Why not `to_model_content`.** The relay's mapper (`services/agent/content.py:30`) is a
deliberately never-raise boundary: it SKIPS a malformed block so one bad block can't abort a chat
turn. Routing the build through it would reintroduce the exact silent-drop this module exists to
delete — a build that quietly ignores the attachment. Here every failure RAISES
`BuildAttachmentError`, which the router turns into a 422 naming the file. A clear "we couldn't
read your file" beats a build that pretends it read it. The validation PRIMITIVE
(`bytes_match_declared`) is shared; the lenient mapper is not.

**Fail-first, before provisioning.** The whole resolution runs at the very top of
`SessionManager.start` — before the per-user lock, before the sandbox — so a rejected attachment
costs the user nothing (no container, no quota, no lock to compensate).

Kinds:
  * `image` / `document`  → bytes rehydrated from Blob → `BinaryContent` (vision).
  * `office`              → the extracted Markdown ALREADY persisted on the part at upload time,
                            fenced. No blob re-download, no governor re-run: the parse governor
                            ran once at upload (`untrusted-file-parsing-and-cdn-chart-allowlist`
                            learning) and the server already holds its output.
  * inline text (csv/txt) → the content persisted on the text part, fenced.
  * `deck`                → rejected (the feature is gated off).
"""

from __future__ import annotations

import re
import uuid
from typing import Any

import sqlalchemy as sa
from pydantic_ai import BinaryContent
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models.attachment import Attachment
from src.db.models.conversation import Conversation
from src.db.models.message import Message, MessageRole
from src.services.media.magic import bytes_match_declared
from src.services.storage import (
    ObjectStorage,
    StorageError,
    StorageNotFoundError,
    assert_owned,
    get_storage,
)

# The per-attachment ceiling on derived/extracted text entering the build prompt.
#
# 256 KiB is the LARGEST per-attachment text any composer-produced part can legitimately carry:
# it is the portal's own `MAX_TEXT_FILE_SIZE` (`utils/attachmentInput.js`) for an inline csv/txt,
# and office parts sit far under it (the extractor truncates at `MAX_OFFICE_TEXT_CHARS` = 100 KiB
# — `services/extract/office.py`). So the ceiling never fires on our own producers' output; it
# fires only on a part hand-crafted through the append API, whose own text bound
# (`_TEXT_BLOCK_MAX_BYTES` = 512 KiB, `api/v1/conversations/router.py`) is deliberately looser
# than any composer can emit. It is a build-path backstop, not a duplicate of that check.
#
# It is also a real context bound: ~256 KiB ≈ 65k tokens, roughly a third of the model's window,
# so a single attachment can never blow the context mid-build (which would burn quota and surface
# as a confusing failure minutes in). This is a PER-ATTACHMENT bound; the aggregate across the
# ≤5-files-per-message composer cap is not bounded here.
MAX_ATTACHMENT_PROMPT_TEXT_CHARS = 256 * 1024

# Fence sanitization, byte-matching the portal's `attachmentStore.js` (`sanitizeFenceName` /
# `neutralizeFence`) so client-side and server-side assembly agree on the model-facing shape.
# Both patterns are single-pass character/literal classes with no nesting or backtracking — the
# ReDoS learning (`redos-secret-redaction-regex`) is satisfied by construction.
_FENCE_NAME_STRIP = re.compile(r'[\r\n"<>]')
_FENCE_NAME_MAX = 200
_FENCE_CLOSE = re.compile(r"</(attachment)", re.IGNORECASE)


class BuildAttachmentError(Exception):
    """An attachment could not be materialized → the router's 422. The message is USER-FACING
    (it names the file) and carries no internal detail (`.claude/rules/security.md`)."""


class ConversationNotFoundError(Exception):
    """The referenced conversation is not the caller's, or does not belong to the start's
    project → the router's non-leaking 404 (ADR-0004). A cross-user id and a cross-project id
    are both indistinguishable from a missing one: grounding a build in another project's files
    must not even be detectable, let alone possible."""


def _sanitize_fence_name(name: Any) -> str:
    """Strip characters that could break out of the fence's `name="…"` attribute."""
    return _FENCE_NAME_STRIP.sub(" ", str(name or ""))[:_FENCE_NAME_MAX]


def _neutralize_fence(text: str) -> str:
    """Neutralize any literal `</attachment>` inside fenced DATA so attacker-controlled content
    (a filename or the file body itself) cannot close the fence early and have the remainder read
    as instructions — the uploads-are-untrusted boundary (`.claude/rules/security.md`)."""
    return _FENCE_CLOSE.sub(r"<\\/\1", text)


def _fence(name: Any, fence_type: str, text: str) -> str:
    """The model-facing DATA fence — must match the portal's `officeFence`.

    EVERY interpolated value is defended, `type` included: it is the office part's persisted
    `format`, which is client-authored JSON like `name` is, so a fence built from it is only
    safe if it is sanitized like `name` is. The upload path never writes anything but
    `word`/`excel` and `_validate_office_part` now rejects the rest at the append boundary —
    this is the second lock on the same door, because the fence's claim is "attacker-controlled
    content cannot close the fence early", and that has to be true of the fence itself, not of
    the callers that happen to reach it today.
    """
    return (
        f'<attachment name="{_sanitize_fence_name(name)}" '
        f'type="{_sanitize_fence_name(fence_type)}">\n'
        f"{_neutralize_fence(text)}\n"
        f"</attachment>"
    )


def _display_name(part: dict[str, Any], fallback: str) -> str:
    """The file name for the fence + any user-facing error, sanitized: it is client-supplied and
    lands in both an attribute and an error message, so it may not carry quotes/newlines.

    A file part carries `name` at the top level; an inline csv/txt keeps its descriptor nested
    under `attachment` (the part's own `text` IS the file body) — so look there too, else every
    csv would fence as "the attached file".
    """
    name = part.get("name")
    if not isinstance(name, str) or not name:
        inline = part.get("attachment")
        name = inline.get("name") if isinstance(inline, dict) else None
    return _sanitize_fence_name(name) if isinstance(name, str) and name else fallback


def _bounded_text(text: Any, name: str) -> str:
    """A part's persisted text, ceiling-enforced. Absent/malformed or over-ceiling → 422."""
    if not isinstance(text, str) or not text:
        raise BuildAttachmentError(
            f'Could not read the contents of "{name}". Remove it and attach it again.'
        )
    if len(text) > MAX_ATTACHMENT_PROMPT_TEXT_CHARS:
        raise BuildAttachmentError(
            f'"{name}" is too large to include in a build: its text content exceeds the '
            f"{MAX_ATTACHMENT_PROMPT_TEXT_CHARS // 1024} KB per-file limit. "
            "Attach a smaller file and try again."
        )
    return text


def _is_build_outcome(parts: Any) -> bool:
    """True iff a message records a build outcome — the boundary the part collection starts from.

    The marker is a `build`-kind part: the persisted build-outcome message that plan
    `2026-07-16-003` U5 appends at each terminal (status / previewUrl / sessionId), whose part
    kind that plan adds to `_validate_parts`. It does NOT exist yet, so today NO message matches
    and the boundary is the thread start — which is exactly right for a single-build thread and
    needs no revisiting when 003 lands. Matching on the part (not on a role or a message flag) is
    what makes this forward-compatible: 003 chose the kind, this reads it.
    """
    if not isinstance(parts, list):
        return False
    return any(isinstance(part, dict) and part.get("type") == "build" for part in parts)


async def _binary_from_part(
    db: AsyncSession,
    storage: ObjectStorage,
    user_id: uuid.UUID,
    part: dict[str, Any],
    name: str,
) -> BinaryContent:
    """An image/document part → `BinaryContent`, rehydrated from Blob.

    The storage key + media type come from the `attachments` TABLE, never from the part: the part
    is client-authored JSON, so trusting its `key` would hand a caller an arbitrary object-store
    read (`att/{other_user}/…`), and trusting its `mediaType` would skip the upload path's
    validation. The row is looked up by (`user_id`, `attachmentId`) — owner-scoped by
    construction (ADR-0004) — exactly as `attachments/router.py::_load_owned` does.
    """
    attachment_id = part.get("attachmentId")
    if not isinstance(attachment_id, str):
        raise BuildAttachmentError(f'Could not read "{name}". Remove it and attach it again.')
    row = await db.scalar(
        sa.select(Attachment).where(
            Attachment.user_id == user_id, Attachment.attachment_id == attachment_id
        )
    )
    if row is None:
        raise BuildAttachmentError(
            f'"{name}" is no longer available. Attach it again and retry the build.'
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
    # Re-assert the magic-byte gate the upload path applied. Bytes in the store were validated at
    # upload, so a mismatch here means they are not what the row claims — RAISE (the relay's
    # mapper would silently drop the block; that is the bug being fixed).
    if not bytes_match_declared(row.media_type, data):
        raise BuildAttachmentError(
            f'"{name}" does not match its file type and cannot be used in a build. '
            "Attach it again and retry."
        )
    return BinaryContent(data=data, media_type=row.media_type)


def _collect_parts(messages: list[Message]) -> list[dict[str, Any]]:
    """The attachment-bearing parts of every USER message since the last build-outcome message
    (or the thread start), in thread order, deduped.

    NOT "the latest user message": plan `2026-07-16-003` interposes interview answer turns
    between the attachment-carrying prompt and the start, so latest-message semantics would
    silently drop the file — reintroducing the exact bug this module fixes. The boundary is the
    last build outcome, so a SECOND build in the same thread doesn't re-materialize the first
    build's files (already in that app's code) while an interview flow keeps them.

    Deduped on `attachmentId` — the identity the `attachments` table is keyed on with `user_id`,
    and the one field `_validate_parts` guarantees on every file part (`key` is NOT validated
    there, so it is not a sound dedupe axis). Re-attaching the same file across turns therefore
    reaches the model once.
    """
    start = 0
    for index, message in enumerate(messages):
        if _is_build_outcome(message.parts):
            start = index + 1
    collected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for message in messages[start:]:
        if message.role is not MessageRole.USER or not isinstance(message.parts, list):
            continue
        for part in message.parts:
            if not isinstance(part, dict):
                continue
            key = _dedupe_key(part)
            if key is None or key in seen:
                continue
            seen.add(key)
            collected.append(part)
    return collected


def _dedupe_key(part: dict[str, Any]) -> str | None:
    """The attachment identity of an attachment-bearing part, or None for plain prose (which is
    not an attachment: the build's instruction text arrives on the start body's `prompt`)."""
    if part.get("type") == "file":
        att_id = part.get("attachmentId")
        return att_id if isinstance(att_id, str) else None
    if part.get("type") == "text":
        # An inline csv/txt: `{type:'text', text:<content>, attachment:{…}}`. A text part WITHOUT
        # `attachment` is prose, not a file.
        inline = part.get("attachment")
        if not isinstance(inline, dict):
            return None
        att_id = inline.get("attachmentId")
        return att_id if isinstance(att_id, str) else None
    return None


async def resolve_build_attachments(
    db: AsyncSession,
    user_id: uuid.UUID,
    project_id: uuid.UUID,
    conversation_id: uuid.UUID,
) -> list[str | BinaryContent]:
    """The conversation's attachments as pydantic-ai content, in thread order.

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
    messages = list(
        await db.scalars(
            sa.select(Message)
            .where(Message.conversation_id == conversation_id, Message.user_id == user_id)
            .order_by(Message.seq)
        )
    )
    parts = _collect_parts(messages)
    if not parts:
        return []

    content: list[str | BinaryContent] = []
    # Resolved lazily: only the binary kinds need the object store, so a text-only/office-only
    # attachment set must not fail a start just because storage is unconfigured (dev/test).
    storage: ObjectStorage | None = None
    for part in parts:
        name = _display_name(part, "the attached file")
        if part.get("type") == "text":
            content.append(_fence(name, "text", _bounded_text(part.get("text"), name)))
            continue
        kind = part.get("kind")
        if kind == "office":
            fence_type = part.get("format")
            if not isinstance(fence_type, str):
                raise BuildAttachmentError(
                    f'Could not read the contents of "{name}". Remove it and attach it again.'
                )
            content.append(_fence(name, fence_type, _bounded_text(part.get("text"), name)))
        elif kind == "deck":
            raise BuildAttachmentError(
                f'PowerPoint decks are not supported in builds yet, so "{name}" cannot be used. '
                "Remove it and try again."
            )
        elif kind in ("image", "document"):
            if storage is None:
                try:
                    storage = get_storage()
                except StorageError:
                    raise BuildAttachmentError(
                        f'Could not read "{name}" right now. Please try again in a moment.'
                    ) from None
            content.append(await _binary_from_part(db, storage, user_id, part, name))
        else:
            raise BuildAttachmentError(
                f'"{name}" is not a file type a build can use. Remove it and try again.'
            )
    return content
