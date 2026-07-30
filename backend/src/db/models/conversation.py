"""The `conversations` table — one chat, keyed by a CLIENT-MINTED id, with server-owned mode.

A conversation is minted by the SPA (`crypto.randomUUID`) and its id is externally
meaningful: a *builder* conversation's id IS the deployed `appId` (Plan B's
`app_registry.conversation_id` soft-links to it), so the id must be PRESERVED across the
migration (R4) — the SPA supplies it on the first append, never the server. The
`UUIDv7PrimaryKeyMixin` default only covers a hypothetical server-minted row.

`kind` is a native PG enum (ADR-0008); it retires with the builder-thread endpoint (U13).
`mode` is the unified chat's STICKY, SERVER-OWNED mode (U4 / plan 2026-07-22-002): tool
gating derives from THIS column, never from anything the client sends, and it changes only
between turns through the explicit switch endpoint (or the atomic Build-it transition).
`title`/`context` are the mutable header fields the SPA owns. The legacy `code` JSONB (the
single builder code snapshot) was dropped in migration 0024 — code truth lives in
`app_registry.current_code` and the build snapshots.
Ownership is `user_id` (ADR-0004) — every read scoped by it.
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


class ConversationKind(StrEnum):
    """The three chat kinds (Express `KINDS`, verbatim strings). Native PG enum labels —
    stable, professional, safe in API responses."""

    PLANNING = "planning"
    ASSISTANT = "assistant"
    BUILDER = "builder"


class ConversationMode(StrEnum):
    """The unified chat's three modes (D1). Server-owned, sticky between turns: the per-mode
    toolset registry (U8) keys on this value, so a wrong-mode tool is structurally absent —
    never merely prompt-forbidden."""

    ASK = "ask"
    PLAN = "plan"
    WRITE = "write"


# Native PG enums, shared by the model columns and the Alembic migrations. `create_type=False`:
# the owning migration runs CREATE/DROP TYPE explicitly (so a downgrade drops it) — the column
# must not try to create the type itself. Mirrors `app_registry.app_status_enum`.
conversation_kind_enum = sa.Enum(
    ConversationKind,
    name="conversation_kind",
    values_callable=lambda enum: [member.value for member in enum],
    create_type=False,
)

conversation_mode_enum = sa.Enum(
    ConversationMode,
    name="conversation_mode",
    values_callable=lambda enum: [member.value for member in enum],
    create_type=False,
)


class Conversation(UUIDv7PrimaryKeyMixin, TimestampMixin, OwnedByUserMixin, Base):
    __tablename__ = "conversations"

    # The parent project (R2, KD-4). Every conversation — every kind — is a *session*
    # under exactly one project. NOT NULL FK; the DB cascade is a row backstop only
    # (blob-aware cleanup runs through the U6 service, KD-3a). `user_id` remains the
    # isolation predicate (ADR-0004); `project_id` is organizational, not tenancy, and
    # a project and its children always share the same `user_id`.
    project_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid,
        sa.ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    kind: Mapped[ConversationKind] = mapped_column(conversation_kind_enum, nullable=False)
    # The sticky server-owned chat mode (D1). Defaults to `plan` — the root Build box mints
    # new chats in Plan mode (U13), and a mode the server never recorded must still be a
    # DEFINED mode (fail-first: no nullable "unknown" state for a gating input).
    mode: Mapped[ConversationMode] = mapped_column(
        conversation_mode_enum,
        nullable=False,
        server_default=sa.text("'plan'::conversation_mode"),
    )
    # Derived client-side from the first message; mutable via PATCH. TEXT (short in
    # practice — the SPA caps it ~40 chars — but unbounded here).
    title: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    # Opaque builder-generation settings ({theme, uploadedFiles, dataSchema, …}) — stored
    # verbatim, never inspected by the server.
    context: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
