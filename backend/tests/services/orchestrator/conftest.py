"""Orchestrator-test scaffolding.

The autouse `_no_live_model` guard forbids any live model request for the whole package
(mirrors `tests/services/agent/conftest.py`): the `FunctionModel` never hits the network, but an
accidental real call fails loudly instead of billing Foundry.

`CollectingSink` is the in-process `on_progress` double (every emitted envelope lands in
`.events`). `billing_factory` binds BRAIN's per-model-step session to the rolled-back test session
(the substitution `claude/router.py` makes via `dependency_overrides`, adapted for a construction-
time dependency). The run-context-provider fixture lives with the harness that consumes it (U6).
"""

from __future__ import annotations

import contextlib
from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager

import pytest
from pydantic_ai import models
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.v1.build_sessions.schemas import ProgressEnvelope


@pytest.fixture(autouse=True)
def _no_live_model():
    previous = models.ALLOW_MODEL_REQUESTS
    models.ALLOW_MODEL_REQUESTS = False
    yield
    models.ALLOW_MODEL_REQUESTS = previous


class CollectingSink:
    """A `ProgressSink` double: every emitted envelope is appended to `events`."""

    def __init__(self) -> None:
        self.events: list[ProgressEnvelope] = []

    async def __call__(self, env: ProgressEnvelope) -> None:
        self.events.append(env)


@pytest.fixture
def sink() -> CollectingSink:
    return CollectingSink()


@pytest.fixture
def billing_factory(
    db_session: AsyncSession,
) -> Callable[[], AbstractAsyncContextManager[AsyncSession]]:
    """A per-model-step session factory bound to the rolled-back test session, so metering writes
    are observable and rolled back. Each `factory()` yields the SAME test session (in production a
    fresh `async_sessionmaker` session per step); the harness owns the commit."""

    @contextlib.asynccontextmanager
    async def _session() -> AsyncIterator[AsyncSession]:
        yield db_session

    return _session
