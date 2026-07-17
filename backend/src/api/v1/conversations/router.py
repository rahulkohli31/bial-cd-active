"""Conversations HTTP endpoints — user-scoped chat header + message reads/writes (R14, R4).

Byte-matches the Express `/api/conversations` contract (`server/conversations.js`) the SPA's
`conversationApi.js` consumes: the wire shape carries Mongo-style `_id` (the SPA normalizes
`_id → id`), camelCase timestamps, and the `{error:{message}}` envelope. Identity is ALWAYS the
authenticated caller; every query is scoped by `user_id` (a dropped predicate is a cross-user
leak). This unit ships list / get-with-messages / patch; append (atomic header-upsert + message
insert) and delete-with-cleanup (sweeps attachment objects, releases deck PDFs) land in U9.

`POST /builder-thread` (plan 003-U1) is the one route here with no Express ancestor: it resolves
a project's ONE canonical build conversation, so the portal's project page always opens into the
same thread instead of minting a new builder chat per send.
"""

from __future__ import annotations

import datetime
import math
import re
import uuid
from typing import Annotated, Any, TypeGuard

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.exc import StaleDataError

from src.api.deps import CurrentUser, DbSession
from src.api.deps_csrf import RequireCsrf
from src.api.v1.attachments.router import storage_dependency
from src.api.v1.conversations.schemas import (
    AppendResponse,
    BuilderThreadRequest,
    BuilderThreadResponse,
    ConversationDetailResponse,
    ConversationListResponse,
)
from src.core.errors import AppApiError
from src.db.models.app_registry import AppRegistry
from src.db.models.conversation import Conversation, ConversationKind
from src.db.models.message import Message, MessageRole
from src.schemas import AUTH_401, ErrorEnvelope, OkResponse, error_responses
from src.services.conversations import gather_and_delete_conversation
from src.services.projects import owned_project_or_404
from src.services.storage import ObjectStorage, sweep_blobs

router = APIRouter(prefix="/conversations", tags=["conversations"])

# All conversations routes are cookie-authed (`current_user`, 401 DetailBody); their own
# raises are `AppApiError` -> ErrorEnvelope. The shared 401 spec (`AUTH_401`) is reused across
# routes.

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
# The only `format` values an office part can legitimately carry — the upload route emits
# exactly these two (`services/extract/office.py`; a .pptx is the separate `deck` kind). A
# tuple, like the `kind` check it sits next to: membership compares by `==`, so a client that
# sends an unhashable `format` (a JSON object/array) is rejected rather than raising a
# TypeError out of a set lookup.
_OFFICE_FORMATS = ("word", "excel")
# The append route now has TWO 409s that mean opposite things, so the transient one carries a
# machine-readable code. Without it the only discriminator is the prose, and a client that read
# 409 as "already saved" (the permanent one) tells the user their message landed when it did not
# — and sends them to reload, destroying the text the retry needed. The C3 surface already
# solved this the same way (`build_session_already_active`); this route inherits the pattern.
_SEQ_CONFLICT_CODE = "message_seq_conflict"
# A conversation-owned storage handle for the delete sweep (swappable in tests).
StorageDep = Annotated[ObjectStorage, Depends(storage_dependency)]


async def _json_object_body(request: Request) -> dict[str, Any]:
    """The request body as a JSON object, or a 400 naming the real defect.

    Coercing an unparseable / non-object body to `{}` turns a lost write into a cheerful
    success: a truncated builder auto-save would PATCH nothing and still answer `200 {ok:true}`.
    Parse once at the boundary instead (`.claude/rules/fail-first.md`)."""
    try:
        body: Any = await request.json()
    except (ValueError, TypeError):  # fmt: skip  # ruff py314 strips parens
        raise AppApiError(400, "Invalid JSON body.") from None
    if not isinstance(body, dict):
        raise AppApiError(400, "Request body must be a JSON object.")
    return body


# The three helpers below share one rule (KTD-1): an ABSENT optional field — and an explicit
# `null`, which is how a client says "unset" for a nullable column — keeps its defined default;
# a PRESENT value of the wrong type is a client bug and raises, never a silent coercion.


def _optional_str(value: Any, field: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str):
        raise AppApiError(400, f"{field} must be a string")
    return value


