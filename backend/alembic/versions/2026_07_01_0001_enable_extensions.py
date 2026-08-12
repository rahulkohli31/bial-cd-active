"""enable extensions (pgvector) and assert native uuidv7()

Revision ID: 0001_enable_extensions
Revises:
Create Date: 2026-07-01

Foundation migration: no tables yet (auth/quota/admin models land in later
phases). It provisions the `vector` extension for future embedding/RAG flows
(actual use deferred to a later phase) and asserts the server ships `uuidv7()` so an
under-versioned Postgres fails here, at migration time, rather than silently at
the first UUIDv7 PK insert (ADR-0006, ADR-0013).
"""

from __future__ import annotations

from alembic import op

revision: str = "0001_enable_extensions"
down_revision: str | None = None
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    # Fail loudly now if the server predates PostgreSQL 18's native uuidv7().
    op.execute("SELECT uuidv7()")


def downgrade() -> None:
    op.execute("DROP EXTENSION IF EXISTS vector")
