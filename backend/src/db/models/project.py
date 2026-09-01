"""The `projects` table — the parent container for a user's work (R1, R2; ADR-0004).

A project is the durable home a citizen developer builds a single tool inside: it
links the user's three work surfaces — the codebase (its one `app_registry` row,
R22/one-app-per-project), the chats, and the plan-kind chats (both the
`conversations` table, distinguished by `ChatKind`). Everything Phase-2
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
# The marketplace's search text configuration (#145, migration 0034), named ONCE here —
# where the generated column it must match lives — and imported by the marketplace router
# rather than redeclared. A query parsed under a different configuration than the one the
# generated column was built with stems differently and silently under-matches; that
# failure mode is exactly why this can't be two independent constants that happen to agree.
DESCRIPTION_TSV_REGCONFIG = "english"


class Project(UUIDv7PrimaryKeyMixin, TimestampMixin, OwnedByUserMixin, Base):
    __tablename__ = "projects"

    __table_args__ = (
        # DECLARED HERE EVEN THOUGH THE MIGRATION CREATES IT. The index is raw SQL in
        # migration 0034, and a model that does not know about it is not merely untidy:
        # `alembic revision --autogenerate` against a fully-migrated database emits a
        # `drop_index` for it, so the next person to autogenerate anything silently picks
        # up a DROP of the marketplace's search index (#147 round 3, reproduced).
        # `deployment.py` already declares its indexes this way.
        sa.Index(
            "ix_projects_description_tsv",
            "description_tsv",
            postgresql_using="gin",
        ),
    )

    name: Mapped[str] = mapped_column(sa.String(MAX_PROJECT_NAME), nullable=False)
    # Optional shared chat context (R15/R16). NULL = no description; the write
    # boundary normalizes empty/whitespace to NULL so there is no undefined
    # empty-string third state (KD-8). Length is capped at the boundary, not here.
    description: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    # The marketplace's search index (#145, migration 0034). DERIVED from `description` by
    # Postgres, never written from here — declared `Computed(persisted=True)` so SQLAlchemy
    # excludes it from INSERT/UPDATE rather than letting the database reject the write.
    # Mapped at all (instead of raw SQL in the query) so the marketplace's `@@` match and
    # `ts_rank_cd` ordering reference a typed column the checkers can see.
    #
    # `coalesce(description, '')` mirrors the migration exactly: a NULL description yields
    # an empty tsvector, which matches nothing — which is how an app with no description
    # stays out of search while remaining in the unfiltered catalog (#145, accepted).
    #
    # `deferred=True`: this column is otherwise mapped, non-deferred, and `Project` is
    # loaded as a full entity on the chat hot path (`conversations/turns.py`,
    # `conversations/transition.py`, `services/projects/resolve.py`) —
    # every one of those was pulling the tsvector along for no reason. Deferred loading
    # does NOT affect the marketplace router: it never loads `Project` as an ORM instance,
    # it references `Project.description_tsv` as a raw column expression in `.where()` /
    # `order_by()`, which works identically whether the mapped attribute is deferred or not.
    #
    # DEPLOY-ORDERING HAZARD: this column must exist before the image that maps it ships —
    # migrate first, THEN deploy; roll back in the reverse order (previous image first,
    # then downgrade).
    #
    # What actually breaks, measured against a downgraded database rather than assumed
    # (#147 round 3 corrected an earlier, wronger version of this note): a Project SELECT
    # SUCCEEDS, because `deferred=True` above keeps this column out of the SELECT list —
    # so the chat hot paths are NOT affected. A Project INSERT FAILS with `UndefinedColumn`,
    # because SQLAlchemy postfetches `Computed`/server-default columns via RETURNING
    # regardless of deferral. The blast radius is therefore `POST /v1/projects` — NEW
    # PROJECT CREATION — not "every chat turn". Narrower than it first looked, and worth
    # being exact about, since this is the sentence a deploy runbook acts on.
    description_tsv: Mapped[str | None] = mapped_column(
        TSVECTOR,
        sa.Computed(
            f"to_tsvector('{DESCRIPTION_TSV_REGCONFIG}', coalesce(description, ''))",
            persisted=True,
        ),
        nullable=True,
        deferred=True,
    )
