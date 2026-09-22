"""An index on `conversations.updated_at` — the retention pass reads the whole table by it

Revision ID: 0046_conversation_updated_at
Revises: 0045_conversation_touch
Create Date: 2026-09-22

`TimestampMixin` declares no index, and `updated_at` is what the retention sweep both filters
and orders by on every tick, and what the conversation list orders by on every page load.
Without this each of those is a sequential scan over every conversation on the platform.

PLAIN `CREATE INDEX`, NOT CONCURRENTLY, matching 0034 and 0040. The build holds a SHARE lock,
blocking writes — and since 0045 a message send writes `conversations` too, so that is every
chat send for the duration of the build. CONCURRENTLY buys that back at the price of an
`autocommit_block()` to escape this revision's transaction and of an INVALID index left for
someone to find and drop by hand if the build is interrupted; at this table's size that is the
worse trade. The `lock_timeout` is the guard that earns its place either way: it bounds how
long the build QUEUES behind an open reader, which is the wait with no natural end.
"""

from __future__ import annotations

from alembic import op

revision: str = "0046_conversation_updated_at"
down_revision: str | None = "0045_conversation_touch"
branch_labels: str | None = None
depends_on: str | None = None

_INDEX = "ix_conversations_updated_at"


def upgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(f"CREATE INDEX {_INDEX} ON conversations (updated_at)")
    op.execute("SET LOCAL lock_timeout = DEFAULT")


def downgrade() -> None:
    op.execute("SET LOCAL lock_timeout = '5s'")
    op.execute(f"DROP INDEX IF EXISTS {_INDEX}")
    op.execute("SET LOCAL lock_timeout = DEFAULT")
