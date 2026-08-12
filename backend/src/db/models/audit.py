"""The `audit_logs` table — append-only accountability for every permission-gated
/ state-changing action (R9, ADR-0005).

Mirrors the Express audit trail (`portal/server/audit-repo.js`): record WHO did
WHAT to WHICH resource — never the record CONTENTS. One row per gated mutation /
admin action. The action vocabulary is an OPEN string (create/update/delete,
approve/reject/disable, clear-data, …) that grows per domain, so `action` is a
plain `String`, not a native PG enum — a new gated action must not need an
`ALTER TYPE`.

NOT `OwnedByUserMixin`: that mixin is a NOT-NULL, ON DELETE CASCADE ownership FK,
but an audit row records an ACTOR who may act on another user's resource, and the
trail must SURVIVE the actor's deletion. So the actor is an explicit NULLABLE FK
with ON DELETE SET NULL — a deleted user's accountability rows remain (actor
unlinked), never cascade-deleted with them.
"""

from __future__ import annotations

import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base
from src.db.mixins import TimestampMixin, UUIDv7PrimaryKeyMixin


class AuditLog(UUIDv7PrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "audit_logs"

    # Composite index for the "audit rows queryable by resource" read path.
    __table_args__ = (sa.Index("ix_audit_logs_resource", "resource_type", "resource_id"),)

    # The acting user. Nullable + ON DELETE SET NULL so the append-only trail
    # OUTLIVES the actor (accountability must not vanish when a user is offboarded);
    # NULL also covers a system/anonymous actor (Express stores username-or-null).
    actor_id: Mapped[uuid.UUID | None] = mapped_column(
        sa.Uuid,
        sa.ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    # Open vocabulary (create/update/delete, approve/reject/disable, clear-data, …).
    # A String, not a native enum: a new gated action must not require a migration.
    action: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    # The kind of thing acted on ("feedback", "attachment", "conversation", "app").
    resource_type: Mapped[str] = mapped_column(sa.String(64), nullable=False)
    # The target id — a String (not Uuid) so it holds OUR UUIDs AND external ids
    # (appId, conversationId). Nullable: some actions (bulk clear-data) have no
    # single target, only a `detail.count`.
    resource_id: Mapped[str | None] = mapped_column(sa.String(255), nullable=True)
    # Optional structured context (e.g. {"count": 42}, changed field names) — NEVER
    # the record contents (security.md: no user data in the audit blob beyond ids).
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
