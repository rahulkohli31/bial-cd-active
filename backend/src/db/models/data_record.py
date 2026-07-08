"""The `data_records` table — the per-app data-service store (R22, R24; ADR-0004).

App-scoped, NOT directly user-scoped: a record belongs to an APP (whose own row is
user-owned), so the isolation predicate is `app_id`. Every records query MUST carry
`WHERE app_id = :app_id` — a dropped predicate is a cross-APP data leak (BOLA), the
security-critical failure this whole plane exists to prevent.

The record `id` is preserved at the later backfill (an existing record id becomes the
row PK — AE3); new records get a UUIDv7 PK. `data` is the app's opaque JSON payload;
`search_text` is the derived, lowercased scalar-leaf projection (Express `_search`)
the search endpoint matches with a substring `ILIKE` (U5). `bytes` is the per-record
quota unit. `created_by` is the actor (the Bearer runner-token's user, or NULL for an
anonymous open app); `created_in_draft` tags a build-time test row.
"""

from __future__ import annotations

import uuid
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base
from src.db.mixins import TimestampMixin, UUIDv7PrimaryKeyMixin


class DataRecord(UUIDv7PrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "data_records"

    __table_args__ = (
        # The hot read path: records for one app in one collection, newest-first.
        sa.Index("ix_data_records_app_collection", "app_id", "collection"),
        # The list/search default orderings (newest-first by created/updated) per app.
        sa.Index("ix_data_records_app_created", "app_id", "created_at"),
        sa.Index("ix_data_records_app_updated", "app_id", "updated_at"),
    )

    # The app-scope boundary — FK to the owning app, CASCADE so a hard-deleted app's
    # records go with it (belt-and-suspenders; admin hard-delete purges explicitly).
    app_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid,
        sa.ForeignKey("app_registry.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Logical grouping within the app (Express `^[A-Za-z0-9_-]{1,64}$`, default
    # 'default'); validated at the write boundary (U5).
    collection: Mapped[str] = mapped_column(
        sa.String(64), server_default="default", nullable=False
    )
    # The app's opaque record payload (reserved keys stripped at the boundary).
    data: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    # The actor: the injected (Bearer) user's id, or NULL for an anonymous open app.
    created_by: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid, nullable=True)
    # True when created while the app was not yet approved (a build-time test row);
    # clear-data can purge only these.
    created_in_draft: Mapped[bool] = mapped_column(
        sa.Boolean, server_default=sa.text("false"), nullable=False
    )
    # Per-record quota unit: byte length of the serialized `data` (excludes the
    # derived `search_text`). Reserved atomically against the app's data_bytes cap.
    bytes: Mapped[int] = mapped_column(sa.Integer, server_default=sa.text("0"), nullable=False)
    # The derived free-text projection (Express `_search`): lowercased scalar leaves
    # joined by spaces, capped. Matched by substring ILIKE in the search endpoint.
    search_text: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
