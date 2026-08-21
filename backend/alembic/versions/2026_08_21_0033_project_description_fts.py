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

Revision ID: 0033_project_description_fts
Revises: 0032_rejection_standing
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0033_project_description_fts"
down_revision: str | Sequence[str] | None = "0032_rejection_standing"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_INDEX = "ix_projects_description_tsv"


def upgrade() -> None:
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


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {_INDEX}")
    op.execute("ALTER TABLE projects DROP COLUMN IF EXISTS description_tsv")
