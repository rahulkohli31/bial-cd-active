"""U14 — no container is destroyed unless a durable copy of its work is confirmed current.

R9, R11. Every other guard in this system protects money. This one protects work, and it is the
last thing standing between a scheduled process with ARM delete authority and somebody's unsaved
afternoon.

THE TEST THAT MATTERS MOST is `test_a_storage_off_deployment_cannot_authorise_a_single_delete`.
`manager.py` reads `StorageUnconfiguredError` as a CONFIRMED absent bundle, which is correct for
the build path — on a storage-off deployment you must not offer a restore that cannot work. Read
by a destroy path, that same value says "nothing to preserve, safe to delete" about every
container at once, so the most natural misconfiguration in the system would produce a worker that
deleted the entire fleet while believing it had verified each one.
"""

from __future__ import annotations

import uuid

import pytest
import redis.asyncio as aioredis

from src.services.build_sessions import durable_copy
from src.services.build_sessions.durable_copy import (
    CopyState,
    confirm_durable_copy,
    head_of_bundle,
)
from src.services.build_sessions.reaper import reap_user
from src.services.redis import registry_key
from src.services.redis.keys import (
    REGISTRY_FIELD_APP_NAME,
    REGISTRY_FIELD_FQDN,
    REGISTRY_FIELD_STATE,
)
from src.services.storage import recovery_key, snapshot_key
from src.services.storage.errors import StorageError, StorageUnconfiguredError
from tests.fakes import FakeSandboxClient, FakeStorage, a_git_bundle

APP = uuid.uuid4()
USER = uuid.uuid4()
HEAD = "a" * 40
OLDER = "b" * 40


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> FakeStorage:
    fake = FakeStorage()
    monkeypatch.setattr(durable_copy, "get_storage", lambda: fake)
    return fake


async def _put_recovery(store: FakeStorage, sha: str | None) -> None:
    await store.put(
        recovery_key(APP),
        a_git_bundle(sha or HEAD),
        metadata={"head_sha": sha} if sha else {},
    )


# --- the comparison ---------------------------------------------------------------


async def test_a_recovery_copy_matching_head_is_confirmed(store: FakeStorage) -> None:
    await _put_recovery(store, HEAD)

    verdict = await confirm_durable_copy(APP, container_head=HEAD)

    assert verdict.state is CopyState.CONFIRMED_CURRENT
    assert verdict.may_destroy is True


async def test_a_recovery_copy_behind_head_is_stale_not_destroyable(store: FakeStorage) -> None:
    """*Covers AE7.* The deadline lapsed but the newest copy predates the newest change. A copy
    must be taken first; until one is, this container is not eligible for anything."""
    await _put_recovery(store, OLDER)

    verdict = await confirm_durable_copy(APP, container_head=HEAD)

    assert verdict.state is CopyState.STALE
    assert verdict.may_destroy is False


async def test_currency_is_the_sha_not_the_timestamp(store: FakeStorage) -> None:
    """Azure stamps `last_modified` in WHOLE SECONDS, so a Save and an autosave inside one second
    are indistinguishable by time. On this path "indistinguishable" means deleting a container
    whose newest change was never copied — so the comparison is the sha, and a fresh blob whose
    sha is stale still reads STALE."""
    await _put_recovery(store, OLDER)
    # As freshly written as anything can be; the clock says current, the content does not.
    assert store.mtimes[recovery_key(APP)] is not None

    assert (await confirm_durable_copy(APP, container_head=HEAD)).state is CopyState.STALE


async def test_the_saved_bundle_is_not_a_substitute_for_the_recovery_slot(
    store: FakeStorage,
) -> None:
    """R11 asks for the builder's LAST COMPLETED CHANGE. The recovery slot is the platform's
    autosave at turn boundaries; `snapshot_key` is the user's explicit Save. A builder who never
    pressed Save still has work worth keeping — and they are the population most likely to be
    reclaimed, so reading the wrong slot would lose exactly the work it was written to protect."""
    await store.put(snapshot_key(APP), a_git_bundle(HEAD), metadata={"head_sha": HEAD})

    verdict = await confirm_durable_copy(APP, container_head=HEAD)

    assert verdict.state is CopyState.UNCONFIRMED
    assert verdict.may_destroy is False


# --- everything unreadable spares -------------------------------------------------


