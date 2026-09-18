"""Redis coordination primitives: the one-per-user lock, idle heartbeat, and registry-state
helpers, built only from the frozen key builders and TTL constants (`services/redis/keys.py`,
`api/v1/build_sessions/schemas.py`).

Two release primitives, not interchangeable: `release_lock_as_holder` compare-and-deletes against
the holder's OWN token (graceful stop/end); `reap_lock` has no token, so it compares against the
CURRENTLY stored value instead (reaper/reconcile) — clearing a drifted lock without clobbering a
same-user racing acquire. The lock fails CLOSED: any Redis error on acquire denies, never grants.

REDIS-ERROR POLICY: only `acquire_lock` catches `RedisError` (to retype it as
`LockUnavailableError`); every other primitive lets it propagate, deliberately. Answer-bearing
primitives (`lock_is_held`, `read_registry`, `renew_lock`, `mark_serving`) must never swallow —
that would fabricate a certain answer from an ambiguous store; a swallowed `mark_serving` error
in particular would report "the stamp was refused" for a store that never answered, and its
caller raises an alarm on exactly that. `mark_registry_ending` returns nothing to
fabricate; it is an ordering guard, and its failure must abort the reaper sequence rather than
let it delete a container a racing `attach_existing` still believes is ready.
`release_lock_as_holder` and `write_heartbeat` look like they want a guard; they don't —
callers that need one already have it, and inside `_holding_user_lock`'s protected region
the raise IS what triggers compensation.

WHY THIS EXISTS
---------------
Redis here is distrusted for two confirmed reasons, stated once for every module that relies on
them. Its `maxmemory-policy` is unverified, and under `allkeys-*` an untimed key is as evictable
as a TTL'd one, so anything that must survive memory pressure lives in Postgres instead. And
production (Azure Managed Redis) reports `clusteringPolicy = EnterpriseCluster` (confirmed
2026-08-18) — reads as unclustered, but the database beneath IS sharded — so every command here
is single-key by construction, and a dev Redis cannot reproduce the cross-slot rejection.
"""

from __future__ import annotations

import enum
import math
import secrets
import time
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Final

import redis.asyncio as aioredis
import structlog
from redis.exceptions import RedisError

from src.api.v1.build_sessions.schemas import (
    HEARTBEAT_TTL_SECONDS,
    HIDDEN_SURFACE_PRESENT_STAY_SECONDS,
    LIVENESS_LEASE_CLOCK_SKEW_GRACE_SECONDS,
    LIVENESS_LEASE_TTL_SECONDS,
    LOCK_TTL_SECONDS,
    RELAUNCH_PREVIEW_STAY_SECONDS,
    SERVED_TRAFFIC_STAY_SECONDS,
    STARTING_MARKER_TTL_SECONDS,
    SURFACE_PRESENT_STAY_SECONDS,
    TURN_ENDED_UNCHANGED_STAY_SECONDS,
    RenewalOutcome,
    SurfacePresence,
)
from src.services.redis import (
    REGISTRY_STATE_ENDING,
    REGISTRY_STATE_READY,
    heartbeat_key,
    lease_key,
    legacy_registry_key,
    lock_key,
    registry_key,
)
from src.services.redis.keys import (
    REGISTRY_FIELD_ADOPTED_FROM_LEGACY,
    REGISTRY_FIELD_APP_NAME,
    REGISTRY_FIELD_CREATED_AT,
    REGISTRY_FIELD_PREVIEW_STAY_UNTIL,
    REGISTRY_FIELD_SERVING_SINCE,
    REGISTRY_FIELD_STATE,
    REGISTRY_FIELD_STAY_WRITER,
    starting_key,
)

_log = structlog.get_logger()

_LOCK_TOKEN_BYTES: Final = 32


class LockUnavailableError(RedisError):
    """Redis failed to answer, so whether the lock is held is UNKNOWN — distinct from
    `acquire_lock` returning `None`, which means the lock is genuinely HELD (409). A Redis
    failure used to collapse into that same `None`, so an outage looked like a conflict:
    "already active" when no session existed. Same fail-closed outcome, different truth.

    SUBCLASSES `RedisError` (like `StorageUnconfiguredError(StorageError)`) so every existing
    `except RedisError` — including the 503 mapping in `services/redis/errors.py` — keeps
    working with no caller change."""


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


# --- the one-per-user lock ---------------------------------------------------


async def acquire_lock(redis: aioredis.Redis, user_uuid: uuid.UUID) -> str | None:
    """`SET lock NX EX LOCK_TTL` — the `NX` is the one-sandbox-per-user enforcement
    point. Returns the fresh holder token on success, `None` when the lock is already
    HELD — and `None` means only that.

    Fails CLOSED in both directions: a Redis error never hands out a token. It raises
    `LockUnavailableError` rather than returning `None` because "held" and "unknown" are
    different answers that the HTTP layer owes the user differently — 409 vs 503. Ambiguity
    denies, but it must deny HONESTLY."""
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
    """Holder release (graceful stop/end): compare-and-delete with the holder's
    OWN token — a process never deletes a lock it no longer owns. Idempotent: a stale
    token is a no-op. Released LAST in the snapshot / teardown / reaper ordering.

    BARE — see the REDIS-ERROR POLICY in the module docstring. It LOOKS like a compensation
    path that wants a guard, and it is not: every caller that needs one already has it, and at
    `_holding_user_lock`'s clean exit the raise is the very mechanism that triggers teardown —
    that call sits inside the block whose `except BaseException` spawns the compensation."""
    deleted = await redis.eval(_CAS_DELETE_LUA, 1, lock_key(user_uuid), token)
    return bool(deleted)


async def reap_lock(redis: aioredis.Redis, user_uuid: uuid.UUID) -> bool:
    """Reaper / reconcile release: the in-process token is gone by construction, so read
    the CURRENT stored value and compare-and-delete THAT observed value. This clears a
    drifted lock WITHOUT clobbering a same-user racing fresh acquire (if a fresh acquire
    replaced the value between the read and the delete, the CAS matches nothing). This is
    still a compare-and-delete — compared against the *observed* value, not a held
    token. MUST NOT reuse `release_lock_as_holder` (it would match nothing, the lock
    would linger to its TTL, and the next start would 409 on a phantom session)."""
    observed = await redis.get(lock_key(user_uuid))
    if observed is None:
        return False  # already lapsed / never held
    deleted = await redis.eval(_CAS_DELETE_LUA, 1, lock_key(user_uuid), observed)
    return bool(deleted)


