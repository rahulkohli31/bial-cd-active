"""Chat-endpoint fixtures: bind the disconnect-safe billing drain to the rolled-back test
session (so billing is observable + rolled back), and a helper to inject a `TestModel`."""

from __future__ import annotations

import contextlib

import pytest


@pytest.fixture(autouse=True)
def _override_billing(app, db_session) -> None:
    from src.api.v1.claude.router import billing_session_factory

    @contextlib.asynccontextmanager
    async def _session():
        # Yield the test session; do NOT close/rollback it here (the fixture owns teardown).
        yield db_session

    app.dependency_overrides[billing_session_factory] = lambda: lambda: _session()


@pytest.fixture
def set_chat_model(app):
    """Inject a Pydantic AI model (a TestModel) for the chat endpoint."""

    def _set(model) -> None:
        from src.api.v1.claude.router import chat_model

        app.dependency_overrides[chat_model] = lambda: model

    return _set
