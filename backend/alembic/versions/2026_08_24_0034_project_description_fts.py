"""Full-text search over `projects.description` — the marketplace's search index (#145).

NATIVE POSTGRES ONLY, and that is the whole point. There is no BM25 on Azure Database for
PostgreSQL Flexible Server: neither ParadeDB `pg_search` nor Tiger Data `pg_textsearch` is
on Microsoft's extension allowlist, and BM25 ships only on Azure HorizonDB, a different
product. `pg_trgm` (typo tolerance) IS allowlisted but needs BIAL infra to change a server
parameter before a migration can run — a blocking external dependency. So this uses
`tsvector` + GIN + `websearch_to_tsquery` + `ts_rank_cd`, which is core Postgres: no
extension, no server parameter, no infra request.

Worth stating why that is fine rather than a compromise. BM25 beats `ts_rank_cd` on inverse
document frequency, term-frequency saturation and document-length normalization — all three
of which start to matter at thousands of documents. This catalog will hold roughly 10-200
published apps, where the ranking formula is not what decides whether search works.

GENERATED ... STORED rather than a trigger: the column cannot drift from the description it
indexes, there is no trigger to forget on a future bulk update, and `downgrade` is two
drops. `coalesce(description, '')` because the column is nullable — a NULL description
yields an empty tsvector, which matches no query, which is exactly the documented behaviour
(an app with no description is absent from search but still present in the unfiltered
catalog).

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
    # LOCKING, for whoever runs this against production (#147 review).
    #
    # `ADD COLUMN ... GENERATED ALWAYS AS (...) STORED` is NOT the metadata-only fast path a
    # plain nullable `ADD COLUMN` gets: Postgres computes and stores the expression for every
    # existing row under ACCESS EXCLUSIVE, blocking reads AND writes on `projects` for the
    # duration. And `projects` is not the 10-200-row marketplace catalog — it is every user's
    # every project. The `CREATE INDEX` below is not CONCURRENTLY either, so it holds a SHARE
    # lock (blocking writes) while it builds.
    #
    # Both are almost certainly a non-event at this table's real size, which is why this is
    # not an `autocommit_block()` + CONCURRENTLY rewrite (that variant leaves an INVALID
    # index needing manual cleanup if it fails, a worse trade at this scale).
    #
    # The queue behaviour is the part that bites: without a `lock_timeout`, if any session
    # holds even an AccessShareLock when the ALTER queues, everything behind it stalls FIFO
    # rather than failing fast. Production alembic runs OUT OF BAND here, so the operator who
    # most needs that protection is the one least likely to be reading this file at the time
    # — hence SET rather than recommended (#147 round 3).
    #
    # AND IT IS RESET AT THE END OF `upgrade()`, which is not optional. `SET LOCAL` is scoped
    # to the TRANSACTION, not to the revision, and `alembic/env.py` leaves
    # `transaction_per_migration` at its default of False — so one `alembic upgrade` runs
    # every pending revision inside a SINGLE transaction. Without the reset below this
    # timeout would leak into every migration applied after this one in the same run, and a
    # later revision that legitimately waits more than 5s on a lock would abort with
    # `lock_not_available` and roll back the WHOLE upgrade. An earlier version of this
    # comment claimed the scoping was automatic; it is not.
    #
    # The two-arg `to_tsvector('english', ...)` is required, not stylistic: the one-arg form
    # is rejected outright with `ERROR: generation expression is not immutable`.
    op.execute("SET LOCAL lock_timeout = '5s'")
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

    # THE MARKETPLACE'S TWO COLLAPSES, indexed to match `_live_catalog`'s predicates exactly.
    # Without them each collapse Seq Scans `deployments`, and that table is append-only with
    # no reaper — so the cost tracks TOTAL HISTORICAL DEPLOY ATTEMPTS across the platform's
    # life, not the 10-200 live apps this catalog is sized for. Measured on PG18 at 51k rows:
    # ~100-180ms of DB time per request without, ~35ms with (#147 round 3).
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

    # Hand the rest of the upgrade back its original lock behaviour — see the note above.
    op.execute("SET LOCAL lock_timeout = DEFAULT")


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {_UNPUBLISHED_IDX}")
    op.execute(f"DROP INDEX IF EXISTS {_SUCCESS_IDX}")
    op.execute(f"DROP INDEX IF EXISTS {_INDEX}")
    op.execute("ALTER TABLE projects DROP COLUMN IF EXISTS description_tsv")
