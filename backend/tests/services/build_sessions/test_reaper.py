"""The reaper ordering + reconciliation sweep (deterministic fakeredis + a fake client).
Asserts the drifted-lock reclaim, the mark-ending-before-teardown order,
and sweep idempotency/timer-safety."""

from __future__ import annotations

import ast
import base64
import time
import uuid
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
import redis.asyncio as aioredis
import structlog.testing

from src.api.v1.build_sessions.schemas import (
    LIVENESS_LEASE_TTL_SECONDS,
    RELAUNCH_PREVIEW_STAY_SECONDS,
)
from src.services.build_sessions import locks, pass_history, reaper
from src.services.build_sessions.alarms import SERVING_PROOF_NEVER_ARRIVED
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
    REGISTRY_FIELD_SERVING_SINCE,
    REGISTRY_FIELD_SHARED_SERVED_COUNT,
    REGISTRY_FIELD_STATE,
    REGISTRY_FIELD_TOKEN_REF,
    starting_key,
)
from src.services.sandbox import SandboxError, SandboxHandle
from src.services.sandbox.base import DevStatus, ExecResult
from src.services.storage import recovery_key
from tests.fakes import (
    FakeSandboxClient,
    FakeStorage,
    a_git_bundle,
    a_sandbox_name,
    a_shared_sandbox_name,
)

USER = uuid.uuid4()
OTHER = uuid.uuid4()


@pytest.fixture(autouse=True)
def attempts(monkeypatch: pytest.MonkeyPatch) -> list[CopyAttempt]:
    """Every copy-before-reclaim outcome this test recorded, WITHOUT touching the database.

    AUTOUSE, AND NOT FOR CONVENIENCE: `record_durable_copy_attempt` opens its own session and
    COMMITS, so an unspied reap here would leave a permanent row in the SHARED test database
    that `test_reclamation_report_only.py` counts. The real writer is exercised, against a
    connection that rolls back, in `test_durable_copy_gate.py`."""
    recorded: list[CopyAttempt] = []

    async def _spy(attempt: CopyAttempt) -> None:
        recorded.append(attempt)

    monkeypatch.setattr(pass_history, "record_durable_copy_attempt", _spy)
    return recorded


@pytest.fixture(autouse=True)
def _forget_the_divert_streak() -> None:
    """The refusal counter is PROCESS-LOCAL, so a divert driven here would otherwise ride into
    whatever test runs next in this interpreter."""
    reset_divert_streaks_for_tests()


#: A name the platform could actually have MINTED — `sbx-` + 28 lowercase hex, the exact shape
#: `manager.app_name_for` produces, not the old "sbx-x" that no code path can emit.


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
    serving_since: str | None = None,
    created_at: str = "2026-07-14T00:00:00+00:00",
    state: str = REGISTRY_STATE_READY,
) -> None:
    """`serving_since` is a TRI-STATE and the default is the quiet one:

      None     -> the field is not written at all: PRE-CUTOVER, read as proven, never probed.
      ""       -> the container exists and has never served.
      ISO-8601 -> a standing proof.

    The default is `None` because this file is overwhelmingly about REAPING, and a pre-cutover
    record is the one reading that costs the sweep no container probe — so the forty tests below
    that have nothing to say about serving keep exercising exactly the sweep they were written
    for. The tests that DO care say which reading they mean, and the section at the foot of this
    file is where all three are pinned.
    """
    await redis.hset(
        registry_key(user),
        mapping={
            REGISTRY_FIELD_APP_NAME: app_name,
            REGISTRY_FIELD_FQDN: f"{app_name}.example",
            REGISTRY_FIELD_TOKEN_REF: "ref-123",
            REGISTRY_FIELD_CREATED_AT: created_at,
            REGISTRY_FIELD_STATE: state,
        },
    )
    if serving_since is not None:
        await redis.hset(registry_key(user), REGISTRY_FIELD_SERVING_SINCE, serving_since)
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
    """THE ONE PLACE A BAD STRING BECOMES AN ARM DELETE: `reap_user` hands the registry's
    `app_name` to the control plane unchecked, so a corrupted write or a `pub-` name (a
    citizen's live app) would be a delete request for something that is not ours.

    Fails closed but still clears the bad record — refusing without clearing would retry
    the same refusal every five minutes forever; the container itself is untouched.

    Mutation-check: drop the guard and the `pub-` case deletes a published application."""
    await _seed(fake_redis, USER, app_name=app_name)
    client = FakeSandboxClient()

    with structlog.testing.capture_logs() as logs:
        assert await reaper.reap_user(fake_redis, USER, client) is False

    assert client.torn_down == [], "a name we cannot vouch for must never reach ARM"
    assert await locks.read_registry(fake_redis, USER) is None, "the bogus record is cleared"
    assert any("not a sandbox name" in str(entry.get("event", "")) for entry in logs)


async def test_reap_user_tears_down_a_shared_sandbox_in_the_slot(
    fake_redis: aioredis.Redis,
) -> None:
    """#198: the one per-user slot `reap_user` reaps can hold EITHER lineage. Before the gate
    widened, a `shr-` name here fell into the "not a sandbox name" refusal above — the record
    would be cleared while the container itself, which the platform provably minted, kept
    running and billing forever, unowned by any registry entry."""
    shared_name = a_shared_sandbox_name("colleague")
    await _seed(fake_redis, USER, app_name=shared_name)
    client = FakeSandboxClient()

    assert await reaper.reap_user(fake_redis, USER, client) is True

    assert shared_name in client.torn_down
    assert await locks.read_registry(fake_redis, USER) is None


