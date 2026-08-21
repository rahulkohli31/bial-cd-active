"""The `projects` table — the parent container for a user's work (R1, R2; ADR-0004).

A project is the durable home a citizen developer builds a single tool inside: it
links the user's three work surfaces — the codebase (its one `app_registry` row,
R22/one-app-per-project), the chats, and the plan-kind chats (both the
`conversations` table, distinguished by `ConversationKind`). Everything Phase-2
attaches to (per-app DB isolation, deploy target, governance record) hangs off the
project, so it is the keystone that lands before versioning.

Ownership is the single-tenant boundary (`OwnedByUserMixin` → `user_id`, ADR-0004):
every query over a project is scoped by the owning `user_id`, and a project and its
children must share that `user_id` (a user cannot file work under another user's
project). There is NO `org_id` — the user IS the isolation boundary.

`description` is optional (NULL = no description) and doubles as shared grounding
injected into every chat in the project (R16, U8). It is length-bounded and
normalized (empty/whitespace → NULL) at the Pydantic write boundary (U4/U7), NOT the
column — because it is injected into every project chat turn, an unbounded value is
an uncapped per-turn token cost (KD-8, R20). The cap constant lives here so the
write boundary (U4/U7) and the injection point (U8) share one source of truth.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base
from src.db.mixins import OwnedByUserMixin, TimestampMixin, UUIDv7PrimaryKeyMixin

# Bounded so a name can index/display sanely. This is now the sole app/project display
# name — the admin registry sources each app's name from its owning project (#48).
MAX_PROJECT_NAME = 120
# The description is injected into EVERY project chat turn (R16/U8), so it is capped:
# an unbounded description is an uncapped per-turn token cost (KD-8). Enforced at the
# Pydantic write boundary (U4/U7); this constant is the shared source of truth.
MAX_PROJECT_DESCRIPTION = 2000


class Project(UUIDv7PrimaryKeyMixin, TimestampMixin, OwnedByUserMixin, Base):
    __tablename__ = "projects"

    name: Mapped[str] = mapped_column(sa.String(MAX_PROJECT_NAME), nullable=False)
    # Optional shared chat context (R15/R16). NULL = no description; the write
    # boundary normalizes empty/whitespace to NULL so there is no undefined
    # empty-string third state (KD-8). Length is capped at the boundary, not here.
    description: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    # The marketplace's search index (#145, migration 0033). DERIVED from `description` by
    # Postgres, never written from here — declared `Computed(persisted=True)` so SQLAlchemy
    # excludes it from INSERT/UPDATE rather than letting the database reject the write.
    # Mapped at all (instead of raw SQL in the query) so the marketplace's `@@` match and
    # `ts_rank_cd` ordering reference a typed column the checkers can see.
    #
    # `coalesce(description, '')` mirrors the migration exactly: a NULL description yields
    # an empty tsvector, which matches nothing — which is how an app with no description
    # stays out of search while remaining in the unfiltered catalog (#145, accepted).
    description_tsv: Mapped[str | None] = mapped_column(
        TSVECTOR,
        sa.Computed("to_tsvector('english', coalesce(description, ''))", persisted=True),
        nullable=True,
    )
