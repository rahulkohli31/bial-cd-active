"""Conversations wire-shape schemas — the SPA's Mongo-style `_id` + camelCase envelopes.

Every conversations route returns a pre-built `JSONResponse` (to emit the exact Express
wire shape and the `{"error":{"message"}}` envelope), so these models are
DOCUMENTED-ONLY: FastAPI advertises them in OpenAPI but never validates or reshapes the
response — the characterization tests are the byte-identical guard. `_id` needs an
explicit alias because Pydantic treats a leading-underscore field name as private.
"""

from __future__ import annotations

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


class ConversationDetailResponse(CamelModel):
    conversation: HeaderOut
    messages: list[MessageOut]


class AppendedMessage(CamelModel):
    id: str = Field(alias="_id")
    seq: int


class AppendResponse(CamelModel):
    ok: bool
    message: AppendedMessage
