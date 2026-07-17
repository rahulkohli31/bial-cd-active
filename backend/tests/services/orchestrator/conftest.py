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
import uuid
from collections.abc import AsyncIterator, Callable, Sequence
from contextlib import AbstractAsyncContextManager

import pytest
from pydantic_ai import BinaryContent, models
from pydantic_ai.models import Model
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.v1.build_sessions.schemas import ProgressEnvelope
from src.services.orchestrator.harness import (
    BuildOrchestrator,
    BuildSpec,
    RunContextProvider,
    SessionFactory,
)


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


def make_provider(
    prompt: str | Sequence[str | BinaryContent], app_id: uuid.UUID
) -> RunContextProvider:
    """A KD-13 run-context provider double: `session_id -> BuildSpec{prompt, app_id}`. The prompt
    may be a bare string or the R3 multimodal sequence SESSION-API resolves for a turn carrying
    attachments."""

    async def _provider(session_id: uuid.UUID) -> BuildSpec:
        return BuildSpec(prompt=prompt, app_id=app_id)

    return _provider


def make_orchestrator(
    model: Model,
    session_factory: SessionFactory,
    *,
    prompt: str | Sequence[str | BinaryContent] = "build a records app",
    app_id: uuid.UUID | None = None,
) -> tuple[BuildOrchestrator, uuid.UUID]:
    """Construct a BuildOrchestrator wired to test doubles. `readiness_poll_s=0` so the readiness
    poll never sleeps in the suite; a small poll budget keeps a stuck-dev test bounded."""
    resolved_app_id = app_id if app_id is not None else uuid.uuid4()
    orchestrator = BuildOrchestrator(
        model=model,
        session_factory=session_factory,
        run_context_provider=make_provider(prompt, resolved_app_id),
        readiness_max_polls=5,
        readiness_poll_s=0.0,
    )
    return orchestrator, resolved_app_id
