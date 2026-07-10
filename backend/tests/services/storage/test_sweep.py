"""Post-commit blob sweep (KD-3): NOTHING may surface after the rows are committed.

The Azure backend wraps most failures into `StorageError`, but transport errors
(e.g. `ServiceResponseError`) escape that hierarchy — the sweep must swallow-and-log
ANY failure and keep going, or a committed delete 500s and abandons the rest.
"""

from __future__ import annotations

import asyncio

from src.services.storage import sweep_blobs
from src.services.storage.sweep import _SWEEP_CONCURRENCY
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


class _ConcurrencyTrackingStorage(FakeStorage):
    """Records how many deletes are ever in-flight at once, so the bounded-fan-out
    contract can be asserted (a serial sweep would peak at 1; an unbounded one at N)."""

    def __init__(self) -> None:
        super().__init__()
        self.in_flight = 0
        self.max_in_flight = 0

    async def delete(self, key: str) -> None:
        self.in_flight += 1
        self.max_in_flight = max(self.max_in_flight, self.in_flight)
        try:
            await asyncio.sleep(0)  # yield so siblings can pile up under the semaphore
            await super().delete(key)
        finally:
            self.in_flight -= 1


async def test_sweep_runs_concurrently_but_bounded() -> None:
    storage = _ConcurrencyTrackingStorage()
    keys = [f"k{i}" for i in range(_SWEEP_CONCURRENCY * 3)]
    storage.objects = dict.fromkeys(keys, b"x")

    await sweep_blobs(storage, keys)

    assert storage.objects == {}  # every key swept
    # Fanned out past a serial sweep (peak > 1) yet never exceeded the semaphore ceiling.
    assert storage.max_in_flight > 1
    assert storage.max_in_flight <= _SWEEP_CONCURRENCY