async def test_reap_user_skips_the_durable_copy_gate_for_a_shared_view_even_with_an_app_id(
    fake_redis: aioredis.Redis,
) -> None:
    """The bug a live Azure run found: `sweep_all`'s own `_owning_app_id` resolves a `shr-`
    record to the OWNER's app id (`_app_names_to_owners` keys the name off the recipient but
    carries the shared app's id as the value), so the scheduled sweep always calls `reap_user`
    with `app_id is not None` for a shared view. Gating on that id would run the durable-copy
    check against a recovery slot this container never wrote to (R22: read-never-write for a
    recipient) — and a diverted or unreachable verdict then REFUSES the reap outright, sparing
    the container forever. No storage is bound in this test at all: if the gate ran, touching
    it would fail loudly rather than silently pass, which is exactly the point — a shared view
    must never reach the gate regardless of which app_id a caller resolved for it."""
    shared_name = a_shared_sandbox_name("colleague")
    await _seed(fake_redis, USER, app_name=shared_name)
    client = FakeSandboxClient()
    some_unrelated_app_id = uuid.uuid4()  # stands in for the OWNER's app id, wrongly resolved

    assert await reaper.reap_user(fake_redis, USER, client, app_id=some_unrelated_app_id) is True

    assert shared_name in client.torn_down
    assert await locks.read_registry(fake_redis, USER) is None


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
    assert await reaper.reconcile_user(fake_redis, USER, client, has_live_session=True) is False
    assert client.torn_down == []
    assert await locks.read_registry(fake_redis, USER) is not None
    # Even with no in-proc session, both-alive is left alone (bounded by the heartbeat TTL).
    assert await reaper.reconcile_user(fake_redis, USER, client, has_live_session=False) is False


# --- the certified-dead reap-through ----------------------------------
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
    # dead session, lock + heartbeat still lingering: the certified reconcile reaps the
    # ghost so the user is never told a build is running when nothing is.
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
    # window is where an in-flight start lives, between its heartbeat seed and its
    # `_active_by_user` registration. `sweep_all` takes no certified_dead parameter at all.
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
    # A second immediate sweep is a clean no-op (idempotent / timer-safe).
    assert (await reaper.sweep_all(fake_redis, client)).reaped == 0


async def test_sweep_all_skips_live_users(fake_redis: aioredis.Redis) -> None:
    await _seed(fake_redis, USER, with_lock=True, with_heartbeat=False)
    client = FakeSandboxClient()
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
    """`reap_user` consults `confirm_durable_copy` only when handed an `app_id`. Reconcile-on-start
    opts out (a builder is standing right there); the scheduled sweep has nobody watching and does
    almost all of the deleting, so it resolves the id and is gated — the operator endpoint passes
    no map and stays ungated.

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
    """THE INVERSION, the worst outcome this feature can produce: between enumeration and
    delete the builder started a fresh sandbox, so the registry now names `sbx-new`. Reaping
    by USER destroys the live `sbx-new` and leaves `sbx-old` — the actual orphan — standing
    and billing, then reports one destruction that is wrong in both directions.

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
    feature could tell an operator."""
    await _preserve(fake_storage, APP)
    by_name, by_user = FakeSandboxClient(), FakeSandboxClient()

    assert (
        await reaper.reap_the_container_we_judged(
            fake_redis, by_name, app_name=a_sandbox_name("ghost"), user_uuid=USER, app_id=APP
        )
        is True
    )
    assert by_name.torn_down == [a_sandbox_name("ghost")]
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
    """The name-keyed reap is not a way around the durable-copy gate. No recovery copy means
    nothing was established, and nothing established never authorises a delete."""
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


# --- The janitor takes the copy too, and it is a SECOND call site -------------
#
# `reap_user` and `reap_the_container_we_judged` each had their own `confirm_durable_copy`
# call, and each one only logged. A test suite that exercised only `reap_user` — the obvious
# one, since that is where the gate tests live — would leave the janitor ungated: the caller
# with nobody watching it, sparing the same containers pass after pass forever.


def _a_container_that_bundles(
    *, head: str, bundles_to: str, name: str = SBX, ancestry: str = "0 0"
) -> FakeSandboxClient:
    """A container that attaches AND answers the snapshot ladder — commit, bundle, base64.

    The bare `FakeSandboxClient` refuses to attach at all (no `attach_handle`), which is the right
    default for every test above and is exactly the state that spares. This scenario needs the
    opposite: a container the reaper can genuinely take a copy out of."""
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
    """★ THE SECOND CALL SITE. The recovery copy is behind the container, so the durable-copy
    policy says take one and then reclaim — this path used to just spare and log, unread.

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
    """THE POPULATION THAT BILLS FOREVER, and the reason the record exists at all: an
    unregistered orphan has no address (`attach_existing` builds its handle from the registry),
    so it cannot be bundled from, and the honest answer stays "spare" — exactly as before. What
    changes is that it stops being silent: the same spared container is now a row an operator
    can find instead of a log line repeating every fifteen minutes.

    Mutation check: drop the `record_durable_copy_attempt` call from the unreachable arm and
    this goes red — nothing else in the codebase notices a permanently-spared container."""
    await _seed(fake_redis, USER, app_name=a_sandbox_name("live"))  # names a DIFFERENT container
    client = FakeSandboxClient()

    destroyed = await reaper.reap_the_container_we_judged(
        fake_redis, client, app_name=a_sandbox_name("ghost"), user_uuid=USER, app_id=APP
    )

    assert destroyed is False
    assert client.torn_down == []
    assert attempts == [CopyAttempt.UNREACHABLE]


def test_the_reaper_never_binds_the_pass_record_at_module_scope() -> None:
    """★ THE IMPORT BOUNDARY THE COPY-BEFORE-RECLAIM WORK HAD TO WRITE AROUND, pinned so it cannot
    quietly close. `pass_history` imports `src.workers.reclamation` for its cron staleness window,
    so a module-level `pass_history` import in the reaper would import that worker module back and
    drag the ORM engine (built at `src.db.base` import) behind every import of `reaper`. Asserted
    on the SOURCE via AST: an in-process check is vacuous because `conftest.py` already imports
    `src.main` before any test runs, populating `sys.modules`; parsing (not grepping) also catches
    a re-spelled import path. Mutation check: hoist the `pass_history` import in
    `_take_the_copy_we_promised` to module scope."""
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


# The relaunched preview's stay of execution. `sweep_all` honours an unexpired stay and
# `reconcile_user` reaps through it; the asymmetry belongs to `reaper.py`. The pair below is
# the regression guard against collapsing the two into one behaviour.


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
    # reconcile-on-start defaults to honor_stay=False and reaps the preview even mid-stay.
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


# #198 — a shared-runtime view's traffic-based stay renewal and its absolute session ceiling.
# Both are no-ops for an ordinary build sandbox: `is_a_shared_sandbox_name` gates both, so
# nothing above this section could have exercised either path.