async def lock_is_held(redis: aioredis.Redis, user_uuid: uuid.UUID) -> bool:
    return bool(await redis.exists(lock_key(user_uuid)))


# --- the idle heartbeat -------------------------------------------------------


async def write_heartbeat(redis: aioredis.Redis, user_uuid: uuid.UUID) -> datetime:
    """`SET heartbeat <iso8601> EX HEARTBEAT_TTL` — presence = active, expiry = idle and
    reaper-eligible; returns the UTC instant the reaper treats the session idle. BARE, like
    `release_lock_as_holder` (module's REDIS-ERROR POLICY). TWO IN-BUILD RENEWERS run on different
    clocks — `SessionManager.on_progress` per frame, and the turn engine's liveness-lease loop on a
    wall clock inside the TTL, because a tool call longer than `HEARTBEAT_TTL_SECONDS` emits no
    frame and the frame-driven one alone let the heartbeat expire under a live build. Both guard
    their own call; at the relaunch and start seeds the raise tears the container down, each
    sitting inside `_holding_user_lock`'s compensated region before the scope adopts the lock."""
    now = datetime.now(UTC)
    await redis.set(heartbeat_key(user_uuid), now.isoformat(), ex=HEARTBEAT_TTL_SECONDS)
    return now + timedelta(seconds=HEARTBEAT_TTL_SECONDS)


async def heartbeat_is_alive(redis: aioredis.Redis, user_uuid: uuid.UUID) -> bool:
    return bool(await redis.exists(heartbeat_key(user_uuid)))


# --- the wall-clock liveness lease (sandbox key family 4) --------------------
# THE ONE SIGNAL HERE THAT IS LEGIBLE FROM ANOTHER PROCESS. Everything above is either
# in-process (`live_users`) or a facade a crashed builder leaves standing for a TTL; the
# heartbeat is seeded once per turn, so ~90 s into any build the only thing keeping the
# sweep off a live container is an in-memory set that is empty everywhere else. That is
# why nothing capable of destroying a container may run out of the API process until this
# exists, and why the reader below fails closed at both ends.
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
    """Push this user's liveness lease out by one TTL; True iff the write landed.

    TTL is NOT a parameter: `liveness_lease_is_held` bounds itself to the same module
    constant, so a longer TTL would write a lease that can never read as held — silent, no
    protection. Skips (LOUD: logs + returns False) when no registry record exists, so a lease
    never outlives the record and spares whatever container this user gets NEXT. BARE on
    Redis errors per the module's REDIS-ERROR POLICY; the turn's renewal loop guards there."""
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
    """True only while unexpired AND inside the window a renewal could have produced
    (`now < deadline <= now + TTL + CLOCK_SKEW_GRACE`); fails CLOSED — absent, empty,
    unparseable, lapsed, or absurd all read False (reapable).

    THE GRACE IS LOAD-BEARING; do not tighten it to a bare TTL. Writer and reader are
    different processes/clocks — without it, a lagging reader computes a ceiling below a
    fresh deadline, calls it absurd, and reaps a live build (reproduced at 100 ms of skew).
    Lazy Redis expiry doesn't make this redundant: PRESENCE alone would spare a container."""
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
    """Drop the lease. Idempotent; issued from three places: the turn's `finally` (container
    handed back), the reaper's ordered reap (record gone), and the certified-dead reconcile
    (a turn killed mid-build left it behind, and honouring it would 409 the next start).

    Unconditional `DEL`, deliberately NOT a compare-and-delete like the lock's: a lease has
    exactly one writer at a time (the turn holding the user's slot), so there is no second
    holder to protect against, and a CAS would only leave a stale lease standing once the
    value had moved on."""
    await redis.delete(lease_key(user_uuid))


# --- the start-in-flight marker (sandbox key family 5) ------------------------
# A start in flight is a fact the platform holds, not one a tab remembers. `_holding_user_lock`
# (`manager.py`) is the ONE writer — the single skeleton behind the build start, the relaunch,
# and the turn's `ensure_sandbox` — so every door into a container reports "starting" the same
# way, and a second press or a turn arriving mid-start finds the same marker rather than racing
# a second provision.
#
# NOT a registry field, on purpose: a registry entry written before a container exists is
# exactly the "registered ⇒ spared" hazard this marker exists to avoid, and it would be visible
# to the sweep, the reconciler and the ARM reconciliation as a container that does not exist.


async def write_starting_marker(
    redis: aioredis.Redis, user_uuid: uuid.UUID, project_id: uuid.UUID
) -> None:
    """`SET starting <project_id> EX STARTING_MARKER_TTL_SECONDS`, issued once the lock is
    held. TTL is MANDATORY — past it, reapable again as if never written: a bounded claim,
    not a pardon.

    BARE on Redis errors (module REDIS-ERROR POLICY) — safe only because of WHERE it's
    called: inside `_holding_user_lock`'s try, after the lock but before the scope yields,
    so a raise reaches compensation and tears down a not-yet-real container. Called ABOVE
    that try, the already-held lock would leak for the full TTL instead."""
    await redis.set(starting_key(user_uuid), str(project_id), ex=STARTING_MARKER_TTL_SECONDS)


def _parse_starting_marker(raw: object, user_uuid: uuid.UUID) -> uuid.UUID | None:
    """The ONE reading of a marker's value, shared by both readers below — they feed ONE
    predicate (is a start in flight) from different call sites (`reaper.reconcile_user` /
    `reclamation_pass._claim_of` via the direct read, `project_preview_state` via the pipelined
    one). A value one called garbage and the other called a claim would spare a container on
    one path and destroy it on the other.

    Fails toward `None` on anything unparseable rather than treat garbage as a claim — same
    fail-closed reading `liveness_lease_is_held` gives an unparseable deadline."""
    if raw is None:
        return None
    value = raw.decode() if isinstance(raw, bytes) else str(raw)
    try:
        return uuid.UUID(value)
    except ValueError:
        _log.warning("unreadable starting marker; treating as absent", user_id=str(user_uuid))
        return None


