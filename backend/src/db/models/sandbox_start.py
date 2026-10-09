"""The `sandbox_starts` table — one row per sandbox start, with how long each stage took.

WHY THIS EXISTS. Production log history cannot be read back afterwards, so where the time of a
start goes is kept here: which kind of start it was, whether it took a ready container, and the
milliseconds of each stage. A row is written once the environment of a container about to be
born is complete, and closed when its first page is seen or the start fails; an attach to a
running container writes none.

The row names who started which app and when, so no route returns one: the only read is a
superadmin aggregate, and a row goes with its user, its app, or after ninety days.
"""

from __future__ import annotations

import enum
import uuid
from datetime import datetime, timedelta
from typing import Final

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base
from src.db.mixins import OwnedByUserMixin, UUIDv7PrimaryKeyMixin

SANDBOX_START_RETENTION: Final = timedelta(days=90)


class SandboxStartKind(enum.StrEnum):
    REOPEN = "reopen"
    NEW_PROJECT = "new_project"
    CHAT = "chat"
    SWITCH = "switch"
    SHARED_VIEW = "shared_view"


class SandboxProjectType(enum.StrEnum):
    PLAIN = "plain"
    #: The start's environment carries the connector coordinates and so its data identity.
    CONNECTOR = "connector"


class SandboxStartMiss(enum.StrEnum):
    """Why a start created a container rather than claiming a ready one."""

    NO_READY = "no_ready"
    UNHEALTHY = "unhealthy"
    CLAIM_FAILED = "claim_failed"
    SIZE_ZERO = "size_zero"


class SandboxStartOutcome(enum.StrEnum):
    """`NULL` on a start that neither served a page anyone watched nor failed: the turn that
    began it ended first, its watcher gave up, or the process ended mid-start."""

    SERVED = "served"
    FAILED = "failed"


def _native(enum_type: type[enum.StrEnum], name: str) -> sa.Enum:
    """A native PG enum the migration creates and drops; the column never creates the type."""
    return sa.Enum(
        enum_type,
        name=name,
        values_callable=lambda members: [member.value for member in members],
        create_type=False,
    )


sandbox_start_kind_enum = _native(SandboxStartKind, "sandbox_start_kind")
sandbox_project_type_enum = _native(SandboxProjectType, "sandbox_project_type")
sandbox_start_miss_enum = _native(SandboxStartMiss, "sandbox_start_miss")
sandbox_start_outcome_enum = _native(SandboxStartOutcome, "sandbox_start_outcome")


class SandboxStart(UUIDv7PrimaryKeyMixin, OwnedByUserMixin, Base):
    """`user_id` is who started it, which for a shared view is the colleague viewing it; `app_id`
    is then the owner's app."""

    __tablename__ = "sandbox_starts"

    app_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid, sa.ForeignKey("app_registry.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[SandboxStartKind] = mapped_column(sandbox_start_kind_enum, nullable=False)
    project_type: Mapped[SandboxProjectType] = mapped_column(
        sandbox_project_type_enum, nullable=False
    )
    claimed: Mapped[bool] = mapped_column(
        sa.Boolean, nullable=False, server_default=sa.text("false")
    )
    miss_reason: Mapped[SandboxStartMiss | None] = mapped_column(
        sandbox_start_miss_enum, nullable=True
    )
    # How many ready containers there were when this start asked for one.
    ready_count: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), nullable=False, index=True
    )
    ended_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True), nullable=True)
    outcome: Mapped[SandboxStartOutcome | None] = mapped_column(
        sandbox_start_outcome_enum, nullable=True
    )
    # Contiguous stages, so they sum to the time from the door to the first page.
    admission_ms: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    settings_ms: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    create_ms: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    dev_start_ms: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    first_page_ms: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    # The browser's own click-to-visible clock, a separate measure from the stages above.
    browser_visible_ms: Mapped[int | None] = mapped_column(sa.Integer, nullable=True)
    # Step name to milliseconds, for the steps inside a stage.
    sub_steps: Mapped[dict[str, int]] = mapped_column(
        JSONB, nullable=False, server_default=sa.text("'{}'::jsonb")
    )
    # Whether the restore reinstalled packages because the saved lockfile differs from the
    # image's. `NULL` when no restore ran.
    reinstalled: Mapped[bool | None] = mapped_column(sa.Boolean, nullable=True)