async def test_a_storage_off_deployment_cannot_authorise_a_single_delete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE FLEET-DELETING MISCONFIGURATION (Q4).

    `manager.py:156` returns False on `StorageUnconfiguredError` and calls it a CONFIRMED absent,
    which is right for its caller. Consumed here that value would mean "no work to preserve" for
    every container simultaneously — a worker deleting the whole fleet while every check read
    green. It is a fact about the deployment, not about anybody's work.

    Mutation-check: let the `StorageUnconfiguredError` arm fall through to the no-bundle branch
    and this stays UNCONFIRMED only by accident; make it return CONFIRMED_CURRENT and this is the
    single test that goes red."""

    def _no_store() -> object:
        raise StorageUnconfiguredError("no OBJECT_STORE__ block on this deployment")

    monkeypatch.setattr(durable_copy, "get_storage", _no_store)

    verdict = await confirm_durable_copy(APP, container_head=HEAD)

    assert verdict.state is CopyState.UNCONFIRMED
    assert verdict.may_destroy is False


async def test_an_unreachable_store_spares_rather_than_destroys(
    store: FakeStorage, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A timeout is not a death certificate. Only positive confirmation may take a destructive
    branch — an outage must never read as 'nothing to lose'."""

    async def _boom(_key: str) -> None:
        raise StorageError("blob unreachable", provider="fake", key="k")

    monkeypatch.setattr(store, "head", _boom)

    assert (await confirm_durable_copy(APP, container_head=HEAD)).may_destroy is False


async def test_a_bundle_with_no_stamped_sha_cannot_be_compared(store: FakeStorage) -> None:
    """Older bundles predate the metadata stamp. A copy whose head is unknown is a signal that
    could not be read, and R4 sends every one of those to escalate."""
    await store.put(recovery_key(APP), a_git_bundle(HEAD), metadata={})

    assert (await confirm_durable_copy(APP, container_head=HEAD)).state is CopyState.UNCONFIRMED


async def test_no_recovery_copy_at_all_is_unconfirmed_not_permission(store: FakeStorage) -> None:
    """The most tempting wrong answer in the whole unit: "there is no copy, so there is nothing to
    preserve". There is no copy, so there is nothing to preserve it WITH."""
    assert (await confirm_durable_copy(APP, container_head=HEAD)).may_destroy is False


# --- the unreachable-container fallback -------------------------------------------


async def test_an_unreachable_container_falls_back_to_a_parseable_bundle(
    store: FakeStorage,
) -> None:
    """THE GATE MUST STAY SATISFIABLE. An orphan has no registry record and may not answer at all,
    so requiring the live `HEAD` comparison in this branch would spare every genuinely-dead
    container forever and collect nothing — which is the round-1 wording this replaced.

    A present, parseable bundle stands in. The real comparison still happens in the normal case."""
    await _put_recovery(store, HEAD)

    verdict = await confirm_durable_copy(APP, container_head=None)

    assert verdict.state is CopyState.CONFIRMED_CURRENT


async def test_an_unreachable_container_with_no_bundle_still_escalates(
    store: FakeStorage,
) -> None:
    """The fallback is a fallback, not a bypass: no bundle and no container means nothing was
    established, and nothing established never authorises a delete."""
    assert (await confirm_durable_copy(APP, container_head=None)).may_destroy is False


def test_a_bundle_header_is_checked_against_itself_not_its_metadata() -> None:
    """The write and the check must not share a source. Confirming a fresh copy by re-reading the
    metadata we just wrote proves only that we can write metadata."""
    assert head_of_bundle(a_git_bundle(HEAD)) == HEAD
    assert head_of_bundle(b"not a bundle at all") is None


# --- reap_user is gated too, which it never was ------------------------------------


async def _register(redis: aioredis.Redis) -> None:
    await redis.hset(
        registry_key(USER),
        mapping={
            REGISTRY_FIELD_APP_NAME: "sbx-x",
            REGISTRY_FIELD_FQDN: "sbx-x.example.io",
            REGISTRY_FIELD_STATE: "ready",
        },
    )


async def test_reap_user_refuses_when_the_copy_cannot_be_confirmed(
    fake_redis: aioredis.Redis, store: FakeStorage
) -> None:
    """THE F1 PATH WAS THE UNGATED ONE. `reap_user` called `sandbox_client.teardown` with no
    durable-copy check at all, and it is the path that does almost all of the deleting — so a gate
    added only to the orphan path would have protected the rare case and left the common one
    exactly as it was."""
    await _register(fake_redis)
    client = FakeSandboxClient()

    reaped = await reap_user(fake_redis, USER, client, app_id=APP)

    assert reaped is False
    assert client.torn_down == []
    # SPARED AND REPORTED — the lock and registry stay so a later pass retries once a copy exists.
    assert await fake_redis.exists(registry_key(USER)) == 1


async def test_reap_user_proceeds_once_the_copy_is_confirmed(
    fake_redis: aioredis.Redis, store: FakeStorage
) -> None:
    await _register(fake_redis)
    await _put_recovery(store, HEAD)
    client = FakeSandboxClient()

    reaped = await reap_user(fake_redis, USER, client, app_id=APP)

    assert reaped is True
    assert client.torn_down == ["sbx-x"]


async def test_a_caller_that_passes_no_app_id_is_unchanged(
    fake_redis: aioredis.Redis, store: FakeStorage
) -> None:
    """Reconcile-on-start and the sweep reap a user's OWN stale state, where the builder is about
    to be handed a fresh container anyway. They stay byte-identical; the scheduled janitor — the
    process with no human watching it — passes the id and is gated."""
    await _register(fake_redis)
    client = FakeSandboxClient()

    assert await reap_user(fake_redis, USER, client) is True
    assert client.torn_down == ["sbx-x"]
