"""Alembic round-trip for the tombstone's description column (0037): head → 0036 →
head against the real test database.

WHY THIS EXISTS
Mirrors `test_app_registry_submissions_migration.py` — programmatic `alembic.command` off the
shared `alembic.ini`, database returned to head in a `finally` so a failed assertion cannot
poison the rest of the suite — and pins the two things that are decisions rather than defaults:

  * The column is NOT NULL with an EMPTY server default, not nullable. Every `deleted_projects`
    row in every environment predates it, and the description they described is on a `projects`
    row that is already gone — a NOT NULL with no default would fail the migration outright, and
    a nullable column would make "this project had no description" and "we did not record one"
    the same value to the administrator reading it.
  * An existing tombstone survives the upgrade carrying `''`. That is the accepted loss, asserted
    rather than assumed: rows written before this shipped cannot be backfilled and are accepted
    as lost, so this test is the record of what they end up holding.

DESTRUCTIVE LANE. Every run of this up/down round-trip permanently burns a `pg_attribute` slot
on the shared `citizen_one_test` database — a dropped column never frees its attnum, ~1600 per
table ever — so it is out of the default lane. Run it before shipping a migration:
`uv run pytest -m destructive_migration`. The walk is one revision wide, so the cost is one slot
on `deleted_projects` (at 12 of 1600 when this was written) and none on `app_registry`, which is
the table with the real budget problem.
"""

from __future__ import annotations

import asyncio
import importlib.util
import uuid
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine
from sqlalchemy.pool import NullPool

from alembic import command
from src.config import settings

pytestmark = pytest.mark.destructive_migration

_BACKEND_ROOT = Path(__file__).resolve().parent.parent.parent
_MIGRATION_PATH = (
    _BACKEND_ROOT / "alembic" / "versions" / "2026_09_07_0037_deleted_project_description.py"
)
_TABLE = "deleted_projects"
_COLUMN = "project_description"


def _alembic_config() -> Config:
    return Config(str(_BACKEND_ROOT / "alembic.ini"))


def _run_sql(work) -> Any:
    """Run one async callable against a fresh NullPool engine — sync wrapper (alembic's
    env.py owns the loop during the commands)."""

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
            text("SELECT column_name FROM information_schema.columns WHERE table_name = :table"),
            {"table": _TABLE},
        )
        return {row[0] for row in rows}

    return _run_sql(_read)


def _shape() -> Any:
    async def _read(conn) -> Any:
        return (
            await conn.execute(
                text(
                    "SELECT data_type, is_nullable, column_default "
                    "FROM information_schema.columns "
                    "WHERE table_name = :table AND column_name = :column"
                ),
                {"table": _TABLE, "column": _COLUMN},
            )
        ).one()

    return _run_sql(_read)


def _toggle_0037(direction: str) -> None:
    """Run 0037's OWN `upgrade()` / `downgrade()` and nothing else.

    ★ WHY NOT `command.downgrade(config, "0036_deleted_projects")`. That is what this used to
    do, and it was correct until `0038_app_previous_status` chained onto 0037. Alembic steps
    down the whole linear chain, so the walk now runs 0038's downgrade too — dropping and
    re-adding `app_registry.previous_status` on every run of this test.

    That silently defeats 0038's own stated decision. Its docstring skips a round trip
    precisely because `app_registry` is the table already close to its ~1600-attnum budget,
    and a dropped column never frees its attnum. This test was burning two slots a run on the
    one table the project cannot afford to burn.

    So the DDL is driven directly against the connection through alembic's own `Operations`,
    which is the same shape 0038's test uses to run its backfill statement: the migration's
    real code, none of its neighbours'.
    """
    module = _migration_0037()

    async def _go() -> None:
        engine = create_async_engine(settings.DATABASE_URL.get_secret_value(), poolclass=NullPool)
        try:
            async with engine.begin() as conn:
                await conn.run_sync(lambda sync_conn: _run_op(sync_conn, module, direction))
        finally:
            await engine.dispose()

    asyncio.run(_go())


