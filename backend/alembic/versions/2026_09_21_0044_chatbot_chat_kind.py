"""chatbot — a third chat kind that never has a project (issue #ChatBot)

Revision ID: 0044_chatbot_chat_kind
Revises: 0043_pending_teardown
Create Date: 2026-09-21

ChatBot is a project-less, general-purpose chat, distinct from PLAN/BUILD — it never reaches
the turn engine (`services/turns/engine.py`), which pins a live project sandbox on every turn
of both existing kinds, so it is the one kind that genuinely has no project to be scoped by.

`conversations.project_id` goes from NOT NULL to nullable; the new CHECK constraint
(`ck_conversations_chatbot_projectless`) is the actual safety net — it makes "CHATBOT never
has a project, PLAN/BUILD always do" a database invariant instead of an API-layer convention,
so a bug elsewhere cannot silently create an orphaned PLAN/BUILD row or a project-attached
CHATBOT row.

ADDITIVE, NO BACKFILL: every existing row is PLAN/BUILD with a real `project_id`, which the
new CHECK constraint accepts unchanged. `downgrade` is only safe to run while no CHATBOT rows
exist — restoring NOT NULL against a table that already has a null `project_id` row fails
outright, which is correct (a downgrade that silently deleted or reassigned those rows would
be worse). The new `chat_kind` enum label is never removed on downgrade — Postgres cannot
drop an enum label, the same constraint 0035 already documents for `mode_switch`.
"""

from __future__ import annotations

from alembic import op

revision: str = "0044_chatbot_chat_kind"
down_revision: str | None = "0043_pending_teardown"
branch_labels: str | None = None
depends_on: str | None = None


def upgrade() -> None:
    op.execute("ALTER TYPE chat_kind ADD VALUE IF NOT EXISTS 'chatbot'")
    op.alter_column("conversations", "project_id", nullable=True)
    op.create_check_constraint(
        "ck_conversations_chatbot_projectless",
        "conversations",
        "(project_id IS NULL) = (kind = 'chatbot')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_conversations_chatbot_projectless", "conversations", type_="check")
    op.alter_column("conversations", "project_id", nullable=False)
