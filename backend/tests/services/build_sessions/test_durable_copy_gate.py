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

import base64
import uuid
from collections.abc import Sequence

import pytest
import redis.asyncio as aioredis
import sqlalchemy as sa
import structlog.testing
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models.worker_pass import PassOutcome, WorkerPass
from src.services.build_sessions import durable_copy, pass_history
from src.services.build_sessions.alarms import RECOVERY_WRITE_DID_NOT_LAND_EVENT
from src.services.build_sessions.durable_copy import CopyState, confirm_durable_copy
from src.services.build_sessions.pass_history import (
    _ATTEMPT_MEANING,
    CopyAttempt,
    reclamation_pass_freshness,
    record_durable_copy_attempt,
)
from src.services.build_sessions.reaper import reap_user
from src.services.build_sessions.snapshot import reset_divert_streaks_for_tests
from src.services.redis import registry_key
from src.services.redis.keys import (
    REGISTRY_FIELD_APP_NAME,
    REGISTRY_FIELD_FQDN,
    REGISTRY_FIELD_STATE,
)
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
#: What the commit step inside `write_recovery_copy` turns the working tree into. Distinct from
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
    """Every copy-before-reclaim outcome this test recorded, WITHOUT touching the database.

    AUTOUSE, AND THAT IS NOT CONVENIENCE. `record_durable_copy_attempt` opens its own session and
    COMMITS — it has to, because in production the row must land even when the reap it describes
    has just failed. Inside a suite that is otherwise one rolled-back transaction, that means
    every gated reap driven from this file would leave a permanent row in the SHARED test
    database, and `test_reclamation_report_only.py::test_a_zero_candidate_pass_still_writes_a_
    record` counts every row in that table — so unguarded, this file breaks a test in another one,
    on every run, forever. `test_the_copy_record_reaches_the_database_and_is_committed` below is
    the single place the real writer runs, and it runs against a connection that rolls back."""
    recorded: list[CopyAttempt] = []

    async def _spy(attempt: CopyAttempt) -> None:
        recorded.append(attempt)

    monkeypatch.setattr(pass_history, "record_durable_copy_attempt", _spy)
    return recorded


@pytest.fixture(autouse=True)
def _forget_the_divert_streak() -> None:
    """The refusal counter behind U2's escalation is PROCESS-LOCAL, so a divert driven here would
    otherwise be carried into whatever test ran next in this interpreter."""
    reset_divert_streaks_for_tests()


async def _put_recovery(store: FakeStorage, sha: str | None) -> None:
    await store.put(
        recovery_key(APP),
        a_git_bundle(sha or HEAD),
        metadata={"head_sha": sha} if sha else {},
    )


# --- the comparison ---------------------------------------------------------------


async def test_a_recovery_copy_matching_head_is_confirmed(store: FakeStorage) -> None:
    await _put_recovery(store, HEAD)

    verdict = await confirm_durable_copy(APP, container_head=HEAD, container_dirty=False)

    assert verdict.state is CopyState.CONFIRMED_CURRENT
    assert verdict.may_destroy is True


async def test_a_recovery_copy_behind_head_is_stale_not_destroyable(store: FakeStorage) -> None:
    """*Covers AE7.* The deadline lapsed but the newest copy predates the newest change. A copy
    must be taken first; until one is, this container is not eligible for anything."""
    await _put_recovery(store, OLDER)

    verdict = await confirm_durable_copy(APP, container_head=HEAD, container_dirty=False)

    assert verdict.state is CopyState.STALE
    assert verdict.may_destroy is False


