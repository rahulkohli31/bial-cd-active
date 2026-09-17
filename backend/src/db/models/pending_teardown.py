"""The `pending_teardowns` table — a durable claim on a container deletion the platform still
owes, reachable after the per-user registry has moved on to the next container.

WHY THIS EXISTS. A switch stops using the outgoing container at once, and the per-user registry
is overwritten by the incoming one on that same request — nothing in Redis names the outgoing
container any more. This row is the only thing that still does, and it must survive long enough
for a detached routine to stop the turn, write the code back, and destroy the container by name.

`app_name` is UNIQUE: `app_name_for(app_id)` is stable across teardown and recreate, so at most
one owed deletion may exist per container name at a time — the constraint IS the concurrency
claim's `ON CONFLICT` inference target.

`instance_ref` is the discriminator the name cannot provide, and it is the per-user registry's
OWN `created_at` (re-stamped at every registration), not the ARM resource id. An ARM container
app's resource id is derived from nothing but its name, subscription and resource group, so it
reads identically for the container this row names and for whatever gets created under the same
name afterward — it cannot discriminate. The registry stamp actually changes across teardown and
recreate, which is what a discriminator needs to do.

No state enum: the row is deleted on success, so its existence IS the state. `claimed_until` is
both the concurrency claim and the retry schedule — a sweep claims with a conditional UPDATE on
`claimed_until <= now()`, which increments `attempts`.

`app_id` and `project_id` carry no ForeignKey, mirroring `DeletedProject`: this row must outlive
the app or project it names, or a citizen deleting the outgoing project mid-wait would silently
forgive a container the platform still owes."""

from __future__ import annotations

import uuid
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base
from src.db.mixins import OwnedByUserMixin, TimestampMixin, UUIDv7PrimaryKeyMixin

# `app_name_for` / `shr_name_for` (`sandbox/base.py`) both emit exactly a 4-char prefix plus
# 28 hex characters — ACA's own 32-character cap on a container name.
MAX_APP_NAME = 32


class PendingTeardown(UUIDv7PrimaryKeyMixin, OwnedByUserMixin, TimestampMixin, Base):
    __tablename__ = "pending_teardowns"

    __table_args__ = (sa.UniqueConstraint("app_name", name="uq_pending_teardowns_app_name"),)

    # No ForeignKey — see the module docstring.
    app_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, nullable=False, index=True)
    app_name: Mapped[str] = mapped_column(sa.String(MAX_APP_NAME), nullable=False)
    # No ForeignKey — see the module docstring.
    project_id: Mapped[uuid.UUID] = mapped_column(sa.Uuid, nullable=False, index=True)
    instance_ref: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    # The outgoing Build turn this row's cooperative stop targets. NULL for a Plan turn
    # (hard-cancelled, nothing to wait on) and for the lapse/ceiling triggers (no turn in
    # flight). No ForeignKey, matching `app_registry.conversation_id`: a soft link a reader
    # must tolerate resolving to nothing.
    conversation_id: Mapped[uuid.UUID | None] = mapped_column(sa.Uuid, nullable=True)
    # How many times this row has been claimed, counting the write that created it. Past the
    # service-layer cap the routine destroys rather than spares — see the module docstring.
    attempts: Mapped[int] = mapped_column(sa.Integer, nullable=False, server_default=sa.text("1"))
    # No default: the value is always the writer's own claim, computed from R11's bound, and a
    # server-generated "now" would read as an already-lapsed claim on a row nobody has acted on
    # yet.
    claimed_until: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
    last_error: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
