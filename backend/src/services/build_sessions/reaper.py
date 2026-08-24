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
* `reap_the_container_we_judged` is the janitor's, and it is keyed by CONTAINER NAME rather
  than by user — because the reclamation pass judges a container, and a user's record can name
  a different one (or none) by the time the delete lands. See its docstring for both ways that
  divergence bites.

WHAT THE SWEEP STRUCTURALLY CANNOT SEE. It enumerates from Redis
(`_scan_the_registry_namespace`), so it only ever reaches a container it already has a record of.
A sandbox whose registry entry is gone
— a flushed or replaced Redis, a container older than the registry, a teardown that failed after
`delete_registry` — is invisible here FOREVER and bills until a human notices; one did, for
twelve days. The Azure-side view that closes that gap is `inventory.take_sandbox_inventory`,
surfaced at `POST /v1/admin/reconcile-sandboxes`, and it REPORTS rather than deletes.

Reaper ordering for one stale user (C5): mark-ending → teardown → clear registry →
release lock (LAST). The reaper reclaims a possibly-drifted lock via the value-guarded
`reap_lock`, NEVER the holder release (the crashed session's in-process token is gone).

SINGLE-REPLICA CONSTRAINT (still binding, but no longer for the sweep). The live-session
shield (`has_live_session` / `sweep_all`'s `live_users`) reads an IN-PROCESS set. On a second
replica that set is blind to the first replica's builds, so replica B would reap a sandbox
replica A is actively building in — and it bit precisely in the quiet stretches the shield
was written for, because the only other liveness signal here
(`lock_is_held AND heartbeat_is_alive`) lapses at the heartbeat TTL between renews.

**U12 (R10) closes that for the SWEEP.** The turn engine now renews a wall-clock liveness
lease (C5 family 4) for the duration of every turn, and `reconcile_user` reads it before the
lock/heartbeat pair — so a build in flight is legible to a sweep running anywhere, and
`live_users` degrades from load-bearing to a fast in-process shortcut.

**What still binds is `certified_dead`.** That flag is a caller ASSERTION whose third premise
is "this is the only replica", and no second process can make it. So a worker may run
`sweep_all` and may never pass `certified_dead=True` (pinned by
`tests/services/build_sessions/test_reaper.py::test_no_worker_module_may_certify_death`).
Raising the replica count is likewise still a deploy-time question, not a runtime guard — a
process cannot detect its siblings, which is why the origin's Scope Boundaries reject a
startup assertion — and the per-replica rate-limit store is the other blocker (ADR-0029).

Corrected 2026-08-11 (ADR-0029): the sentence removed here read "There is no in-process
background sweeper by design" — which flatly contradicted this docstring's OWN opening
paragraph 26 lines above, added when the 1.6.5 sweeper shipped. Both statements lived in one
file for months and the false one is the one people quoted. ADR-0011 is now Accepted, and the
lease that was promised as R10 has since landed (U12) — see the constraint note above for what
it did and did not unblock.
"""

from __future__ import annotations

import uuid
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime

import redis.asyncio as aioredis
import structlog

from src.services.build_sessions.durable_copy import CopyVerdict, confirm_durable_copy
from src.services.build_sessions.integrity import container_state
from src.services.build_sessions.locks import (
    delete_registry,
    heartbeat_is_alive,
    liveness_lease_is_held,
    lock_is_held,
    mark_registry_ending,
    read_registry,
    reap_lock,
    release_liveness_lease,
    stay_of_execution_is_current,
)
from src.services.build_sessions.snapshot import RecoveryOutcome, write_recovery_copy
from src.services.redis import registry_scan_patterns
from src.services.redis.keys import REGISTRY_FIELD_APP_NAME, REGISTRY_FIELD_FQDN
from src.services.sandbox import SandboxClient, SandboxError, SandboxHandle
from src.services.sandbox.base import SANDBOX_NAME_PREFIX

_log = structlog.get_logger()

#: `app_name_for` mints `sbx-` + `app_id.hex[:28]`. Both halves are pinned here because the guard
#: below is a fail-closed check on a name we are about to DELETE, and a guard that accepts more
#: than the minter produces is a guard with a gap in it.
_NAME_SLUG_LENGTH = 28
_HEX_LOWER = frozenset("0123456789abcdef")


async def _scan_the_registry_namespace(redis: aioredis.Redis) -> AsyncIterator[str]:
    """SCAN-iterate every registry pattern the namespace currently spans (never `KEYS`).

    Plural because of the R22 dual-read window: during it the namespace is the environment-scoped
    prefix plus the legacy one (C5). This is only what makes the legacy keys REACHABLE — the read
    that actually rescues them is the dual-read inside `locks.read_registry`, because this loop
    hands on a user id, not a record.
    """
    for pattern in registry_scan_patterns():
        async for raw_key in redis.scan_iter(match=pattern):
            yield str(raw_key)


def _user_from_registry_key(key: str) -> uuid.UUID | None:
    try:
        return uuid.UUID(key.rsplit(":", 1)[-1])
    except ValueError:
        return None


def _handle_named(app_name: str, *, fqdn: str = "") -> SandboxHandle:
    """The minimal handle a teardown needs — ACA delete is keyed by `app_name` alone, and no
    in-process token survives a crash. The `fqdn` is carried when we happen to know it and left
    empty when we do not; nothing on the teardown path reads it."""
    return SandboxHandle(
        fqdn=fqdn,
        token="",
        app_name=app_name,
        preview_url=f"https://{fqdn}/",
        ready=False,
    )


def _minimal_handle(reg: dict[str, str]) -> SandboxHandle:
    """The same handle, reconstructed from a registry record — the shape `reap_user` tears down."""
    return _handle_named(
        reg.get(REGISTRY_FIELD_APP_NAME, ""), fqdn=reg.get(REGISTRY_FIELD_FQDN, "")
    )


def is_a_sandbox_name(app_name: str) -> bool:
    """Could this string be a container THIS platform minted? (`manager.app_name_for`.)

    THE LAST CHECK BEFORE A STRING BECOMES AN ARM DELETE, and until now there wasn't one. The reap
    path rebuilds its teardown target from the registry record — the least trustworthy input in
    the system, by this ADR's own argument: the store it distrusts, the one family with no TTL,
    written by several code paths, surviving crashes. Whatever that record said got deleted.
    `reg.get(APP_NAME, "")` even turns a *missing* field into a delete request for `""`.

    So the shape is checked rather than assumed: `sbx-` + exactly 28 lowercase hex characters,
    which is what `app_name_for` mints and nothing else is. A `pub-` name is a citizen's live
    published application; the managed environment and unrelated workloads share the resource
    group. None of them are ours to delete on the say-so of a corrupted hash.

    Deliberately NOT a substring or prefix test alone: `startswith("sbx-")` would pass `sbx-` on
    its own, and the whole point is that the string has to look like something we could have
    minted, not merely something with our prefix glued on."""
    if not app_name.startswith(SANDBOX_NAME_PREFIX):
        return False
    slug = app_name[len(SANDBOX_NAME_PREFIX) :]
    return len(slug) == _NAME_SLUG_LENGTH and all(c in _HEX_LOWER for c in slug)


@dataclass(frozen=True)
class _Reachable:
    """The container we are judging: attached, and whatever it said about itself.

    THE HANDLE IS CARRIED RATHER THAN RE-DERIVED, and that is the whole reason this is a record
    and not a bare sha. U5 writes a recovery copy out of the very container whose `HEAD` the gate
    just compared, and attaching a second time to do it would re-read the registry — the one input
    on this path that changes underneath us. A builder starting a fresh sandbox between the two
    reads would have the copy bundle a DIFFERENT container's tree into this app's recovery slot:
    the exact loss the gate exists to prevent, performed by the code added to prevent it.

    `head` is `None` when the attach SUCCEEDED but the state probe did not answer. That is not the
    same as not reaching the container at all, and the two arms want it separated: the gate reads
    a `None` head as "fall back to the bundle", while the copy can still be taken from a container
    that merely failed to count its commits.

    `uncommitted` RIDES ALONGSIDE `head` BECAUSE A HEAD ALONE STOPPED BEING AN ANSWER (U19). The
    agent no longer commits as it works, so a turn that wrote files leaves `HEAD` where the last
    turn's recovery copy was stamped — and a gate handed only the head reads that as preserved and
    destroys the tree. This field is the difference between "nothing changed since the copy" and
    "nothing was COMMITTED since the copy". `None` when the probe did not answer, which
    `confirm_durable_copy` refuses rather than guesses."""

    handle: SandboxHandle
    head: str | None
    uncommitted: bool | None


async def _reach_the_container(
    sandbox_client: SandboxClient, user_uuid: uuid.UUID
) -> _Reachable | None:
    """Attach to this user's container and ask it for its `HEAD`. `None` if it cannot be asked.

    THE DURABLE-COPY GATE IS ONLY A GATE IF THIS RUNS. `confirm_durable_copy` reads a `None` head
    as "the container could not be reached, so a present and parseable recovery copy stands in" —
    a deliberate fallback, because an orphan that is already dead can never answer and a gate
    nothing can satisfy collects nothing. Passing `None` UNCONDITIONALLY, which this call site
    used to do, turned that fallback into the only reachable branch: the `stamped == container_
    head` comparison and the whole `STALE` verdict became dead code, so a container holding a
    turn's worth of work newer than its last autosave read as "provably preserved" and died.

    ATTACH, THEN ASK THE SAME LADDER THE SAVE INDICATOR ASKS. `attach_existing` is the one path
    that recovers the supervisor bearer (its durable home is the container's own ACA env, not this
    process's memory), and `container_state` is exactly what `project_save_state` answers with. A
    reaper must not hold a second opinion about what HEAD means.

    A FAILED ATTACH IS `None`, and that is honest rather than permissive: the branch it feeds
    still demands a parseable recovery bundle before anything is destroyed, and U5 refuses to take
    a copy it has nowhere to take one from."""
    try:
        handle = await sandbox_client.attach_existing(str(user_uuid))
    except SandboxError:
        # Gone, ending, unreachable, or its bearer unrecoverable — every one of them means "this
        # container cannot be asked anything", which is precisely the case the fallback is for.
        return None
    state = await container_state(sandbox_client, handle)
    return _Reachable(
        handle=handle,
        head=state.head if state is not None else None,
        # BOTH FIELDS COME FROM THE SAME PROBE, so a state that did not answer leaves both
        # unknown rather than leaving `uncommitted` looking like a confident "clean".
        uncommitted=state.uncommitted if state is not None else None,
    )


async def _take_the_copy_we_promised(
    sandbox_client: SandboxClient,
    *,
    app_id: uuid.UUID,
    verdict: CopyVerdict,
    reached: _Reachable | None,
    expected_name: str,
) -> bool:
    """ADR-0029 §7's second half. True when this container may now be reclaimed.

    §7 promises that if the newest durable copy predates the newest change, a copy is TAKEN before
    the container is reclaimed. Until U5 neither call site took one: both read the verdict, logged
    "not provably preserved" and spared — so a container whose autosave had silently failed was
    spared on that pass, and on every pass after it, forever. It kept a supervisor, a dev server
    and an ACA replica alive and billing, and the only trace was a log line that repeated every
    fifteen minutes and looked, to anyone reading it, like the guard working correctly. ASM30
    found the platform in exactly that state.

    THE COPY GOES THROUGH U3'S GUARDED WRITE, never a raw `put`: `write_recovery_copy` promotes a
    tree only when it is a descendant of the copy already on record, and diverts anything else to a
    per-occurrence key.

    THAT ALONE WAS NOT ENOUGH, and an adversarial review proved it. The guard cannot run when
    there is nothing comparable on record — an empty slot, or a bundle written before the head
    stamp existed — and a reverted container has exactly that shape on exactly the population this
    unit exists for. So the first version of this fix became the thing it was written to prevent:
    the reverted tree was written in unguarded, this function read the WRITTEN as proof, and the
    container holding the only real copy was deleted in the same call. A write with no comparison
    behind it is now kept but does NOT authorise the destroy (`UNGUARDED`, below).

    EVERY ARM THAT DOES NOT ESTABLISH A COPY SPARES, and every arm writes a record. The sparing is
    the pre-existing behaviour and is not up for negotiation on a destroy path; the record is what
    stops a permanently-spared container from being silent, which is the half of ASM30 that made
    the leak invisible rather than merely expensive."""
    # IMPORTED HERE, NOT AT MODULE SCOPE, and for ONE accurate reason rather than two. There is
    # no import cycle — `src.workers.reclamation` imports the reaper function-scoped, so nothing
    # closes a loop at module-import time, and an earlier version of this comment claimed
    # otherwise. What is true is the weight: `pass_history` reaches `src.db.base`, which BUILDS
    # THE ORM ENGINE at import, so a module-level bind puts that (and `src.broker`, by way of
    # `src.workers.reclamation`) behind every import of the reaper — including the cold one
    # `test_the_reaper_imports_without_the_fastapi_app` performs.
    from src.services.build_sessions.pass_history import (
        CopyAttempt,
        record_durable_copy_attempt,
    )

    if verdict.may_destroy:
        # SPLIT ON WHY, not just on the verdict, because `may_destroy` is True for two different
        # facts. One is "the sha comparison ran and the copy matches" — genuinely nothing to take.
        # The other is `confirm_durable_copy`'s deliberate fallback: the container could not be
        # read, so a present, parseable bundle stands in. In that second case NOTHING about
        # currency was established, and recording it as "the durable copy was already current"
        # writes the one row an operator would use to find "we destroyed containers we could not
        # verify" and makes it say the opposite.
        compared = reached is not None and reached.head is not None
        await record_durable_copy_attempt(
            CopyAttempt.NOTHING_TO_COPY if compared else CopyAttempt.UNVERIFIED_FALLBACK
        )
        return True
    if reached is None or reached.handle.app_name != expected_name:
        # NOTHING TO COPY FROM. Either the container would not attach, or — and this is the one
        # worth spelling out — the registry has moved on and the handle we hold names a DIFFERENT
        # container. `attach_existing` builds its handle from the record, so a builder who started
        # a fresh sandbox between the record read and the attach hands us their live container.
        # Bundling that tree into this app's recovery slot would overwrite one app's only copy
        # with another app's work; U3's guard would probably divert it, but "probably caught one
        # layer down" is not a reason to hand it the wrong tree.
        _log.warning(
            "no copy taken: nothing to copy from, so this container is spared again",
            app_id=str(app_id),
            expected=expected_name,
            reached=reached.handle.app_name if reached else None,
        )
        await record_durable_copy_attempt(CopyAttempt.UNREACHABLE)
        return False
    try:
        written = await write_recovery_copy(
            sandbox_client, reached.handle, app_id, taken_at=datetime.now(UTC)
        )
    except Exception:
        # BROAD ON PURPOSE, and it is the fail-CLOSED direction. Every way this can fail — the
        # exec, the bundle, the base64 read-back, the store, bytes that will not parse as a
        # bundle — means the same single thing here: the copy did not land. The arm it takes is
        # the sparing one, which can never destroy anything, so narrowing would buy no safety and
        # would cost the record: an unforeseen exception would escape into `sweep_all`'s per-user
        # handler, end this user's reap, and leave behind exactly the silence this unit removes.
        # `CancelledError` is a `BaseException` and still propagates, so a shutdown still stops
        # the sweep rather than being logged and swallowed.
        _log.exception(
            "no copy taken: the recovery write raised, so this container is spared again",
            app_id=str(app_id),
            app_name=expected_name,
        )
        await record_durable_copy_attempt(CopyAttempt.FAILED)
        return False
    if written.outcome is RecoveryOutcome.DIVERTED:
        # U3 refused to promote this tree and preserved it under `divert_key` instead. It has
        # already raised the pinned "recovery write did not land" alarm with the two shas that
        # explain why, so nothing is re-alarmed here — the container is simply spared, which is
        # the only answer available when the tree in hand cannot be shown to contain the work.
        await record_durable_copy_attempt(CopyAttempt.REFUSED)
        return False
    if written.recorded_head is None:
        # THE COPY LANDED, BUT NO GUARD RAN. There was nothing on record to compare it against, so
        # `write_recovery_copy` took its first-write arm — which is right at a turn boundary,
        # where the container is alive and the tree is the citizen's, and wrong here.
        #
        # A REVERTED CONTAINER HAS EXACTLY THIS SHAPE. An app whose every autosave failed — the
        # ASM30 population this unit exists for — has an empty recovery slot, so a reverted
        # container's empty tree becomes the first copy on record, `recoverable_work` ranks it
        # newest by `last_modified`, and the citizen's next build is restored from the template
        # over their saved app. Then this function would return True and delete the container
        # holding the only real tree. An adversarial review reproduced exactly that.
        #
        # So: keep the copy (it is strictly better than nothing), and spare. A later pass with a
        # comparable copy on record can destroy it properly.
        _log.warning(
            "no guarded copy: this was the first copy on record, so the container is spared",
            app_id=str(app_id),
            app_name=expected_name,
            bundled_head=written.bundled_head,
        )
        await record_durable_copy_attempt(CopyAttempt.UNGUARDED)
        return False
    # WRITTEN, or SKIPPED because the commit step found the slot already holding this exact tree.
    # Both mean the recovery slot now contains what the container contains, which is the fact the
    # gate wanted and could not establish from the outside — and both compared against a real
    # recorded head, which is what makes them evidence rather than an assumption.
    await record_durable_copy_attempt(
        CopyAttempt.COPIED
        if written.outcome is RecoveryOutcome.WRITTEN
        else CopyAttempt.NOTHING_TO_COPY
    )
    return True


async def reap_user(
    redis: aioredis.Redis,
    user_uuid: uuid.UUID,
    sandbox_client: SandboxClient,
    *,
    strict: bool = False,
    app_id: uuid.UUID | None = None,
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
    strict arm changes who is told, never what is left behind.

    THE DURABLE-COPY GATE (U14, R9/R11). `app_id` opts this call into it. Until U14 this function
    called `teardown` with no such check at all — the F1 path, which does almost all of the
    deleting, was the one path with none of the protection — so the gate had to be added HERE and
    not only on the orphan path. It is optional rather than required for one reason: the two
    in-repo callers that reap a user's OWN stale state (reconcile-on-start, and the sweep) run
    where the builder is about to get a fresh container anyway, and a `None` keeps their behaviour
    byte-identical while the scheduled janitor — the process with no human watching it — passes
    the id and is gated. A gate nobody can satisfy protects nothing; a gate the janitor cannot
    skip protects the thing that matters."""
    reg = await read_registry(redis, user_uuid)
    if reg is None:
        # No sandbox registered — just clear any orphaned lock so a crashed-tab user is
        # never locked out (the reconcile side of KTD-3).
        await reap_lock(redis, user_uuid)
        return False
    registered_name = reg.get(REGISTRY_FIELD_APP_NAME, "")
    if not is_a_sandbox_name(registered_name):
        # FAIL CLOSED ON A NAME WE CANNOT VOUCH FOR. Everything below hands this string to an ARM
        # delete, and the record it came from is the least trustworthy input here. Refusing but
        # KEEPING the record would re-refuse every five minutes forever, so the record goes and
        # the container — which is somebody else's if it is anything — is left alone.
        _log.error(
            "refusing to reap: the registry names something that is not a sandbox name",
            user_id=str(user_uuid),
            app_name=registered_name,
        )
        await delete_registry(redis, user_uuid)
        await release_liveness_lease(redis, user_uuid)
        await reap_lock(redis, user_uuid)
        return False
    if app_id is not None:
        # THE REAL HEAD, not a hardcoded `None`. See `_reach_the_container`: a constant `None`
        # here made the gate's fallback its only branch, and the comparison it exists to perform
        # unreachable. A container that will not answer still falls back — it just has to
        # actually not answer first.
        reached = await _reach_the_container(sandbox_client, user_uuid)
        verdict = await confirm_durable_copy(
            app_id,
            container_head=reached.head if reached else None,
            container_dirty=reached.uncommitted if reached else None,
        )
        # AND THEN TAKE THE COPY (U5, ADR-0029 §7), rather than sparing on the strength of the
        # verdict alone. This branch used to end here with a log line, so a container whose
        # autosave had failed was spared on this pass and on every pass after it.
        if not await _take_the_copy_we_promised(
            sandbox_client,
            app_id=app_id,
            verdict=verdict,
            reached=reached,
            expected_name=registered_name,
        ):
            # SPARE AND REPORT — never destroy. The container keeps its lock and registry, so a
            # later pass retries once the store is readable again or a copy has been taken.
            _log.warning(
                "reap refused: this container's work is not provably preserved",
                user_id=str(user_uuid),
                app_id=str(app_id),
                copy_state=str(verdict.state),
                reason=verdict.reason,
            )
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
    # ...and the R10 lease goes WITH the record it belonged to (C5). Only here, after a
    # teardown that actually succeeded: the failure arm above keeps lock + registry so a
    # later sweep retries, and dropping the lease there would strip the protection off a
    # container that is still standing and may still be building.
    await release_liveness_lease(redis, user_uuid)
    await reap_lock(redis, user_uuid)  # step 3: release the (possibly drifted) lock — LAST
    return True


async def reap_the_container_we_judged(
    redis: aioredis.Redis,
    sandbox_client: SandboxClient,
    *,
    app_name: str,
    user_uuid: uuid.UUID,
    app_id: uuid.UUID,
) -> bool:
    """The ordered reap for ONE container, keyed by NAME. True only when it actually deleted it.

    WHY THIS IS NOT `reap_user`. The janitor judges a CONTAINER: it enumerated Azure, classified
    `sbx-abc`, staged `sbx-abc` and came back a pass later for `sbx-abc`. `reap_user` reaps a
    USER — it reads that user's registry and destroys whatever container the record happens to
    name right now. Those are the same container right up until they are not, and both ways they
    diverge are this feature's own failure modes rather than exotica:

    * the builder started a fresh sandbox between enumeration and delete, so the record names the
      NEW container. Reaping by user would delete the live one and leave the judged orphan
      standing — the exact inversion of the job, performed by the thing that exists to prevent it;
    * there is no record at all, which IS the unregistered-orphan population this whole system was
      built to collect. Reaping by user returns False having deleted nothing, while the pass
      counted the container destroyed and an operator read a report that was simply untrue.

    So the ARM delete is keyed by the name we judged, and the user's Redis state is touched ONLY
    when the registry still names that container. A record naming something else describes a
    container that is alive and is none of this pass's business; a record that is absent describes
    nothing at all.

    THE FOUR-STEP ORDERING SURVIVES for the case where the record IS ours: `mark_registry_ending`
    (guards a concurrent attach) → `teardown` → `delete_registry` + lease → `reap_lock` LAST."""
    reg = await read_registry(redis, user_uuid)
    ours = reg is not None and reg.get(REGISTRY_FIELD_APP_NAME) == app_name
    # The container is only reachable THROUGH the registry — `attach_existing` builds its handle
    # from that record — so a container the store no longer claims can be judged on its recovery
    # copy alone. That is the gate's documented fallback, and it still demands a parseable bundle.
    # It is also why U5 cannot take a copy for an unregistered orphan: there is no address to
    # bundle from, and the address we DO have belongs to somebody else's container.
    reached = await _reach_the_container(sandbox_client, user_uuid) if ours else None
    verdict = await confirm_durable_copy(
        app_id,
        container_head=reached.head if reached else None,
        container_dirty=reached.uncommitted if reached else None,
    )
    # AND THEN TAKE THE COPY (U5, ADR-0029 §7). The janitor is the caller with nobody watching it,
    # so it is the one that was quietly sparing the same containers pass after pass.
    if not await _take_the_copy_we_promised(
        sandbox_client,
        app_id=app_id,
        verdict=verdict,
        reached=reached,
        expected_name=app_name,
    ):
        # SPARE AND REPORT — never destroy. Nothing is cleared, so the next pass retries once a
        # copy exists or the store is readable again.
        _log.warning(
            "reclamation refused: this container's work is not provably preserved",
            app_name=app_name,
            user_id=str(user_uuid),
            app_id=str(app_id),
            copy_state=str(verdict.state),
            reason=verdict.reason,
        )
        return False
    if ours:
        await mark_registry_ending(redis, user_uuid)  # step 1: guard a concurrent attach
    try:
        await sandbox_client.teardown(
            _handle_named(app_name, fqdn=(reg or {}).get(REGISTRY_FIELD_FQDN, ""))
        )
    except SandboxError:
        # KEEP whatever state there is so a later pass retries; clearing it now would orphan a
        # container that is still standing. Not silent (logged), and NOT counted as destroyed.
        _log.exception(
            "reclamation teardown failed; leaving state for a later pass", app_name=app_name
        )
        return False
    if ours:
        await delete_registry(redis, user_uuid)
        await release_liveness_lease(redis, user_uuid)
        await reap_lock(redis, user_uuid)  # LAST
    return True


async def reconcile_user(
    redis: aioredis.Redis,
    user_uuid: uuid.UUID,
    sandbox_client: SandboxClient,
    *,
    has_live_session: bool,
    honor_stay: bool = False,
    certified_dead: bool = False,
    app_ids_by_name: Mapping[str, uuid.UUID] | None = None,
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

    The R10 LIVENESS LEASE is the same asymmetry again, one key over, and for the same
    reason — so do not "simplify" it into one behaviour either:

    * The sweep honours a held lease unconditionally. A timer has no business destroying a
      container an agent is making tool calls inside, and the lease is the ONLY input here
      that says so from outside the process running the build.
    * Reconcile-on-start (`certified_dead=True`) DELETES it and reaps through. A turn killed
      mid-build leaves a live lease behind; honouring it there would 409 the same builder's
      next start until the TTL lapsed — reproducing the crashed-tab lockout this function
      exists to prevent, via the mechanism added to protect them. Both, not either: the
      delete clears a lease left where there is no longer a registry to reap, and the
      read-guard survives a delete that a racing renewal immediately undoes.

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
      together leave nobody alive to be holding that lock. THE THIRD IS WHY NO WORKER MAY
      EVER PASS THIS: a background process on another container establishes none of the
      three, and a second replica removes the premise outright. The default is `False`
      precisely so the flag has to be spelled out to be wrong, and
      `test_no_worker_module_may_certify_death` pins that no module under `src/workers/`
      spells it. Without the flag at all, a process that
      died mid-build left a lock+heartbeat lingering up to the heartbeat TTL, and every
      start in that window 409ed on a build that no longer existed (walkthrough #10).
      A genuinely live build still 409s — it is caught by the `_active_by_user` check
      BEFORE this function is ever reached, never by the Redis facade.

    `app_ids_by_name` is the THIRD caller asymmetry, and it is what puts the scheduled sweep
    under the U14 durable-copy gate. `reap_user`'s gate is opt-in via `app_id`, so a caller that
    resolves no id reaps ungated — correct for reconcile-on-start, where a builder is standing
    right there about to be handed a fresh container, and wrong for a timer in another process
    with nobody watching. The worker passes the map (app name → owning app id, forward-matched
    from the app table); the request handlers pass nothing and stay byte-identical.
    """
    if has_live_session:
        return False  # a session this process still owns is never reaped by heartbeat lapse
    if certified_dead:
        # DELETE THE LEASE, do not merely decline to read it — and do it here, above every
        # other arm, so a stray lease is cleared even when there is no registry left to
        # reap. Leaving it would let the background sweep go on sparing a container this
        # call has already certified dead and is about to tear down, and the next build
        # registers a DIFFERENT container under the same user.
        await release_liveness_lease(redis, user_uuid)
    reg = await read_registry(redis, user_uuid)
    if reg is None:
        await reap_lock(redis, user_uuid)  # clear any orphaned lock (no lockout)
        return False
    if not certified_dead and await liveness_lease_is_held(redis, user_uuid):
        # R10: the one liveness input readable from a process that is not running the build.
        # Checked BEFORE the lock/heartbeat pair below because it outranks it in both
        # directions — a live build has lost that pair 90 seconds in, and a dead one leaves
        # it standing for a TTL. A held lease means an agent is making tool calls inside
        # that container right now.
        return False
    if (
        not certified_dead
        and await lock_is_held(redis, user_uuid)
        and await heartbeat_is_alive(redis, user_uuid)
    ):
        return False  # looks live + recent (bounded by the heartbeat TTL) — leave it
    if honor_stay and await stay_of_execution_is_current(redis, user_uuid):
        return False  # a relaunched preview inside its lease — the sweep spares it
    return await reap_user(
        redis, user_uuid, sandbox_client, app_id=_owning_app_id(reg, app_ids_by_name, user_uuid)
    )


def _owning_app_id(
    reg: dict[str, str],
    app_ids_by_name: Mapping[str, uuid.UUID] | None,
    user_uuid: uuid.UUID,
) -> uuid.UUID | None:
    """The app id behind this registry record, when the caller supplied the map to resolve it.

    THE UNMATCHED CASE IS A DELIBERATE, NARROW HOLE and is logged rather than hidden. A registry
    record naming a container with no app row describes an app that no longer exists, so there is
    no recovery slot to compare against: the gate would return UNCONFIRMED forever and the
    container would be spared until it was deleted by hand, which is the leak this system exists
    to close. Reaping it is the same behaviour every caller had before the gate existed."""
    if app_ids_by_name is None:
        return None
    app_id = app_ids_by_name.get(reg.get(REGISTRY_FIELD_APP_NAME, ""))
    if app_id is None:
        _log.info(
            "reaping a registered container with no app row; nothing to preserve, gate skipped",
            user_id=str(user_uuid),
            app_name=reg.get(REGISTRY_FIELD_APP_NAME, ""),
        )
    return app_id


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
    app_ids_by_name: Mapping[str, uuid.UUID] | None = None,
) -> SweepResult:
    """SCAN-iterate the registry namespace (never `KEYS`) and reconcile each user;
    returns what it reaped AND what it could not. Idempotent + concurrency-safe, so it is safe
    to call on a timer (KTD-3). `live_users` are the SessionManager's live in-process sessions —
    never reaped.

    THIS IS THE SCHEDULED READER OF THE R10 LEASE, and that matters as much as writing it:
    a lease nothing consults is a container nothing spares. It also keeps the default
    `certified_dead=False` — a sweep holds none of the three facts that certification rests
    on, and the third of them (single replica) is exactly what a worker removes.

    Passes `honor_stay=True`: a relaunched preview (#43) inside its bounded stay of
    execution is spared here, because a timer has no reason to kill a container the user
    is still looking at. Reconcile-on-start passes the opposite (see `reconcile_user`) —
    that build needs the slot, and sparing the preview there would orphan its container.
    The asymmetry is the design, not an oversight.

    `app_ids_by_name` is FORWARDED, not resolved here: this loop hands on a user id and never
    reads a record, so the name→id match has to happen where the record is (`reconcile_user`).
    The scheduled worker supplies it and is therefore gated by the durable-copy precondition;
    the operator endpoint supplies nothing, keeping the hand-triggered sweep exactly as it was."""
    live = live_users if live_users is not None else set()
    reaped = 0
    failed = 0
    # EVERY pattern the namespace currently spans, which during the R22 dual-read window is the
    # environment-scoped one AND the legacy one (C5). Two literals, deliberately: a single
    # `bial:*:sandbox:registry:*` would reach into OTHER ENVIRONMENTS, which is the whole hazard
    # R22 closes. A user with a key under both prefixes is visited twice; `reconcile_user` is
    # idempotent, and `seen` keeps the counts honest anyway.
    seen: set[uuid.UUID] = set()
    async for raw_key in _scan_the_registry_namespace(redis):
        user_uuid = _user_from_registry_key(str(raw_key))
        if user_uuid is None or user_uuid in live or user_uuid in seen:
            continue
        seen.add(user_uuid)
        # ONE USER'S FAILURE IS ONE USER'S FAILURE. This loop used to be unguarded, so the
        # first exception ended the whole cycle and every user later in SCAN order went
        # unreconciled — silently, because SCAN order is not stable enough for anyone to
        # notice the same victims twice. The reachable case is an ARM throttle: `reap_user`
        # deletes through a blocking ARM poller, and a sweep with real work to do issues
        # enough calls to earn a 429. Cancellation still propagates — a shutdown must stop
        # the sweep, not be logged and swallowed per user.
        try:
            if await reconcile_user(
                redis,
                user_uuid,
                sandbox_client,
                has_live_session=False,
                honor_stay=True,
                app_ids_by_name=app_ids_by_name,
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
