"""`sandbox_pool.project_type` against the real migrated schema, and its round trip.

The round trip walks the chain for real and sits in the destructive lane
(`-m destructive_migration`), since every up/down round trip burns `pg_attribute` slots.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from alembic.config import Config
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from alembic import command
from src.config import settings
from src.services.sandbox.base import a_fresh_sandbox_name

_BACKEND_ROOT = Path(__file__).resolve().parent.parent.parent

_COLUMN_SQL = (
    "SELECT data_type, udt_name, is_nullable, column_default FROM information_schema.columns "
    "WHERE table_name = :table AND column_name = 'project_type'"
)


async def test_the_type_is_the_shared_enum_not_null_and_plain_by_default(db_session) -> None:
    """Plain by default, because an older release writes rows without the column."""
    row = (await db_session.execute(sa.text(_COLUMN_SQL), {"table": "sandbox_pool"})).one()

    assert (row.data_type, row.udt_name, row.is_nullable) == (
        "USER-DEFINED",
        "sandbox_project_type",
        "NO",
    )
    assert row.column_default == "'plain'::sandbox_project_type"


def _sql(statements: list[tuple[str, dict[str, Any]]]) -> list[Any]:
    """Raw SQL between alembic commands, on an engine of its own that commits its own work."""

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


@pytest.mark.destructive_migration
def test_a_row_from_before_reads_plain_and_the_downgrade_drops_only_the_column() -> None:
    """The start records keep their own `project_type` through the downgrade: the type is theirs,
    and this revision only borrows it."""
    config = Config(str(_BACKEND_ROOT / "alembic.ini"))
    name = a_fresh_sandbox_name()
    row_sql = "SELECT state::text AS state FROM sandbox_pool WHERE name = :name"
    try:
        command.upgrade(config, "head")
        command.downgrade(config, "0056_start_miss_connector")
        _sql(
            [
                (
                    "INSERT INTO sandbox_pool (name, fqdn, image_ref, state, state_changed_at) "
                    "VALUES (:name, 'x.example', 'acr/img:v1', 'ready', now())",
                    {"name": name},
                )
            ]
        )

        command.upgrade(config, "head")
        [typed] = _sql(
            [
                (
                    "SELECT project_type::text AS project_type, state::text AS state "
                    "FROM sandbox_pool WHERE name = :name",
                    {"name": name},
                )
            ]
        )
        assert [(r.project_type, r.state) for r in typed] == [("plain", "ready")]

        command.downgrade(config, "0056_start_miss_connector")
        pool_column, starts_column, kept = _sql(
            [
                (_COLUMN_SQL, {"table": "sandbox_pool"}),
                (_COLUMN_SQL, {"table": "sandbox_starts"}),
                (row_sql, {"name": name}),
            ]
        )
        assert pool_column == []
        assert [r.udt_name for r in starts_column] == ["sandbox_project_type"]
        assert [r.state for r in kept] == ["ready"]

        command.upgrade(config, "head")
        [again] = _sql([(row_sql, {"name": name})])
        assert [r.state for r in again] == ["ready"]
    finally:
        command.upgrade(config, "head")
        _sql([("DELETE FROM sandbox_pool WHERE name = :name", {"name": name})])
