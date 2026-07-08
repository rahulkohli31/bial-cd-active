"""Conversations HTTP endpoints — user-scoped chat header + message reads/writes (R14, R4).

Byte-matches the Express `/api/conversations` contract (`server/conversations.js`) the SPA's
`conversationApi.js` consumes: the wire shape carries Mongo-style `_id` (the SPA normalizes
`_id → id`), camelCase timestamps, and the `{error:{message}}` envelope. Identity is ALWAYS the
authenticated caller; every query is scoped by `user_id` (a dropped predicate is a cross-user
leak). This unit ships list / get-with-messages / patch; append (atomic header-upsert + message
insert) and delete-with-cleanup (sweeps attachment objects, releases deck PDFs) land in U9.
"""

from __future__ import annotations

import datetime
import math
import re
import uuid
from typing import Annotated, Any, TypeGuard

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError

from src.api.deps import CurrentUser, DbSession
from src.api.v1.attachments.router import storage_dependency
from src.db.models.attachment import Attachment
from src.db.models.conversation import Conversation, ConversationKind
from src.db.models.message import Message, MessageRole
from src.services.extract.office import PPTX_MEDIA_TYPE
from src.services.storage import ObjectStorage, StorageError

router = APIRouter(prefix="/conversations", tags=["conversations"])

# Client-minted id shape (Express `ID_RE`) — a safe key token, no `/` or `..`.
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
# The valid `?kind` values (Express `KINDS`).
_KINDS = {k.value for k in ConversationKind}
# Newest-first list cap and per-conversation message cap (Express limits).
_LIST_LIMIT = 200
_MESSAGES_LIMIT = 1000
# A text/office part's UTF-8 byte cap (Express `TEXT_BLOCK_MAX_CHARS`).
_TEXT_BLOCK_MAX_BYTES = 512 * 1024
# The two message roles the SPA sends (Express `role`).
_ROLES = {r.value for r in MessageRole}
# A conversation-owned storage handle for the delete sweep (swappable in tests).
StorageDep = Annotated[ObjectStorage, Depends(storage_dependency)]


def _error(message: str, status_code: int) -> JSONResponse:
    """The Express error envelope the SPA reads: `{"error": {"message": …}}`."""
    return JSONResponse(status_code=status_code, content={"error": {"message": message}})


def _iso(dt: datetime.datetime) -> str:
    """A UTC ISO-8601 string with millisecond precision + `Z`, matching JS
    `Date.toISOString()` (the SPA-minted / stored timestamp format)."""
    utc = dt.astimezone(datetime.UTC)
    return utc.strftime("%Y-%m-%dT%H:%M:%S.") + f"{utc.microsecond // 1000:03d}Z"


def _header_dict(conv: Conversation) -> dict[str, Any]:
    """One conversation header in the SPA's expected shape (`_id`, not `id`). Optional
    fields are omitted when unset (matching the raw Cosmos doc the SPA normalizes)."""
    header: dict[str, Any] = {
        "_id": str(conv.id),
        "kind": conv.kind.value,
        "createdAt": _iso(conv.created_at),
        "updatedAt": _iso(conv.updated_at),
    }
    if conv.title is not None:
        header["title"] = conv.title
    if conv.context is not None:
        header["context"] = conv.context
    if conv.code is not None:
        header["code"] = conv.code
    return header


def _message_dict(msg: Message) -> dict[str, Any]:
    """One message in the SPA's expected shape (`_id`, `parts` — the JSONB `parts` column)."""
    return {
        "_id": str(msg.id),
        "role": msg.role.value,
        "parts": msg.parts,
        "seq": msg.seq,
        "createdAt": _iso(msg.created_at),
    }


@router.get("")
async def list_conversations(
    user: CurrentUser, db: DbSession, kind: str | None = None
) -> JSONResponse:
    # Optional kind filter; an unknown value is a client error (not an empty list).
    if kind is not None and kind not in _KINDS:
        return _error("Unknown kind.", 400)
    query = sa.select(Conversation).where(Conversation.user_id == user.id)
    if kind is not None:
        query = query.where(Conversation.kind == ConversationKind(kind))
    query = query.order_by(Conversation.updated_at.desc()).limit(_LIST_LIMIT)
    rows = (await db.execute(query)).scalars().all()
    return JSONResponse(content={"conversations": [_header_dict(c) for c in rows]})