async def read_starting_marker(redis: aioredis.Redis, user_uuid: uuid.UUID) -> uuid.UUID | None:
    """The project id a start names for this user, or `None` when nothing is starting.

    `reclamation_pass._claim_of` reads this (or the pipelined form below) to add the marker as
    a fourth disjunct to the reclamation spare predicate."""
    return _parse_starting_marker(await redis.get(starting_key(user_uuid)), user_uuid)


async def clear_starting_marker(redis: aioredis.Redis, user_uuid: uuid.UUID) -> None:
    """Unconditional `DEL`. Idempotent; issued from `_holding_user_lock`'s clean exit (the scope
    that releases OR adopts the lock — a start in flight is over the instant either happens) and
    from the compensation arm (the body failed, nothing left to claim).

    Both sites GUARD this call, deliberately unlike `release_lock_as_holder`/`write_heartbeat`
    whose raise IS the compensation trigger. A marker that fails to clear should not tear down a
    just-provisioned container, or skip teardown of a failed one — its TTL backstops either way."""
    await redis.delete(starting_key(user_uuid))


async def read_registry_and_starting_marker(
    redis: aioredis.Redis, user_uuid: uuid.UUID
) -> tuple[dict[str, str] | None, uuid.UUID | None]:
    """The one round trip `project_preview_state` spends on Redis: registry hash + starting
    marker as ONE PIPELINE (two commands), not two sequential round trips — keeps the frozen
    cost budget ("one registry hash read") honest instead of doubling it on every poll.

    Falls back to `read_registry`'s legacy-prefix adoption ONLY when the pipelined `HGETALL`
    comes back empty — that migration is itself a second round trip, so it stays off the hot
    path, rare not routine. `RedisError` propagates from `pipe.execute()` exactly as a bare
    `hgetall` would, so `project_preview_state`'s existing `except RedisError` keeps working."""
    pipe = redis.pipeline(transaction=False)
    pipe.hgetall(registry_key(user_uuid))
    pipe.get(starting_key(user_uuid))
    raw_registry, raw_starting = await pipe.execute()
    registry = {str(k): str(v) for k, v in raw_registry.items()} if raw_registry else None
    if registry is None:
        registry = await _adopt_a_pre_cutover_record(redis, user_uuid)
    return registry, _parse_starting_marker(raw_starting, user_uuid)


# --- the lingering preview's stay of execution -------------------------------
# A FIELD ON THE REGISTRY HASH, not a key of its own, so it cannot outlive the record it
# reprieves — which is why every write below is guarded on that record still existing.


class DeadlineWriter(enum.StrEnum):
    """Every party permitted to push a sandbox's keep-alive deadline forward.

    A CLOSED SET, deliberately: before this, a container stayed up because *something* renewed
    *something* and no operator could say what — how the origin incident's containers outlived
    everyone who might have stopped them. Adding a member is a deliberate, reviewed act.

    Weighted toward the CONTAINER over the keyboard: what's happening *inside* it and what its
    app is serving is direct evidence of use; keystrokes are only a proxy for presence."""

    #: The wall-clock lease, published by the turn engine for the duration of a turn.
    #: OUTRANKS EVERYTHING, and does it structurally rather than by comparing numbers: the lease
    #: is its own key with its own TTL, and `liveness_lease_is_held` spares a container before any
    #: deadline is consulted. A turn in flight cannot be out-voted by an expiring stay.
    TURN_IN_FLIGHT = "turn_in_flight"
    #: Requests the generated app actually served, self-reported by the sandbox and excluding
    #: control-plane probes. Buys a BOUNDED extension — never indefinite life.
    APP_SERVED_TRAFFIC = "app_served_traffic"
    #: A deliberate act that puts a container in front of the builder: the relaunch of a saved
    #: preview, and a colleague's shared launch. Both are already project-scoped requests, so the
    #: extension is a side effect of the call being made anyway — no keystroke listener, no new
    #: machinery. Save, Stop and Deploy do NOT write it: they act on a container whose lifetime is
    #: already held by the turn's lease or by the presence of a surface.
    BUILDER_ACTED = "builder_acted"
    #: A turn ended having WRITTEN NOTHING (`workspace_touched` is False). The
    #: weakest evidence in the set on purpose: it is pure keyboard, with nothing on the container
    #: side to show for it — no file changed, no tool ran that could have. Bounds the cost of a
    #: chat-only (Plan-kind) session that pins the workspace on every turn without ever
    #: producing anything to keep it pinned for. Never chosen for a FAILED turn's write attempt —
    #: `_pardon_the_container` keys on WHAT the turn did, not on how it ended.
    TURN_ENDED_UNCHANGED = "turn_ended_unchanged"
    #: A screen that can frame this project is open and polling. The only writer a BROWSER can
    #: reach, and the only one that keeps renewing while nobody types: presence is what holds a
    #: container, and silence is how leaving is spelled. Bounded twice over — by its own short
    #: TTL, so the renewals stopping is enough on its own, and by the absolute age ceiling, which
    #: nothing here can push past.
    SURFACE_PRESENT = "surface_present"


#: How long each writer's evidence is worth. Traffic buys less than a deliberate action because it
#: is weaker evidence of intent — a background poll from a left-open app tab is still traffic.
DEADLINE_WRITER_TTL_SECONDS: Final[Mapping[DeadlineWriter, int]] = {
    DeadlineWriter.TURN_IN_FLIGHT: RELAUNCH_PREVIEW_STAY_SECONDS,
    DeadlineWriter.APP_SERVED_TRAFFIC: SERVED_TRAFFIC_STAY_SECONDS,
    DeadlineWriter.BUILDER_ACTED: RELAUNCH_PREVIEW_STAY_SECONDS,
    DeadlineWriter.TURN_ENDED_UNCHANGED: TURN_ENDED_UNCHANGED_STAY_SECONDS,
    DeadlineWriter.SURFACE_PRESENT: SURFACE_PRESENT_STAY_SECONDS,
}

