"""Conversation-test fixtures: swap the object store for an in-memory fake so the
delete-with-cleanup sweep runs without Azurite.

The "a build is running in this thread" seam (`building`) moved up to `tests/api/v1/conftest.py`
— BOTH conversation surfaces consult that gate, so it may not live beside only one of them.
"""

from __future__ import annotations

import pytest

from tests.fakes import FakeStorage


@pytest.fixture
def fake_storage() -> FakeStorage:
    return FakeStorage()


@pytest.fixture(autouse=True)
def _override_storage(app, fake_storage) -> None:
    from src.api.v1.attachments.router import storage_dependency

    app.dependency_overrides[storage_dependency] = lambda: fake_storage
