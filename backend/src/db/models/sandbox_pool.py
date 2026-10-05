"""The `sandbox_pool` table — the ledger of containers made ahead of time for the pool, and of a
start's own create until the registry records it.

A row is written before its container is created and deleted once the person's registry record
names the container. A claim is a compare-and-set on one `ready` row, so two starts never receive
the same container; a start's own create is `claimed` from the outset. No `user_id`: a row's
container belongs to nobody until the registry, scoped by user, records it. `name` holds only the
shape `a_fresh_sandbox_name` mints, since every path that deletes a container refuses any other.
"""

from __future__ import annotations

import enum
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base
from src.db.mixins import UUIDv7PrimaryKeyMixin
from src.db.models.pending_teardown import MAX_APP_NAME


class SandboxPoolState(enum.StrEnum):
    FILLING = "filling"
    READY = "ready"
    CLAIMED = "claimed"
    RETIRING = "retiring"


sandbox_pool_state_enum = sa.Enum(
    SandboxPoolState,
    name="sandbox_pool_state",
    values_callable=lambda members: [member.value for member in members],
    create_type=False,
)


class SandboxPoolMember(UUIDv7PrimaryKeyMixin, Base):
    __tablename__ = "sandbox_pool"

    __table_args__ = (
        sa.UniqueConstraint("name", name="uq_sandbox_pool_name"),
        sa.CheckConstraint("name ~ '^sbx-[0-9a-f]{28}$'", name="ck_sandbox_pool_name_shape"),
    )

    name: Mapped[str] = mapped_column(sa.String(MAX_APP_NAME), nullable=False)
    # The container's own address; unknown until its create has answered.
    fqdn: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    # The image the container was made from, which is how an image change is seen.
    image_ref: Mapped[str] = mapped_column(sa.Text, nullable=False)
    state: Mapped[SandboxPoolState] = mapped_column(sandbox_pool_state_enum, nullable=False)
    state_changed_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), nullable=False)