#: What a surface's visibility is worth, and the reason the client sends a WORD rather than a
#: number: a client naming its own seconds is a client that can ask for more life than the
#: ceiling allows. A hidden tab earns the longer budget because its timer is not trustworthy —
#: browsers throttle, sleep and freeze background tabs — not because it is better evidence.
PRESENCE_STAY_SECONDS: Final[Mapping[SurfacePresence, int]] = {
    SurfacePresence.VISIBLE: SURFACE_PRESENT_STAY_SECONDS,
    SurfacePresence.HIDDEN: HIDDEN_SURFACE_PRESENT_STAY_SECONDS,
}


async def grant_stay_of_execution(
    redis: aioredis.Redis,
    user_uuid: uuid.UUID,
    *,
    writer: DeadlineWriter = DeadlineWriter.BUILDER_ACTED,
    ttl_seconds: int | None = None,
) -> datetime:
    """Stamp the registry hash with the UTC instant this preview's reprieve lapses, and return
    it. Guarded on registry existence like `mark_registry_ending` — never conjures a hash for a
    user with no sandbox; the skip still returns the computed deadline but logs LOUD, since the
    caller (which discards the return) can't otherwise tell "no lease" from success.

    Every caller names its writer; the TTL comes from that identity, and the name is stamped
    beside the deadline for audit. THE DEADLINE NEVER MOVES BACKWARD: `max(existing, computed)`
    keeps a weaker writer from truncating a stronger one's reprieve, no lock needed."""
    ttl = DEADLINE_WRITER_TTL_SECONDS[writer] if ttl_seconds is None else ttl_seconds
    deadline = datetime.now(UTC) + timedelta(seconds=ttl)
    if not await redis.exists(registry_key(user_uuid)):
        _log.warning(
            "no registry hash to stamp a preview stay onto; the container has no lease",
            user_id=str(user_uuid),
            writer=str(writer),
        )
        return deadline
    standing = await _standing_stay(redis, user_uuid)
    if standing is not None and standing >= deadline:
        # A stronger (or simply more recent) writer already bought more time. Leave the deadline
        # and its provenance alone rather than recording this one as the reason for a reprieve it
        # did not grant.
        return standing
    await redis.hset(
        registry_key(user_uuid),
        mapping={
            REGISTRY_FIELD_PREVIEW_STAY_UNTIL: deadline.isoformat(timespec="microseconds"),
            REGISTRY_FIELD_STAY_WRITER: str(writer),
        },
    )
    return deadline


# Push the stay forward, but ONLY onto the hash that still names the container the caller aimed
# at. Three reads and a write, atomic — a Redis Lua script runs single-threaded.
#
# WHY THE IDENTITY GUARD HAS TO BE INSIDE THE SCRIPT. `registry:{user_id}` is per USER, and it
# survives a container swap: opening a second project rewrites the same key in place. A browser
# renewing on a timer is exactly the caller that can be mid-flight across that swap, and a
# Python-side read followed by a Python-side write leaves open the interleaving where the tab
# holding project A pushes a stay onto project B's freshly registered container — sparing a
# container A's own teardown is about to delete, under a reprieve nobody granted it.
#
# A refusal is not an error. `not_this_container` is the ordinary reading a moment after somebody
# opens another project, and the tab that asked is told which of the three happened.
# THE MONOTONIC COMPARISON HAPPENS IN HERE, not in the caller. Reading the standing deadline in
# Python and writing the larger one back is two round trips with a gap between them, and a surface
# is renewing every forty-five seconds: a hidden tab buying twenty minutes and a visible one buying
# five can interleave so the five-minute write lands last and cuts fifteen minutes off a reprieve
# already granted — the exact thing this function's docstring promises can never happen.
#
# THE COMPARISON IS LEXICOGRAPHIC, AND THAT IS ONLY SOUND BECAUSE THE VALUES ARE FIXED WIDTH. Every
# writer here stamps `isoformat(timespec="microseconds")` on an aware UTC instant, so the strings
# share one offset and one length and sort exactly as the instants do. Drop the timespec and a
# whole-microsecond value renders three characters shorter, which sorts BEFORE a longer string in
# the same second — wrong, rarely, in a guard whose whole job is never to be wrong.
#
# A standing value that is longer WINS and is returned, so the caller reports the deadline that is
# actually in force rather than the one it proposed.
_CAS_GRANT_PRESENCE_STAY_LUA: Final = (
    "if redis.call('EXISTS', KEYS[1]) == 0 then return 'nothing_running' end "
    f"if redis.call('HGET', KEYS[1], '{REGISTRY_FIELD_APP_NAME}') ~= ARGV[1] "
    "then return 'not_this_container' end "
    f"local standing = redis.call('HGET', KEYS[1], '{REGISTRY_FIELD_PREVIEW_STAY_UNTIL}') "
    "if standing and standing ~= '' and standing >= ARGV[2] "
    "then return 'renewed:' .. standing end "
    f"redis.call('HSET', KEYS[1], '{REGISTRY_FIELD_PREVIEW_STAY_UNTIL}', ARGV[2], "
    f"'{REGISTRY_FIELD_STAY_WRITER}', ARGV[3]) return 'renewed:' .. ARGV[2]"
)


async def renew_presence_stay(
    redis: aioredis.Redis,
    user_uuid: uuid.UUID,
    *,
    app_name: str,
    presence: SurfacePresence,
) -> tuple[RenewalOutcome, datetime | None]:
    """Hold this project's container open because a surface framing it is still there.

    THE DEADLINE STILL NEVER MOVES BACKWARD. The `max(standing, computed)` that
    `grant_stay_of_execution` performs is performed here too, and it matters more: a hidden
    surface buys twenty minutes and a visible one buys five, so the very next tick after a tab
    comes back on screen would otherwise cut fifteen minutes off a reprieve already granted.

    The comparison and the write are ONE script, so two surfaces renewing at once cannot interleave
    into a shorter deadline; the returned instant is whichever one is actually in force."""
    ttl = PRESENCE_STAY_SECONDS[presence]
    deadline = datetime.now(UTC) + timedelta(seconds=ttl)
    answer = await redis.eval(
        _CAS_GRANT_PRESENCE_STAY_LUA,
        1,
        registry_key(user_uuid),
        app_name,
        deadline.isoformat(timespec="microseconds"),
        str(DeadlineWriter.SURFACE_PRESENT),
    )
    raw = answer.decode() if isinstance(answer, bytes) else str(answer)
    if not raw.startswith("renewed:"):
        return RenewalOutcome(raw), None
    return RenewalOutcome.RENEWED, _the_instant_in_force(raw.removeprefix("renewed:"))


