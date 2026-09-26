"""The manual go-live route's columns and enum, gone from `app_registry`.

The live-schema checks run in the default lane against the database `alembic upgrade head` built.
The round trips are marked `destructive_migration`: every add and drop on `app_registry` burns
pg_attribute slots on the shared test database for good, so they run only on request:
`uv run pytest -m destructive_migration`.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from alembic import command
from src.api.v1.deploy.schemas import PublishState, approved_retry_commit, compute_publish_state
from src.config import settings
from src.db.models.app_registry import AppRegistry

_BACKEND_ROOT = Path(__file__).resolve().parent.parent.parent
_REVISION = "0048_drop_manual_go_live"
_DROPPED = frozenset({"approval_route", "deployed_submission_id", "deployed_at", "deployed_url"})
_SHA = "5e" * 20

_COLUMNS_SQL = sa.text(
    "SELECT column_name, data_type, udt_name, is_nullable, character_maximum_length "
    "FROM information_schema.columns WHERE table_name = 'app_registry'"
)
_LABELS_SQL = sa.text(
    "SELECT e.enumlabel FROM pg_enum e JOIN pg_type t ON t.oid = e.enumtypid "
    "WHERE t.typname = 'approval_route' ORDER BY e.enumsortorder"
)


async def test_the_live_schema_has_none_of_the_manual_go_live_columns(db_session) -> None:
    columns = {row.column_name for row in (await db_session.execute(_COLUMNS_SQL)).all()}
    assert not (_DROPPED & columns)
    assert {"status", "approved_submission_id", "approved_commit_sha", "declaration"} <= columns


async def test_the_live_schema_has_no_approval_route_type(db_session) -> None:
    assert (await db_session.scalars(_LABELS_SQL)).all() == []


def test_the_model_declares_none_of_the_manual_go_live_columns() -> None:
    assert not (_DROPPED & set(AppRegistry.__table__.columns.keys()))


def _config() -> Config:
    return Config(str(_BACKEND_ROOT / "alembic.ini"))


def _pre_drop_revision(config: Config) -> str:
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


async def _columns(conn: AsyncConnection) -> dict[str, Any]:
    return {row.column_name: row for row in (await conn.execute(_COLUMNS_SQL)).all()}


async def _labels(conn: AsyncConnection) -> list[str]:
    return list((await conn.scalars(_LABELS_SQL)).all())


def _seed(user_id: uuid.UUID, app_id: uuid.UUID) -> Callable[[AsyncConnection], Awaitable[None]]:
    async def _insert(conn: AsyncConnection) -> None:
        project_id = uuid.uuid4()
        await conn.execute(
            sa.text(
                "INSERT INTO users (id, azure_oid, email) "
                "VALUES (:id, :oid, 'go-live-migration@rvaiglobal.com')"
            ),
            {"id": user_id, "oid": f"oid-{user_id}"},
        )
        await conn.execute(
            sa.text("INSERT INTO projects (id, user_id, name) VALUES (:id, :user_id, 'seed')"),
            {"id": project_id, "user_id": user_id},
        )
        await conn.execute(
            sa.text(
                "INSERT INTO app_registry (id, user_id, project_id, app_key, status, "
                "source_submission_id, source_commit_sha, approved_submission_id, "
                "approved_commit_sha, approved_at) "
                "VALUES (:id, :user_id, :project_id, :app_key, CAST('approved' AS app_status), "
                ":submission_id, :sha, :submission_id, :sha, :approved_at)"
            ),
            {
                "id": app_id,
                "user_id": user_id,
                "project_id": project_id,
                "app_key": f"bial_seed_{uuid.uuid4().hex[:20]}",
                "submission_id": uuid.uuid4(),
                "sha": _SHA,
                "approved_at": datetime.now(UTC) - timedelta(days=30),
            },
        )

    return _insert


def _cleanup(user_id: uuid.UUID) -> Callable[[AsyncConnection], Awaitable[None]]:
    async def _delete(conn: AsyncConnection) -> None:
        # users -> projects -> app_registry cascade removes every seeded row.
        await conn.execute(sa.text("DELETE FROM users WHERE id = :id"), {"id": user_id})

    return _delete


@pytest.mark.destructive_migration
def test_the_drop_round_trips_and_the_downgrade_restores_the_columns_empty() -> None:
    config = _config()
    command.upgrade(config, "head")
    user_id, app_id = uuid.uuid4(), uuid.uuid4()
    _run(_seed(user_id, app_id))
    at_head = _run(_columns)
    assert not (_DROPPED & set(at_head))
    assert _run(_labels) == []

    async def _dropped_values(conn: AsyncConnection) -> Any:
        return (
            await conn.execute(
                sa.text(
                    "SELECT approval_route, deployed_submission_id, deployed_at, deployed_url "
                    "FROM app_registry WHERE id = :id"
                ),
                {"id": app_id},
            )
        ).one()

    try:
        command.downgrade(config, _pre_drop_revision(config))
        restored = _run(_columns)
        assert set(restored) == set(at_head) | _DROPPED
        assert all(restored[name].is_nullable == "YES" for name in _DROPPED)
        assert restored["approval_route"].udt_name == "approval_route"
        assert restored["deployed_submission_id"].data_type == "uuid"
        assert restored["deployed_at"].data_type == "timestamp with time zone"
        assert restored["deployed_url"].character_maximum_length == 2083
        assert _run(_labels) == ["runbook", "self_publish"]
        assert tuple(_run(_dropped_values)) == (None, None, None, None)
    finally:
        _run(_cleanup(user_id))
        command.upgrade(config, "head")

    assert set(_run(_columns)) == set(at_head)
    assert _run(_labels) == []


@pytest.mark.destructive_migration
def test_an_app_marked_live_by_hand_offers_try_again_after_the_drop() -> None:
    config = _config()
    command.upgrade(config, "head")
    command.downgrade(config, _pre_drop_revision(config))
    user_id, app_id = uuid.uuid4(), uuid.uuid4()
    _run(_seed(user_id, app_id))

    async def _mark_live_by_hand(conn: AsyncConnection) -> None:
        await conn.execute(
            sa.text(
                "UPDATE app_registry SET approval_route = 'runbook', "
                "deployed_submission_id = approved_submission_id, deployed_at = now(), "
                "deployed_url = 'https://apps.bial.example.com/gate-ops' WHERE id = :id"
            ),
            {"id": app_id},
        )

    async def _load(conn: AsyncConnection) -> AppRegistry:
        async with AsyncSession(bind=conn) as session:
            app = await session.get(AppRegistry, app_id)
        assert app is not None
        return app

    try:
        _run(_mark_live_by_hand)
        command.upgrade(config, "head")
        app = _run(_load)
        assert compute_publish_state(app, None, None) is PublishState.DID_NOT_START
        assert approved_retry_commit(app, None, approved_went_live=False, copy_failures=0) == _SHA
    finally:
        _run(_cleanup(user_id))
        command.upgrade(config, "head")
