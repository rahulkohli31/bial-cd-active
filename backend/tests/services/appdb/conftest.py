"""Fixtures for the per-project-database suite.

These tests create and destroy REAL databases and roles on the shared test cluster, on a
separate AUTOCOMMIT engine. The `db_session` fixture's outer-transaction rollback cannot
undo any of that, so every test that provisions must register the project with `salted`
and let the fixture salt the earth afterwards — otherwise the cluster accumulates orphan
databases across runs.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession

from src.db.models.user import User
from src.services.appdb.engine import (
    get_maintenance_engine,
    reset_maintenance_engine_for_tests,
)
from src.services.appdb.names import database_name, role_name
from src.services.appdb.teardown import salt_the_earth
from tests.factories import ProjectFactory, UserFactory


@pytest.fixture
async def salted() -> AsyncIterator[list[uuid.UUID]]:
    """Collect project ids whose database/role must be destroyed after the test.

    `salt_the_earth` is idempotent and never raises, so registering a project that was
    never actually provisioned is free — register FIRST, provision second, and a failure
    mid-provision still gets cleaned up.
    """
    project_ids: list[uuid.UUID] = []
    try:
        yield project_ids
    finally:
        for project_id in project_ids:
            await salt_the_earth(
                db_name=database_name(project_id), role_name=role_name(project_id)
            )
        # Drop the cached engine too: a test that monkeypatched `settings.app_db` must not
        # leave an engine built from the previous configuration behind.
        await reset_maintenance_engine_for_tests()


@pytest.fixture
async def committed_project(test_engine: AsyncEngine) -> AsyncIterator[uuid.UUID]:
    """A user + project COMMITTED for real, outside the rolled-back `db_session`.

    The concurrency test needs two independent sessions racing over one project, which the
    single connection-bound `db_session` cannot provide. Deleting the user cascades to the
    project and on to its `project_databases` row.
    """
    async with AsyncSession(test_engine, expire_on_commit=False) as session:
        user = await UserFactory.create(session, email=f"appdb-{uuid.uuid4().hex}@rvaiglobal.com")
        project = await ProjectFactory.create(session, user.id)
        await session.commit()
        user_id, project_id = user.id, project.id
    try:
        yield project_id
    finally:
        async with AsyncSession(test_engine, expire_on_commit=False) as session:
            await session.execute(sa.delete(User).where(User.id == user_id))
            await session.commit()


@pytest.fixture
async def maintenance() -> AsyncIterator[AsyncEngine]:
    """The live maintenance engine, for catalog assertions."""
    engine = get_maintenance_engine()
    assert engine is not None, (
        "APP_DB__* must be configured in .env.test — see .env.test.example for the "
        "maintenance role this suite needs."
    )
    yield engine
