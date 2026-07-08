"""The `messages` table — one row per chat turn, ordered by the client-minted `seq` (R14).

Persistence is SPA-driven (the `/api/claude` chat path is a stateless relay that persists
nothing; the SPA appends each turn via the conversations API — see the U8 work notes). So the
durable record is the SPA's neutral message shape: `id`/`seq`/`role`/`schema_version` promoted
to real columns and the content `parts[]` array stored opaquely in JSONB `parts`. This is the
Open-Question-P1 recommended default — round-trippable and forward-compatible WITHOUT changing
the request contract (R1). Ordering is by `seq` ALONE — a client-minted monotonic per-conversation
counter (user=N, assistant=N+1), mirroring Express `messages-repo.js` (`created_at` is not a key).

NOTE (deliberate divergence from the plan's aspirational schema): no per-message token columns
and no `pydantic_ai_version`. Token accounting lives in `token_usage` (U6) exactly as Express
keeps it off the message doc; and `parts` is the SPA's parts shape (versioned by
`schema_version`), not Pydantic-AI-native serialization, so a pydantic-ai version stamp would be
dead schema. If the server later becomes authoritative for history, add them then.
"""

from __future__ import annotations

import uuid
from enum import StrEnum
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base
from src.db.mixins import OwnedByUserMixin, TimestampMixin, UUIDv7PrimaryKeyMixin


class MessageRole(StrEnum):
    """The two message roles (Express `role`, verbatim strings). Native PG enum labels."""

    USER = "user"
    ASSISTANT = "assistant"


# Native PG enum, shared by the model column and the migration (create_type=False — the
# migration owns CREATE/DROP TYPE). Mirrors `conversation_kind_enum`.
message_role_enum = sa.Enum(
    MessageRole,
    name="message_role",
    values_callable=lambda enum: [member.value for member in enum],
    create_type=False,
)


class Message(UUIDv7PrimaryKeyMixin, TimestampMixin, OwnedByUserMixin, Base):
    __tablename__ = "messages"

    __table_args__ = (
        # `seq` is the SOLE per-conversation ordering key — enforce its uniqueness so a
        # duplicated turn is a caught IntegrityError (idempotent-success), not silent
        # ordering corruption.
        sa.UniqueConstraint("conversation_id", "seq", name="uq_messages_conversation_seq"),
    )

    # FK to the owning conversation; a deleted conversation cascades its messages away.
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[MessageRole] = mapped_column(message_role_enum, nullable=False)
    # Client-minted monotonic per-conversation counter — the SOLE ordering key.
    seq: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    # The SPA's message schema version (default 1) — the version stamp for `parts`.
    schema_version: Mapped[int] = mapped_column(
        sa.Integer, server_default=sa.text("1"), nullable=False
    )
    # The content parts array, stored opaquely (text/image/document/office/deck parts).
    parts: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
