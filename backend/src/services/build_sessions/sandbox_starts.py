"""One sandbox start on the books: its `sandbox_starts` row, filled from the start's stopwatch.

Admitted at the lock every door into a container passes, written once the environment of a
container about to be born is complete, closed when its first page is seen or the start fails. A
start that attaches to a running container is never written. Each write runs in a short session
of its own and a database failure is logged, never raised: timing must not fail or hold up the
start it times.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
import structlog
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models.sandbox_start import (
    SandboxProjectType,
    SandboxStart,
    SandboxStartKind,
    SandboxStartOutcome,
)
from src.services.build_sessions.alarms import SANDBOX_START_NOT_RECORDED_EVENT
from src.services.lake.env import identity_resource_id_for_env
from src.services.sandbox.stopwatch import Stopwatch

_log = structlog.get_logger()

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]


class StartRecord(Stopwatch):
    """A start's stopwatch plus the row it is written to. The id exists from the door, so a
    second press joining this start can be handed the same one."""

    def __init__(self) -> None:
        super().__init__()
        self.id = uuid.uuid7()
        self.kind: SandboxStartKind | None = None
        self._user_id: uuid.UUID | None = None
        self._books: SessionFactory | None = None
        self._written = False
        self._closed = False

    @property
    def on_the_books(self) -> uuid.UUID | None:
        """This start's id once its row exists, which is what a client may report against."""
        return self.id if self._written else None

    def admitted(
        self, kind: SandboxStartKind, *, user_id: uuid.UUID, books: SessionFactory
    ) -> None:
        self.kind = kind
        self._user_id = user_id
        self._books = books
        self.split("admitted")

    async def open(self, *, app_id: uuid.UUID, env: dict[str, str]) -> None:
        """`env` is the complete environment of the container about to be born: its settings
        stage ends here and the row is written. A connector project is one whose environment
        carries the data identity, the fact the create reads it from. Only an admitted start is
        written."""
        self.split("settings")
        if self._books is None or self.kind is None or self._user_id is None:
            return
        has_identity = identity_resource_id_for_env(env) is not None
        try:
            async with self._books() as db:
                await db.execute(
                    sa.insert(SandboxStart).values(
                        id=self.id,
                        user_id=self._user_id,
                        app_id=app_id,
                        kind=self.kind,
                        project_type=(
                            SandboxProjectType.CONNECTOR
                            if has_identity
                            else SandboxProjectType.PLAIN
                        ),
                        started_at=self.started_at,
                    )
                )
                await db.commit()
        except (SQLAlchemyError, OSError):  # fmt: skip  # ruff py314 strips parens
            _log.warning(
                SANDBOX_START_NOT_RECORDED_EVENT, step="open", start_id=str(self.id), exc_info=True
            )
            return
        self._written = True

    async def close(self, outcome: SandboxStartOutcome | None) -> None:
        """Stop the stopwatch and write what it holds. The first close wins; a start whose row
        was never written closes nothing. A start that served ended at its first page, which
        can be seconds before the close that records it. `None` closes a start that neither
        failed nor was seen to serve: the stages it reached are kept and it has no end."""
        books = self._books
        if not self._written or self._closed or books is None:
            return
        self._closed = True
        self.stop()
        to_first_page = self.elapsed_ms(None, "first_page")
        if outcome is None and to_first_page is not None:
            # A page was seen; only the close that would have said so never ran.
            outcome = SandboxStartOutcome.SERVED
        ended_at: datetime | None = None
        if to_first_page is not None:
            ended_at = self.started_at + timedelta(milliseconds=to_first_page)
        elif outcome is not None:
            ended_at = datetime.now(UTC)
        try:
            async with books() as db:
                await db.execute(
                    sa.update(SandboxStart)
                    .where(SandboxStart.id == self.id, SandboxStart.user_id == self._user_id)
                    .values(
                        ended_at=ended_at,
                        outcome=outcome,
                        admission_ms=self.elapsed_ms(None, "admitted"),
                        settings_ms=self.elapsed_ms("admitted", "settings"),
                        create_ms=self.elapsed_ms("settings", "created"),
                        dev_start_ms=self.elapsed_ms("created", "dev_started"),
                        first_page_ms=self.elapsed_ms("dev_started", "first_page"),
                        sub_steps=dict(self.laps),
                        reinstalled=self.reinstalled,
                    )
                )
                await db.commit()
        except (SQLAlchemyError, OSError):  # fmt: skip  # ruff py314 strips parens
            _log.warning(
                SANDBOX_START_NOT_RECORDED_EVENT,
                step="close",
                start_id=str(self.id),
                exc_info=True,
            )
