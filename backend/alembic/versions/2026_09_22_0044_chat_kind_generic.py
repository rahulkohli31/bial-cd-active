"""chat_kind gains `generic`, and a conversation without a project becomes representable

Revision ID: 0044_chat_kind_generic
Revises: 0043_pending_teardown
Create Date: 2026-09-22

A third kind of chat: one that belongs to a citizen rather than to a project, is handed no
toolset and resolves no container. Two changes carry it, and they belong together —
`conversations.project_id` may only become nullable if something holds the line for the two
kinds that must still have one.

THE TYPE IS REBUILT, NOT EXTENDED. `ALTER TYPE … ADD VALUE` is instant and needs no rewrite,
but it has no inverse in PostgreSQL and it cannot be USED in the transaction that adds it —
which would push the CHECK constraint below into a second revision. Rebuilding matches how
`chat_kind` itself was built (0035) and downgrades cleanly. The cost is the cast: both kind
columns are rewritten under ACCESS EXCLUSIVE, and `messages` is the largest table, so the lock
window scales with accumulated history. `alembic/env.py` runs a transaction per migration, so
that rewrite commits on its own rather than inside a longer run.

`ck_conversations_parentage` IS ONE CONSTRAINT OVER BOTH DIRECTIONS, spelled as a biconditional
so a violation names the shape that is wrong rather than half of it. `kind` carries its own NOT
NULL, and that is not redundant: a NULL kind makes the equality UNKNOWN, and a CHECK constraint
PASSES on UNKNOWN.

THE LABELS ARE WRITTEN OUT AS LITERALS, never imported from `src.db.models.conversation` — a
migration is a historical record and must not change the day that model does (ADR-0008). Both
type lifecycles are explicit (`create_type=False`).

`downgrade` restores the STRUCTURE and refuses to guess: a generic conversation has no project
to restore, so the downgrade DELETES those rows (and their messages, which cascade) rather than
inventing parentage for them. Treat a downgrade past this revision on a live database as a
data-loss operation.
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0044_chat_kind_generic"
down_revision: str | None = "0043_pending_teardown"
branch_labels: str | None = None
depends_on: str | None = None

# Labels spelled out, never imported. `create_type=False` so THIS migration owns each lifecycle.
chat_kind_next = postgresql.ENUM("plan", "build", "generic", name="chat_kind", create_type=False)
chat_kind_prev = postgresql.ENUM("plan", "build", name="chat_kind", create_type=False)

# The parentage rule, as one expression. Mirrors `Conversation.PARENTAGE_SHAPE`.
PARENTAGE_SHAPE = "(kind = 'generic') = (project_id IS NULL)"

_SWAP = "ALTER TABLE {table} ALTER COLUMN kind TYPE chat_kind USING kind::text::chat_kind"


def _rebuild(target: postgresql.ENUM) -> None:
    """Swap `chat_kind` for a type carrying `target`'s labels, rewriting both kind columns.

    The old type is renamed out of the way rather than dropped first: a type in use cannot be
    dropped, and the columns can only be moved once the new type exists under the real name.
    """
    op.execute(sa.text("ALTER TYPE chat_kind RENAME TO chat_kind_old"))
    target.create(op.get_bind(), checkfirst=False)
    for table in ("conversations", "messages"):
        op.execute(sa.text(_SWAP.format(table=table)))
    op.execute(sa.text("DROP TYPE chat_kind_old"))


def upgrade() -> None:
    _rebuild(chat_kind_next)

    op.alter_column("conversations", "project_id", existing_type=sa.Uuid(), nullable=True)
    op.create_check_constraint("ck_conversations_parentage", "conversations", PARENTAGE_SHAPE)


def downgrade() -> None:
    op.drop_constraint("ck_conversations_parentage", "conversations", type_="check")

    # A generic chat has no project, so there is nothing to make it NOT NULL with. Its messages
    # cascade; its attachment rows are SET NULL by their own foreign key and are then reachable
    # only by the orphan reclaimer.
    op.execute(sa.text("DELETE FROM conversations WHERE kind = 'generic'"))

    op.alter_column("conversations", "project_id", existing_type=sa.Uuid(), nullable=False)
    _rebuild(chat_kind_prev)
