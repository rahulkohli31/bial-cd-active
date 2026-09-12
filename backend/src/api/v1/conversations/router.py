"""Conversations HTTP endpoints — user-scoped chat headers (list / get / patch / delete)
plus the project's canonical builder thread.

The legacy SPA message append/read endpoints are gone: the `messages` table holds native
pydantic-ai batches written by the server, and the browser never persists a transcript.
What remains is the header CRUD the SPA still drives, in the Express wire shape — Mongo-style
`_id` (the SPA normalizes `_id → id`), camelCase timestamps, and the `{error:{message}}`
envelope. Identity is ALWAYS the authenticated caller and every query is scoped by `user_id`.

No route here changes what a chat is; the kind is fixed at creation. Building from a plan
creates a SECOND chat (`transition.py`) rather than mutating the plan chat.
"""

from __future__ import annotations

import datetime
import re
import uuid
from typing import Annotated, Any

import sqlalchemy as sa
from fastapi import APIRouter, Depends, Query, Request
from fastapi.responses import JSONResponse
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm.exc import StaleDataError

from src.api.deps import CurrentUser, DbSession
from src.api.deps_csrf import RequireCsrf
from src.api.v1.attachments.router import storage_dependency
from src.api.v1.conversations.schemas import (
    ConversationCreateRequest,
    ConversationCreateResponse,
    ConversationDetailResponse,
    ConversationListResponse,
)
from src.core.errors import AppApiError
from src.db.models.conversation import ChatKind, Conversation
from src.schemas import AUTH_401, ErrorEnvelope, OkResponse, error_responses
from src.services.conversations import gather_and_delete_conversation
from src.services.messages.projection import measured_context_tokens, project_conversation
from src.services.messages.store import load_rows
from src.services.projects import owned_project_or_404
from src.services.storage import ObjectStorage, sweep_blobs
from src.services.turns.engine import get_turn_engine

router = APIRouter(prefix="/conversations", tags=["conversations"])

# All conversations routes are cookie-authed (`current_user`, 401 DetailBody); their own
# raises are `AppApiError` -> ErrorEnvelope. The shared 401 spec (`AUTH_401`) is reused across
# routes.

# Client-minted id shape (Express `ID_RE`) — a safe key token, no `/` or `..`.
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
# The valid `?kind` values.
_KINDS = {k.value for k in ChatKind}
# Newest-first list cap (Express limit).
_LIST_LIMIT = 200
# A conversation-owned storage handle for the delete sweep (swappable in tests).
StorageDep = Annotated[ObjectStorage, Depends(storage_dependency)]


async def _json_object_body(request: Request) -> dict[str, Any]:
    """The request body as a JSON object, or a 400 naming the real defect.

    Coercing an unparseable / non-object body to `{}` turns a lost write into a cheerful
    success: a truncated builder auto-save would PATCH nothing and still answer `200 {ok:true}`.
    Parse once at the boundary instead."""
    try:
        body: Any = await request.json()
    except (ValueError, TypeError):  # fmt: skip  # ruff py314 strips parens
        raise AppApiError(400, "Invalid JSON body.") from None
    if not isinstance(body, dict):
        raise AppApiError(400, "Request body must be a JSON object.")
    return body


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
        # The parent project — the SPA resolves it to the breadcrumb
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
    return header


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
        query = query.where(Conversation.kind == ChatKind(kind))
    # Optional project scope. Already user-scoped, so a cross-user project_id simply
    # returns nothing — no leak, no separate ownership check needed.
    if project_id is not None:
        query = query.where(Conversation.project_id == project_id)
    query = query.order_by(Conversation.updated_at.desc()).limit(_LIST_LIMIT)
    rows = (await db.execute(query)).scalars().all()
    return JSONResponse(content={"conversations": [_header_dict(c) for c in rows]})


# --- the project's ONE canonical builder thread -------------------------------


