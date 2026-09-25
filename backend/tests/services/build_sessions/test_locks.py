"""The lock / heartbeat / registry-state primitives (deterministic fakeredis)."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime

import pytest
import redis.asyncio as aioredis
from redis.exceptions import RedisError

from src.api.v1.build_sessions.schemas import (
    HEARTBEAT_TTL_SECONDS,
    LOCK_RENEW_CADENCE_SECONDS,
    LOCK_TTL_SECONDS,
    STARTING_MARKER_TTL_SECONDS,
)
from src.services.build_sessions import locks
from src.services.redis import (
    REGISTRY_STATE_ENDING,
    REGISTRY_STATE_READY,
    heartbeat_key,
    registry_key,
)
from src.services.redis.keys import (
    REGISTRY_FIELD_APP_NAME,
    REGISTRY_FIELD_SERVING_SINCE,
    REGISTRY_FIELD_STATE,
    starting_key,
)
from tests.fakes import a_sandbox_name

USER = uuid.uuid4()
OTHER = uuid.uuid4()

#: A container name the platform could actually have MINTED — `sbx-` + 28 lowercase hex.
SBX = a_sandbox_name("locks")


async def test_acquire_is_exclusive_per_user(fake_redis: aioredis.Redis) -> None:
    token = await locks.acquire_lock(fake_redis, USER)
    assert token is not None
    # A second acquire for the same user is refused (the NX one-per-user enforcement).
    assert await locks.acquire_lock(fake_redis, USER) is None
    assert await locks.acquire_lock(fake_redis, OTHER) is not None


async def test_holder_release_is_compare_and_delete(fake_redis: aioredis.Redis) -> None:
    token = await locks.acquire_lock(fake_redis, USER)
    assert token is not None
    assert await locks.release_lock_as_holder(fake_redis, USER, "not-the-token") is False
    assert await locks.lock_is_held(fake_redis, USER) is True
    assert await locks.release_lock_as_holder(fake_redis, USER, token) is True
    assert await locks.lock_is_held(fake_redis, USER) is False


async def test_renew_extends_on_match_and_signals_lost_on_mismatch(
    fake_redis: aioredis.Redis,
) -> None:
    token = await locks.acquire_lock(fake_redis, USER)
    assert token is not None
    assert await locks.renew_lock(fake_redis, USER, token) is True
    assert await locks.renew_lock(fake_redis, USER, "stale-token") is False  # lock lost


async def test_heartbeat_sets_ttl_and_reports_alive(fake_redis: aioredis.Redis) -> None:
    expires = await locks.write_heartbeat(fake_redis, USER)
    assert await locks.heartbeat_is_alive(fake_redis, USER) is True
    ttl = await fake_redis.ttl(heartbeat_key(USER))
    assert 0 < ttl <= HEARTBEAT_TTL_SECONDS
    assert expires > datetime.now(UTC)


def test_lock_ttl_has_renew_headroom() -> None:
    # A renew always has head-room, so an active build never drops its lock at the cadence.
    assert LOCK_TTL_SECONDS == 900
    assert LOCK_TTL_SECONDS > LOCK_RENEW_CADENCE_SECONDS


async def test_reap_lock_reclaims_a_drifted_lock(fake_redis: aioredis.Redis) -> None:
    await locks.acquire_lock(fake_redis, USER)  # a crashed session's token — not ours
    assert await locks.lock_is_held(fake_redis, USER) is True
    assert await locks.reap_lock(fake_redis, USER) is True  # value-guarded reclaim
    assert await locks.lock_is_held(fake_redis, USER) is False
    assert await locks.reap_lock(fake_redis, USER) is False


async def test_acquire_fails_closed_on_redis_error(
    fake_redis: aioredis.Redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Fail CLOSED, but SAY WHICH KIND of closed.

    The fail-closed half is non-negotiable: a Redis error never hands out a token. `None` is
    reserved for the one certain answer — "the lock is genuinely held" — because the caller
    turns that into a 409 naming a live build session. Folding an outage into the same `None`
    is what made every Redis blip surface as "a build session is already active" for a user who
    had none."""
    # `LockUnavailableError` SUBCLASSES `RedisError` (the additive `StorageUnconfiguredError`
    # shape), so a caller that only knows `except RedisError` still catches it.

    async def boom(*args: object, **kwargs: object) -> object:
        raise RedisError("redis is down")

    monkeypatch.setattr(fake_redis, "set", boom)
    with pytest.raises(locks.LockUnavailableError) as caught:
        await locks.acquire_lock(fake_redis, USER)
    assert isinstance(caught.value, RedisError)  # every existing `except RedisError` still hits
    assert "redis is down" not in str(caught.value)  # no store detail on the way out


