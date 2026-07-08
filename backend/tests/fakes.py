"""Shared test doubles. `FakeStorage` is a dict-backed `ObjectStorage` so attachment
upload/download/delete and conversation delete-sweeps run without Azurite (no `integration`
marker needed)."""

from __future__ import annotations

from datetime import timedelta

from src.services.storage.base import ListPage, ObjectMeta, ObjectStorage
from src.services.storage.errors import StorageNotFoundError


class FakeStorage(ObjectStorage):
    """A dict-backed `ObjectStorage` — just enough of the ABC for the attachment routes."""

    def __init__(self) -> None:
        super().__init__(provider="fake")
        self.objects: dict[str, bytes] = {}

    async def put(self, key, data, *, content_type=None, metadata=None):
        self.objects[key] = data
        return ObjectMeta(
            key=key, size=len(data), content_type=content_type, etag=None, last_modified=None
        )

    async def get(self, key):
        if key not in self.objects:
            raise StorageNotFoundError("object not found", provider="fake", key=key)
        return self.objects[key]

    async def head(self, key):
        data = self.objects.get(key)
        if data is None:
            return None
        return ObjectMeta(
            key=key, size=len(data), content_type=None, etag=None, last_modified=None
        )

    async def delete(self, key):
        self.objects.pop(key, None)

    async def list(self, prefix, *, page_size=1000, token=None):
        return ListPage(
            keys=tuple(k for k in self.objects if k.startswith(prefix)), next_token=None
        )

    async def _signed_read_url_impl(self, key, *, expires_in: timedelta):
        return f"https://fake.local/{key}"

    async def aclose(self):
        return None
