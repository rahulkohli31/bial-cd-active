"""No container is destroyed until its tree has been written back.

Every other guard in this system protects money. This one protects work, and it is the last thing
standing between a scheduled process with ARM delete authority and somebody's unsaved afternoon.

TWO SHAPES OF FAILURE ARE PINNED HERE, and they pull in opposite directions. A write-back that
does not land must SPARE the container, so the next pass retries rather than deleting work nobody
copied. A container that will not answer at all must still be COLLECTABLE against a saved bundle,
or a wedged container is spared on every pass forever and bills forever."""

from __future__ import annotations

import base64
import uuid
from collections.abc import Sequence

import pytest
import redis.asyncio as aioredis
import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession
from structlog.testing import capture_logs

from src.db.models.worker_pass import PassOutcome, WorkerPass
from src.services.build_sessions import pass_history
from src.services.build_sessions.alarms import REAP_FOUND_NO_REPOSITORY_EVENT
from src.services.build_sessions.integrity import PORCELAIN_FAILED_MARK
from src.services.build_sessions.pass_history import (
    _ATTEMPT_MEANING,
    CopyAttempt,
    record_durable_copy_attempt,
)
from src.services.build_sessions.reaper import reap_user
from src.services.build_sessions.snapshot import (
    _NO_REPOSITORY_EXIT,
    SavedCopyOutcome,
    SavedCopyWrite,
    write_the_tree_back,
)
from src.services.redis import registry_key
from src.services.redis.keys import (
    REGISTRY_FIELD_APP_NAME,
    REGISTRY_FIELD_FQDN,
    REGISTRY_FIELD_STATE,
)
from src.services.sandbox import SandboxError
from src.services.sandbox.base import ExecResult, SandboxHandle
from src.services.storage import snapshot_key
from src.services.storage.errors import StorageError
from tests.fakes import FakeSandboxClient, FakeStorage, a_git_bundle, a_sandbox_name

APP = uuid.uuid4()
USER = uuid.uuid4()
HEAD = "a" * 40
OLDER = "b" * 40
#: A container that has factory-reset: its HEAD is unrelated to anything on record.
REVERTED = "e" * 40
#: What the commit step inside the write-back turns the working tree into. Distinct from `HEAD` on
#: purpose: the snapshot commits BEFORE it bundles, so the sha that lands in the slot is never the
#: sha the container reported, and a fixture that reused one value would hide the difference.
BUNDLED = "c" * 40


@pytest.fixture(autouse=True)
def attempts(monkeypatch: pytest.MonkeyPatch) -> list[CopyAttempt]:
    """Every write-back outcome this test recorded, without touching the database.

    AUTOUSE, not convenience: `record_durable_copy_attempt` opens its own session and commits, so
    inside this suite's otherwise-rolled-back transaction an unguarded reap would leave a
    permanent row in the shared test database. The one place the real writer runs is
    `test_the_copy_record_reaches_the_database_and_is_committed` below, which counts every row."""
    recorded: list[CopyAttempt] = []

    async def _spy(attempt: CopyAttempt) -> None:
        recorded.append(attempt)

    monkeypatch.setattr(pass_history, "record_durable_copy_attempt", _spy)
    return recorded


async def _put_saved(store: FakeStorage, sha: str | None) -> None:
    await store.put(
        snapshot_key(APP),
        a_git_bundle(sha or HEAD),
        metadata={"head_sha": sha} if sha else {},
    )


async def _register(redis: aioredis.Redis) -> None:
    await redis.hset(
        registry_key(USER),
        mapping={
            REGISTRY_FIELD_APP_NAME: a_sandbox_name("x"),
            REGISTRY_FIELD_FQDN: f"{a_sandbox_name('x')}.example.io",
            REGISTRY_FIELD_STATE: "ready",
        },
    )


