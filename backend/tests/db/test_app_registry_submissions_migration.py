"""Alembic round-trip for the app_registry submissions re-shape (0018, APPROVAL):
head → 0017 → head against the real test DB. Proves `upgrade` drops both JSX-era
JSONB snapshot columns and adds the seven typed submission columns, that the legacy
status reset targets EXACTLY `pending`/`approved` (sparing `disabled`/`rejected`,
whose reactivation would silently re-open a refused app's data plane — D13), and
that `downgrade` recreates structure, never data. Mirrors
`test_app_files_drop_migration.py`: programmatic `alembic.command` off the shared
`alembic.ini`, the DB returned to head in a `finally` so a failed assertion can't
poison the rest of the suite."""

from __future__ import annotations

import asyncio
import uuid
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
_PRE_RESHAPE_REVISION = "0017_drop_app_files"
_ENUM_SQL = "SELECT 1 FROM pg_type WHERE typname = 'app_status'"

_NEW_COLUMNS = frozenset(
    {
        "source_submission_id",
        "source_commit_sha",
        "submitted_at",
        "approved_submission_id",
        "approved_commit_sha",
        "deployed_submission_id",
        "deployed_at",
    }
)
_DROPPED_COLUMNS = frozenset({"source_snapshot", "approved_snapshot"})


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
    """Introspect app_registry's column set + whether the app_status enum exists."""

    async def _read(conn) -> dict[str, Any]:
        rows = await conn.execute(
            text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'app_registry'"
            )
        )
        columns = {row[0] for row in rows}
        enum_present = await conn.scalar(text(_ENUM_SQL))
        return {"columns": columns, "enum_present": enum_present}

    return _run_sql(_read)


def test_app_registry_submissions_round_trip() -> None:
    config = _alembic_config()
    command.upgrade(config, "head")  # normalize: at head the re-shape is applied

    at_head = _snapshot()
    assert _NEW_COLUMNS <= at_head["columns"]
    assert not (_DROPPED_COLUMNS & at_head["columns"])
    # The enum is not this migration's to touch (D7) — it survives the upgrade.
    assert at_head["enum_present"] == 1

    try:
        command.downgrade(config, _PRE_RESHAPE_REVISION)
        restored = _snapshot()
        # Structure only: the JSONB columns reappear (empty), the typed ones go.
        assert _DROPPED_COLUMNS <= restored["columns"]
        assert not (_NEW_COLUMNS & restored["columns"])
        assert restored["enum_present"] == 1
    finally:
        # ALWAYS return to head so the rest of the suite sees the re-shaped schema.
        command.upgrade(config, "head")

    final = _snapshot()
    assert _NEW_COLUMNS <= final["columns"]
    assert not (_DROPPED_COLUMNS & final["columns"])


def test_legacy_status_reset_is_status_scoped() -> None:
    """Seed one row per status at 0017 (pre-re-shape), upgrade, and assert the reset
    targets EXACTLY pending/approved: both land at draft with NULL refs, draft stays
    draft, and disabled/rejected keep their refused-at-the-data-plane statuses."""
    config = _alembic_config()
    command.upgrade(config, "head")
    command.downgrade(config, _PRE_RESHAPE_REVISION)

    user_id = uuid.uuid4()
    statuses = ("draft", "pending", "approved", "disabled", "rejected")
    app_ids = {status: uuid.uuid4() for status in statuses}

    async def _seed(conn) -> None:
        await conn.execute(
            text(
                "INSERT INTO users (id, azure_oid, email) "
                "VALUES (:id, :oid, 'migration-seed@rvaiglobal.com')"
            ),
            {"id": user_id, "oid": f"oid-{user_id}"},
        )
        for status, app_id in app_ids.items():
            project_id = uuid.uuid4()
            await conn.execute(
                text(
                    "INSERT INTO projects (id, user_id, name) "
                    "VALUES (:id, :user_id, 'migration seed')"
                ),
                {"id": project_id, "user_id": user_id},
            )
            # A JSX-shaped approved_snapshot on the approved row proves the data
            # column really is discarded, not migrated.
            snapshot = (
                '{"compiled": "var x=1", "src": "<App/>", "entry": "PreviewApp"}'
                if status == "approved"
                else None
            )
            await conn.execute(
                text(
                    "INSERT INTO app_registry "
                    "(id, user_id, project_id, app_key, status, approved_snapshot) "
                    "VALUES (:id, :user_id, :project_id, :app_key, "
                    "CAST(:status AS app_status), CAST(:snapshot AS jsonb))"
                ),
                {
                    "id": app_id,
                    "user_id": user_id,
                    "project_id": project_id,
                    "app_key": f"bial_seed_{uuid.uuid4().hex[:20]}",
                    "status": status,
                    "snapshot": snapshot,
                },
            )

    async def _read_rows(conn) -> dict[uuid.UUID, dict[str, Any]]:
        rows = await conn.execute(
            text(
                "SELECT id, status, source_submission_id, approved_submission_id, "
                "deployed_submission_id FROM app_registry WHERE user_id = :user_id"
            ),
            {"user_id": user_id},
        )
        return {
            row.id: {
                "status": row.status,
                "refs": (
                    row.source_submission_id,
                    row.approved_submission_id,
                    row.deployed_submission_id,
                ),
            }
            for row in rows
        }

    async def _cleanup(conn) -> None:
        # users → projects → app_registry cascade removes every seeded row.
        await conn.execute(text("DELETE FROM users WHERE id = :id"), {"id": user_id})

    _run_sql(_seed)
    try:
        command.upgrade(config, "head")
        rows = _run_sql(_read_rows)

        # pending + approved → draft (the JSX-era in-flight rows are reset)…
        assert rows[app_ids["pending"]]["status"] == "draft"
        assert rows[app_ids["approved"]]["status"] == "draft"
        # …with every new ref column NULL (nothing to backfill from).
        assert rows[app_ids["pending"]]["refs"] == (None, None, None)
        assert rows[app_ids["approved"]]["refs"] == (None, None, None)
        # draft is untouched.
        assert rows[app_ids["draft"]]["status"] == "draft"
        # disabled/rejected are SPARED: resetting them to the active `draft` would
        # silently re-open a kill-switched/rejected app through its unchanged key.
        assert rows[app_ids["disabled"]]["status"] == "disabled"
        assert rows[app_ids["rejected"]]["status"] == "rejected"
    finally:
        _run_sql(_cleanup)
        command.upgrade(config, "head")
