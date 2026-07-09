"""Post-commit blob sweep (KD-3): NOTHING may surface after the rows are committed.

The Azure backend wraps most failures into `StorageError`, but transport errors
(e.g. `ServiceResponseError`) escape that hierarchy — the sweep must swallow-and-log
ANY failure and keep going, or a committed delete 500s and abandons the rest.
"""

from __future__ import annotations

from src.services.storage import sweep_blobs
from tests.fakes import FakeStorage


class _FlakyStorage(FakeStorage):
    """Deletes normally, except a marker key that raises a NON-StorageError (the
    ServiceResponseError class of escape)."""

    async def delete(self, key: str) -> None:
        if key == "boom":
            raise RuntimeError("transport dropped mid-response")
        await super().delete(key)


async def test_sweep_survives_any_failure_and_finishes_the_batch() -> None:
    storage = _FlakyStorage()
    storage.objects = {"a": b"1", "c": b"3"}

    # "boom" raises a raw RuntimeError mid-batch — the sweep neither raises nor stops.
    await sweep_blobs(storage, ["a", "boom", "c"])

    assert storage.objects == {}  # every real key was still swept


async def test_sweep_missing_key_is_a_noop() -> None:
    storage = FakeStorage()
    await sweep_blobs(storage, ["never-existed"])  # no raise
