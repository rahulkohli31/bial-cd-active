"""Alembic round-trip for the deployments table (0025): head -> 0024 -> head against the
real test DB.

Two things a plain `create_table` migration can get wrong, both pinned here: dropping
`postgresql_where` still applies cleanly but leaves every app undeployable after its
first deploy, and leaving the `create_type=False` enum behind on downgrade fails the
next upgrade on a duplicate type — on someone else's machine, not this one.

Mirrors `test_app_registry_submissions_migration.py`; the DB returns to head in a
`finally` so a failed assertion cannot poison the rest of the suite.
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

# Creating and dropping a table permanently burns pg_attribute slots on the shared
# citizen_one_test DB, so this is OUT of the default lane:
#   `uv run pytest -m destructive_migration`
pytestmark = pytest.mark.destructive_migration

_BACKEND_ROOT = Path(__file__).resolve().parent.parent.parent
_PRE_REVISION = "0024_messages_native_reset"
_INDEX = "uq_deployments_one_in_flight"


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


def _snapshot() -> dict[str, Any]:
    async def _read(conn) -> dict[str, Any]:
        rows = await conn.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'deployments'"
            )
        )
        index_def = await conn.scalar(
            text("SELECT indexdef FROM pg_indexes WHERE indexname = :name"),
            {"name": _INDEX},
        )
        enum_present = await conn.scalar(
            text("SELECT 1 FROM pg_type WHERE typname = 'deployment_status'")
        )
        return {
            "columns": {row[0] for row in rows},
            "index_def": index_def,
            "enum_present": enum_present,
        }

    return _run_sql(_read)


# THE WHOLE `deployments` TABLE AT HEAD, not just what revision 0025 created — the later
# columns marked below belong in this frozen set too, so it stays exhaustive as the table grows.
_EXPECTED_COLUMNS = frozenset(
    {
        "id",
        "user_id",
        "app_id",
        "status",
        "step",
        "head_sha",
        "image_digest",
        "acr_run_id",
        "container_app_name",
        "revision_name",
        "url",
        "failure_code",
        "failure_detail",
        "heartbeat_at",
        "finished_at",
        "created_at",
        "updated_at",
        "unpublished_at",  # 0028 — the marketplace unpublish stamp
    }
)


def test_deployments_round_trip() -> None:
    config = _alembic_config()
    command.upgrade(config, "head")

    at_head = _snapshot()
    assert at_head["columns"] == _EXPECTED_COLUMNS
    assert at_head["enum_present"] == 1
    assert at_head["index_def"] is not None
    assert "UNIQUE" in at_head["index_def"]
    assert "WHERE (status = 'running'" in at_head["index_def"]

    try:
        command.downgrade(config, _PRE_REVISION)
        gone = _snapshot()
        assert gone["columns"] == set()
        assert gone["index_def"] is None
        assert gone["enum_present"] is None
    finally:
        command.upgrade(config, "head")

    restored = _snapshot()
    assert restored["columns"] == _EXPECTED_COLUMNS
    assert restored["enum_present"] == 1
    assert restored["index_def"] is not None
    assert "WHERE (status = 'running'" in restored["index_def"]
