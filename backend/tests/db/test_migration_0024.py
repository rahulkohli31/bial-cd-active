"""Alembic round-trip for the destructive messages reset (0024, U4): head → 0023 → head
against the real test DB — WITH seeded legacy data, because this revision's whole point is
destroying it. At 0023 the legacy shape is recreated and seeded (a conversation carrying a
`code` snapshot, a parts-shaped message, an attachment linked to the conversation); the
upgrade must then prove:

  * every conversation row is gone (and the legacy message with it);
  * the attachment ROW survives with its conversation link severed to NULL (blob-aware
    cleanup is the reclaimer's job — a migration must never strand a blob);
  * `messages` is reborn in the native shape (entry_kind/visibility/mode/payload/meta;
    role/parts gone) and `message_role` is dropped while the three new enums exist;
  * `conversations` gains `mode` and loses `code`.

Mirrors `test_data_records_drop_migration.py`: programmatic `alembic.command` off the shared
`alembic.ini`, the DB returned to head in a `finally` so a failed assertion can't poison the
rest of the suite.
"""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any

import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from alembic import command
from src.config import settings

# Every run of this up/down round-trip permanently burns pg_attribute slots on the shared
# citizen_one_test DB (a dropped column never frees its attnum; ~1600 per table, ever), so it
# is OUT of the default lane. Run it before shipping a migration:
#   `uv run pytest -m destructive_migration`
pytestmark = pytest.mark.destructive_migration

_BACKEND_ROOT = Path(__file__).resolve().parent.parent.parent
_PRE_RESET_REVISION = "0023_drop_data_records"
_COLUMNS_SQL = "SELECT column_name FROM information_schema.columns WHERE table_name = :table"
_ENUM_SQL = "SELECT 1 FROM pg_type WHERE typname = :name"

_NATIVE_COLUMNS = frozenset({"entry_kind", "visibility", "mode", "payload", "meta"})
_LEGACY_COLUMNS = frozenset({"role", "parts"})


def _alembic_config() -> Config:
    return Config(str(_BACKEND_ROOT / "alembic.ini"))


def _run(coro_factory) -> Any:
    """Run one async step on a fresh NullPool engine (alembic's env.py owns the loop during
    the commands, so these run between them)."""

    async def _wrapped() -> Any:
        engine = create_async_engine(settings.DATABASE_URL.get_secret_value(), poolclass=NullPool)
        try:
            async with engine.begin() as conn:
                return await coro_factory(conn)
        finally:
            await engine.dispose()

    return asyncio.run(_wrapped())


def _snapshot() -> dict[str, Any]:
    async def _read(conn) -> dict[str, Any]:
        messages_columns = set(
            (await conn.execute(text(_COLUMNS_SQL), {"table": "messages"})).scalars()
        )
        conversations_columns = set(
            (await conn.execute(text(_COLUMNS_SQL), {"table": "conversations"})).scalars()
        )
        enums = {
            name: (await conn.execute(text(_ENUM_SQL), {"name": name})).scalar() is not None
            for name in (
                "message_role",
                "conversation_mode",
                "message_entry_kind",
                "message_visibility",
            )
        }
        conversation_count = await conn.scalar(text("SELECT count(*) FROM conversations"))
        message_count = await conn.scalar(text("SELECT count(*) FROM messages"))
        return {
            "messages_columns": messages_columns,
            "conversations_columns": conversations_columns,
            "enums": enums,
            "conversations": conversation_count,
            "messages": message_count,
        }

    return _run(_read)