async def _load_owned(
    db: DbSession, user_id: uuid.UUID, conversation_id: str
) -> Conversation | JSONResponse:
    """Resolve a caller-owned conversation from a path id, or the matching error response:
    400 for a malformed id token, 404 for a well-formed id that resolves to nothing the
    caller owns (owner-scoped — a cross-user id is indistinguishable from a missing one)."""
    if not _ID_RE.match(conversation_id):
        return _error("Invalid conversation id.", 400)
    try:
        cid = uuid.UUID(conversation_id)
    except ValueError:
        # A valid id token that isn't a UUID can key no stored conversation.
        return _error("Conversation not found.", 404)
    conv = await db.scalar(
        sa.select(Conversation).where(Conversation.id == cid, Conversation.user_id == user_id)
    )
    if conv is None:
        return _error("Conversation not found.", 404)
    return conv


@router.get("/{conversation_id}")
async def get_conversation(conversation_id: str, user: CurrentUser, db: DbSession) -> JSONResponse:
    owned = await _load_owned(db, user.id, conversation_id)
    if isinstance(owned, JSONResponse):
        return owned
    messages = (
        (
            await db.execute(
                sa.select(Message)
                .where(Message.conversation_id == owned.id, Message.user_id == user.id)
                .order_by(Message.seq.asc())
                .limit(_MESSAGES_LIMIT)
            )
        )
        .scalars()
        .all()
    )
    return JSONResponse(
        content={
            "conversation": _header_dict(owned),
            "messages": [_message_dict(m) for m in messages],
        }
    )


def _validate_code_snapshot(code: Any) -> JSONResponse | None:
    """Validate a PATCH `code` snapshot (Express `validateCodeSnapshot`): an object with a
    non-empty `source` and `entry`. Returns the 400 response on failure, else None."""
    if not isinstance(code, dict):
        return _error("code must be an object", 400)
    source = code.get("source")
    if not isinstance(source, str) or not source:
        return _error("code.source is required", 400)
    entry = code.get("entry")
    if not isinstance(entry, str) or not entry:
        return _error("code.entry is required", 400)
    return None


@router.patch("/{conversation_id}")
async def patch_conversation(
    conversation_id: str, request: Request, user: CurrentUser, db: DbSession
) -> JSONResponse:
    if not _ID_RE.match(conversation_id):
        return _error("Invalid conversation id.", 400)
    try:
        body: Any = await request.json()
    except ValueError, TypeError:
        body = {}
    if not isinstance(body, dict):
        body = {}

    # Code is validated BEFORE the ownership check (Express order): a malformed snapshot is
    # a 400 even against another user's id.
    if "code" in body and (bad := _validate_code_snapshot(body["code"])) is not None:
        return bad

    owned = await _load_owned(db, user.id, conversation_id)
    if isinstance(owned, JSONResponse):
        return owned

    # Apply only the fields present in the body (absent ≠ null — `key in body` distinguishes).
    if "code" in body:
        owned.code = {"current": body["code"]}
    if "title" in body:
        # title is a text column — a non-string would 500 on commit; 400 instead.
        # (context is JSONB and legitimately accepts objects, so it is not narrowed.)
        if not isinstance(body["title"], str):
            return _error("title must be a string", 400)
        owned.title = body["title"]
    if "context" in body:
        owned.context = body["context"]
    await db.commit()
    return JSONResponse(content={"ok": True})


# --- U9: append (atomic header-upsert + message insert) -----------------------


def _as_uuid(value: Any) -> uuid.UUID | None:
    if not isinstance(value, str):
        return None
    try:
        return uuid.UUID(value)
    except ValueError:
        return None


def _is_finite_number(value: Any) -> TypeGuard[int | float]:
    # A finite JSON number (json.loads can emit NaN/Infinity — exclude them and bool).
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _parse_iso_or(value: Any, default: datetime.datetime) -> datetime.datetime:
    """A client-minted ISO timestamp string → aware datetime, else `default`."""
    if not isinstance(value, str):
        return default
    try:
        parsed = datetime.datetime.fromisoformat(value)
    except ValueError:
        return default
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=datetime.UTC)