def _bundles[Client: FakeSandboxClient](
    client: Client,
    *,
    head: str,
    bundles_to: str,
    attaches_as: str | None = None,
    porcelain: str = "",
    commits: int = 4,
) -> Client:
    """A container that attaches AND answers the whole snapshot ladder: commit, bundle, base64.

    A client that answered the state probe to EVERY command would hand `base64` its own porcelain,
    which fails to decode, takes the sparing arm, and passes for the wrong reason — so the handler
    discriminates by `cmd[0]`. `attaches_as` exists for one test: the registry can name a different
    container by the time we attach, and the write-back must refuse rather than bundle somebody
    else's tree into this slot. `commits` and `porcelain` are the two fields the starter-template
    predicate reads, and a fixture that pinned either one could never exercise both arms."""
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
            return ExecResult(stdout=f"{head}@@{porcelain}@@{commits}@@", stderr="", exit=0)
        if cmd[0] == "base64":
            return ExecResult(stdout=bundle, stderr="", exit=0)
        return ExecResult(stdout="", stderr="", exit=0)

    client.exec_handler = handler
    return client


def _the_commit_exits[Client: FakeSandboxClient](client: Client, code: int) -> Client:
    """`client`, except that the save's commit step exits with `code`."""
    answer_the_rest = client.exec_handler
    assert answer_the_rest is not None

    def handler(cmd: list[str]) -> ExecResult:
        if cmd[0] == "sh" and "git add -A" in cmd[-1]:
            return ExecResult(stdout="", stderr="", exit=code)
        return answer_the_rest(cmd)

    client.exec_handler = handler
    return client


def _lost_its_repository[Client: FakeSandboxClient](client: Client) -> Client:
    """A container a restart wiped: it attaches, its state probe finds no HEAD and no readable
    tree, and the save's commit step refuses with the no-repository exit."""
    return _the_commit_exits(
        _bundles(client, head="", bundles_to=BUNDLED, porcelain=PORCELAIN_FAILED_MARK, commits=0),
        _NO_REPOSITORY_EXIT,
    )


class _ReadsTheSlotAtTeardown(FakeSandboxClient):
    """Records what the saved copy held AT THE MOMENT teardown was called.

    Order is the promise, not just the pair of facts: the tree is written back BEFORE the
    container is reclaimed, and a test that only checks the slot afterwards would pass just as
    happily against an implementation that tears the container down first and bundles from a
    corpse."""

    def __init__(self, store: FakeStorage) -> None:
        super().__init__()
        self._store = store
        self.slot_at_teardown: str | None = None

    async def teardown(self, handle: SandboxHandle) -> None:
        meta = await self._store.head(snapshot_key(APP))
        self.slot_at_teardown = (meta.metadata or {}).get("head_sha") if meta else None
        await super().teardown(handle)


# --- the write-back itself ----------------------------------------------------------
#
# The reap reaches it through a caller; the shutdown routine calls it directly and with nobody
# watching, so the arms are pinned at the function that decides them as well as through the
# caller that carries the decision on.


def _the_handle() -> SandboxHandle:
    return SandboxHandle(
        fqdn=f"{a_sandbox_name('x')}.example.io",
        token="tok",
        app_name=a_sandbox_name("x"),
        preview_url=f"https://{a_sandbox_name('x')}.example.io/",
        ready=True,
    )


async def _write_back(client: FakeSandboxClient) -> SavedCopyWrite:
    return await write_the_tree_back(client, _the_handle(), APP)


async def test_a_dying_containers_tree_lands_in_the_saved_copy(
    fake_storage: FakeStorage,
) -> None:
    """The ordinary teardown: the citizen worked, and the copy they reopen has to be that work
    rather than the one they last clicked Save on.
    Mutation check: return `SKIPPED` unconditionally and this goes red — the container is
    destroyed and the afternoon goes with it."""
    await _put_saved(fake_storage, OLDER)
    client = _bundles(FakeSandboxClient(), head=HEAD, bundles_to=BUNDLED, porcelain=" M page.tsx")

    written = await _write_back(client)

    assert written.outcome is SavedCopyOutcome.WRITTEN
    assert written.bundled_head == BUNDLED
    # What a reopen reads back: the bundle itself, stamped with the tree it carries. A key holding
    # the right bytes under the wrong stamp restores as an older tree at the next comparison.
    assert fake_storage.objects[snapshot_key(APP)] == a_git_bundle(BUNDLED)
    meta = await fake_storage.head(snapshot_key(APP))
    assert meta is not None and (meta.metadata or {})["head_sha"] == BUNDLED