async def _seed_shared_view(
    redis: aioredis.Redis,
    user: uuid.UUID,
    *,
    app_name: str = a_shared_sandbox_name("colleague"),
    created_at: str | None = None,
    stay: str | None = None,
) -> None:
    """A launched shared view as it actually sits in Redis: registry + (optionally) a stay, NO
    lock and NO heartbeat — the same shape a relaunched preview leaves, `_seed_preview`'s own
    sibling.

    `created_at` DEFAULTS TO NOW, deliberately NOT `_seed`'s own fixed 2026-07-14 default: this
    section's ceiling check is age-sensitive, and every other test in this file is free to use
    that fixed historical stamp only because none of them read age at all. A test that means to
    exercise the ceiling passes its own stale `created_at` explicitly."""
    await _seed(
        redis,
        user,
        app_name=app_name,
        with_lock=False,
        with_heartbeat=False,
        created_at=created_at or datetime.now(UTC).isoformat(),
    )
    if stay is not None:
        await redis.hset(registry_key(user), REGISTRY_FIELD_PREVIEW_STAY_UNTIL, stay)


def _reachable_shared_view_client(
    app_name: str, *, served: int | None, truncated: bool = False
) -> FakeSandboxClient:
    client = FakeSandboxClient()
    client.attach_handle = SandboxHandle(
        fqdn=f"{app_name}.example",
        token="tok",  # noqa: S106 - a fake, never a real bearer
        app_name=app_name,
        preview_url=f"https://{app_name}.example/",
        ready=True,
    )
    client.served_count_value = served
    client.served_count_truncated = truncated
    return client


async def test_the_sweep_renews_a_shared_views_stay_when_traffic_increased(
    fake_redis: aioredis.Redis,
) -> None:
    """The whole mechanism, end to end: a shared view with NO standing stay at all (fresh from
    Launch) survives a sweep purely because the supervisor reports new traffic."""
    name = a_shared_sandbox_name("colleague")
    await _seed_shared_view(fake_redis, USER, app_name=name)
    client = _reachable_shared_view_client(name, served=3)

    assert (await reaper.sweep_all(fake_redis, client)).reaped == 0
    assert client.torn_down == []
    reg = await locks.read_registry(fake_redis, USER)
    assert reg is not None
    assert reg[REGISTRY_FIELD_SHARED_SERVED_COUNT] == "3"
    assert await locks.stay_of_execution_is_current(fake_redis, USER) is True


async def test_the_sweep_does_not_renew_on_an_unchanged_served_count(
    fake_redis: aioredis.Redis,
) -> None:
    """A steady background poll from an idle, forgotten tab is still traffic to a naive counter
    — the count must have INCREASED since the last pass, or a container nobody is reading would
    renew itself forever."""
    name = a_shared_sandbox_name("colleague")
    await _seed_shared_view(fake_redis, USER, app_name=name, stay=_in(-1))  # already lapsed
    await fake_redis.hset(registry_key(USER), REGISTRY_FIELD_SHARED_SERVED_COUNT, "5")
    client = _reachable_shared_view_client(name, served=5)  # same count as last seen

    assert (await reaper.sweep_all(fake_redis, client)).reaped == 1
    assert name in client.torn_down


async def test_a_dropped_count_after_a_log_roll_still_renews(
    fake_redis: aioredis.Redis,
) -> None:
    """Caddy's own rolling (`roll_size 1MiB roll_keep 1`) starts a fresh, smaller access log the
    moment it rotates: `served` resets low and `truncated` flips back to `False` immediately,
    even for a session that has been busy the whole time. This used to be indistinguishable from
    a genuine regression (a restarted supervisor, a corrupted field) under `count <= last_seen`,
    which read the drop as "no new traffic" and reaped an actively-used session right at the roll
    boundary. Comparing with `==` instead treats ANY change — up or down — as evidence something
    happened, since only an EXACT match means nothing new occurred since the last pass; see
    `test_the_sweep_does_not_renew_on_an_unchanged_served_count` for that unchanged case."""
    name = a_shared_sandbox_name("colleague")
    await _seed_shared_view(fake_redis, USER, app_name=name, stay=_in(-1))  # already lapsed
    await fake_redis.hset(registry_key(USER), REGISTRY_FIELD_SHARED_SERVED_COUNT, "500")
    client = _reachable_shared_view_client(name, served=20, truncated=False)  # just rolled

    assert (await reaper.sweep_all(fake_redis, client)).reaped == 0
    assert client.torn_down == []
    reg = await locks.read_registry(fake_redis, USER)
    assert reg is not None
    assert reg[REGISTRY_FIELD_SHARED_SERVED_COUNT] == "20"
    assert await locks.stay_of_execution_is_current(fake_redis, USER) is True


async def test_a_truncated_reading_renews_even_with_a_saturated_count(
    fake_redis: aioredis.Redis,
) -> None:
    """The bug a live run would eventually find: the supervisor's `served` count is a bounded
    TAIL of the access log, not a cumulative total, so once real traffic pushes the log past
    that window the count plateaus (or drops) even though someone is actively using the app.
    Comparing it as if it were monotonic made `count <= last_seen` come back True forever from
    that point on — `truncated=True` is the supervisor's own admission that the reading is a
    window, and it must renew the stay on its own, without regard to whether the count itself
    moved."""
    name = a_shared_sandbox_name("colleague")
    await _seed_shared_view(fake_redis, USER, app_name=name, stay=_in(-1))  # already lapsed
    await fake_redis.hset(registry_key(USER), REGISTRY_FIELD_SHARED_SERVED_COUNT, "500")
    # Same count as last seen, AND lower than it could plausibly be after this much traffic —
    # exactly what a saturated tail-window reading looks like.
    client = _reachable_shared_view_client(name, served=500, truncated=True)

    assert (await reaper.sweep_all(fake_redis, client)).reaped == 0
    assert client.torn_down == []
    assert await locks.stay_of_execution_is_current(fake_redis, USER) is True


async def test_the_sweep_treats_an_unreachable_shared_view_as_no_new_evidence(
    fake_redis: aioredis.Redis,
) -> None:
    """`served_count` returning `None` (could not ask) must not renew, and must not be read as
    a confirmed absence of traffic either — the standing stay, if any, is what decides."""
    name = a_shared_sandbox_name("colleague")
    await _seed_shared_view(fake_redis, USER, app_name=name, stay=_in(-1))
    client = FakeSandboxClient()  # no attach_handle: attach_existing raises SandboxGoneError

    assert (await reaper.sweep_all(fake_redis, client)).reaped == 1
    assert name in client.torn_down


