"""Project-test fixtures: swap the object store for an in-memory fake (cascade-delete blob
sweep), inject a chat model, and bind the chat relay's billing drain to the test session
(so the U8/U11 description/code-seed tests that hit `/v1/claude` roll back cleanly)."""

from __future__ import annotations

import contextlib

import pytest

from tests.fakes import FakeStorage


@pytest.fixture
def fake_storage() -> FakeStorage:
    return FakeStorage()


@pytest.fixture(autouse=True)
def _override_billing(app, db_session) -> None:
    from src.api.v1.claude.router import billing_session_factory

    @contextlib.asynccontextmanager
    async def _session():
        yield db_session  # the fixture owns teardown (rollback); don't close here

    app.dependency_overrides[billing_session_factory] = lambda: lambda: _session()


@pytest.fixture(autouse=True)
def _override_storage(app, fake_storage) -> None:
    from src.api.v1.attachments.router import storage_dependency

    app.dependency_overrides[storage_dependency] = lambda: fake_storage


@pytest.fixture
def set_chat_model(app):
    """Inject a Pydantic AI model (a TestModel/FunctionModel) for description generation —
    the describe endpoint resolves the same `chat_model` dependency as the chat relay."""

    def _set(model) -> None:
        from src.api.v1.claude.router import chat_model

        app.dependency_overrides[chat_model] = lambda: model

    return _set
