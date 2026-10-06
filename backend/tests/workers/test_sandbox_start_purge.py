"""The hourly purge of sandbox start rows: what it deletes, what it keeps, and that a second run
changes nothing the first did not."""

from __future__ import annotations

import contextlib
import uuid
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import pytest
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from src.db import base as db_base
from src.db.models.sandbox_start import SandboxProjectType, SandboxStart, SandboxStartKind
from src.workers.sandbox_start_purge import purge_old_sandbox_starts
from tests.factories import AppRegistryFactory, UserFactory


@pytest.fixture
def purge_reads_this_test(db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch) -> None:
    """The pass opens a session of its own, so it is pointed at the rolled-back test session that
    holds the rows."""

    @contextlib.asynccontextmanager
    async def _session() -> AsyncIterator[AsyncSession]:
        yield db_session

    monkeypatch.setattr(db_base, "async_session_factory", lambda: _session())


async def _started(db: AsyncSession, user_id: uuid.UUID, app_id: uuid.UUID, days_ago: int) -> None:
    db.add(
        SandboxStart(
            user_id=user_id,
            app_id=app_id,
            kind=SandboxStartKind.REOPEN,
            project_type=SandboxProjectType.PLAIN,
            started_at=datetime.now(UTC) - timedelta(days=days_ago),
        )
    )
    await db.flush()


async def _ages(db: AsyncSession) -> list[int]:
    started = await db.scalars(sa.select(SandboxStart.started_at))
    return sorted((datetime.now(UTC) - at).days for at in started)


async def test_the_purge_deletes_rows_past_ninety_days_and_keeps_the_rest(
    db_session: AsyncSession, purge_reads_this_test: None
) -> None:
    # Mutation check: move the line to 120 days and the 91-day row survives.
    await db_session.execute(sa.delete(SandboxStart))
    user = await UserFactory.create(db_session, email="purge@rvaiglobal.com")
    app = await AppRegistryFactory.create(db_session, user_id=user.id)
    for days_ago in (91, 89, 0):
        await _started(db_session, user.id, app.id, days_ago)

    await purge_old_sandbox_starts()
    assert await _ages(db_session) == [0, 89]

    await purge_old_sandbox_starts()
    assert await _ages(db_session) == [0, 89], "a second pass deleted what the first kept"
