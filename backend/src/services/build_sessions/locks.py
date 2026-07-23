"""C5 coordination primitives: the one-per-user lock, the idle heartbeat, and the
registry-state helpers.

All keys go through the frozen `services/redis/keys.py` builders — no track ever
hand-writes a key string (KTD-10). TTL/cadence use the **C3-frozen** constants
(`LOCK_TTL_SECONDS=900`, `LOCK_RENEW_CADENCE_SECONDS=300`, `HEARTBEAT_TTL_SECONDS=90`),
never C5's proposed 300 s lock default — a 300 s lock with a 300 s renew cadence has
zero head-room and can drop the lock under an active build.

Two **distinct** release primitives (do NOT collapse them into one — the #1 verified
correctness trap):

* `release_lock_as_holder` — the graceful stop/end path: compare-and-delete with the
  holder's OWN in-process token, so a process never deletes a lock it no longer owns.
* `reap_lock` — the reaper / reconcile path: the in-process token is gone by
  construction (crashed / restarted session, or another user's stale lock), so it reads
  the CURRENT stored value and compare-and-deletes THAT observed value — clearing a
  drifted lock without clobbering a same-user racing fresh acquire.

The lock is security-sensitive, so it fails CLOSED: any Redis error on acquire denies
(never a silent grant).

REDIS-ERROR POLICY — `acquire_lock` is the ONLY primitive here that catches `RedisError`,
and it catches it to RETYPE it (`LockUnavailableError`), never to answer with it. Every
other one lets it propagate raw, and that is a decision, not an omission:

* Swallowing in an ANSWER-BEARING primitive manufactures a certain-looking answer out of an
  ambiguous store — `lock_is_held` ⇒ False is fail-OPEN, `read_registry` ⇒ None is a phantom
  "no sandbox", `renew_lock` ⇒ False is a phantom "lock lost" that ends a healthy build.
  `mark_registry_ending` is an ordering guard ("step 1: guard a concurrent attach") whose
  failure must abort the reaper sequence rather than let it delete a container a racing
  `attach_existing` still believes is ready.
* `release_lock_as_holder` and `write_heartbeat` LOOK like compensation paths that want a
  guard. **They do not — resist the temptation, it has already been tried once.** Two
  independent reasons, and each alone is sufficient:
    1. REDUNDANT where a guard is genuinely wanted. Every caller that needs one already has
       it, at the call site, per `_do_finalize`'s log-and-continue pattern:
       `manager.py:365` (`_compensate_lock_and_container`), `manager.py:1046`
       (`_do_finalize`), `manager.py:873` (the in-build renew/heartbeat loop). An
       in-primitive guard would make those unreachable and misleading.
    2. ACTIVELY HARMFUL where there is no call-site guard, because there the raise IS the
       mechanism. At the `_holding_user_lock` clean exit and at BOTH heartbeat seeds —
       relaunch's AND start's, each now placed inside the block BEFORE `scope.adopt()` — the
       call sits inside the protected region whose `except BaseException` spawns the
       compensation that tears the container down (the `_holding_user_lock` docstring says so
       outright). A swallow returns normally, compensation never runs, and the container is
       left ALIVE behind a lock nobody releases. And at the two lock/heartbeat ENDPOINTS it
       would turn a Redis outage into `200 {"released": true}` / `200 {"alive": true}` — a
       lie in a response body.

So a Redis error from this module surfaces to its caller, and the HTTP layer maps it:
`services/redis/errors.py` turns it into a 503 with user-facing copy (U3).
"""

from __future__ import annotations

import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Final

import redis.asyncio as aioredis
import structlog
from redis.exceptions import RedisError

