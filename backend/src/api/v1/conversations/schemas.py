"""Conversations wire-shape schemas — the SPA's Mongo-style `_id` + camelCase envelopes.

Every conversations route returns a pre-built `JSONResponse` (to emit the exact Express
wire shape and the `{"error":{"message"}}` envelope), so the RESPONSE models here are
DOCUMENTED-ONLY: FastAPI advertises them in OpenAPI but never validates or reshapes the
response — the characterization tests are the byte-identical guard. `_id` needs an
explicit alias because Pydantic treats a leading-underscore field name as private.

The legacy message-append/read schemas died with their endpoints (U4's destructive reset);
the projection read shape joins in U6.

The one exception is `BuilderThreadRequest`: the canonical-thread route is net-new (it has
no Express body contract to byte-match), so its body IS parsed through the model and its
validation errors are FastAPI's own 422.
"""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import Field

from src.db.models.conversation import ConversationKind
from src.schemas import CamelModel
from src.services.messages.projection import DisplayItem


class HeaderOut(CamelModel):
    """One conversation header. `title`/`context` are omitted when unset — the route's
    `_header_dict` builds them in only when present. `mode` is the server-owned sticky chat
    mode (U4). This model is documented-only (the route returns a pre-built `JSONResponse`),
    so no exclude-unset serialization flag is involved; the `= None` defaults are what
    document those fields as non-required."""

    id: str = Field(alias="_id")
    project_id: str
    kind: str
    mode: str
    created_at: str
    updated_at: str
    title: str | None = None
    context: Any = None


class ConversationListResponse(CamelModel):
    conversations: list[HeaderOut]


class ConversationCreateRequest(CamelModel):
    """`POST /conversations` — create the row BEFORE the first turn (U7). The id stays
    client-minted (`crypto.randomUUID`, the R14 model); the server validates ownership of the
    parent project and makes the call idempotent per owner, so the SPA's synchronous
    mint-then-navigate flow needs no extra round trip on a retry."""

    id: uuid.UUID
    project_id: uuid.UUID
    kind: ConversationKind
    title: str | None = None
    context: Any = None


class ConversationCreateResponse(CamelModel):
    conversation: HeaderOut


class BuilderThreadRequest(CamelModel):
    """The `POST /conversations/builder-thread` body — the project whose canonical build
    thread to resolve. Unlike the rest of this module these two ARE live models: the route
    parses its body through `BuilderThreadRequest` and hand-builds only the response (to keep
    the `_id` wire shape), so a malformed `projectId` is FastAPI's 422, not a hand-rolled 400."""

    project_id: uuid.UUID


class BuilderThreadResponse(CamelModel):
    """The project's ONE canonical builder conversation — resolved or freshly created."""

    conversation: HeaderOut


class ActiveTurnOut(CamelModel):
    """The in-flight turn, when one is running (U10 wires the real registry; until then the
    route always answers null). `last_seq` is the turn's newest event seq — the cursor a
    subscriber resumes the event stream from."""

    turn_id: str
    last_seq: int


class ConversationDetailResponse(CamelModel):
    """Header + display projection + the U10 `activeTurn` seam — one read rebuilds the chat.
    `projection` items are the `services/messages/projection.py` models (the single
    history→display derivation; documented here, produced there)."""

    conversation: HeaderOut
    projection: list[DisplayItem]
    active_turn: ActiveTurnOut | None = None
