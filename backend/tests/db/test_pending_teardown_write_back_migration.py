"""Revision 0054: an owed teardown says whether its container's tree is written back first.

DEFAULT LANE: the revision's real `downgrade()` and `upgrade()` run inside the per-test transaction
against seeded rows and are rolled back with it, so the round trip is proved with rows present
and costs the shared database nothing.
"""

from __future__ import annotations

import importlib.util
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import ModuleType

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from tests.factories import UserFactory

_BACKEND_ROOT = Path(__file__).resolve().parent.parent.parent
_MIGRATION_PATH = _BACKEND_ROOT / "alembic" / "versions" / "2026_10_05_0054_teardown_write_back.py"
_BEFORE = "sbx-" + "1" * 28
_DURING_THE_ROLLBACK = "sbx-6d0f3a9e21c84b7f95e0a1c2d3b4"
_A_SHARED_VIEW = "shr-" + "3" * 28


def _migration() -> ModuleType:
    """Imported by path (the versions directory is not a package), so the test runs the
    revision's own code rather than a copy of it."""
    spec = importlib.util.spec_from_file_location("migration_0054", _MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_REVISION = _migration()


async def _run(db_session, step: str) -> None:
    connection = await db_session.connection()

    def _go(sync_connection) -> None:
        with Operations.context(MigrationContext.configure(sync_connection)):
            getattr(_REVISION, step)()

    await connection.run_sync(_go)


async def _owe(db_session, user_id: uuid.UUID, app_name: str) -> None:
    """A row in the shape a process older than the column writes: `write_back` is not named."""
    await db_session.execute(
        sa.text(
            "INSERT INTO pending_teardowns "
            "(user_id, app_id, app_name, project_id, instance_ref, claimed_until) "
            "VALUES (:user_id, :app_id, :app_name, :project_id, :instance_ref, :claimed_until)"
        ),
        {
            "user_id": user_id,
            "app_id": uuid.uuid4(),
            "app_name": app_name,
            "project_id": uuid.uuid4(),
            "instance_ref": datetime.now(UTC),
            "claimed_until": datetime.now(UTC) + timedelta(minutes=5),
        },
    )


def test_the_revision_id_fits_the_version_column() -> None:
    """Against a literal, never the string that produced it — that passes at any length."""
    assert _REVISION.revision == "0054_teardown_write_back"
    assert len("0054_teardown_write_back") <= 32
    assert _REVISION.down_revision == "0052_sandbox_starts"


async def test_a_row_that_does_not_name_the_column_keeps_its_write_back(db_session) -> None:
    """★ Every row from before the revision, and every row an older process writes after it, is
    written back as it always was.

    Mutation check: default the column to false and both rows lose the write-back a switch owes
    them, so the outgoing project's unsaved work is destroyed unread."""
    user = await UserFactory.create(db_session)
    await _owe(db_session, user.id, _BEFORE)

    await _run(db_session, "downgrade")
    columns = (
        await db_session.execute(
            sa.text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'pending_teardowns'"
            )
        )
    ).scalars()
    assert "write_back" not in set(columns)
    await _owe(db_session, user.id, _DURING_THE_ROLLBACK)
    await _run(db_session, "upgrade")
    await _owe(db_session, user.id, "sbx-" + "2" * 28)

    rows = await db_session.execute(
        sa.text("SELECT app_name, write_back FROM pending_teardowns WHERE user_id = :user_id"),
        {"user_id": user.id},
    )
    assert dict(rows.all()) == {
        _BEFORE: True,
        _DURING_THE_ROLLBACK: True,
        "sbx-" + "2" * 28: True,
    }
    nullable = await db_session.scalar(
        sa.text(
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_name = 'pending_teardowns' AND column_name = 'write_back'"
        )
    )
    assert nullable == "NO"


async def test_the_upgrade_marks_a_shared_views_row_as_never_written_back(db_session) -> None:
    """A `shr-` row from before the revision owes a shared view, which is never written back; a
    build sandbox's row beside it keeps its write-back.

    Mutation check: drop the upgrade's UPDATE and the view's row is left claiming a write-back."""
    user = await UserFactory.create(db_session)
    await _owe(db_session, user.id, _BEFORE)
    await _owe(db_session, user.id, _A_SHARED_VIEW)

    await _run(db_session, "downgrade")
    await _run(db_session, "upgrade")

    rows = await db_session.execute(
        sa.text("SELECT app_name, write_back FROM pending_teardowns WHERE user_id = :user_id"),
        {"user_id": user.id},
    )
    assert dict(rows.all()) == {_BEFORE: True, _A_SHARED_VIEW: False}
