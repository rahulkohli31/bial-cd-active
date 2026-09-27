"""Alembic round trip for the per-person connector ledger's drop: head → 0046 → head, against the
real test database.

`upgrade` removes `connector_access_requests` and the `connector_request_status` type and leaves
`project_connectors` and `connector_window_kind` alone; `downgrade` recreates the ledger with the
shape it had, partial unique index included. The database is returned to head in a `finally`, so
a failed assertion cannot poison the rest of the suite.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from alembic import command
from src.config import settings

# Every run of this round trip permanently burns pg_attribute slots on the shared test database
# (a dropped column never frees its attnum), so it is out of the default lane:
#   `uv run pytest -m destructive_migration`
pytestmark = pytest.mark.destructive_migration

_BACKEND_ROOT = Path(__file__).resolve().parent.parent.parent
_PRE_DROP_REVISION = "0046_conversation_updated_at"


def _snapshot() -> dict[str, Any]:
    """Which connector tables, types and ledger indexes exist — on a fresh NullPool engine,
    because alembic's env.py owns the event loop while the commands run."""

    async def _read() -> dict[str, Any]:
        engine = create_async_engine(settings.DATABASE_URL.get_secret_value(), poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                ledger = await conn.scalar(text("SELECT to_regclass('connector_access_requests')"))
                project = await conn.scalar(text("SELECT to_regclass('project_connectors')"))
                labels = (
                    await conn.scalars(
                        text(
                            "SELECT enumlabel FROM pg_enum e "
                            "JOIN pg_type t ON t.oid = e.enumtypid "
                            "WHERE t.typname = 'connector_request_status' "
                            "ORDER BY e.enumsortorder"
                        )
                    )
                ).all()
                window_kind = await conn.scalar(
                    text("SELECT 1 FROM pg_type WHERE typname = 'connector_window_kind'")
                )
                index_rows = await conn.execute(
                    text(
                        "SELECT indexname, indexdef FROM pg_indexes "
                        "WHERE tablename = 'connector_access_requests'"
                    )
                )
                indexes: dict[str, str] = {name: sql for name, sql in index_rows.all()}
                foreign_key_rows = await conn.execute(
                    text(
                        "SELECT a.attname, c.confdeltype::text FROM pg_constraint c "
                        "JOIN pg_attribute a ON a.attrelid = c.conrelid "
                        "AND a.attnum = ANY(c.conkey) "
                        "WHERE c.conrelid = to_regclass('connector_access_requests') "
                        "AND c.contype = 'f'"
                    )
                )
                foreign_keys: dict[str, str] = {
                    column: action for column, action in foreign_key_rows.all()
                }
        finally:
            await engine.dispose()
        return {
            "ledger": ledger,
            "project": project,
            "labels": list(labels),
            "window_kind": window_kind,
            "indexes": indexes,
            "foreign_keys": foreign_keys,
        }

    return asyncio.run(_read())


def test_the_ledger_drop_round_trips() -> None:
    config = Config(str(_BACKEND_ROOT / "alembic.ini"))
    command.upgrade(config, "head")

    at_head = _snapshot()
    assert at_head["project"] is not None
    assert at_head["window_kind"] == 1
    assert at_head["ledger"] is None
    assert at_head["labels"] == []

    try:
        command.downgrade(config, _PRE_DROP_REVISION)
        restored = _snapshot()
        assert restored["ledger"] is not None
        assert restored["labels"] == ["pending", "approved", "declined", "cancelled"]
        assert restored["project"] is not None
        assert restored["window_kind"] == 1
        assert set(restored["indexes"]) == {
            "connector_access_requests_pkey",
            "ix_connector_access_requests_user_id",
            "ix_connector_access_requests_user_connector",
            "uq_connector_access_requests_one_pending",
        }
        one_pending = restored["indexes"]["uq_connector_access_requests_one_pending"]
        assert one_pending.startswith("CREATE UNIQUE INDEX")
        assert "(user_id, connector_key)" in one_pending
        assert "WHERE (status = 'pending'::connector_request_status)" in one_pending
        # `c` is CASCADE and `n` is SET NULL: the asks go with the person, the decision outlives
        # the administrator who made it.
        assert restored["foreign_keys"] == {"user_id": "c", "decided_by_id": "n"}
    finally:
        command.upgrade(config, "head")

    final = _snapshot()
    assert final["ledger"] is None
    assert final["labels"] == []
    assert final["project"] is not None
    assert final["window_kind"] == 1
