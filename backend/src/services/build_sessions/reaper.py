"""The reaper: reconcile-on-start + the full sweep (C5, KTD-3).

Two entry points, plus a background sweeper added in 1.6.5 (`main.py`'s lifespan runs
`_reap_abandoned_sandboxes` on a `SWEEP_INTERVAL_SECONDS` timer — this docstring used to say no
such thing existed, which stopped being true when abandoned-sandbox reclamation shipped):

* `reconcile_user` runs at the top of every `start`, reaping the requesting user's OWN
  stale lock/registry/heartbeat before acquiring — this closes the "crashed tab → can
  never start again" lockout at the exact moment it matters.
* `sweep_all` reconciles EVERY registered user; it is idempotent + concurrency-safe
  (teardown idempotent, value-guarded reaper release), so an operator can trigger it on
  a timer via the `internal/reap` endpoint.

WHAT THE SWEEP STRUCTURALLY CANNOT SEE. It enumerates from Redis (`_REGISTRY_SCAN_MATCH`), so it
only ever reaches a container it already has a record of. A sandbox whose registry entry is gone
— a flushed or replaced Redis, a container older than the registry, a teardown that failed after
`delete_registry` — is invisible here FOREVER and bills until a human notices; one did, for
twelve days. The Azure-side view that closes that gap is `inventory.take_sandbox_inventory`,
surfaced at `POST /v1/admin/reconcile-sandboxes`, and it REPORTS rather than deletes.

Reaper ordering for one stale user (C5): mark-ending → teardown → clear registry →
release lock (LAST). The reaper reclaims a possibly-drifted lock via the value-guarded
`reap_lock`, NEVER the holder release (the crashed session's in-process token is gone).

SINGLE-REPLICA CONSTRAINT (binding — see the deploy checklist). The live-session shield
below (`has_live_session` / `sweep_all`'s `live_users`) reads an IN-PROCESS set. On a
second replica that set is blind to the first replica's builds, so replica B would reap a
sandbox replica A is actively building in — and it bites precisely in the quiet stretches
the shield was written for, because the only cross-process liveness signal here
(`lock_is_held AND heartbeat_is_alive`) lapses at the heartbeat TTL between renews.
Scaling past one replica needs a shared liveness view, not just a shared Redis, so it is a
deploy-time constraint, not a runtime guard (a process cannot detect its siblings — the
origin's Scope Boundaries reject a startup assertion for exactly that reason).

Corrected 2026-08-11 (ADR-0029): the sentence removed here read "There is no in-process
background sweeper by design" — which flatly contradicted this docstring's OWN opening
paragraph 26 lines above, added when the 1.6.5 sweeper shipped. Both statements lived in one
file for months and the false one is the one people quoted. ADR-0011 is now Accepted and the
lease that removes this constraint is R10 (ADR-0029 §8); until that lands, the single-replica
pin still binds.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
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
    redis: aioredis.Redis,
    user_uuid: uuid.UUID,
    sandbox_client: SandboxClient,
    *,
    strict: bool = False,
) -> bool:
    """The ordered reap for ONE user's stale sandbox. Returns True if it reaped.

    `strict` decides who owns a FAILED teardown, and exists because `False` answers two
    different questions with one value: "there was nothing registered" and "there was, and
    it would not die". A sweep cannot tell them apart and does not need to — it is
    fire-and-forget, it runs again in five minutes, and raising at it would only turn a
    retryable blip into a crashed background task. So the default stays lenient.

    A caller that is about to ACT on the outcome does need them apart. `release_project_
    sandbox` frees the slot so another project can take it; if the container is still
    standing, the very next thing the client does is walk back into the reclaim refusal it
    was just told had been resolved. `strict=True` re-raises so that caller can answer 503
    instead of reporting a release that did not happen (#83 review, blocker 2).

    Either way the lock and registry are KEPT on failure so a later sweep retries — the
    strict arm changes who is told, never what is left behind."""
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
        if strict:
            raise
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
    certified_dead: bool = False,
) -> bool:
    """Reconcile the user's OWN stale state. Reap ONLY when a registry entry exists AND
    this process holds NO live in-process session for the user (load-bearing: `run_build`
    outlives the SSE disconnect, so a live multi-minute build whose tab closed >90 s would
    otherwise be reaped mid-flight) AND the state does not merely LOOK live (see
    `certified_dead` for when "looks live" is provably a lie). Returns True if it reaped.

    `honor_stay` is the ASYMMETRY between this function's two callers, and it is
    deliberate — do NOT "simplify" it to one behaviour:

    * `sweep_all` (background timer) passes `honor_stay=True`. A relaunched preview (#43)
      — and, identically, a COMPLETED build's pardoned preview (#13/R2) — holds no lock
      and renews no heartbeat, so it trips the guard above the instant its heartbeat
      lapses; its bounded stay of execution is the ONLY thing standing between a preview
      the user is actively viewing and the sweep. Honoring it there is the entire point
      of the lease.
    * reconcile-on-start keeps the default `honor_stay=False` and reaps THROUGH an
      unexpired stay. The incoming build needs the single per-user sandbox slot: if start
      spared the preview, the build would register its own container over that registry
      entry and ORPHAN the preview's container — a strictly worse leak than the one the
      lease exists to fix.

    `certified_dead` (#10/R3 — the 409 reap-through) is the second caller asymmetry:

    * The sweep keeps the default `False`, so `lock_is_held AND heartbeat_is_alive` still
      shields what it was built to shield — an in-flight start's pre-adopt window (the
      heartbeat is seeded and the lock held BEFORE the session lands in
      `_active_by_user`, so a concurrently-firing sweep sees "not live" and must trust
      the Redis facade).
    * Reconcile-on-start passes `True`, CERTIFYING the facade is residue: that call site
      runs under the per-user `_start_lock_for` (serializing every start AND relaunch
      body for the user), has already established `user_id not in _active_by_user`, and
      the deploy contract is SINGLE-REPLICA (see the module docstring) — three facts that
      together leave nobody alive to be holding that lock. Without this, a process that
      died mid-build left a lock+heartbeat lingering up to the heartbeat TTL, and every
      start in that window 409ed on a build that no longer existed (walkthrough #10).
      A genuinely live build still 409s — it is caught by the `_active_by_user` check
      BEFORE this function is ever reached, never by the Redis facade.
    """
    if has_live_session:
        return False  # a session this process still owns is never reaped by heartbeat lapse
    reg = await read_registry(redis, user_uuid)
    if reg is None:
        await reap_lock(redis, user_uuid)  # clear any orphaned lock (no lockout)
        return False
    if (
        not certified_dead
        and await lock_is_held(redis, user_uuid)
        and await heartbeat_is_alive(redis, user_uuid)
    ):
        return False  # looks live + recent (bounded by the heartbeat TTL) — leave it
    if honor_stay and await stay_of_execution_is_current(redis, user_uuid):
        return False  # a relaunched preview inside its lease — the sweep spares it
    return await reap_user(redis, user_uuid, sandbox_client)


@dataclass(frozen=True)
class SweepResult:
    """One sweep's outcome. `failed` exists so a sweep that reconciled nothing because
    everything threw cannot be read as a sweep that found nothing to do."""

    reaped: int
    failed: int


async def sweep_all(
    redis: aioredis.Redis,
    sandbox_client: SandboxClient,
    *,
    live_users: set[uuid.UUID] | None = None,
) -> SweepResult:
    """SCAN-iterate the registry namespace (never `KEYS`) and reconcile each user;
    returns what it reaped AND what it could not. Idempotent + concurrency-safe, so it is safe
    to call on a timer (KTD-3). `live_users` are the SessionManager's live in-process sessions —
    never reaped.

    Passes `honor_stay=True`: a relaunched preview (#43) inside its bounded stay of
    execution is spared here, because a timer has no reason to kill a container the user
    is still looking at. Reconcile-on-start passes the opposite (see `reconcile_user`) —
    that build needs the slot, and sparing the preview there would orphan its container.
    The asymmetry is the design, not an oversight."""
    live = live_users if live_users is not None else set()
    reaped = 0
    failed = 0
    async for raw_key in redis.scan_iter(match=_REGISTRY_SCAN_MATCH):
        user_uuid = _user_from_registry_key(str(raw_key))
        if user_uuid is None or user_uuid in live:
            continue
        # ONE USER'S FAILURE IS ONE USER'S FAILURE. This loop used to be unguarded, so the
        # first exception ended the whole cycle and every user later in SCAN order went
        # unreconciled — silently, because SCAN order is not stable enough for anyone to
        # notice the same victims twice. The reachable case is an ARM throttle: `reap_user`
        # deletes through a blocking ARM poller, and a sweep with real work to do issues
        # enough calls to earn a 429. Cancellation still propagates — a shutdown must stop
        # the sweep, not be logged and swallowed per user.
        try:
            if await reconcile_user(
                redis, user_uuid, sandbox_client, has_live_session=False, honor_stay=True
            ):
                reaped += 1
        except Exception as exc:
            failed += 1
            _log.exception(
                "sweep skipped one user; continuing",
                user_id=str(user_uuid),
                error_type=type(exc).__name__,
            )
    # COUNT THE FAILURES, and hand them back. Isolating one user is right; reporting only
    # `reaped` is not — a sweep where EVERY user threw (an expired ACA credential, a
    # subscription-wide throttle) returns 0 and is indistinguishable from a sweep with nothing
    # to do. The operator endpoint would answer 200 `{"reaped": 0}` and write an audit row
    # saying the same, while containers accumulate and bill. The per-user log lines exist but
    # nothing aggregates them.
    return SweepResult(reaped=reaped, failed=failed)