async def test_a_tree_with_no_saved_copy_yet_is_written_rather_than_refused(
    fake_storage: FakeStorage,
) -> None:
    """★ AN EMPTY SLOT IS NOT A REASON TO REFUSE. A citizen who built across several turns and
    never pressed Save has their whole app in this container and nothing in the store, so a
    write-back that demanded something to compare against would delete exactly the population it
    exists for.
    Mutation check: refuse when the slot is empty and this goes red."""
    client = _bundles(FakeSandboxClient(), head=HEAD, bundles_to=BUNDLED, porcelain=" M page.tsx")

    assert (await _write_back(client)).outcome is SavedCopyOutcome.WRITTEN
    assert fake_storage.objects[snapshot_key(APP)] == a_git_bundle(BUNDLED)


async def test_a_container_still_holding_the_starter_template_writes_nothing_at_all(
    fake_storage: FakeStorage,
) -> None:
    """★ THE ONE THING THAT MUST NOT BE WRITTEN. A container that reverted to its baked image
    presents as one commit and a clean tree — exactly like a project nobody has built — and
    writing that blank tree back would stamp the template in as the citizen's app.
    Mutation check: drop the `is_the_untouched_starter` arm and this goes red: the saved app is
    overwritten with the template."""
    await _put_saved(fake_storage, OLDER)
    on_record = fake_storage.objects[snapshot_key(APP)]
    client = _bundles(FakeSandboxClient(), head=REVERTED, bundles_to=REVERTED, commits=1)

    written = await _write_back(client)

    assert written.outcome is SavedCopyOutcome.SKIPPED
    assert written.bundled_head is None, "nothing was bundled"
    assert fake_storage.objects[snapshot_key(APP)] == on_record, "byte-identical, not clobbered"


async def test_a_starter_tree_the_framework_rewrote_still_counts_as_untouched(
    fake_storage: FakeStorage,
) -> None:
    """★ THE FORGIVING CHURN SET, AND THE ASYMMETRY IS DELIBERATE. `next dev` normalises
    `tsconfig.json` on every boot, so a factory-reset container that merely STARTED reports a
    dirty tree — and under the stricter `REGENERATED_ONLY` set it would read as "not the starter"
    and its blank tree would be written straight over the citizen's saved app.
    Mutation check: swap `clean_but_for_churn` for `only_regenerated_files_changed` and this goes
    red."""
    await _put_saved(fake_storage, OLDER)
    on_record = fake_storage.objects[snapshot_key(APP)]
    client = _bundles(
        FakeSandboxClient(),
        head=REVERTED,
        bundles_to=REVERTED,
        commits=1,
        porcelain=" M tsconfig.json",
    )

    assert (await _write_back(client)).outcome is SavedCopyOutcome.SKIPPED
    assert fake_storage.objects[snapshot_key(APP)] == on_record


async def test_a_tree_that_has_been_bundled_before_is_never_read_as_the_starter(
    fake_storage: FakeStorage,
) -> None:
    """The other side of the same predicate, so "skips the starter" cannot be satisfied by a
    write-back that skips everything. The commit count is the CONTAINER's own history: a fresh
    provision seeds exactly one root commit and every platform bundle adds one, so more than one
    is positive proof this tree has been built in.
    Mutation check: read `commits <= 1` and a container mid-build stops being written back."""
    await _put_saved(fake_storage, OLDER)
    client = _bundles(FakeSandboxClient(), head=HEAD, bundles_to=BUNDLED, commits=2)

    assert (await _write_back(client)).outcome is SavedCopyOutcome.WRITTEN