async def test_a_shared_view_past_its_ceiling_is_reaped_despite_a_current_stay(
    fake_redis: aioredis.Redis,
) -> None:
    """Requirement 20: the absolute ceiling is independent of the renewable stay. A container
    whose supervisor keeps reporting traffic — a wedged loop, or a spoofed report — must still
    end at the ceiling, not live forever on the strength of it."""
    from src.api.v1.build_sessions.schemas import SHARED_PREVIEW_ABSOLUTE_CEILING_SECONDS

    name = a_shared_sandbox_name("colleague")
    stale_created_at = (
        datetime.now(UTC) - timedelta(seconds=SHARED_PREVIEW_ABSOLUTE_CEILING_SECONDS + 60)
    ).isoformat()
    await _seed_shared_view(
        fake_redis, USER, app_name=name, created_at=stale_created_at, stay=_in(600)
    )
    client = _reachable_shared_view_client(name, served=None)  # no probe needed to prove this

    assert (await reaper.sweep_all(fake_redis, client)).reaped == 1
    assert name in client.torn_down


async def test_a_shared_view_within_its_ceiling_and_a_current_stay_is_spared(
    fake_redis: aioredis.Redis,
) -> None:
    """The ceiling only ever SUBTRACTS from what a stay would otherwise spare — sanity-checked
    the other direction so the two tests can't both pass on a classifier that ignores the age
    entirely."""
    name = a_shared_sandbox_name("colleague")
    await _seed_shared_view(fake_redis, USER, app_name=name, stay=_in(600))
    client = _reachable_shared_view_client(name, served=None)

    assert (await reaper.sweep_all(fake_redis, client)).reaped == 0
    assert client.torn_down == []


async def test_the_ceiling_never_touches_an_ordinary_build_preview(
    fake_redis: aioredis.Redis,
) -> None:
    """Regression guard: `_shared_view_past_its_ceiling` gates on `is_a_shared_sandbox_name`,
    so an `sbx-` preview old enough to trip the SAME age threshold must be unaffected by it —
    its own (much longer) `RELAUNCH_PREVIEW_STAY_SECONDS` stay is what governs it."""
    from src.api.v1.build_sessions.schemas import SHARED_PREVIEW_ABSOLUTE_CEILING_SECONDS

    stale_created_at = (
        datetime.now(UTC) - timedelta(seconds=SHARED_PREVIEW_ABSOLUTE_CEILING_SECONDS + 60)
    ).isoformat()
    await _seed_preview(fake_redis, USER, stay=_in(600))
    await fake_redis.hset(registry_key(USER), REGISTRY_FIELD_CREATED_AT, stale_created_at)
    client = FakeSandboxClient()

    assert (await reaper.sweep_all(fake_redis, client)).reaped == 0
    assert client.torn_down == []


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
    # A tz-naive stamp must be read as UTC, never as the HOST's local time. A SINGLE naive
    # stamp at the current instant does not prove that: read as local it lands a whole UTC
    # offset away from now, which on UTC itself and every host EAST of it still reads as
    # lapsed — so the assertion would hold even with the tz handling wrong. Measured against
    # a local-reading mutation: UTC and +05:30 both stayed green; only a westward host went
    # red. CI runs on UTC, exactly where the single-stamp version proves nothing.
    #
    # The PAIR pins it on a host at any UTC offset — both stamps sit 10 minutes either side of
    # UTC now, far outside any real offset's ability to flip a verdict by accident:
    #   * naive UTC now+10min  -> True  as UTC; read as local on any EAST host (e.g. +05:30)
    #                                   it shifts into the PAST -> False.
    #   * naive UTC now-10min  -> False as UTC; read as local on any WEST host (e.g. -08:00)
    #                                   it shifts into the FUTURE -> True.
    # So either stamp goes red the moment it is read as local time anywhere off UTC.
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
    # FAIL CLOSED on the OTHER side too: a year-9999 stamp is perfectly parseable, so a bare
    # `deadline > now` check would hand out a reprieve measured in millennia — reached THROUGH
    # the parse instead of around it. The window is bounded both ways:
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
    whole cycle and every later user in SCAN order went unreconciled, silently — SCAN order is
    not stable enough to notice the same victims twice.

    The reachable case is an ARM throttle: `attach_existing`'s liveness confirmation raises
    `AcaError`, which is NOT a `SandboxError` and so escapes every handler on the path.

    Mutation-check: drop the per-user `try/except` in `sweep_all` and this goes red."""
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


# --- The wall-clock liveness lease --------------------------------
#
# The lock+heartbeat pair is a FACADE in both directions: a crashed builder leaves it
# standing, and a live one loses its heartbeat 90 seconds in. The lease is the one input
# here that is readable from a process NOT running the build. The two behaviours below are
# opposite ON PURPOSE — a timer must be conservative, a request path decisive — and the pair
# is the regression guard against collapsing them into one.


async def _hold_a_lease(redis: aioredis.Redis, user: uuid.UUID) -> None:
    """Exactly what a mid-build turn's renewal task leaves in the store."""
    assert await locks.renew_liveness_lease(redis, user) is True
    assert await locks.liveness_lease_is_held(redis, user) is True


