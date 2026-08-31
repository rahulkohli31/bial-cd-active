"""chat_kind — two three-valued concepts collapse into one two-valued one, on both tables

Revision ID: 0035_chat_kind
Revises: 0034_project_description_fts
Create Date: 2026-08-30

WHAT THIS IS. `conversation_kind` (planning / assistant / builder) and `conversation_mode`
(ask / plan / write) were two independent three-valued enums that between them decided one
thing: what a run is allowed to do. They become one two-valued `chat_kind` (plan / build),
fixed at creation, on both `conversations` and `messages`.

THE MAPPING IS HONEST, NOT CLEVER: **every existing row on both tables becomes `build`** (R53).
Until now any conversation could be switched into building at any moment, so "was this a Plan
chat?" is not a question the stored rows can answer — a conversation stamped `plan` was one
mode switch away from writing files, and the per-row `mode` stamp records where a batch ran,
not what the chat was for. Claiming otherwise would invent a distinction the data does not
carry. Every migrated chat therefore opens as a Build chat, with its full transcript intact.

`messages.mode` is RENAMED to `messages.kind` rather than reused under the old name: every
reader changes anyway, and leaving a column called `mode` behind is how a deleted vocabulary
survives. A rename is one statement, preserves the data, and burns no fresh `pg_attribute`
slots on the shared test database the way add-copy-drop would.

TWO DATA STEPS RIDE WITH THE DDL, and both exist because a migrated transcript must not be
left holding something the new code cannot answer:

  * **The mode-switch marker rows are deleted.** They were hidden, they rendered nothing
    (`services/messages/projection.py` skipped them), and their whole job was to tell the model
    where in history the mode changed. There are no mode boundaries any more. Once no row
    carries the label, the `mode_switch` member goes from the Python enum; the PG label is left
    in place and inert, because swapping the type to remove one unreferenced label would
    rewrite the largest table for no behavioural gain.

  * **Every outstanding plan-options card is resolved as `refine`.** Not because a migrated
    conversation would wedge — it would not: `repair_dangling_tool_calls` is the one choke
    point where history is assembled and its first documented case is "no answer anywhere → a
    synthesized 'interrupted' result is stitched in". The reason is the CARD: a migrated Build
    chat would otherwise project a live Build-it offer for a tool its new toolset does not
    contain, and the citizen would press a button nothing can answer. The overlay written here
    is exactly the shape `plan_options.record_build_failure` already writes — `entry_kind =
    system_event`, `visibility = hidden`, empty payload, `meta.kind = plan_options_resolved`,
    `choice = refine`. Deliberately NOT a `ToolReturnPart`: no historical payload is rewritten
    and no `ToolReturnPart` is synthesized, because the model-history half is already covered.

`downgrade` restores the STRUCTURE, not the distinctions: both retired types come back and both
columns return to them, with every row reading `builder` / `write`. The deleted markers and the
resolution overlays do not come back. This is the same one-way-door posture as 0024, and it is
deliberate — the distinctions were never recoverable from the rows.

Hand-finalized (ADR-0013). ADR-0008 governs the native enums; this revision owns all three type
lifecycles explicitly (`create_type=False` on every one).
"""

from __future__ import annotations

import json
from typing import Any

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "0035_chat_kind"
down_revision: str | None = "0034_project_description_fts"
branch_labels: str | None = None
depends_on: str | None = None

# Native enums (ADR-0008) — `create_type=False` so THIS migration owns each lifecycle.
chat_kind = postgresql.ENUM("plan", "build", name="chat_kind", create_type=False)
conversation_kind = postgresql.ENUM(
    "planning", "assistant", "builder", name="conversation_kind", create_type=False
)
conversation_mode = postgresql.ENUM(
    "ask", "plan", "write", name="conversation_mode", create_type=False
)

PLAN_OPTIONS_TOOL = "present_plan_options"
META_PENDING = "plan_options_pending"
META_RESOLVED = "plan_options_resolved"

# The payload serialization contract these overlay rows are written under. They carry an EMPTY
# payload, so the stamp is unobservable either way; it reads 2 because the revision ships with
# the code that writes 2, and a row must never claim an older contract than the one it was
# written under. See `services/messages/store.SCHEMA_VERSION`.
_OVERLAY_SCHEMA_VERSION = 2


def _is_open(resolution: str | None) -> bool:
    """A card is still ACTIONABLE — and so still presses a dead button after the migration —
    when it has no resolution at all, OR its resolution is a `build_failed`, which RE-ARMS the
    card (`plan_options._is_open_resolution`). Both must be closed here, and closing only the
    newest per conversation would not be enough: an older unresolved card projects as `pending`
    too, so it draws its own live offer."""
    return resolution is None or resolution.startswith("build_failed")


