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
    held. Fails CLOSED: a Redis error denies (returns `None`), never a silent grant."""
    token = secrets.token_urlsafe(_LOCK_TOKEN_BYTES)
    try:
        acquired = await redis.set(lock_key(user_uuid), token, nx=True, ex=LOCK_TTL_SECONDS)
    except RedisError:
        _log.exception("lock acquire failed closed (denying)", user_id=str(user_uuid))
        return None
    return token if acquired else None


async def renew_lock(redis: aioredis.Redis, user_uuid: uuid.UUID, token: str) -> bool:
    """Re-`EX` the lock only if `token` still matches (the caller still owns it). Returns
    `False` when the lock was lost (token mismatch / expired) -> `build_session_lock_lost`."""
    renewed = await redis.eval(_CAS_RENEW_LUA, 1, lock_key(user_uuid), token, LOCK_TTL_SECONDS)
    return bool(renewed)


async def release_lock_as_holder(redis: aioredis.Redis, user_uuid: uuid.UUID, token: str) -> bool:
    """Holder release (graceful stop/end, KTD-2): compare-and-delete with the holder's
    OWN token — a process never deletes a lock it no longer owns. Idempotent: a stale
    token is a no-op. Released LAST in the C4 / reaper ordering."""
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
    session idle."""
    now = datetime.now(UTC)
    await redis.set(heartbeat_key(user_uuid), now.isoformat(), ex=HEARTBEAT_TTL_SECONDS)
    return now + timedelta(seconds=HEARTBEAT_TTL_SECONDS)


async def heartbeat_is_alive(redis: aioredis.Redis, user_uuid: uuid.UUID) -> bool:
    return bool(await redis.exists(heartbeat_key(user_uuid)))


# --- the relaunched preview's stay of execution (#43) ------------------------
# A relaunched preview deliberately does NOT occupy the one-per-user build slot: it
# holds no lock and nothing renews its heartbeat. That leaves its container's lifetime
# unowned, so it gets an explicit, bounded LEASE written onto the registry hash. The
# background sweep honors an unexpired stay; reconcile-on-start does NOT (see reaper).


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