def _optional_dict(value: Any, field: str) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, dict):
        raise AppApiError(400, f"{field} must be an object")
    return value


def _optional_iso(value: Any, default: datetime.datetime, field: str) -> datetime.datetime:
    """A client-minted ISO-8601 timestamp → aware datetime; absent → `default` (server now).
    A present-but-unparseable value raises rather than silently re-dating the row."""
    if value is None:
        return default
    if not isinstance(value, str):
        raise AppApiError(400, f"{field} must be an ISO-8601 string")
    try:
        parsed = datetime.datetime.fromisoformat(value)
    except ValueError:
        raise AppApiError(400, f"{field} must be an ISO-8601 string") from None
    aware = parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=datetime.UTC)
    try:
        # An extreme offset (e.g. `0001-01-01T00:00:00+14:00`) parses cleanly but leaves
        # datetime's year range once normalized to UTC — which is exactly what asyncpg's
        # timestamptz encoder does at flush, raising a DataError the IntegrityError handler
        # never sees. Probe it here so the caller gets this endpoint's 400, not a bare 500.
        aware.astimezone(datetime.UTC)
    except (OverflowError, OSError, ValueError):  # fmt: skip  # ruff py314 strips parens
        raise AppApiError(400, f"{field} is out of range") from None
    return aware


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
        # The parent project (R2/KD-4) — the SPA resolves it to the breadcrumb
        # ("ProjectName / chat title"); the chat itself stays addressed flat by its own id.
        "projectId": str(conv.project_id),
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


@router.get(
    "",
    # response_model is DOCUMENTED-ONLY (the route returns a pre-built JSONResponse, so FastAPI
    # skips response_model serialization). The omit-when-unset shape is produced by
    # `_header_dict`, NOT by an `exclude_none` flag — so no dead `response_model_exclude_none`.
    response_model=ConversationListResponse,
    responses=error_responses((400, ErrorEnvelope, "Unknown kind"), AUTH_401),
)
async def list_conversations(
    user: CurrentUser,
    db: DbSession,
    kind: str | None = None,
    project_id: Annotated[uuid.UUID | None, Query(alias="projectId")] = None,
) -> JSONResponse:
    # Optional kind filter; an unknown value is a client error (not an empty list).
    if kind is not None and kind not in _KINDS:
        raise AppApiError(400, "Unknown kind.")
    query = sa.select(Conversation).where(Conversation.user_id == user.id)
    if kind is not None:
        query = query.where(Conversation.kind == ConversationKind(kind))
    # Optional project scope (R6). Already user-scoped, so a cross-user project_id simply
    # returns nothing — no leak, no separate ownership check needed.
    if project_id is not None:
        query = query.where(Conversation.project_id == project_id)
    query = query.order_by(Conversation.updated_at.desc()).limit(_LIST_LIMIT)
    rows = (await db.execute(query)).scalars().all()
    return JSONResponse(content={"conversations": [_header_dict(c) for c in rows]})


# --- 003-U1: the project's ONE canonical builder thread -----------------------

# The advisory-lock namespace for the get-or-create below. `pg_advisory_xact_lock` shares one
# global 64-bit space with every other advisory lock in the database, so the two-int form
# (classid, objid) is what namespaces ours: this constant is the classid, and no other caller may
# reuse it for a different meaning. Named for what it protects — a project gets exactly one
# canonical thread, and this lock is what enforces it.
_ONE_THREAD_TO_RULE_THEM_ALL = 0x7B1A_1CD0


def _project_lock_key(project_id: uuid.UUID) -> int:
    """A project UUID → the signed int32 `objid` half of the advisory lock key.

    A 128-bit id does not fit an int32, so this is a truncating hash and two projects CAN
    collide. That is deliberate and harmless: a collision only makes two unrelated first-opens
    take turns for the microseconds the select-or-insert holds the lock. Correctness rests on
    "the same project always maps to the same key", which truncation preserves; uniqueness
    across projects is a throughput nicety, not an invariant.
    """
    return (project_id.int & 0xFFFF_FFFF) - 0x8000_0000