def _resolve_outstanding_plan_options() -> None:
    """Write one `refine` overlay per outstanding card, newest seq onward, per conversation.

    Done in Python rather than as one statement because the resolutions live in two places —
    row `meta` for synthesized/overlay records and a `ToolReturnPart` inside the native JSONB
    payload for real ones — and walking the payload in SQL would encode `_scan`'s reading of
    the wire shape into a second, unversioned place. The row count is small (a dev database
    returned 32 conversations), and `messages.id` carries a `uuidv7()` server default, so the
    ids these inserts mint are UUIDv7 without the revision minting them itself.
    """
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT id, user_id, conversation_id, seq, meta, payload "
            "FROM messages ORDER BY conversation_id, seq"
        )
    ).mappings()

    pendings: dict[Any, list[str]] = {}
    resolutions: dict[Any, dict[str, str]] = {}
    owners: dict[Any, Any] = {}
    head_seq: dict[Any, int] = {}

    for row in rows:
        conversation_id = row["conversation_id"]
        owners.setdefault(conversation_id, row["user_id"])
        head_seq[conversation_id] = max(head_seq.get(conversation_id, -1), int(row["seq"]))
        meta = row["meta"] if isinstance(row["meta"], dict) else {}
        call_id = meta.get("toolCallId")
        if meta.get("kind") == META_PENDING and isinstance(call_id, str):
            pendings.setdefault(conversation_id, []).append(call_id)
        if meta.get("kind") == META_RESOLVED and isinstance(call_id, str):
            resolutions.setdefault(conversation_id, {})[call_id] = str(
                meta.get("choice", "refine")
            )
        payload = row["payload"]
        if isinstance(payload, str):  # a driver that hands JSONB back as text
            payload = json.loads(payload)
        for message in payload if isinstance(payload, list) else []:
            if not isinstance(message, dict):
                continue
            for part in message.get("parts", []):
                if (
                    isinstance(part, dict)
                    and part.get("part_kind") == "tool-return"
                    and part.get("tool_name") == PLAN_OPTIONS_TOOL
                    and isinstance(part.get("tool_call_id"), str)
                ):
                    resolutions.setdefault(conversation_id, {})[part["tool_call_id"]] = str(
                        part.get("content", "")
                    )

    insert = sa.text(
        "INSERT INTO messages "
        "(user_id, conversation_id, seq, schema_version, entry_kind, visibility, kind, "
        " payload, meta) "
        "VALUES (:user_id, :conversation_id, :seq, :schema_version, 'system_event', 'hidden', "
        " 'build', '[]'::jsonb, CAST(:meta AS jsonb))"
    )
    for conversation_id, call_ids in pendings.items():
        answered = resolutions.get(conversation_id, {})
        next_seq = head_seq[conversation_id] + 1
        for call_id in call_ids:
            if not _is_open(answered.get(call_id)):
                continue
            bind.execute(
                insert,
                {
                    "user_id": owners[conversation_id],
                    "conversation_id": conversation_id,
                    "seq": next_seq,
                    "schema_version": _OVERLAY_SCHEMA_VERSION,
                    "meta": json.dumps(
                        {"kind": META_RESOLVED, "toolCallId": call_id, "choice": "refine"}
                    ),
                },
            )
            next_seq += 1


def upgrade() -> None:
    # The retired markers go first: fewer rows for the type rewrite below to touch, and the
    # `mode_switch` label is inert from this point on.
    op.execute(sa.text("DELETE FROM messages WHERE entry_kind = 'mode_switch'"))

    chat_kind.create(op.get_bind(), checkfirst=True)

    # `conversations`: the kind column swaps type (every row becomes `build` — R53), and the
    # mode column goes entirely. Dropping the column takes its server default with it, which is
    # what lets `conversation_mode` be dropped at the end.
    op.execute(
        sa.text(
            "ALTER TABLE conversations ALTER COLUMN kind TYPE chat_kind USING 'build'::chat_kind"
        )
    )
    op.drop_column("conversations", "mode")

    # `messages`: RENAME then retype, so the data survives and no fresh `pg_attribute` slot is
    # burned (an add-copy-drop would cost two per round-trip test run, permanently, on the
    # shared test database).
    op.execute(sa.text("ALTER TABLE messages RENAME COLUMN mode TO kind"))
    op.execute(
        sa.text("ALTER TABLE messages ALTER COLUMN kind TYPE chat_kind USING 'build'::chat_kind")
    )

    # Only now, with every row stamped, is it safe to retire a card that would otherwise draw a
    # Build-it button for a tool the migrated chat's toolset does not contain.
    _resolve_outstanding_plan_options()

    conversation_kind.drop(op.get_bind(), checkfirst=True)
    conversation_mode.drop(op.get_bind(), checkfirst=True)


def downgrade() -> None:
    conversation_kind.create(op.get_bind(), checkfirst=True)
    conversation_mode.create(op.get_bind(), checkfirst=True)

    op.execute(
        sa.text(
            "ALTER TABLE messages ALTER COLUMN kind TYPE conversation_mode "
            "USING 'write'::conversation_mode"
        )
    )
    op.execute(sa.text("ALTER TABLE messages RENAME COLUMN kind TO mode"))

    op.execute(
        sa.text(
            "ALTER TABLE conversations "
            "ALTER COLUMN kind TYPE conversation_kind USING 'builder'::conversation_kind"
        )
    )
    op.add_column(
        "conversations",
        sa.Column(
            "mode",
            conversation_mode,
            nullable=False,
            server_default=sa.text("'plan'::conversation_mode"),
        ),
    )

    chat_kind.drop(op.get_bind(), checkfirst=True)
