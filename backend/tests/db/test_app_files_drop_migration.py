"""Alembic round-trip for the app_files drop (0017, OPEN-SANDBOX): head → 0016 → head
against the real test DB. Proves `upgrade` actually removes the `app_files` table AND the
`app_file_status` PG enum, and `downgrade` recreates the structure. Mirrors
`test_migration_downgrade.py`: programmatic `alembic.command` off the shared `alembic.ini`,
the DB returned to head in a `finally` so a failed assertion can't poison the rest of the
suite (0017 is the head, so `upgrade head` is a no-op restore)."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from alembic import command
from src.config import settings

_BACKEND_ROOT = Path(__file__).resolve().parent.parent.parent
_PRE_DROP_REVISION = "0016_user_suspended_at"
_ENUM_SQL = "SELECT 1 FROM pg_type WHERE typname = 'app_file_status'"


def _alembic_config() -> Config:
    return Config(str(_BACKEND_ROOT / "alembic.ini"))


def _snapshot() -> dict[str, Any]:
    """Introspect whether app_files (table) + app_file_status (enum) exist — sync wrapper
    on a fresh NullPool engine (alembic's env.py owns the loop during the commands)."""

    async def _read() -> dict[str, Any]:
        engine = create_async_engine(settings.DATABASE_URL.get_secret_value(), poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                table = await conn.scalar(text("SELECT to_regclass('app_files')"))
                enum_present = await conn.scalar(text(_ENUM_SQL))
        finally:
            await engine.dispose()
        return {"table": table, "enum_present": enum_present}

    return asyncio.run(_read())


def test_drop_app_files_round_trip() -> None:
    config = _alembic_config()
    command.upgrade(config, "head")  # normalize: at head, app_files + its enum are dropped

    # At head (0017) the table and its native enum are gone.
    at_head = _snapshot()
    assert at_head["table"] is None
    assert at_head["enum_present"] is None

    try:
        # Downgrade to just before the drop → the table + enum reappear (structure only).
        command.downgrade(config, _PRE_DROP_REVISION)
        restored = _snapshot()
        assert restored["table"] is not None
        assert restored["enum_present"] == 1
    finally:
        # ALWAYS return to head so the rest of the suite sees the dropped schema.
        command.upgrade(config, "head")

    final = _snapshot()
    assert final["table"] is None
    assert final["enum_present"] is None
