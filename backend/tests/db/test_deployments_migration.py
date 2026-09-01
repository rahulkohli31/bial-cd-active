"""Alembic round-trip for the deployments table (0025): head → 0024 → head against the
real test DB.

Two things a plain `create_table` migration can still get wrong, both pinned here:

* **The partial index.** `postgresql_where` is what makes `uq_deployments_one_in_flight`
  partial. Drop that clause and the migration still applies cleanly — and every app becomes
  undeployable after its first deploy. So the test reads the index DEFINITION back out of
  `pg_indexes` and asserts the predicate is there, rather than merely asserting the index
  exists.
* **The enum lifecycle.** `create_type=False` on the model means THIS migration owns
  `CREATE`/`DROP TYPE`. A downgrade that drops the table but leaves the type behind makes
  the next upgrade fail on a duplicate type, which only ever shows up on someone else's
  machine.

Mirrors `test_app_registry_submissions_migration.py`: programmatic `alembic.command` off
the shared `alembic.ini`, the DB returned to head in a `finally` so a failed assertion
cannot poison the rest of the suite.
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
    """Columns, the partial-index definition, and whether the enum type exists."""

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


# THE WHOLE `deployments` TABLE AT HEAD, not just what revision 0025 created.
#
# The last three arrived AFTER this file was written and were never added to it, so this
# assertion has been red since revision 0026 — invisibly, because it only runs in the opt-in
# `destructive_migration` lane that nobody runs on a routine change. Corrected here rather than
# left standing: this lane is the pre-ship check for a migration, and a lane with a permanent
# red in it is a lane whose green means nothing. (Unrelated to the chat-kind work; noted so the
# diff is not mistaken for it.)
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
        "classification",  # 0026 — the data-classification gate
        "classification_score",  # 0029 — the review's score
        "unpublished_at",  # 0028 — the marketplace unpublish stamp
    }
)


def test_deployments_round_trip() -> None:
    config = _alembic_config()
    command.upgrade(config, "head")

    at_head = _snapshot()
    assert at_head["columns"] == _EXPECTED_COLUMNS
    assert at_head["enum_present"] == 1
    # THE assertion this file exists for: the index must be PARTIAL. An index without the
    # predicate applies cleanly and then forbids every redeploy.
    assert at_head["index_def"] is not None
    assert "UNIQUE" in at_head["index_def"]
    assert "WHERE (status = 'running'" in at_head["index_def"]

    try:
        command.downgrade(config, _PRE_REVISION)
        gone = _snapshot()
        assert gone["columns"] == set()
        assert gone["index_def"] is None
        # The type goes WITH the table. Leaving it behind makes the next upgrade fail on a
        # duplicate type — on someone else's machine, not this one.
        assert gone["enum_present"] is None
    finally:
        # ALWAYS return to head so the rest of the suite sees the table.
        command.upgrade(config, "head")

    restored = _snapshot()
    assert restored["columns"] == _EXPECTED_COLUMNS
    assert restored["enum_present"] == 1
    assert restored["index_def"] is not None
    assert "WHERE (status = 'running'" in restored["index_def"]