async def test_a_matching_head_over_a_dirty_tree_is_not_destroyable(store: FakeStorage) -> None:
    """★ THE U19 REGRESSION. A HEAD match stopped meaning "preserved" when the agent stopped
    committing.

    Before U19 the build agent committed as it worked, so a turn that wrote files MOVED `HEAD` and
    a copy from the previous turn was detectably behind it — this gate's whole comparison rested on
    that. U19 deleted the commit discipline, so "HEAD unchanged + dirty tree" is now the shape of
    every building turn. A turn that dies before its finalizer (process death, OOM, a deploy
    restart) leaves `HEAD` exactly where the LAST turn's copy was stamped.

    So this is the shape that used to read CONFIRMED_CURRENT and destroy a whole turn's work while
    writing an audit row saying it was safe. The copy is not behind HEAD — it is behind the WORKING
    TREE, which is why STALE (a known state with a known remedy: copy first, then reclaim) rather
    than UNCONFIRMED.

    Mutation check: drop `container_dirty` from the head-match arm in `durable_copy.py` and this
    goes red while every other test in this file stays green — which is exactly how the defect
    shipped."""
    await _put_recovery(store, HEAD)

    verdict = await confirm_durable_copy(APP, container_head=HEAD, container_dirty=True)

    assert verdict.state is CopyState.STALE
    assert verdict.may_destroy is False
    # The reason must name the TREE, not the head — an operator reading "behind HEAD" over a
    # matching head would reasonably conclude the gate was broken.
    assert "uncommitted" in verdict.reason


async def test_a_matching_head_on_an_unread_tree_spares_rather_than_guesses(
    store: FakeStorage,
) -> None:
    """We reached the container and read its HEAD, but the tree probe did not answer. That is an
    unestablished fact on a path that authorises destruction, and this module's governing rule is
    that every such branch spares. `None` is deliberately NOT collapsed into `False`: a default
    that reads "clean" is precisely the permissive shape the regression above came from."""
    await _put_recovery(store, HEAD)

    verdict = await confirm_durable_copy(APP, container_head=HEAD, container_dirty=None)

    assert verdict.state is CopyState.UNCONFIRMED
    assert verdict.may_destroy is False


async def test_a_clean_tree_at_a_matching_head_is_still_collected(store: FakeStorage) -> None:
    """THE OTHER HALF, and the reason the fix is not just "never confirm". A gate that spares
    everything forever is as broken as one that destroys live work — it collects nothing and the
    fleet bills forever, which is the failure the reaper exists to prevent. The benign case must
    still authorise."""
    await _put_recovery(store, HEAD)

    verdict = await confirm_durable_copy(APP, container_head=HEAD, container_dirty=False)

    assert verdict.state is CopyState.CONFIRMED_CURRENT
    assert verdict.may_destroy is True


async def test_currency_is_the_sha_not_the_timestamp(store: FakeStorage) -> None:
    """Azure stamps `last_modified` in WHOLE SECONDS, so a Save and an autosave inside one second
    are indistinguishable by time. On this path "indistinguishable" means deleting a container
    whose newest change was never copied — so the comparison is the sha, and a fresh blob whose
    sha is stale still reads STALE."""
    await _put_recovery(store, OLDER)
    # As freshly written as anything can be; the clock says current, the content does not.
    assert store.mtimes[recovery_key(APP)] is not None

    assert (
        await confirm_durable_copy(APP, container_head=HEAD, container_dirty=False)
    ).state is CopyState.STALE


async def test_the_saved_bundle_is_not_a_substitute_for_the_recovery_slot(
    store: FakeStorage,
) -> None:
    """R11 asks for the builder's LAST COMPLETED CHANGE. The recovery slot is the platform's
    autosave at turn boundaries; `snapshot_key` is the user's explicit Save. A builder who never
    pressed Save still has work worth keeping — and they are the population most likely to be
    reclaimed, so reading the wrong slot would lose exactly the work it was written to protect."""
    await store.put(snapshot_key(APP), a_git_bundle(HEAD), metadata={"head_sha": HEAD})

    verdict = await confirm_durable_copy(APP, container_head=HEAD, container_dirty=False)

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
    could not be read, and R4 sends every one of those to escalate."""
    await store.put(recovery_key(APP), a_git_bundle(HEAD), metadata={})

    assert (
        await confirm_durable_copy(APP, container_head=HEAD, container_dirty=False)
    ).state is CopyState.UNCONFIRMED


async def test_no_recovery_copy_at_all_is_unconfirmed_not_permission(store: FakeStorage) -> None:
    """The most tempting wrong answer in the whole unit: "there is no copy, so there is nothing to
    preserve". There is no copy, so there is nothing to preserve it WITH."""
    assert (
        await confirm_durable_copy(APP, container_head=HEAD, container_dirty=False)
    ).may_destroy is False