def _seed_legacy() -> dict[str, uuid.UUID]:
    """Seed a legacy thread at 0023: user → project → conversation (with `code`) → parts
    message → attachment linked to the conversation. Raw SQL — the ORM models now describe
    the NATIVE shape and must not be let near the legacy one."""
    ids = {
        "user": uuid.uuid4(),
        "project": uuid.uuid4(),
        "conversation": uuid.uuid4(),
        "attachment": uuid.uuid4(),
    }

    async def _write(conn) -> None:
        await conn.execute(
            text("INSERT INTO users (id, azure_oid, email) VALUES (:id, :oid, :email)"),
            {"id": ids["user"], "oid": f"legacy-{ids['user']}", "email": "legacy@rvaiglobal.com"},
        )
        await conn.execute(
            text("INSERT INTO projects (id, user_id, name) VALUES (:id, :user, 'Legacy')"),
            {"id": ids["project"], "user": ids["user"]},
        )
        await conn.execute(
            text(
                "INSERT INTO conversations (id, user_id, project_id, kind, code) "
                "VALUES (:id, :user, :project, 'builder', '{\"current\": {}}'::jsonb)"
            ),
            {"id": ids["conversation"], "user": ids["user"], "project": ids["project"]},
        )
        await conn.execute(
            text(
                "INSERT INTO messages (user_id, conversation_id, role, seq, parts) "
                "VALUES (:user, :conversation, 'user', 0, '[{\"type\": \"text\"}]'::jsonb)"
            ),
            {"user": ids["user"], "conversation": ids["conversation"]},
        )
        await conn.execute(
            text(
                "INSERT INTO attachments "
                "(id, user_id, attachment_id, media_type, size, storage_key, conversation_id) "
                "VALUES (:id, :user, 'att-legacy', 'image/png', 4, :key, :conversation)"
            ),
            {
                "id": ids["attachment"],
                "user": ids["user"],
                "key": f"att/{ids['user']}/att-legacy",
                "conversation": ids["conversation"],
            },
        )

    _run(_write)
    return ids


def _cleanup_seed(ids: dict[str, uuid.UUID]) -> None:
    """Remove the seeded user tree (attachment/project cascade with the user)."""

    async def _delete(conn) -> None:
        await conn.execute(text("DELETE FROM users WHERE id = :id"), {"id": ids["user"]})

    _run(_delete)


def test_messages_native_reset_round_trip() -> None:
    config = _alembic_config()
    command.upgrade(config, "head")  # normalize

    at_head = _snapshot()
    assert _NATIVE_COLUMNS <= at_head["messages_columns"]
    assert at_head["messages_columns"].isdisjoint(_LEGACY_COLUMNS)
    assert "mode" in at_head["conversations_columns"]
    assert "code" not in at_head["conversations_columns"]
    assert at_head["enums"] == {
        "message_role": False,
        "conversation_mode": True,
        "message_entry_kind": True,
        "message_visibility": True,
    }

    ids: dict[str, uuid.UUID] | None = None
    try:
        # Down to 0023: the legacy shape returns (empty), ready to seed.
        command.downgrade(config, _PRE_RESET_REVISION)
        legacy = _snapshot()
        assert _LEGACY_COLUMNS <= legacy["messages_columns"]
        assert legacy["messages_columns"].isdisjoint(_NATIVE_COLUMNS)
        assert "code" in legacy["conversations_columns"]
        assert "mode" not in legacy["conversations_columns"]
        assert legacy["enums"]["message_role"] is True

        ids = _seed_legacy()

        # THE RESET: legacy data destroyed, native shape in place.
        command.upgrade(config, "head")
        reset = _snapshot()
        assert reset["conversations"] == 0
        assert reset["messages"] == 0
        assert _NATIVE_COLUMNS <= reset["messages_columns"]

        async def _attachment_state(conn) -> tuple[int, uuid.UUID | None]:
            row = (
                await conn.execute(
                    text("SELECT conversation_id FROM attachments WHERE id = :id"),
                    {"id": ids["attachment"]},
                )
            ).one_or_none()
            count = 1 if row is not None else 0
            return count, row[0] if row is not None else None

        count, link = _run(_attachment_state)
        # The upload ROW survives with its link severed — the reclaimer owns blob-aware
        # cleanup; a migration that CASCADE-deleted the row would strand the blob forever.
        assert count == 1
        assert link is None
    finally:
        command.upgrade(config, "head")
        if ids is not None:
            _cleanup_seed(ids)

    final = _snapshot()
    assert _NATIVE_COLUMNS <= final["messages_columns"]
    assert final["messages_columns"].isdisjoint(_LEGACY_COLUMNS)