async def test_the_sweep_spares_a_container_whose_turn_holds_a_lease(
    fake_redis: aioredis.Redis,
) -> None:
    # ★ A claimed container mid-build, with the lock AND the heartbeat both already
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
    # ★ THE CRASHED-TAB LOCKOUT, in new clothes: a turn killed mid-build leaves a live lease
    # behind, and if reconcile-on-start honoured it the SAME builder's next start would 409
    # until the TTL expired — the exact lockout reconcile-on-start exists to prevent.
    #
    # It has to be DELETED, not merely ignored: a lease left in place would keep sparing the
    # container from the background sweep after the reconcile had already torn it down.
    #
    # Mutation-checked, honest result recorded: this test does NOT go red when the
    # certified-dead delete is removed, because the reap that follows clears the lease anyway
    # one step later. The two tests below cover that call directly (the stray-lease case and
    # the survives-the-delete case) — all three exist because the pre-existing
    # `..._reaps_through_a_lingering_lock_and_heartbeat` test seeds only lock+heartbeat and
    # would stay green through every one of these regressions.
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
    # declines to read it; the delete alone hides the held-lease case from every other test
    # here. So the delete is neutered for this one test, leaving the `not certified_dead`
    # guard as the only thing standing between the builder and a 409 that lasts until the TTL.
    #
    # Not a hypothetical pair of belts: the lease has a RENEWAL LOOP behind it, so a zombie
    # task re-writing the key between the delete and the read would restore exactly this
    # state — and the reaper would then refuse to reclaim a slot it has already certified
    # nobody is using.
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
    """★ `certified_dead=True` is a CALLER ASSERTION resting on single-replica deploy — the
    premise a worker removes; a worker passing it would reap live builds while their owners
    watched them die. Asserted on the SOURCE, not by calling anything, because the property
    is "no such call exists" with no runtime moment to observe it.

    RECURSIVE ON PURPOSE: a non-recursive glob let a worker organised as a subpackage
    (`src/workers/reclamation/tasks.py`) carry the flag with `assert modules` still satisfied
    by files beside it. PARSES RATHER THAN GREPS for the mirror reason: a substring scan would
    fire on this very docstring's warning and train people to delete the warning instead."""
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


# --- The pre-adopt window, and the one signal that can cover it ------------------
#
# A turn's life splits into three intervals, each needing a signal that can ACTUALLY BE HELD
# in it — checkable only interval by interval:
#
#   1. claim → a registry hash exists. Nothing can be written here and nothing needs to be:
#      `reconcile_user` returns above without reaping when there is no registry, and both write
#      primitives refuse in this window on purpose (a lease written for a user with no record
#      would spare whatever container that user gets NEXT).
#   2. registry hash → adopt-and-seed-the-heartbeat. THIS ONE. The lock/heartbeat disjunct is
#      an AND, so lock-held-with-no-heartbeat is reapable; the starting marker below spans
#      exactly this interval.
#   3. adopt → terminal. The liveness lease, covered above and in `test_liveness_lease.py`.


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


# --- the serving proof, watched out of turn ---------------------------------------------------
#
# Every OTHER observer of `serving_since` lives inside a turn: the engine's `_watch_preview` is
# created when a turn starts streaming and cancelled at its terminal. The common shape is the
# opposite of that — the build finishes, the turn ends, the citizen keeps using the app, and THEN
# the dev server dies. Nothing would retract the stamp, the registry hash is the one family with
# no TTL, and "your app is running" would decay from "it is serving" into "it served once, ever"
# while the pane framed nginx's app-gone page: the measured 2026-09-10 defect, one door down.
#
# So the sweep watches too, LEVEL-TRIGGERED — it reads what the container is doing right now and
# makes the stamp agree, in both directions. It is also the whole remedy for a browser tab that
# has no button and no idea anything is wrong, which is why it ships WITH this change rather
# than as a later hardening.
#
# WHAT IT MAY NEVER DO IS DECIDE ANYTHING. A container that has not yet served is not therefore
# reapable, so every test below asserts the reap verdict beside the stamp — a section that
# checked only stamps would stay green on a sweep that had started destroying containers for
# failing to serve fast enough.


class _ProbeableClient(FakeSandboxClient):
    """A container the reaper's ladder can actually reach, with a scripted supervisor answer.

    `attach_existing` is COUNTED because the probe is the only thing in `reconcile_user` that
    attaches at all — so an empty `attached_as` is the assertion "no probe was taken", which is
    what the pre-cutover and age-gate arms are about. Reachability is opt-in: leave
    `attach_handle` None and this fake spells the container it cannot reach, which is the arm
    that must change nothing."""

    def __init__(
        self,
        *,
        running: bool = True,
        ready: bool = True,
        reachable: bool = True,
        root_status: int | None = None,
    ) -> None:
        super().__init__()
        self.attached_as: list[str] = []
        self.scripted = DevStatus(
            running=running,
            ready=ready,
            port=3000,
            # 137 is the OOM killer's signature, and the field is what `app_serving_lost` carries.
            exit_code=None if running else 137,
            # `None` BY DEFAULT, AND THAT IS THE READING EVERY TEST ABOVE WAS WRITTEN UNDER — not
            # a neutral placeholder. `shows_a_page` grandfathers an absent `root_status` as a page
            # (a sandbox image built before the field cannot answer, and refusing to frame the
            # whole existing fleet is worse than the window it would close), so leaving this out
            # keeps `shows_a_page` exactly equal to `ready` and every assertion in this section
            # goes on asserting what it asserted.
            #
            # AND THAT EQUALITY IS PRECISELY WHY THE KNOB HAD TO EXIST. While it did not, no
            # reading in this file could tell the sweep's page gate apart from the fail-open
            # `ready` it replaced — the gate could be reverted to `if not status.ready` and all
            # 65 tests here stayed green. A test that means the page-less reading now says
            # `root_status=404` out loud.
            root_status=root_status,
        )
        if reachable:
            self.attach_handle = SandboxHandle(
                fqdn=f"{SBX}.example",
                token="tok",  # noqa: S106 - a fake, never a real bearer
                app_name=SBX,
                preview_url=f"https://{SBX}.example/",
                ready=ready,
            )

    async def attach_existing(self, user_id: str) -> SandboxHandle:
        self.attached_as.append(user_id)
        return await super().attach_existing(user_id)

    async def dev_status(self, handle: SandboxHandle) -> DevStatus:
        return self.scripted


async def _stamp(redis: aioredis.Redis, user: uuid.UUID) -> str | None:
    reg = await locks.read_registry(redis, user)
    return None if reg is None else reg.get(REGISTRY_FIELD_SERVING_SINCE)


async def test_the_sweep_stamps_a_container_whose_in_turn_watchers_were_all_lost(
    fake_redis: aioredis.Redis,
) -> None:
    """★ THE BACKSTOP. An API process restarted mid-wait, so every observer that could have
    taken this container's first serve is gone — and the container is serving perfectly well.
    Without this the citizen's pane sits on "getting your app ready" over a working app with no
    button to press, in this tab and in every already-loaded one.

    Spared by the lock/heartbeat arm on the way in, and STILL spared on the way out: the
    observation is an observation."""
    await _seed(fake_redis, USER, serving_since="")
    client = _ProbeableClient()

    reaped = await reaper.reconcile_user(fake_redis, USER, client, has_live_session=False)

    assert reaped is False, "an observation must never become a verdict"
    assert client.torn_down == []
    stamped = await _stamp(fake_redis, USER)
    assert stamped is not None and stamped != ""
    assert datetime.fromisoformat(stamped) <= datetime.now(UTC)