from src.api.v1.build_sessions.schemas import (
    HEARTBEAT_TTL_SECONDS,
    LOCK_TTL_SECONDS,
    RELAUNCH_PREVIEW_STAY_SECONDS,
)
from src.services.redis import (
    REGISTRY_STATE_ENDING,
    heartbeat_key,
    lock_key,
    registry_key,
)
from src.services.redis.keys import REGISTRY_FIELD_PREVIEW_STAY_UNTIL, REGISTRY_FIELD_STATE

_log = structlog.get_logger()

_LOCK_TOKEN_BYTES: Final = 32


class LockUnavailableError(RedisError):
    """The one-per-user lock was neither granted nor refused — Redis failed to answer, so
    whether the lock is held is UNKNOWN (U3).

    `acquire_lock` returns `None` for exactly one thing: the lock is genuinely HELD. That
    is a certain answer and the caller renders it as a 409 naming the live session. A
    Redis failure used to collapse into the same `None`, which made every outage look like
    a conflict — the endpoint told users "a build session is already active" when no
    session existed anywhere. Same fail-closed outcome (no token is ever handed out on an
    error), different surfaced truth.

    Additive by design, mirroring `StorageUnconfiguredError(StorageError)`: it SUBCLASSES
    `RedisError`, so every existing `except RedisError` — including
    `services/redis/errors.py::build_coordination_or_503`, which maps it to the 503 —
    keeps working with no change, and no caller has to learn a new type to stay correct.
    """


# Compare-and-delete: DEL the key only if its current value equals ARGV[1]. Atomic (a
# Redis Lua script runs single-threaded, so nothing interleaves between the GET and the
# DEL). Shared by BOTH release primitives — they differ only in WHICH value they compare.
_CAS_DELETE_LUA: Final = (
    "if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('DEL', KEYS[1]) "
    "else return 0 end"
)

# Renew: EXPIRE the key only if it is still the caller's own lock (token match).
_CAS_RENEW_LUA: Final = (
    "if redis.call('GET', KEYS[1]) == ARGV[1] then return redis.call('EXPIRE', KEYS[1], ARGV[2]) "
    "else return 0 end"
)


# --- the one-per-user lock (C5) ----------------------------------------------


async def acquire_lock(redis: aioredis.Redis, user_uuid: uuid.UUID) -> str | None:
    """`SET lock NX EX LOCK_TTL` — the `NX` is the one-sandbox-per-user enforcement
    point. Returns the fresh holder token on success, `None` when the lock is already
    HELD — and `None` means only that.

    Fails CLOSED in both directions: a Redis error never hands out a token. It raises
    `LockUnavailableError` rather than returning `None` because "held" and "unknown" are
    different answers that the HTTP layer owes the user differently — 409 vs 503 (U3, and
    `.claude/rules/fail-first.md`: ambiguity denies, but it must deny HONESTLY)."""
    token = secrets.token_urlsafe(_LOCK_TOKEN_BYTES)
    try:
        acquired = await redis.set(lock_key(user_uuid), token, nx=True, ex=LOCK_TTL_SECONDS)
    except RedisError as exc:
        _log.exception("lock acquire failed closed (denying)", user_id=str(user_uuid))
        raise LockUnavailableError("the one-per-user lock could not be read") from exc
    return token if acquired else None


async def renew_lock(redis: aioredis.Redis, user_uuid: uuid.UUID, token: str) -> bool:
    """Re-`EX` the lock only if `token` still matches (the caller still owns it). Returns
    `False` when the lock was lost (token mismatch / expired) -> `build_session_lock_lost`."""
    renewed = await redis.eval(_CAS_RENEW_LUA, 1, lock_key(user_uuid), token, LOCK_TTL_SECONDS)
    return bool(renewed)


