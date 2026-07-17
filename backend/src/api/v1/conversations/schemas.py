"""Conversations wire-shape schemas — the SPA's Mongo-style `_id` + camelCase envelopes.

Every conversations route returns a pre-built `JSONResponse` (to emit the exact Express
wire shape and the `{"error":{"message"}}` envelope), so the RESPONSE models here are
DOCUMENTED-ONLY: FastAPI advertises them in OpenAPI but never validates or reshapes the
response — the characterization tests are the byte-identical guard. `_id` needs an
explicit alias because Pydantic treats a leading-underscore field name as private.

The one exception is `BuilderThreadRequest`: the canonical-thread route is net-new (it has
no Express body contract to byte-match), so its body IS parsed through the model and its
validation errors are FastAPI's own 422.
"""

from __future__ import annotations

import uuid
from typing import Any

from pydantic import Field

from src.schemas import CamelModel


class HeaderOut(CamelModel):
    """One conversation header. `title`/`context`/`code` are omitted when unset — the
    route's `_header_dict` builds them in only when present. This model is
    documented-only (the route returns a pre-built `JSONResponse`), so no exclude-unset
    serialization flag is involved; the `= None` defaults are what document those fields
    as non-required."""

    id: str = Field(alias="_id")
    project_id: str
    kind: str
    created_at: str
    updated_at: str
    title: str | None = None
    context: Any = None
    code: Any = None


class MessageOut(CamelModel):
    """One message — `_id`, role, the JSONB `parts`, seq, createdAt (`_message_dict`)."""

    id: str = Field(alias="_id")
    role: str
    parts: Any
    seq: int
    created_at: str


class ConversationListResponse(CamelModel):
    conversations: list[HeaderOut]


class BuilderThreadRequest(CamelModel):
    """The `POST /conversations/builder-thread` body — the project whose canonical build
    thread to resolve. Unlike the rest of this module these two ARE live models: the route
    parses its body through `BuilderThreadRequest` and hand-builds only the response (to keep
    the `_id` wire shape), so a malformed `projectId` is FastAPI's 422, not a hand-rolled 400."""

    project_id: uuid.UUID


class BuilderThreadResponse(CamelModel):
    """The project's ONE canonical builder conversation — resolved or freshly created."""

    conversation: HeaderOut


class ConversationDetailResponse(CamelModel):
    conversation: HeaderOut
    messages: list[MessageOut]


class AppendedMessage(CamelModel):
    id: str = Field(alias="_id")
    seq: int


class AppendResponse(CamelModel):
    ok: bool
    message: AppendedMessage