async def test_a_container_that_still_answers_nothing_is_not_therefore_reapable(
    fake_redis: aioredis.Redis,
) -> None:
    """THE DISCRIMINATOR for the test above, and the rule this whole section is written under.
    Same spared arm, same probe, but the app is not answering yet: the stamp stays empty AND the
    container stays exactly where it was. A build that is merely slow is not a container to
    destroy."""
    await _seed(fake_redis, USER, serving_since="")
    client = _ProbeableClient(running=True, ready=False)

    reaped = await reaper.reconcile_user(fake_redis, USER, client, has_live_session=False)

    assert reaped is False
    assert client.torn_down == []
    assert await _stamp(fake_redis, USER) == ""
    assert await locks.read_registry(fake_redis, USER) is not None


async def test_the_sweep_will_not_stamp_a_root_that_answers_without_a_page(
    fake_redis: aioredis.Redis,
) -> None:
    """★ THE BACKSTOP HOLDS THE SAME BAR AS EVERY OTHER OBSERVER, and nothing in this file said
    so until this test did. The dev server is up and answering on every probe — `ready` is True —
    and the app root is 404ing because the agent has not written `app/page.tsx` yet. That is not
    a serve, and a proof minted here is worse than one minted anywhere else: the four in-turn
    observers all refuse this reading, so a sweep that accepted it would quietly overturn their
    refusal five minutes later, on a container nothing has ever watched paint. The citizen's pane
    flips to RUNNING and frames the blank white document measured on 2026-09-10.

    THE SECOND HALF IS NOT DECORATION. An absence assertion goes green just as readily when the
    stamp arm is dead as when it correctly declined — and the arm really can be dead here, since
    the age gate, the `ready` state and the empty sentinel all have to line up before a probe is
    taken at all. Flipping the same container's root to 200 and sweeping again proves the sweep
    was live the whole time and was refusing this reading specifically.

    Mutation check: revert the gate to `if not status.ready:` and the first half goes red. That
    mutant survived all 65 tests in this file before this one existed."""
    await _seed(fake_redis, USER, serving_since="")
    client = _ProbeableClient(root_status=404)

    reaped = await reaper.reconcile_user(fake_redis, USER, client, has_live_session=False)

    assert reaped is False, "an observation must never become a verdict"
    assert client.torn_down == [], (
        "a container with nothing to show yet is not a container to destroy"
    )
    assert await _stamp(fake_redis, USER) == "", "a 404 at the root was recorded as a serve"

    # The agent writes the page. Same container, same registry record, same sweep — the stamp arm
    # is still open because the sentinel is still empty, and only the reading has changed.
    client.scripted = replace(client.scripted, root_status=200)

    assert await reaper.reconcile_user(fake_redis, USER, client, has_live_session=False) is False
    stamped = await _stamp(fake_redis, USER)
    assert stamped is not None and stamped != "", (
        "the sweep's stamp arm was never live, so the refusal above proved nothing"
    )


async def test_a_container_the_sweep_has_already_condemned_is_never_asked_anything(
    fake_redis: aioredis.Redis,
) -> None:
    """THE STRONGEST FORM OF "AN OBSERVATION IS NEVER AN INPUT": on the arm that REAPS, no
    observation is taken at all. There is nothing for a verdict to be influenced by, because
    there is no reading — and a container whose lock, heartbeat, lease, marker and stay have all
    lapsed is reaped on exactly the evidence it always was.

    It also matters for the sweep's cost: the probe is bounded per SPARED user, and a fleet of
    dead containers must not turn the reap into a queue of attach timeouts."""
    await _seed(fake_redis, USER, with_lock=False, with_heartbeat=False, serving_since="")
    client = _ProbeableClient()

    assert await reaper.reconcile_user(fake_redis, USER, client, has_live_session=False) is True
    assert SBX in client.torn_down
    assert client.attached_as == [], "the reaping arm stopped to take a reading"


async def test_the_sweep_retracts_the_proof_of_an_app_that_has_stopped_answering(
    fake_redis: aioredis.Redis,
) -> None:
    """★ THE OTHER DIRECTION, and the one no in-turn observer can reach: the turn ended hours
    ago and the dev server has since died. The proof comes off, the pane falls back to a wait,
    and nothing is torn down — retracting a claim costs the citizen a card, destroying a
    container costs them their work.

    RETRACTED TO THE SENTINEL, NEVER DELETED: an absent field is the pre-cutover reading and is
    grandfathered as PROVEN, so a delete here would turn a dead app back into a running one."""
    await _seed(fake_redis, USER, serving_since="2026-09-10T09:41:04+00:00")
    client = _ProbeableClient(running=False, ready=False)

    reaped = await reaper.reconcile_user(fake_redis, USER, client, has_live_session=False)

    assert reaped is False
    assert client.torn_down == []
    assert await fake_redis.hexists(registry_key(USER), REGISTRY_FIELD_SERVING_SINCE) == 1
    assert await _stamp(fake_redis, USER) == ""


async def test_a_dead_child_that_is_still_serving_keeps_its_proof(
    fake_redis: aioredis.Redis,
) -> None:
    """`running=False, ready=True` is a DOCUMENTED NORMAL state, not a crash: the open sandbox
    lets the agent `pkill` our child and `nohup` its own replacement, which then answers the
    port. `ready` means a request to the app root actually succeeded, so retracting on `running`
    alone would take the frame away from exactly the apps that are working.

    Mutation-check: relax the guard to `if status.running: return` and this goes red."""
    await _seed(fake_redis, USER, serving_since="2026-09-10T09:41:04+00:00")
    client = _ProbeableClient(running=False, ready=True)

    assert await reaper.reconcile_user(fake_redis, USER, client, has_live_session=False) is False
    assert await _stamp(fake_redis, USER) == "2026-09-10T09:41:04+00:00"


