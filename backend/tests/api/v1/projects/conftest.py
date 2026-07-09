"""Project-test fixtures: swap the object store for an in-memory fake so the
cascade-delete blob sweep runs without Azurite."""

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