@router.post(
    "/builder-thread",
    response_model=BuilderThreadResponse,
    dependencies=[RequireCsrf],
    responses=error_responses(
        AUTH_401,
        (403, ErrorEnvelope, "CSRF check failed"),
        (404, ErrorEnvelope, "Project not found (or not owned by the caller)"),
    ),
)
async def builder_thread(
    body: BuilderThreadRequest, user: CurrentUser, db: DbSession
) -> JSONResponse:
    """Resolve the project's ONE canonical build conversation, creating it if absent.

    Canonicalization is NEWEST-WINS and derived, not stored: a project's thread is
    `max(created_at)` over its builder-kind rows, so this needs no schema change and older
    builder chats stay readable history in the recents list. Only this thread is where new
    work happens.

    RACE: a naive select-then-insert double-creates on two concurrent first-opens (two tabs,
    or a double-click) — there is no unique constraint on (project_id, kind) to catch it, and
    under newest-wins the loser's row would silently BECOME the canonical thread, orphaning
    whatever the winner had already written. A transaction-scoped advisory lock keyed on the
    project serializes the select-or-insert to a single row, with zero schema change. It is
    released with the transaction, so no path can leak it.
    """
    # Ownership FIRST — a foreign or missing project is the same non-leaking 404 (ADR-0004),
    # decided before we take a lock keyed on an id the caller may not own.
    project = await owned_project_or_404(db, user.id, body.project_id)

    await db.execute(
        sa.select(
            sa.func.pg_advisory_xact_lock(
                _ONE_THREAD_TO_RULE_THEM_ALL, _project_lock_key(project.id)
            )
        )
    )

    thread = await db.scalar(
        sa.select(Conversation)
        .where(
            Conversation.project_id == project.id,
            Conversation.user_id == user.id,
            Conversation.kind == ConversationKind.BUILDER,
        )
        .order_by(Conversation.created_at.desc(), Conversation.id.desc())
        .limit(1)
    )
    if thread is None:
        thread = Conversation(
            user_id=user.id,
            project_id=project.id,
            kind=ConversationKind.BUILDER,
            title=project.name,
        )
        db.add(thread)
        # Refresh through the flush before projecting: `created_at`/`updated_at` are
        # server-side defaults, so `_header_dict` would touch unloaded attributes on a
        # freshly-added row and raise MissingGreenlet under async.
        await db.flush()
        await db.refresh(thread)
    await db.commit()
    return JSONResponse(content={"conversation": _header_dict(thread)})


async def _load_owned(db: DbSession, user_id: uuid.UUID, conversation_id: str) -> Conversation:
    """Resolve a caller-owned conversation from a path id, or RAISE the matching error:
    400 for a malformed id token, 404 for a well-formed id that resolves to nothing the
    caller owns (owner-scoped — a cross-user id is indistinguishable from a missing one).
    Raising (vs the old sentinel return) lets the callers drop their isinstance guards
    while keeping the exact status/body."""
    if not _ID_RE.match(conversation_id):
        raise AppApiError(400, "Invalid conversation id.")
    try:
        cid = uuid.UUID(conversation_id)
    except ValueError:
        # A valid id token that isn't a UUID can key no stored conversation.
        raise AppApiError(404, "Conversation not found.") from None
    conv = await db.scalar(
        sa.select(Conversation).where(Conversation.id == cid, Conversation.user_id == user_id)
    )
    if conv is None:
        raise AppApiError(404, "Conversation not found.")
    return conv


@router.get(
    "/{conversation_id}",
    # Documented-only (JSONResponse) — see list_conversations. `_header_dict` owns the
    # omit-when-unset shape, so no dead `response_model_exclude_none`.
    response_model=ConversationDetailResponse,
    responses=error_responses(
        (400, ErrorEnvelope, "Invalid conversation id"),
        (404, ErrorEnvelope, "Conversation not found"),
        AUTH_401,
    ),
)
async def get_conversation(conversation_id: str, user: CurrentUser, db: DbSession) -> JSONResponse:
    owned = await _load_owned(db, user.id, conversation_id)
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


def _validate_code_snapshot(code: Any) -> None:
    """Validate a PATCH `code` snapshot (Express `validateCodeSnapshot`): an object with a
    non-empty `source` and `entry`. Raises `AppApiError(400)` on failure (was a sentinel
    return), else returns None."""
    if not isinstance(code, dict):
        raise AppApiError(400, "code must be an object")
    source = code.get("source")
    if not isinstance(source, str) or not source:
        raise AppApiError(400, "code.source is required")
    entry = code.get("entry")
    if not isinstance(entry, str) or not entry:
        raise AppApiError(400, "code.entry is required")


