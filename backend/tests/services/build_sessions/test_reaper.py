"""U3 — the reaper ordering + reconciliation sweep (deterministic fakeredis + a fake C2
client). Asserts the KTD-3 drifted-lock reclaim, the mark-ending-before-teardown order,
and sweep idempotency/timer-safety."""

from __future__ import annotations

import ast
import base64
import re
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import redis.asyncio as aioredis
import structlog.testing

from src.api.v1.build_sessions.schemas import (
    LIVENESS_LEASE_TTL_SECONDS,
    RELAUNCH_PREVIEW_STAY_SECONDS,
)
from src.main import create_app
from src.services.build_sessions import locks, pass_history, reaper
from src.services.build_sessions.pass_history import CopyAttempt
from src.services.build_sessions.snapshot import reset_divert_streaks_for_tests
from src.services.redis import (
    REGISTRY_STATE_ENDING,
    REGISTRY_STATE_READY,
    heartbeat_key,
    lease_key,
    lock_key,
    registry_key,
)
from src.services.redis.keys import (
    REGISTRY_FIELD_APP_NAME,
    REGISTRY_FIELD_CREATED_AT,
    REGISTRY_FIELD_FQDN,
    REGISTRY_FIELD_PREVIEW_STAY_UNTIL,
    REGISTRY_FIELD_STATE,
    REGISTRY_FIELD_TOKEN_REF,
    starting_key,
)
from src.services.sandbox import SandboxError, SandboxHandle
from src.services.sandbox.base import ExecResult
from src.services.storage import recovery_key
from tests.fakes import FakeSandboxClient, FakeStorage, a_git_bundle, a_sandbox_name

USER = uuid.uuid4()
OTHER = uuid.uuid4()


@pytest.fixture(autouse=True)
def attempts(monkeypatch: pytest.MonkeyPatch) -> list[CopyAttempt]:
    """Every U5 copy-before-reclaim outcome this test recorded, WITHOUT touching the database.

    AUTOUSE, AND NOT FOR CONVENIENCE. `record_durable_copy_attempt` opens its own session and
    COMMITS — it must, because the row has to land even when the reap it describes has just
    failed — so every gated reap driven from this file would otherwise leave a permanent row in
    the SHARED test database, and `test_reclamation_report_only.py` counts every row in that
    table. The real writer is exercised in `test_durable_copy_gate.py`, against a connection that
    rolls back."""
    recorded: list[CopyAttempt] = []

    async def _spy(attempt: CopyAttempt) -> None:
        recorded.append(attempt)

    monkeypatch.setattr(pass_history, "record_durable_copy_attempt", _spy)
    return recorded


@pytest.fixture(autouse=True)
def _forget_the_divert_streak() -> None:
    """U3's refusal counter is PROCESS-LOCAL, so a divert driven here would otherwise ride into
    whatever test runs next in this interpreter."""
    reset_divert_streaks_for_tests()


#: A name the platform could actually have MINTED — `sbx-` + 28 lowercase hex, the exact shape
#: `manager.app_name_for` produces. The old fixture said "sbx-x", which no code path can emit, and
#: that shorthand is precisely what let a missing name guard on the ARM delete path go unnoticed:
#: a fixture that cannot represent a real name cannot test what happens to an unreal one.


SBX = a_sandbox_name("x")
APP = uuid.uuid4()
LOCK_TTL = 900
HB_TTL = 90


async def _seed(
    redis: aioredis.Redis,
    user: uuid.UUID,
    *,
    app_name: str = SBX,
    with_lock: bool = True,
    with_heartbeat: bool = True,
) -> None:
    await redis.hset(
        registry_key(user),
        mapping={
            REGISTRY_FIELD_APP_NAME: app_name,
            REGISTRY_FIELD_FQDN: f"{app_name}.example",
            REGISTRY_FIELD_TOKEN_REF: "ref-123",
            REGISTRY_FIELD_CREATED_AT: "2026-07-14T00:00:00+00:00",
            REGISTRY_FIELD_STATE: REGISTRY_STATE_READY,
        },
    )
    if with_lock:
        await redis.set(lock_key(user), "some-crashed-token", ex=LOCK_TTL)
    if with_heartbeat:
        await redis.set(heartbeat_key(user), "beat", ex=HB_TTL)


class OrderTrackingClient(FakeSandboxClient):
    """Records the registry `state` observed AT teardown time — proves mark-ending runs
    BEFORE teardown (the guard a concurrent attach depends on)."""

    def __init__(self, redis: aioredis.Redis, user: uuid.UUID) -> None:
        super().__init__()
        self._redis = redis
        self._user = user
        self.state_at_teardown: str | None = None

    async def teardown(self, handle: SandboxHandle) -> None:
        reg = await self._redis.hgetall(registry_key(self._user))
        value = reg.get(REGISTRY_FIELD_STATE)
        self.state_at_teardown = value.decode() if isinstance(value, bytes) else value
        await super().teardown(handle)


async def test_reap_user_marks_ending_before_teardown_then_releases(
    fake_redis: aioredis.Redis,
) -> None:
    await _seed(fake_redis, USER)
    client = OrderTrackingClient(fake_redis, USER)
    assert await reaper.reap_user(fake_redis, USER, client) is True
    assert client.state_at_teardown == REGISTRY_STATE_ENDING  # marked BEFORE teardown
    assert SBX in client.torn_down
    assert await locks.read_registry(fake_redis, USER) is None  # registry cleared
    assert await locks.lock_is_held(fake_redis, USER) is False  # lock released AFTER teardown


@pytest.mark.parametrize(
    "app_name",
    [
        "",  # the field is missing from the record — `reg.get(..., "")` handed on verbatim
        "pub-0123456789abcdef0123456789",  # a PUBLISHED app: a citizen's live application
        "bial-dev-aca-env",  # infrastructure that happens to share the resource group
        "sbx",  # the prefix without its separator
        "SBX-0123456789abcdef0123456789ab",  # ARM names are lowercase; this is not one of ours
    ],
)
async def test_a_record_naming_something_that_is_not_a_sandbox_deletes_nothing(
    fake_redis: aioredis.Redis, app_name: str
) -> None:
    """THE ONE PLACE A BAD STRING BECOMES AN ARM DELETE.

    `reap_user` rebuilds a teardown handle from the registry with `reg.get(app_name, "")` and hands
    it straight to the control plane. Whatever that record says gets deleted — and the record is
    the least trustworthy input in the system: it is the store the whole ADR distrusts, it has no
    TTL, it is written by several code paths, and the reap path had no check that the name it was
    about to destroy was even a sandbox. An empty string, a corrupted write, or a `pub-` name that
    got in by any route was a delete request for something that is not ours.

    FAILS CLOSED AND CLEARS THE RECORD. Refusing but keeping the record would retry the same
    refusal every five minutes forever; clearing it stops the loop and leaves the loud log line as
    the only trace, which is the correct trade for a record we have already established is wrong.
    The container itself is untouched — if it is real, it is somebody else's to delete.

    Mutation-check: drop the guard and the `pub-` case deletes a published application."""
    await _seed(fake_redis, USER, app_name=app_name)
    client = FakeSandboxClient()

    with structlog.testing.capture_logs() as logs:
        assert await reaper.reap_user(fake_redis, USER, client) is False

    assert client.torn_down == [], "a name we cannot vouch for must never reach ARM"
    assert await locks.read_registry(fake_redis, USER) is None, "the bogus record is cleared"
    assert any("not a sandbox name" in str(entry.get("event", "")) for entry in logs)


