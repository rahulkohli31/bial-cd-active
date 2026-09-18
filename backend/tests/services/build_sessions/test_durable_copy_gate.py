"""No container is destroyed unless a durable copy of its work is confirmed current.

Every other guard in this system protects money. This one protects work, and it is the
last thing standing between a scheduled process with ARM delete authority and somebody's unsaved
afternoon.

THE TEST THAT MATTERS MOST is `test_a_storage_off_deployment_cannot_authorise_a_single_delete`.
`manager.py` reads `StorageUnconfiguredError` as a CONFIRMED absent bundle, which is correct for
the build path. Read by a destroy path, that same value would say "nothing to preserve, safe to
delete" about every container at once — the misconfiguration that deletes the whole fleet while
believing it verified each one."""

from __future__ import annotations

import base64
import uuid
from collections.abc import Sequence
from datetime import UTC, datetime

import pytest
import redis.asyncio as aioredis
import sqlalchemy as sa
import structlog.testing
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models.worker_pass import PassOutcome, WorkerPass
from src.services.build_sessions import durable_copy, pass_history
from src.services.build_sessions.durable_copy import CopyState, confirm_durable_copy
from src.services.build_sessions.pass_history import (
    _ATTEMPT_MEANING,
    CopyAttempt,
    reclamation_pass_freshness,
    record_durable_copy_attempt,
)
from src.services.build_sessions.reaper import reap_the_container_we_judged, reap_user
from src.services.build_sessions.snapshot import (
    SAVED_COPY_WRITE_DID_NOT_LAND_EVENT,
    SavedCopyOutcome,
    SavedCopyWrite,
    reset_divert_streaks_for_tests,
    write_saved_copy_under_guard,
)
from src.services.redis import registry_key
from src.services.redis.keys import (
    REGISTRY_FIELD_APP_NAME,
    REGISTRY_FIELD_FQDN,
    REGISTRY_FIELD_STATE,
)
from src.services.sandbox import SandboxError
from src.services.sandbox.base import ExecResult, SandboxHandle
from src.services.storage import divert_prefix, recovery_key, snapshot_key
from src.services.storage.errors import StorageError, StorageUnconfiguredError
from tests.fakes import FakeSandboxClient, FakeStorage, a_git_bundle, a_sandbox_name

APP = uuid.uuid4()
USER = uuid.uuid4()
HEAD = "a" * 40
OLDER = "b" * 40
#: A container that has factory-reset: its HEAD is unrelated to anything on record.
REVERTED = "e" * 40
#: What the commit step inside the guarded write turns the working tree into. Distinct from
#: `HEAD` on purpose: the snapshot commits BEFORE it bundles, so the sha that lands in the slot is
#: never the sha the gate compared, and a fixture that reused one value would hide the difference.
BUNDLED = "c" * 40


@pytest.fixture
def store(monkeypatch: pytest.MonkeyPatch) -> FakeStorage:
    fake = FakeStorage()
    monkeypatch.setattr(durable_copy, "get_storage", lambda: fake)
    return fake


@pytest.fixture(autouse=True)
def attempts(monkeypatch: pytest.MonkeyPatch) -> list[CopyAttempt]:
    """Every copy-before-reclaim outcome this test recorded, without touching the database.

    AUTOUSE, not convenience: `record_durable_copy_attempt` opens its own session and commits, so
    inside this suite's otherwise-rolled-back transaction an unguarded reap would leave a
    permanent row that `test_reclamation_report_only.py` counts. The one place the real writer
    runs is `test_the_copy_record_reaches_the_database_and_is_committed` below."""
    recorded: list[CopyAttempt] = []

    async def _spy(attempt: CopyAttempt) -> None:
        recorded.append(attempt)

    monkeypatch.setattr(pass_history, "record_durable_copy_attempt", _spy)
    return recorded


@pytest.fixture(autouse=True)
def _forget_the_divert_streak() -> None:
    """The refusal counter behind the divert escalation is PROCESS-LOCAL, so a divert here would
    otherwise be carried into whatever test ran next in this interpreter."""
    reset_divert_streaks_for_tests()


async def _put_saved(store: FakeStorage, sha: str | None) -> None:
    await store.put(
        snapshot_key(APP),
        a_git_bundle(sha or HEAD),
        metadata={"head_sha": sha} if sha else {},
    )


# --- the comparison ---------------------------------------------------------------


async def test_a_saved_copy_matching_head_is_confirmed(store: FakeStorage) -> None:
    await _put_saved(store, HEAD)

    verdict = await confirm_durable_copy(APP, container_head=HEAD, container_dirty=False)

    assert verdict.state is CopyState.CONFIRMED_CURRENT
    assert verdict.may_destroy is True


async def test_a_saved_copy_behind_head_is_stale_not_destroyable(store: FakeStorage) -> None:
    await _put_saved(store, OLDER)

    verdict = await confirm_durable_copy(APP, container_head=HEAD, container_dirty=False)

    assert verdict.state is CopyState.STALE
    assert verdict.may_destroy is False


async def test_a_matching_head_over_a_dirty_tree_is_not_destroyable(store: FakeStorage) -> None:
    """★ THE REGRESSION: a HEAD match stopped meaning "preserved" once the agent stopped
    committing as it worked.
    Mutation check: drop `container_dirty` from the head-match arm in `durable_copy.py` and this
    goes red."""
    await _put_saved(store, HEAD)

    verdict = await confirm_durable_copy(APP, container_head=HEAD, container_dirty=True)

    assert verdict.state is CopyState.STALE
    assert verdict.may_destroy is False
    # The reason must name the TREE, not the head — "behind HEAD" over a matching head would read
    # as the gate being broken.
    assert "uncommitted" in verdict.reason


