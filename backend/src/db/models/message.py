"""The `messages` table — one row per persisted NATIVE pydantic-ai batch (U4, plan
2026-07-22-002).

Rebuilt in place by migration 0024 (destructive reset — the legacy SPA parts shape is gone).
One row per persisted batch: a whole turn in a Plan chat, a single agent step in a Build chat
(so a crash mid-build loses at most the in-flight step), or a system/lifecycle entry. The JSONB
`payload` is 100% native `ModelMessagesTypeAdapter` serialization — the ONLY
transformations applied at the seam are attachment-binary externalization and secret redaction
(`services/messages/store.py`); everything else round-trips byte-faithfully.

Classification lives on the ROW, never in the payload (verified against pinned pydantic-ai
2.5.0: `UserPromptPart` has no metadata field, extra payload keys are silently absorbed at the
TypeAdapter boundary — an unknown dict coerces to `CachePoint`! — and `ModelRequest.metadata`
is droppable by upstream's own history merge). `entry_kind` + `visibility` are SQL predicates:
the history loader feeds hidden marker rows to the model like any message; the UI projection
filters them with a WHERE clause, not a payload inspection.

`seq` is the server-owned, GAP-FREE per-conversation ordering key — allocated by the store's
two-writer retry discipline, never by a client. `schema_version` stamps the payload's
serialization contract (bump it when the pinned pydantic-ai's wire shape changes so old rows
stay decodable). `meta` carries system-entry metadata (build session id, start marker,
preview URL…) — row-side, so the payload stays pure native.
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
from src.db.models.conversation import ChatKind, chat_kind_enum


class MessageEntryKind(StrEnum):
    """What kind of batch this row holds. Native PG enum labels (ADR-0008).

    * `turn` — a whole Plan-chat turn (user prompt + the run's new messages).
    * `step` — one Build-chat agent step (BRAIN persists per step for crash durability).
    * `system_event` — a lifecycle record (build outcome, provision/quota/stop events).

    THE RETIRED `mode_switch` LABEL. There was a fourth: a hidden direction-aware marker
    written by the mode-switch endpoint so the model could see where in history the mode
    changed. There are no mode boundaries any more — a chat's kind is fixed at creation — so
    revision 0035 deleted every such row and the member went with the endpoint. The PG label
    is left in place and inert: nothing writes it, nothing reads it, and swapping the type to
    remove one unreferenced label would rewrite the largest table for no behavioural gain.
    """

    TURN = "turn"
    STEP = "step"
    SYSTEM_EVENT = "system_event"


class MessageVisibility(StrEnum):
    """Whether the UI projection renders this row. The MODEL sees hidden rows (the history
    loader ignores this column); only projection/display reads filter on it."""

    VISIBLE = "visible"
    HIDDEN = "hidden"


# Native PG enums, shared by the model columns and migration 0024 (`create_type=False` — the
# migration owns CREATE/DROP TYPE). Mirrors `chat_kind_enum`.
message_entry_kind_enum = sa.Enum(
    MessageEntryKind,
    name="message_entry_kind",
    values_callable=lambda enum: [member.value for member in enum],
    create_type=False,
)

message_visibility_enum = sa.Enum(
    MessageVisibility,
    name="message_visibility",
    values_callable=lambda enum: [member.value for member in enum],
    create_type=False,
)


class Message(UUIDv7PrimaryKeyMixin, TimestampMixin, OwnedByUserMixin, Base):
    __tablename__ = "messages"

    __table_args__ = (
        # `seq` is the SOLE per-conversation ordering key — unique so a two-writer collision
        # is a caught IntegrityError the store retries, never silent ordering corruption.
        sa.UniqueConstraint("conversation_id", "seq", name="uq_messages_conversation_seq"),
    )

    # FK to the owning conversation; a deleted conversation cascades its messages away.
    conversation_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False, index=True
    )
    # Server-owned, gap-free per-conversation ordering key (max+1 under the unique constraint's
    # protection — `services/messages/store.py` owns allocation and its retry loop).
    seq: Mapped[int] = mapped_column(sa.Integer, nullable=False)
    # The payload serialization contract version (1 = pydantic-ai 2.5.0 native batch with
    # attachment-ref externalization). Bump on any wire-shape-affecting upgrade.
    schema_version: Mapped[int] = mapped_column(
        sa.Integer, server_default=sa.text("1"), nullable=False
    )
    entry_kind: Mapped[MessageEntryKind] = mapped_column(message_entry_kind_enum, nullable=False)
    visibility: Mapped[MessageVisibility] = mapped_column(
        message_visibility_enum,
        nullable=False,
        server_default=sa.text("'visible'::message_visibility"),
    )
    # Which kind of chat the batch ran under — an AUDIT stamp, and deliberately kept even
    # though nothing under `src/` reads it for a decision any more (R53 names the per-row stamp
    # in as many words, and "what was this row written under" is cheap to keep and impossible
    # to reconstruct later). Renamed from `mode` by revision 0035 rather than reused under the
    # old name: leaving a column called `mode` behind is how a deleted vocabulary survives.
    kind: Mapped[ChatKind] = mapped_column(chat_kind_enum, nullable=False)
    # The native batch: `ModelMessagesTypeAdapter.dump_python(mode="json")` output, with
    # `BinaryContent` externalized to attachment references and secrets redacted. A list of
    # ModelMessage dicts; may be empty for a pure lifecycle entry.
    payload: Mapped[list[Any]] = mapped_column(JSONB, nullable=False)
    # System-entry metadata (build sessionId / startedSeq / previewUrl / status / reason…) —
    # OUTSIDE the payload so the payload stays pure native. NULL for ordinary turns/steps.
    meta: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
