"""The `conversations` table — one chat, keyed by a CLIENT-MINTED id, with a fixed kind.

A conversation is minted by the SPA (`crypto.randomUUID`) and supplies its own id on the first
append, never the server; the `UUIDv7PrimaryKeyMixin` default only covers a hypothetical
server-minted row.

THE ID IS NO LONGER THE APP'S ID. This said "a *builder* conversation's id IS the deployed
`appId`" — that 1:1 identity is retired and `app_registry.py` says so at both ends: an app is
PROJECT-scoped with its own id (`uq_app_registry_project`, one app per project), and
`app_registry.conversation_id` was repurposed into a soft HEAD POINTER at the last build session
that touched the app. Many conversations build against one app over its life, so an id read as an
`appId` names the wrong thing. The id still must not be reassigned — it is what the SPA routes on
and what `conversation_id` points at — but preserving it is no longer preserving an app identity.

`kind` is a native PG enum (ADR-0008) and it is the WHOLE classification: `plan` or `build`,
chosen at creation and never changed, because no route mutates it (R14/R15). Tool gating
derives from this column and from nothing the client sends. There is no `mode` column any
more — it and its switch endpoint were dropped in migration 0035, and the two three-valued
vocabularies they carried collapsed into this one two-valued one.
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

    WHAT MAY READ IT IS NAMED BY ROLE, NOT BY COUNT. `services/agent/toolsets.py` reads it to
    decide what the model CAN DO — the guardrail. A Plan chat cannot change the app because
    `write_file`, `edit_file`, `insert_lines`, `apply_schema_change`, the sandbox-routed
    `run_command` and `declare_done` are not in the list handed to that run, never because
    something downstream notices which kind of chat it is. `services/agent/mode_prompts.py`
    reads it to decide what the model is TOLD. `services/turns/engine.py` reads it to select a
    HARNESS SHAPE: the node loop with its per-step billing fold versus a single
    `chat_agent.run`. Those three questions are the whole permitted set; everything else that
    holds a kind is stamping a row.

    An earlier version of this paragraph counted the readers ("exactly two") and the engine's
    own comment counted them differently ("three ... the closed set"). Both were wrong, and a
    number in a docstring cannot go red when it stops being true. Read the roles here; trust a
    test for the census.
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
    # An opaque SPA-owned bag: stored verbatim, never inspected by the server, and settable
    # through `POST /conversations` and the header PATCH.
    #
    # NOTHING WRITES IT TODAY, AND IT IS KEPT ANYWAY. It carried the Express-POC builder's
    # generation settings; `theme` went with the Select Theme control (#157 B1) and
    # `uploadedFiles` never had a producer, so the last live round trip through it was dead and
    # has been removed. Production rows still hold POC-era payloads no code can reconstruct, so
    # the column stays and a `DROP COLUMN` is a separate, staged decision — not a dead-code
    # sweep. The read path is untouched: a row's stored value is still served on the header.
    context: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
