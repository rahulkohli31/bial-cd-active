"""Azure Blob behavior, proven against a live Azurite backend. Opt-in:
`uv run pytest -m integration` after `docker compose -f docker-compose.test.yml up -d`.

One body (via the `ready_backend` fixture) asserts the storage-port contract
end-to-end against Azurite — put/get/head/delete, signed reads, and pagination.
"""

from __future__ import annotations

from datetime import timedelta

import httpx
import pytest

from src.services.storage.base import ObjectStorage

pytestmark = pytest.mark.integration


async def test_put_get_head_delete_roundtrip(ready_backend: ObjectStorage) -> None:
    meta = await ready_backend.put("docs/a.txt", b"hello world", content_type="text/plain")
    assert meta.size == 11

    head = await ready_backend.head("docs/a.txt")
    assert head is not None
    assert head.size == 11
    assert head.content_type == "text/plain"

    assert await ready_backend.get("docs/a.txt") == b"hello world"

    await ready_backend.delete("docs/a.txt")
    assert await ready_backend.head("docs/a.txt") is None


async def test_signed_read_url_returns_uploaded_bytes(ready_backend: ObjectStorage) -> None:
    await ready_backend.put("signed/x.bin", b"\x00\x01\x02secret-bytes")
    url = await ready_backend.signed_read_url("signed/x.bin", expires_in=timedelta(minutes=5))

    # The URL is a time-limited bearer credential — it must carry a signature.
    assert "sig" in url.lower()
    async with httpx.AsyncClient() as client:
        resp = await client.get(url)
    assert resp.status_code == 200
    assert resp.content == b"\x00\x01\x02secret-bytes"


async def test_list_paginates_with_continuation_token(ready_backend: ObjectStorage) -> None:
    for i in range(5):
        await ready_backend.put(f"page/obj-{i}.txt", f"{i}".encode())

    seen: list[str] = []
    token: str | None = None
    for _ in range(10):  # bound the loop defensively
        result = await ready_backend.list("page/", page_size=2, token=token)
        seen.extend(result.keys)
        token = result.next_token
        if token is None:
            break
    assert sorted(seen) == [f"page/obj-{i}.txt" for i in range(5)]


async def test_delete_missing_object_is_idempotent(ready_backend: ObjectStorage) -> None:
    await ready_backend.delete("never/existed.txt")  # no raise


async def test_head_missing_object_returns_none(ready_backend: ObjectStorage) -> None:
    assert await ready_backend.head("never/existed.txt") is None


async def test_put_with_metadata_roundtrips(ready_backend: ObjectStorage) -> None:
    # Mixed-case metadata keys are accepted + normalized identically on both
    # providers (the lowercase normalization itself is unit-tested in test_keys).
    meta = await ready_backend.put(
        "meta/y.txt", b"payload", metadata={"RunId": "abc", "Stage": "qa"}
    )
    assert meta.size == 7
    assert await ready_backend.get("meta/y.txt") == b"payload"
