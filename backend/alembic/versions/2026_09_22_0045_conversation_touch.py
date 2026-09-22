"""A statement-level trigger keeps `conversations.updated_at` reading "last touched by a
person" — a new message and a title/context edit share the one timestamp, because both are
that, and no new column is needed to say so.

Revision ID: 0045_conversation_touch
Revises: 0044_chat_kind_generic
Create Date: 2026-09-22

NO NEW COLUMN. `updated_at` already exists (`TimestampMixin`); its ORM-level `onupdate` still
covers the title/context PATCH — the only other writer to an existing conversation row
(verified against the whole service tree: nothing background touches one). The trigger below
covers the other half, a new message, and writes BEHIND SQLAlchemy: a session already holding
a `Conversation` object does not see the advanced value until it is refreshed.

WHY THIS EXISTS, AND WHY STATEMENT-LEVEL — THIS IS THE REPO'S FIRST TRIGGER. Messages are
appended in batches, several rows landing in one INSERT, and a ROW-level trigger fires once
per row — each firing its own UPDATE against the same conversation and leaving a dead tuple
behind for a batch that touched one conversation N times. `REFERENCING NEW TABLE` gives a
STATEMENT-level trigger one pass over every row the statement actually wrote, so it issues a
single UPDATE naming every distinct conversation touched — parity with what application code
updating the row directly would have done, whether the batch is one row or many. That UPDATE
takes a row lock on each conversation it touches for the rest of the writing transaction, so
two concurrent appends to the same conversation queue behind each other rather than racing —
proved, not assumed, by a real-concurrency regression test.

THE BACKFILL WALKS BY `messages.seq`, NOT `max(created_at)`. `uq_messages_conversation_seq`
already indexes `(conversation_id, seq)`, gap-free and server-owned, so "the newest row per
conversation" is an index seek (`ORDER BY seq DESC LIMIT 1`, per conversation) rather than a
scan of the largest table in the schema. The `LATERAL` join is also what leaves a conversation
with no messages alone: it has nothing to join against, so it keeps reading at its creation
time — the fallback the retention pass needs.

THE BACKFILL RUNS OUTSIDE THIS REVISION'S TRANSACTION, and the split is forced. `CREATE
TRIGGER` takes SHARE ROW EXCLUSIVE on `messages`, which conflicts with the ROW EXCLUSIVE every
INSERT needs, and a transaction holds a lock until it commits — so a backfill sharing that
transaction would block every message send on the platform for as long as the scan ran.
`autocommit_block()` commits the trigger first and runs the backfill on its own. The price is
that the two halves land separately: a run that dies in the backfill leaves the trigger
installed and the revision unstamped, so the DDL is written to be re-runnable and a repeated
`alembic upgrade` finishes the job.

`downgrade` DROPS THE TRIGGER AND FUNCTION ONLY; it restores no data. The trigger's writes are
ordinary UPDATEs with no prior value recorded to undo, and the backfill has no inverse worth
running — going back to "every conversation reads as last-touched at creation" would be
reintroducing a state that was already wrong before this revision, not restoring a true one. A
downgrade past this revision leaves every `updated_at` exactly where it stood.
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision: str = "0045_conversation_touch"
down_revision: str | None = "0044_chat_kind_generic"
branch_labels: str | None = None
depends_on: str | None = None

_FUNCTION_NAME = "touch_conversation_on_message_insert"
_TRIGGER_NAME = "trg_touch_conversation_on_message_insert"

# `REFERENCING NEW TABLE` is what makes this a STATEMENT-level trigger's one pass over an
# N-row INSERT rather than N row-level firings; `SELECT DISTINCT` is what keeps a batch that
# touches one conversation many times to a single UPDATE against it.
_CREATE_FUNCTION = f"""
CREATE OR REPLACE FUNCTION {_FUNCTION_NAME}() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    UPDATE conversations
    SET updated_at = now()
    WHERE id IN (SELECT DISTINCT conversation_id FROM new_rows);
    RETURN NULL;
END;
$$;
"""

_CREATE_TRIGGER = f"""
CREATE TRIGGER {_TRIGGER_NAME}
AFTER INSERT ON messages
REFERENCING NEW TABLE AS new_rows
FOR EACH STATEMENT
EXECUTE FUNCTION {_FUNCTION_NAME}();
"""

# A CORRELATED SUBQUERY, NOT A JOIN, and the shape is forced rather than chosen: PostgreSQL
# does not let the UPDATE's own target be named from its `FROM` clause, so a `FROM LATERAL`
# that reads `c.id` is rejected outright. Correlating the SET is the same seek — for each
# conversation, one look at the `(conversation_id, seq)` index for the newest row, never a
# max() over `created_at`. The `WHERE EXISTS` is what leaves a conversation with no messages
# alone: without it the subquery answers NULL and the UPDATE would write that over a perfectly
# good creation time, which is the one value the retention pass falls back to.
_BACKFILL = """
UPDATE conversations AS c
SET updated_at = (
    SELECT m.created_at
    FROM messages AS m
    WHERE m.conversation_id = c.id
    ORDER BY m.seq DESC
    LIMIT 1
)
WHERE EXISTS (SELECT 1 FROM messages AS m WHERE m.conversation_id = c.id);
"""


def upgrade() -> None:
    # RE-RUNNABLE, because the backfill below commits separately from this DDL: a run that dies
    # in the backfill leaves the trigger installed and the revision unstamped, and the retry
    # starts again here.
    op.execute(sa.text(_CREATE_FUNCTION))
    op.execute(sa.text(f"DROP TRIGGER IF EXISTS {_TRIGGER_NAME} ON messages"))
    op.execute(sa.text(_CREATE_TRIGGER))

    # The trigger is committed before the scan starts, which is what releases its SHARE ROW
    # EXCLUSIVE lock on `messages` — the lock every INSERT's ROW EXCLUSIVE conflicts with.
    # Creating the trigger FIRST is still the ordering that matters: a message appended while
    # the backfill runs is recorded by the trigger, so nothing falls between the two halves.
    with op.get_context().autocommit_block():
        op.execute(sa.text(_BACKFILL))


def downgrade() -> None:
    op.execute(sa.text(f"DROP TRIGGER {_TRIGGER_NAME} ON messages"))
    op.execute(sa.text(f"DROP FUNCTION {_FUNCTION_NAME}()"))
