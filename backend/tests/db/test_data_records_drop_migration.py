"""Alembic round-trip for the shared-data-plane drop (0023, U6): head → 0022 → head
against the real test DB. Proves `upgrade` actually removes BOTH tables (`data_records`,
`clear_data_tokens`) AND the four `app_registry` counter columns, and that `downgrade`
recreates all six structures. Mirrors `test_app_files_drop_migration.py`: programmatic
`alembic.command` off the shared `alembic.ini`, the DB returned to head in a `finally` so a
failed assertion can't poison the rest of the suite."""

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

# Every run of this up/down round-trip permanently burns pg_attribute slots on the shared
# citizen_one_test DB (a dropped column never frees its attnum; ~1600 per table, ever), so it
# is OUT of the default lane. Run it before shipping a migration:
#   `uv run pytest -m destructive_migration`
pytestmark = pytest.mark.destructive_migration

_BACKEND_ROOT = Path(__file__).resolve().parent.parent.parent
_PRE_DROP_REVISION = "0022_project_database_registry"
_COUNTER_COLUMNS = frozenset({"data_count", "data_bytes", "file_count", "file_bytes"})
_COLUMNS_SQL = "SELECT column_name FROM information_schema.columns WHERE table_name = :table"


def _alembic_config() -> Config:
    return Config(str(_BACKEND_ROOT / "alembic.ini"))


def _snapshot() -> dict[str, Any]:
    """Introspect both dropped tables + the surviving app_registry's columns — sync wrapper
    on a fresh NullPool engine (alembic's env.py owns the loop during the commands)."""

    async def _read() -> dict[str, Any]:
        engine = create_async_engine(settings.DATABASE_URL.get_secret_value(), poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                records = await conn.scalar(text("SELECT to_regclass('data_records')"))
                tokens = await conn.scalar(text("SELECT to_regclass('clear_data_tokens')"))
                columns = set(
                    (await conn.execute(text(_COLUMNS_SQL), {"table": "app_registry"})).scalars()
                )
        finally:
            await engine.dispose()
        return {"records": records, "tokens": tokens, "app_registry_columns": columns}

    return asyncio.run(_read())


def test_drop_data_records_round_trip() -> None:
    config = _alembic_config()
    command.upgrade(config, "head")  # normalize: at head, both tables + 4 columns are dropped

    at_head = _snapshot()
    assert at_head["records"] is None
    assert at_head["tokens"] is None
    # The counters are gone, but app_registry itself survives intact — a drop that took the
    # table with it would pass a "column is absent" check and destroy the lifecycle spine.
    assert at_head["app_registry_columns"].isdisjoint(_COUNTER_COLUMNS)
    assert {"id", "app_key", "status", "project_id"} <= at_head["app_registry_columns"]

    try:
        # Downgrade to just before the drop → both tables and all four columns reappear.
        command.downgrade(config, _PRE_DROP_REVISION)
        restored = _snapshot()
        assert restored["records"] is not None
        assert restored["tokens"] is not None
        assert _COUNTER_COLUMNS <= restored["app_registry_columns"]
    finally:
        # ALWAYS return to head so the rest of the suite sees the dropped schema.
        command.upgrade(config, "head")

    final = _snapshot()
    assert final["records"] is None
    assert final["tokens"] is None
    assert final["app_registry_columns"].isdisjoint(_COUNTER_COLUMNS)