# --- the unreachable-container fallback -------------------------------------------


async def test_an_unreachable_container_falls_back_to_a_parseable_bundle(
    store: FakeStorage,
) -> None:
    """THE GATE MUST STAY SATISFIABLE. An orphan has no registry record and may not answer at all,
    so requiring the live `HEAD` comparison in this branch would spare every genuinely-dead
    container forever and collect nothing — which is the round-1 wording this replaced.

    A present, parseable bundle stands in. The real comparison still happens in the normal case."""
    await _put_recovery(store, HEAD)

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
    assert client.torn_down == [a_sandbox_name("x")]


# --- and it compares against a REAL head ------------------------------------------


def _reachable(client: FakeSandboxClient, *, head: str) -> None:
    """Make the fake container attachable AND answerable, which is the state the comparison needs.

    `attach_existing` refusing is the DEFAULT here (a fake with no `attach_handle` raises
    `SandboxGoneError`), and that default is why the hardcoded `None` went unnoticed for so long:
    every existing test in this file drives the unreachable branch, so the fallback was the only
    branch anything exercised."""
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
    """THE COMPARISON THIS GATE IS NAMED FOR, WHICH NEVER ONCE RAN.

    `reap_user` passed a hardcoded `container_head=None`, so the "could not read the container,
    trust the bundle" fallback was the only reachable branch: the `stamped == container_head`
    comparison and the entire STALE verdict were dead code. A container holding a turn's worth of
    work newer than its last autosave therefore read as provably preserved — the exact loss this
    unit exists to prevent, by the one path that is supposed to prevent it.

    Mutation-check: put `container_head=None` back and this goes red — the fallback fires, the
    verdict is CONFIRMED_CURRENT, and the container with the uncopied work is torn down."""
    await _register(fake_redis)
    await _put_recovery(store, OLDER)  # the copy is BEHIND the container
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
    gate that simply refuses everything reachable. A copy that IS current authorises the delete."""
    await _register(fake_redis)
    await _put_recovery(store, HEAD)
    client = FakeSandboxClient()
    _reachable(client, head=HEAD)

    assert await reap_user(fake_redis, USER, client, app_id=APP) is True
    assert client.torn_down == [a_sandbox_name("x")]


async def test_a_caller_that_passes_no_app_id_is_unchanged(
    fake_redis: aioredis.Redis, store: FakeStorage
) -> None:
    """Reconcile-on-start and the sweep reap a user's OWN stale state, where the builder is about
    to be handed a fresh container anyway. They stay byte-identical; the scheduled janitor — the
    process with no human watching it — passes the id and is gated."""
    await _register(fake_redis)
    client = FakeSandboxClient()

    assert await reap_user(fake_redis, USER, client) is True
    assert client.torn_down == [a_sandbox_name("x")]


# =============================================================================
# U5 — ADR-0029 §7: the copy is TAKEN, not merely found missing
# =============================================================================
#
# §7 promises that if the newest durable copy predates the newest change, a copy is taken BEFORE
# the container is reclaimed. Neither call site took one. Both read the verdict, logged "not
# provably preserved" and spared — so a container whose autosave had silently failed was spared on
# that pass, and on every pass after it, forever: a supervisor, a dev server and an ACA replica
# billing indefinitely, with the only trace a log line that repeated every fifteen minutes and
# read, to anyone scanning it, like the guard working exactly as designed. ASM30 found the
# platform in that state.
#
# These tests use the SINGLETON store (`fake_storage`) rather than the `store` fixture above,
# because `snapshot.py` resolves the store through the accessor and the local fixture only rebinds
# `durable_copy`'s name for it. Two fakes would mean the gate reads one store and the write lands
# in another — every assertion below would pass against a copy nobody could ever restore.


def _bundles[Client: FakeSandboxClient](
    client: Client,
    *,
    head: str,
    bundles_to: str,
    ancestry: str = "0 0",
    attaches_as: str | None = None,
) -> Client:
    """A container that attaches AND answers the whole snapshot ladder: commit, bundle, base64.

    `_reachable` above stops at the state probe, which was enough while the gate only ever
    compared a sha against blob metadata. U5 writes a real bundle out of the same container, and a
    client that answers the state probe to EVERY command hands `base64` its own porcelain — which
    fails to decode, takes the sparing arm, and makes the test green for entirely the wrong
    reason. So the ladder is scripted properly and the failure arm is driven deliberately instead.

    `attaches_as` exists for one test: the registry can name a different container by the time we
    attach, and the copy must refuse rather than bundle somebody else's tree into this app's slot.
    """
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
            return ExecResult(stdout=f"{head}@@@@4@@{answered}", stderr="", exit=0)
        if cmd[0] == "base64":
            return ExecResult(stdout=bundle, stderr="", exit=0)
        return ExecResult(stdout="", stderr="", exit=0)

    client.exec_handler = handler
    return client


class _ReadsTheSlotAtTeardown(FakeSandboxClient):
    """Records what the recovery slot held AT THE MOMENT teardown was called.

    ORDER IS THE PROMISE, not just the pair of facts. §7 says a copy is taken BEFORE the container
    is reclaimed; a test that only checks the slot afterwards passes just as happily against an
    implementation that tears the container down first and then tries to bundle from a corpse."""

    def __init__(self, store: FakeStorage) -> None:
        super().__init__()
        self._store = store
        self.slot_at_teardown: str | None = None

    async def teardown(self, handle: SandboxHandle) -> None:
        meta = await self._store.head(recovery_key(APP))
        self.slot_at_teardown = (meta.metadata or {}).get("head_sha") if meta else None
        await super().teardown(handle)


async def test_a_copy_that_predates_the_newest_change_is_taken_before_the_reap(
    fake_redis: aioredis.Redis, fake_storage: FakeStorage, attempts: list[CopyAttempt]
) -> None:
    """★ THE UNIT. The recovery copy is behind the container, which is precisely the `STALE`
    verdict ADR-0029 §7 says to resolve by TAKING a copy — and until U5 was the verdict that got
    a container spared forever instead.

    Deleting this test loses the only proof that a stale-copy container is ever collected at all:
    every other test in this file asserts sparing, so a regression to "spare and log" reads as a
    guard doing its job.

    Mutation check: put `if not verdict.may_destroy: return False` back in `reap_user` and this
    goes red — nothing is torn down and the slot still holds the older tree."""
    await _register(fake_redis)
    await _put_recovery(fake_storage, OLDER)
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
    """The store refuses the upload. Taking a copy is what §7 asks for; SUCCEEDING at it is not
    something this code can promise, so the arm that matters is the one where it fails — and it
    must land back on the pre-existing behaviour (spare, never destroy) rather than on the new
    one. A copy that did not land authorises nothing.

    And it leaves a RECORD. That is the half ASM30 shows is load-bearing: sparing quietly is how
    the leak stayed invisible for as long as it did.

    Mutation check: return True from the `except` arm in `_take_the_copy_we_promised` and this
    goes red — the container is torn down having preserved nothing."""
    await _register(fake_redis)
    await _put_recovery(fake_storage, OLDER)
    client = _bundles(FakeSandboxClient(), head=HEAD, bundles_to=BUNDLED)

    async def _the_store_says_no(*_a: object, **_k: object) -> None:
        raise StorageError("blob unreachable", provider="fake", key=recovery_key(APP))

    monkeypatch.setattr(fake_storage, "put", _the_store_says_no)

    assert await reap_user(fake_redis, USER, client, app_id=APP) is False
    assert client.torn_down == []
    assert await fake_redis.exists(registry_key(USER)) == 1, "state stays for a later pass"
    assert attempts == [CopyAttempt.FAILED]


async def test_a_tree_that_fails_the_lineage_guard_diverts_and_spares_rather_than_clobbering(
    fake_redis: aioredis.Redis, fake_storage: FakeStorage, attempts: list[CopyAttempt]
) -> None:
    """★ THE REASON THE FIX GOES THROUGH U3'S GUARDED WRITE AND NOT A RAW `put`.

    The container's HEAD is not a descendant of the copy on record — a reverted or re-initialised
    workspace. A fix that simply "took a copy" here would stamp that tree in as the newest copy of
    the user's work and then destroy the container on the strength of it, which is the 2026-08-18
    loss performed by the code written to prevent it. So the write diverts, the existing bundle is
    untouched to the byte, the pinned alarm fires, and the container is spared.

    Mutation check: read `RecoveryOutcome.DIVERTED` as authorising (return True) and this goes red
    at the teardown assertion, with the good bundle still sitting in the slot it would have been
    deleted alongside."""
    await _register(fake_redis)
    await _put_recovery(fake_storage, OLDER)
    good_bundle = fake_storage.objects[recovery_key(APP)]
    client = _bundles(FakeSandboxClient(), head=HEAD, bundles_to=BUNDLED, ancestry="0 1")

    with structlog.testing.capture_logs() as logs:
        assert await reap_user(fake_redis, USER, client, app_id=APP) is False

    assert client.torn_down == []
    assert fake_storage.objects[recovery_key(APP)] == good_bundle, "byte-identical, not clobbered"
    assert [k for k in fake_storage.objects if k.startswith(divert_prefix(APP))], (
        "the refused tree is preserved, not thrown away"
    )
    assert any(e.get("event") == RECOVERY_WRITE_DID_NOT_LAND_EVENT for e in logs)
    assert attempts == [CopyAttempt.REFUSED]


async def test_a_current_copy_is_reclaimed_without_taking_a_second_one(
    fake_redis: aioredis.Redis, fake_storage: FakeStorage, attempts: list[CopyAttempt]
) -> None:
    """The other direction, so "takes a copy" cannot be satisfied by a reaper that bundles every
    container it ever looks at. A copy that is already current is the whole point of the gate; a
    second one would cost an exec, a bundle and an upload per container per pass, on the path that
    walks the entire fleet every fifteen minutes.

    Mutation check: drop the `verdict.may_destroy` early-out from `_take_the_copy_we_promised` and
    this goes red — the slot is re-stamped with a tree nobody asked for."""
    await _register(fake_redis)
    await _put_recovery(fake_storage, HEAD)
    written_at = fake_storage.mtimes[recovery_key(APP)]
    client = _bundles(FakeSandboxClient(), head=HEAD, bundles_to=BUNDLED)

    assert await reap_user(fake_redis, USER, client, app_id=APP) is True

    assert client.torn_down == [a_sandbox_name("x")]
    assert fake_storage.mtimes[recovery_key(APP)] == written_at, "nothing was re-uploaded"
    assert attempts == [CopyAttempt.NOTHING_TO_COPY]


async def test_a_container_that_will_not_attach_is_spared_with_a_record_not_in_silence(
    fake_redis: aioredis.Redis, fake_storage: FakeStorage, attempts: list[CopyAttempt]
) -> None:
    """There is no copy and no way to take one, so the pre-existing sparing stands. What must NOT
    stand is the silence: this is the shape that bills forever, and the record is the only thing
    an operator can look for that does not depend on the failing component to announce itself.

    Mutation check: drop the `record_durable_copy_attempt` call from the unreachable arm and this
    goes red while every other assertion in the file stays green — which is exactly the state the
    two call sites were already in."""
    await _register(fake_redis)
    client = FakeSandboxClient()  # no `attach_handle`: `attach_existing` raises SandboxGoneError

    assert await reap_user(fake_redis, USER, client, app_id=APP) is False
    assert client.torn_down == []
    assert attempts == [CopyAttempt.UNREACHABLE]


async def test_a_copy_is_never_taken_from_a_container_the_record_no_longer_names(
    fake_redis: aioredis.Redis, fake_storage: FakeStorage, attempts: list[CopyAttempt]
) -> None:
    """★ THE WAY THIS FIX COULD ITSELF DESTROY WORK, closed before it can happen.

    `attach_existing` builds its handle from the registry record, and the record is the one input
    on this path that changes underneath us — a builder starting a fresh sandbox between the
    record read and the attach hands us their LIVE container. Bundling that tree into this app's
    recovery slot would overwrite one app's only copy with another app's work, through the code
    added to stop exactly that.

    Mutation check: drop the `reached.handle.app_name != expected_name` guard and this goes red —
    the other container's tree is bundled straight over the copy on record."""
    await _register(fake_redis)  # the registry names sbx-x
    await _put_recovery(fake_storage, OLDER)
    on_record = fake_storage.objects[recovery_key(APP)]
    client = _bundles(
        FakeSandboxClient(),
        head=HEAD,
        bundles_to=BUNDLED,
        attaches_as=a_sandbox_name("someone-else"),
    )

    assert await reap_user(fake_redis, USER, client, app_id=APP) is False
    assert client.torn_down == []
    assert fake_storage.objects[recovery_key(APP)] == on_record
    assert attempts == [CopyAttempt.UNREACHABLE]


