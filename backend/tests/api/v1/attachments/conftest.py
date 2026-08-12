"""Attachment-test fixtures: an in-memory object store swapped in for the real Azure backend
so the upload/download/delete lane runs without Azurite (no `integration` marker needed)."""

from __future__ import annotations

import pytest

from tests.fakes import FakeStorage


@pytest.fixture
def fake_storage() -> FakeStorage:
    return FakeStorage()


@pytest.fixture(autouse=True)
def _override_storage(app, fake_storage) -> None:
    # Point the attachment routes at the in-memory store for every test in this package.
    from src.api.v1.attachments.router import storage_dependency

    app.dependency_overrides[storage_dependency] = lambda: fake_storage
