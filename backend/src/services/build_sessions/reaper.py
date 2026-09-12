"""The reaper: reconcile-on-start + the full sweep.

Two entry points, plus `src/workers/sandbox_reap.py`, the scheduled caller of
`sweep_all`:

* `reconcile_user` reaps the caller's OWN stale lock/registry/heartbeat at the top of
  every `start` — closes the "crashed tab -> can never start again" lockout.
* `sweep_all` reconciles EVERY registered user, idempotent + concurrency-safe; runs on
  a schedule, or by hand at `POST /v1/build-sessions/internal/reap`.
* `reap_the_container_we_judged` is the janitor's, keyed by CONTAINER NAME rather than
  user, because a user's record can name a different container by the time the delete
  lands. See its own docstring.

A COMPLETED build is not torn down: the registry stays with a bounded stay-of-execution
lease and the lock releases. `sweep_all` honours an unexpired lease; `reconcile_user`
reaps through one — the incoming build needs the slot. The sweep only reaches containers
with a Redis registry record; one whose record is gone is invisible here forever
(`inventory.take_sandbox_inventory`, `POST /v1/admin/apps/reconcile-sandboxes`, reports
rather than deletes).

WHY THIS EXISTS. The live-session shield (`has_live_session`) reads an IN-PROCESS set,
blind on a second replica — it bit in the quiet stretches between heartbeat renews. A
wall-clock LIVENESS LEASE, renewed every turn, closed that for the sweep;
`certified_dead` still asserts single-replica, and no worker may ever pass it
(`test_no_worker_module_may_certify_death`)."""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final, Literal

import redis.asyncio as aioredis
import structlog

from src.api.v1.build_sessions.schemas import SHARED_PREVIEW_ABSOLUTE_CEILING_SECONDS
from src.services.build_sessions.alarms import (
    APP_FIRST_SERVED_EVENT,
    APP_SERVING_LOST_EVENT,
    SERVING_PROOF_NEVER_ARRIVED,
    SERVING_PROOF_STAMP_REFUSED,
)
from src.services.build_sessions.durable_copy import CopyVerdict, confirm_durable_copy
from src.services.build_sessions.integrity import container_state
from src.services.build_sessions.locks import (
    DeadlineWriter,
    an_instant_on_the_hash,
    clear_serving,
    delete_registry,
    elapsed_ms,
    grant_stay_of_execution,
    heartbeat_is_alive,
    liveness_lease_is_held,
    lock_is_held,
    mark_registry_ending,
    mark_serving,
    read_registry,
    read_starting_marker,
    reap_lock,
    release_liveness_lease,
    stamp_is_proven,
    stay_of_execution_is_current,
)
from src.services.build_sessions.snapshot import RecoveryOutcome, write_recovery_copy
from src.services.redis import REGISTRY_STATE_READY, registry_key, registry_scan_patterns
from src.services.redis.keys import (
    REGISTRY_FIELD_APP_NAME,
    REGISTRY_FIELD_CREATED_AT,
    REGISTRY_FIELD_FQDN,
    REGISTRY_FIELD_SERVING_SINCE,
    REGISTRY_FIELD_SHARED_SERVED_COUNT,
    REGISTRY_FIELD_STATE,
)
from src.services.sandbox import DevStatus, SandboxClient, SandboxError, SandboxHandle
from src.services.sandbox.base import SANDBOX_NAME_PREFIX, SHARED_SANDBOX_NAME_PREFIX

_log = structlog.get_logger()

#: `app_name_for` mints `sbx-` + `app_id.hex[:28]`. Both halves are pinned here because the guard
#: below is a fail-closed check on a name we are about to DELETE, and a guard that accepts more
#: than the minter produces is a guard with a gap in it.
_NAME_SLUG_LENGTH = 28
_HEX_LOWER = frozenset("0123456789abcdef")

#: HOW LONG A LIVE OBSERVER IS GIVEN before this sweep concludes that nobody is coming. Mirrors
#: `manager._COLD_READY_BUDGET_SECONDS` (120s) — the budget a cold restore's own readiness wait
#: runs on — because it is the same number for the same reason: below it, someone is plausibly
#: still watching this container and their stamp is the honest one (it carries the instant they
#: saw, not the instant a sweep happened to look); above it, every in-process observer that could
#: have taken it is gone.
#:
#: MIRRORED RATHER THAN IMPORTED, and that is a cycle rather than taste: `manager` imports THIS
#: module (`manager.py:101`), so a module-scope import would not resolve, and a function-scoped
#: one would drag `src.db.base` — which builds the ORM engine at import — behind the cold import
#: `test_the_reaper_imports_without_the_fastapi_app` performs.
_NOBODY_IS_COMING_AFTER_SECONDS: float = 120.0

#: THE PROBE'S WHOLE BUDGET, and it is a ceiling on the SWEEP rather than on the container.
#: `sweep_all` reconciles users one after another, and `attach_existing` against a container that
#: will not answer costs `_PROBE_MAX_ATTEMPTS` supervisor GETs backing off to `_PROBE_MAX_SECONDS`
#: plus up to two ARM round trips (`sandbox/client.py:840-901`) — roughly nine seconds each. A
#: fleet of dead containers would turn an unbounded observation into a sweep that never finishes
#: its list, and the REAP is what would starve. An observation that runs out of time is simply not
#: taken; the next pass, five minutes out, takes it.
_PROBE_BUDGET_SECONDS: float = 10.0

#: WHICH observer this module is, in `APP_FIRST_SERVED_EVENT`'s `observer` vocabulary — a named
#: constant for the same reason the turn engine and the session manager each have one: a typo in
#: one of the three call sites below would mint an observer that never existed, and the log rule
#: keyed on the name would go on matching nothing while looking perfectly healthy.
_OBSERVER_RECONCILER: Final = "reconciler"