async def test_a_probe_that_could_not_count_is_never_read_as_the_starter(
    fake_storage: FakeStorage,
) -> None:
    """`commits == 0` means the probe could not count, and unknown is never permission to skip a
    write on a path that ends in a delete.
    Mutation check: read `commits != 1` as "not the starter" — which it is — but treat 0 as bare
    and this goes red."""
    client = _bundles(FakeSandboxClient(), head=HEAD, bundles_to=BUNDLED, commits=0)

    assert (await _write_back(client)).outcome is SavedCopyOutcome.WRITTEN


async def test_a_container_that_will_not_answer_raises_rather_than_reporting_an_outcome(
    fake_storage: FakeStorage,
) -> None:
    """An unestablished fact on a path that ends in an ARM delete is not an outcome to return: a
    caller pattern-matching on two members would have to remember that a third means "we could not
    tell", and the one that forgets destroys a container nobody read.
    Mutation check: return an outcome instead of raising and this goes red."""
    await _put_saved(fake_storage, OLDER)
    client = FakeSandboxClient()
    client.exec_handler = lambda _cmd: ExecResult(stdout="", stderr="no", exit=1)

    with pytest.raises(SandboxError):
        await _write_back(client)

    assert fake_storage.objects[snapshot_key(APP)] == a_git_bundle(OLDER)


# --- reap_user writes the tree back before it reclaims -------------------------------


async def test_a_tree_is_written_back_before_the_reap(
    fake_redis: aioredis.Redis, fake_storage: FakeStorage, attempts: list[CopyAttempt]
) -> None:
    """★ THE UNIT. `reap_user` used to call `sandbox_client.teardown` with no write-back at all,
    on the path that does almost all of the deleting.
    Mutation check: skip the write-back and this goes red — the container goes and the slot still
    holds the older tree."""
    await _register(fake_redis)
    await _put_saved(fake_storage, OLDER)
    client = _bundles(_ReadsTheSlotAtTeardown(fake_storage), head=HEAD, bundles_to=BUNDLED)

    assert await reap_user(fake_redis, USER, client, app_id=APP) is True

    # THE ORDER, not just the outcome: the slot already held this tree when the delete ran.
    assert client.slot_at_teardown == BUNDLED
    assert client.torn_down == [a_sandbox_name("x")]
    assert attempts == [CopyAttempt.COPIED]