# Retire a provisioning stay now that provisioning is demonstrably over, guarded on the same
# `app_name` identity every other write here carries.
#
# THE ONE WRITE IN THIS MODULE THAT MAY SHORTEN A DEADLINE, and the exception is narrow enough to
# state exactly: a start grants a long stay because a hung restore can block for the better part
# of twenty minutes with nothing else protecting the container — not the heartbeat, which is not
# seeded yet, and not the starting marker, whose TTL is shorter than that worst case. The moment
# the app answers a request, that reason is gone. Leaving the long stay standing would keep a
# container nobody came back to alive for half an hour after the tab closed, which is precisely
# what presence renewal exists to stop paying for.
#
# It is issued by the same code path that granted the long stay, at the point that path can prove
# provisioning ended. `grant_stay_of_execution`'s monotonic rule is untouched and still governs
# every OTHER writer: it exists so a weaker writer cannot truncate a stronger one's reprieve, and
# nothing here is a different writer arriving with a weaker claim.
_CAS_SETTLE_STAY_LUA: Final = (
    f"if redis.call('HGET', KEYS[1], '{REGISTRY_FIELD_APP_NAME}') ~= ARGV[1] then return 0 end "
    f"redis.call('HSET', KEYS[1], '{REGISTRY_FIELD_PREVIEW_STAY_UNTIL}', ARGV[2], "
    f"'{REGISTRY_FIELD_STAY_WRITER}', ARGV[3]) return 1"
)


async def settle_stay_once_the_app_is_serving(
    redis: aioredis.Redis, user_uuid: uuid.UUID, *, app_name: str
) -> datetime | None:
    """Hand this container's lifetime to the screen that asked for it, and return the deadline.

    From here the surface framing the app renews on its own poll, so what the platform owes is the
    GAP until the first renewal arrives and nothing more.

    NEVER LENGTHENS. A standing stay already shorter than the grace — a presence renewal that has
    landed while the start was finishing — is left exactly where it is, so this can only ever give
    a container less time, never more.

    `None` when the registry names a different container, or none at all: a start whose slot was
    taken while it finished has nothing here to settle."""
    grace = datetime.now(UTC) + timedelta(seconds=SURFACE_PRESENT_STAY_SECONDS)
    standing = await _standing_stay(redis, user_uuid)
    if standing is not None and standing <= grace:
        return standing
    settled = await redis.eval(
        _CAS_SETTLE_STAY_LUA,
        1,
        registry_key(user_uuid),
        app_name,
        grace.isoformat(timespec="microseconds"),
        str(DeadlineWriter.SURFACE_PRESENT),
    )
    return grace if settled else None


def _the_instant_in_force(raw: str) -> datetime | None:
    """The deadline the script reported it kept, or `None` when it cannot be read.

    `None` here means only that the CALLER cannot name the instant — the write itself succeeded
    and the hash holds whichever value won. A renewal reporting no instant is still a renewal."""
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


async def _standing_stay(redis: aioredis.Redis, user_uuid: uuid.UUID) -> datetime | None:
    """The stay currently on the hash, or `None` when absent or unreadable.

    An unreadable value reads as ABSENT here, which lets the new writer overwrite it. That is the
    opposite of `stay_of_execution_is_current`'s fail-closed reading, and deliberately so: there,
    an unparseable value must not spare a container; here, it must not block a legitimate
    extension and strand a live preview behind a corrupt field."""
    raw = await redis.hget(registry_key(user_uuid), REGISTRY_FIELD_PREVIEW_STAY_UNTIL)
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.decode() if isinstance(raw, bytes) else str(raw))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