@router.post(
    "",
    status_code=201,
    response_model=ConversationCreateResponse,
    dependencies=[RequireCsrf],
    responses=error_responses(
        AUTH_401,
        (403, ErrorEnvelope, "CSRF check failed"),
        (404, ErrorEnvelope, "Project not found (or not owned by the caller)"),
        (409, ErrorEnvelope, "The conversation id is already in use"),
    ),
)
async def create_conversation(
    body: ConversationCreateRequest, user: CurrentUser, db: DbSession
) -> JSONResponse:
    """Create a conversation row BEFORE its first turn.

    The stateless-turn relay 404s an unknown conversation, so the SPA creates the row it just
    minted, then streams. Idempotent per owner: re-POSTing the same id with the
    same parentage answers 200 with the existing header (a retry, a second tab), while an id
    that exists under ANYONE else or under different parentage is a 409 — one arm, one
    message, so existence under another owner is never distinguishable from a parentage
    conflict, matching the platform's single-tenant, owner-scoped isolation rule."""
    existing = await db.get(Conversation, body.id)
    if existing is None:
        project = await owned_project_or_404(db, user.id, body.project_id)
        row = Conversation(
            id=body.id,
            user_id=user.id,
            project_id=project.id,
            kind=body.kind,
            title=body.title,
            context=body.context,
        )
        db.add(row)
        try:
            # Refresh through the flush before projecting (server-default timestamps would
            # MissingGreenlet on a fresh row otherwise — the builder-thread pattern).
            await db.flush()
        except IntegrityError:
            # Two tabs raced the same mint — the winner's row is the truth; fall through to
            # the idempotency arm below on a fresh load.
            await db.rollback()
            existing = await db.get(Conversation, body.id)
        else:
            await db.refresh(row)
            await db.commit()
            return JSONResponse(status_code=201, content={"conversation": _header_dict(row)})

    if (
        existing is not None
        and existing.user_id == user.id
        and existing.project_id == body.project_id
        and existing.kind == body.kind
    ):
        return JSONResponse(content={"conversation": _header_dict(existing)})
    raise AppApiError(409, "This conversation id is already in use.")


async def _load_owned(db: DbSession, user_id: uuid.UUID, conversation_id: str) -> Conversation:
    """Resolve a caller-owned conversation from a path id, or RAISE the matching error:
    400 for a malformed id token, 404 for a well-formed id that resolves to nothing the
    caller owns (owner-scoped — a cross-user id is indistinguishable from a missing one)."""
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
    """The conversation header + the display projection — everything a reopened chat
    needs, in one read. The projection is THE derivation (`services/messages/projection.py`);
    the live catch-up snapshot reuses it, so reload and live can never disagree.

    `activeTurn`: the in-process turn registry's answer — `{turnId, lastSeq}` while a
    turn runs (the cursor a subscriber resumes `GET /events` from), null when settled.

    `contextTokens`: how full the chat is — the provider's raw prompt count for the largest
    turn served here, the same figure the send route refuses on. Null when nothing has been
    measured yet, which a client should read as unknown rather than as empty.
    """
    owned = await _load_owned(db, user.id, conversation_id)
    # include_hidden=True: hidden rows render nothing, but the projection needs the unclosed
    # `build_started` markers to derive the in-progress anchor (crashed/mid-build reloads).
    rows = await load_rows(db, user_id=user.id, conversation_id=owned.id, include_hidden=True)
    items = await project_conversation(db, user_id=user.id, rows=rows)
    active = get_turn_engine().active_turn_info(owned.id)
    return JSONResponse(
        content={
            "conversation": _header_dict(owned),
            "projection": [item.model_dump(mode="json", by_alias=True) for item in items],
            "activeTurn": (
                {"turnId": str(active.turn_id), "lastSeq": active.last_seq}
                if active is not None
                else None
            ),
            # The SAME rows the projection above walked — one read, two derivations, so the
            # transcript on screen and the meter under it can never describe different chats.
            "contextTokens": measured_context_tokens(rows),
        }
    )


@router.patch(
    "/{conversation_id}",
    response_model=OkResponse,
    responses=error_responses(
        (400, ErrorEnvelope, "Invalid conversation id or title"),
        (404, ErrorEnvelope, "Conversation not found"),
        AUTH_401,
    ),
)
async def patch_conversation(
    conversation_id: str, request: Request, user: CurrentUser, db: DbSession
) -> JSONResponse:
    """Update the mutable header fields the SPA owns: `title` and `context`. The legacy `code`
    snapshot is gone with its column (0024) — code truth lives in the build snapshots
    (`app_registry.current_code` followed it in migration 0039); a body that still sends
    `code` gets a 400 naming the retirement, not a silent ignore."""
    if not _ID_RE.match(conversation_id):
        raise AppApiError(400, "Invalid conversation id.")
    body = await _json_object_body(request)

    if "code" in body:
        raise AppApiError(400, "code snapshots are no longer stored on conversations.")

    owned = await _load_owned(db, user.id, conversation_id)

    # Apply only the fields present in the body (absent ≠ null — `key in body` distinguishes).
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


# --- delete with cleanup ------------------------------------------------------


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
    # delete never destroys a blob a restored row still points at (rollback safety).
    blob_keys = await gather_and_delete_conversation(db, owned, user_id=user.id)
    await db.commit()
    await sweep_blobs(storage, blob_keys)
    return JSONResponse(content={"ok": True})
