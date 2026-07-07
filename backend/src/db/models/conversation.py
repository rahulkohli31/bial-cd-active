"""The `conversations` table — one row per chat, keyed by the CLIENT-MINTED id (R14, R4).

A conversation is minted by the SPA (`crypto.randomUUID`) and its id is externally
meaningful: a *builder* conversation's id IS the deployed `appId` (Plan B's
`app_registry.conversation_id` soft-links to it), so the id must be PRESERVED across the
migration (R4) — the SPA supplies it on the first append (U9), never the server. The
`UUIDv7PrimaryKeyMixin` default only covers a hypothetical server-minted row; in the faithful
port every id arrives client-minted (a valid `uuid`, v4 today / v7 for backfilled rows).

`kind` is a native PG enum (ADR-0008). `title`/`context`/`code` are the mutable header fields
the SPA owns: `context` is opaque builder-generation settings, `code` holds the single builder
code snapshot (`{current: {...}}`). Ownership is `user_id` (ADR-0004) — every read scoped by it.
Stored to LAST, not to mirror Cosmos: the Cosmos shape does not constrain these columns.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base
from src.db.mixins import OwnedByUserMixin, TimestampMixin, UUIDv7PrimaryKeyMixin


class ConversationKind(StrEnum):
    """The three chat kinds (Express `KINDS`, verbatim strings). Native PG enum labels —
    stable, professional, safe in API responses."""

    PLANNING = "planning"
    ASSISTANT = "assistant"
    BUILDER = "builder"


# Native PG enum, shared by the model column and the Alembic migration. `create_type=False`:
# the migration owns CREATE/DROP TYPE explicitly (so a downgrade drops it) — the column must
# not try to create the type itself. Mirrors `app_registry.app_status_enum`.
conversation_kind_enum = sa.Enum(
    ConversationKind,
    name="conversation_kind",
    values_callable=lambda enum: [member.value for member in enum],
    create_type=False,
)


class Conversation(UUIDv7PrimaryKeyMixin, TimestampMixin, OwnedByUserMixin, Base):
    __tablename__ = "conversations"

    kind: Mapped[ConversationKind] = mapped_column(conversation_kind_enum, nullable=False)
    # Derived client-side from the first message; mutable via PATCH. TEXT (short in
    # practice — the SPA caps it ~40 chars — but unbounded here).
    title: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    # Opaque builder-generation settings ({theme, uploadedFiles, dataSchema, …}) — stored
    # verbatim, never inspected by the server.
    context: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    # The single builder code snapshot: {current: {source, entry, createdAt?, model?}}.
    # Only builder conversations carry it; NULL otherwise.
    code: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