def _validate_parts(parts: Any) -> str | None:
    """Validate a message's content parts (Express `validateParts`) — returns the error string
    or None. Two part types: `text` and `file` (kinds image/document/office/deck)."""
    if not isinstance(parts, list) or not parts:
        return "message.parts must be a non-empty array"
    for part in parts:
        if not isinstance(part, dict):
            return "message.parts contains an invalid entry"
        part_type = part.get("type")
        if part_type == "text":
            text = part.get("text")
            if not isinstance(text, str):
                return "a text part must carry a string"
            if len(text.encode("utf-8")) > _TEXT_BLOCK_MAX_BYTES:
                return "a text part is too large"
        elif part_type == "file":
            att_id = part.get("attachmentId")
            if not isinstance(att_id, str) or not _ID_RE.match(att_id):
                return "a file part has an invalid attachmentId"
            if part.get("kind") not in ("image", "document", "office", "deck"):
                return "a file part has an invalid kind"
            if not isinstance(part.get("mediaType"), str):
                return "a file part has an invalid mediaType"
            if part.get("kind") == "office" and (error := _validate_office_part(part)) is not None:
                return error
            if part.get("kind") == "deck" and (error := _validate_deck_part(part)) is not None:
                return error
        else:
            return f"unsupported part type: {part_type}"
    return None


def _validate_office_part(part: dict[str, Any]) -> str | None:
    # Office parts persist their extracted Markdown (re-sent every turn) — bound it like text.
    text = part.get("text")
    if not isinstance(text, str):
        return "an office file part must carry extracted text"
    if len(text.encode("utf-8")) > _TEXT_BLOCK_MAX_BYTES:
        return "an office file part text is too large"
    note = part.get("truncationNote")
    if note is not None and (not isinstance(note, str) or len(note) > 1000):
        return "an office file part has an invalid truncation note"
    return None


def _validate_deck_part(part: dict[str, Any]) -> str | None:
    pdf_file_id = part.get("pdfFileId")
    if not isinstance(pdf_file_id, str) or not pdf_file_id or len(pdf_file_id) > 256:
        return "a deck file part has an invalid pdfFileId"
    page_count = part.get("pageCount")
    if page_count is not None and (
        not isinstance(page_count, int) or isinstance(page_count, bool) or page_count < 0
    ):
        return "a deck file part has an invalid pageCount"
    return None


def _validate_message_input(message: Any) -> str | None:
    if not isinstance(message, dict):
        return "message is required"
    message_id = message.get("_id")
    if not isinstance(message_id, str) or not _ID_RE.match(message_id):
        return "message._id is invalid"
    if message.get("role") not in _ROLES:
        return "message.role must be user or assistant"
    if not _is_finite_number(message.get("seq")):
        return "message.seq must be a number"
    return _validate_parts(message.get("parts"))


