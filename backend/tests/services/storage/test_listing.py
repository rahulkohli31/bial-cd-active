"""`all_keys_under` (APPROVAL R23): the exhaustive prefix walk the delete paths
rely on. Pagination is the point — a single-page listing silently misses keys past
the page size, which on the delete paths means citizen source surviving a hard
delete — and errors must RAISE, never read as an empty prefix."""

from __future__ import annotations

import pytest

from src.services.storage.errors import StorageError
from src.services.storage.listing import all_keys_under
from tests.fakes import FakeStorage


class _ExplodingListStorage(FakeStorage):
    async def list(self, prefix, *, page_size=1000, token=None):
        raise StorageError("listing blew up", provider="fake", key=prefix)


async def test_walks_every_page_to_exhaustion() -> None:
    store = FakeStorage()
    for i in range(5):
        store.objects[f"submissions/app/{i:02d}.bundle"] = b"x"
    store.objects["submissions/other/00.bundle"] = b"x"  # outside the prefix

    keys = await all_keys_under(store, "submissions/app/", page_size=2)  # 3 pages
    assert sorted(keys) == [f"submissions/app/{i:02d}.bundle" for i in range(5)]


async def test_empty_prefix_is_an_empty_list() -> None:
    assert await all_keys_under(FakeStorage(), "submissions/none/") == []


async def test_storage_error_raises_not_empty() -> None:
    # Fail-first: a failed enumeration must never be indistinguishable from an
    # empty prefix — the delete paths would silently under-delete.
    with pytest.raises(StorageError):
        await all_keys_under(_ExplodingListStorage(), "submissions/app/")
