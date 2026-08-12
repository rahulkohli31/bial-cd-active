"""Reusable declarative mixins (ADR-0013, ADR-0004, ADR-0006).

New models compose these rather than re-declaring the columns:

* `UUIDv7PrimaryKeyMixin` — time-sortable, index-friendly UUIDv7 PK. App-side
  `uuid.uuid7()` default (Python 3.14 stdlib) plus a PostgreSQL 18 native
  `uuidv7()` server default so raw SQL inserts also get a v7 key.
* `TimestampMixin` — `created_at` / `updated_at`, server-defaulted to `now()`.
* `OwnedByUserMixin` — the single-tenant ownership boundary: a non-nullable,
  indexed `user_id` FK. BIAL has NO `org_id` — the user IS the isolation boundary
  (ADR-0004). Every query over a model carrying this mixin must filter by
  `user_id`; a dropped predicate is a cross-user leak. The `users` table lands
  with auth (a later phase); this mixin stays dormant until a model composes it.
"""

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column


class UUIDv7PrimaryKeyMixin:
    id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid,
        primary_key=True,
        default=uuid.uuid7,
        server_default=sa.text("uuidv7()"),
    )


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        server_default=sa.func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True),
        server_default=sa.func.now(),
        onupdate=sa.func.now(),
        nullable=False,
    )


class OwnedByUserMixin:
    user_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid,
        sa.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
