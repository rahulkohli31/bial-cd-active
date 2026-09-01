"""Revision 0035: two three-valued enums collapse into one two-valued `chat_kind`.

TWO LANES, deliberately split (L11 — every up/down round-trip permanently burns `pg_attribute`
slots on the shared `citizen_one_test` database, so round-trip coverage is budgeted rather than
added reflexively):

* **default lane** — the SHAPE the fresh upgrade left behind, asserted against the real migrated
  schema inside the per-test transaction. Both retired PG types are gone, `chat_kind` carries
  exactly two labels, and both columns sit on it NOT NULL.
* **destructive lane** (`uv run pytest -m destructive_migration`) — the DATA STEP, which is the
  half that cannot be proved from a fresh schema. The chain is walked for real: head → 0034 →
  seed a conversation in the old shape carrying all three retired modes, a mode-switch marker,
  and an unresolved plan-options card → upgrade → assert what a migrated transcript looks like.
  Then downgrade, and prove the structure comes back.

AE38 lives in the second lane. The projection half of it — that a migrated row's PROSE still
renders, which the backfill would otherwise have silently stopped — is asserted separately and
by mutation in `tests/services/messages/test_projection.py`, because it is a property of the
projection predicate rather than of the DDL.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from alembic.config import Config
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from alembic import command
from src.config import settings
from src.db.models.conversation import ChatKind
from src.db.models.message import MessageEntryKind
from src.services.messages.store import SCHEMA_VERSION

_BACKEND_ROOT = Path(__file__).resolve().parent.parent.parent
_PRE_REVISION = "0034_project_description_fts"

_COLUMN_SQL = (
    "SELECT is_nullable, column_default, udt_name FROM information_schema.columns "
    "WHERE table_name = :table AND column_name = :column"
)
_TYPE_LABELS_SQL = (
    "SELECT enumlabel FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid "
    "WHERE t.typname = :name ORDER BY e.enumsortorder"
)


# --- the shape the fresh upgrade left -------------------------------------------------


async def test_chat_kind_exists_with_exactly_two_labels(db_session) -> None:
    labels = (
        (await db_session.execute(sa.text(_TYPE_LABELS_SQL), {"name": "chat_kind"}))
        .scalars()
        .all()
    )
    assert list(labels) == ["plan", "build"]
    # …and the Python enum agrees, so a value the database accepts is one the code can name.
    assert [member.value for member in ChatKind] == list(labels)


@pytest.mark.parametrize("retired", ["conversation_kind", "conversation_mode"])
async def test_both_retired_types_are_gone(db_session, retired: str) -> None:
    exists = await db_session.scalar(
        sa.text("SELECT 1 FROM pg_type WHERE typname = :name"), {"name": retired}
    )
    assert exists is None


@pytest.mark.parametrize("table", ["conversations", "messages"])
async def test_the_kind_column_is_the_native_type_not_null(db_session, table: str) -> None:
    row = (
        await db_session.execute(sa.text(_COLUMN_SQL), {"table": table, "column": "kind"})
    ).one()
    assert row.udt_name == "chat_kind"  # native enum (ADR-0008), not a varchar with a check
    assert row.is_nullable == "NO"
    # NO SERVER DEFAULT, on either table, and that is the point: a chat whose kind the creator
    # did not choose is a programming error, not a chat that quietly becomes one of them. The
    # column this replaced on `conversations` defaulted to 'plan'.
    assert row.column_default is None


@pytest.mark.parametrize("table", ["conversations", "messages"])
async def test_no_column_called_mode_survives_on_either_table(db_session, table: str) -> None:
    found = (
        await db_session.execute(sa.text(_COLUMN_SQL), {"table": table, "column": "mode"})
    ).all()
    assert found == []


def test_the_schema_version_was_bumped_with_the_revision() -> None:
    """Not housekeeping: the backfill rewrote every historical row's kind stamp to `build`, and
    the projection's narration drop reads that stamp. `schema_version` is what tells a rewritten
    row from a natively-written one, so without this bump a migrated Plan turn's prose would
    stop rendering on reload. See `tests/services/messages/test_projection.py`."""
    assert SCHEMA_VERSION == 2


def test_the_mode_switch_entry_kind_is_gone_from_python() -> None:
    """The PG label is deliberately left in place and INERT — swapping the type to remove one
    unreferenced label would rewrite the largest table for no behavioural gain — so the Python
    member is what carries the guarantee. Nothing writes it, and the data step deleted every row
    that held it, which is what makes dropping the member free."""
    assert not hasattr(MessageEntryKind, "MODE_SWITCH")


# --- the data step: the real chain walk (destructive lane) -----------------------------


def _alembic_config() -> Config:
    return Config(str(_BACKEND_ROOT / "alembic.ini"))


def _sql(statements: list[tuple[str, dict[str, Any]]]) -> list[Any]:
    """Run raw parameterized SQL between alembic commands on a fresh engine (alembic's env.py
    owns the loop during the commands, so this runs outside the suite's per-test transaction
    and must commit its own work)."""

    async def _run() -> list[Any]:
        engine = create_async_engine(settings.DATABASE_URL.get_secret_value(), poolclass=NullPool)
        results: list[Any] = []
        try:
            async with engine.begin() as conn:
                for statement, params in statements:
                    result = await conn.execute(sa.text(statement), params)
                    results.append(result.all() if result.returns_rows else None)
        finally:
            await engine.dispose()
        return results

    return asyncio.run(_run())


def _response_with_a_call(tool_call_id: str) -> str:
    """One stored `ModelResponse` carrying prose beside a `present_plan_options` call — the
    shape a real Plan turn persisted, and the one whose card must not survive the migration."""
    return json.dumps(
        [
            {
                "kind": "response",
                "parts": [
                    {"part_kind": "text", "content": "Here is what your visitor log will do."},
                    {
                        "part_kind": "tool-call",
                        "tool_name": "present_plan_options",
                        "args": "{}",
                        "tool_call_id": tool_call_id,
                    },
                ],
            }
        ]
    )


_PROSE = json.dumps(
    [{"kind": "response", "parts": [{"part_kind": "text", "content": "It tracks visitors."}]}]
)
_MARKER = json.dumps(
    [
        {
            "kind": "request",
            "parts": [{"part_kind": "user-prompt", "content": "[mode changed: plan → write]"}],
        }
    ]
)


@pytest.mark.destructive_migration
def test_the_data_step_over_a_conversation_in_the_old_shape() -> None:
    """★ AE38. A conversation carrying all three retired modes, a mode-switch marker, and an
    unresolved plan-options card, walked through the real revision.

    The card is the reason the data step exists at all — not a wedged conversation, which
    `repair_dangling_tool_calls` already handles, but a migrated Build chat projecting a live
    Build-it offer for a tool its new toolset does not contain, under a button nothing can
    answer."""
    config = _alembic_config()
    command.upgrade(config, "head")  # normalize the start state (a no-op when at head)

    user_id, project_id, conversation_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()
    tag = conversation_id.hex[:8]
    call_id = f"call-{tag}"
    try:
        command.downgrade(config, _PRE_REVISION)
        _sql(
            [
                (
                    "INSERT INTO users (id, azure_oid, email, token_version) "
                    "VALUES (:id, :oid, :email, 0)",
                    {"id": user_id, "oid": f"b35-{tag}", "email": f"b35-{tag}@rvaiglobal.com"},
                ),
                (
                    "INSERT INTO projects (id, user_id, name) VALUES (:id, :user_id, :name)",
                    {"id": project_id, "user_id": user_id, "name": f"Visitor Log {tag}"},
                ),
                (
                    "INSERT INTO conversations (id, user_id, project_id, kind, mode) "
                    "VALUES (:id, :user_id, :project_id, 'planning', 'plan')",
                    {"id": conversation_id, "user_id": user_id, "project_id": project_id},
                ),
                # seq 0 (ask), 1 (plan, with the unresolved card), 2 (the marker), 3 (write) —
                # all three retired modes, in the order a real thread produced them.
                (
                    "INSERT INTO messages "
                    "(user_id, conversation_id, seq, entry_kind, visibility, mode, payload) "
                    "VALUES (:u, :c, 0, 'turn', 'visible', 'ask', CAST(:p AS jsonb))",
                    {"u": user_id, "c": conversation_id, "p": _PROSE},
                ),
                (
                    "INSERT INTO messages "
                    "(user_id, conversation_id, seq, entry_kind, visibility, mode, payload, meta) "
                    "VALUES (:u, :c, 1, 'turn', 'visible', 'plan', CAST(:p AS jsonb), "
                    "        CAST(:m AS jsonb))",
                    {
                        "u": user_id,
                        "c": conversation_id,
                        "p": _response_with_a_call(call_id),
                        "m": json.dumps({"kind": "plan_options_pending", "toolCallId": call_id}),
                    },
                ),
                (
                    "INSERT INTO messages "
                    "(user_id, conversation_id, seq, entry_kind, visibility, mode, payload) "
                    "VALUES (:u, :c, 2, 'mode_switch', 'hidden', 'write', CAST(:p AS jsonb))",
                    {"u": user_id, "c": conversation_id, "p": _MARKER},
                ),
                (
                    "INSERT INTO messages "
                    "(user_id, conversation_id, seq, entry_kind, visibility, mode, payload) "
                    "VALUES (:u, :c, 3, 'step', 'visible', 'write', CAST(:p AS jsonb))",
                    {"u": user_id, "c": conversation_id, "p": _PROSE},
                ),
            ]
        )

        command.upgrade(config, "head")

        [[conversation], rows] = _sql(
            [
                (
                    "SELECT kind::text AS kind FROM conversations WHERE id = :id",
                    {"id": conversation_id},
                ),
                (
                    "SELECT seq, entry_kind::text AS entry_kind, visibility::text AS visibility, "
                    "       kind::text AS kind, schema_version, meta "
                    "FROM messages WHERE conversation_id = :c ORDER BY seq",
                    {"c": conversation_id},
                ),
            ]
        )

        # R53's honest mapping: every migrated conversation becomes a Build chat, because "was
        # this a Plan chat?" is not a question the stored rows can answer — any conversation was
        # one mode switch away from writing files.
        assert conversation.kind == "build"

        # The marker is DELETED, not carried across as an inert row. Its Python enum member is
        # gone, so a surviving row would raise on the first read of that conversation.
        assert all(row.entry_kind != "mode_switch" for row in rows)

        # Everything else survives, in order, with its stamp rewritten.
        assert [row.seq for row in rows] == [0, 1, 3, 4]
        assert {row.kind for row in rows} == {"build"}

        # The pre-migration rows keep their OWN schema version — that is the whole point of the
        # bump, and a data step that rewrote it would silently un-render their prose.
        assert [row.schema_version for row in rows[:3]] == [1, 1, 1]

        # The card is retired, exactly once, in the shape `record_build_failure` already writes:
        # a hidden system overlay with an empty payload. NOT a synthesized `ToolReturnPart` —
        # no historical payload is rewritten.
        overlay = rows[-1]
        assert overlay.entry_kind == "system_event"
        assert overlay.visibility == "hidden"
        assert overlay.meta == {
            "kind": "plan_options_resolved",
            "toolCallId": call_id,
            "choice": "refine",
        }
        assert overlay.schema_version == SCHEMA_VERSION
    finally:
        _sql([("DELETE FROM users WHERE id = :id", {"id": user_id})])
        command.upgrade(config, "head")


@pytest.mark.destructive_migration
def test_downgrade_restores_the_structure_and_upgrade_takes_it_away_again() -> None:
    """The structure comes back; the DISTINCTIONS do not, and that is deliberate — the same
    one-way-door posture as revision 0024. Nothing can reconstruct which conversations were
    once `planning` rather than `builder`, so the downgrade stamps them all `builder` /
    `write` rather than inventing an answer."""
    config = _alembic_config()
    command.upgrade(config, "head")

    def _types() -> set[str]:
        [rows] = _sql(
            [
                (
                    "SELECT typname FROM pg_type WHERE typname IN "
                    "('chat_kind', 'conversation_kind', 'conversation_mode')",
                    {},
                )
            ]
        )
        return {row.typname for row in rows}

    def _columns(table: str) -> set[str]:
        [rows] = _sql(
            [
                (
                    "SELECT column_name FROM information_schema.columns WHERE table_name = :t",
                    {"t": table},
                )
            ]
        )
        return {row.column_name for row in rows}

    assert _types() == {"chat_kind"}
    assert "mode" not in _columns("conversations")
    assert "kind" in _columns("messages") and "mode" not in _columns("messages")

    try:
        command.downgrade(config, _PRE_REVISION)
        assert _types() == {"conversation_kind", "conversation_mode"}
        assert {"kind", "mode"} <= _columns("conversations")
        assert "mode" in _columns("messages") and "kind" not in _columns("messages")
    finally:
        command.upgrade(config, "head")

    assert _types() == {"chat_kind"}
    assert "mode" not in _columns("conversations")