async def test_a_matching_head_on_an_unread_tree_spares_rather_than_guesses(
    store: FakeStorage,
) -> None:
    """An unestablished fact on a path that authorises destruction must spare. `None` is
    deliberately NOT collapsed into `False` — a default that reads "clean" is the permissive
    shape the regression above came from."""
    await _put_saved(store, HEAD)

    verdict = await confirm_durable_copy(APP, container_head=HEAD, container_dirty=None)

    assert verdict.state is CopyState.UNCONFIRMED
    assert verdict.may_destroy is False


async def test_a_clean_tree_at_a_matching_head_is_still_collected(store: FakeStorage) -> None:
    """THE OTHER HALF: a gate that spares everything forever is as broken as one that destroys
    live work — it collects nothing and the fleet bills forever. The benign case must still
    authorise."""
    await _put_saved(store, HEAD)

    verdict = await confirm_durable_copy(APP, container_head=HEAD, container_dirty=False)

    assert verdict.state is CopyState.CONFIRMED_CURRENT
    assert verdict.may_destroy is True


async def test_currency_is_the_sha_not_the_timestamp(store: FakeStorage) -> None:
    """Azure stamps `last_modified` in WHOLE SECONDS, so a Save and an autosave inside one second
    are indistinguishable by time. The comparison is therefore the sha: a fresh blob whose sha is
    stale still reads STALE."""
    await _put_saved(store, OLDER)
    # As freshly written as anything can be; the clock says current, the content does not.
    assert store.mtimes[snapshot_key(APP)] is not None

    assert (
        await confirm_durable_copy(APP, container_head=HEAD, container_dirty=False)
    ).state is CopyState.STALE


async def test_the_recovery_slot_is_not_a_substitute_for_the_saved_copy(
    store: FakeStorage,
) -> None:
    """THE SLOT IS THE SAVED COPY, and this test pins which one because the file it lives in once
    pinned the other. A shutdown writes the container's tree back over `snapshot_key` through the
    ancestry guard, and every copy this path takes lands there — so a gate reading `recovery_key`
    would be satisfied by a slot nothing on the destroy path maintains, and could never be
    satisfied by the copy the caller just took. One question, one slot.

    Mutation check: point `confirm_durable_copy` back at `recovery_key` and this goes red."""
    await store.put(recovery_key(APP), a_git_bundle(HEAD), metadata={"head_sha": HEAD})

    verdict = await confirm_durable_copy(APP, container_head=HEAD, container_dirty=False)

    assert verdict.state is CopyState.UNCONFIRMED
    assert verdict.may_destroy is False


# --- everything unreadable spares -------------------------------------------------


