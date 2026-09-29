"""`users.has_signed_in` against the migrated schema.

The live-schema checks run in the default lane against the database `alembic upgrade head` built.
The round trip is marked `destructive_migration`: every add and drop on `users` burns a
pg_attribute slot on the shared test database for good, so it runs only on request:
`uv run pytest -m destructive_migration`.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.ext.asyncio import AsyncConnection, create_async_engine
from sqlalchemy.pool import NullPool

from alembic import command
from src.config import settings
from src.db.models.user import User
from tests.factories import UserFactory

_BACKEND_ROOT = Path(__file__).resolve().parent.parent.parent
_REVISION = "0050_user_has_signed_in"

_COLUMN_SQL = sa.text(
    "SELECT data_type, is_nullable, column_default FROM information_schema.columns "
    "WHERE table_name = 'users' AND column_name = 'has_signed_in'"
)


async def test_the_column_is_a_non_null_boolean_defaulting_to_true(db_session) -> None:
    row = (await db_session.execute(_COLUMN_SQL)).one()
    assert (row.data_type, row.is_nullable, row.column_default) == ("boolean", "NO", "true")


async def test_an_insert_that_names_no_marker_reads_signed_in(db_session) -> None:
    """Sign-in's insert never names the column, so the default is what marks a new user."""
    user_id = await db_session.scalar(
        sa.text(
            "INSERT INTO users (id, azure_oid, email) "
            "VALUES (:id, 'oid-marker-default', 'marker-default@rvaiglobal.com') RETURNING id"
        ),
        {"id": uuid.uuid4()},
    )
    marker = await db_session.scalar(sa.select(User.has_signed_in).where(User.id == user_id))
    assert marker is True


async def test_a_never_signed_in_user_keeps_false(db_session) -> None:
    user = await UserFactory.create(db_session, has_signed_in=False)
    marker = await db_session.scalar(sa.select(User.has_signed_in).where(User.id == user.id))
    assert marker is False


def _config() -> Config:
    return Config(str(_BACKEND_ROOT / "alembic.ini"))


def _pre_marker_revision(config: Config) -> str:
    previous = ScriptDirectory.from_config(config).get_revision(_REVISION).down_revision
    assert isinstance(previous, str)
    return previous


def _run[T](work: Callable[[AsyncConnection], Awaitable[T]]) -> T:
    """One unit of SQL on a fresh NullPool engine: alembic's env.py owns the event loop while
    the commands run, so nothing here may outlive its own `asyncio.run`."""

    async def _go() -> T:
        engine = create_async_engine(settings.DATABASE_URL.get_secret_value(), poolclass=NullPool)
        try:
            async with engine.begin() as conn:
                return await work(conn)
        finally:
            await engine.dispose()

    return asyncio.run(_go())


async def _has_column(conn: AsyncConnection) -> bool:
    return (await conn.execute(_COLUMN_SQL)).first() is not None


@pytest.mark.destructive_migration
def test_existing_users_read_signed_in_after_the_upgrade_and_the_downgrade_drops_the_column() -> (
    None
):
    config = _config()
    command.upgrade(config, "head")
    command.downgrade(config, _pre_marker_revision(config))
    user_id = uuid.uuid4()

    async def _seed(conn: AsyncConnection) -> None:
        await conn.execute(
            sa.text(
                "INSERT INTO users (id, azure_oid, email) "
                "VALUES (:id, :oid, 'marker-migration@rvaiglobal.com')"
            ),
            {"id": user_id, "oid": f"oid-{user_id}"},
        )

    async def _seeded_marker(conn: AsyncConnection) -> bool | None:
        marker: bool | None = await conn.scalar(
            sa.text("SELECT has_signed_in FROM users WHERE id = :id"), {"id": user_id}
        )
        return marker

    async def _rows_not_signed_in(conn: AsyncConnection) -> int:
        count: int = await conn.scalar(
            sa.text("SELECT count(*) FROM users WHERE has_signed_in IS NOT TRUE")
        )
        return count

    async def _cleanup(conn: AsyncConnection) -> None:
        await conn.execute(sa.text("DELETE FROM users WHERE id = :id"), {"id": user_id})

    try:
        assert _run(_has_column) is False
        _run(_seed)

        command.upgrade(config, _REVISION)
        assert _run(_seeded_marker) is True
        assert _run(_rows_not_signed_in) == 0

        command.downgrade(config, _pre_marker_revision(config))
        assert _run(_has_column) is False
    finally:
        _run(_cleanup)
        command.upgrade(config, "head")

    assert _run(_has_column) is True
