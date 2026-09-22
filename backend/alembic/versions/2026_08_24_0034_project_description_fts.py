"""Full-text search over `projects.description` — the marketplace's search index.

NATIVE POSTGRES ONLY: no BM25 extension is on Azure Flexible Server's allowlist, and
`pg_trgm` needs a BIAL infra approval first — so this uses `tsvector` + GIN +
`websearch_to_tsquery` + `ts_rank_cd` instead. Not a compromise at this scale (~10-200 apps).

GENERATED ... STORED, not a trigger, so the column can't drift. `coalesce(description, '')`
makes a NULL description an empty tsvector: absent from search, still in the catalog.

Revision ID: 0034_project_description_fts
Revises: 0033_harness_counters
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0034_project_description_fts"
down_revision: str | Sequence[str] | None = "0033_harness_counters"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX = "ix_projects_description_tsv"
_SUCCESS_IDX = "ix_deployments_success_collapse"
_UNPUBLISHED_IDX = "ix_deployments_unpublished_collapse"


def upgrade() -> None:
    # LOCKING, for whoever runs this against production.
    #
    # `ADD COLUMN ... GENERATED ALWAYS AS (...) STORED` is NOT the metadata-only fast path a
    # plain nullable `ADD COLUMN` gets: Postgres computes and stores the expression for every
    # existing row under ACCESS EXCLUSIVE, blocking reads AND writes on `projects` for the
    # duration. And `projects` is not the 10-200-row marketplace catalog — it is every user's
    # every project. The GIN `CREATE INDEX` on `projects` is not CONCURRENTLY either, so it
    # holds a SHARE lock (blocking writes) while it builds. (The two `deployments` indexes
    # now built FIRST take their own SHARE lock on a DIFFERENT table, and are ordered ahead
    # of the ALTER precisely so `projects` is not held under ACCESS EXCLUSIVE while they
    # build.)
    #
    # Both are almost certainly a non-event at this table's real size, which is why this is
    # not an `autocommit_block()` + CONCURRENTLY rewrite (that variant leaves an INVALID
    # index needing manual cleanup if it fails, a worse trade at this scale).
    #
    # The queue behaviour is the part that bites: without a `lock_timeout`, if any session
    # holds even an AccessShareLock when the ALTER queues, everything behind it stalls FIFO
    # rather than failing fast. Production alembic runs OUT OF BAND here, so the operator who
    # most needs that protection is the one least likely to be reading this file at the time
    # — hence SET rather than recommended.
    #
    # AND IT IS RESET AT THE END OF `upgrade()`. `SET LOCAL` is scoped to the TRANSACTION,
    # and `alembic/env.py` runs one transaction PER MIGRATION — so the timeout dies with this
    # revision and cannot reach a later one. What the reset buys is inside the revision: any
    # statement added after it waits on locks the way it otherwise would, rather than
    # inheriting a 5s ceiling and aborting with `lock_not_available`.
    #
    # The two-arg `to_tsvector('english', ...)` is required, not stylistic: the one-arg form
    # is rejected outright with `ERROR: generation expression is not immutable`.
    op.execute("SET LOCAL lock_timeout = '5s'")
    # THE MARKETPLACE'S TWO COLLAPSES, indexed to match `_live_catalog`'s predicates exactly.
    # Without them each collapse Seq Scans `deployments`, and that table is append-only with
    # no reaper — so the cost tracks TOTAL HISTORICAL DEPLOY ATTEMPTS across the platform's
    # life, not the 10-200 live apps this catalog is sized for. Measured on PG18 at 51k rows:
    # ~100-180ms of DB time per request without, ~35ms with.
    #
    # THESE RUN BEFORE THE `ALTER TABLE projects` BELOW, deliberately. Alembic wraps the
    # whole revision in ONE transaction, so every lock it takes is held until commit —
    # ordering the ALTER first would hold an ACCESS EXCLUSIVE lock on `projects`, the
    # busier table, for the entire duration of both index builds on `deployments`.
    # Building here costs nothing and shortens that window to the ALTER itself.
    op.execute(
        f"""
        CREATE INDEX {_SUCCESS_IDX} ON deployments (app_id, id DESC)
        WHERE status = 'succeeded' AND url IS NOT NULL
        """
    )
    op.execute(
        f"""
        CREATE INDEX {_UNPUBLISHED_IDX} ON deployments (app_id, id DESC)
        WHERE unpublished_at IS NOT NULL
        """
    )

    op.execute(
        """
        ALTER TABLE projects
        ADD COLUMN description_tsv tsvector
        GENERATED ALWAYS AS (to_tsvector('english', coalesce(description, ''))) STORED
        """
    )
    # GIN, not GiST: GIN is the standard choice for tsvector lookup workloads — slower to
    # build, materially faster to search, and this column is written far less than it is read.
    op.execute(f"CREATE INDEX {_INDEX} ON projects USING GIN (description_tsv)")

    # Hand the rest of the upgrade back its original lock behaviour — see the note above.
    op.execute("SET LOCAL lock_timeout = DEFAULT")


def downgrade() -> None:
    # The same guard `upgrade()` argues is mandatory, for the same reason: the DROP COLUMN
    # below takes ACCESS EXCLUSIVE on `projects` and queues behind any open reader, blocking
    # every request for that table while it waits. A rollback is exactly when someone is in a
    # hurry on a busy database, so the half that runs under pressure should not be the half
    # without the timeout. Reset at the end for the same reason `upgrade()` resets.
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(f"DROP INDEX IF EXISTS {_UNPUBLISHED_IDX}")
    op.execute(f"DROP INDEX IF EXISTS {_SUCCESS_IDX}")
    op.execute(f"DROP INDEX IF EXISTS {_INDEX}")
    op.execute("ALTER TABLE projects DROP COLUMN IF EXISTS description_tsv")
    op.execute("SET LOCAL lock_timeout = DEFAULT")
