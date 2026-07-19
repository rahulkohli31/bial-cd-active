"""Alembic round-trip for the app_registry.name drop (0020, #48/F5): head → 0019 → head
against the real test DB. Proves `upgrade` removes the `name` column and `downgrade`
recreates its STRUCTURE (`String(120) NOT NULL server_default=""`). The DATA is NOT restored
— a schema-only round-trip. Mirrors `test_app_files_drop_migration.py`: programmatic
`alembic.command` off the shared `alembic.ini`, the DB returned to head in a `finally` so a
failed assertion can't poison the rest of the suite (0020 is the head, so `upgrade head`
restores the dropped-column state)."""

from __future__ import annotations

import asyncio
from pathlib import Path

from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from alembic import command
from src.config import settings

_BACKEND_ROOT = Path(__file__).resolve().parent.parent.parent
_PRE_DROP_REVISION = "0019_app_registry_deployed_url"
_COLUMN_SQL = (
    "SELECT 1 FROM information_schema.columns "
    "WHERE table_name = 'app_registry' AND column_name = 'name'"
)


def _alembic_config() -> Config:
    return Config(str(_BACKEND_ROOT / "alembic.ini"))


def _name_column_present() -> int | None:
    """1 if app_registry.name exists in the live test DB, else None — sync wrapper on a
    fresh NullPool engine (alembic's env.py owns the loop during the commands)."""

    async def _read() -> int | None:
        engine = create_async_engine(settings.DATABASE_URL.get_secret_value(), poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                return await conn.scalar(text(_COLUMN_SQL))
        finally:
            await engine.dispose()

    return asyncio.run(_read())


def test_drop_app_registry_name_round_trip() -> None:
    config = _alembic_config()
    command.upgrade(config, "head")  # normalize: at head (0020) the name column is gone

    assert _name_column_present() is None

    try:
        # Downgrade to just before the drop → the column reappears (structure only, no data).
        command.downgrade(config, _PRE_DROP_REVISION)
        assert _name_column_present() == 1
    finally:
        # ALWAYS return to head so the rest of the suite sees the dropped column.
        command.upgrade(config, "head")

    assert _name_column_present() is None