async def test_a_storage_off_deployment_cannot_authorise_a_single_delete(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """THE FLEET-DELETING MISCONFIGURATION: `manager.py::head_presence` reads
    `StorageUnconfiguredError` as a CONFIRMED absent bundle, correct for its build-path caller.
    Consumed here that same value would mean "no work to preserve" for every container at once.
    Mutation-check: make the `StorageUnconfiguredError` arm return CONFIRMED_CURRENT and this is
    the single test that goes red."""

    def _no_store() -> object:
        raise StorageUnconfiguredError("no OBJECT_STORE__ block on this deployment")

    monkeypatch.setattr(durable_copy, "get_storage", _no_store)

    verdict = await confirm_durable_copy(APP, container_head=HEAD, container_dirty=False)

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

    assert (
        await confirm_durable_copy(APP, container_head=HEAD, container_dirty=False)
    ).may_destroy is False


async def test_a_bundle_with_no_stamped_sha_cannot_be_compared(store: FakeStorage) -> None:
    """Older bundles predate the metadata stamp. A copy whose head is unknown is a signal that
    could not be read, and every one of those escalates."""
    await store.put(snapshot_key(APP), a_git_bundle(HEAD), metadata={})

    assert (
        await confirm_durable_copy(APP, container_head=HEAD, container_dirty=False)
    ).state is CopyState.UNCONFIRMED


async def test_no_saved_copy_at_all_is_unconfirmed_not_permission(store: FakeStorage) -> None:
    """The most tempting wrong answer in the whole unit: "there is no copy, so there is nothing to
    preserve". There is no copy, so there is nothing to preserve it WITH."""
    assert (
        await confirm_durable_copy(APP, container_head=HEAD, container_dirty=False)
    ).may_destroy is False


# --- the unreachable-container fallback -------------------------------------------


async def test_an_unreachable_container_falls_back_to_a_parseable_bundle(
    store: FakeStorage,
) -> None:
    """An orphan has no registry record and may not answer at all, so requiring a live HEAD
    comparison on this branch would spare every genuinely-dead container forever and collect
    nothing. A present, parseable bundle stands in instead."""
    await _put_saved(store, HEAD)

    verdict = await confirm_durable_copy(APP, container_head=None, container_dirty=None)

    assert verdict.state is CopyState.CONFIRMED_CURRENT


async def test_an_unreachable_container_with_no_bundle_still_escalates(
    store: FakeStorage,
) -> None:
    """The fallback is a fallback, not a bypass: no bundle and no container means nothing was
    established, and nothing established never authorises a delete."""
    assert (
        await confirm_durable_copy(APP, container_head=None, container_dirty=None)
    ).may_destroy is False


# --- reap_user is gated too, which it never was ------------------------------------


async def _register(redis: aioredis.Redis) -> None:
    await redis.hset(
        registry_key(USER),
        mapping={
            REGISTRY_FIELD_APP_NAME: a_sandbox_name("x"),
            REGISTRY_FIELD_FQDN: f"{a_sandbox_name('x')}.example.io",
            REGISTRY_FIELD_STATE: "ready",
        },
    )


async def test_reap_user_refuses_when_the_copy_cannot_be_confirmed(
    fake_redis: aioredis.Redis, store: FakeStorage
) -> None:
    """`reap_user` used to call `sandbox_client.teardown` with no durable-copy check at all, on
    the path that does almost all of the deleting — a gate added only to the orphan path would
    have left this, the common case, exactly as it was."""
    await _register(fake_redis)
    client = FakeSandboxClient()

    reaped = await reap_user(fake_redis, USER, client, app_id=APP)

    assert reaped is False
    assert client.torn_down == []
    # Spared AND reported: the lock and registry stay so a later pass retries once a copy exists.
    assert await fake_redis.exists(registry_key(USER)) == 1


async def test_reap_user_proceeds_once_the_copy_is_confirmed(
    fake_redis: aioredis.Redis, store: FakeStorage
) -> None:
    await _register(fake_redis)
    await _put_saved(store, HEAD)
    client = FakeSandboxClient()

    reaped = await reap_user(fake_redis, USER, client, app_id=APP)

    assert reaped is True
    assert client.torn_down == [a_sandbox_name("x")]


# --- and it compares against a REAL head ------------------------------------------


def _reachable(client: FakeSandboxClient, *, head: str) -> None:
    """Make the fake container attachable AND answerable, which is the state the comparison
    needs. A fake with no `attach_handle` raises `SandboxGoneError` by default — which is why the
    hardcoded `None` this file's later tests replaced went unnoticed for so long: every test here
    that predates them drives only the unreachable branch."""
    client.attach_handle = SandboxHandle(
        fqdn=f"{a_sandbox_name('x')}.example.io",
        token="tok",
        app_name=a_sandbox_name("x"),
        preview_url=f"https://{a_sandbox_name('x')}.example.io/",
        ready=True,
    )
    # `state_script`'s four `@@`-separated fields: HEAD, a clean porcelain, the commit count.
    client.exec_handler = lambda _cmd: ExecResult(stdout=f"{head}\n@@\n@@\n1\n", stderr="", exit=0)


async def test_a_reachable_container_is_compared_against_its_real_head(
    fake_redis: aioredis.Redis, store: FakeStorage
) -> None:
    """THE COMPARISON THIS GATE IS NAMED FOR, WHICH NEVER ONCE RAN: `reap_user` passed a
    hardcoded `container_head=None`, so the "could not read the container, trust the bundle"
    fallback was the only reachable branch, and the STALE verdict was dead code.
    Mutation-check: put `container_head=None` back and this goes red — the fallback fires, the
    verdict is CONFIRMED_CURRENT, and the container with the uncopied work is torn down."""
    await _register(fake_redis)
    await _put_saved(store, OLDER)  # the copy is BEHIND the container
    client = FakeSandboxClient()
    _reachable(client, head=HEAD)

    reaped = await reap_user(fake_redis, USER, client, app_id=APP)

    assert reaped is False
    assert client.torn_down == []
    assert await fake_redis.exists(registry_key(USER)) == 1


async def test_a_reachable_container_whose_copy_matches_is_still_reaped(
    fake_redis: aioredis.Redis, store: FakeStorage
) -> None:
    """The other direction of the same comparison, so "reads the head" cannot be satisfied by a
    gate that simply refuses everything reachable."""
    await _register(fake_redis)
    await _put_saved(store, HEAD)
    client = FakeSandboxClient()
    _reachable(client, head=HEAD)

    assert await reap_user(fake_redis, USER, client, app_id=APP) is True
    assert client.torn_down == [a_sandbox_name("x")]


async def test_a_caller_that_passes_no_app_id_is_unchanged(
    fake_redis: aioredis.Redis, store: FakeStorage
) -> None:
    """Reconcile-on-start and the sweep reap a user's OWN stale state, where the builder is about
    to be handed a fresh container anyway, so they stay byte-identical. Only the scheduled janitor
    — the process with no human watching it — passes the id and is gated."""
    await _register(fake_redis)
    client = FakeSandboxClient()

    assert await reap_user(fake_redis, USER, client) is True
    assert client.torn_down == [a_sandbox_name("x")]


# =============================================================================
# The copy is TAKEN, not merely found missing
# =============================================================================
#
# The contract: if the newest durable copy predates the newest change, a copy is taken BEFORE
# the container is reclaimed. Both call sites used to read the STALE verdict, log it, and spare
# — forever — which let an autosave failure bill a dead container indefinitely with nothing but a
# repeating log line to show for it.
#
# These tests use the SINGLETON store (`fake_storage`) rather than the `store` fixture above,
# because `snapshot.py` resolves the store through the accessor and the local fixture only
# rebinds `durable_copy`'s name for it. Two fakes would mean the gate reads one store and the
# write lands in another — every assertion below would pass against a copy nobody could restore.


def _bundles[Client: FakeSandboxClient](
    client: Client,
    *,
    head: str,
    bundles_to: str,
    ancestry: str = "0 0",
    attaches_as: str | None = None,
    porcelain: str = "",
) -> Client:
    """A container that attaches AND answers the whole snapshot ladder: commit, bundle, base64.

    `_reachable` above stops at the state probe, which was enough while the gate only ever
    compared a sha against blob metadata. A client that answered the state probe to EVERY command
    would hand `base64` its own porcelain, which fails to decode, takes the sparing arm, and
    passes for the wrong reason — so the handler discriminates by `cmd[0]`.
    `attaches_as` exists for one test: the registry can name a different container by the time we
    attach, and the copy must refuse rather than bundle somebody else's tree into this slot.
    `porcelain` is what `git status` reports, and it is the input the two churn predicates
    disagree about — an empty one can never tell them apart."""
    name = attaches_as or a_sandbox_name("x")
    client.attach_handle = SandboxHandle(
        fqdn=f"{name}.example.io",
        token="tok",
        app_name=name,
        preview_url=f"https://{name}.example.io/",
        ready=True,
    )
    bundle = base64.b64encode(a_git_bundle(bundles_to)).decode()

    def handler(cmd: list[str]) -> ExecResult:
        if cmd[0] == "sh" and "rev-parse" in cmd[-1]:
            # The ancestry field answers only when the probe ASKED — an unasked question must
            # stay distinguishable from a judgement (`Ancestry.NOT_ASKED`).
            answered = ancestry if "merge-base" in cmd[-1] else ""
            return ExecResult(stdout=f"{head}@@{porcelain}@@4@@{answered}", stderr="", exit=0)
        if cmd[0] == "base64":
            return ExecResult(stdout=bundle, stderr="", exit=0)
        return ExecResult(stdout="", stderr="", exit=0)

    client.exec_handler = handler
    return client


class _ReadsTheSlotAtTeardown(FakeSandboxClient):
    """Records what the saved copy held AT THE MOMENT teardown was called.

    Order is the promise, not just the pair of facts: a copy is taken BEFORE the container is
    reclaimed, and a test that only checks the slot afterwards would pass just as happily against
    an implementation that tears the container down first and bundles from a corpse."""

    def __init__(self, store: FakeStorage) -> None:
        super().__init__()
        self._store = store
        self.slot_at_teardown: str | None = None

    async def teardown(self, handle: SandboxHandle) -> None:
        meta = await self._store.head(snapshot_key(APP))
        self.slot_at_teardown = (meta.metadata or {}).get("head_sha") if meta else None
        await super().teardown(handle)


async def test_a_copy_that_predates_the_newest_change_is_taken_before_the_reap(
    fake_redis: aioredis.Redis, fake_storage: FakeStorage, attempts: list[CopyAttempt]
) -> None:
    """★ THE UNIT: the saved copy is behind the container, and the STALE verdict resolves by
    TAKING a copy — before this fix the container was spared forever instead. Deleting this test
    loses the only proof a stale-copy container is ever collected at all.
    Mutation check: put `if not verdict.may_destroy: return False` back in `reap_user` and this
    goes red — nothing is torn down and the slot still holds the older tree."""
    await _register(fake_redis)
    await _put_saved(fake_storage, OLDER)
    client = _bundles(_ReadsTheSlotAtTeardown(fake_storage), head=HEAD, bundles_to=BUNDLED)

    assert await reap_user(fake_redis, USER, client, app_id=APP) is True

    # THE ORDER, not just the outcome: the slot already held this turn's tree when the delete ran.
    assert client.slot_at_teardown == BUNDLED
    assert client.torn_down == [a_sandbox_name("x")]
    assert attempts == [CopyAttempt.COPIED]


async def test_a_copy_that_will_not_take_spares_the_container_and_records_why(
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
    attempts: list[CopyAttempt],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The store refuses the upload. Taking a copy is what the contract asks for; succeeding at
    it is not something this code can promise, so this must land back on the pre-existing
    behaviour (spare, never destroy) — and leave a RECORD, since sparing quietly is how the leak
    stayed invisible for as long as it did.
    Mutation check: return True from the `except` arm in `_take_the_copy_we_promised` → red."""
    await _register(fake_redis)
    await _put_saved(fake_storage, OLDER)
    client = _bundles(FakeSandboxClient(), head=HEAD, bundles_to=BUNDLED)

    async def _the_store_says_no(*_a: object, **_k: object) -> None:
        raise StorageError("blob unreachable", provider="fake", key=snapshot_key(APP))

    monkeypatch.setattr(fake_storage, "put", _the_store_says_no)

    assert await reap_user(fake_redis, USER, client, app_id=APP) is False
    assert client.torn_down == []
    assert await fake_redis.exists(registry_key(USER)) == 1, "state stays for a later pass"
    assert attempts == [CopyAttempt.FAILED]


async def test_a_tree_that_fails_the_lineage_guard_diverts_and_spares_rather_than_clobbering(
    fake_redis: aioredis.Redis, fake_storage: FakeStorage, attempts: list[CopyAttempt]
) -> None:
    """★ THE REASON THE FIX GOES THROUGH THE GUARDED WRITE AND NOT A RAW `put`: the container's
    HEAD is not a descendant of the copy on record (a reverted or re-initialised workspace), so
    simply taking a copy here would stamp that tree in as the newest and destroy the container on
    the strength of it — the loss performed by the code written to prevent it.
    Mutation check: read `SavedCopyOutcome.DIVERTED` as authorising (return True) → red."""
    await _register(fake_redis)
    await _put_saved(fake_storage, OLDER)
    good_bundle = fake_storage.objects[snapshot_key(APP)]
    client = _bundles(FakeSandboxClient(), head=HEAD, bundles_to=BUNDLED, ancestry="0 1")

    with structlog.testing.capture_logs() as logs:
        assert await reap_user(fake_redis, USER, client, app_id=APP) is False

    assert client.torn_down == []
    assert fake_storage.objects[snapshot_key(APP)] == good_bundle, "byte-identical, not clobbered"
    assert [k for k in fake_storage.objects if k.startswith(divert_prefix(APP))], (
        "the refused tree is preserved, not thrown away"
    )
    assert any(e.get("event") == SAVED_COPY_WRITE_DID_NOT_LAND_EVENT for e in logs)
    assert attempts == [CopyAttempt.REFUSED]


async def test_a_current_copy_is_reclaimed_without_taking_a_second_one(
    fake_redis: aioredis.Redis, fake_storage: FakeStorage, attempts: list[CopyAttempt]
) -> None:
    """The other direction, so "takes a copy" cannot be satisfied by a reaper that bundles every
    container it looks at — a second copy would cost an exec, a bundle and an upload per
    container per pass, on the path that walks the entire fleet every fifteen minutes.
    Mutation check: drop the `verdict.may_destroy` early-out from `_take_the_copy_we_promised` and
    this goes red — the slot is re-stamped with a tree nobody asked for."""
    await _register(fake_redis)
    await _put_saved(fake_storage, HEAD)
    written_at = fake_storage.mtimes[snapshot_key(APP)]
    client = _bundles(FakeSandboxClient(), head=HEAD, bundles_to=BUNDLED)

    assert await reap_user(fake_redis, USER, client, app_id=APP) is True

    assert client.torn_down == [a_sandbox_name("x")]
    assert fake_storage.mtimes[snapshot_key(APP)] == written_at, "nothing was re-uploaded"
    assert attempts == [CopyAttempt.NOTHING_TO_COPY]


async def test_a_container_that_will_not_attach_is_spared_with_a_record_not_in_silence(
    fake_redis: aioredis.Redis, fake_storage: FakeStorage, attempts: list[CopyAttempt]
) -> None:
    """There is no copy and no way to take one, so the pre-existing sparing stands — but not in
    silence: the record is the only thing an operator can look for that doesn't depend on the
    failing component to announce itself.
    Mutation check: drop the `record_durable_copy_attempt` call from the unreachable arm and this
    goes red while every other assertion in the file stays green — exactly the state the two call
    sites were already in."""
    await _register(fake_redis)
    client = FakeSandboxClient()  # no `attach_handle`: `attach_existing` raises SandboxGoneError

    assert await reap_user(fake_redis, USER, client, app_id=APP) is False
    assert client.torn_down == []
    assert attempts == [CopyAttempt.UNREACHABLE]


async def test_a_copy_is_never_taken_from_a_container_the_record_no_longer_names(
    fake_redis: aioredis.Redis, fake_storage: FakeStorage, attempts: list[CopyAttempt]
) -> None:
    """`attach_existing` builds its handle from the registry record, and the record is the one
    input on this path that can change underneath us — a builder starting a fresh sandbox between
    the record read and the attach hands us their LIVE container. Bundling that tree into this
    app's saved copy would overwrite one app's only copy with another app's work.
    Mutation check: drop the `reached.handle.app_name != expected_name` guard and this goes red —
    the other container's tree is bundled straight over the copy on record."""
    await _register(fake_redis)  # the registry names sbx-x
    await _put_saved(fake_storage, OLDER)
    on_record = fake_storage.objects[snapshot_key(APP)]
    client = _bundles(
        FakeSandboxClient(),
        head=HEAD,
        bundles_to=BUNDLED,
        attaches_as=a_sandbox_name("someone-else"),
    )

    assert await reap_user(fake_redis, USER, client, app_id=APP) is False
    assert client.torn_down == []
    assert fake_storage.objects[snapshot_key(APP)] == on_record
    assert attempts == [CopyAttempt.UNREACHABLE]


def test_every_copy_attempt_carries_an_explanation_an_operator_can_act_on() -> None:
    """A row saying a container was spared, with no `detail` and no outcome mapped to it, is a row
    nobody can act on — and worse, an unmapped member raises `KeyError` inside
    `record_durable_copy_attempt`'s swallow, so the record vanishes and the sparing goes silent
    again.
    Mutation check: add a member to `CopyAttempt` without a `_ATTEMPT_MEANING` row and this goes
    red."""
    assert set(_ATTEMPT_MEANING) == set(CopyAttempt)
    assert all(detail.strip() for _, detail in _ATTEMPT_MEANING.values())


# --- and the record genuinely reaches Postgres -------------------------------------


@pytest.fixture
def copy_record_writes_here(  # noqa: ANN201
    db_session: AsyncSession, monkeypatch: pytest.MonkeyPatch
):
    """Point the REAL `record_durable_copy_attempt` at this test's connection.

    The production function opens its OWN session, because the row must land even when the reap
    it describes has just failed — which makes it invisible to a suite running inside one
    rolled-back transaction. The factory is rebound to hand back THIS connection's session with
    `commit` neutered: committing the harness transaction would leak rows into every later test.
    `committed` is not bookkeeping — autoflush means a bare `add()` is visible to the very next
    SELECT, so a writer that forgot to commit would read as healthy here while writing nothing."""
    import src.db.base as db_base

    class _NoCommitSession:
        def __init__(self, inner: AsyncSession) -> None:
            self._inner = inner
            self.committed = False

        async def __aenter__(self) -> _NoCommitSession:
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

        def add(self, instance: object) -> None:
            self._inner.add(instance)

        async def commit(self) -> None:
            self.committed = True
            await self._inner.flush()

    sessions: list[_NoCommitSession] = []

    def _factory() -> _NoCommitSession:
        sessions.append(_NoCommitSession(db_session))
        return sessions[-1]

    monkeypatch.setattr(db_base, "async_session_factory", _factory)
    return db_session, sessions


async def test_the_copy_record_reaches_the_database_and_is_committed(
    copy_record_writes_here: tuple[AsyncSession, Sequence[object]],
) -> None:
    """THE ONE TEST THAT RUNS THE REAL WRITER. Everything above spies on it, so without this the
    whole "a spared container is visible" claim rests on a list in a test file.
    Mutation check: drop the `await db.commit()` and this goes red on the last assertion — the row
    is still visible in here through autoflush, exactly how a writer that persists nothing in
    production reads as healthy in a suite."""
    db, sessions = copy_record_writes_here

    await record_durable_copy_attempt(CopyAttempt.UNREACHABLE)

    rows = list((await db.execute(sa.select(WorkerPass))).scalars())
    assert len(rows) == 1
    assert rows[0].task_name == pass_history.DURABLE_COPY_TASK_NAME
    assert rows[0].outcome is PassOutcome.DECLINED
    assert rows[0].counts == {"copied": 0, "spared": 1}
    assert rows[0].detail
    assert all(getattr(session, "committed", False) for session in sessions)


async def test_a_copy_record_never_makes_a_dead_reclamation_worker_look_alive(
    copy_record_writes_here: tuple[AsyncSession, Sequence[object]],
) -> None:
    """`reclamation_pass_freshness` reads the single newest row for `RECLAMATION_TASK_NAME` and
    pronounces the scheduler alive on the strength of it — the only detector of a dead worker in
    the system, since a crashlooping scheduler emits no alarms of its own. Filing a per-container
    copy attempt under the pass's own name would keep that detector permanently satisfied by a
    different subsystem.
    Mutation check: set `DURABLE_COPY_TASK_NAME = RECLAMATION_TASK_NAME` and this goes red."""
    db, _ = copy_record_writes_here
    before = await reclamation_pass_freshness(db)

    await record_durable_copy_attempt(CopyAttempt.COPIED)

    assert await reclamation_pass_freshness(db) == before


# =============================================================================
# The promotion nobody can compare — the path an adversarial review reproduced
# =============================================================================


async def test_an_uncomparable_copy_on_record_is_never_overwritten_and_never_authorises_a_reap(
    fake_redis: aioredis.Redis, fake_storage: FakeStorage, attempts: list[CopyAttempt]
) -> None:
    """★★ THE VERIFIED DATA-LOSS PATH: the saved copy holds a bundle written before the head
    stamp existed, so `confirm_durable_copy` cannot compare and returns UNCONFIRMED, and a copy is
    then taken. Read "no head to compare against" as "nothing to protect" and the reverted tree
    goes over the user's only durable copy, and that write is then read as proof and the container
    deleted.
    Mutation check: promote when `recorded` is unreadable (`snapshot.py`) → red."""
    await _put_saved(fake_storage, None)  # present, but carrying no head_sha
    before = await fake_storage.get(snapshot_key(APP))
    await _register(fake_redis)
    client = _bundles(FakeSandboxClient(), head=REVERTED, bundles_to=REVERTED, ancestry="0 1")

    reaped = await reap_user(fake_redis, USER, client, app_id=APP)

    assert reaped is False, "an unverifiable copy must never authorise a destroy"
    assert client.torn_down == []
    assert await fake_storage.get(snapshot_key(APP)) == before
    assert attempts == [CopyAttempt.REFUSED]


async def test_a_first_write_is_refused_and_parked_rather_than_taken_in_silence(
    fake_redis: aioredis.Redis, fake_storage: FakeStorage, attempts: list[CopyAttempt]
) -> None:
    """★★ AN EMPTY SLOT IS NOT PROOF, and this is the arm where that costs the most. An app with
    no saved copy and a container that has reverted to its baked image present identically: no
    head on record, nothing to compare against. Promote there and the template tree becomes the
    app's newest durable copy, which an automatic restore then hands back over the real work — the
    loss performed by the guard written to prevent it.

    The bytes are still not thrown away: a false refusal would otherwise discard the newest copy
    of somebody's afternoon, so they are parked where an operator can promote them.
    Mutation check: write the saved copy when `recorded is None` and this goes red."""
    await _register(fake_redis)
    client = _bundles(FakeSandboxClient(), head=REVERTED, bundles_to=REVERTED, ancestry="0 1")

    reaped = await reap_user(fake_redis, USER, client, app_id=APP)

    assert reaped is False
    assert client.torn_down == []
    assert await fake_storage.head(snapshot_key(APP)) is None, "no saved copy was invented"
    assert [k for k in fake_storage.objects if k.startswith(divert_prefix(APP))], (
        "the refused tree is parked, not thrown away"
    )
    assert attempts == [CopyAttempt.REFUSED]


async def test_a_destroy_on_the_unreadable_container_fallback_says_so(
    fake_redis: aioredis.Redis, fake_storage: FakeStorage, attempts: list[CopyAttempt]
) -> None:
    """`may_destroy` is True for two different facts, and only one of them is "already current" —
    the other is the deliberate fallback where the container could not be read and a present,
    parseable bundle stands in. Recording that as "the durable copy was already current" writes
    the one row an operator would use to find "we destroyed containers we could not verify" and
    makes it say the opposite.
    Mutation check: record `NOTHING_TO_COPY` unconditionally and this goes red."""
    await _register(fake_redis)
    await _put_saved(fake_storage, HEAD)
    client = FakeSandboxClient()  # attaches, but its state probe answers nothing usable

    assert await reap_user(fake_redis, USER, client, app_id=APP) is True
    assert attempts == [CopyAttempt.UNVERIFIED_FALLBACK]


def test_every_copy_attempt_still_maps_to_an_operator_sentence() -> None:
    """The table is the operator's whole vocabulary; a member without a row is a row that
    vanishes."""
    from src.services.build_sessions.pass_history import _ATTEMPT_MEANING

    assert set(_ATTEMPT_MEANING) == set(CopyAttempt)


# =============================================================================
# The guarded promotion itself
# =============================================================================
#
# Everything above reaches the guard through a reap, which is the caller that exists today. The
# shutdown routine calls it directly and with nobody watching, so the arms are pinned here at the
# function that decides them rather than only through the caller that carries the decision on.


def _the_handle() -> SandboxHandle:
    return SandboxHandle(
        fqdn=f"{a_sandbox_name('x')}.example.io",
        token="tok",
        app_name=a_sandbox_name("x"),
        preview_url=f"https://{a_sandbox_name('x')}.example.io/",
        ready=True,
    )


async def _guarded_write(client: FakeSandboxClient) -> SavedCopyWrite:
    return await write_saved_copy_under_guard(
        client, _the_handle(), APP, taken_at=datetime.now(UTC)
    )


async def test_a_dirty_descendant_tree_lands_in_the_saved_copy(
    fake_storage: FakeStorage,
) -> None:
    """The ordinary shutdown: the citizen worked, the tree descends from what they saved, and the
    copy they reopen has to be that work rather than the one they last clicked Save on.
    Mutation check: return `SKIPPED` from the descendant arm and this goes red — the container is
    destroyed and the afternoon goes with it."""
    await _put_saved(fake_storage, OLDER)
    client = _bundles(FakeSandboxClient(), head=HEAD, bundles_to=BUNDLED, porcelain=" M page.tsx")

    written = await _guarded_write(client)

    assert written.outcome is SavedCopyOutcome.WRITTEN
    assert written.recorded_head == OLDER
    assert written.bundled_head == BUNDLED
    # What a reopen reads back: the bundle itself, stamped with the tree it carries. A key holding
    # the right bytes under the wrong stamp restores as an older tree at the next comparison.
    assert fake_storage.objects[snapshot_key(APP)] == a_git_bundle(BUNDLED)
    meta = await fake_storage.head(snapshot_key(APP))
    assert meta is not None and (meta.metadata or {})["head_sha"] == BUNDLED


async def test_a_clean_tree_at_the_saved_head_writes_nothing_at_all(
    fake_storage: FakeStorage,
) -> None:
    """A shutdown runs on every departure, and most of them have nothing to store. Bundling anyway
    would cost four execs, a base64 of the whole tree and an upload on every one — and would
    re-stamp a copy nobody changed, making `last_modified` lie about when the work was done.
    Mutation check: drop the `SKIPPED` arm and this goes red on the untouched mtime."""
    await _put_saved(fake_storage, HEAD)
    written_at = fake_storage.mtimes[snapshot_key(APP)]
    client = _bundles(FakeSandboxClient(), head=HEAD, bundles_to=BUNDLED)

    written = await _guarded_write(client)

    assert written.outcome is SavedCopyOutcome.SKIPPED
    assert written.bundled_head is None, "nothing was bundled"
    assert fake_storage.mtimes[snapshot_key(APP)] == written_at


async def test_a_tree_changed_only_where_the_agent_may_edit_is_still_written(
    fake_storage: FakeStorage,
) -> None:
    """★ THE SAVE SIDE MUST NOT FORGIVE FRAMEWORK CHURN. `tsconfig.json` is rewritten by `next
    dev` AND named editable in the agent's own prompt, so the reap side forgives it and this side
    must not: a path alias the agent added is the citizen's change, and forgiving it here writes
    nothing and then destroys the container holding it.
    Mutation check: swap `holds_unsaved_work` for `clean_but_for_churn` and this goes red while
    every other arm stays green — which is exactly how it would ship."""
    await _put_saved(fake_storage, HEAD)
    client = _bundles(
        FakeSandboxClient(), head=HEAD, bundles_to=BUNDLED, porcelain=" M tsconfig.json"
    )

    written = await _guarded_write(client)

    assert written.outcome is SavedCopyOutcome.WRITTEN
    assert fake_storage.objects[snapshot_key(APP)] == a_git_bundle(BUNDLED)


async def test_a_tree_changed_only_where_the_framework_writes_is_not_written(
    fake_storage: FakeStorage,
) -> None:
    """The other side of the same predicate, so "writes when dirty" cannot be satisfied by a guard
    that bundles every container it looks at. Next regenerates `next-env.d.ts` on boot, the file
    carries its own do-not-edit banner, and nothing the agent is permitted to do touches it.
    Mutation check: read `uncommitted` raw and this goes red — every merely-started container
    pays for a bundle and an upload it had no work to justify."""
    await _put_saved(fake_storage, HEAD)
    client = _bundles(
        FakeSandboxClient(), head=HEAD, bundles_to=BUNDLED, porcelain=" M next-env.d.ts"
    )

    assert (await _guarded_write(client)).outcome is SavedCopyOutcome.SKIPPED


async def test_a_tree_that_is_not_a_descendant_is_parked_and_the_saved_copy_stands(
    fake_storage: FakeStorage,
) -> None:
    """★ THE WHOLE POINT OF THE UNIT, at the function that decides it. `git reset --hard`, a
    re-initialised repository and a container reverted to its baked image all present as a tree
    the saved copy is not an ancestor of — and this path ends in an ARM delete, so promoting one
    would replace the citizen's work with whatever the container happens to hold now.
    Mutation check: accept any ancestry short of `UNREADABLE` and this goes red."""
    await _put_saved(fake_storage, OLDER)
    on_record = fake_storage.objects[snapshot_key(APP)]
    client = _bundles(FakeSandboxClient(), head=REVERTED, bundles_to=REVERTED, ancestry="0 1")

    with structlog.testing.capture_logs() as logs:
        written = await _guarded_write(client)

    assert written.outcome is SavedCopyOutcome.DIVERTED
    assert written.diverted_to is not None
    assert fake_storage.objects[snapshot_key(APP)] == on_record, "byte-identical, not clobbered"
    assert fake_storage.objects[written.diverted_to] == a_git_bundle(REVERTED), (
        "the refused tree is parked BEFORE the refusal is returned, not left in a dying container"
    )
    assert any(e.get("event") == SAVED_COPY_WRITE_DID_NOT_LAND_EVENT for e in logs)


async def test_a_container_that_will_not_answer_raises_rather_than_reporting_an_outcome(
    fake_storage: FakeStorage,
) -> None:
    """An unestablished fact on a path that ends in an ARM delete is not an outcome to return: a
    caller pattern-matching on three members would have to remember that a fourth means "we could
    not tell", and the one that forgets destroys a container nobody read.
    Mutation check: return an outcome instead of raising and this goes red."""
    await _put_saved(fake_storage, OLDER)
    client = FakeSandboxClient()
    client.exec_handler = lambda _cmd: ExecResult(stdout="", stderr="no", exit=1)

    with pytest.raises(SandboxError):
        await _guarded_write(client)

    assert fake_storage.objects[snapshot_key(APP)] == a_git_bundle(OLDER)


# --- and the janitor's call site gates against the same copy -----------------------


async def test_the_janitor_gates_against_the_saved_copy_and_spares_when_it_cannot_confirm(
    fake_redis: aioredis.Redis, fake_storage: FakeStorage, attempts: list[CopyAttempt]
) -> None:
    """The second call site, keyed by container name rather than by user. It reads the same slot —
    a janitor gating against a copy nothing writes would spare the whole fleet forever — and an
    unreachable container still spares, because a timeout is not a death certificate.
    Mutation check: seed the recovery slot instead and the first half goes red."""
    await _register(fake_redis)
    await _put_saved(fake_storage, OLDER)
    client = _bundles(FakeSandboxClient(), head=HEAD, bundles_to=BUNDLED)

    assert await reap_the_container_we_judged(
        fake_redis, client, app_name=a_sandbox_name("x"), user_uuid=USER, app_id=APP
    )
    meta = await fake_storage.head(snapshot_key(APP))
    assert meta is not None and (meta.metadata or {})["head_sha"] == BUNDLED
    assert client.torn_down == [a_sandbox_name("x")]
    assert attempts == [CopyAttempt.COPIED]


async def test_the_janitor_spares_the_container_it_cannot_take_a_copy_from(
    fake_redis: aioredis.Redis, fake_storage: FakeStorage, attempts: list[CopyAttempt]
) -> None:
    """The sparing half of the same call site: no saved copy to compare against, and a tree that
    cannot be shown to descend from one. Nothing was established, so nothing is destroyed.
    Mutation check: authorise the destroy on a refusal and this goes red — the janitor collects a
    container whose only copy of the work is the one it is about to delete."""
    await _register(fake_redis)
    client = _bundles(FakeSandboxClient(), head=REVERTED, bundles_to=REVERTED, ancestry="0 1")

    assert (
        await reap_the_container_we_judged(
            fake_redis, client, app_name=a_sandbox_name("x"), user_uuid=USER, app_id=APP
        )
        is False
    )
    assert client.torn_down == []
    assert await fake_storage.head(snapshot_key(APP)) is None
    assert attempts == [CopyAttempt.REFUSED]


async def test_a_saved_copy_stamped_with_something_that_is_not_a_sha_is_never_promoted_over(
    fake_storage: FakeStorage,
) -> None:
    """The stamp is a value the platform wrote and the STORE returned, and the ancestry question
    is composed into a shell string — so a stamp that is not sha-shaped never reaches the shell,
    and a comparison that could not be made is not a comparison that passed.
    Mutation check: pass `recorded` through to the probe unchecked and this goes red."""
    await fake_storage.put(
        snapshot_key(APP), a_git_bundle(HEAD), metadata={"head_sha": "; rm -rf /"}
    )
    on_record = fake_storage.objects[snapshot_key(APP)]
    client = _bundles(FakeSandboxClient(), head=HEAD, bundles_to=BUNDLED)

    written = await _guarded_write(client)

    assert written.outcome is SavedCopyOutcome.DIVERTED
    assert written.recorded_head == "; rm -rf /", (
        "the alarm still names the stamp to go and look at"
    )
    assert fake_storage.objects[snapshot_key(APP)] == on_record