@router.post("/{conversation_id}/messages")
async def append_message(
    conversation_id: str, request: Request, user: CurrentUser, db: DbSession
) -> JSONResponse:
    if not _ID_RE.match(conversation_id):
        return _error("Invalid conversation id.", 400)
    try:
        body: Any = await request.json()
    except ValueError, TypeError:
        body = {}
    if not isinstance(body, dict):
        body = {}

    message = body.get("message")
    if (message_error := _validate_message_input(message)) is not None:
        return _error(message_error, 400)
    # Validation guarantees a dict; this redundant narrow satisfies the type checker.
    if not isinstance(message, dict):
        return _error("message is required", 400)
    header = body.get("header")
    if not isinstance(header, dict) or header.get("kind") not in _KINDS:
        return _error("header.kind must be planning, assistant, or builder.", 400)

    # ID_RE-valid but non-UUID tokens can't key our Uuid columns (the SPA only mints UUIDs).
    conversation_uuid = _as_uuid(conversation_id)
    if conversation_uuid is None:
        return _error("Invalid conversation id.", 400)
    message_uuid = _as_uuid(message["_id"])
    if message_uuid is None:
        return _error("message._id is invalid", 400)

    now = datetime.datetime.now(datetime.UTC)
    # Header: a cross-user id collision is a 409 (write-IDOR closed), never a silent overwrite.
    existing = await db.scalar(sa.select(Conversation).where(Conversation.id == conversation_uuid))
    if existing is not None and existing.user_id != user.id:
        return _error("Conversation id already in use.", 409)
    if existing is None:
        title = header.get("title")
        context = header.get("context")
        db.add(
            Conversation(
                id=conversation_uuid,
                user_id=user.id,
                kind=ConversationKind(header["kind"]),
                title=title if isinstance(title, str) else None,
                context=context if isinstance(context, dict) else None,
                created_at=_parse_iso_or(header.get("createdAt"), now),
            )
        )
    else:
        title = header.get("title")
        if isinstance(title, str):
            existing.title = title
        if "context" in header:
            context = header.get("context")
            existing.context = context if isinstance(context, dict) else None
        # Touch so every append surfaces the conversation as recent (Express $sets updatedAt).
        existing.updated_at = now

    seq = int(message["seq"])
    # Message insert is idempotent on a duplicate _id (Express swallows the dup-key as success).
    already = await db.scalar(sa.select(Message.id).where(Message.id == message_uuid))
    if already is None:
        schema_version_raw = message.get("schemaVersion")
        db.add(
            Message(
                id=message_uuid,
                conversation_id=conversation_uuid,
                user_id=user.id,
                role=MessageRole(message["role"]),
                seq=seq,
                schema_version=int(schema_version_raw)
                if _is_finite_number(schema_version_raw)
                else 1,
                parts=message["parts"],
                created_at=_parse_iso_or(message.get("createdAt"), now),
            )
        )
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        # A concurrent append of the SAME turn (same conversation_id+seq) won the race —
        # idempotent success, matching the pre-checked dup message-id handling above.
        if "uq_messages_conversation_seq" in str(exc.orig):
            return JSONResponse(
                status_code=201,
                content={"ok": True, "message": {"_id": str(message_uuid), "seq": seq}},
            )
        # Otherwise a concurrent insert of the same conversation id won — surface as a 409.
        return _error("Conversation id already in use.", 409)
    return JSONResponse(
        status_code=201, content={"ok": True, "message": {"_id": str(message_uuid), "seq": seq}}
    )


# --- U9: delete with cleanup --------------------------------------------------


@router.delete("/{conversation_id}")
async def delete_conversation(
    conversation_id: str, user: CurrentUser, db: DbSession, storage: StorageDep
) -> JSONResponse:
    owned = await _load_owned(db, user.id, conversation_id)
    if isinstance(owned, JSONResponse):
        return owned

    # Gather the file parts across the conversation's messages so their objects can be swept.
    messages = (
        (
            await db.execute(
                sa.select(Message).where(
                    Message.conversation_id == owned.id, Message.user_id == user.id
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
    # NOTE: a deck part's internal Files-API `pdfFileId` release is deferred with the Foundry
    # hosting-mode decision — Azure-hosted Foundry has no Files API, so there is nothing to
    # release; if Anthropic-hosted mode is confirmed, wire the release here (U13/ADR-0026).

    if attachment_ids:
        attachments = (
            (
                await db.execute(
                    sa.select(Attachment).where(
                        Attachment.user_id == user.id,
                        Attachment.attachment_id.in_(attachment_ids),
                    )
                )
            )
            .scalars()
            .all()
        )
        for attachment in attachments:
            try:
                await storage.delete(attachment.storage_key)
            except StorageError:
                # Best-effort: a missing object / container must not block the delete.
                pass
            # A deck attachment also wrote a derived `{key}.pdf` sibling — sweep it too so it
            # doesn't leak (best-effort; a missing object / store error must not block delete).
            if attachment.media_type == PPTX_MEDIA_TYPE:
                try:
                    await storage.delete(attachment.storage_key + ".pdf")
                except StorageError:
                    pass
            await db.delete(attachment)

    # Delete the conversation; its messages cascade (FK ON DELETE CASCADE).
    await db.delete(owned)
    await db.commit()
    return JSONResponse(content={"ok": True})
