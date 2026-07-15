"""Post-commit blob sweep (KD-3): NOTHING may surface after the rows are committed.

The Azure backend wraps most failures into `StorageError`, but transport errors
(e.g. `ServiceResponseError`) escape that hierarchy — the sweep must swallow-and-log
ANY failure and keep going, or a committed delete 500s and abandons the rest.
"""

from __future__ import annotations

import asyncio
import uuid

from src.services.storage import sweep_blobs
from src.services.storage.app_containers import AppContainerStore
from src.services.storage.sweep import _SWEEP_CONCURRENCY, sweep_app_containers
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


# --- sweep_app_containers (per-app Blob container sweep, KTD-7) ---------------

_A1 = uuid.UUID("019f1c00-0000-7000-8000-0000000000a1")
_A2 = uuid.UUID("019f1c00-0000-7000-8000-0000000000a2")
_BOOM = uuid.UUID("019f1c00-0000-7000-8000-0000000000ff")


class _FakeContainerStore(AppContainerStore):
    """A container store that records deletes without touching Azure. Overrides `__init__` so
    no config/client is needed — only `delete_container` is exercised by the sweep."""

    def __init__(self) -> None:  # noqa: D107 — deliberately does not call super()
        self.deleted: list[uuid.UUID] = []

    async def delete_container(self, app_id: uuid.UUID) -> None:
        if app_id == _BOOM:
            raise RuntimeError("transport dropped mid-response")  # the non-StorageError escape
        self.deleted.append(app_id)


async def test_sweep_app_containers_deletes_every_id() -> None:
    store = _FakeContainerStore()
    await sweep_app_containers(store, [_A1, _A2])
    assert set(store.deleted) == {_A1, _A2}


async def test_sweep_app_containers_survives_failure_and_finishes_the_batch() -> None:
    store = _FakeContainerStore()
    # _BOOM raises a raw RuntimeError mid-batch — the sweep neither raises nor stops.
    await sweep_app_containers(store, [_A1, _BOOM, _A2])
    assert set(store.deleted) == {_A1, _A2}  # the other two were still swept


async def test_sweep_app_containers_store_none_is_noop_even_with_ids() -> None:
    # Storage disabled (dev/test): the sweep no-ops even though app_ids is non-empty (KTD-2),
    # so a project delete still succeeds without an object store.
    await sweep_app_containers(None, [_A1, _A2])  # no raise, nothing to assert but the no-throw


async def test_sweep_app_containers_empty_ids_is_noop() -> None:
    store = _FakeContainerStore()
    await sweep_app_containers(store, [])
    assert store.deleted == []
