"""The `conversations` table — one chat, keyed by a CLIENT-MINTED id, with a fixed kind.

Minted by the SPA (`crypto.randomUUID`) on first append, never the server. THE ID IS NO LONGER THE
APP'S ID: a builder conversation's id was once the deployed `appId`; that identity is retired.
Apps are PROJECT-scoped with their own id, and `app_registry.conversation_id` is now a soft HEAD
POINTER at the last build session touching the app — never reassigned (SPA-routed), never a name.

`kind` is a native PG enum, the WHOLE classification (`plan`/`build`/`generic`, fixed at
creation) — tool gating derives from this column alone, never the client. `project_id` is present
for exactly the two kinds that build an application; a `generic` chat is the citizen's, not a
project's, and a CHECK constraint holds that biconditional at the database.
`title`/`context` are SPA-owned mutable fields; legacy `code` JSONB was dropped in migration 0024
(truth: the build snapshots — `app_registry.current_code` followed it in migration 0039, once
its one remaining reader was deleted). Ownership is `user_id` — every read scoped by it.
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
    """What a chat IS — chosen at creation, never changed.

    Named by ROLE, not count, for what may read it: `toolsets.py` decides what the model CAN DO — a
    Plan chat can't touch the app because every mutating tool (`write_file`, `edit_file`,
    `run_command`, `declare_done`) is omitted from its list, never gated downstream.
    `mode_prompts.py` decides what the model is TOLD; `turns/engine.py` picks the HARNESS SHAPE
    (node loop vs `chat_agent.run`). Those three are the whole permitted set — anything else
    holding a kind is stamping a row.

    `GENERIC` is the one kind with no project and no container: its turn resolves no workspace, is
    handed no toolset, and answers from the transcript and its attachments alone. A reader that
    asks "is this build?" and treats every other answer as plan is wrong for it — every site that
    decides on a kind names all three."""

    PLAN = "plan"
    BUILD = "build"
    GENERIC = "generic"


# Native PG enum, shared by the model columns and the Alembic migrations. `create_type=False`:
# the owning migration runs CREATE/DROP TYPE explicitly (so a downgrade drops it) — the column
# must not try to create the type itself. Mirrors `app_registry.app_status_enum`.
chat_kind_enum = sa.Enum(
    ChatKind,
    name="chat_kind",
    values_callable=lambda enum: [member.value for member in enum],
    create_type=False,
)


# The parentage rule, as one expression, kept as a module constant so the DDL in the migration
# and any future reader see exactly the string the constraint carries. A biconditional, not two
# checks: a generic chat has no project AND a project-bearing chat is never generic.
PARENTAGE_SHAPE = "(kind = 'generic') = (project_id IS NULL)"


class Conversation(UUIDv7PrimaryKeyMixin, TimestampMixin, OwnedByUserMixin, Base):
    __tablename__ = "conversations"

    # `updated_at` (from `TimestampMixin`) reads as "last touched by a person" and is kept by
    # TWO writers, not one: the ORM `onupdate` here covers a title/context PATCH, and migration
    # 0045's statement-level trigger on `messages` covers a new message — the trigger writes
    # behind SQLAlchemy, so a session already holding this row is stale until refreshed.
    __table_args__ = (
        sa.CheckConstraint(PARENTAGE_SHAPE, name="ck_conversations_parentage"),
        # Created by migration 0046 for the retention pass's scan by age.
        sa.Index("ix_conversations_updated_at", "updated_at"),
    )

    # The parent project — present for a plan or build chat, absent for a generic one, and
    # `ck_conversations_parentage` above admits no third combination. The DB cascade is a row
    # backstop only (blob-aware cleanup runs through the conversation-delete service's
    # project-cascade path). `user_id` remains the isolation predicate; `project_id` is
    # organizational, not tenancy, and a project and its children always share the same `user_id`.
    project_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid,
        sa.ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    # Fixed at creation and never changed: there is no route that mutates it, and
    # no server default — a chat whose kind the creator did not choose is a programming error,
    # not a chat that quietly becomes one of them (fail-first).
    kind: Mapped[ChatKind] = mapped_column(chat_kind_enum, nullable=False)
    # Derived client-side from the first message; mutable via PATCH. TEXT (short in
    # practice — the SPA caps it ~40 chars — but unbounded here).
    title: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    # An opaque SPA-owned bag: stored verbatim, never inspected by the server, and settable
    # through `POST /conversations` and the header PATCH.
    #
    # NOTHING WRITES IT TODAY, AND IT IS KEPT ANYWAY. It carried the Express-POC builder's
    # generation settings; `theme` went with the Select Theme control and
    # `uploadedFiles` never had a producer, so the last live round trip through it was dead and
    # has been removed. Production rows still hold POC-era payloads no code can reconstruct, so
    # the column stays and a `DROP COLUMN` is a separate, staged decision — not a dead-code
    # sweep. The read path is untouched: a row's stored value is still served on the header.
    context: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