def _run_op(sync_conn: Any, module: ModuleType, direction: str) -> None:
    ctx = MigrationContext.configure(sync_conn)
    with Operations.context(ctx):
        (module.upgrade if direction == "up" else module.downgrade)()


def _migration_0037() -> ModuleType:
    """Import 0037 BY PATH (the versions dir is not a package), so the round trip exercises the
    migration's real DDL rather than a copy of it that could drift."""
    spec = importlib.util.spec_from_file_location("migration_0037", _MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_description_column_round_trips() -> None:
    command.upgrade(_alembic_config(), "head")
    assert _COLUMN in _columns()

    try:
        _toggle_0037("down")
        assert _COLUMN not in _columns()
    finally:
        # IDEMPOTENT, because this `finally` runs on the failure path too. A bare
        # `_toggle_0037("up")` on a body that failed BEFORE the downgrade landed re-adds a
        # column that is already there — and `DuplicateColumn` from the cleanup replaces the
        # assertion error that actually explains what went wrong. Worse, a run that dies
        # between the two leaves the schema and `alembic_version` disagreeing, and nothing in
        # the suite repairs that: every later test on this database then fails for a reason
        # that has nothing to do with it.
        if _COLUMN not in _columns():
            _toggle_0037("up")

    assert _COLUMN in _columns()


def test_the_column_is_not_null_with_an_empty_default() -> None:
    """The SHAPE, not just the presence.

    A downgrade that drifted, or an upgrade re-authored as `nullable=True`, would still pass
    the round-trip above. The three properties asserted here are each load-bearing: `text` so a
    long description is not truncated into the one field that cannot be recovered, `NO` so an
    unset value reads as empty rather than unknown, and a `''` default so the ALTER can land on
    a table that already has rows.
    """
    command.upgrade(_alembic_config(), "head")
    row = _shape()

    assert row.data_type == "text"
    assert row.is_nullable == "NO"
    assert row.column_default is not None, "no default — the ALTER cannot land on existing rows"
    assert "''" in row.column_default


def test_a_tombstone_written_before_this_shipped_upgrades_to_an_empty_description() -> None:
    """The accepted loss, asserted rather than assumed: the already-deleted cannot be backfilled.

    Seed a tombstone at 0036 — the shape every existing row is in — and upgrade. It must
    survive, because the project it describes is long gone and there is nothing to reconstruct
    a description from. `''` is what it gets, and an administrator reading it cannot tell it
    apart from a project that genuinely had no description. That is the accepted loss.
    """
    command.upgrade(_alembic_config(), "head")
    _toggle_0037("down")

    project_id, owner_id = uuid.uuid4(), uuid.uuid4()

    async def _seed(conn) -> None:
        await conn.execute(
            text(
                "INSERT INTO deleted_projects "
                "(project_id, project_name, owner_id, owner_email, deleted_by, "
                " deleted_by_name, remark) "
                "VALUES (:project_id, 'Visitor Log', :owner_id, "
                " 'seed@rvaiglobal.com', :owner_id, 'Seed Citizen', "
                " 'Recorded before the description column existed')"
            ),
            {"project_id": project_id, "owner_id": owner_id},
        )

    async def _read(conn) -> Any:
        return await conn.scalar(
            text("SELECT project_description FROM deleted_projects WHERE project_id = :id"),
            {"id": project_id},
        )

    async def _cleanup(conn) -> None:
        # No FK reaches this table by design (the project row is gone), so it is deleted
        # directly rather than through a cascade.
        await conn.execute(
            text("DELETE FROM deleted_projects WHERE project_id = :id"), {"id": project_id}
        )

    _run_sql(_seed)
    try:
        # The upgrade is the thing under test, so it runs inside the try — and it is what puts
        # the column back, which is why the `finally` below must not run it a second time.
        _toggle_0037("up")
        assert _run_sql(_read) == ""
    finally:
        # Restore the database whichever way the assertion went: put the column back only if
        # the upgrade above did not get that far, then drop the seeded tombstone (which needs
        # the column present to be addressable by the ordinary delete).
        if _COLUMN not in _columns():
            _toggle_0037("up")
        _run_sql(_cleanup)
