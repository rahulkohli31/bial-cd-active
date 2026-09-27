"""The `projects` table — the parent container for a user's work.

WHY THIS EXISTS
A project is the durable home a citizen developer builds a single tool inside: it
links the user's three work surfaces — the codebase (its one `app_registry` row,
one-app-per-project), the chats, and the plan-kind chats (both the
`conversations` table, distinguished by `ChatKind`). Everything Phase-2
attaches to (per-app DB isolation, deploy target, governance record) hangs off the
project, so it is the keystone that lands before versioning.

Ownership is the single-tenant boundary (`OwnedByUserMixin` → `user_id`):
every query over a project is scoped by the owning `user_id`, and a project and its
children must share that `user_id` (a user cannot file work under another user's
project). There is NO `org_id` — the user IS the isolation boundary.

`description` is REQUIRED ON CREATE and word-bounded (#191), and doubles as
shared grounding injected into every chat in the project (R16, U8) and as the marketplace's
listing/search text (#145). NULL is still a legal column value — a project created before
#191 keeps working with none, and the rule is enforced at the Pydantic write boundary
(U4/U7/#191), NOT the column, so no migration or backfill was needed to make it mandatory.
Length and word-count are normalized (empty/whitespace → NULL, checked against the bounds
below) at that same boundary — because the field is injected into every project chat turn,
an unbounded value is an uncapped per-turn token cost (KD-8, R20). The cap constants live
here so the write boundary (U4/U7/#191) and the injection point (U8) share one source of
truth.
"""

from __future__ import annotations

import sqlalchemy as sa
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column

from src.db.base import Base
from src.db.mixins import OwnedByUserMixin, TimestampMixin, UUIDv7PrimaryKeyMixin

# Bounded so a name can index/display sanely. This is now the sole app/project display
# name — the admin registry sources each app's name from its owning project.
MAX_PROJECT_NAME = 120
# The title cap a PERSON meets. The column stays VARCHAR(120) and rows written
# before this shipped keep their names — the cap applies on create and rename, never
# retroactively, and the list clamps a long stored name visually instead of truncating it.
# Words, not characters: a title is a name, and "about 6 to 8 words" is something a citizen
# can act on where "60 characters" is not. `src/core/words.py` owns the splitting rule and
# `portal/src/utils/words.ts` mirrors it exactly, because a title that passes in the browser
# must not be refused by the API.
MAX_PROJECT_NAME_WORDS = 8
# The description is injected into EVERY project chat turn, so it is capped:
# an unbounded description is an uncapped per-turn token cost. Enforced at the
# Pydantic write boundary; this constant is the shared source of truth.
MAX_PROJECT_DESCRIPTION = 2000
# The WORD bounds a person is told about (#191) — the character cap above is only the
# paste backstop, same relationship as MAX_PROJECT_NAME_WORDS to MAX_PROJECT_NAME. Unlike
# the title, this one has a MINIMUM too: a one-line description embeds into a single vector
# for semantic search (#191 slice 3), so a description too short to say anything embeds to
# nothing worth matching. The maximum is a retrieval requirement as much as a storage one —
# a long multi-topic description embeds to a vector that matches everything weakly, and
# `ts_rank_cd` has no document-length normalisation, so a rambling description can out-rank
# a precise one purely by containing more terms.
MIN_PROJECT_DESCRIPTION_WORDS = 5
MAX_PROJECT_DESCRIPTION_WORDS = 120
# The marketplace's search text configuration (#145, migration 0034), named ONCE here —
# where the generated column it must match lives — and imported by the marketplace router
# rather than redeclared. A query parsed under a different configuration than the one the
# generated column was built with stems differently and silently under-matches; that
# failure mode is exactly why this can't be two independent constants that happen to agree.
DESCRIPTION_TSV_REGCONFIG = "english"
# `text-embedding-3-small`'s default dimension count (#191 slice 3, migration 0040) — under
# pgvector's 2000-dimension ceiling for both HNSW and IVFFlat, so an index stays available as
# the catalog grows. The model's `dimensions` shortening knob is honoured by the deployment
# but deliberately not used: the default is already under the ceiling, so shortening would
# cost recall and buy nothing.
DESCRIPTION_EMBEDDING_DIMENSIONS = 1536


