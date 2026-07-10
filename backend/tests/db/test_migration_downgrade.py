"""Alembic downgrade round-trip: head → pre-projects (0014) → head, in-suite (U3).

`tests/db/test_projects_migration.py` proves the shape 0015 PRODUCED; this proves the
chain actually walks back and forward again against the real test DB: downgrading below
0015/0016 removes everything they added (projects, the project_id wiring, current_code,
users.suspended_at) and restores `uq_app_registry_owner_conversation`; re-upgrading
restores head. Uses the programmatic `alembic.command` API off the same `alembic.ini`
config `tests/test_alembic_single_head.py` reads. The DB is returned to head in a
`finally` so a failed assertion can't poison the rest of the suite.

NOTE: the 0015 downgrade is a schema-shape rollback that is safe only pre-divergence
(see its docstring); the suite's per-test transactions roll back, so the tables are
empty here and the constraint recreation cannot collide.
"""

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
_PRE_PROJECTS_REVISION = "0014_merge_plan_ab_heads"

_COLUMNS_SQL = "SELECT column_name FROM information_schema.columns WHERE table_name = :table"
_CONSTRAINTS_SQL = "SELECT conname FROM pg_constraint WHERE conrelid = 'app_registry'::regclass"


def _alembic_config() -> Config:
    return Config(str(_BACKEND_ROOT / "alembic.ini"))


def _snapshot() -> dict[str, Any]:
    """Introspect the schema bits 0015/0016 own (sync wrapper — alembic's env.py owns
    the loop during the commands, so this runs between them on a fresh engine)."""

    async def _read() -> dict[str, Any]:
        engine = create_async_engine(settings.DATABASE_URL.get_secret_value(), poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                projects_table = await conn.scalar(text("SELECT to_regclass('projects')"))
                app_registry_columns = set(
                    (await conn.execute(text(_COLUMNS_SQL), {"table": "app_registry"})).scalars()
                )
                users_columns = set(
                    (await conn.execute(text(_COLUMNS_SQL), {"table": "users"})).scalars()
                )
                app_registry_constraints = set(
                    (await conn.execute(text(_CONSTRAINTS_SQL))).scalars()
                )
        finally:
            await engine.dispose()
        return {
            "projects_table": projects_table,
            "app_registry_columns": app_registry_columns,
            "users_columns": users_columns,
            "app_registry_constraints": app_registry_constraints,
        }

    return asyncio.run(_read())


def test_downgrade_to_pre_projects_and_back() -> None:
    config = _alembic_config()
    command.upgrade(config, "head")  # normalize the start state (a no-op when at head)
    try:
        command.downgrade(config, _PRE_PROJECTS_REVISION)
        state = _snapshot()
        # Everything 0015 added is gone…
        assert state["projects_table"] is None
        assert "project_id" not in state["app_registry_columns"]
        assert "current_code" not in state["app_registry_columns"]
        assert "uq_app_registry_project" not in state["app_registry_constraints"]
        # …the pre-projects uniqueness is back…
        assert "uq_app_registry_owner_conversation" in state["app_registry_constraints"]
        # …and 0016's suspension marker is gone too.
        assert "suspended_at" not in state["users_columns"]
    finally:
        # ALWAYS restore head — even on assertion failure — so the rest of the suite
        # runs against the schema it expects.
        command.upgrade(config, "head")

    state = _snapshot()
    assert state["projects_table"] is not None
    assert {"project_id", "current_code"} <= state["app_registry_columns"]
    assert "uq_app_registry_project" in state["app_registry_constraints"]
    assert "uq_app_registry_owner_conversation" not in state["app_registry_constraints"]
    assert "suspended_at" in state["users_columns"]