async def test_a_write_back_that_will_not_land_spares_the_container_and_records_why(
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
    attempts: list[CopyAttempt],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The store refuses the upload. Writing the tree back is what the contract asks for;
    succeeding at it is not something this code can promise, so a failure must spare and leave a
    RECORD — sparing quietly is how the leak stayed invisible for as long as it did.
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


async def test_a_container_that_will_not_attach_is_collected_against_a_saved_bundle(
    fake_redis: aioredis.Redis, fake_storage: FakeStorage, attempts: list[CopyAttempt]
) -> None:
    """★ THE ESCAPE HATCH FOR A CONTAINER THAT WILL NOT ANSWER. Nothing can be bundled from a
    container that will not attach, and `reap_user` carries no strike count of its own — so
    without this a wedged container is spared on every pass, forever, billing forever.
    Mutation check: return False whenever `reached is None` and this goes red."""
    await _register(fake_redis)
    await _put_saved(fake_storage, HEAD)
    client = FakeSandboxClient()  # no `attach_handle`: `attach_existing` raises SandboxGoneError

    assert await reap_user(fake_redis, USER, client, app_id=APP) is True
    assert client.torn_down == [a_sandbox_name("x")]
    assert attempts == [CopyAttempt.NOTHING_TO_COPY]


async def test_a_container_that_will_not_attach_with_no_bundle_is_spared_not_silently_dropped(
    fake_redis: aioredis.Redis, fake_storage: FakeStorage, attempts: list[CopyAttempt]
) -> None:
    """The other side of the same arm: nothing to bundle from AND nothing on record, so the only
    copy of this citizen's work may be inside the container we cannot read. Spared — and not in
    silence, since the record is the only thing an operator can look for that does not depend on
    the failing component to announce itself.
    Mutation check: drop the `store.head` check and this goes red — the container is destroyed
    with nothing anywhere."""
    await _register(fake_redis)
    client = FakeSandboxClient()

    assert await reap_user(fake_redis, USER, client, app_id=APP) is False
    assert client.torn_down == []
    assert attempts == [CopyAttempt.UNREACHABLE]


async def test_a_store_that_raises_spares_the_container_instead_of_escaping(
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
    attempts: list[CopyAttempt],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The container will not attach AND the store will not answer — the one combination where
    the question "is there a copy to destroy this against?" cannot be answered at all.

    A STORE THAT RAISES IS A FACT ABOUT THE DEPLOYMENT, NEVER ABOUT ANYBODY'S WORK, so it takes
    the sparing arm exactly as a missing bundle does. This call sits outside the broad `except`
    that guards the write-back below it, so an escaping error would not merely mis-handle this
    container — it would leave `sweep_all`'s per-user handler and end the whole of this
    citizen's reap.
    Mutation check: delete the `except` arm in `_a_saved_bundle_stands_in` and this goes red."""
    await _register(fake_redis)
    client = FakeSandboxClient()  # no `attach_handle`: `attach_existing` raises SandboxGoneError

    async def _the_store_will_not_answer(*_a: object, **_k: object) -> None:
        raise StorageError("blob unreachable", provider="fake", key=snapshot_key(APP))

    monkeypatch.setattr(fake_storage, "head", _the_store_will_not_answer)

    assert await reap_user(fake_redis, USER, client, app_id=APP) is False
    assert client.torn_down == []
    assert await fake_redis.exists(registry_key(USER)) == 1, "state stays for a later pass"
    assert attempts == [CopyAttempt.UNREACHABLE]


async def test_a_tree_is_never_bundled_from_a_container_the_record_no_longer_names(
    fake_redis: aioredis.Redis, fake_storage: FakeStorage, attempts: list[CopyAttempt]
) -> None:
    """`attach_existing` builds its handle from the registry record, and the record is the one
    input on this path that can change underneath us — a builder starting a fresh sandbox between
    the record read and the attach hands us their LIVE container. Bundling that tree into this
    app's saved copy would overwrite one app's only copy with another app's work.
    Mutation check: drop the `reached.app_name != expected_name` guard and this goes red — the
    other container's tree is bundled straight over the copy on record."""
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


async def test_a_caller_that_passes_no_app_id_writes_nothing_back(
    fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """Reconcile-on-start and the sweep reap a user's OWN stale state, where the builder is about
    to be handed a fresh container anyway. Only a caller that names the app can write its tree
    back — there is nowhere to put it otherwise."""
    await _register(fake_redis)
    client = FakeSandboxClient()

    assert await reap_user(fake_redis, USER, client) is True
    assert client.torn_down == [a_sandbox_name("x")]
    assert await fake_storage.head(snapshot_key(APP)) is None


@pytest.mark.parametrize("saved", [OLDER, None], ids=["saved-before", "never-saved"])
async def test_a_container_that_lost_its_repository_is_reclaimed_not_spared_forever(
    fake_redis: aioredis.Redis,
    fake_storage: FakeStorage,
    attempts: list[CopyAttempt],
    monkeypatch: pytest.MonkeyPatch,
    saved: str | None,
) -> None:
    """★ NO LATER PASS COULD SAVE IT. A restart that discards the container's disk takes `.git`
    with it, and nothing on the platform can save a tree without one: Save, this write-back and
    the quarantine all refuse it the same way. Spared, it is retried on every pass and billed
    forever. Whatever copy is on record stands untouched for the next relaunch to restore, and an
    app that was never saved loses nothing a later pass could have kept.
    Mutation check: let `WorkspaceHasNoRepositoryError` fall into the broad `except` and this
    goes red."""
    await _register(fake_redis)
    if saved is not None:
        await _put_saved(fake_storage, saved)
    on_record = dict(fake_storage.objects)
    stored: list[str] = []

    async def _no_store(key: str, *_a: object, **_k: object) -> None:
        stored.append(key)

    monkeypatch.setattr(fake_storage, "put", _no_store)
    client = _lost_its_repository(FakeSandboxClient())

    with capture_logs() as logs:
        assert await reap_user(fake_redis, USER, client, app_id=APP) is True

    assert client.torn_down == [a_sandbox_name("x")]
    assert await fake_redis.exists(registry_key(USER)) == 0
    assert attempts == [CopyAttempt.NOTHING_TO_COPY]
    assert stored == [], "nothing may be written over the copy on record"
    assert fake_storage.objects == on_record
    found = [log for log in logs if log["event"] == REAP_FOUND_NO_REPOSITORY_EVENT]
    assert [(log["log_level"], log["app_id"], log["app_name"]) for log in found] == [
        ("warning", str(APP), a_sandbox_name("x"))
    ]


async def test_a_commit_that_fails_for_any_other_reason_still_spares(
    fake_redis: aioredis.Redis, fake_storage: FakeStorage, attempts: list[CopyAttempt]
) -> None:
    """Only the missing repository reclaims. A full disk or a locked index fails the same commit
    step, and that tree is still there to be written back on a later pass.
    Mutation check: catch `SandboxError` where the reaper catches `WorkspaceHasNoRepositoryError`
    and this goes red."""
    await _register(fake_redis)
    await _put_saved(fake_storage, OLDER)
    client = _the_commit_exits(
        _bundles(FakeSandboxClient(), head=HEAD, bundles_to=BUNDLED, porcelain=" M page.tsx"), 1
    )

    assert await reap_user(fake_redis, USER, client, app_id=APP) is False
    assert client.torn_down == []
    assert await fake_redis.exists(registry_key(USER)) == 1, "state stays for a later pass"
    assert attempts == [CopyAttempt.FAILED]


async def test_a_missing_repository_the_state_probe_contradicts_still_spares(
    fake_redis: aioredis.Redis, fake_storage: FakeStorage, attempts: list[CopyAttempt]
) -> None:
    """★ The state probe has just read a HEAD over a dirty tree, so a no-repository answer from
    the commit is a failing git, not a lost disk, and destroying on it loses the unsaved tree.
    Mutation check: re-raise `WorkspaceHasNoRepositoryError` whatever the state probe read and
    this goes red."""
    await _register(fake_redis)
    await _put_saved(fake_storage, OLDER)
    client = _the_commit_exits(
        _bundles(FakeSandboxClient(), head=HEAD, bundles_to=BUNDLED, porcelain=" M page.tsx"),
        _NO_REPOSITORY_EXIT,
    )

    with capture_logs() as logs:
        assert await reap_user(fake_redis, USER, client, app_id=APP) is False

    assert client.torn_down == []
    assert await fake_redis.exists(registry_key(USER)) == 1, "state stays for a later pass"
    assert attempts == [CopyAttempt.FAILED]
    assert not [log for log in logs if log["event"] == REAP_FOUND_NO_REPOSITORY_EVENT]


# --- the record an operator reads ----------------------------------------------------


def test_every_copy_attempt_carries_an_explanation_an_operator_can_act_on() -> None:
    """A row saying a container was spared, with no `detail` and no outcome mapped to it, is a row
    nobody can act on — and worse, an unmapped member raises `KeyError` inside
    `record_durable_copy_attempt`'s swallow, so the record vanishes and the sparing goes silent
    again.
    Mutation check: add a member to `CopyAttempt` without a `_ATTEMPT_MEANING` row and this goes
    red."""
    assert set(_ATTEMPT_MEANING) == set(CopyAttempt)
    assert all(detail.strip() for _, detail in _ATTEMPT_MEANING.values())


@pytest.fixture
def copy_record_writes_here(  # noqa: ANN201
    db_session: AsyncSession,
    monkeypatch: pytest.MonkeyPatch,
    empty_worker_passes: None,
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