class Project(UUIDv7PrimaryKeyMixin, TimestampMixin, OwnedByUserMixin, Base):
    __tablename__ = "projects"

    __table_args__ = (
        # DECLARED HERE EVEN THOUGH THE MIGRATION CREATES IT. The index is raw SQL in
        # migration 0034, and a model that does not know about it is not merely untidy:
        # `alembic revision --autogenerate` against a fully-migrated database emits a
        # `drop_index` for it, so the next person to autogenerate anything silently picks
        # up a DROP of the marketplace's search index.
        # `deployment.py` already declares its indexes this way.
        sa.Index(
            "ix_projects_description_tsv",
            "description_tsv",
            postgresql_using="gin",
        ),
        # Same "declared here even though the migration creates it" reasoning as above —
        # migration 0040 builds this raw SQL, and an unaware model would let a future
        # autogenerate emit a DROP of the semantic-search index (#191 slice 3).
        sa.Index(
            "ix_projects_description_embedding",
            "description_embedding",
            postgresql_using="hnsw",
            postgresql_ops={"description_embedding": "vector_cosine_ops"},
        ),
    )

    name: Mapped[str] = mapped_column(sa.String(MAX_PROJECT_NAME), nullable=False)
    # Shared chat context (R15/R16), the marketplace listing (#145), and the search/duplicate-
    # check text (#191). Required and word-bounded at the Pydantic write boundary as of #191 —
    # the COLUMN stays nullable regardless, so a project written before #191 with no
    # description is untouched (R13/R14). Length/word-count are checked at that same boundary,
    # not here.
    description: Mapped[str | None] = mapped_column(sa.Text, nullable=True)
    # The marketplace's search index (migration 0034). DERIVED from `description` by
    # Postgres, never written from here — declared `Computed(persisted=True)` so SQLAlchemy
    # excludes it from INSERT/UPDATE rather than letting the database reject the write.
    # Mapped at all (instead of raw SQL in the query) so the marketplace's `@@` match and
    # `ts_rank_cd` ordering reference a typed column the checkers can see.
    #
    # `coalesce(description, '')` mirrors the migration exactly: a NULL description yields
    # an empty tsvector, which matches nothing — which is how an app with no description
    # stays out of search while remaining in the unfiltered catalog.
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
    # What actually breaks, measured against a downgraded database rather than assumed: a
    # Project SELECT SUCCEEDS, because `deferred=True` above keeps this column out of the
    # SELECT list — so the chat hot paths are NOT affected. A Project INSERT FAILS with
    # `UndefinedColumn`, because SQLAlchemy postfetches `Computed`/server-default columns via
    # RETURNING regardless of deferral. The blast radius is therefore `POST /v1/projects` —
    # NEW PROJECT CREATION — not "every chat turn". Narrower than it first looked, and worth
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
    # The semantic-search index (#191 slice 3, migration 0040). UNLIKE `description_tsv`
    # above, this is NOT `Computed` — an embedding call is a network round trip Postgres
    # cannot make, so it is written explicitly from the API process (`api/v1/projects/
    # router.py`) whenever a description is first set or changes (R25). NULL for every
    # pre-#191 project and for any row whose embedding call failed (R26) — both read as
    # keyword-only by the hybrid search query, not as an error.
    #
    # `deferred=True` for the same reason `description_tsv` is: `Project` is loaded as a
    # full entity on the chat hot path, which has no use for a 1536-float vector.
    description_embedding: Mapped[list[float] | None] = mapped_column(
        Vector(DESCRIPTION_EMBEDDING_DIMENSIONS),
        nullable=True,
        deferred=True,
    )
