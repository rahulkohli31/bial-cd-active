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


class ChatKind(StrEnum):
    """What a chat IS — chosen when it is created and never changed (R14/R15).

    This replaced two independent three-valued enums that between them decided one thing: what
    a run is allowed to do. One of them (planning / assistant / builder) gated nothing but a
    legacy relay's base prompt. The other (ask / plan / write) gated the toolset — correctly —
    and also selected prompt segments, drove a per-turn restatement cadence, decided whether
    narration reached the screen, decided which copy of the app a turn read, and was stamped on
    every stored row.

    THE KIND HAS EXACTLY TWO READERS AND BOTH ARE NAMED, because "one module reads it" would be
    nearly-true rather than true. `services/agent/toolsets.py` reads it to decide what the model
    CAN DO — the guardrail. A Plan chat cannot change the app because `write_file`, `edit_file`,
    `insert_lines`, `apply_schema_change`, the sandbox-routed `run_command` and `declare_done`
    are not in the list handed to that run, never because something downstream notices which
    kind of chat it is. `services/turns/engine.py` reads it once more, to select a HARNESS
    SHAPE: the node loop with its per-step billing fold versus a single `chat_agent.run`, and
    with it the run's `output_type`. Everything else that holds a kind is stamping a row.
    """

    PLAN = "plan"
    BUILD = "build"


# Native PG enum, shared by the model columns and the Alembic migrations. `create_type=False`:
# the owning migration runs CREATE/DROP TYPE explicitly (so a downgrade drops it) — the column
# must not try to create the type itself. Mirrors `app_registry.app_status_enum`.
chat_kind_enum = sa.Enum(
    ChatKind,
    name="chat_kind",
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
    # Fixed at creation and never changed: there is no route that mutates it (R14/R15), and
    # no server default — a chat whose kind the creator did not choose is a programming error,
    # not a chat that quietly becomes one of them (fail-first).
    kind: Mapped[ChatKind] = mapped_column(chat_kind_enum, nullable=False)
    # Derived client-side from the first message; mutable via PATCH. TEXT (short in
    # practice — the SPA caps it ~40 chars — but unbounded here).
    title: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    # Opaque builder-generation settings ({theme, uploadedFiles, dataSchema, …}) — stored
    # verbatim, never inspected by the server.
    context: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