async def stay_of_execution_is_current(redis: aioredis.Redis, user_uuid: uuid.UUID) -> bool:
    """True only while a granted stay is demonstrably unexpired AND inside the bound this
    module could ever have granted (`now < deadline <= now + RELAUNCH_PREVIEW_STAY_SECONDS`).
    Fails CLOSED — absent, empty, unparseable, or absurd ⇒ False (reapable).

    "Unexpired" alone is NOT enough: a parseable year-9999 stamp (bad clock, hand-edited hash,
    a future writer using a different unit) would grant a millennia-long reprieve reached
    through the parse rather than around it — so the window is bounded on BOTH sides, and
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


# --- the serving proof --------------------------------------------------------
# ANOTHER FIELD ON THE REGISTRY HASH, and the only one that means the app ANSWERED something.
# `state == ready` says a container was scheduled; this says something watched it serve, and the
# preview pane is allowed to say "your app is running" off this field and nothing else. Both
# writes below are compare-and-sets against the very hash they stamp, for the reason spelled out
# on the first script.

# Stamp `serving_since`, but only if this hash still names the container the observer watched, is
# still `ready`, and has never been stamped. Three reads and a write, atomic (a Redis Lua script
# runs single-threaded, so nothing interleaves).
#
# WHY A COMPARE-AND-SET AND NOT `redis.exists()` + `HSETNX`. The registry key is
# `registry:{user_id}` — per USER, not per container — and it SURVIVES a container swap: when the
# one-per-user slot flips to another of this citizen's projects, the same key is rewritten in
# place. So `exists()` is true for the REPLACEMENT container just as it was for the original, and
# a slow observer returning after the flip would stamp "serving" onto a container it never
# watched — after which the pane frames the new project's app on the old one's evidence. The
# `app_name` comparison is what refuses that, and it has to happen INSIDE the script: a
# Python-side read followed by a Python-side write leaves open exactly the interleaving the flip
# needs. A refusal is not an error — it is `SERVING_PROOF_STAMP_REFUSED`, the near-miss recorded.
#
# AND WHY `HSETNX` ALONE CANNOT WORK: the create-time write in `sandbox/client.py` puts an
# empty-string SENTINEL in the field, so the field always exists and `HSETNX` would refuse every
# legitimate first stamp — silently, with the same 0 a genuine second stamp returns. Emptiness,
# not absence, is how "never served" is spelled; `redis/keys.py` says why absence is reserved.
#
# The script returns a literal 1 rather than the `HSET` reply because `HSET` answers 0 when it
# overwrites an existing field, which the sentinel always is — returning it would report every
# successful first stamp as a refusal.
_CAS_MARK_SERVING_LUA: Final = (
    f"if redis.call('HGET', KEYS[1], '{REGISTRY_FIELD_APP_NAME}') ~= ARGV[1] then return 0 end "
    f"if redis.call('HGET', KEYS[1], '{REGISTRY_FIELD_STATE}') ~= '{REGISTRY_STATE_READY}' "
    "then return 0 end "
    f"local stamped = redis.call('HGET', KEYS[1], '{REGISTRY_FIELD_SERVING_SINCE}') "
    "if stamped and stamped ~= '' then return 0 end "
    f"redis.call('HSET', KEYS[1], '{REGISTRY_FIELD_SERVING_SINCE}', ARGV[2]) return 1"
)

# Retract a standing proof, guarded on the same `app_name` identity for the same reason.
#
# BACK TO THE SENTINEL, NEVER `HDEL`: an absent field is the PRE-CUTOVER reading, which is
# PROVEN, so deleting the stamp would turn a crashed app into a running one. And the proof has to
# be actually standing — answering 0 for "there was nothing to retract" is what stops the crash
# edge logging a loss that never happened.
#
# DELIBERATELY NOT GUARDED ON `state`, unlike the stamp above: a crash edge can be observed after
# the reaper has already flipped this hash to `ending`, and refusing there would leave a proof
# standing on a container being torn down.
_CAS_CLEAR_SERVING_LUA: Final = (
    f"if redis.call('HGET', KEYS[1], '{REGISTRY_FIELD_APP_NAME}') ~= ARGV[1] then return 0 end "
    f"local stamped = redis.call('HGET', KEYS[1], '{REGISTRY_FIELD_SERVING_SINCE}') "
    "if not stamped or stamped == '' then return 0 end "
    f"redis.call('HSET', KEYS[1], '{REGISTRY_FIELD_SERVING_SINCE}', '') return 1"
)


async def mark_serving(
    redis: aioredis.Redis, user_uuid: uuid.UUID, *, app_name: str, when: datetime
) -> bool:
    """Record the instant this user's sandbox was first watched to SERVE a request. True iff
    this call is the one that stamped it.

    FIRST SERVE WINS. A second observer of the same container changes nothing and returns False,
    so the field keeps meaning "first serve" rather than "most recent sighting" — which is the
    only reason `ms_since_container_created` on the `app_first_served` line is an answer to
    anything.

    False therefore carries two situations, and the caller reacts identically to both: do not
    stamp. It is a no-op second sighting, or the write was REFUSED because the hash is gone,
    marked `ending`, or names another project's container — the near-miss that owes a
    `SERVING_PROOF_STAMP_REFUSED`. Only the caller, which knows whether it had already watched
    this container serve, can tell those apart, so only the caller logs.

    BARE on Redis errors, per the module's REDIS-ERROR POLICY: this is answer-bearing, and a
    swallowed error would hand the caller a refusal that never happened."""
    # Defensive, the same reading `_standing_stay` gives a deadline: a naive stamp is read as
    # UTC, never local. A naive value written here would be ambiguous on the hash forever, and
    # the hash outlives the process that wrote it.
    stamp = when if when.tzinfo is not None else when.replace(tzinfo=UTC)
    stamped = await redis.eval(
        _CAS_MARK_SERVING_LUA, 1, registry_key(user_uuid), app_name, stamp.isoformat()
    )
    return bool(stamped)


async def clear_serving(redis: aioredis.Redis, user_uuid: uuid.UUID, *, app_name: str) -> bool:
    """Retract this user's sandbox's serving proof — the app stopped answering. True iff a
    standing proof was actually retracted.

    Idempotent, and False on a hash that names a different container: the identity guard the
    stamp carries, for the identical reason. Retracting costs the citizen a card, not an error
    page — the pane falls back to "getting your app ready" — but callers still debounce, because
    a single unanswered poll is a poll, not a dead app."""
    cleared = await redis.eval(_CAS_CLEAR_SERVING_LUA, 1, registry_key(user_uuid), app_name)
    return bool(cleared)


# --- registry state -----------------------------------------------------------
# The concrete sandbox client owns registry CREATE/DELETE (services/sandbox/client.py); these
# helpers are the reaper's read + the mark-ending flip. Both use the frozen key
# builders — the sandbox layer and this layer never share a helper module (the frozen
# keys.py IS the shared contract), because sandbox/ must not import build_sessions/.


# --- READING WHAT THE STAMP SAYS ---------------------------------------------------------------
#
# THE THREE READERS EVERY OBSERVER SHARES, and they live here because here is the one module the
# turn engine, the session manager and the reaper all already import. They were written four
# times over — `engine._elapsed_ms`, `manager._ms_since_created`, `manager._first_served_at` and
# `reaper._an_instant_on_the_hash`, plus three more inline subtractions in the reaper — because
# each of those files was written against a different half of the same change and none could edit
# another. Four parsers of one field format is four places for a naive-datetime bug to hide in.
#
# NOT in `redis/keys.py`, which is the FROZEN key/field contract and is imported by
# `services/sandbox/` — a layer that must not reach into `build_sessions/`. Keys stay a vocabulary;
# these are the reading of it.


def an_instant_on_the_hash(reg: Mapping[str, str] | None, field: str) -> datetime | None:
    """One ISO-8601 field off a registry record, or `None` when the hash cannot say.

    UNREADABLE READS AS UNKNOWN, never as "ancient". Callers use these instants to decide whether
    a live observer might still be waiting, and a corrupt field resolving to the beginning of time
    would answer "nobody is coming" about a container three seconds old.

    A NAIVE VALUE IS UTC, never local. Every writer of these fields writes UTC, and the hash
    outlives the process that wrote it — guessing local time for a record another deployment left
    behind would silently offset every derived number by hours.
    """
    if reg is None:
        return None
    raw = reg.get(field)
    if not raw:
        return None
    try:
        instant = datetime.fromisoformat(raw)
    except ValueError:
        return None
    return instant if instant.tzinfo is not None else instant.replace(tzinfo=UTC)


def elapsed_ms(since: datetime | None, until: datetime) -> int | None:
    """Milliseconds between two instants, or `None` when the first one is unknown.

    THE SUBTRACTION HAPPENS ONCE SO THE OPERATOR DOES NOT HAVE TO. `app_first_served`'s
    `ms_since_container_created` and `app_serving_lost`'s `served_for_ms` are both "how long
    between two timestamps that end up in two different log lines" — and the 2026-09-10
    measurement had to reconstruct exactly that number off a screen recording, because no line
    carried it. `None` rather than a raise: this feeds a log field, and a malformed timestamp
    written by some older process must not be able to kill the observer that is reporting it.
    """
    return None if since is None else int((until - since).total_seconds() * 1000)


def stamp_is_proven(reg: Mapping[str, str]) -> bool:
    """Has anything ever watched this container's app ANSWER a request?

    THE ONE PLACE THE THREE READINGS ARE DECIDED, and `redis/keys.py::REGISTRY_FIELD_SERVING_SINCE`
    is the contract it implements:
      absent   -> a hash written before the stamp existed: PRE-CUTOVER, read as PROVEN.
      ""       -> the container exists and has NEVER served: UNPROVEN.
      ISO-8601 -> the instant of first serve: PROVEN.

    ABSENT MUST STAY PROVEN until the fleet has turned over. At the deploy instant every live hash
    is `state=ready` with no stamp, and reading that as unproven would flip every working preview
    to `starting` — which the frame veto withholds the iframe on — unframing healthy apps at fleet
    scale. That is a false negative in the exact shape `PreviewLifeState` was written to kill, and
    it is worse than the eight-second window this change closes. DELETABLE one stay window after
    deploy (nothing writes a hash without the field any more, create-time seed and legacy adoption
    included), and `test_preview_state.py` pins all three readings so that deletion is a one-line
    change against a red test.

    KEPT SEPARATE FROM `_registry_serves_and_is_ready`, deliberately: that predicate is also the
    start path's "is there a container I can reuse?", and a container that is merely still booting
    is perfectly reusable. Tightening it there would tear down healthy containers and make cold
    starts worse. Two questions, one comparison each.
    """
    stamp = reg.get(REGISTRY_FIELD_SERVING_SINCE)
    return stamp is None or stamp != ""


async def read_registry(redis: aioredis.Redis, user_uuid: uuid.UUID) -> dict[str, str] | None:
    """Read the sandbox record, falling back to the legacy key and migrating what it finds.
    The dual-read has to live HERE — `sweep_all` re-enters here with the parsed user id rather
    than reading off its scan, so a legacy record reached by any other route is never rescued.

    Precedence is current-then-legacy, never reversed: every write goes to the current key, the
    newer claim by definition. `SandboxClient._read_registry` must behave identically (kept
    separate — sandbox/ may not import build_sessions/) — `test_key_migration.py` stops them
    drifting."""
    raw = await redis.hgetall(registry_key(user_uuid))
    if raw:
        return {str(k): str(v) for k, v in raw.items()}
    return await _adopt_a_pre_cutover_record(redis, user_uuid)


async def _adopt_a_pre_cutover_record(
    redis: aioredis.Redis, user_uuid: uuid.UUID
) -> dict[str, str] | None:
    """Migrate one legacy-prefix registry hash into the environment-scoped namespace, on read:
    `COPY`, not a read-then-`HSET`, so a field this module has never heard of (e.g.
    `preview_stay_until`, from a different subsystem) is never dropped in transit.

    Copy first, delete second: dying between them leaves a legacy key the next read ignores
    (current key wins) and `delete_registry` clears from both prefixes either way; the reverse
    order would lose the record outright.

    TWO FIELDS ARE ADDED rather than copied, because a legacy record cannot carry either: the
    adoption marker, and the serving proof — see the comment on the write below for why a
    verbatim copy of the proof's ABSENCE would be a rollout-breaking bug rather than a
    faithful mirror."""
    raw = await redis.hgetall(legacy_registry_key(user_uuid))
    if not raw:
        return None

    # SINGLE-KEY COMMANDS ONLY (module docstring): the tempting `COPY legacy
    # current` is cross-slot and is rejected outright in production. Getting it wrong fails on
    # the path built to RESCUE the fleet — `read_registry` is deliberately unguarded, so every
    # pre-cutover user's attach would 500 and no legacy record would ever migrate.
    #
    # `hset(mapping=raw)` carries the identical field set, because `raw` is already the complete
    # hash from the HGETALL above. What COPY bought was server-side atomicity against a racing
    # writer — see the narrowed race below.
    #
    # THE LEGACY KEY IS NOT DELETED HERE. It used to be, and that made the mitigation worse than
    # the exposure it mitigates: a process pointed at the WRONG Redis would not merely read another
    # environment's legacy record, it would relocate it under its own prefix and delete the
    # original — leaving the owning environment with a running container and no record. That is
    # precisely the orphan class the sweep exists to collect, manufactured by the very fix meant to
    # prevent it. Termination does not depend on this delete: `delete_registry` clears BOTH
    # prefixes when the session ends, and once the current key exists this function is never
    # reached again (the caller finds the current key first).
    if await redis.exists(registry_key(user_uuid)):
        # A racing writer created the current record between the HGETALL above and here, and
        # THAT record is the newer claim. Answering with the legacy hash still in hand would
        # return a superseded `app_name` — a teardown pointed at the wrong container.
        current = await redis.hgetall(registry_key(user_uuid))
        return {str(k): str(v) for k, v in current.items()} if current else None

    inherited = {str(k): str(v) for k, v in raw.items()}

    # THE MIRROR WRITES THE SERVING PROOF EXPLICITLY, and that is load-bearing rather than tidy.
    # A verbatim copy would carry no `serving_since`, and an adoption can land at ANY time — a
    # pre-cutover container attached months from now still arrives here — so "absent can only
    # mean pre-cutover" would never become true, and the grandfather arm that reads absence as
    # PROVEN could never be safely retired. Writing a real instant is precisely what makes
    # absence impossible on any record written after the cutover.
    #
    # The instant is this record's OWN `created_at`, and PROVEN is the correct reading for it:
    # the container was scheduled before the stamp existed, and it is very likely serving a
    # citizen at this moment. The alternative — the `""` sentinel — would retire a live preview
    # on the spot, which is the single thing the grandfather arm exists to prevent.
    first_served_at = inherited.get(REGISTRY_FIELD_CREATED_AT, "")
    if not first_served_at:
        # A truncated legacy hash: no birthday to inherit. Falling back to NOW keeps the PROVEN
        # reading without inventing a history — the instant recorded is simply the earliest this
        # platform can honestly claim to have known the container was there.
        _log.warning(
            "adopting a legacy registry record with no created_at; stamping its serving proof "
            "at the adoption instant instead",
            user_id=str(user_uuid),
        )
        first_served_at = datetime.now(UTC).isoformat()

    # The residual race COPY closed and this does not: a writer landing between the `exists`
    # above and the `hset` below is overwritten. It is narrow and benign in the shapes that
    # actually occur — two concurrent MIGRATIONS write byte-identical content, and the only
    # other writer (`_write_registry` during provisioning) runs under the per-user start lock,
    # which a caller reaching this line does not hold. Accepted deliberately over a command
    # that cannot run on the substrate.
    # Inline comprehension, not the `inherited` variable a reader would reach for first (nor any
    # other `dict[str, str]`): redis-py types `mapping` as `Mapping[FieldT, EncodableT]` whose
    # KEY parameter is invariant, so a named `dict[str, str]` fails every type gate — spread into
    # the literal as well — while the identical inline literal passes.
    await redis.hset(
        registry_key(user_uuid),
        # The adoption marker rides along in the same write, so a record can never exist under the
        # current prefix having been adopted without saying so. `delete_registry` is the only
        # reader: it is what tells "this environment owns the legacy key too" apart from "some
        # other deployment does".
        mapping={
            **{str(k): str(v) for k, v in raw.items()},
            REGISTRY_FIELD_ADOPTED_FROM_LEGACY: "1",
            REGISTRY_FIELD_SERVING_SINCE: first_served_at,
        },
    )
    _log.info(
        "sandbox_registry_migrated_to_the_environment_namespace",
        user_id=str(user_uuid),
        detail=(
            "a record written before R22, copied under the environment prefix; the legacy key "
            "is left for delete_registry, never removed on read"
        ),
    )
    # The serving proof goes back to the caller, unlike the adoption marker beside it: the marker
    # is bookkeeping between this migration and the delete and nothing should branch on it, while
    # the proof is exactly a field consumers branch on — a caller handed a record whose stamp it
    # cannot see would have to re-read the hash to learn what this write just put there.
    return inherited | {REGISTRY_FIELD_SERVING_SINCE: first_served_at}


async def mark_registry_ending(redis: aioredis.Redis, user_uuid: uuid.UUID) -> None:
    """Flip the registry `state` to `ending`, set FIRST in the reaper
    ordering so a concurrent `attach_existing` sees a dying container and does not
    reconnect. Guarded on existence so it never conjures a partial registry hash."""
    if await redis.exists(registry_key(user_uuid)):
        await redis.hset(registry_key(user_uuid), REGISTRY_FIELD_STATE, REGISTRY_STATE_ENDING)


# Delete the record ONLY while it still names this container, and report whether the legacy key
# went with it. The guard belongs inside the script: a Python-side read followed by a Python-side
# delete leaves exactly the gap a start needs to register its replacement. 2 means the legacy key
# is ours to clear too.
_CAS_DELETE_REGISTRY_BY_NAME_LUA: Final = (
    f"if redis.call('HGET', KEYS[1], '{REGISTRY_FIELD_APP_NAME}') ~= ARGV[1] then return 0 end "
    f"local adopted = redis.call('HGET', KEYS[1], '{REGISTRY_FIELD_ADOPTED_FROM_LEGACY}') "
    "redis.call('DEL', KEYS[1]) "
    "if adopted then return 2 end return 1"
)

#: `_CAS_DELETE_REGISTRY_BY_NAME_LUA`'s "and the legacy key too" answer.
_ALSO_THE_LEGACY_KEY: Final = 2


async def delete_registry_if_it_still_names(
    redis: aioredis.Redis, user_uuid: uuid.UUID, app_name: str
) -> bool:
    """Clear this citizen's sandbox record, but only while it still names `app_name`. True when it
    did.

    THE PER-USER RECORD OUTLIVES THE SESSION THAT WROTE IT. One key holds whichever container this
    citizen's single workspace is currently running, and a switch replaces its contents while the
    outgoing session is still unwinding — so a session that deletes by user id alone deletes
    whatever arrived after it, and the container that record named goes on running with nothing
    left to find it. Ask by name, and a session that is merely late cannot take the live one's
    place away."""
    run_script = redis.eval  # aliased to keep the call off the JS-oriented eval guard
    deleted = await run_script(
        _CAS_DELETE_REGISTRY_BY_NAME_LUA, 1, registry_key(user_uuid), app_name
    )
    if int(deleted) == _ALSO_THE_LEGACY_KEY:
        await redis.delete(legacy_registry_key(user_uuid))
    return bool(deleted)


async def delete_registry(redis: aioredis.Redis, user_uuid: uuid.UUID) -> None:
    """Clear the sandbox record under BOTH prefixes — the ONLY place the legacy key is removed
    (migration-on-read leaves it; see `_adopt_a_pre_cutover_record`), so a pre-cutover record
    doesn't outlive its session and get retried by every later pass forever.

    TWO single-key deletes (sharding, see module docstring), current-first. CONDITIONAL on THIS
    environment having adopted the record — an unconditional delete would destroy ANOTHER
    deployment's record sharing this Redis; re-adoption on the next read keeps termination
    guaranteed either way."""
    adopted = await redis.hget(registry_key(user_uuid), REGISTRY_FIELD_ADOPTED_FROM_LEGACY)
    await redis.delete(registry_key(user_uuid))
    if adopted:
        await redis.delete(legacy_registry_key(user_uuid))
