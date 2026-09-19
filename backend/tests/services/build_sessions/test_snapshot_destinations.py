"""Destination — the two places a bundle can go, and what the stamped one guarantees."""

from __future__ import annotations

import datetime as dt
import uuid

import pytest

from src.services.build_sessions.snapshot import Destination, write_snapshot
from src.services.sandbox.base import ExecResult, SandboxHandle
from src.services.storage import quarantine_key, snapshot_key
from tests.fakes import FakeSandboxClient, FakeStorage, a_git_bundle

APP = uuid.UUID("0198f2c0-2222-7000-8000-0000000d1ff7")
MOVED_ON = "b" * 40
TAKEN_AT = dt.datetime(2026, 8, 18, 11, 30, 0, 123456, tzinfo=dt.UTC)

_HANDLE = SandboxHandle(
    fqdn="sbx.example.io",
    token="tok",
    app_name="sbx-app",
    preview_url="https://sbx.example.io/",
    ready=True,
)


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> FakeStorage:
    fake = FakeStorage()
    monkeypatch.setattr("src.services.build_sessions.snapshot.get_storage", lambda: fake)
    return fake


def _container(*, bundles_to: str) -> FakeSandboxClient:
    """A container that answers the whole snapshot ladder: commit, bundle, base64."""
    import base64 as _b64

    client = FakeSandboxClient()
    bundle = _b64.b64encode(a_git_bundle(bundles_to)).decode()

    def handler(cmd: list[str]) -> ExecResult:
        if cmd[0] == "base64":
            return ExecResult(stdout=bundle, stderr="", exit=0)
        return ExecResult(stdout="", stderr="", exit=0)

    client.exec_handler = handler
    return client


async def _head_sha_in_slot(store: FakeStorage, key: str) -> str | None:
    meta = await store.head(key)
    return (meta.metadata or {}).get("head_sha") if meta else None


async def test_write_snapshot_defaults_to_the_users_saved_bundle(store: FakeStorage) -> None:
    """The key every caller of this function means, so it is the default rather than a flag."""
    await write_snapshot(_container(bundles_to=MOVED_ON), _HANDLE, APP)

    assert await _head_sha_in_slot(store, snapshot_key(APP)) == MOVED_ON


async def test_a_quarantine_write_lands_where_an_operator_can_find_it(
    store: FakeStorage,
) -> None:
    client = _container(bundles_to=MOVED_ON)

    await write_snapshot(client, _HANDLE, APP, destination=Destination.quarantine(APP, TAKEN_AT))

    assert await _head_sha_in_slot(store, quarantine_key(APP, TAKEN_AT)) == MOVED_ON
    assert await store.head(snapshot_key(APP)) is None, "the saved copy was not touched"


def test_the_stamped_keys_sort_chronologically() -> None:
    """An operator listing these wants them in the order they happened, and lexical order is the
    only order a blob listing offers."""
    earlier = quarantine_key(APP, TAKEN_AT)
    later = quarantine_key(APP, TAKEN_AT + dt.timedelta(microseconds=1))

    assert earlier < later


def test_a_naive_datetime_is_refused_rather_than_read_as_utc() -> None:
    """Two objects stamped from different offsets would sort by wall clock rather than by when
    they happened — silently reordering an operator's evidence."""
    from src.services.storage.errors import StorageError

    with pytest.raises(StorageError):
        quarantine_key(APP, dt.datetime(2026, 8, 18, 11, 30))  # noqa: DTZ001