async def test_reconcile_reaps_on_expired_lock(fake_redis: aioredis.Redis) -> None:
    await _seed(fake_redis, USER, with_lock=False, with_heartbeat=False)
    client = FakeSandboxClient()
    assert await reaper.reconcile_user(fake_redis, USER, client, has_live_session=False) is True
    assert SBX in client.torn_down
    assert await locks.read_registry(fake_redis, USER) is None


async def test_reconcile_reaps_on_lapsed_heartbeat(fake_redis: aioredis.Redis) -> None:
    await _seed(fake_redis, USER, with_lock=True, with_heartbeat=False)
    client = FakeSandboxClient()
    assert await reaper.reconcile_user(fake_redis, USER, client, has_live_session=False) is True
    assert await locks.read_registry(fake_redis, USER) is None


async def test_reconcile_leaves_a_live_session_untouched(fake_redis: aioredis.Redis) -> None:
    await _seed(fake_redis, USER, with_lock=True, with_heartbeat=True)
    client = FakeSandboxClient()
    # A session this process still owns is never reaped by heartbeat lapse.
    assert await reaper.reconcile_user(fake_redis, USER, client, has_live_session=True) is False
    assert client.torn_down == []
    assert await locks.read_registry(fake_redis, USER) is not None
    # Even with no in-proc session, both-alive is left alone (bounded by the heartbeat TTL).
    assert await reaper.reconcile_user(fake_redis, USER, client, has_live_session=False) is False


# --- #10/R3: the certified-dead reap-through ----------------------------------
#
# `lock_is_held AND heartbeat_is_alive` is a FACADE, not liveness: a process that died
# mid-build leaves both lingering up to their TTLs. Whether the facade may be trusted
# depends on what the CALLER knows, so the flag is a caller assertion, not a heuristic:
# reconcile-on-start (under `_start_lock_for`, `_active_by_user` checked, single replica)
# certifies nobody is alive and reaps through; the sweep certifies nothing and keeps the
# shield (it is what protects an in-flight start's pre-adopt seeded heartbeat).


async def test_certified_dead_reaps_through_a_lingering_lock_and_heartbeat(
    fake_redis: aioredis.Redis,
) -> None:
    # THE WALKTHROUGH 409 (#10): dead session, lock + heartbeat still lingering. The
    # certified reconcile reaps the ghost and the immediately-following acquire succeeds —
    # the user is never told a build is running when nothing is.
    await _seed(fake_redis, USER, with_lock=True, with_heartbeat=True)
    client = FakeSandboxClient()
    assert (
        await reaper.reconcile_user(
            fake_redis, USER, client, has_live_session=False, certified_dead=True
        )
        is True
    )
    assert SBX in client.torn_down  # the ghost's container is executed, not orphaned
    assert await locks.read_registry(fake_redis, USER) is None
    assert await locks.acquire_lock(fake_redis, USER) is not None  # no 409 on a phantom


async def test_the_sweep_never_certifies_and_still_trusts_the_facade(
    fake_redis: aioredis.Redis,
) -> None:
    # The sweep holds neither certifying fact, so lock+heartbeat MUST still shield — that
    # window is exactly where an in-flight start lives between its heartbeat seed and its
    # `_active_by_user` registration. `sweep_all` has no certified_dead parameter at all;
    # this pins that its inner reconcile keeps the default.
    await _seed(fake_redis, USER, with_lock=True, with_heartbeat=True)
    client = FakeSandboxClient()
    assert (await reaper.sweep_all(fake_redis, client)).reaped == 0
    assert client.torn_down == []
    assert await locks.read_registry(fake_redis, USER) is not None


async def test_certification_never_overrides_an_in_process_session(
    fake_redis: aioredis.Redis,
) -> None:
    # has_live_session=True wins over everything, certification included: the in-process
    # session IS liveness, not a facade — a caller that passes both has contradicted
    # itself, and the safe reading wins.
    await _seed(fake_redis, USER, with_lock=True, with_heartbeat=True)
    client = FakeSandboxClient()
    assert (
        await reaper.reconcile_user(
            fake_redis, USER, client, has_live_session=True, certified_dead=True
        )
        is False
    )
    assert client.torn_down == []
    assert await locks.read_registry(fake_redis, USER) is not None


async def test_reconcile_reclaims_drifted_lock_and_next_start_acquires(
    fake_redis: aioredis.Redis,
) -> None:
    # KTD-3 core: registry + a LIVE lock + an ABSENT heartbeat + no in-proc session.
    await _seed(fake_redis, USER, with_lock=True, with_heartbeat=False)
    client = FakeSandboxClient()
    assert await locks.lock_is_held(fake_redis, USER) is True
    assert await reaper.reconcile_user(fake_redis, USER, client, has_live_session=False) is True
    # The value-guarded reaper release DELETED the still-live lock (never the holder helper).
    assert await locks.lock_is_held(fake_redis, USER) is False
    # The immediately-following start acquire succeeds — no 409 on a phantom session.
    assert await locks.acquire_lock(fake_redis, USER) is not None


async def test_sweep_all_reaps_lapsed_and_is_idempotent(fake_redis: aioredis.Redis) -> None:
    await _seed(fake_redis, USER, with_lock=True, with_heartbeat=False)  # lapsed -> reapable
    await _seed(
        fake_redis, OTHER, app_name=a_sandbox_name("y"), with_lock=True, with_heartbeat=True
    )  # live
    client = FakeSandboxClient()
    assert (await reaper.sweep_all(fake_redis, client)).reaped == 1  # only USER
    assert await locks.read_registry(fake_redis, USER) is None
    assert await locks.read_registry(fake_redis, OTHER) is not None
    # A second immediate sweep is a clean no-op (idempotent / timer-safe, KTD-3).
    assert (await reaper.sweep_all(fake_redis, client)).reaped == 0


async def test_sweep_all_skips_live_users(fake_redis: aioredis.Redis) -> None:
    await _seed(fake_redis, USER, with_lock=True, with_heartbeat=False)
    client = FakeSandboxClient()
    # A user with a live in-proc session is never reaped, so its registry survives.
    assert (await reaper.sweep_all(fake_redis, client, live_users={USER})).reaped == 0
    assert await locks.read_registry(fake_redis, USER) is not None


