"""The `project_shares` table — the platform's first junction table (#198).

WHY THIS EXISTS. Every other list on the platform is scoped by a single `user_id` under
`OwnedByUserMixin` (ADR-0004): one owner, one row, no exceptions. Sharing needs a genuine
many-to-many — one project can be shared with several colleagues, and one colleague can have
several projects shared with them — and nothing in the schema expresses that today (15 model
files, zero `relationship()` calls, zero link tables). This is the first one.

NO `shared_by_user_id` COLUMN. It would always equal `projects.user_id` at write time — only
an owner can create a share (`services/projects/shares.py::create_share` enforces it) — and
this platform has no project-reparenting feature that could ever make the two diverge. Storing
it would be a second copy of a fact `projects.user_id` already answers; callers that need "who
shared this" join through `Project.user_id` instead. The share-CREATE audit row
(`services/audit/log.py::append_audit`, action `project:share_create`) is the place that
records WHO acted, stamped independently from the session at the moment of the grant — that is
a point-in-time accountability record, a different job from this table's current-membership one.

`ON DELETE CASCADE` ON BOTH FKS, deliberately DB-level here (unlike `delete_project_cascade`'s
app/conversation rows, which are enumerated and deleted explicitly so their object-store blobs
can be swept in step). A share row has no object-store footprint of its own — nothing to sweep
— so letting Postgres collapse it for free when either side of the relationship disappears is
correct, not a shortcut. Deleting a project takes its shares with it; the RUNTIME container a
recipient may have live is a separate concern the delete route tears down explicitly before the
commit reaches this cascade (the shared runtime, once it exists).

Not composed from `OwnedByUserMixin`: that mixin is specifically the single-owner-column
pattern the rest of the schema follows, and this table's two user references are not that
shape — neither `project_id` nor `shared_with_user_id` alone is "the owner" of a share row.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base
from src.db.mixins import TimestampMixin, UUIDv7PrimaryKeyMixin


class ProjectShare(UUIDv7PrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "project_shares"

    __table_args__ = (
        # ONE ROW PER (project, recipient) — the idempotent-re-share invariant (R3) AND the
        # `ON CONFLICT` inference target `create_share` upserts against, so sharing the same
        # project with the same colleague twice never creates a second row.
        sa.UniqueConstraint(
            "project_id", "shared_with_user_id", name="uq_project_shares_project_recipient"
        ),
    )

    project_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid,
        sa.ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Indexed independently of the unique constraint above (which is `(project_id,
    # shared_with_user_id)` and so is USABLE but not IDEAL for "every project shared with
    # user X" — that query leads with the second column). "Shared with me" is a citizen-facing
    # list read on every visit to the projects screen; "shares for project X" is an
    # owner-only, low-cardinality read off the already-indexed leading column above.
    shared_with_user_id: Mapped[uuid.UUID] = mapped_column(
        sa.Uuid,
        sa.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