@router.patch(
    "/{conversation_id}",
    response_model=OkResponse,
    responses=error_responses(
        (400, ErrorEnvelope, "Invalid conversation id or code/title snapshot"),
        (404, ErrorEnvelope, "Conversation not found"),
        AUTH_401,
    ),
)
async def patch_conversation(
    conversation_id: str, request: Request, user: CurrentUser, db: DbSession
) -> JSONResponse:
    if not _ID_RE.match(conversation_id):
        raise AppApiError(400, "Invalid conversation id.")
    body = await _json_object_body(request)

    # Code is validated BEFORE the ownership check (Express order): a malformed snapshot is
    # a 400 even against another user's id. `_validate_code_snapshot` now raises on failure,
    # so this short-circuits ahead of `_load_owned` exactly as the sentinel return did.
    if "code" in body:
        _validate_code_snapshot(body["code"])

    owned = await _load_owned(db, user.id, conversation_id)

    # Apply only the fields present in the body (absent ≠ null — `key in body` distinguishes).
    if "code" in body:
        owned.code = {"current": body["code"]}
        # Mirror the built code to the project's single app — the source of truth for code
        # continuity (KD-9/U11) — so a later session in the project seeds from it. Builder
        # sessions only (planning/assistant chats don't own code); if the project has no app
        # yet (built before provision), the next post-provision code write picks it up.
        if owned.kind == ConversationKind.BUILDER:
            app = await db.scalar(
                sa.select(AppRegistry).where(
                    AppRegistry.project_id == owned.project_id,
                    AppRegistry.user_id == user.id,
                )
            )
            if app is not None:
                app.current_code = {"current": body["code"]}
    if "title" in body:
        # title is a text column — a non-string would 500 on commit; 400 instead.
        # (context is JSONB and legitimately accepts objects, so it is not narrowed.)
        if not isinstance(body["title"], str):
            raise AppApiError(400, "title must be a string")
        owned.title = body["title"]
    if "context" in body:
        owned.context = body["context"]
    try:
        await db.commit()
    except StaleDataError:
        # The conversation (or its whole project) was deleted between our load and this
        # flush — the loser of that race gets the same non-leaking 404 a PATCH one
        # second later would, not a 500 (builder auto-save vs delete is routine).
        raise AppApiError(404, "Conversation not found.") from None
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


def _is_whole_number(value: Any) -> bool:
    # A finite JSON number with no fractional part. `int(2.7)` would TRUNCATE to 2, which can
    # silently collide with an existing turn under the idempotent-seq handling.
    return _is_finite_number(value) and int(value) == value


def _fits_int32(value: int | float) -> bool:
    # `seq` and `schema_version` are `sa.Integer` (int32) columns. An out-of-range whole number
    # makes asyncpg raise a DataError — NOT an IntegrityError, so it escapes the handler below
    # as a bare 500. Bound it at the boundary instead.
    return -(2**31) <= value <= 2**31 - 1


def _validate_parts(parts: Any) -> str | None:
    """Validate a message's content parts (Express `validateParts`) — returns the error string
    or None. Two part types: `text` and `file` (kinds image/document/office/deck).

    `build` is NOT among them — it is the server's own record and is refused before this runs
    (`_carries_build_part`). It reaches the closed-set `else` below only if that gate is ever
    bypassed, which is the right answer anyway."""
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


def _carries_build_part(message: Any) -> bool:
    """True if an inbound message carries a `build` part — which no honest client ever does.

    A `build` part is the SERVER'S record of what a build produced. `services/build_sessions/
    outcome.py` writes it straight through the ORM model, never through this route, and no portal
    code path appends one (the live outcome card renders from memory and is deliberately not
    persisted). So the shape has no legitimate inbound producer, and it was never inert:

      * `attachments.py::_last_build_boundary` reads a build part's `startedSeq` to decide which
        of the user's files the NEXT build sees — a forged part moves that boundary and silently
        drops their own attachments;
      * `outcome.py::_already_recorded` keys on `sessionId` to decide the outcome is already
        written — a forged part makes the real writer return early, so the true outcome of that
        build is never recorded at all.

    Self-scoped (a caller can only write into their own thread), but "you can only corrupt your
    own transcript" is not a reason to accept a write to a server-owned surface.
    """
    if not isinstance(message, dict):
        return False
    parts = message.get("parts")
    if not isinstance(parts, list):
        return False
    return any(isinstance(part, dict) and part.get("type") == "build" for part in parts)


