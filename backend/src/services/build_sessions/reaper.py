"""The reaper: reconcile-on-start + the full sweep (C5, KTD-3).

There is NO in-process background sweeper (that would re-open the Stage-0-frozen
`main.py` lifespan). Instead:

* `reconcile_user` runs at the top of every `start`, reaping the requesting user's OWN
  stale lock/registry/heartbeat before acquiring — this closes the "crashed tab → can
  never start again" lockout at the exact moment it matters.
* `sweep_all` reconciles EVERY registered user; it is idempotent + concurrency-safe
  (teardown idempotent, value-guarded reaper release), so an operator can trigger it on
  a timer via the `internal/reap` endpoint.

Reaper ordering for one stale user (C5): mark-ending → teardown → clear registry →
release lock (LAST). The reaper reclaims a possibly-drifted lock via the value-guarded
`reap_lock`, NEVER the holder release (the crashed session's in-process token is gone).
"""

from __future__ import annotations

import uuid
from typing import Final

import redis.asyncio as aioredis
import structlog

from src.services.build_sessions.locks import (
    delete_registry,
    heartbeat_is_alive,
    lock_is_held,
    mark_registry_ending,
    read_registry,
    reap_lock,
    stay_of_execution_is_current,
)
from src.services.redis import KEY_PREFIX
from src.services.redis.keys import REGISTRY_FIELD_APP_NAME, REGISTRY_FIELD_FQDN
from src.services.sandbox import SandboxClient, SandboxError, SandboxHandle

_log = structlog.get_logger()

_REGISTRY_SCAN_MATCH: Final = f"{KEY_PREFIX}registry:*"


def _user_from_registry_key(key: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(key.rsplit(":", 1)[-1])
    except ValueError:
        return None


def _minimal_handle(reg: dict[str, str]) -> SandboxHandle:
    """Reconstruct the minimal handle the reaper needs to tear down a crashed session's
    container — ACA delete is keyed by `app_name`, and no in-process token survives."""
    fqdn = reg.get(REGISTRY_FIELD_FQDN, "")
    return SandboxHandle(
        fqdn=fqdn,
        token="",
        app_name=reg.get(REGISTRY_FIELD_APP_NAME, ""),
        preview_url=f"https://{fqdn}/",
        ready=False,
    )


async def reap_user(
    redis: aioredis.Redis, user_uuid: uuid.UUID, sandbox_client: SandboxClient
) -> bool:
    """The ordered reap for ONE user's stale sandbox. Returns True if it reaped."""
    reg = await read_registry(redis, user_uuid)
    if reg is None:
        # No sandbox registered — just clear any orphaned lock so a crashed-tab user is
        # never locked out (the reconcile side of KTD-3).
        await reap_lock(redis, user_uuid)
        return False
    await mark_registry_ending(redis, user_uuid)  # step 1: guard a concurrent attach
    try:
        await sandbox_client.teardown(_minimal_handle(reg))  # step 2: idempotent teardown
    except SandboxError:
        # Teardown failed — KEEP the lock + registry so a later sweep retries; clearing
        # them now would orphan a still-live container. Not silent (logged).
        _log.exception(
            "reaper teardown failed; leaving state for a later sweep", user_id=str(user_uuid)
        )
        return False
    await delete_registry(redis, user_uuid)  # registry cleared
    await reap_lock(redis, user_uuid)  # step 3: release the (possibly drifted) lock — LAST
    return True


async def reconcile_user(
    redis: aioredis.Redis,
    user_uuid: uuid.UUID,
    sandbox_client: SandboxClient,
    *,
    has_live_session: bool,
    honor_stay: bool = False,
) -> bool:
    """Reconcile the user's OWN stale state. Reap ONLY when a registry entry exists AND
    this process holds NO live in-process session for the user (load-bearing: `run_build`
    outlives the SSE disconnect, so a live multi-minute build whose tab closed >90 s would
    otherwise be reaped mid-flight) AND (the lock is gone OR the heartbeat has lapsed).
    Returns True if it reaped.

    `honor_stay` is the ASYMMETRY between this function's two callers, and it is
    deliberate — do NOT "simplify" it to one behaviour:

    * `sweep_all` (background timer) passes `honor_stay=True`. A relaunched preview (#43)
      holds no lock and renews no heartbeat, so it trips the guard above the instant its
      seeded heartbeat lapses; its bounded stay of execution is the ONLY thing standing
      between a preview the user is actively viewing and the sweep. Honoring it there is
      the entire point of the lease.
    * reconcile-on-start keeps the default `honor_stay=False` and reaps THROUGH an
      unexpired stay. The incoming build needs the single per-user sandbox slot: if start
      spared the preview, the build would register its own container over that registry
      entry and ORPHAN the preview's container — a strictly worse leak than the one the
      lease exists to fix.
    """
    if has_live_session:
        return False  # a session this process still owns is never reaped by heartbeat lapse
    reg = await read_registry(redis, user_uuid)
    if reg is None:
        await reap_lock(redis, user_uuid)  # clear any orphaned lock (no lockout)
        return False
    if await lock_is_held(redis, user_uuid) and await heartbeat_is_alive(redis, user_uuid):
        return False  # looks live + recent (bounded by the heartbeat TTL) — leave it
    if honor_stay and await stay_of_execution_is_current(redis, user_uuid):
        return False  # a relaunched preview inside its lease — the sweep spares it
    return await reap_user(redis, user_uuid, sandbox_client)


async def sweep_all(
    redis: aioredis.Redis,
    sandbox_client: SandboxClient,
    *,
    live_users: set[uuid.UUID] | None = None,
) -> int:
    """SCAN-iterate the registry namespace (never `KEYS`) and reconcile each user;
    returns the number reaped. Idempotent + concurrency-safe, so it is safe to call on a
    timer (KTD-3). `live_users` are the SessionManager's live in-process sessions —
    never reaped.

    Passes `honor_stay=True`: a relaunched preview (#43) inside its bounded stay of
    execution is spared here, because a timer has no reason to kill a container the user
    is still looking at. Reconcile-on-start passes the opposite (see `reconcile_user`) —
    that build needs the slot, and sparing the preview there would orphan its container.
    The asymmetry is the design, not an oversight."""
    live = live_users if live_users is not None else set()
    reaped = 0
    async for raw_key in redis.scan_iter(match=_REGISTRY_SCAN_MATCH):
        user_uuid = _user_from_registry_key(str(raw_key))
        if user_uuid is None or user_uuid in live:
            continue
        if await reconcile_user(
            redis, user_uuid, sandbox_client, has_live_session=False, honor_stay=True
        ):
            reaped += 1
    return reaped