def test_every_copy_attempt_carries_an_explanation_an_operator_can_act_on() -> None:
    """A row saying a container was spared, with no `detail` and no outcome mapped to it, is a row
    nobody can do anything with — and the failure is worse than useless: an unmapped member raises
    `KeyError` INSIDE `record_durable_copy_attempt`'s swallow, so the record simply vanishes and
    the sparing goes silent again, which is the exact condition this unit exists to remove.

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

    The production function opens its OWN session, because it has to land even when the reap it
    describes has just failed — which is precisely what makes it invisible to a suite running
    inside one rolled-back transaction. So the factory is rebound to hand back THIS connection's
    session with `commit` neutered: committing the harness transaction would leak rows into every
    later test, and `test_reclamation_report_only.py` counts every row in that table.

    `committed` is not bookkeeping. Autoflush means a bare `add()` is visible to the very next
    SELECT, so a writer that forgot to commit would read as perfectly healthy here while writing
    nothing at all in production. Modelled on `record_pass_writes_here`, which exists for the
    same reason one layer up."""
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
    is still visible in here through autoflush, which is exactly how a writer that persists
    nothing in production reads as healthy in a suite."""
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
    """★ THE REASON THESE ROWS CARRY THEIR OWN `task_name`.

    `reclamation_pass_freshness` reads the single newest row for `RECLAMATION_TASK_NAME` and
    pronounces the scheduler alive on the strength of it. That is the ONLY detector of a dead
    worker in the system — every other alarm the pass raises is emitted by the pass, so a
    crashlooping scheduler emits none of them. Filing a per-container copy attempt under the
    pass's own name would keep that detector permanently satisfied by a completely different
    subsystem, and the one alarm that can see a dead worker would never fire again.

    Mutation check: set `DURABLE_COPY_TASK_NAME = RECLAMATION_TASK_NAME` and this goes red."""
    db, _ = copy_record_writes_here
    before = await reclamation_pass_freshness(db)

    await record_durable_copy_attempt(CopyAttempt.COPIED)

    assert await reclamation_pass_freshness(db) == before


# =============================================================================
# The unguarded first copy — the P1 an adversarial review reproduced
# =============================================================================


async def test_an_uncomparable_copy_on_record_is_never_overwritten_and_never_authorises_a_reap(
    fake_redis: aioredis.Redis, fake_storage: FakeStorage, attempts: list[CopyAttempt]
) -> None:
    """★★ THE VERIFIED DATA-LOSS PATH, in the shape the review drove it.

    The recovery slot holds a bundle written before the head stamp existed — `durable_copy.py`
    documents that population — so `confirm_durable_copy` cannot compare and returns UNCONFIRMED.
    U5 then takes the copy. Before this fix, `write_recovery_copy` read "no head to compare
    against" as "nothing to protect" and wrote the reverted tree straight over the user's only
    durable copy, into a store with no versioning and no soft delete — and U5 read that WRITTEN as
    proof and deleted the container in the same call.

    Mutation check: restore either half (the `recorded is None` write in `snapshot.py`, or the
    `recorded_head is None` spare in `reaper.py`) and this goes red."""
    await _put_recovery(fake_storage, None)  # present, but carrying no head_sha
    before = await fake_storage.get(recovery_key(APP))
    await _register(fake_redis)
    client = _bundles(FakeSandboxClient(), head=REVERTED, bundles_to=REVERTED, ancestry="0 1")

    reaped = await reap_user(fake_redis, USER, client, app_id=APP)

    assert reaped is False, "an unverifiable copy must never authorise a destroy"
    assert client.torn_down == []
    assert await fake_storage.get(recovery_key(APP)) == before
    assert attempts == [CopyAttempt.REFUSED]


async def test_the_first_copy_on_record_is_kept_but_does_not_authorise_a_reap(
    fake_redis: aioredis.Redis, fake_storage: FakeStorage, attempts: list[CopyAttempt]
) -> None:
    """★ THE OTHER HALF, and it needs no legacy bundle at all — it is U5's own target population.

    An app whose every autosave failed (ASM30) has an EMPTY recovery slot. A reverted container's
    empty tree then becomes the first copy on record, `recoverable_work` ranks it newest by
    `last_modified`, and the citizen's next build is restored from the template over their saved
    app. Taking the copy is still right — it is strictly better than nothing — but it is not
    evidence, so it must not authorise the delete.

    Mutation check: drop the `recorded_head is None` arm in `reaper.py` and this goes red."""
    await _register(fake_redis)
    client = _bundles(FakeSandboxClient(), head=REVERTED, bundles_to=REVERTED, ancestry="0 1")

    reaped = await reap_user(fake_redis, USER, client, app_id=APP)

    assert reaped is False
    assert client.torn_down == []
    # …and the copy was still taken, which is the half that must NOT regress into "spare and do
    # nothing" — that is the forever-billing leak this unit exists to close.
    assert await fake_storage.head(recovery_key(APP)) is not None
    assert attempts == [CopyAttempt.UNGUARDED]


async def test_a_destroy_on_the_unreadable_container_fallback_says_so(
    fake_redis: aioredis.Redis, fake_storage: FakeStorage, attempts: list[CopyAttempt]
) -> None:
    """★ `may_destroy` is True for two different facts, and only one of them is "already current".

    The other is `confirm_durable_copy`'s deliberate fallback: the container could not be read, so
    a present, parseable bundle stands in — a trade that exists so a genuinely dead container can
    ever be collected at all. Nothing about currency was established there, and recording it as
    "the durable copy was already current" writes the one row an operator would use to find "we
    destroyed containers we could not verify" and makes it say the opposite.

    Mutation check: record `NOTHING_TO_COPY` unconditionally and this goes red."""
    await _register(fake_redis)
    await _put_recovery(fake_storage, HEAD)
    client = FakeSandboxClient()  # attaches, but its state probe answers nothing usable

    assert await reap_user(fake_redis, USER, client, app_id=APP) is True
    assert attempts == [CopyAttempt.UNVERIFIED_FALLBACK]


def test_every_copy_attempt_still_maps_to_an_operator_sentence() -> None:
    """The table is the operator's whole vocabulary; a member without a row is a row that vanishes
    (or, before the lookup moved inside the swallow, an aborted reap)."""
    from src.services.build_sessions.pass_history import _ATTEMPT_MEANING

    assert set(_ATTEMPT_MEANING) == set(CopyAttempt)