def _validate_office_part(part: dict[str, Any]) -> str | None:
    # `format` is a CLOSED set, not free text: the upload route derives it from the file's own
    # OPC structure and can only ever emit these two (`extract/office.py`; `powerpoint` is the
    # separate deck kind). Bound it here because it is not merely displayed — the build path
    # interpolates it into the model-facing `<attachment type="…">` fence
    # (`build_sessions/attachments.py::_fence`), and untrusted input belongs in a closed set at
    # the boundary, not sanitized on the way out (parse, don't validate).
    if part.get("format") not in _OFFICE_FORMATS:
        return "an office file part has an invalid format"
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
    if not _is_whole_number(message["seq"]):
        return "message.seq must be an integer"
    if not _fits_int32(message["seq"]):
        return "message.seq is out of range"
    # Absent → the documented `1` default; present-but-malformed is a client bug.
    schema_version = message.get("schemaVersion")
    if schema_version is not None:
        if not _is_whole_number(schema_version):
            return "message.schemaVersion must be an integer"
        if not _fits_int32(schema_version):
            return "message.schemaVersion is out of range"
    return _validate_parts(message.get("parts"))


async def _free_seq(db: DbSession, conversation_uuid: uuid.UUID, requested: int) -> int:
    """The seq this turn actually gets: the requested one when free, else the transcript's end.

    Reallocation continues the transcript (`max + 1`); it deliberately does NOT hunt for a hole in
    a gappy transcript, because seq IS the ordering — backfilling a gap would move a turn backwards
    in time, in front of turns the model has already answered.
    """
    taken = await db.scalar(
        sa.select(Message.id).where(
            Message.conversation_id == conversation_uuid, Message.seq == requested
        )
    )
    if taken is None:
        return requested
    highest = await db.scalar(
        sa.select(sa.func.max(Message.seq)).where(Message.conversation_id == conversation_uuid)
    )
    if highest is None:
        return requested
    reallocated = int(highest) + 1
    if not _fits_int32(reallocated):
        # The SAME boundary the client's own `seq` is held to (`_validate_message_input`), applied
        # to the value WE pick. Without it the asymmetry is a bare 500: an out-of-range seq makes
        # asyncpg raise a DataError, not an IntegrityError, so it sails straight past the collision
        # handler below — the exact hazard the comment there already names, reintroduced by the
        # reallocation path that was added after it.
        #
        # The transient code is the honest one of the route's two 409s. It is TRUE that nothing was
        # written, and that is the half the portal acts on: it keeps the user's draft and says so.
        # The code-less 409 means "already saved", which here would be a flat lie AND would send
        # them to reload, destroying the text. Retrying cannot actually help (this conversation's
        # seq space is spent — only reachable once a message already holds seq 2**31-1), but being
        # told to retry beats being told a message that vanished was stored.
        raise AppApiError(
            409, "Could not store this message — please try again.", code=_SEQ_CONFLICT_CODE
        )
    return reallocated