async def release_lock_as_holder(redis: aioredis.Redis, user_uuid: uuid.UUID, token: str) -> bool:
    """Holder release (graceful stop/end, KTD-2): compare-and-delete with the holder's
    OWN token — a process never deletes a lock it no longer owns. Idempotent: a stale
    token is a no-op. Released LAST in the C4 / reaper ordering.

    BARE — see the REDIS-ERROR POLICY in the module docstring. It LOOKS like a compensation
    path that wants a guard, and it is not: every caller that needs one already has it, and
    at `manager.py:398` the raise is the very mechanism that triggers teardown."""
    deleted = await redis.eval(_CAS_DELETE_LUA, 1, lock_key(user_uuid), token)
    return bool(deleted)


async def reap_lock(redis: aioredis.Redis, user_uuid: uuid.UUID) -> bool:
    """Reaper / reconcile release: the in-process token is gone by construction, so read
    the CURRENT stored value and compare-and-delete THAT observed value. This clears a
    drifted lock WITHOUT clobbering a same-user racing fresh acquire (if a fresh acquire
    replaced the value between the read and the delete, the CAS matches nothing). This is
    still C5's compare-and-delete — compared against the *observed* value, not a held
    token. MUST NOT reuse `release_lock_as_holder` (it would match nothing, the lock
    would linger to its TTL, and the next start would 409 on a phantom session — KTD-3)."""
    observed = await redis.get(lock_key(user_uuid))
    if observed is None:
        return False  # already lapsed / never held
    deleted = await redis.eval(_CAS_DELETE_LUA, 1, lock_key(user_uuid), observed)
    return bool(deleted)


async def lock_is_held(redis: aioredis.Redis, user_uuid: uuid.UUID) -> bool:
    return bool(await redis.exists(lock_key(user_uuid)))


def lock_expires_at(anchor: datetime | None = None) -> datetime:
    """The UTC instant a just-set lock lapses if not renewed (`LOCK_TTL_SECONDS` out)."""
    return (anchor or datetime.now(UTC)) + timedelta(seconds=LOCK_TTL_SECONDS)


# --- the idle heartbeat (C5) -------------------------------------------------


async def write_heartbeat(redis: aioredis.Redis, user_uuid: uuid.UUID) -> datetime:
    """`SET heartbeat <iso8601> EX HEARTBEAT_TTL` — presence = active, expiry = idle
    (eligible for reaper teardown). Returns the UTC instant the reaper considers the
    session idle.

    BARE for the same reason as `release_lock_as_holder` — see the REDIS-ERROR POLICY in the
    module docstring. The in-build renew loop already guards its own call, while at BOTH the
    relaunch and start heartbeat seeds the raise is what tears the container down — each seed
    sits inside `_holding_user_lock`'s compensated region, before the scope adopts the lock."""
    now = datetime.now(UTC)
    await redis.set(heartbeat_key(user_uuid), now.isoformat(), ex=HEARTBEAT_TTL_SECONDS)
    return now + timedelta(seconds=HEARTBEAT_TTL_SECONDS)


async def heartbeat_is_alive(redis: aioredis.Redis, user_uuid: uuid.UUID) -> bool:
    return bool(await redis.exists(heartbeat_key(user_uuid)))


# --- the lingering preview's stay of execution (#43, #13) --------------------
# A relaunched preview (#43) and a COMPLETED build's pardoned preview (#13/R2, granted by
# `manager._pardon_the_container`) deliberately do NOT occupy the one-per-user build slot:
# they hold no lock and nothing renews their heartbeat. That leaves the container's
# lifetime unowned, so it gets an explicit, bounded LEASE written onto the registry hash.
# The background sweep honors an unexpired stay; reconcile-on-start does NOT (see reaper).