async def test_a_container_that_cannot_be_reached_keeps_its_standing_proof(
    fake_redis: aioredis.Redis,
) -> None:
    """★ UNREACHABLE IS NOT DEAD, and that asymmetry is the whole safety of the retraction. An
    expired ARM credential or a throttled subscription makes every container in the fleet
    unaskable at once; a probe that read silence as death would retract every standing proof in
    the platform and drop every pane back to "getting your app ready" over apps that are serving
    perfectly well."""
    await _seed(fake_redis, USER, serving_since="2026-09-10T09:41:04+00:00")
    client = _ProbeableClient(reachable=False)

    assert await reaper.reconcile_user(fake_redis, USER, client, has_live_session=False) is False
    assert await _stamp(fake_redis, USER) == "2026-09-10T09:41:04+00:00"


async def test_a_pre_cutover_record_is_left_exactly_as_it_is_and_costs_no_probe(
    fake_redis: aioredis.Redis,
) -> None:
    """ABSENT IS NOT EMPTY. A hash written before the field existed says nothing either way, so
    there is nothing to prove and nothing to retract — and asking its container would spend an
    attach plus a supervisor round trip per spared user per sweep to learn nothing.

    Mutation-check: read a missing field as the empty sentinel and `attached_as` stops being
    empty."""
    await _seed(fake_redis, USER)  # the pre-cutover shape: no stamp at all
    client = _ProbeableClient()

    assert await reaper.reconcile_user(fake_redis, USER, client, has_live_session=False) is False
    assert client.attached_as == [], "a pre-cutover record was probed for no reason"
    assert await fake_redis.hexists(registry_key(USER), REGISTRY_FIELD_SERVING_SINCE) == 0


async def test_a_container_younger_than_the_cold_budget_is_left_to_its_own_observers(
    fake_redis: aioredis.Redis,
) -> None:
    """The age gate, and it exists so the sweep does not race a watcher that is already on this.
    Below the cold-start budget somebody is plausibly still watching, and THEIR stamp is the
    honest one — it carries the instant they saw, not the instant a sweep happened to look.

    Mutation-check: delete the `created_at` age gate and `attached_as` stops being empty."""
    await _seed(fake_redis, USER, serving_since="", created_at=datetime.now(UTC).isoformat())
    client = _ProbeableClient()

    assert await reaper.reconcile_user(fake_redis, USER, client, has_live_session=False) is False
    assert client.attached_as == []
    assert await _stamp(fake_redis, USER) == ""


async def test_a_container_the_reaper_has_marked_ending_is_never_stamped(
    fake_redis: aioredis.Redis,
) -> None:
    """A teardown is already committed for this one, so a proof written here would hand the pane
    a running app for a container that is about to stop existing."""
    await _seed(fake_redis, USER, serving_since="", state=REGISTRY_STATE_ENDING)
    client = _ProbeableClient()

    await reaper.reconcile_user(fake_redis, USER, client, has_live_session=False)

    assert client.attached_as == []
    assert await _stamp(fake_redis, USER) == ""


async def test_every_sparing_arm_that_holds_a_record_takes_the_reading(
    fake_redis: aioredis.Redis,
) -> None:
    """FOUR ARMS, ONE BEHAVIOUR. The liveness lease, the start-in-flight marker, the
    lock/heartbeat pair and the stay of execution each spare a container while holding its
    record, and a stranded container has to un-stick through whichever one answered first —
    otherwise the backstop covers only the citizens whose state happens to take the right
    branch, which is nobody's idea of a backstop.

    The fifth arm, `has_live_session`, is deliberately NOT one of them and is asserted here as
    the exception rather than left to be inferred from silence: it returns above the registry
    read, so observing there would cost every build start an extra round trip, and it is never
    True on the scheduled sweep this backstop exists to run on."""
    lease_user, marker_user, pair_user, stay_user, session_user = (uuid.uuid4() for _ in range(5))

    await _seed(fake_redis, lease_user, with_lock=False, with_heartbeat=False, serving_since="")
    await _hold_a_lease(fake_redis, lease_user)

    await _seed(fake_redis, marker_user, with_lock=True, with_heartbeat=False, serving_since="")
    await locks.write_starting_marker(fake_redis, marker_user, uuid.uuid4())

    await _seed(fake_redis, pair_user, serving_since="")  # lock + heartbeat, the default

    await _seed(fake_redis, stay_user, with_lock=False, with_heartbeat=False, serving_since="")
    await fake_redis.hset(
        registry_key(stay_user),
        REGISTRY_FIELD_PREVIEW_STAY_UNTIL,
        _in(RELAUNCH_PREVIEW_STAY_SECONDS),
    )

    await _seed(fake_redis, session_user, serving_since="")

    proven: dict[str, bool] = {}
    for name, user, honor_stay in (
        ("lease", lease_user, False),
        ("marker", marker_user, False),
        ("lock+heartbeat", pair_user, False),
        ("stay", stay_user, True),
    ):
        client = _ProbeableClient()
        spared = await reaper.reconcile_user(
            fake_redis, user, client, has_live_session=False, honor_stay=honor_stay
        )
        assert spared is False, f"the {name} arm stopped sparing"
        assert client.torn_down == []
        proven[name] = bool(await _stamp(fake_redis, user))

    live = _ProbeableClient()
    assert (
        await reaper.reconcile_user(fake_redis, session_user, live, has_live_session=True) is False
    )

    assert proven == {"lease": True, "marker": True, "lock+heartbeat": True, "stay": True}
    assert live.attached_as == [], "the in-process-session arm returns above the record"
    assert await _stamp(fake_redis, session_user) == ""


async def test_a_teardown_of_a_container_that_never_served_anybody_sounds_the_alarm(
    fake_redis: aioredis.Redis,
) -> None:
    """THE INVERSE OF THE SHIPPED BUG. The stamp stops the platform reporting a scheduled
    container as running; this catches the other failure — an app that never answered anything
    at all — which is today indistinguishable in the logs from a flawless build.

    Read off the record while it is still in hand: `reg` was taken before the mark-ending flip
    and the delete is about to remove it for good."""
    await _seed(fake_redis, USER, with_lock=False, with_heartbeat=False, serving_since="")

    with structlog.testing.capture_logs() as logs:
        assert await reaper.reap_user(fake_redis, USER, FakeSandboxClient()) is True

    assert [e for e in logs if e.get("event") == SERVING_PROOF_NEVER_ARRIVED]


