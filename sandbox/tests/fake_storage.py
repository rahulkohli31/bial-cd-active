"""Vendored dict-backed `ObjectStorage` for the Track SANDBOX harness (plan D6).

Mirrors `backend/tests/fakes.py::FakeStorage` but is VENDORED here on purpose: importing the
backend copy would drag in backend's Postgres-bound `tests/conftest.py`. It still subclasses the
REAL frozen `ObjectStorage` port (resolved via the `conftest.py` sys.path bridge), so it is a
genuine port impl — an incomplete one would fail at instantiation with `TypeError`.

`ExplodingStorage` is the ordering-test double: `put` raises so the reference client's C4
abort-before-teardown path (snapshot failure → lock NOT released) can be exercised without Azurite.
"""

from __future__ import annotations

from datetime import timedelta

from src.services.storage.base import ListPage, ObjectMeta, ObjectStorage
from src.services.storage.errors import StorageError, StorageNotFoundError


class FakeStorage(ObjectStorage):
    """A dict-backed `ObjectStorage` — just enough of the ABC for the snapshot round-trip."""

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


class ExplodingStorage(FakeStorage):
    """A `FakeStorage` whose `put` always raises — to prove the C4 abort-before-teardown rule: a
    snapshot that cannot durably store MUST NOT proceed to teardown or release the lock (R3)."""

    async def put(self, key, data, *, content_type=None, metadata=None):
        raise StorageError("simulated Blob put failure", provider="fake", key=key)