async def test_reaper_teardown_failure_keeps_state_for_retry(fake_redis: aioredis.Redis) -> None:
    await _seed(fake_redis, USER, with_lock=True, with_heartbeat=False)
    client = FakeSandboxClient()
    client.teardown_error = SandboxError("teardown boom")
    assert await reaper.reap_user(fake_redis, USER, client) is False
    # Teardown failed -> registry + lock KEPT for a later sweep (never orphan a live box).
    assert await locks.read_registry(fake_redis, USER) is not None
    assert await locks.lock_is_held(fake_redis, USER) is True


async def test_the_scheduled_sweep_resolves_the_owning_app_id_and_the_operator_one_does_not(
    fake_redis: aioredis.Redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """THE ASYMMETRY THAT PUTS THE F1 PATH UNDER THE U14 GATE.

    `reap_user` consults `confirm_durable_copy` only when it is handed an `app_id` — that is how
    the gate is opted out of, and reconcile-on-start opts out on purpose, because a builder is
    standing right there about to be handed a fresh container. The scheduled sweep has nobody
    watching it and does almost all of the deleting, so it resolves the id and is gated. The
    operator endpoint (`POST /v1/internal/reap`) passes no map and is unchanged.

    Mutation-check: drop `app_ids_by_name=app_ids_by_name` from `sweep_all`'s `reconcile_user`
    call and the first assertion goes red — the sweep reaps exactly as it did, ungated."""
    await _seed(fake_redis, USER, with_lock=True, with_heartbeat=False)
    app_id = uuid.uuid4()
    gated_with: list[uuid.UUID | None] = []

    async def _spy_reap(redis, user, client, *, strict=False, app_id=None):  # noqa: ANN001
        gated_with.append(app_id)
        return True

    monkeypatch.setattr(reaper, "reap_user", _spy_reap)

    await reaper.sweep_all(fake_redis, FakeSandboxClient(), app_ids_by_name={SBX: app_id})
    await reaper.sweep_all(fake_redis, FakeSandboxClient())

    assert gated_with == [app_id, None]


# --- the janitor's reap: keyed by CONTAINER, not by user ----------------------
#
# The reclamation pass judges a container. `reap_user` reaps a user, destroying whatever their
# registry names at the moment it looks. Those are the same container right up until they are not,
# and both ways they diverge are this feature's own failure modes rather than exotica.


async def _preserve(store: FakeStorage, app_id: uuid.UUID, *, head: str = "a" * 40) -> None:
    """A recovery copy the durable-copy gate will accept, so these tests are about the reap."""
    await store.put(recovery_key(app_id), a_git_bundle(head), metadata={"head_sha": head})


async def test_the_janitor_destroys_the_container_it_judged_not_the_one_the_record_names(
    fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """THE INVERSION, and it is the worst outcome this feature can produce.

    Between enumeration and delete the builder started a fresh sandbox, so the registry now names
    `sbx-new`. Reaping by USER destroys `sbx-new` — a container somebody is building in right now
    — and leaves `sbx-old`, the orphan that was actually judged, standing and billing. The pass
    then reports one destruction, and it is the wrong one in both directions at once.

    Mutation-check: key the teardown off `reg[app_name]` instead of the argument and this goes
    red — `sbx-new` is torn down and the live user's record is wiped."""
    await _seed(fake_redis, USER, app_name=a_sandbox_name("new"))
    await _preserve(fake_storage, APP)
    client = FakeSandboxClient()

    destroyed = await reaper.reap_the_container_we_judged(
        fake_redis, client, app_name=a_sandbox_name("old"), user_uuid=USER, app_id=APP
    )

    assert destroyed is True
    assert client.torn_down == [a_sandbox_name("old")]
    # The live container's Redis state is NOT ours to touch: it belongs to the other container.
    assert await locks.read_registry(fake_redis, USER) is not None
    assert await locks.lock_is_held(fake_redis, USER) is True


async def test_an_unregistered_orphan_is_actually_deleted_and_says_so(
    fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """THE POPULATION THIS WHOLE SYSTEM EXISTS TO COLLECT — containers with no registry record at
    all. `reap_user` takes its no-registry early-out here: it clears an orphaned lock, returns
    False, and deletes NOTHING, while the pass that called it counted a destruction. The container
    goes on billing and the report says it is gone, which is the single most misleading thing this
    feature could tell an operator.

    The contrast is asserted rather than described: the same state, both functions."""
    await _preserve(fake_storage, APP)
    by_name, by_user = FakeSandboxClient(), FakeSandboxClient()

    assert (
        await reaper.reap_the_container_we_judged(
            fake_redis, by_name, app_name=a_sandbox_name("ghost"), user_uuid=USER, app_id=APP
        )
        is True
    )
    assert by_name.torn_down == [a_sandbox_name("ghost")]
    # And what the user-keyed reap does with the identical state:
    assert await reaper.reap_user(fake_redis, USER, by_user, app_id=APP) is False
    assert by_user.torn_down == []


async def test_the_four_step_ordering_still_runs_when_the_record_does_name_it(
    fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """Keying by name changes WHICH container dies, never HOW. When the registry does still name
    the judged container, the full ordering applies — mark-ending before teardown (the guard a
    concurrent attach depends on), then registry, lease and the lock LAST."""
    await _seed(fake_redis, USER, app_name=SBX)
    await _preserve(fake_storage, APP)
    client = OrderTrackingClient(fake_redis, USER)

    destroyed = await reaper.reap_the_container_we_judged(
        fake_redis, client, app_name=SBX, user_uuid=USER, app_id=APP
    )

    assert destroyed is True
    assert client.state_at_teardown == REGISTRY_STATE_ENDING
    assert client.torn_down == [SBX]
    assert await locks.read_registry(fake_redis, USER) is None
    assert await locks.lock_is_held(fake_redis, USER) is False


async def test_the_janitor_is_still_refused_when_the_work_is_not_preserved(
    fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """The name-keyed reap is not a way around U14. No recovery copy means nothing was
    established, and nothing established never authorises a delete."""
    await _seed(fake_redis, USER, app_name=SBX)
    client = FakeSandboxClient()

    destroyed = await reaper.reap_the_container_we_judged(
        fake_redis, client, app_name=SBX, user_uuid=USER, app_id=APP
    )

    assert destroyed is False
    assert client.torn_down == []
    assert await locks.read_registry(fake_redis, USER) is not None


async def test_a_failed_teardown_is_not_reported_as_a_destruction(
    fake_redis: aioredis.Redis, fake_storage: FakeStorage
) -> None:
    """ARM refused, so the container is still standing. Saying otherwise would have the pass
    report a shrinking fleet while it grows, and would clear the state a later pass needs."""
    await _seed(fake_redis, USER, app_name=SBX)
    await _preserve(fake_storage, APP)
    client = FakeSandboxClient()
    client.teardown_error = SandboxError("ARM said no")

    destroyed = await reaper.reap_the_container_we_judged(
        fake_redis, client, app_name=SBX, user_uuid=USER, app_id=APP
    )

    assert destroyed is False
    assert await locks.read_registry(fake_redis, USER) is not None


# --- U5: the janitor takes the copy too, and it is a SECOND call site -------------
#
# `reap_user` and `reap_the_container_we_judged` each had their own `confirm_durable_copy` call
# and each one only logged. A U5 test suite that exercised only `reap_user` — the obvious one,
# since that is where the gate tests live — would leave the janitor exactly as ASM30 found it:
# the caller with nobody watching it, sparing the same containers pass after pass forever.


def _a_container_that_bundles(
    *, head: str, bundles_to: str, name: str = SBX, ancestry: str = "0 0"
) -> FakeSandboxClient:
    """A container that attaches AND answers the snapshot ladder — commit, bundle, base64.

    The bare `FakeSandboxClient` refuses to attach at all (no `attach_handle`), which is the right
    default for every test above and is exactly the state that spares. U5 needs the opposite:
    a container the reaper can genuinely take a copy out of."""
    client = FakeSandboxClient()
    client.attach_handle = SandboxHandle(
        fqdn=f"{name}.example",
        token="tok",
        app_name=name,
        preview_url=f"https://{name}.example/",
        ready=True,
    )
    bundle = base64.b64encode(a_git_bundle(bundles_to)).decode()

    def handler(cmd: list[str]) -> ExecResult:
        if cmd[0] == "sh" and "rev-parse" in cmd[-1]:
            answered = ancestry if "merge-base" in cmd[-1] else ""
            return ExecResult(stdout=f"{head}@@@@4@@{answered}", stderr="", exit=0)
        if cmd[0] == "base64":
            return ExecResult(stdout=bundle, stderr="", exit=0)
        return ExecResult(stdout="", stderr="", exit=0)

    client.exec_handler = handler
    return client


async def test_the_janitor_takes_the_copy_before_it_destroys_what_it_judged(
    fake_redis: aioredis.Redis, fake_storage: FakeStorage, attempts: list[CopyAttempt]
) -> None:
    """★ THE SECOND CALL SITE. The recovery copy is behind the container, so ADR-0029 §7 says take
    one and then reclaim — and this path is the one that used to spare and log instead, on a timer,
    with nobody reading the log.

    Deleting this test leaves the janitor's copy unproven: `test_durable_copy_gate.py` drives
    `reap_user` only, and the two functions share no code above `_take_the_copy_we_promised`.

    Mutation check: put `if not verdict.may_destroy: return False` back in
    `reap_the_container_we_judged` and this goes red while every gate test stays green."""
    await _seed(fake_redis, USER, app_name=SBX)
    await _preserve(fake_storage, APP, head="b" * 40)  # the copy is BEHIND the container
    client = _a_container_that_bundles(head="a" * 40, bundles_to="c" * 40)

    destroyed = await reaper.reap_the_container_we_judged(
        fake_redis, client, app_name=SBX, user_uuid=USER, app_id=APP
    )

    assert destroyed is True
    assert client.torn_down == [SBX]
    meta = await fake_storage.head(recovery_key(APP))
    assert meta is not None and (meta.metadata or {})["head_sha"] == "c" * 40
    assert attempts == [CopyAttempt.COPIED]


async def test_an_orphan_with_no_copy_is_spared_with_a_record_rather_than_in_silence(
    fake_redis: aioredis.Redis, fake_storage: FakeStorage, attempts: list[CopyAttempt]
) -> None:
    """THE POPULATION THAT BILLS FOREVER, and the reason the record exists at all.

    An unregistered orphan has no address: `attach_existing` builds its handle from the registry,
    so a container the store no longer claims cannot be bundled from — and the handle we COULD
    build names a different container, which must never be copied into this app's slot. So the
    honest answer stays "spare", exactly as before. What changes is that it stops being silent:
    the same container spared on every pass is now a row an operator can find, rather than a log
    line that repeats every fifteen minutes and reads like a guard doing its job.

    Mutation check: drop the `record_durable_copy_attempt` call from the unreachable arm and this
    goes red — nothing else in the codebase notices a permanently-spared container."""
    await _seed(fake_redis, USER, app_name=a_sandbox_name("live"))  # names a DIFFERENT container
    client = FakeSandboxClient()

    destroyed = await reaper.reap_the_container_we_judged(
        fake_redis, client, app_name=a_sandbox_name("ghost"), user_uuid=USER, app_id=APP
    )

    assert destroyed is False
    assert client.torn_down == []
    assert attempts == [CopyAttempt.UNREACHABLE]


def test_the_reaper_never_binds_the_pass_record_at_module_scope() -> None:
    """★ THE IMPORT BOUNDARY U5 HAD TO WRITE AROUND, pinned so it cannot quietly close.

    `pass_history` imports `src.workers.reclamation` for the cron it derives its staleness window
    from. A module-level `from ...pass_history import ...` in the reaper would therefore have this
    service import the worker task module that imports it back, and would put the ORM engine
    (built at `src.db.base` import) behind every import of `reaper` — including the cold one
    `tests/test_import_graph.py::test_the_reaper_imports_without_the_fastapi_app` performs.

    Asserted on the SOURCE, because the property is "no such import exists at module scope" and
    there is no runtime moment at which to observe it: `tests/conftest.py` imports `src.main`
    before anything runs, so by the time an in-process check executes every module is already in
    `sys.modules` and the assertion is vacuous. Parsed rather than grepped, so a re-spelling
    (`from src.services.build_sessions import pass_history`) fails here too.

    Mutation check: hoist the `pass_history` import in `_take_the_copy_we_promised` to the top of
    `reaper.py` and this goes red."""
    source = Path(reaper.__file__).read_text(encoding="utf-8")
    for node in ast.parse(source).body:  # TOP LEVEL ONLY — a function-scoped import is the fix
        names: list[str] = []
        if isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            names = [node.module or "", *(f"{node.module or ''}.{a.name}" for a in node.names)]
        assert not any("pass_history" in name or "workers" in name for name in names), (
            "reaper.py imports the pass-record module at module scope; that inverts the "
            "service/worker direction and drags the ORM engine into every import of the reaper"
        )


# --- #43: the relaunched preview's stay of execution --------------------------
#
# A relaunched preview holds no lock and renews no heartbeat, so it trips the
# lock+heartbeat guard the instant its seeded beat lapses. Its bounded stay is what keeps
# the background sweep off it — and reconcile-on-start is deliberately NOT fooled by that
# stay, because the incoming build needs the one-per-user slot. These two behaviours are
# opposite ON PURPOSE; the pair below is the regression guard against "simplifying" them.


async def _seed_preview(
    redis: aioredis.Redis, user: uuid.UUID, *, stay: str, app_name: str = a_sandbox_name("preview")
) -> None:
    """A relaunched preview as it actually sits in Redis: registry + a raw stay value, NO
    lock (the relaunch released it) and NO heartbeat (nothing renews it)."""
    await _seed(redis, user, app_name=app_name, with_lock=False, with_heartbeat=False)
    await redis.hset(registry_key(user), REGISTRY_FIELD_PREVIEW_STAY_UNTIL, stay)


def _in(seconds: float) -> str:
    return (datetime.now(UTC) + timedelta(seconds=seconds)).isoformat()


async def test_sweep_spares_a_preview_inside_its_stay(fake_redis: aioredis.Redis) -> None:
    await _seed_preview(fake_redis, USER, stay=_in(600))
    client = FakeSandboxClient()
    assert (await reaper.sweep_all(fake_redis, client)).reaped == 0
    assert client.torn_down == []
    assert await locks.read_registry(fake_redis, USER) is not None


async def test_sweep_reaps_a_preview_once_its_stay_lapses(fake_redis: aioredis.Redis) -> None:
    # A past deadline is a LAPSED lease — no sleeping out a real 30-minute TTL.
    await _seed_preview(fake_redis, USER, stay=_in(-1))
    client = FakeSandboxClient()
    assert (await reaper.sweep_all(fake_redis, client)).reaped == 1
    assert a_sandbox_name("preview") in client.torn_down
    assert await locks.read_registry(fake_redis, USER) is None


async def test_start_reconcile_reaps_through_a_current_stay(fake_redis: aioredis.Redis) -> None:
    # THE CRUX. reconcile-on-start defaults to honor_stay=False and reaps the preview even
    # mid-lease: the incoming build claims the slot, so sparing the preview would leave its
    # container running under a registry entry the new build is about to overwrite.
    await _seed_preview(fake_redis, USER, stay=_in(600))
    client = FakeSandboxClient()
    assert await reaper.reconcile_user(fake_redis, USER, client, has_live_session=False) is True
    assert a_sandbox_name("preview") in client.torn_down  # torn down, NOT orphaned
    assert await locks.read_registry(fake_redis, USER) is None
    assert await locks.acquire_lock(fake_redis, USER) is not None  # the slot is free


async def test_a_normal_build_session_is_unaffected_by_the_stay_check(
    fake_redis: aioredis.Redis,
) -> None:
    # No stay field at all (a real build never grants one): both paths behave exactly as
    # they did before the lease existed — live is spared, lapsed is reaped.
    await _seed(fake_redis, USER, with_lock=True, with_heartbeat=True)  # live build
    client = FakeSandboxClient()
    assert (await reaper.sweep_all(fake_redis, client)).reaped == 0
    assert await reaper.reconcile_user(fake_redis, USER, client, has_live_session=False) is False

    await _seed(
        fake_redis, OTHER, app_name=a_sandbox_name("y"), with_lock=True, with_heartbeat=False
    )
    assert (
        await reaper.sweep_all(fake_redis, client)
    ).reaped == 1  # lapsed heartbeat → still reaped
    assert await locks.read_registry(fake_redis, OTHER) is None


async def test_a_malformed_stay_is_lapsed_not_a_reprieve(fake_redis: aioredis.Redis) -> None:
    # FAIL CLOSED: garbage or an empty stay buys NOTHING. An un-reaped container is a real
    # resource leak, so an unreadable lease must never grant an unbounded reprieve.
    await _seed_preview(fake_redis, USER, stay="not-a-timestamp")
    await _seed_preview(fake_redis, OTHER, stay="", app_name=a_sandbox_name("empty"))
    assert await locks.stay_of_execution_is_current(fake_redis, USER) is False
    assert await locks.stay_of_execution_is_current(fake_redis, OTHER) is False
    client = FakeSandboxClient()
    assert (await reaper.sweep_all(fake_redis, client)).reaped == 2
    assert await locks.read_registry(fake_redis, USER) is None
    assert await locks.read_registry(fake_redis, OTHER) is None


async def test_an_absent_stay_reads_as_lapsed(fake_redis: aioredis.Redis) -> None:
    # No registry at all, and a registry with no stay field: both False (reapable).
    assert await locks.stay_of_execution_is_current(fake_redis, USER) is False
    await _seed(fake_redis, USER, with_lock=False, with_heartbeat=False)
    assert await locks.stay_of_execution_is_current(fake_redis, USER) is False


async def test_grant_stay_never_conjures_a_registry(fake_redis: aioredis.Redis) -> None:
    # Guarded on existence exactly like mark_registry_ending: a user with no sandbox must
    # not end up with a one-field registry hash that the sweep would then try to tear down.
    deadline = await locks.grant_stay_of_execution(fake_redis, USER, ttl_seconds=60)
    assert deadline > datetime.now(UTC)
    assert await locks.read_registry(fake_redis, USER) is None
    # With a registry present the deadline lands on the hash and reads back as current.
    await _seed(fake_redis, USER, with_lock=False, with_heartbeat=False)
    await locks.grant_stay_of_execution(fake_redis, USER, ttl_seconds=60)
    assert await locks.stay_of_execution_is_current(fake_redis, USER) is True


async def test_a_naive_stay_stamp_is_read_as_utc(fake_redis: aioredis.Redis) -> None:
    # A tz-naive stamp must not blow up, and must be interpreted as UTC — never as the
    # HOST's local time. Seeding a single naive stamp of the CURRENT instant does not prove
    # that: read as local it lands a whole UTC offset away from now, which on UTC itself
    # and on every host EAST of it still reads as lapsed — so the assertion holds while the
    # tz handling is wrong. Measured against a local-reading mutation: UTC and +05:30 both
    # stayed green; only a westward host went red. CI runs on UTC, i.e. exactly where the
    # single-stamp version proves nothing.
    #
    # The PAIR is what pins it, on a host at any UTC offset. Both stamps are derived from
    # UTC and sit 10 minutes either side of it, which is far outside any real offset's
    # ability to flip a verdict by accident:
    #   * naive UTC now+10min  -> True  as UTC; on any host EAST of UTC (e.g. +05:30),
    #                                   reading it as local shifts it into the PAST -> False.
    #   * naive UTC now-10min  -> False as UTC; on any host WEST of UTC (e.g. -08:00),
    #                                   reading it as local shifts it into the FUTURE -> True.
    # So one of the two goes red the moment the stamp is read as local time anywhere off UTC.
    naive_utc_now = datetime.now(UTC).replace(tzinfo=None)
    await _seed_preview(fake_redis, USER, stay=(naive_utc_now + timedelta(minutes=10)).isoformat())
    assert await locks.stay_of_execution_is_current(fake_redis, USER) is True
    await _seed_preview(
        fake_redis, OTHER, stay=(naive_utc_now - timedelta(minutes=10)).isoformat()
    )
    assert await locks.stay_of_execution_is_current(fake_redis, OTHER) is False


async def test_an_absurdly_distant_stay_is_lapsed_not_an_unbounded_reprieve(
    fake_redis: aioredis.Redis,
) -> None:
    # FAIL CLOSED on the OTHER side too. A year-9999 stamp is perfectly parseable, so a
    # bare `deadline > now` check hands a container nobody owns a reprieve measured in
    # millennia — the unbounded reprieve the fail-closed contract exists to forbid, reached
    # THROUGH the parse instead of around it. The window is bounded on both sides:
    # now < deadline <= now + RELAUNCH_PREVIEW_STAY_SECONDS.
    await _seed_preview(fake_redis, USER, stay="9999-12-31T23:59:59+00:00")
    assert await locks.stay_of_execution_is_current(fake_redis, USER) is False
    # ...and the sweep actually reaps it, rather than sparing it until the year 9999.
    client = FakeSandboxClient()
    assert (await reaper.sweep_all(fake_redis, client)).reaped == 1
    assert a_sandbox_name("preview") in client.torn_down
    assert await locks.read_registry(fake_redis, USER) is None
    # One second past the ceiling is already too far — the bound is the lease length itself,
    # not a generous "looks sane" heuristic.
    await _seed_preview(fake_redis, OTHER, stay=_in(RELAUNCH_PREVIEW_STAY_SECONDS + 60))
    assert await locks.stay_of_execution_is_current(fake_redis, OTHER) is False
    # A stay AT the maximum grantable length is still honored (the ceiling is inclusive) —
    # the clamp must not shave real leases, only absurd ones.
    await _seed_preview(fake_redis, OTHER, stay=_in(RELAUNCH_PREVIEW_STAY_SECONDS - 1))
    assert await locks.stay_of_execution_is_current(fake_redis, OTHER) is True


# --- one user's failure is one user's failure --------------------------------


async def test_a_sweep_that_trips_on_one_user_still_reaps_the_rest(
    fake_redis: aioredis.Redis,
) -> None:
    """★ SWEEP ISOLATION. The scan loop used to be unguarded, so the FIRST exception ended the
    whole cycle and every user later in SCAN order went unreconciled — silently, because SCAN
    order is not stable enough for anyone to notice the same victims twice.

    The reachable case is an ARM throttle: `reap_user` deletes through a blocking ARM poller,
    and `attach_existing`'s liveness confirmation raises `AcaError`, which is NOT a
    `SandboxError` and so escapes every handler on the path.

    Mutation-check: drop the per-user `try/except` in `sweep_all` and this goes red.
    """
    doomed, healthy = uuid.uuid4(), uuid.uuid4()
    await _seed(fake_redis, doomed, app_name=a_sandbox_name("boom"), with_heartbeat=False)
    await _seed(fake_redis, healthy, app_name=a_sandbox_name("fine"), with_heartbeat=False)

    class ThrottledOnOne(FakeSandboxClient):
        async def teardown(self, handle: SandboxHandle) -> None:
            if handle.app_name == a_sandbox_name("boom"):
                raise RuntimeError("ACA get was throttled or 5xx'd")
            await super().teardown(handle)

    client = ThrottledOnOne()
    reaped = (await reaper.sweep_all(fake_redis, client)).reaped

    # The healthy user was reaped despite the other one blowing up mid-sweep.
    assert client.torn_down == [a_sandbox_name("fine")]
    assert reaped == 1
    # And the failed user's state is LEFT for a later sweep rather than half-cleared.
    assert await fake_redis.exists(registry_key(doomed)) == 1


# --- R10 / U12: the wall-clock liveness lease --------------------------------
#
# The lock+heartbeat pair is a FACADE in both directions: a crashed builder leaves it
# standing, and a live one loses its heartbeat 90 seconds in. The lease is the positive
# signal, and it is the one input here that is readable from a process that is NOT running
# the build. The two behaviours below are opposite ON PURPOSE — a timer must be
# conservative, a request path must be decisive — and the pair is the regression guard
# against collapsing them into one.


async def _hold_a_lease(redis: aioredis.Redis, user: uuid.UUID) -> None:
    """Exactly what a mid-build turn's renewal task leaves in the store."""
    assert await locks.renew_liveness_lease(redis, user) is True
    assert await locks.liveness_lease_is_held(redis, user) is True


async def test_the_sweep_spares_a_container_whose_turn_holds_a_lease(
    fake_redis: aioredis.Redis,
) -> None:
    # ★ AE5. A claimed container mid-build, with the lock AND the heartbeat both already
    # lapsed — the state every build over 90 seconds is in. Before the lease, the only thing
    # keeping the sweep off this was `live_users`, which is empty in any other process.
    await _seed(fake_redis, USER, with_lock=False, with_heartbeat=False)
    await _hold_a_lease(fake_redis, USER)
    client = FakeSandboxClient()
    assert (await reaper.sweep_all(fake_redis, client, live_users=set())).reaped == 0
    assert client.torn_down == []
    assert await locks.read_registry(fake_redis, USER) is not None


async def test_the_sweep_reaps_once_the_lease_lapses(fake_redis: aioredis.Redis) -> None:
    # The other half: a lease is a REPRIEVE, not an amnesty. Without this, a sweep that
    # spared everything unconditionally would pass the test above.
    await _seed(fake_redis, USER, with_lock=False, with_heartbeat=False)
    await fake_redis.set(lease_key(USER), str(time.time() - 1), ex=LIVENESS_LEASE_TTL_SECONDS)
    client = FakeSandboxClient()
    assert (await reaper.sweep_all(fake_redis, client, live_users=set())).reaped == 1
    assert SBX in client.torn_down


async def test_certified_dead_deletes_the_lease_and_reaps_through_it(
    fake_redis: aioredis.Redis,
) -> None:
    # ★ THE CRASHED-TAB LOCKOUT, in new clothes. A turn killed mid-build leaves a live lease
    # behind. If reconcile-on-start honoured it, the SAME builder's next start would 409
    # until the TTL expired — precisely the lockout reconcile-on-start exists to prevent,
    # reintroduced by the mechanism added to protect them.
    #
    # And it has to be DELETED, not merely ignored: a lease left in place would go on sparing
    # the container from the background sweep after the reconcile had already decided it was
    # dead and torn it down.
    #
    # Mutation-checked, with the honest result recorded rather than the flattering one: this
    # test does NOT go red when `reconcile_user`'s certified-dead delete is removed, because
    # the reap it then performs clears the lease anyway one step later. The two tests below
    # are what cover that call — the stray-lease case (nothing to reap, so nothing else
    # clears it) and the survives-the-delete case. All three exist because the pre-existing
    # `test_certified_dead_reaps_through_a_lingering_lock_and_heartbeat` seeds only the lock
    # and the heartbeat, and would have stayed green through every one of these regressions.
    await _seed(fake_redis, USER, with_lock=True, with_heartbeat=True)
    await _hold_a_lease(fake_redis, USER)
    client = FakeSandboxClient()
    assert (
        await reaper.reconcile_user(
            fake_redis, USER, client, has_live_session=False, certified_dead=True
        )
        is True
    )
    assert SBX in client.torn_down
    assert await locks.liveness_lease_is_held(fake_redis, USER) is False
    assert await fake_redis.exists(lease_key(USER)) == 0
    assert await locks.acquire_lock(fake_redis, USER) is not None  # no 409 on a phantom


async def test_certification_reaps_through_a_lease_that_survives_the_delete(
    fake_redis: aioredis.Redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    # THE BELT, WITH THE BRACES REMOVED. The certified path both deletes the lease and
    # declines to read it, and the delete alone hides the second half from every other test
    # here — a held lease is never observed because it was just removed. So the delete is
    # neutered for this one test, leaving the `not certified_dead` guard as the only thing
    # standing between the builder and a 409 that lasts until the TTL.
    #
    # It is not a hypothetical pair of belts: the lease has a RENEWAL LOOP behind it, so a
    # zombie task re-writing the key between the delete and the read would restore exactly
    # this state — and the reaper would then refuse to reclaim a slot it has already
    # certified nobody is using.
    #
    # Mutation-check: drop `not certified_dead` from the lease check and this goes red;
    # every other test in this file stays green, which is the whole reason it exists.
    monkeypatch.setattr(reaper, "release_liveness_lease", _a_delete_that_does_not_take)
    await _seed(fake_redis, USER, with_lock=True, with_heartbeat=True)
    await _hold_a_lease(fake_redis, USER)
    client = FakeSandboxClient()
    assert (
        await reaper.reconcile_user(
            fake_redis, USER, client, has_live_session=False, certified_dead=True
        )
        is True
    )
    assert SBX in client.torn_down


async def _a_delete_that_does_not_take(_redis: aioredis.Redis, _user: uuid.UUID) -> None:
    """A release that silently fails to release — the shape a racing renewal produces."""
    return None


async def test_certification_clears_a_stray_lease_even_with_no_sandbox_registered(
    fake_redis: aioredis.Redis,
) -> None:
    # No registry, but a lease left over from a turn whose teardown half-finished. The
    # certified path clears it anyway — the incoming build is about to register its own
    # container, and a lease it never wrote must not be what spares (or fails to spare) it.
    await _seed(fake_redis, USER, with_lock=False, with_heartbeat=False)
    await _hold_a_lease(fake_redis, USER)
    await locks.delete_registry(fake_redis, USER)
    client = FakeSandboxClient()
    assert (
        await reaper.reconcile_user(
            fake_redis, USER, client, has_live_session=False, certified_dead=True
        )
        is False  # nothing registered to reap
    )
    assert await fake_redis.exists(lease_key(USER)) == 0


async def test_an_in_process_session_still_wins_over_a_certification(
    fake_redis: aioredis.Redis,
) -> None:
    # `has_live_session=True` short-circuits BEFORE the lease is touched: a caller passing
    # both has contradicted itself, and the safe reading wins. Were the delete placed above
    # that guard, a live build would lose its protection to a contradictory call.
    await _seed(fake_redis, USER, with_lock=True, with_heartbeat=True)
    await _hold_a_lease(fake_redis, USER)
    client = FakeSandboxClient()
    assert (
        await reaper.reconcile_user(
            fake_redis, USER, client, has_live_session=True, certified_dead=True
        )
        is False
    )
    assert await locks.liveness_lease_is_held(fake_redis, USER) is True


async def test_the_reap_clears_the_lease_with_the_registry(fake_redis: aioredis.Redis) -> None:
    # DISOWN. A lease outliving the record it belonged to would spare whatever container the
    # next builder gets, for up to its TTL.
    await _seed(fake_redis, USER, with_lock=True, with_heartbeat=False)
    await _hold_a_lease(fake_redis, USER)
    client = FakeSandboxClient()
    assert await reaper.reap_user(fake_redis, USER, client) is True
    assert await fake_redis.exists(lease_key(USER)) == 0


async def test_a_failed_teardown_keeps_the_lease_with_the_rest_of_the_state(
    fake_redis: aioredis.Redis,
) -> None:
    # The teardown-failure arm keeps lock + registry so a later sweep retries. The lease is
    # part of that state: clearing it while the container is still standing would strip the
    # protection off a container that may STILL be building.
    await _seed(fake_redis, USER, with_lock=True, with_heartbeat=False)
    await _hold_a_lease(fake_redis, USER)
    client = FakeSandboxClient()
    client.teardown_error = SandboxError("teardown boom")
    assert await reaper.reap_user(fake_redis, USER, client) is False
    assert await locks.liveness_lease_is_held(fake_redis, USER) is True


# --- the boundary a second process may never cross ---------------------------


def test_no_worker_module_may_certify_death() -> None:
    """★ `certified_dead=True` is a CALLER ASSERTION, and one of its three premises is the
    single-replica deploy — the very premise a worker removes.

    The certification reads: this process holds the per-user start lock, has established the
    user has no in-process session, and is the only replica. A background process on another
    container can establish none of the three, so a worker passing it would reap live builds
    while their owners watched them die.

    Asserted on the SOURCE rather than by calling anything, because the property is "no such
    call exists" — there is no runtime moment at which to observe it, and a worker that
    acquires the call in six months is exactly the regression worth catching. The parameter
    already defaults to `False`, so the flag has to be spelled out to be wrong: its textual
    absence under `src/workers/` IS the boundary.

    RECURSIVE ON PURPOSE. A non-recursive `glob` was the first spelling here, and it let a
    worker organised as a subpackage — `src/workers/reclamation/tasks.py` — carry a literal
    `certified_dead=True` with this test still green, while `assert modules` stayed satisfied
    by the top-level files beside it.

    AND IT PARSES RATHER THAN GREPS, for the mirror-image reason. A substring scan fired on
    `sandbox_reap.py`'s own docstring, which says a worker may never pass this flag — a matcher
    that flags the WARNING trains people to delete the warning. What is forbidden is the
    keyword argument, so that is what is looked for: `certified_dead=True` as an actual call
    site, in prose nowhere.
    """
    worker_dir = Path(__file__).resolve().parents[3] / "src" / "workers"
    modules = sorted(worker_dir.rglob("*.py"))
    assert modules, "the worker package moved; this boundary is no longer being checked"
    for module in modules:
        tree = ast.parse(module.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for keyword in node.keywords:
                certifies = (
                    keyword.arg == "certified_dead"
                    and isinstance(keyword.value, ast.Constant)
                    and keyword.value.value is True
                )
                assert not certifies, (
                    f"{module.name} certifies death, and that certification rests on the "
                    "single-replica contract a worker removes (C5 §Liveness lease)"
                )


# --- U11 / R99: the pre-adopt window, and the one signal that can cover it ------------------
#
# A turn's life splits into three intervals, and the point of this section is that each needs a
# signal that can ACTUALLY BE HELD in it — "at least one spare signal is held" is only checkable
# interval by interval.
#
#   1. claim → a registry hash exists. Nothing can be written here and nothing needs to be:
#      `reconcile_user` returns above without reaping when there is no registry, and both write
#      primitives refuse in this window on purpose (a lease written for a user with no record
#      would spare whatever container that user gets NEXT).
#   2. registry hash → adopt-and-seed-the-heartbeat. THIS ONE. The lock/heartbeat disjunct is an
#      AND, so lock-held-with-no-heartbeat is reapable, and the turn's own door grants nothing
#      across it. The starting marker spans exactly this interval.
#   3. adopt → terminal. The R10 liveness lease, covered above and in `test_liveness_lease.py`.


async def test_the_starting_marker_spares_a_container_mid_cold_start(
    fake_redis: aioredis.Redis,
) -> None:
    """★ INTERVAL 2. The registry hash has landed and the heartbeat has not been seeded yet —
    the shape a sweep sees when it lands in the middle of a cold start.

    Asserted through `reconcile_user` itself rather than by mocking the predicate, because what
    is being tested is the ORDER of its arms as much as the disjunct."""
    await _seed(fake_redis, USER, with_lock=True, with_heartbeat=False)
    await locks.write_starting_marker(fake_redis, USER, uuid.uuid4())
    client = FakeSandboxClient()

    assert await reaper.reconcile_user(fake_redis, USER, client, has_live_session=False) is False
    assert client.torn_down == []
    assert await locks.read_registry(fake_redis, USER) is not None


async def test_the_same_container_without_the_marker_is_reaped(
    fake_redis: aioredis.Redis,
) -> None:
    """THE DISCRIMINATOR, and without it the test above proves nothing.

    Identical state, marker absent: reaped. So the sparing above is the marker's doing and not
    some other arm quietly answering first."""
    await _seed(fake_redis, USER, with_lock=True, with_heartbeat=False)
    client = FakeSandboxClient()

    assert await reaper.reconcile_user(fake_redis, USER, client, has_live_session=False) is True
    assert SBX in client.torn_down


async def test_a_marker_that_outlives_its_start_stops_sparing(
    fake_redis: aioredis.Redis,
) -> None:
    """★ A BOUNDED CLAIM, NOT A PARDON. Past its TTL the container is reapable again exactly as
    if nothing had been written.

    This is the assertion that keeps the marker from becoming the registry hash's mistake under
    a new name: "registered ⇒ spared" is the failure mode the whole reclamation design exists to
    remove, and a claim with no expiry is that failure mode with an extra step."""
    await _seed(fake_redis, USER, with_lock=True, with_heartbeat=False)
    await locks.write_starting_marker(fake_redis, USER, uuid.uuid4())
    assert (
        await reaper.reconcile_user(fake_redis, USER, FakeSandboxClient(), has_live_session=False)
        is False
    )

    # The TTL lapses — expressed as the key expiring, which is what a wall clock does to it.
    await fake_redis.delete(starting_key(USER))

    client = FakeSandboxClient()
    assert await reaper.reconcile_user(fake_redis, USER, client, has_live_session=False) is True
    assert SBX in client.torn_down


async def test_a_marker_alone_does_not_conjure_a_container_to_spare(
    fake_redis: aioredis.Redis,
) -> None:
    """INTERVAL 1, stated as the absence it is. With no registry record there is nothing to
    reap and nothing to spare, so the marker changes no outcome — which is why interval 1 needs
    no signal rather than needing one nobody wrote."""
    await locks.write_starting_marker(fake_redis, USER, uuid.uuid4())
    client = FakeSandboxClient()

    assert await reaper.reconcile_user(fake_redis, USER, client, has_live_session=False) is False
    assert client.torn_down == []


async def test_the_reclamation_passes_own_predicate_agrees(fake_redis: aioredis.Redis) -> None:
    """THE SECOND READER. `reconcile_user` is the per-user sweep; the fleet pass builds a
    `RegistryClaim` and asks `spares_the_container`. Both must count the marker, or a container
    spared by one is destroyed by the other — and the fleet pass is the one that destroys."""
    from src.services.build_sessions.reclamation_pass import claim_for_container

    await _seed(fake_redis, USER, with_lock=True, with_heartbeat=False)
    unspared = await claim_for_container(fake_redis, app_name=SBX)
    assert unspared is not None and unspared.spares_the_container is False

    await locks.write_starting_marker(fake_redis, USER, uuid.uuid4())
    spared = await claim_for_container(fake_redis, app_name=SBX)
    assert spared is not None and spared.starting is True
    assert spared.spares_the_container is True


_A_ROUTE_IN_PROSE = re.compile(r"`(?:GET|POST|PATCH|PUT|DELETE) (/v1/[A-Za-z0-9/_{}.-]+)`")


def test_every_route_the_reapers_prose_hands_an_operator_actually_resolves() -> None:
    """THE COMMENT IS AN INCIDENT-TIME INSTRUCTION, so it is asserted like one.

    The module docstring names the Azure-side inventory endpoint as the ONLY way to see a
    container whose registry record is gone — the twelve-day, ~$80 case it describes. An
    operator reads that line mid-incident and pastes it. It named `/v1/admin/reconcile-sandboxes`
    while the governance router mounts at `/admin/apps`, so the paste 404'd and the one lever
    that closes the gap looked absent.

    Asserted against the MOUNTED app rather than against a hard-coded string, because the
    failure mode is a prefix moving underneath the prose, not the prose being mistyped: pinning
    the literal would go red on the fix and green on the rot. `openapi()` is the resolution
    point — `include_router` defers to `_IncludedRouter`, so `app.routes` holds no paths until
    the schema is built.

    Mutation check: drop `/apps` from the docstring's URL and this goes red."""
    source = Path(reaper.__file__).read_text(encoding="utf-8")
    named = set(_A_ROUTE_IN_PROSE.findall(source))
    assert named, "the reaper's prose names no route; this guard has lost its subject"

    mounted = set(create_app().openapi()["paths"])
    assert named <= mounted, (
        f"the reaper's prose hands an operator {sorted(named - mounted)}, which no route serves"
    )