async def test_a_teardown_of_a_container_that_did_serve_is_silent(
    fake_redis: aioredis.Redis,
) -> None:
    """THE DISCRIMINATOR, and without it the alarm above would be satisfied by a line on every
    teardown — an alarm that fires on the happy path is noise, and noise is what an operator
    learns to filter.

    The pre-cutover reading is the second silent case, for a different reason: a record written
    before the field existed says nothing about whether that container served, so alarming on
    its silence would page somebody once for every container alive at deploy time."""
    served_user, pre_cutover_user = uuid.uuid4(), uuid.uuid4()
    await _seed(
        fake_redis,
        served_user,
        with_lock=False,
        with_heartbeat=False,
        serving_since="2026-09-10T09:41:04+00:00",
    )
    await _seed(fake_redis, pre_cutover_user, with_lock=False, with_heartbeat=False)

    with structlog.testing.capture_logs() as logs:
        assert await reaper.reap_user(fake_redis, served_user, FakeSandboxClient()) is True
        assert await reaper.reap_user(fake_redis, pre_cutover_user, FakeSandboxClient()) is True

    assert [e for e in logs if e.get("event") == SERVING_PROOF_NEVER_ARRIVED] == []


# --- the fleet sweep's re-ask cadence ---------------------------------------------------------
#
# THE COST THIS GUARDS. A record that is READY and already carries a real stamp is the STEADY
# STATE of every healthy preview, so the "is it still serving?" arm matches essentially every
# live container on every pass. `sweep_all` walks its users one at a time, and each probe is an
# `attach_existing` plus a `dev_status` against a real sandbox — so asking every live preview
# every five minutes turns a Redis-only pass into N serial container round trips, and a sweep
# that overruns its own cadence starves the reap. These pin the thinning that stops that, and
# they pin it WITHOUT wall-clock luck: `now` is passed in, never read from the clock.


def _a_proven_record() -> dict[str, str]:
    """READY, stamped, spared — the shape every healthy preview holds between turns."""
    return {
        REGISTRY_FIELD_STATE: REGISTRY_STATE_READY,
        REGISTRY_FIELD_APP_NAME: SBX,
        REGISTRY_FIELD_CREATED_AT: "2026-09-10T09:00:00+00:00",
        REGISTRY_FIELD_SERVING_SINCE: "2026-09-10T09:41:04+00:00",
    }


def test_reconcile_on_start_re_asks_every_time_it_is_asked() -> None:
    """★ THE UNTHINNED PATH, and it is the one a citizen is waiting on.

    Reconcile-on-start is about ONE user who just pressed something, so there is no fleet to
    multiply and the answer is about to decide what they see. It pays the probe on every pass,
    whichever shard the user falls in — so this asserts the arm over a full cycle of passes.
    """
    reg = _a_proven_record()
    for pass_offset in range(reaper._RE_ASK_A_PROVEN_STAMP_EVERY_N_SWEEPS):
        now = datetime.fromtimestamp(pass_offset * reaper._SWEEP_CADENCE_SECONDS, tz=UTC)
        assert (
            reaper._what_this_record_is_missing(reg, now, USER, thin_the_re_ask=False) == "retract"
        ), f"the on-start path declined to re-ask on pass {pass_offset}"


def test_the_fleet_sweep_re_asks_each_proven_container_exactly_once_per_cycle() -> None:
    """★ THE THINNED PATH: over one full cycle of passes a given user comes up exactly ONCE.

    Exactly once is the whole property — never (the crash is never noticed out of turn) and more
    than once (the cost is not actually thinned) are both failures, so both are caught by the
    same equality rather than by a `>= 1`.
    """
    reg = _a_proven_record()
    for user in (USER, uuid.uuid4(), uuid.uuid4()):
        asked = [
            pass_offset
            for pass_offset in range(reaper._RE_ASK_A_PROVEN_STAMP_EVERY_N_SWEEPS)
            if reaper._what_this_record_is_missing(
                reg,
                datetime.fromtimestamp(pass_offset * reaper._SWEEP_CADENCE_SECONDS, tz=UTC),
                user,
                thin_the_re_ask=True,
            )
            == "retract"
        ]
        assert len(asked) == 1, f"{user} came up on passes {asked}, not exactly one"


def test_the_thinning_spreads_the_fleet_across_passes_instead_of_spiking() -> None:
    """★ WHY IT IS SHARDED BY USER AND NOT BY THE STAMP'S AGE.

    An `age % 30min` gate would have been simpler and wrong in a way no single-user test would
    show: every container stamped in the same busy minute — which is what a morning's builds look
    like — would come due in the same minute forever after, so the cost would arrive as one spike
    per cycle rather than being thinned at all. Sharding by user id spreads it: on any ONE pass,
    a fleet lands in every shard, so no pass carries the whole fleet.
    """
    reg = _a_proven_record()
    fleet = [uuid.uuid4() for _ in range(600)]
    now = datetime.fromtimestamp(0, tz=UTC)
    asked = [
        u
        for u in fleet
        if reaper._what_this_record_is_missing(reg, now, u, thin_the_re_ask=True) == "retract"
    ]
    # A sixth of six hundred is a hundred; the bound is loose enough not to be a coin-flip test
    # and tight enough that "the whole fleet on one pass" and "nobody, ever" both fail it.
    assert 40 < len(asked) < 200, f"{len(asked)} of {len(fleet)} on a single pass"


def test_thinning_never_reaches_the_arm_that_stamps_an_unproven_container() -> None:
    """The thinning is for the RE-ask only. A container that has never served is a citizen
    staring at a wait card with no observer left alive, and it is asked on every pass it is
    eligible for — being made to wait another half hour for the answer is the opposite of the
    fix. Its own gate is the 120-second `_NOBODY_IS_COMING_AFTER_SECONDS` age, not this shard."""
    unproven = _a_proven_record() | {REGISTRY_FIELD_SERVING_SINCE: ""}
    old_enough = datetime.fromisoformat("2026-09-10T09:00:00+00:00") + timedelta(seconds=300)
    for pass_offset in range(reaper._RE_ASK_A_PROVEN_STAMP_EVERY_N_SWEEPS):
        now = old_enough + timedelta(seconds=pass_offset * reaper._SWEEP_CADENCE_SECONDS)
        assert (
            reaper._what_this_record_is_missing(unproven, now, USER, thin_the_re_ask=True)
            == "stamp"
        ), f"an unproven container was thinned out of its own stamp on pass {pass_offset}"
