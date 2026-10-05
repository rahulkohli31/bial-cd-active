"""Revision 0053: an owed teardown says whether it owes a build sandbox or a shared view.

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
_MIGRATION_PATH = (
    _BACKEND_ROOT / "alembic" / "versions" / "2026_10_05_0053_pending_teardown_kind.py"
)
_BUILD_NAME = "sbx-" + "1" * 28
_SHARED_NAME = "shr-" + "2" * 28
_UNRELATED_NAME = "sbx-6d0f3a9e21c84b7f95e0a1c2d3b4"


def _migration() -> ModuleType:
    """Imported by path (the versions directory is not a package), so the test runs the
    revision's own code rather than a copy of it."""
    spec = importlib.util.spec_from_file_location("migration_0053", _MIGRATION_PATH)
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
    """A row in whichever shape the table has right now: no column is named that either side
    of the revision lacks."""
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


async def _kinds(db_session, user_id: uuid.UUID) -> dict[str, str | None]:
    rows = await db_session.execute(
        sa.text("SELECT app_name, kind::text FROM pending_teardowns WHERE user_id = :user_id"),
        {"user_id": user_id},
    )
    return {name: kind for name, kind in rows.all()}


def test_the_revision_id_fits_the_version_column() -> None:
    """Against a literal, never the string that produced it — that passes at any length."""
    assert _REVISION.revision == "0053_pending_teardown_kind"
    assert len("0053_pending_teardown_kind") <= 32
    assert _REVISION.down_revision == "0052_sandbox_starts"


async def test_the_column_is_a_nullable_two_label_enum(db_session) -> None:
    """Nullable on purpose: a process older than the column can still write a row."""
    nullable = await db_session.scalar(
        sa.text(
            "SELECT is_nullable FROM information_schema.columns "
            "WHERE table_name = 'pending_teardowns' AND column_name = 'kind'"
        )
    )
    labels = (
        await db_session.execute(
            sa.text(
                "SELECT enumlabel FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid "
                "WHERE t.typname = 'pending_teardown_kind' ORDER BY e.enumsortorder"
            )
        )
    ).scalars()
    assert nullable == "YES"
    assert list(labels) == ["build", "shared"]


async def test_the_upgrade_fills_the_kind_from_the_name_and_the_round_trip_keeps_the_rows(
    db_session,
) -> None:
    """★ Rows written before the revision and rows written while it is rolled back both come
    through the round trip, each owed as the kind its name said.

    Mutation check: backfill every row as `build` and the `shr-` row reads as a build sandbox,
    which the routine would write back over its owner's saved copy."""
    user = await UserFactory.create(db_session)
    await _owe(db_session, user.id, _BUILD_NAME)
    await _owe(db_session, user.id, _SHARED_NAME)

    await _run(db_session, "downgrade")
    columns = (
        await db_session.execute(
            sa.text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name = 'pending_teardowns'"
            )
        )
    ).scalars()
    assert "kind" not in set(columns)
    await _owe(db_session, user.id, _UNRELATED_NAME)
    await _run(db_session, "upgrade")

    assert await _kinds(db_session, user.id) == {
        _BUILD_NAME: "build",
        _SHARED_NAME: "shared",
        _UNRELATED_NAME: "build",
    }