async def test_acquire_none_is_reserved_for_a_lock_that_is_genuinely_held(
    fake_redis: aioredis.Redis,
) -> None:
    # The other half of the split, pinned at the same seam: with a HEALTHY Redis, a
    # refused acquire still returns `None` — so the 409 the manager builds from it always
    # describes a real holder. Without this, "raise on error" could be satisfied by raising
    # on everything.
    assert await locks.acquire_lock(fake_redis, USER) is not None
    assert await locks.acquire_lock(fake_redis, USER) is None


async def _boom(*args: object, **kwargs: object) -> object:
    raise RedisError("redis is down")


async def _cancelled(*args: object, **kwargs: object) -> object:
    raise asyncio.CancelledError


async def test_the_one_guard_never_swallows_cancellation(
    fake_redis: aioredis.Redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`acquire_lock` is the single primitive here that catches, and it catches NARROWLY.
    `CancelledError` is a `BaseException`, so it passes through untouched — load-bearing on
    a ~23-minute build endpoint, where a guard widened to `except Exception` would eat a
    cancellation and wedge the task instead of unwinding it. It would also silently convert
    a cancel into a fail-closed `None`, i.e. a phantom "lock already held"."""
    monkeypatch.setattr(fake_redis, "set", _cancelled)
    with pytest.raises(asyncio.CancelledError):
        await locks.acquire_lock(fake_redis, USER)


@pytest.mark.parametrize(
    ("method", "call"),
    [
        ("eval", lambda r: locks.renew_lock(r, USER, "a-token")),
        ("eval", lambda r: locks.release_lock_as_holder(r, USER, "a-token")),
        ("set", lambda r: locks.write_heartbeat(r, USER)),
        ("get", lambda r: locks.reap_lock(r, USER)),
        ("exists", lambda r: locks.lock_is_held(r, USER)),
        ("hgetall", lambda r: locks.read_registry(r, USER)),
        ("exists", lambda r: locks.mark_registry_ending(r, USER)),
        ("delete", lambda r: locks.delete_registry(r, USER)),
        # Both serving-proof writes are answer-bearing in the same way `renew_lock` is: their
        # `False` means "the store said no", and a swallowed error would hand the caller a
        # REFUSAL that never happened — which `SERVING_PROOF_STAMP_REFUSED` is the alarm for.
        # An alarm raised on an outage is an alarm nobody can act on.
        ("eval", lambda r: locks.mark_serving(r, USER, app_name=SBX, when=datetime.now(UTC))),
        ("eval", lambda r: locks.clear_serving(r, USER, app_name=SBX)),
    ],
)
async def test_every_primitive_but_acquire_still_surfaces_redis_errors(
    fake_redis: aioredis.Redis,
    monkeypatch: pytest.MonkeyPatch,
    method: str,
    call: Callable[[aioredis.Redis], Awaitable[object]],
) -> None:
    """`acquire_lock` is the ONLY primitive in this module that catches `RedisError`. This
    pins every other one bare, so no future guard can be added silently.

    Two distinct reasons live in this one list. Most entries are ANSWER-BEARING, and a swallow
    would manufacture a false answer out of an ambiguous store: `lock_is_held` False is
    fail-OPEN, `read_registry` None is a phantom "no sandbox", `renew_lock` False is a phantom
    "lock lost" that ends a healthy build, and a swallowed `mark_registry_ending` lets a
    concurrent attach reconnect to a container the reaper is about to delete."""
    # `release_lock_as_holder` and `write_heartbeat` are here for a DIFFERENT reason, and it is
    # the one that is easy to get wrong: they look like compensation paths that deserve a guard,
    # but every caller that wants one already guards at the call site
    # (`_compensate_lock_and_container`, `_pardon_the_container`), and inside
    # `_holding_user_lock` / `relaunch_preview` the raise is precisely what triggers that
    # compensation. See the two `test_relaunch_spares_the_container_when_*` tests in
    # `test_manager.py`, which pin that behaviour end to end.
    monkeypatch.setattr(fake_redis, method, _boom)
    with pytest.raises(RedisError):
        await call(fake_redis)


async def test_registry_state_helpers(fake_redis: aioredis.Redis) -> None:
    # mark_ending on an absent registry never conjures a partial hash.
    await locks.mark_registry_ending(fake_redis, USER)
    assert await locks.read_registry(fake_redis, USER) is None

    await fake_redis.hset(
        registry_key(USER),
        mapping={REGISTRY_FIELD_APP_NAME: "sbx-x", REGISTRY_FIELD_STATE: "ready"},
    )
    await locks.mark_registry_ending(fake_redis, USER)
    reg = await locks.read_registry(fake_redis, USER)
    assert reg is not None
    assert reg[REGISTRY_FIELD_STATE] == REGISTRY_STATE_ENDING

    await locks.delete_registry(fake_redis, USER)
    assert await locks.read_registry(fake_redis, USER) is None


# --- the serving proof --------------------------------------------------------------
# `mark_serving` / `clear_serving` are the ONLY writers of `serving_since`, the one field on the
# registry hash that means the app ANSWERED a request rather than that a container was
# SCHEDULED. Everything the preview pane says about a running app is downstream of them, so
# every refusal below is a real hazard rather than a defensive nicety:
#
#   * a DIFFERENT app_name — the registry key is per USER and survives a container swap, so a
#     slow observer returning after the one-per-user slot flipped would stamp "serving" onto a
#     container it never watched, and the pane would frame the new project's app on the old
#     one's evidence;
#   * `state=ending` — the reaper has already committed to destroying it;
#   * an instant already standing — FIRST SERVE WINS, or `ms_since_container_created` on the
#     `app_first_served` line stops meaning anything;
#   * no hash at all — a stamp must never CONJURE a record for a user with no sandbox.
#
# The read side of these three values lives in `test_preview_state.py`; this is the write side.


async def _a_registered_container(
    redis: aioredis.Redis,
    user: uuid.UUID,
    *,
    app_name: str = SBX,
    state: str = REGISTRY_STATE_READY,
    serving_since: str = "",
) -> None:
    """The hash as `_write_registry` leaves it — including the empty serving sentinel, which is
    what makes `HSETNX` the wrong primitive here: the field always EXISTS, so a bare `HSETNX`
    would refuse every legitimate first stamp."""
    await redis.hset(
        registry_key(user),
        mapping={
            REGISTRY_FIELD_APP_NAME: app_name,
            REGISTRY_FIELD_STATE: state,
            REGISTRY_FIELD_SERVING_SINCE: serving_since,
        },
    )


async def _stamp_on(redis: aioredis.Redis, user: uuid.UUID) -> str | None:
    reg = await locks.read_registry(redis, user)
    return None if reg is None else reg.get(REGISTRY_FIELD_SERVING_SINCE)


async def test_the_first_observer_to_watch_the_app_answer_stamps_the_instant(
    fake_redis: aioredis.Redis,
) -> None:
    await _a_registered_container(fake_redis, USER)
    when = datetime(2026, 9, 10, 9, 41, 4, tzinfo=UTC)

    assert await locks.mark_serving(fake_redis, USER, app_name=SBX, when=when) is True
    assert await _stamp_on(fake_redis, USER) == when.isoformat()


async def test_a_second_sighting_changes_nothing_because_first_serve_wins(
    fake_redis: aioredis.Redis,
) -> None:
    """Four observers can each watch the same container answer, and a crash-and-recover puts the
    turn watcher back at the same door. The field has to keep meaning "first serve" rather than
    "most recent sighting", or the operator's `ms_since_container_created` — the eight-second
    number from 2026-09-10 — silently becomes the age of the last poll."""
    await _a_registered_container(fake_redis, USER)
    first = datetime(2026, 9, 10, 9, 41, 4, tzinfo=UTC)
    later = datetime(2026, 9, 10, 9, 55, 0, tzinfo=UTC)

    assert await locks.mark_serving(fake_redis, USER, app_name=SBX, when=first) is True
    assert await locks.mark_serving(fake_redis, USER, app_name=SBX, when=later) is False
    assert await _stamp_on(fake_redis, USER) == first.isoformat(), "the instant moved"


async def test_a_stamp_aimed_at_a_container_that_no_longer_holds_the_slot_is_refused(
    fake_redis: aioredis.Redis,
) -> None:
    """★ THE NEAR-MISS THE COMPARE-AND-SET EXISTS FOR. The slot flipped to another of this
    citizen's projects between the observation and the write; `redis.exists()` would still be
    true, so an `exists`-then-`HSETNX` would stamp the successor with the predecessor's evidence
    and the pane would frame the wrong project's app.

    Mutation-check: drop the `app_name` comparison from `_CAS_MARK_SERVING_LUA` and this goes
    red on both assertions."""
    await _a_registered_container(fake_redis, USER, app_name=a_sandbox_name("successor"))

    stamped = await locks.mark_serving(fake_redis, USER, app_name=SBX, when=datetime.now(UTC))

    assert stamped is False
    assert await _stamp_on(fake_redis, USER) == "", "the successor was stamped"


async def test_a_container_the_reaper_has_marked_ending_refuses_the_stamp(
    fake_redis: aioredis.Redis,
) -> None:
    """`ending` means the teardown is already committed. A proof written here would hand the
    pane a running app for a container that is about to stop existing."""
    await _a_registered_container(fake_redis, USER, state=REGISTRY_STATE_ENDING)

    assert (
        await locks.mark_serving(fake_redis, USER, app_name=SBX, when=datetime.now(UTC)) is False
    )
    assert await _stamp_on(fake_redis, USER) == ""


async def test_a_user_with_no_sandbox_at_all_gets_no_hash_conjured(
    fake_redis: aioredis.Redis,
) -> None:
    """A `HSET` on a missing key CREATES it, which is how a stamp could invent a one-field
    registry record for a user who has no container — a record the sweep would then read, fail
    to make sense of, and act on. The `app_name` comparison is what refuses it: `HGET` on a
    missing key answers nil, which matches nothing."""
    assert (
        await locks.mark_serving(fake_redis, USER, app_name=SBX, when=datetime.now(UTC)) is False
    )
    assert await fake_redis.exists(registry_key(USER)) == 0


async def test_a_naive_instant_is_stamped_as_utc_rather_than_left_ambiguous(
    fake_redis: aioredis.Redis,
) -> None:
    """The hash outlives the process that wrote it, so a naive value on it would be ambiguous
    forever — and `ms_since_container_created` would be wrong by whatever the writer's offset
    happened to be. Read as UTC on the way in, the same defensive reading every other consumer
    of an instant on this hash takes."""
    await _a_registered_container(fake_redis, USER)
    naive = datetime(2026, 9, 10, 9, 41, 4)  # noqa: DTZ001 - the ambiguity IS the subject

    assert await locks.mark_serving(fake_redis, USER, app_name=SBX, when=naive) is True
    assert await _stamp_on(fake_redis, USER) == naive.replace(tzinfo=UTC).isoformat()


async def test_retracting_a_proof_puts_the_sentinel_back_and_never_deletes_the_field(
    fake_redis: aioredis.Redis,
) -> None:
    """★ THE RETRACTION'S WHOLE SAFETY. An ABSENT `serving_since` is the PRE-CUTOVER reading and
    is grandfathered as PROVEN, so an `HDEL` here would turn a crashed app into a running one —
    the shipped bug upside down, and on the one path whose job is to say the app has stopped.

    Mutation-check: swap the `HSET … ''` in `_CAS_CLEAR_SERVING_LUA` for an `HDEL` and the
    presence assertion goes red while the value assertion still passes."""
    await _a_registered_container(fake_redis, USER, serving_since="2026-09-10T09:41:04+00:00")

    assert await locks.clear_serving(fake_redis, USER, app_name=SBX) is True
    assert await fake_redis.hexists(registry_key(USER), REGISTRY_FIELD_SERVING_SINCE) == 1
    assert await _stamp_on(fake_redis, USER) == ""


async def test_retracting_a_proof_that_was_never_standing_reports_nothing_to_retract(
    fake_redis: aioredis.Redis,
) -> None:
    """False is what stops the crash edge logging `app_serving_lost` for a container that never
    served anybody in the first place — a loss that never happened."""
    await _a_registered_container(fake_redis, USER)

    assert await locks.clear_serving(fake_redis, USER, app_name=SBX) is False


async def test_retracting_carries_the_same_identity_guard_as_the_stamp(
    fake_redis: aioredis.Redis,
) -> None:
    """A crash observed against the container that HAS gone must not strip the proof off the
    replacement that took the slot — that would unframe a healthy app."""
    await _a_registered_container(
        fake_redis,
        USER,
        app_name=a_sandbox_name("successor"),
        serving_since="2026-09-10T10:00:00Z",
    )

    assert await locks.clear_serving(fake_redis, USER, app_name=SBX) is False
    assert await _stamp_on(fake_redis, USER) == "2026-09-10T10:00:00Z"


async def test_retracting_still_works_on_a_container_already_marked_ending(
    fake_redis: aioredis.Redis,
) -> None:
    """DELIBERATELY UNLIKE THE STAMP, which refuses on `ending`. A crash edge can be observed
    after the reaper has flipped the hash, and refusing here would leave a standing proof on a
    container being torn down — the pane framing an app that is actively being destroyed.

    Mutation-check: add the `state == ready` guard to `_CAS_CLEAR_SERVING_LUA` and this goes
    red."""
    await _a_registered_container(
        fake_redis, USER, state=REGISTRY_STATE_ENDING, serving_since="2026-09-10T10:00:00Z"
    )

    assert await locks.clear_serving(fake_redis, USER, app_name=SBX) is True
    assert await _stamp_on(fake_redis, USER) == ""


async def test_an_app_that_comes_back_can_be_proven_again(fake_redis: aioredis.Redis) -> None:
    """The retraction is RECOVERABLE, which is what makes it a card rather than a dead end: the
    field goes back to the sentinel, so the next observer to watch this app answer re-stamps it
    and the pane re-frames without anybody restarting anything."""
    await _a_registered_container(fake_redis, USER)
    died = datetime(2026, 9, 10, 9, 41, 4, tzinfo=UTC)
    recovered = datetime(2026, 9, 10, 9, 44, 0, tzinfo=UTC)

    assert await locks.mark_serving(fake_redis, USER, app_name=SBX, when=died) is True
    assert await locks.clear_serving(fake_redis, USER, app_name=SBX) is True
    assert await locks.mark_serving(fake_redis, USER, app_name=SBX, when=recovered) is True
    assert await _stamp_on(fake_redis, USER) == recovered.isoformat()


def test_neither_serving_script_names_a_registry_field_by_hand() -> None:
    """Both scripts are BUILT from the `REGISTRY_FIELD_*` constants at module scope, so renaming
    a field cannot leave a Lua string pointing at the old spelling — a drift that fails SILENTLY,
    as a stamp that never lands and a pane that never says "running".

    READ FROM THE SOURCE, NOT FROM THE ASSEMBLED STRING, and that is the whole point: the
    finished script naturally contains `serving_since`, so substituting the constants back out
    of it would erase a hand-typed literal exactly as it erases an interpolated one and the
    test would prove nothing. In the AST an interpolation is a `FormattedValue` and a
    hand-typed name is a `Constant` — which is a difference a test can actually see.

    Modelled on `test_key_migration.py::test_no_module_builds_a_sandbox_key_by_hand`, which
    greps the source for the same class of bypass one namespace up."""
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(locks))
    scripts = {"_CAS_MARK_SERVING_LUA", "_CAS_CLEAR_SERVING_LUA"}
    literals: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.AnnAssign | ast.Assign):
            continue
        targets = [node.target] if isinstance(node, ast.AnnAssign) else node.targets
        if not any(isinstance(t, ast.Name) and t.id in scripts for t in targets):
            continue
        assert node.value is not None
        literals += [
            part.value
            for part in ast.walk(node.value)
            if isinstance(part, ast.Constant) and isinstance(part.value, str)
        ]

    assert literals, "neither script was found — this probe has gone inert, not green"
    hand_typed = [
        text
        for text in literals
        for field in (
            REGISTRY_FIELD_APP_NAME,
            REGISTRY_FIELD_STATE,
            REGISTRY_FIELD_SERVING_SINCE,
        )
        if field in text
    ]
    assert hand_typed == [], (
        f"a registry field is spelled by hand inside a serving-proof Lua script: {hand_typed}. "
        f"Interpolate the REGISTRY_FIELD_* constant instead, or a rename drifts silently."
    )


# --- the start-in-flight marker -----------------------------------------------------
# `write_starting_marker` / `read_starting_marker` / `clear_starting_marker` are the
# primitives `_holding_user_lock` (manager.py) builds the `starting` fact from; this section
# pins them in isolation, the way `read_registry`/`mark_registry_ending` are pinned above.
# The pipelined read `project_preview_state` actually calls is proven end to end in
# `test_preview_state.py`; here it is pinned as a primitive only.

PROJECT = uuid.uuid4()


async def test_a_written_marker_names_the_project_and_carries_a_mandatory_ttl(
    fake_redis: aioredis.Redis,
) -> None:
    """The whole payload is the project id, and the TTL is mandatory rather than a default."""
    await locks.write_starting_marker(fake_redis, USER, PROJECT)

    assert await locks.read_starting_marker(fake_redis, USER) == PROJECT
    ttl = await fake_redis.ttl(starting_key(USER))
    assert 0 < ttl <= STARTING_MARKER_TTL_SECONDS


async def test_no_marker_reads_as_absent(fake_redis: aioredis.Redis) -> None:
    assert await locks.read_starting_marker(fake_redis, USER) is None


async def test_an_unreadable_marker_value_reads_as_absent_not_as_a_claim(
    fake_redis: aioredis.Redis,
) -> None:
    """Fails toward `None` on a value this process cannot parse — a hand-edited key or a
    future writer using a different shape — rather than treating garbage as a start in
    flight. The same fail-closed reading `liveness_lease_is_held` gives an unparseable
    deadline, applied to a marker whose only job is to answer, never to spare on the strength
    of a value nobody can account for."""
    await fake_redis.set(starting_key(USER), "not-a-uuid", ex=STARTING_MARKER_TTL_SECONDS)

    assert await locks.read_starting_marker(fake_redis, USER) is None


async def test_clearing_is_idempotent(fake_redis: aioredis.Redis) -> None:
    await locks.write_starting_marker(fake_redis, USER, PROJECT)

    await locks.clear_starting_marker(fake_redis, USER)
    assert await locks.read_starting_marker(fake_redis, USER) is None
    await locks.clear_starting_marker(fake_redis, USER)
    assert await locks.read_starting_marker(fake_redis, USER) is None


async def test_an_abandoned_marker_expires_on_its_own(fake_redis: aioredis.Redis) -> None:
    """A marker is a BOUNDED claim, not a pardon: past its TTL it stops naming anything, with
    no second actor required to clear it. fakeredis has no fast-forward clock, so the lapse is
    simulated by deleting the key directly — indistinguishable, from a reader's side, from the
    TTL having done it, which is the property this test is actually pinning."""
    await locks.write_starting_marker(fake_redis, USER, PROJECT)
    assert await locks.read_starting_marker(fake_redis, USER) == PROJECT

    await fake_redis.delete(starting_key(USER))  # simulates the TTL lapsing

    assert await locks.read_starting_marker(fake_redis, USER) is None


async def test_the_pipelined_read_returns_the_registry_the_marker_and_the_clock_in_one_trip(
    fake_redis: aioredis.Redis,
) -> None:
    """The exact reading `project_preview_state` spends its one Redis round trip on: three
    commands, not three round trips."""
    await fake_redis.hset(registry_key(USER), mapping={REGISTRY_FIELD_APP_NAME: "sbx-x"})
    await locks.write_starting_marker(fake_redis, USER, PROJECT)

    reg, starting, began_at = await locks.read_registry_and_starting_marker(fake_redis, USER)

    assert reg is not None and reg[REGISTRY_FIELD_APP_NAME] == "sbx-x"
    assert starting == PROJECT
    assert began_at is not None
    # Written a moment ago, so its full TTL is still ahead of it and the derived instant is now.
    assert abs((datetime.now(UTC) - began_at).total_seconds()) < 5


async def test_the_marker_clock_reads_the_wait_a_reload_would_have_forgotten(
    fake_redis: aioredis.Redis,
) -> None:
    """THE DEFECT THIS FIELD EXISTS FOR. The pane used to count from its own mount, so a reload
    two minutes into a start reported one second. The marker is written once per start and
    never renewed, so what is left of its TTL is the wait — and it is the same answer however
    many times the page has been reloaded on top of it.

    Ninety seconds spent is simulated by shortening the key's expiry, which is what a TTL
    decaying looks like from a reader's side."""
    await locks.write_starting_marker(fake_redis, USER, PROJECT)
    await fake_redis.expire(starting_key(USER), STARTING_MARKER_TTL_SECONDS - 90)

    _, _, began_at = await locks.read_registry_and_starting_marker(fake_redis, USER)

    assert began_at is not None
    assert 85 < (datetime.now(UTC) - began_at).total_seconds() < 95


async def test_the_marker_clock_says_nothing_when_there_is_no_marker(
    fake_redis: aioredis.Redis,
) -> None:
    """`PTTL` answers -2 for a key that is not there and -1 for one with no expiry. Neither is
    an instant, and inventing one would date a wait that is not happening."""
    await fake_redis.set(starting_key(USER), str(PROJECT))  # no expiry: PTTL answers -1

    _, starting, began_at = await locks.read_registry_and_starting_marker(fake_redis, USER)

    assert starting == PROJECT  # the marker is still a claim...
    assert began_at is None  # ...it just cannot date itself


async def test_the_pipelined_read_answers_both_absent_with_no_registry_or_marker(
    fake_redis: aioredis.Redis,
) -> None:
    reg, starting, began_at = await locks.read_registry_and_starting_marker(fake_redis, USER)
    assert reg is None
    assert starting is None
    assert began_at is None


async def test_the_pipelined_read_still_migrates_a_legacy_registry_record(
    fake_redis: aioredis.Redis,
) -> None:
    """The legacy-prefix adoption `read_registry` performs on a plain read must not be lost by
    routing through the pipeline instead."""
    from src.services.redis.keys import legacy_registry_key

    await fake_redis.hset(legacy_registry_key(USER), mapping={REGISTRY_FIELD_APP_NAME: "sbx-x"})

    reg, starting, _ = await locks.read_registry_and_starting_marker(fake_redis, USER)

    assert reg is not None and reg[REGISTRY_FIELD_APP_NAME] == "sbx-x"
    assert starting is None


class _BoomPipeline:
    """A pipeline stub whose `execute()` fails — the shape `pipe.execute()` actually takes on
    a real outage, as opposed to the single-command `fake_redis.<method> = _boom` swap the
    parametrized test above uses. Queuing methods return `self` so the fluent
    `pipe.hgetall(...).get(...)` in `read_registry_and_starting_marker` still chains."""

    def hgetall(self, *_args: object, **_kwargs: object) -> _BoomPipeline:
        return self

    def get(self, *_args: object, **_kwargs: object) -> _BoomPipeline:
        return self

    def pttl(self, *_args: object, **_kwargs: object) -> _BoomPipeline:
        return self

    async def execute(self) -> object:
        raise RedisError("redis is down")


async def test_the_pipelined_read_surfaces_redis_errors_bare(
    fake_redis: aioredis.Redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BARE, per the module's REDIS-ERROR POLICY: a `RedisError` from `pipe.execute()`
    propagates exactly as a bare `hgetall` would have, so `project_preview_state`'s existing
    `except RedisError` keeps working unchanged."""
    monkeypatch.setattr(fake_redis, "pipeline", lambda *a, **k: _BoomPipeline())
    with pytest.raises(RedisError):
        await locks.read_registry_and_starting_marker(fake_redis, USER)


async def test_write_starting_marker_surfaces_redis_errors_bare(
    fake_redis: aioredis.Redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The write must SURFACE rather than be swallowed into a silent non-start: it sits inside
    `_holding_user_lock`'s try, so raising is what reaches the compensation arm that releases
    the lock this request already holds. Swallowing it would leave a start nobody can see
    behind a lock nobody can explain. The PLACEMENT half of that contract — that the call is
    inside the try and not above it — is pinned by
    `test_manager.py::test_a_failed_starting_marker_write_leaks_no_lock`."""
    monkeypatch.setattr(fake_redis, "set", _boom)
    with pytest.raises(RedisError):
        await locks.write_starting_marker(fake_redis, USER, PROJECT)
