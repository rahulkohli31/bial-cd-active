"""U3 — the C5 lock / heartbeat / registry-state primitives (deterministic fakeredis)."""

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
from src.services.redis import REGISTRY_STATE_ENDING, heartbeat_key, registry_key
from src.services.redis.keys import REGISTRY_FIELD_APP_NAME, REGISTRY_FIELD_STATE, starting_key

USER = uuid.uuid4()
OTHER = uuid.uuid4()


async def test_acquire_is_exclusive_per_user(fake_redis: aioredis.Redis) -> None:
    token = await locks.acquire_lock(fake_redis, USER)
    assert token is not None
    # A second acquire for the same user is refused (the NX one-per-user enforcement).
    assert await locks.acquire_lock(fake_redis, USER) is None
    # A different user acquires independently.
    assert await locks.acquire_lock(fake_redis, OTHER) is not None


async def test_holder_release_is_compare_and_delete(fake_redis: aioredis.Redis) -> None:
    token = await locks.acquire_lock(fake_redis, USER)
    assert token is not None
    # A stale token never releases someone else's lock.
    assert await locks.release_lock_as_holder(fake_redis, USER, "not-the-token") is False
    assert await locks.lock_is_held(fake_redis, USER) is True
    # The holder's own token releases.
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
    assert expires > datetime.now(UTC)  # a future idle instant


def test_lock_ttl_has_renew_headroom() -> None:
    # The C3-frozen constants (900/300), NOT C5's proposed 300 s lock default: a renew
    # always has head-room, so an active build never drops its lock at the cadence.
    assert LOCK_TTL_SECONDS == 900
    assert LOCK_TTL_SECONDS > LOCK_RENEW_CADENCE_SECONDS


async def test_reap_lock_reclaims_a_drifted_lock(fake_redis: aioredis.Redis) -> None:
    await locks.acquire_lock(fake_redis, USER)  # a crashed session's token — not ours
    assert await locks.lock_is_held(fake_redis, USER) is True
    assert await locks.reap_lock(fake_redis, USER) is True  # value-guarded reclaim
    assert await locks.lock_is_held(fake_redis, USER) is False
    # A second reap on an absent lock is a clean no-op.
    assert await locks.reap_lock(fake_redis, USER) is False


async def test_acquire_fails_closed_on_redis_error(
    fake_redis: aioredis.Redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """U3 — fail CLOSED, but SAY WHICH KIND of closed.

    The fail-closed half is unchanged and non-negotiable: a Redis error never hands out a
    token. What changed is the signal. `None` is reserved for the one certain answer —
    "the lock is genuinely held" — because the caller turns that into a 409 naming a live
    build session. Folding an outage into the same `None` is what made every Redis blip
    surface as "a build session is already active" for a user who had none.

    `LockUnavailableError` SUBCLASSES `RedisError` (the additive `StorageUnconfiguredError`
    shape), so a caller that only knows `except RedisError` still catches it.
    """

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
    # The other half of the U3 split, pinned at the same seam: with a HEALTHY Redis, a
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

    Two distinct reasons live in this one list. Most entries are ANSWER-BEARING, and a
    swallow would manufacture a false answer out of an ambiguous store: `lock_is_held` False
    is fail-OPEN, `read_registry` None is a phantom "no sandbox", `renew_lock` False is a
    phantom "lock lost" that ends a healthy build, and a swallowed `mark_registry_ending`
    lets a concurrent attach reconnect to a container the reaper is about to delete.

    `release_lock_as_holder` and `write_heartbeat` are here for a DIFFERENT reason, and it
    is the one that is easy to get wrong (it was, once): they look like compensation paths
    that deserve a guard, but every caller that wants one already guards at the call site
    (`manager.py:365`, `:1046`, `:873`), and at `manager.py:398` / `:554` the raise is
    precisely what triggers the container teardown. See the two
    `test_relaunch_tears_down_the_container_when_*` tests in `test_manager.py`, which pin
    that behaviour end to end."""
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


# --- U13: the start-in-flight marker ------------------------------------------------
# `write_starting_marker` / `read_starting_marker` / `clear_starting_marker` are the
# primitives `_holding_user_lock` (manager.py) builds the `starting` fact from; this section
# pins them in isolation, the way `read_registry`/`mark_registry_ending` are pinned above.
# The pipelined read `project_preview_state` actually calls is `test_preview_state.py`'s to
# prove end to end — here it is pinned as a primitive: what it returns, and that it fails the
# same way a bare `hgetall` would have.

PROJECT = uuid.uuid4()


async def test_a_written_marker_names_the_project_and_carries_a_mandatory_ttl(
    fake_redis: aioredis.Redis,
) -> None:
    """The whole payload is the project id, and the TTL is not a default — a marker with none
    would be the registry hash's own mistake (ADR-0029) repeated in a new key."""
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
    await locks.clear_starting_marker(fake_redis, USER)  # a second clear is a clean no-op
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


async def test_the_pipelined_read_returns_both_the_registry_and_the_marker_in_one_round_trip(
    fake_redis: aioredis.Redis,
) -> None:
    """The exact pairing `project_preview_state` spends its one Redis round trip on (C3 §8.3):
    two commands, not two round trips."""
    await fake_redis.hset(registry_key(USER), mapping={REGISTRY_FIELD_APP_NAME: "sbx-x"})
    await locks.write_starting_marker(fake_redis, USER, PROJECT)

    reg, starting = await locks.read_registry_and_starting_marker(fake_redis, USER)

    assert reg is not None and reg[REGISTRY_FIELD_APP_NAME] == "sbx-x"
    assert starting == PROJECT


async def test_the_pipelined_read_answers_both_absent_with_no_registry_or_marker(
    fake_redis: aioredis.Redis,
) -> None:
    reg, starting = await locks.read_registry_and_starting_marker(fake_redis, USER)
    assert reg is None
    assert starting is None


async def test_the_pipelined_read_still_migrates_a_legacy_registry_record(
    fake_redis: aioredis.Redis,
) -> None:
    """The legacy-prefix adoption `read_registry` performs on a plain read must not be lost by
    routing through the pipeline instead: a pre-R22 user starting a build for the first time
    since the cutover still finds their record."""
    from src.services.redis.keys import legacy_registry_key

    await fake_redis.hset(legacy_registry_key(USER), mapping={REGISTRY_FIELD_APP_NAME: "sbx-x"})

    reg, starting = await locks.read_registry_and_starting_marker(fake_redis, USER)

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

    async def execute(self) -> object:
        raise RedisError("redis is down")


async def test_the_pipelined_read_surfaces_redis_errors_bare(
    fake_redis: aioredis.Redis, monkeypatch: pytest.MonkeyPatch
) -> None:
    """BARE, per the module's REDIS-ERROR POLICY: a `RedisError` from `pipe.execute()`
    propagates exactly as a bare `hgetall` would have, so `project_preview_state`'s existing
    `except RedisError` (answering `unknown`) keeps working unchanged."""
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
