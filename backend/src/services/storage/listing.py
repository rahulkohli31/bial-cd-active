"""Exhaustive prefix enumeration for the delete paths (APPROVAL R23).

`all_keys_under` walks `ListPage.next_token` to exhaustion — a single-page listing
silently misses everything past `DEFAULT_PAGE_SIZE`, which on the delete paths
means citizen source surviving a hard delete. Errors RAISE upward deliberately
(the opposite of `sweep.py`'s swallow-and-log): a partial enumeration is
indistinguishable from a complete one, so the caller must decide — the project
cascade lets the raise roll the whole delete back with nothing destroyed (KD-3),
and `nuke_app` lets it surface so the admin's delete fails retryably instead of
dropping the row and stranding unfindable blobs.
"""

from __future__ import annotations

from src.services.storage.base import ObjectStorage
from src.services.storage.constants import DEFAULT_PAGE_SIZE


async def all_keys_under(
    storage: ObjectStorage, prefix: str, *, page_size: int = DEFAULT_PAGE_SIZE
) -> list[str]:
    """Every key under `prefix`, across every page. Raises `StorageError` upward —
    an enumeration failure must never read as an empty prefix (fail-first)."""
    keys: list[str] = []
    token: str | None = None
    while True:
        page = await storage.list(prefix, page_size=page_size, token=token)
        keys.extend(page.keys)
        if page.next_token is None:
            return keys
        token = page.next_token
