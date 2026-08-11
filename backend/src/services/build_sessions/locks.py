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

import math
import secrets
import time
import uuid
from datetime import UTC, datetime, timedelta
from typing import Final

import redis.asyncio as aioredis
import structlog
from redis.exceptions import RedisError

from src.api.v1.build_sessions.schemas import (
    HEARTBEAT_TTL_SECONDS,
    LIVENESS_LEASE_CLOCK_SKEW_GRACE_SECONDS,
    LIVENESS_LEASE_TTL_SECONDS,
    LOCK_TTL_SECONDS,
    RELAUNCH_PREVIEW_STAY_SECONDS,
)
from src.services.redis import (
    REGISTRY_STATE_ENDING,
    heartbeat_key,
    lease_key,
    legacy_registry_key,
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


# --- the R10 wall-clock liveness lease (C5 family 4) -------------------------
# THE ONE SIGNAL HERE THAT IS LEGIBLE FROM ANOTHER PROCESS. Everything above is either
# in-process (`live_users`) or a facade a crashed builder leaves standing for a TTL; the
# heartbeat is seeded once per turn, so ~90 s into any build the only thing keeping the
# sweep off a live container is an in-memory set that is empty everywhere else. That is
# why nothing capable of destroying a container may run out of the API process until this
# exists (ADR-0029 §8), and why the reader below fails closed at both ends.
#
# The renewal loop lives on the TURN (`services/turns/engine.py`), beside the preview
# watcher: a background task the turn owns, stopped in its `finally`, idempotent.


def _wall_clock_now() -> float:
    """`time.time()` — and it must STAY `time.time()`.

    A `time.monotonic()` reading is meaningless outside the process that took it, and
    cross-process readability is the entire reason this family exists: the reader is a
    sweep that is not running the build (today in the API process, tomorrow on the worker).
    Named rather than inlined so the choice has one place to be documented, one place to be
    changed by accident, and a seam a test can drive a scripted clock through."""
    return time.time()


async def renew_liveness_lease(redis: aioredis.Redis, user_uuid: uuid.UUID) -> bool:
    """Push this user's liveness lease out by one TTL. True if the write LANDED.

    The TTL is deliberately NOT a parameter. `liveness_lease_is_held` bounds what it will
    honour by the same module constant, so a caller passing a longer one would write a lease
    that can never read as held — silently buying no protection at all while returning True.
    One constant, read by both sides, is the only shape in which the write and the read agree.

    `SET lease <deadline-epoch-seconds> EX ttl_seconds`. Both halves matter and neither is
    redundant: the TTL is what stops an abandoned lease from pinning a container forever
    (the registry hash's missing TTL is the root cause of ADR-0029), and the stored deadline
    is what a reader compares against so a lease is never merely "present" — presence
    without a bound is how a stale key becomes an indefinite reprieve.

    DISOWNED WITH THE REGISTRY, guarded exactly like `grant_stay_of_execution`: a lease
    belongs to a sandbox record, and one written for a user with no record would spare
    whatever container that user gets next. The skip returns False and is LOUD, because the
    caller is a build that would otherwise carry on believing itself protected — the precise
    failure this family was added to remove.

    BARE on Redis errors, per the module's REDIS-ERROR POLICY. The caller (the turn's
    renewal loop) guards at the call site and logs; swallowing here would let a build run on
    with no lease and nothing said."""
    if not await redis.exists(registry_key(user_uuid)):
        _log.warning(
            "no registry hash to renew a liveness lease against; the build is unprotected",
            user_id=str(user_uuid),
        )
        return False
    deadline = _wall_clock_now() + LIVENESS_LEASE_TTL_SECONDS
    await redis.set(lease_key(user_uuid), str(deadline), ex=LIVENESS_LEASE_TTL_SECONDS)
    return True


async def liveness_lease_is_held(redis: aioredis.Redis, user_uuid: uuid.UUID) -> bool:
    """True only while a renewed lease is demonstrably unexpired AND inside the window a
    renewal could have produced. Fails CLOSED — absent, empty, unparseable, lapsed or absurd
    all read False (reapable).

    The upper bound is not decoration, and it is the same lesson `stay_of_execution_is_current`
    already paid for: "unexpired" alone lets a bad clock, a hand-edited key, or a future
    writer using milliseconds place a deadline centuries out, which is an unbounded hold on
    a container reached through the parse rather than around it. So the window is bounded on
    both sides — `now < deadline <= now + TTL + CLOCK_SKEW_GRACE` — and nothing survives much
    longer than a fresh renewal would have granted.

    THE GRACE IS LOAD-BEARING; do not "tighten" it back to a bare TTL. Writer and reader are
    different processes by design, so they are different clocks. A reader lagging the writer
    by any amount at all computes a ceiling below the deadline a renewal one millisecond old
    just wrote, calls it absurd, and hands a live build to the reaper. Reproduced with a
    scripted clock at 100 ms of skew before the grace existed.

    The comparison is not made redundant by the key's own expiry. Redis expiry is lazy, the
    value is what a future writer could get wrong, and a reader that trusted mere PRESENCE
    would spare a container on the strength of a key nobody can account for."""
    raw = await redis.get(lease_key(user_uuid))
    if raw is None:
        return False
    value = raw.decode() if isinstance(raw, bytes) else str(raw)
    try:
        deadline = float(value)
    except ValueError:
        _log.warning("unreadable liveness lease; treating as lapsed", user_id=str(user_uuid))
        return False
    if not math.isfinite(deadline):
        # `float()` accepts "nan" and "inf" without complaint. NaN would fall through as
        # False anyway (every comparison against it is False), but `inf` would sail past a
        # naive `deadline > now` — so both are named here rather than left to luck.
        _log.warning("non-finite liveness lease; treating as lapsed", user_id=str(user_uuid))
        return False
    now = _wall_clock_now()
    if deadline > now + LIVENESS_LEASE_TTL_SECONDS + LIVENESS_LEASE_CLOCK_SKEW_GRACE_SECONDS:
        _log.warning(
            "liveness lease exceeds the maximum renewable window; treating as lapsed",
            user_id=str(user_uuid),
        )
        return False
    return deadline > now


async def release_liveness_lease(redis: aioredis.Redis, user_uuid: uuid.UUID) -> None:
    """Drop the lease. Idempotent, and issued from three places for three reasons: the turn's
    `finally` (the container has been handed back), the reaper's ordered reap (the record it
    belonged to is gone), and the certified-dead reconcile (a turn killed mid-build left it
    behind, and honouring it would 409 the same builder's next start until the TTL).

    Unconditional `DEL`, deliberately NOT a compare-and-delete like the lock's. The lock
    holds an opaque holder token precisely so a process cannot delete a lock it no longer
    owns; a lease holds a deadline and has exactly one writer at a time — the turn holding
    the user's one-per-user slot — so there is no second holder to protect against, and a
    CAS here would only leave a stale lease standing whenever the value had moved on."""
    await redis.delete(lease_key(user_uuid))


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
    """Read the sandbox record, falling back to the pre-R22 key and migrating what it finds.

    DUAL-READ, and it is the load-bearing half of the R22 cutover (C5). `KEY_PREFIX` used to have
    no environment segment, and it is the sole input to `sweep_all`'s scan and to the Azure
    inventory — so reading only the new key would have made every container live at the cutover
    instant permanently invisible to both, forever, since the registry hash is the one family with
    no TTL. Widening the SCAN alone would NOT have saved them either: `sweep_all` does not read
    the record off the scan, it re-enters HERE with the user id it parsed out of the key name.

    Precedence is current-then-legacy, never the other way round: every write goes to the current
    key, so it is by definition the newer claim, and answering with a superseded `app_name` would
    point a teardown at the wrong container.

    `SandboxClient._read_registry` is the other point read and must behave identically — C5 keeps
    the two separate on purpose (`services/sandbox/` may not import `services/build_sessions/`),
    and `tests/services/build_sessions/test_key_migration.py` is what stops them drifting.
    """
    raw = await redis.hgetall(registry_key(user_uuid))
    if raw:
        return {str(k): str(v) for k, v in raw.items()}
    return await _adopt_a_pre_cutover_record(redis, user_uuid)


async def _adopt_a_pre_cutover_record(
    redis: aioredis.Redis, user_uuid: uuid.UUID
) -> dict[str, str] | None:
    """Migrate one legacy-prefix registry hash into the environment-scoped namespace, on read.

    `COPY`, not a read-then-`HSET`. It is one atomic server-side move of whatever the key holds,
    so a field this module has never heard of cannot be dropped in transit — and the optional
    `preview_stay_until` is a live example of exactly such a field, written by a different
    subsystem. A client-side rewrite would also re-encode every value through this process's
    idea of a `str`, which is a second way to lose information for no benefit.

    Copy first, delete second. Dying between them leaves a legacy key that the next read ignores
    (the current key now wins) and that `delete_registry` clears from both prefixes, so the
    sequence terminates either way. The reverse order would lose the record outright.
    """
    raw = await redis.hgetall(legacy_registry_key(user_uuid))
    if not raw:
        return None

    # SINGLE-KEY COMMANDS ONLY. This used to be `COPY legacy current`, which is elegant and
    # unusable: the two keys carry no hash tag, so they hash to different slots, and a
    # cross-slot multi-key command is REJECTED on a clustered Redis. The production instance
    # is Azure Managed Redis Enterprise and its clustering policy is an explicitly unverified
    # provisioning gate — and this very plan rejects `RedisScheduleSource` for exactly this
    # reason. Getting it wrong fails on the path built to RESCUE the fleet: `read_registry` is
    # deliberately unguarded, so every pre-cutover user's attach would 500 and no legacy record
    # would ever migrate. fakeredis is single-instance and cannot catch it.
    #
    # `hset(mapping=raw)` carries the identical field set, because `raw` is already the complete
    # hash from the HGETALL above. What COPY bought was server-side atomicity against a racing
    # writer — see the narrowed race below.
    #
    # THE LEGACY KEY IS NOT DELETED HERE. It used to be, and that made the mitigation worse than
    # the exposure it mitigates: a process pointed at the WRONG Redis would not merely read
    # another environment's legacy record, it would relocate it under its own prefix and delete
    # the original — leaving the owning environment with a running container and no record. That
    # is precisely the orphan class ADR-0029 exists to collect, manufactured by R22's own
    # remedy. Termination does not depend on this delete: `delete_registry` clears BOTH prefixes
    # when the session ends, and once the current key exists this function is never reached again
    # (the caller finds the current key first).
    if await redis.exists(registry_key(user_uuid)):
        # A racing writer created the current record between the HGETALL above and here, and
        # THAT record is the newer claim. Answering with the legacy hash still in hand would
        # return a superseded `app_name` — a teardown pointed at the wrong container.
        current = await redis.hgetall(registry_key(user_uuid))
        return {str(k): str(v) for k, v in current.items()} if current else None

    # The residual race COPY closed and this does not: a writer landing between the `exists`
    # above and the `hset` below is overwritten. It is narrow and benign in the shapes that
    # actually occur — two concurrent MIGRATIONS write byte-identical content, and the only
    # other writer (`_write_registry` during provisioning) runs under the per-user start lock,
    # which a caller reaching this line does not hold. Accepted deliberately over a command
    # that cannot run on the substrate.
    # Inline comprehension, not a `dict[str, str]` variable: redis-py types `mapping` as
    # `Mapping[FieldT, EncodableT]` whose KEY parameter is invariant, so a named
    # `dict[str, str]` fails every type gate while the identical inline literal passes.
    await redis.hset(registry_key(user_uuid), mapping={str(k): str(v) for k, v in raw.items()})
    _log.info(
        "sandbox_registry_migrated_to_the_environment_namespace",
        user_id=str(user_uuid),
        detail=(
            "a record written before R22, copied under the environment prefix; the legacy key "
            "is left for delete_registry, never removed on read"
        ),
    )
    return {str(k): str(v) for k, v in raw.items()}


async def mark_registry_ending(redis: aioredis.Redis, user_uuid: uuid.UUID) -> None:
    """Flip the registry `state` to `ending` (C5 mark-ending), set FIRST in the reaper
    ordering so a concurrent `attach_existing` sees a dying container and does not
    reconnect. Guarded on existence so it never conjures a partial registry hash."""
    if await redis.exists(registry_key(user_uuid)):
        await redis.hset(registry_key(user_uuid), REGISTRY_FIELD_STATE, REGISTRY_STATE_ENDING)


async def delete_registry(redis: aioredis.Redis, user_uuid: uuid.UUID) -> None:
    """Clear the sandbox record under BOTH prefixes (C5 dual-read window).

    This is what makes the window terminate, and it is the ONLY place the legacy key is removed
    — migration-on-read deliberately leaves it (see `_adopt_a_pre_cutover_record`). Without a
    legacy `DEL` here, a pre-cutover record would survive its own session forever, every later
    pass would read it, tear down a container that is already gone, and never clear it: a
    permanent per-pass ARM call plus a log line that looks like real work. The legacy arm is
    removed in release B, once the inventory reports zero legacy-prefix records.

    TWO SINGLE-KEY DELETES, not one two-key `DEL`. The keys carry no hash tag and hash to
    different slots, so a multi-key command is rejected outright on a clustered Redis — and the
    production clustering policy is an unverified provisioning gate. Issued current-first so an
    interruption between them leaves only the legacy key, which the next read migrates rather
    than the reverse (a surviving CURRENT key with the legacy one gone would be read as live).
    """
    await redis.delete(registry_key(user_uuid))
    await redis.delete(legacy_registry_key(user_uuid))
