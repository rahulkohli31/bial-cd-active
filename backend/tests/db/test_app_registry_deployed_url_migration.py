"""Alembic round-trip for the deployed-URL column (0019, PILOT R5): head → 0018 →
head against the real test DB. Proves `upgrade` adds the nullable `deployed_url`
column, that an existing row survives the upgrade with a NULL address (every row
predates the column — a NOT NULL here would fail the migration outright), and that
`downgrade` drops it cleanly. Mirrors `test_app_registry_submissions_migration.py`:
programmatic `alembic.command` off the shared `alembic.ini`, DB returned to head in
a `finally` so a failed assertion can't poison the rest of the suite."""

from __future__ import annotations

import asyncio
import uuid
from pathlib import Path
from typing import Any

from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from alembic import command
from src.config import settings

_BACKEND_ROOT = Path(__file__).resolve().parent.parent.parent
_PRE_URL_REVISION = "0018_app_registry_submissions"
_COLUMN = "deployed_url"


def _alembic_config() -> Config:
    return Config(str(_BACKEND_ROOT / "alembic.ini"))


def _run_sql(work) -> Any:
    """Run one async callable against a fresh NullPool engine — sync wrapper
    (alembic's env.py owns the loop during the commands)."""

    async def _go() -> Any:
        engine = create_async_engine(settings.DATABASE_URL.get_secret_value(), poolclass=NullPool)
        try:
            async with engine.begin() as conn:
                return await work(conn)
        finally:
            await engine.dispose()

    return asyncio.run(_go())


def _columns() -> set[str]:
    async def _read(conn) -> set[str]:
        rows = await conn.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'app_registry'"
            )
        )
        return {row[0] for row in rows}

    return _run_sql(_read)


def test_deployed_url_round_trip() -> None:
    config = _alembic_config()
    command.upgrade(config, "head")
    assert _COLUMN in _columns()

    try:
        command.downgrade(config, _PRE_URL_REVISION)
        assert _COLUMN not in _columns()
    finally:
        command.upgrade(config, "head")

    assert _COLUMN in _columns()


def test_an_existing_row_upgrades_to_a_null_url() -> None:
    """Seed a row at 0018 (before the column existed) and upgrade: it must survive with
    a NULL address. Every row in every environment is exactly this row — an app deployed
    before R5 has no recorded URL, which is precisely why the column is nullable."""
    config = _alembic_config()
    command.upgrade(config, "head")
    command.downgrade(config, _PRE_URL_REVISION)

    user_id, project_id, app_id = uuid.uuid4(), uuid.uuid4(), uuid.uuid4()

    async def _seed(conn) -> None:
        await conn.execute(
            text(
                "INSERT INTO users (id, azure_oid, email) "
                "VALUES (:id, :oid, 'url-migration-seed@rvaiglobal.com')"
            ),
            {"id": user_id, "oid": f"oid-{user_id}"},
        )
        await conn.execute(
            text("INSERT INTO projects (id, user_id, name) VALUES (:id, :user_id, 'seed')"),
            {"id": project_id, "user_id": user_id},
        )
        await conn.execute(
            text(
                "INSERT INTO app_registry (id, user_id, project_id, app_key, status) "
                "VALUES (:id, :user_id, :project_id, :app_key, CAST('approved' AS app_status))"
            ),
            {
                "id": app_id,
                "user_id": user_id,
                "project_id": project_id,
                "app_key": f"bial_seed_{uuid.uuid4().hex[:20]}",
            },
        )

    async def _read(conn) -> Any:
        return await conn.scalar(
            text("SELECT deployed_url FROM app_registry WHERE id = :id"), {"id": app_id}
        )

    async def _cleanup(conn) -> None:
        # users → projects → app_registry cascade removes every seeded row.
        await conn.execute(text("DELETE FROM users WHERE id = :id"), {"id": user_id})

    _run_sql(_seed)
    try:
        command.upgrade(config, "head")
        assert _run_sql(_read) is None
    finally:
        _run_sql(_cleanup)
        command.upgrade(config, "head")