async def grant_stay_of_execution(
    redis: aioredis.Redis,
    user_uuid: uuid.UUID,
    *,
    ttl_seconds: int = RELAUNCH_PREVIEW_STAY_SECONDS,
) -> datetime:
    """Stamp the registry hash with the UTC instant this preview's reprieve lapses, and
    return it. Guarded on registry existence exactly like `mark_registry_ending`, so it
    never conjures a partial registry hash for a user who has no sandbox.

    The returned deadline is what this call COMPUTED, not proof that it landed: when the
    guard skips the write there is no lease at all, and the caller (which discards the
    return) would otherwise see "no registry, no lease" as indistinguishable from success.
    So the skip is LOUD — a container running with nothing owning its lifetime is exactly
    the state the lease exists to prevent."""
    deadline = datetime.now(UTC) + timedelta(seconds=ttl_seconds)
    if not await redis.exists(registry_key(user_uuid)):
        _log.warning(
            "no registry hash to stamp a preview stay onto; the container has no lease",
            user_id=str(user_uuid),
        )
        return deadline
    await redis.hset(
        registry_key(user_uuid), REGISTRY_FIELD_PREVIEW_STAY_UNTIL, deadline.isoformat()
    )
    return deadline


async def stay_of_execution_is_current(redis: aioredis.Redis, user_uuid: uuid.UUID) -> bool:
    """True only while a granted stay is demonstrably unexpired AND inside the bound this
    module could ever have granted. Fails CLOSED — absent, empty, unparseable, or absurd
    ⇒ False (reapable). A malformed value must never buy an unbounded reprieve for a
    container nobody owns; the safe direction here is reaping.

    "Unexpired" alone is NOT enough: a perfectly parseable year-9999 stamp (a bad clock, a
    hand-edited hash, a future writer with a different unit) would then grant a reprieve
    measured in millennia — the exact unbounded reprieve the docstring above forbids, just
    reached through the parse rather than around it. So the window is bounded on BOTH
    sides by construction: `now < deadline <= now + RELAUNCH_PREVIEW_STAY_SECONDS`, i.e.
    nothing survives longer than a freshly granted stay would have."""
    raw = await redis.hget(registry_key(user_uuid), REGISTRY_FIELD_PREVIEW_STAY_UNTIL)
    if raw is None:
        return False
    value = raw.decode() if isinstance(raw, bytes) else str(raw)
    try:
        deadline = datetime.fromisoformat(value)
    except ValueError:
        _log.warning("unparseable preview stay; treating as lapsed", user_id=str(user_uuid))
        return False
    if deadline.tzinfo is None:  # defensive: a naive stamp is read as UTC, never local
        deadline = deadline.replace(tzinfo=UTC)
    now = datetime.now(UTC)
    ceiling = now + timedelta(seconds=RELAUNCH_PREVIEW_STAY_SECONDS)
    if deadline > ceiling:
        _log.warning(
            "preview stay exceeds the maximum grantable lease; treating as lapsed",
            user_id=str(user_uuid),
        )
        return False
    return deadline > now


# --- registry state (C5) -----------------------------------------------------
# The concrete C2 client owns registry CREATE/DELETE (services/sandbox/client.py); these
# helpers are the reaper's read + the mark-ending flip (KTD-10). Both use the frozen key
# builders — the sandbox layer and this layer never share a helper module (the frozen
# keys.py IS the shared contract), because sandbox/ must not import build_sessions/.


async def read_registry(redis: aioredis.Redis, user_uuid: uuid.UUID) -> dict[str, str] | None:
    raw = await redis.hgetall(registry_key(user_uuid))
    if not raw:
        return None
    return {str(k): str(v) for k, v in raw.items()}


async def mark_registry_ending(redis: aioredis.Redis, user_uuid: uuid.UUID) -> None:
    """Flip the registry `state` to `ending` (C5 mark-ending), set FIRST in the reaper
    ordering so a concurrent `attach_existing` sees a dying container and does not
    reconnect. Guarded on existence so it never conjures a partial registry hash."""
    if await redis.exists(registry_key(user_uuid)):
        await redis.hset(registry_key(user_uuid), REGISTRY_FIELD_STATE, REGISTRY_STATE_ENDING)


async def delete_registry(redis: aioredis.Redis, user_uuid: uuid.UUID) -> None:
    await redis.delete(registry_key(user_uuid))