async def _scan_the_registry_namespace(redis: aioredis.Redis) -> AsyncIterator[str]:
    """SCAN-iterate every registry pattern the namespace currently spans (never `KEYS`).

    Plural while the namespace spans both the current prefix and the legacy one. This only makes
    the legacy keys REACHABLE — the read that rescues them is the dual-read inside
    `locks.read_registry`, because this loop hands on a user id, not a record.
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
    """The minimal teardown handle — ACA delete is keyed by `app_name` alone; `fqdn` is carried
    when known, left empty otherwise, and read by nothing on the teardown path.

    `preview_url` is EMPTY rather than composed from `fqdn`: composing it used to produce
    `https:///` whenever `fqdn` was absent, and teardown never shows this to a browser, so no
    value is the honest one.
    """
    return SandboxHandle(
        fqdn=fqdn,
        token="",
        app_name=app_name,
        preview_url="",
        ready=False,
    )


def _minimal_handle(reg: dict[str, str]) -> SandboxHandle:
    """The same handle, reconstructed from a registry record — the shape `reap_user` tears down."""
    return _handle_named(
        reg.get(REGISTRY_FIELD_APP_NAME, ""), fqdn=reg.get(REGISTRY_FIELD_FQDN, "")
    )


def is_a_sandbox_name(app_name: str) -> bool:
    """Could this string be a container THIS platform minted? (`manager.app_name_for`.)

    THE LAST CHECK BEFORE AN ARM DELETE. The reap path rebuilds its teardown target from a
    registry record that can be corrupted or missing — `reg.get(APP_NAME, "")` turns a missing
    field into a delete request for `""`. So the shape is checked, not assumed: `sbx-` + exactly
    28 lowercase hex. `pub-` names are citizens' live published apps and the resource group holds
    unrelated workloads; none are ours to delete on a corrupted hash's say-so. Not a prefix test
    alone — `startswith("sbx-")` would pass `sbx-` by itself."""
    if not app_name.startswith(SANDBOX_NAME_PREFIX):
        return False
    slug = app_name[len(SANDBOX_NAME_PREFIX) :]
    return len(slug) == _NAME_SLUG_LENGTH and all(c in _HEX_LOWER for c in slug)


def is_a_shared_sandbox_name(app_name: str) -> bool:
    """The `shr-` sibling of `is_a_sandbox_name` (#198) — same fail-closed shape check, same
    reason: a name this platform will hand to an ARM delete has to be provably one it minted
    (`manager.shr_name_for`), not assumed from a prefix alone.

    Wired into `reap_user`'s own gate alongside its `sbx-` sibling: the one per-user slot the
    registry describes can hold EITHER lineage — a builder's own sandbox or a colleague's
    shared-runtime view restored into it — and `reap_user` tears down whichever is there."""
    if not app_name.startswith(SHARED_SANDBOX_NAME_PREFIX):
        return False
    slug = app_name[len(SHARED_SANDBOX_NAME_PREFIX) :]
    return len(slug) == _NAME_SLUG_LENGTH and all(c in _HEX_LOWER for c in slug)


@dataclass(frozen=True)
class _Reachable:
    """The container we are judging: attached, and whatever it said about itself.

    The HANDLE is carried, never re-derived: the registry is the one input that can change under
    us, and a second read could bundle a builder's freshly started sandbox — the WRONG tree —
    into this app's recovery slot. `head` and `uncommitted` are both `None` when the probe did
    not answer, and are separate fields because a head alone conflates "nothing changed" with
    "nothing was COMMITTED", so a gate reading only the head would destroy uncommitted work as
    preserved. `confirm_durable_copy` refuses `None` rather than guessing."""

    handle: SandboxHandle
    head: str | None
    uncommitted: bool | None


async def _reach_the_container(
    sandbox_client: SandboxClient, user_uuid: uuid.UUID
) -> _Reachable | None:
    """Attach to this user's container and ask it for its `HEAD`. `None` if it cannot be asked.

    THE GATE IS ONLY A GATE IF THIS RUNS. `confirm_durable_copy` reads `None` as "unreachable, so
    a parseable recovery bundle stands in" — the fallback for a dead orphan. Pass `None`
    UNCONDITIONALLY instead and that fallback becomes the only reachable branch: the `STALE`
    comparison is dead code, and work newer than the last autosave reads as preserved and dies.
    Uses the ladder `project_save_state` answers with (`attach_existing`, then `container_state`)
    — a reaper must not hold a second opinion about what HEAD means."""
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


# --- the serving proof, watched out of turn -------------------------------------------------
#
# `serving_since` is the one field on the registry hash that means the app ANSWERED something,
# and every other observer of it lives inside a turn: the engine's `_watch_preview` is created
# when a turn starts streaming and cancelled at its terminal. The common shape is the opposite of
# that — the build finishes, the turn ends, the citizen keeps using the app, and THEN the dev
# server dies (OOM, exit 137, a bad edit). Nothing would retract the stamp, the registry hash is
# the one family with no TTL, and so "your app is running" would quietly decay from "it is
# serving" into "it served once, ever" while the pane framed nginx's `@app_gone` page — the
# measured 2026-09-10 defect, rebuilt one door down.
#
# SO THE SWEEP WATCHES TOO. It is the only loop in the platform that runs regardless of turns, it
# already reads every registry hash, and it already holds a `SandboxClient`. What it adds is
# LEVEL-TRIGGERED: it reads what the container is doing right now and makes the stamp agree, in
# both directions — stamping one whose observers were lost, retracting one whose app has stopped
# answering. It is also the whole remedy for a browser tab that has no button and no idea
# anything is wrong (a UI escape hatch was considered and rejected), which is exactly why it has
# to be reliable rather than clever.
#
# WHAT IT MAY NEVER DO IS DECIDE ANYTHING. A container that has not yet served is not therefore
# reapable, and this section is structured so that it CANNOT become evidence in that judgement:
# both entry points (`_observe_the_serving_proof` and `_sound_the_alarm_if_it_never_served`)
# return `None`, so there is no value for the reap decision to read; each of their call sites is
# a statement on its own line beside a `return False` / a teardown that is character for
# character the one that was already there; and nothing here marks a registry `ending`, tears
# anything down, or touches a lock, a lease or a heartbeat.


@dataclass(frozen=True)
class _Answering:
    """What one probe learned: WHICH container answered, and what it said about its dev server.

    `app_name` comes off the handle `attach_existing` built from the record IT read, never off
    the record this sweep read a moment earlier, because it is the identity the compare-and-set
    is keyed on. The registry key is per USER and survives a container swap, so the one-per-user
    slot can flip between those two reads — and the evidence belongs to the container that
    actually answered."""

    app_name: str
    status: DevStatus


async def _ask_whether_the_app_answers(
    sandbox_client: SandboxClient, user_uuid: uuid.UUID
) -> _Answering | None:
    """ONE bounded probe — attach, then ask the supervisor for its dev status. `None` when the
    container could not be asked.

    The reaper's own established ladder, the one `_reach_the_container` climbs, and safe for the
    same reason: `attach_existing` refuses a record already marked `ending`, so this can never
    reach a container on its way out. `dev_status` is asked EXPLICITLY rather than read off the
    attach's trailing `handle.ready`, which swallows its own failure into `ready=False`
    (`sandbox/client.py:917-924`) — indistinguishable from an app that is genuinely not
    answering, and the arm below retracts a citizen's standing proof on that reading.

    UNREACHABLE IS NOT DEAD, and that asymmetry is the whole safety of the retraction: an expired
    ARM credential or a throttled subscription makes every container in the fleet unaskable at
    once, and a probe that read silence as death would retract every standing proof in the
    platform and drop every pane back to "getting your app ready" over apps that are serving
    perfectly well."""
    try:
        async with asyncio.timeout(_PROBE_BUDGET_SECONDS):
            handle = await sandbox_client.attach_existing(str(user_uuid))
            status = await sandbox_client.dev_status(handle)
        return _Answering(app_name=handle.app_name, status=status)
    except TimeoutError:
        _log.info(
            "serving-proof probe ran out of its budget; no reading taken this pass",
            user_id=str(user_uuid),
            budget_s=_PROBE_BUDGET_SECONDS,
        )
        return None
    except SandboxError:
        # Gone, ending, unreachable, or its bearer unrecoverable — every one of them means the
        # same single thing here: this container cannot be asked anything right now.
        return None


#: HOW MANY SWEEPS APART A STILL-PROVEN CONTAINER IS RE-ASKED. Six, at the five-minute cadence,
#: so each one is re-asked about every half hour and each pass carries roughly a sixth of the live
#: fleet instead of all of it. The number is a straight trade of DETECTION LATENCY for SWEEP TIME,
#: and it is the right way round because this observer is the BACKSTOP, not the fast path: a turn
#: that is streaming watches its own container every second, and a citizen looking at a preview
#: that has died gets `LivePreview`'s frame-stalled cover in seconds, from the browser's own side
#: of the wire. What this arm exists for is the app that dies with nobody watching — and half an
#: hour to notice that is not worse than the nothing-at-all it replaces.
_RE_ASK_A_PROVEN_STAMP_EVERY_N_SWEEPS: Final = 6
#: The scheduled cadence of `sweep_all`, from `workers/sandbox_reap.py`. Only used to turn wall
#: clock into a sweep counter below; nothing schedules anything from here.
_SWEEP_CADENCE_SECONDS: Final = 300.0


def _this_users_turn_to_be_re_asked(user_uuid: uuid.UUID, now: datetime) -> bool:
    """Is this the pass on which this user's still-proven container gets re-asked?

    A DETERMINISTIC SHARD, DELIBERATELY, rather than a "last probed at" field on the hash. The
    alternative was one more piece of durable state that every writer would have to maintain and
    every reader could disagree about — for a scheduling detail nothing else needs to know. This
    needs no state at all: the user id fixes the shard, the clock picks the pass, and two replicas
    sweeping the same minute make the same choice.

    IT ALSO SPREADS THE LOAD, which a counter would not have. Each pass carries a different sixth
    of the fleet, so the probe cost is flat across passes instead of arriving as one spike every
    half hour that a naive `age % 30min` gate would have produced — every container stamped in the
    same busy minute would have come due in the same minute forever after.
    """
    pass_number = int(now.timestamp() // _SWEEP_CADENCE_SECONDS)
    return (user_uuid.int + pass_number) % _RE_ASK_A_PROVEN_STAMP_EVERY_N_SWEEPS == 0


def _what_this_record_is_missing(
    reg: dict[str, str], now: datetime, user_uuid: uuid.UUID, *, thin_the_re_ask: bool
) -> Literal["stamp", "retract"] | None:
    """Which correction this hash could want, or `None` to ask the container nothing.

    THE ONLY PLACE THE THREE READINGS ARE INTERPRETED, and the gate that holds the probe to at
    most one per spared user per pass — the probe below is unreachable except through this
    answer.

    ABSENT IS NOT EMPTY. A hash written before the field existed reads as PRE-CUTOVER, which the
    whole rollout treats as PROVEN: there is nothing to prove about it and nothing to retract, so
    it is left exactly as it is. Only the empty sentinel means "this container has never served",
    and only a real instant means "it did"."""
    if reg.get(REGISTRY_FIELD_STATE) != REGISTRY_STATE_READY:
        # `ending`: the reaper has already committed to destroying this container. Stamping one
        # on its way out would hand the pane a proof for a container about to stop existing.
        return None
    stamp = reg.get(REGISTRY_FIELD_SERVING_SINCE)
    if stamp is None:
        return None
    if stamp:
        # PROVEN, so the only open question is whether it is STILL true. An AGE gate is wrong
        # here — a container that first served yesterday can die a minute from now — so on the
        # FLEET SWEEP the cadence is what gets thinned instead, and only on this arm.
        #
        # WHY IT HAD TO BE THINNED THERE. This arm matches the STEADY STATE of every healthy
        # preview: ready, stamped, spared. Asked on every pass it meant one `attach_existing`
        # plus one `dev_status` per live preview per five minutes, forever — and `sweep_all`
        # walks its users ONE AT A TIME, so a fleet of N live previews added N serial probes,
        # each of them up to `_PROBE_BUDGET_SECONDS`, to a pass that used to be Redis-only. A
        # sweep that overruns its own cadence starves the REAP, which is the job that matters.
        #
        # AND WHY ONLY THERE. Reconcile-on-start asks about ONE user, because that user just
        # pressed something — there is no fleet to multiply, and the answer is about to decide
        # what they see. It pays the probe every time.
        return (
            "retract"
            if not thin_the_re_ask or _this_users_turn_to_be_re_asked(user_uuid, now)
            else None
        )
    created = an_instant_on_the_hash(reg, REGISTRY_FIELD_CREATED_AT)
    if created is None or (now - created).total_seconds() < _NOBODY_IS_COMING_AFTER_SECONDS:
        return None
    return "stamp"


async def _observe_the_serving_proof(
    redis: aioredis.Redis,
    user_uuid: uuid.UUID,
    sandbox_client: SandboxClient,
    reg: dict[str, str],
    *,
    thin_the_re_ask: bool,
) -> None:
    """Make this user's serving stamp agree with what their container is doing now.

    RETURNS NOTHING, AND THE EMPTINESS IS THE GUARANTEE: there is no value here for a caller's
    reap decision to read, so an observation cannot quietly become a verdict.

    BROAD ON THE WAY OUT, for the same reason. Every caller is a sparing arm that has already
    made up its mind, and the one thing this must never do is change that outcome — an escaping
    `RedisError` would fail the reconcile a citizen's own build start is waiting on, and an
    escaping anything would be counted by `sweep_all` as a user it could not reconcile. So the
    failure is RECORDED and the arm proceeds exactly as it would have. `CancelledError` is a
    `BaseException` and still propagates, so a shutdown stops the sweep rather than being logged
    and swallowed."""
    try:
        await _make_the_stamp_agree(
            redis, user_uuid, sandbox_client, reg, thin_the_re_ask=thin_the_re_ask
        )
    except Exception:
        _log.exception(
            "the serving-proof observation failed; the reap decision is unaffected",
            user_id=str(user_uuid),
            app_name=reg.get(REGISTRY_FIELD_APP_NAME, ""),
        )


async def _make_the_stamp_agree(
    redis: aioredis.Redis,
    user_uuid: uuid.UUID,
    sandbox_client: SandboxClient,
    reg: dict[str, str],
    *,
    thin_the_re_ask: bool,
) -> None:
    """The observation itself: at most one probe, and at most one compare-and-set."""
    now = datetime.now(UTC)
    arm = _what_this_record_is_missing(reg, now, user_uuid, thin_the_re_ask=thin_the_re_ask)
    if arm is None:
        return
    answered = await _ask_whether_the_app_answers(sandbox_client, user_uuid)  # THE ONE PROBE
    if answered is None:
        return
    status = answered.status
    if arm == "stamp":
        # A PAGE, NOT MERELY AN ANSWER — the same bar every other observer holds, and for the same
        # reason: `ready` counts a 404, and a container answering 404s has nothing to frame. The
        # backstop stamping on a weaker signal than the turn watcher would mean a container the
        # watcher correctly declined to prove got proved five minutes later by this sweep.
        if not status.shows_a_page:
            # Still nothing to show. Nothing is recorded here, on purpose: this pass repeats every
            # five minutes for the life of the container, so a line per pass would be a standing
            # alarm rather than a notice. That this container served nobody is recorded ONCE,
            # where it becomes final — `SERVING_PROOF_NEVER_ARRIVED`, at teardown.
            return
        created = an_instant_on_the_hash(reg, REGISTRY_FIELD_CREATED_AT)
        if await mark_serving(redis, user_uuid, app_name=answered.app_name, when=now):
            _log.info(
                APP_FIRST_SERVED_EVENT,
                user_id=str(user_uuid),
                app_name=answered.app_name,
                serving_since=now.isoformat(),
                # Non-None by construction — the stamp arm is reached only through an age gate
                # that needs it — but expressed rather than asserted, since `python -O` strips
                # an `assert` and this line is the operator's whole answer to "how long".
                ms_since_container_created=elapsed_ms(created, now),
                # WHICH watcher won, and this one winning is itself the finding: it means every
                # in-turn observer for this container was lost. `cold` is deliberately absent
                # rather than guessed — it says which arm CREATED the container, and a sweep that
                # arrived minutes later was not there for it.
                observer=_OBSERVER_RECONCILER,
            )
            return
        await _record_a_refused_stamp(redis, user_uuid, expected_app=answered.app_name)
        return
    # BOTH READINGS HAVE TO SAY NO, and `ready` is the one that carries the weight. `running` is
    # child-process truth about the dev server the supervisor itself started; `ready` means a
    # request to the app root ACTUALLY SUCCEEDED (`sandbox/supervisor/app.py:1118-1130`). The
    # open sandbox lets the agent `pkill` our child and `nohup` its own replacement, so
    # `running=False` alongside `ready=True` is a documented NORMAL state for an app serving its
    # citizen perfectly well — retracting on `running` alone would take the frame away from
    # exactly those apps.
    if status.running or status.ready:
        return
    if await clear_serving(redis, user_uuid, app_name=answered.app_name):
        served_since = an_instant_on_the_hash(reg, REGISTRY_FIELD_SERVING_SINCE)
        _log.warning(
            APP_SERVING_LOST_EVENT,
            user_id=str(user_uuid),
            app_name=answered.app_name,
            # ONE READING, AND THAT IS THE HONEST NUMBER rather than a missing debounce. The
            # in-turn crash edge debounces because it reads the app every second and a single
            # miss is a network hiccup; this reading was taken from a supervisor that ANSWERED,
            # says its own child is dead AND that a request to the app root failed, and the next
            # one is five minutes away. The retraction is also recoverable: the field goes back
            # to the empty sentinel, so the next observer to watch this app serve re-stamps it.
            unanswered_polls=1,
            exit_code=status.exit_code,
            served_for_ms=elapsed_ms(served_since, now),
            observer=_OBSERVER_RECONCILER,
        )


async def _record_a_refused_stamp(
    redis: aioredis.Redis, user_uuid: uuid.UUID, *, expected_app: str
) -> None:
    """Tell a benign race apart from the near-miss, and alarm on the near-miss only.

    `mark_serving` answers False for two very different situations and cannot distinguish them
    from the inside. One is another observer landing the same first serve microseconds earlier —
    first-serve-wins working exactly as designed, and their `app_first_served` line is the
    record, so a second line here would double-count a single event. The other is the dangerous
    one: the hash is gone, marked `ending`, or names a different container, which means this
    observer came back holding evidence about a container that no longer occupies the slot.

    The re-read is what separates them. `found_app_present` is a BOOL and never the other
    project's container name — the id vocabulary in this log stays user-scoped, and naming the
    loser would put one of the citizen's projects into another's build trace."""
    reg = await read_registry(redis, user_uuid)
    if (
        reg is not None
        and reg.get(REGISTRY_FIELD_APP_NAME) == expected_app
        and reg.get(REGISTRY_FIELD_SERVING_SINCE)
    ):
        return
    _log.warning(
        SERVING_PROOF_STAMP_REFUSED,
        user_id=str(user_uuid),
        expected_app=expected_app,
        found_app_present=bool(reg is not None and reg.get(REGISTRY_FIELD_APP_NAME)),
        observer=_OBSERVER_RECONCILER,
    )


def _sound_the_alarm_if_it_never_served(reg: dict[str, str], *, user_uuid: uuid.UUID) -> None:
    """A container is about to stop existing having served nobody, ever — say so, once.

    THE INVERSE OF THE BUG THE STAMP WAS BUILT FOR. The stamp stops the platform reporting a
    scheduled container as running; this catches the other failure, an app that never answered
    anything at all, which is today indistinguishable in the logs from a flawless build.

    ONLY THE EMPTY SENTINEL FIRES IT. An absent field is a pre-cutover record, which says nothing
    either way about whether that container served, and alarming on silence would fill the log
    with the fleet that was already running at deploy time.

    THE REAPER CANNOT SEE WHETHER `dev_start` SUCCEEDED — that happened in another process, and
    the registry hash does not record it — so `reason` says which teardown this was and the
    alarm's own runbook sends the operator to the same build's `sandbox_dev_started` line to
    separate "never started" from "started and never compiled"."""
    # `stamp_is_proven` rather than a bare `!= ""`: absent AND an instant both mean there is
    # nothing to sound an alarm about, and which of the three readings mean what is decided in
    # exactly one place. This line used to re-type that rule as a comparison.
    if stamp_is_proven(reg):
        return
    created = an_instant_on_the_hash(reg, REGISTRY_FIELD_CREATED_AT)
    _log.warning(
        SERVING_PROOF_NEVER_ARRIVED,
        user_id=str(user_uuid),
        app_name=reg.get(REGISTRY_FIELD_APP_NAME, ""),
        lifetime_ms=elapsed_ms(created, datetime.now(UTC)),
        # `reap_idle` for every teardown on this path, and it is accurate rather than convenient:
        # `reap_user` is reached only once the lock, the heartbeat, the liveness lease, the
        # start-in-flight marker and any stay of execution have all lapsed or been certified
        # dead. The other reasons in the vocabulary belong to teardowns the reaper does not do.
        reason="reap_idle",
    )


async def _take_the_copy_we_promised(
    sandbox_client: SandboxClient,
    *,
    app_id: uuid.UUID,
    verdict: CopyVerdict,
    reached: _Reachable | None,
    expected_name: str,
) -> bool:
    """True when this container may now be reclaimed.

    A copy is TAKEN when the newest durable copy predates the newest change. Both call sites once
    spared, so a failed autosave billed forever behind a log line repeating every fifteen minutes
    and looked, to anyone reading it, like the guard working correctly. This really happened. The
    copy goes through `write_recovery_copy`, not a raw `put`: it promotes only a descendant of the
    copy on record and cannot run against an empty slot or pre-stamp bundle, whose write is kept
    but does NOT authorise the destroy (`UNGUARDED`). Every failing arm SPARES and RECORDS."""
    # IMPORTED HERE, NOT AT MODULE SCOPE, and the reason is weight rather than a cycle. There is
    # no import cycle — `src.workers.reclamation` imports the reaper function-scoped, so nothing
    # closes a loop at module-import time. The weight is real: `pass_history` reaches
    # `src.db.base`, which BUILDS THE ORM ENGINE at import, so a module-level bind puts that
    # (and `src.broker`, by way of `src.workers.reclamation`) behind every import of the reaper
    # — including the cold one `test_the_reaper_imports_without_the_fastapi_app` performs.
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
        # with another app's work; the guarded write would probably divert it, but "probably
        # caught one layer down" is not a reason to hand it the wrong tree.
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
        # The guarded write refused to promote this tree and preserved it under `divert_key`. It
        # has already raised the pinned "recovery write did not land" alarm with the two shas that
        # explain why, so nothing is re-alarmed here — the container is simply spared, which is
        # the only answer available when the tree in hand cannot be shown to contain the work.
        await record_durable_copy_attempt(CopyAttempt.REFUSED)
        return False
    if written.recorded_head is None:
        # THE COPY LANDED, BUT NO GUARD RAN. There was nothing on record to compare it against, so
        # `write_recovery_copy` took its first-write arm — which is right at a turn boundary,
        # where the container is alive and the tree is the citizen's, and wrong here.
        #
        # A REVERTED CONTAINER HAS EXACTLY THIS SHAPE. An app whose every autosave failed has an
        # empty recovery slot, so a reverted container's empty tree becomes the first copy on
        # record, `recoverable_work` ranks it newest by `last_modified`, and the citizen's next
        # build is restored from the template over their saved app. Then this function would
        # return True and delete the container holding the only real tree.
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

    `strict` separates "nothing was registered" from "teardown failed". A sweep needs neither
    (fire-and-forget, retried in five minutes); a caller about to ACT does — a still-standing
    container would walk the client back into the refusal `release_project_sandbox` just told it
    was resolved, so `strict=True` re-raises and it can answer 503. Lock + registry are KEPT on
    failure either way. `app_id` opts into the durable-copy gate: `None` suits callers whose
    builder is about to get a fresh container; the unwatched janitor always passes it."""
    reg = await read_registry(redis, user_uuid)
    if reg is None:
        # No sandbox registered — just clear any orphaned lock so a crashed-tab user is
        # never locked out.
        await reap_lock(redis, user_uuid)
        return False
    registered_name = reg.get(REGISTRY_FIELD_APP_NAME, "")
    # #198: the per-user slot this record names can hold EITHER lineage — the user's own build
    # sandbox or a colleague's shared-runtime view restored into it — and both are provably ours
    # to tear down. Recognizing only `sbx-` here was the orphaning bug the shared runtime would
    # otherwise reproduce on every Revoke: the record would be deleted (below) while a `shr-`
    # container it could not vouch for kept running and billing, forever anonymous.
    if not (is_a_sandbox_name(registered_name) or is_a_shared_sandbox_name(registered_name)):
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
    # THE DURABLE-COPY GATE NEVER RUNS FOR A SHARED VIEW (#198), whatever `app_id` the caller
    # resolved. `sweep_all`'s own `_owning_app_id` currently maps a `shr-` registry record to the
    # OWNER's app id (`_app_names_to_owners` keys every `shr-` name off the recipient, but the
    # value it carries is still the shared app's id) — passing that here would gate this
    # RECIPIENT's teardown against the OWNER's recovery slot, and R22 says that storage is
    # read-never-write for a recipient. Worse, a diverted or first-write guarded write then
    # REFUSES the reap outright, sparing the container forever — the exact bill-forever leak R16/
    # R17 exist to close. A shared view holds nothing worth preserving in the first place: the
    # recipient never edits its tree directly, and what they own of it is a restore of the
    # owner's own snapshot, already durable at its source.
    if app_id is not None and not is_a_shared_sandbox_name(registered_name):
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
        # AND THEN TAKE THE COPY, rather than sparing on the strength of the
        # verdict alone.
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
    # THE RECORD IS STILL IN HAND, and this is the last moment it will be: `reg` was read
    # before the mark-ending flip and the delete below is about to remove it for good. The
    # container's whole life is over, so whether it ever served anybody is now a settled fact.
    _sound_the_alarm_if_it_never_served(reg, user_uuid=user_uuid)
    await delete_registry(redis, user_uuid)  # registry cleared
    # ...and the liveness lease goes WITH the record it belonged to. Only here, after a
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

    NOT `reap_user`, which destroys whatever the registry currently names for a user. Keying by
    name keeps the janitor honest across an enumerate-then-delete pass: a sandbox started between
    the two would otherwise be deleted while the judged orphan was spared, and an unregistered
    orphan — the population this exists to collect — reported destroyed with nothing deleted. The
    ARM delete uses the judged name; the user's Redis state is touched ONLY when the registry
    still names it: `mark_registry_ending` -> `teardown` -> `delete_registry` -> `reap_lock`."""
    reg = await read_registry(redis, user_uuid)
    ours = reg is not None and reg.get(REGISTRY_FIELD_APP_NAME) == app_name
    # The container is only reachable THROUGH the registry — `attach_existing` builds its handle
    # from that record — so a container the store no longer claims can be judged on its recovery
    # copy alone. That is the gate's documented fallback, and it still demands a parseable bundle.
    # It is also why no copy can be taken for an unregistered orphan: there is no address to
    # bundle from, and the address we DO have belongs to somebody else's container.
    reached = await _reach_the_container(sandbox_client, user_uuid) if ours else None
    verdict = await confirm_durable_copy(
        app_id,
        container_head=reached.head if reached else None,
        container_dirty=reached.uncommitted if reached else None,
    )
    # AND THEN TAKE THE COPY. The janitor is the caller with nobody watching it.
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


_SHARED_PREVIEW_ABSOLUTE_CEILING = timedelta(seconds=SHARED_PREVIEW_ABSOLUTE_CEILING_SECONDS)


async def _renew_shared_view_from_traffic(
    redis: aioredis.Redis,
    user_uuid: uuid.UUID,
    sandbox_client: SandboxClient,
    reg: dict[str, str],
) -> None:
    """#198's ONLY liveness signal for a shared-runtime view: real traffic through the app,
    self-reported by the supervisor (`SandboxClient.served_count`, excluding every
    control-plane probe — see `sandbox/Caddyfile`). A no-op for every OTHER kind of record: a
    build sandbox has `TURN_IN_FLIGHT`/`BUILDER_ACTED`/its own heartbeat instead, and this must
    never compete with those or run an extra supervisor round trip on their behalf.

    OBSERVATION, NOT AN INPUT, same posture as `_observe_the_serving_proof` and for the same
    reason: it runs ahead of every sparing arm in `reconcile_user`, so a failure here must never
    affect the reap decision reading them. Recorded and swallowed; `CancelledError` still
    propagates."""
    app_name = reg.get(REGISTRY_FIELD_APP_NAME, "")
    if not is_a_shared_sandbox_name(app_name):
        return
    try:
        handle = await sandbox_client.attach_existing(str(user_uuid))
        served = await sandbox_client.served_count(handle)
        if served is None:
            return  # could not ask; the ceiling and the standing stay decide instead
        last_seen_raw = reg.get(REGISTRY_FIELD_SHARED_SERVED_COUNT)
        last_seen = int(last_seen_raw) if last_seen_raw else 0
        # `truncated` IS ITS OWN EVIDENCE OF ONGOING TRAFFIC (`ServedCount`'s own docstring) —
        # checked BEFORE the equality comparison, not folded into it. The supervisor's count is
        # a bounded TAIL, not a cumulative total, so once real traffic pushes the log past that
        # window `served.count` plateaus OR DROPS (a log roll starts a fresh, smaller window at
        # `truncated=False`). Comparing with `<=` treated that drop as "no new traffic" and
        # reaped a session mid-use the moment it rolled, even though the count changing at all —
        # in either direction — while untruncated is itself proof something new happened; only
        # an EXACT match means nothing changed since the last pass. A `truncated` reading means
        # the log has substantial recent activity in it BY DEFINITION — enough to have filled
        # the window — so it renews unconditionally rather than trusting a number that can no
        # longer answer "did anything NEW happen".
        if not served.truncated and served.count == last_seen:
            # No NEW traffic since the last pass — a steady background poll from an idle tab
            # must not read as fresh evidence every five minutes forever, or the ceiling above
            # is the only thing that would ever end a session nobody is actually reading.
            return
        await redis.hset(
            registry_key(user_uuid), REGISTRY_FIELD_SHARED_SERVED_COUNT, str(served.count)
        )
        await grant_stay_of_execution(redis, user_uuid, writer=DeadlineWriter.APP_SERVED_TRAFFIC)
    except Exception:
        _log.exception(
            "shared-view traffic observation failed; the reap decision is unaffected",
            user_id=str(user_uuid),
            app_name=app_name,
        )


def _shared_view_past_its_ceiling(reg: dict[str, str], now: datetime) -> bool:
    """#198's absolute session ceiling (requirement 20) — independent of the renewable traffic
    stay above, so a wedged or spoofed supervisor report can never buy a shared view
    immortality. `False` for every OTHER kind of record (an ordinary build sandbox has no
    ceiling of its own here; its own signals govern it) and for a shared view still inside the
    window — this only ever SUBTRACTS from what the stay would otherwise spare, never adds a
    reason to spare one."""
    if not is_a_shared_sandbox_name(reg.get(REGISTRY_FIELD_APP_NAME, "")):
        return False
    created = an_instant_on_the_hash(reg, REGISTRY_FIELD_CREATED_AT)
    if created is None:
        return False  # cannot prove an age; the arms in `reconcile_user` decide instead
    return now - created >= _SHARED_PREVIEW_ABSOLUTE_CEILING


async def reconcile_user(
    redis: aioredis.Redis,
    user_uuid: uuid.UUID,
    sandbox_client: SandboxClient,
    *,
    has_live_session: bool,
    honor_stay: bool = False,
    certified_dead: bool = False,
    app_ids_by_name: Mapping[str, uuid.UUID] | None = None,
    thin_the_re_ask: bool = False,
) -> bool:
    """Reconcile the user's OWN stale state; True if it reaped. Reaps only when a registry entry
    exists, no live in-process session is held, and the state does not merely LOOK live.

    `honor_stay`, the liveness lease and `certified_dead` are three caller asymmetries, not one
    behaviour with three names; each arm below says what collapsing it would cost.
    `certified_dead` is the sweep's forbidden argument, pinned by
    `test_no_worker_module_may_certify_death`: only a caller under the per-user start lock, on a
    single-replica deploy, holds the facts it asserts.

    FOUR OF THE FIVE SPARING ARMS ALSO TAKE A READING of the serving proof on their way out —
    the liveness lease, the start-in-flight marker, the lock/heartbeat pair and the stay of
    execution, which are the four that hold a registry record when they spare. It is an
    OBSERVATION AND NEVER AN INPUT: `_observe_the_serving_proof` returns nothing, and every
    `return False` below is the one that was already there. The fifth arm, `has_live_session`,
    is deliberately not one of them: it returns above the registry read, so observing there
    would cost every build start an extra Redis round trip, its caller is about to take the
    slot itself anyway, and it is never True on the scheduled sweep — `workers/sandbox_reap.py`
    passes `has_live_session=False` and an empty `live_users`, which is the pass this backstop
    exists to run on."""
    if has_live_session:
        # `run_build` outlives the SSE disconnect, so a multi-minute build whose tab closed
        # over 90 seconds ago still owns a session here and is not reaped mid-flight.
        return False
    if certified_dead:
        # DELETE THE LEASE, do not merely decline to read it — and do it here, above every
        # other arm, so a stray lease is cleared even when there is no registry left to
        # reap. Leaving it would let the background sweep go on sparing a container this
        # call has already certified dead and is about to tear down, and the next build
        # registers a DIFFERENT container under the same user. It would also 409 this same
        # builder's next start until the TTL lapsed — the crashed-tab lockout, reproduced.
        await release_liveness_lease(redis, user_uuid)
    reg = await read_registry(redis, user_uuid)
    if reg is None:
        await reap_lock(redis, user_uuid)  # clear any orphaned lock (no lockout)
        return False
    # #198: a no-op for every record but a shared-runtime view (checked inside), so this changes
    # nothing about the four arms below for an ordinary build sandbox. Ahead of them because a
    # renewal it grants THIS pass must be visible to the stay check further down THIS SAME pass —
    # picking it up only on the next sweep would needlessly reap a session that just proved active.
    await _renew_shared_view_from_traffic(redis, user_uuid, sandbox_client, reg)
    if not certified_dead and await liveness_lease_is_held(redis, user_uuid):
        # The one liveness input readable from a process that is not running the build.
        # Checked BEFORE the lock/heartbeat pair below because it outranks it in both
        # directions — a live build has lost that pair 90 seconds in, and a dead one leaves
        # it standing for a TTL. A held lease means an agent is making tool calls inside
        # that container right now.
        await _observe_the_serving_proof(
            redis, user_uuid, sandbox_client, reg, thin_the_re_ask=thin_the_re_ask
        )
        return False
    if not certified_dead and await read_starting_marker(redis, user_uuid) is not None:
        # THE PRE-ADOPT WINDOW, and the only signal that can cover it. Between the registry hash
        # landing and the container's heartbeat being seeded, the lock/heartbeat pair below is an
        # AND that cannot be satisfied — so a sweep landing mid-cold-start would reap a container
        # this user is seconds away from building in. The marker spans exactly that interval and
        # carries a mandatory TTL, so it stops sparing on its own rather than needing anyone to
        # remember to clear it.
        #
        # BELOW the lease, above the pair, for the same reason the lease sits where it does: a
        # start that has already reached a live turn is answered by the stronger signal first.
        await _observe_the_serving_proof(
            redis, user_uuid, sandbox_client, reg, thin_the_re_ask=thin_the_re_ask
        )
        return False
    if (
        not certified_dead
        and await lock_is_held(redis, user_uuid)
        and await heartbeat_is_alive(redis, user_uuid)
    ):
        # looks live + recent (bounded by the heartbeat TTL) — leave it
        await _observe_the_serving_proof(
            redis, user_uuid, sandbox_client, reg, thin_the_re_ask=thin_the_re_ask
        )
        return False
    if (
        honor_stay
        and await stay_of_execution_is_current(redis, user_uuid)
        # #198: `False` for every record but a shared view (checked inside), so this changes
        # nothing about a relaunched build preview's own reprieve. A shared view past its
        # absolute ceiling falls straight through to the reap below EVEN THOUGH its stay is
        # still current — the one condition nothing renews, by design (requirement 20).
        and not _shared_view_past_its_ceiling(reg, datetime.now(UTC))
    ):
        # A relaunched preview holds no lock and renews no heartbeat, so the stay is all that
        # stands between it and the sweep, which passes True. Reconcile-on-start keeps the
        # default and reaps THROUGH an unexpired stay: the incoming build needs the single
        # per-user slot, and sparing the preview there would orphan its own container.
        await _observe_the_serving_proof(
            redis, user_uuid, sandbox_client, reg, thin_the_re_ask=thin_the_re_ask
        )
        return False
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
    to close."""
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
    """SCAN-iterate the registry namespace (never `KEYS`) and reconcile each user; returns what
    it reaped AND what it could not. Idempotent + concurrency-safe, safe to call on a timer.
    `live_users` are the SessionManager's live in-process sessions — never reaped.

    The scheduled reader of the liveness lease — a lease nothing consults spares nothing. Keeps
    `certified_dead=False`, holding none of the facts certification rests on, and passes
    `honor_stay=True`, since a timer has no reason to kill what the user is still looking at.
    `app_ids_by_name` is FORWARDED: the name->id match happens where the record is read."""
    live = live_users if live_users is not None else set()
    reaped = 0
    failed = 0
    # TWO LITERALS RATHER THAN ONE WILDCARD, deliberately: a single `bial:*:sandbox:registry:*`
    # would match OTHER ENVIRONMENTS' keys and reap their containers. A user with a key under
    # both prefixes is visited twice; `reconcile_user` is idempotent, and `seen` keeps the counts
    # honest anyway.
    seen: set[uuid.UUID] = set()
    async for raw_key in _scan_the_registry_namespace(redis):
        user_uuid = _user_from_registry_key(str(raw_key))
        if user_uuid is None or user_uuid in live or user_uuid in seen:
            continue
        seen.add(user_uuid)
        # ONE USER'S FAILURE IS ONE USER'S FAILURE. Unguarded, the first exception would end
        # the whole cycle and every user later in SCAN order would go unreconciled — silently,
        # because SCAN order is not stable enough for anyone to notice the same victims twice.
        # The reachable case is an ARM throttle: `reap_user`
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
                # THE FLEET IS WHAT MAKES THE PROBE EXPENSIVE, so the fleet is where it is
                # thinned. Reconcile-on-start leaves this False and pays it every time.
                thin_the_re_ask=True,
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