@router.post(
    "/{conversation_id}/messages",
    status_code=201,
    response_model=AppendResponse,
    responses=error_responses(
        (400, ErrorEnvelope, "Invalid conversation id, message, or header"),
        (404, ErrorEnvelope, "header.projectId not found (or not owned by the caller)"),
        (
            409,
            ErrorEnvelope,
            "PERMANENT: conversation id or message._id already in use (retrying cannot help). "
            f"TRANSIENT (`code: {_SEQ_CONFLICT_CODE}`): a concurrent append took the seq and "
            "nothing was written — retry. Branch on `code`, never on the message.",
        ),
        (422, ErrorEnvelope, "The message carries a `build` part, which only the server writes"),
        AUTH_401,
    ),
)
async def append_message(
    conversation_id: str, request: Request, user: CurrentUser, db: DbSession
) -> JSONResponse:
    if not _ID_RE.match(conversation_id):
        raise AppApiError(400, "Invalid conversation id.")
    body = await _json_object_body(request)

    message = body.get("message")
    # Ahead of shape validation on purpose: the answer to a `build` part is "this is not yours to
    # write", whatever shape it arrived in — never a 400 implying a better-formed one would land.
    if _carries_build_part(message):
        raise AppApiError(422, "A build outcome is recorded by the server, not by a client.")
    if (message_error := _validate_message_input(message)) is not None:
        raise AppApiError(400, message_error)
    # Validation guarantees a dict; this redundant narrow satisfies the type checker.
    if not isinstance(message, dict):
        raise AppApiError(400, "message is required")
    header = body.get("header")
    if not isinstance(header, dict) or header.get("kind") not in _KINDS:
        raise AppApiError(400, "header.kind must be planning, assistant, or builder.")

    # ID_RE-valid but non-UUID tokens can't key our Uuid columns (the SPA only mints UUIDs).
    conversation_uuid = _as_uuid(conversation_id)
    if conversation_uuid is None:
        raise AppApiError(400, "Invalid conversation id.")
    message_uuid = _as_uuid(message["_id"])
    if message_uuid is None:
        raise AppApiError(400, "message._id is invalid")

    now = datetime.datetime.now(datetime.UTC)
    # Parse the whole header + the message timestamp BEFORE touching the session, so a
    # malformed field can never leave a half-applied write behind (parse, don't validate).
    header_title = _optional_str(header.get("title"), "header.title")
    header_context = _optional_dict(header.get("context"), "header.context")
    header_created_at = _optional_iso(header.get("createdAt"), now, "header.createdAt")
    message_created_at = _optional_iso(message.get("createdAt"), now, "message.createdAt")
    schema_version = message.get("schemaVersion")

    # Header: a cross-user id collision is a 409 (write-IDOR closed), never a silent overwrite.
    existing = await db.scalar(sa.select(Conversation).where(Conversation.id == conversation_uuid))
    if existing is not None and existing.user_id != user.id:
        raise AppApiError(409, "Conversation id already in use.")
    if existing is None:
        # Bind the new conversation to its project (R2/KD-4) — ONLY on the create branch;
        # the upsert branch never re-parents. Project-first: header.projectId is REQUIRED
        # (every chat lives in a project — there is no fallback) and must be owned by the
        # caller (a missing/cross-user project is the same non-leaking 404, ADR-0004).
        raw_project_id = header.get("projectId")
        if raw_project_id is None:
            raise AppApiError(400, "header.projectId is required")
        project_uuid = _as_uuid(raw_project_id)
        if project_uuid is None:
            raise AppApiError(400, "header.projectId is invalid")
        project = await owned_project_or_404(db, user.id, project_uuid)
        db.add(
            Conversation(
                id=conversation_uuid,
                user_id=user.id,
                project_id=project.id,
                kind=ConversationKind(header["kind"]),
                title=header_title,
                context=header_context,
                created_at=header_created_at,
            )
        )
    else:
        if header_title is not None:
            existing.title = header_title
        if "context" in header:
            # An explicit `null` clears the (nullable) stored context — the client asked for
            # it. A present wrong type already raised above, so this is never a silent wipe.
            existing.context = header_context
        # Touch so every append surfaces the conversation as recent (Express $sets updatedAt).
        existing.updated_at = now

    seq = int(message["seq"])
    # Message insert is idempotent on a replay of the CALLER'S OWN prior insert into THIS
    # conversation (Express swallows the dup-key as success). The owner + conversation
    # predicates are load-bearing: without them a client-minted `_id` colliding with any
    # other user's message short-circuits into a false 201 that writes nothing (ADR-0004 —
    # a dropped scope predicate is a cross-user leak). A foreign or cross-conversation id
    # now misses the short-circuit and falls through to the insert, whose PK constraint
    # raises below.
    # The try covers the `already` SELECT too: its autoflush is what actually INSERTs a
    # newly-added header, so a create-branch FK violation (project deleted mid-append)
    # surfaces there, before the commit.
    try:
        already = await db.scalar(
            sa.select(Message).where(
                Message.id == message_uuid,
                Message.user_id == user.id,
                Message.conversation_id == conversation_uuid,
            )
        )
        if already is None:
            # THE CLIENT'S `seq` IS A HINT, NOT A CLAIM. It is honoured when free and reallocated
            # to the end of the transcript when taken. It used to be taken literally, and a
            # collision was read as a replay — answered 201, wrote nothing. That was sound only
            # while the SPA was the ONLY writer, which stopped being true when a build's end
            # sequence began recording outcomes (`build_sessions/outcome.py`). Two writers
            # allocating `max+1` independently WILL collide (the SPA counts slots it has reserved
            # but not yet persisted; the server counts rows), and the collision has no symptom:
            # the caller is told 201 while its turn is dropped. Resolved BEFORE the insert so the
            # ordinary path is one SELECT and no exception; the IntegrityError arm below is left
            # for a genuine concurrent race.
            seq = await _free_seq(db, conversation_uuid, seq)
            db.add(
                Message(
                    id=message_uuid,
                    conversation_id=conversation_uuid,
                    user_id=user.id,
                    role=MessageRole(message["role"]),
                    seq=seq,
                    schema_version=1 if schema_version is None else int(schema_version),
                    parts=message["parts"],
                    created_at=message_created_at,
                )
            )
        else:
            # A true replay reports the seq the turn ALREADY has — the client re-seeds its
            # counter from this, so echoing the requested seq would re-collide forever.
            seq = already.seq
        await db.commit()
    except StaleDataError:
        # The conversation was deleted between our upsert-branch load and this flush — the
        # flush's `updated_at` UPDATE then matches zero rows. The loser of that race gets the
        # same non-leaking 404 an append one second later would, mirroring patch_conversation
        # (and the create-branch FK handling below), never a bare 500. `get_db` owns the
        # rollback of the failed transaction (as every `raise` here does).
        raise AppApiError(404, "Conversation not found.") from None
    except IntegrityError as exc:
        violated = str(exc.orig)
        # The seq we resolved as free was taken between the check and the commit — a genuine
        # concurrent append (the SPA and a build's end sequence both write here now).
        #
        # This used to answer 201 and write nothing, on the assumption that a taken seq meant a
        # replay of the same turn. That was sound only while the SPA was the ONLY writer; with two
        # writers it silently drops a real message while telling the caller it was stored. A
        # replay is identified by `_id` (handled above), so anything reaching here is a DISTINCT
        # turn that lost a race — and the honest answer is to say so. The caller retries; a 409 it
        # can act on beats a 201 that lied.
        if "uq_messages_conversation_seq" in violated:
            raise AppApiError(
                409,
                "Could not store this message — please try again.",
                code=_SEQ_CONFLICT_CODE,
            ) from exc
        # The message `_id` is already taken — by another user, another conversation, or a
        # concurrent insert of the same id. Name the real defect instead of blaming the
        # conversation id; the whole append (header included) rolls back with the txn.
        if "messages_pkey" in violated:
            raise AppApiError(409, "message._id is already in use.") from exc
        # The parent vanished mid-append — the project deleted between its ownership
        # check and this commit (create branch), or the conversation deleted under the
        # message insert (upsert branch). The loser of that race gets the same
        # non-leaking 404 an append one second later would, not the misleading 409.
        if "project_id_fkey" in violated or "conversation_id_fkey" in violated:
            raise AppApiError(404, "Conversation not found.") from exc
        # Otherwise a concurrent insert of the same conversation id won — surface as a 409.
        raise AppApiError(409, "Conversation id already in use.") from exc
    return JSONResponse(
        status_code=201, content={"ok": True, "message": {"_id": str(message_uuid), "seq": seq}}
    )


# --- U9: delete with cleanup --------------------------------------------------


@router.delete(
    "/{conversation_id}",
    response_model=OkResponse,
    responses=error_responses(
        (400, ErrorEnvelope, "Invalid conversation id"),
        (404, ErrorEnvelope, "Conversation not found"),
        AUTH_401,
    ),
)
async def delete_conversation(
    conversation_id: str, user: CurrentUser, db: DbSession, storage: StorageDep
) -> JSONResponse:
    owned = await _load_owned(db, user.id, conversation_id)

    # Delete the rows (attachments + conversation + cascaded messages) INSIDE the txn,
    # commit, and only THEN best-effort sweep the object-store blobs — so a rolled-back
    # delete never destroys a blob a restored row still points at (KD-3 rollback safety).
    blob_keys = await gather_and_delete_conversation(db, owned, user_id=user.id)
    await db.commit()
    await sweep_blobs(storage, blob_keys)
    return JSONResponse(content={"ok": True})
