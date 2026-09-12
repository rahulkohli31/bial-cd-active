"""The in-process build-session lifecycle: `SessionManager` + `BuildSession`.

THIS FILE NO LONGER STARTS BUILDS. `start` / `_start_locked` / `_run_and_finalize` — the
standalone build path behind the deleted start route — are gone, and with them the only place
that ever assigned `BuildSession.task` or called a `run_build`. What allocates a workspace now is
`ensure_sandbox`, on behalf of a Write chat turn whose agent runs in `services/turns/engine.py`.
The end sequence below is unchanged and still live: `stop` (the take-back) and `force_end` both
reach it, and it is the reader for build sessions the transcript still points at.

WHY THIS EXISTS. The non-serializable core of a session — the `SandboxHandle` holding the raw
bearer, the progress `asyncio.Queue` subscribers, the in-process envelope buffer — lives in
memory, NOT Postgres. On a single replica the whole session is in-process; the frozen Redis keys
(lock/heartbeat/registry) are the durable cross-restart coordination.

Teardown and lock-release belong to this module, SESSION-API-owned, not the build itself.
`_finalize` runs the authoritative end sequence exactly once (guarded by `terminal_committed`):
snapshot → teardown-or-pardon → holder release → emit THE terminal `ended`, always AFTER that
snapshot step so `snapshot_committed` on it is the real post-commit value. A completed build's
container is PARDONED, not executed: it stays up under the bounded stay-of-execution lease
(registry kept, lock released) so the user can use what they just built; every other end path —
quota / escalated / stop / force_end / idle-reap — still tears down and clears the registry.
Every end path converges on this one emission, so the feed carries exactly one terminal, always
truthful.

The sandbox client is threaded IN from the router's `Depends`, never resolved inline, so
`app.dependency_overrides` reach it in tests.
"""

from __future__ import annotations

import asyncio
import enum
import hashlib
import time
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from contextlib import AbstractAsyncContextManager, asynccontextmanager, contextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from functools import partial
from typing import Final, Literal

import redis.asyncio as aioredis
import sqlalchemy as sa
import structlog
from redis.exceptions import RedisError
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.v1.build_sessions.schemas import (
    BuildResult,
    BuildSessionStatus,
    EndedEvent,
    PreviewLifeState,
    PreviewReadyEvent,
    PreviewReconnectingEvent,
    ProgressEnvelope,
)
from src.config import settings
from src.db.base import async_session_factory
from src.db.models.app_registry import AppRegistry
from src.db.models.harness_counter import HarnessCounter
from src.db.models.project import Project
from src.db.models.user import User
from src.services.build_sessions.alarms import (
    APP_FIRST_SERVE_NOT_OBSERVED_EVENT,
    APP_FIRST_SERVED_EVENT,
    BUILD_WORKSPACE_CLAIMED_EVENT,
    PREVIEW_STATE_REPORTED_UNKNOWN_EVENT,
    RECOVERY_WRITE_DID_NOT_LAND_EVENT,
    SANDBOX_TORN_DOWN_EVENT,
    SERVING_PROOF_STAMP_REFUSED,
    WORKSPACE_LOST_WHILE_IDLE_EVENT,
)
from src.services.build_sessions.appdata import build_app_env, resolve_app_for_project
from src.services.build_sessions.appdb_env import provision_app_database
from src.services.build_sessions.appstorage import provision_app_storage
from src.services.build_sessions.counters import count
from src.services.build_sessions.integrity import (
    IntegrityVerdict,
    WorkspaceState,
    clean_but_for_churn,
    container_state,
    only_regenerated_files_changed,
    workspace_integrity,
)
from src.services.build_sessions.liveness import flag_liveness_overpromise
from src.services.build_sessions.locks import (
    DeadlineWriter,
    acquire_lock,
    an_instant_on_the_hash,
    clear_serving,
    clear_starting_marker,
    delete_registry,
    elapsed_ms,
    grant_stay_of_execution,
    mark_registry_ending,
    mark_serving,
    read_registry,
    read_registry_and_starting_marker,
    reap_lock,
    release_lock_as_holder,
    renew_lock,
    stamp_is_proven,
    write_heartbeat,
    write_starting_marker,
)
from src.services.build_sessions.outcome import (
    FORCE_ENDED,
    STOPPED_BY_USER,
    newest_build_outcome_status,
    write_build_outcome,
)
from src.services.build_sessions.reaper import is_a_shared_sandbox_name, reap_user, reconcile_user
from src.services.build_sessions.snapshot import (
    SNAPSHOT_EXEC_TIMEOUT_SECONDS,
    SNAPSHOT_EXECS,
    Destination,
    RecoveryOutcome,
    consecutive_diverts,
    write_recovery_copy,
    write_snapshot,
)
from src.services.orchestrator.constants import READINESS_POLL_S
from src.services.redis import RedisNotConfiguredError, get_redis
from src.services.redis.keys import (
    REGISTRY_FIELD_APP_NAME,
    REGISTRY_FIELD_CREATED_AT,
    REGISTRY_FIELD_FQDN,
    REGISTRY_FIELD_SERVING_SINCE,
    REGISTRY_FIELD_SHARED_OWNER_ID,
    REGISTRY_FIELD_SHARED_PROJECT_ID,
    REGISTRY_FIELD_STATE,
    REGISTRY_STATE_READY,
)
from src.services.sandbox import (
    SANDBOX_NAME_PREFIX,
    SHARED_SANDBOX_NAME_PREFIX,
    CompileState,
    SandboxClient,
    SandboxError,
    SandboxGoneError,
    SandboxHandle,
    SandboxNotReadyError,
)
from src.services.storage import (
    BundleValidationError,
    StorageError,
    StorageNotFoundError,
    StorageUnconfiguredError,
    get_storage,
    parse_bundle_head_sha,
    recovery_key,
    snapshot_key,
)
from src.services.storage.base import ObjectMeta

_log = structlog.get_logger()

# `build_failed` is the only reason that maps to the terminal FAILED status; every other
# end reason (stopped_by_user / idle_teardown / quota_exceeded / completed) is graceful.
_BUILD_FAILED: str = "build_failed"

# The one end reason that PARDONS the container instead of tearing it down: a
# successful build's preview stays live under the idle lease so the user sees what they
# just built. Matches BRAIN's success verdict and `_do_finalize`'s legacy fallback.
_COMPLETED: str = "completed"

# Bounded retry for the restore path's two fallible steps. Budgets differ because the
# steps cost wildly different amounts: `head` is a single cheap metadata call, so retrying it
# is nearly free (worst case ~0.75s of added start latency); `restore_from_snapshot` re-runs a
# whole container provision + `npm install`, so it gets ONE retry — enough to ride out a
# registry blip, bounded enough that a doomed start still fails in ONE-digit minutes rather
# than looping. Be honest about that bound: each restore attempt can block up to the sandbox
# layer's `_RESTORE_TIMEOUT_SECONDS` (600s, `services/sandbox/client.py`) when the npm reconcile
# hangs to its own timeout, so the true worst case here is ~2 × 600s + backoff ≈ 20 minutes, not
# "tens of seconds". That is a deliberately generous ceiling on the RARE hung-install case (the
# common failure — a `set -e` npm error — raises in seconds); an outer start deadline would be a
# design change, not a comment fix.
# Both exhaust into `SnapshotUnavailableError`; neither may fall back to fresh.
_HEAD_ATTEMPTS: int = 3
_HEAD_BACKOFF_SECONDS: float = 0.25


async def head_presence(key: str) -> bool | None:
    """Does this KEY hold a restorable bundle? `True` = present, `False` = CONFIRMED absent,
    `None` = the store could not be reached. A blip is retried; an unanswered check reports
    unknown rather than guessed, since treating it as absent would let finalize's later
    snapshot overwrite real work with a freshly provisioned blank template (mirrors the
    submit route's fail-closed read). Both readers share this one expression: the build path
    aborts on `None` (`snapshot_exists_or_bust`), the projects read shows it as "we cannot
    say". The store is resolved once, outside the retry loop — no-store-configured is
    permanent, not transient."""
    try:
        store = get_storage()
    except StorageUnconfiguredError:
        # NOT a transient failure — the supported storage-off deployment (`src.config` gates
        # the requirement on `is_production`; `provision_app_storage` returns {} here for the
        # same reason). With no store there can be no bundle, so this is a CONFIRMED absent,
        # the exact distinction that matters here. Folding it into the unknown arm instead
        # would 503 EVERY build start on such a deployment.
        return False
    attempt = 0
    while True:
        attempt += 1
        try:
            return await store.head(key) is not None
        except StorageError:
            if attempt >= _HEAD_ATTEMPTS:
                _log.exception(
                    "head-check failed on every attempt; reporting the state as "
                    "UNKNOWN rather than guessing at it",
                    key=key,
                    attempts=attempt,
                )
                return None
            _log.warning(
                "head-check failed; retrying",
                key=key,
                attempt=attempt,
                exc_info=True,
            )
            await _asleep(_HEAD_BACKOFF_SECONDS * 2 ** (attempt - 1))


async def snapshot_presence(app_id: uuid.UUID) -> bool | None:
    """`head_presence` for an app's SAVED bundle — the two readers named above."""
    return await head_presence(snapshot_key(app_id))


async def restorable_presence(app_id: uuid.UUID) -> bool | None:
    """Could the platform put this app back, from anything? Checks `recovery_key` OR
    `snapshot_key` — the pair `newest_restore_source` also consults, since offering a restore
    means "would a restore find something". `snapshot_presence` alone under-reports a builder
    who worked across turns but never pressed save: they have only the turn-boundary recovery
    copy, and "no saved build" is the wrong thing to tell them while holding their workspace.
    Container-independent on purpose (`SaveState.recovery_at` is null in exactly these
    reclaimed cases). Tri-state: confirmed presence wins immediately, two confirmed absences
    are a real `False`, an unreadable store returns `None`."""
    recovery = await head_presence(recovery_key(app_id))
    if recovery:
        return True
    saved = await snapshot_presence(app_id)
    if saved:
        return True
    # Both are now False-or-unknown. One unknown is enough to disqualify a confident "no".
    return False if recovery is False and saved is False else None


async def snapshot_exists_or_bust(app_id: uuid.UUID) -> bool:
    """The build path's reading of `snapshot_presence`: an unknown state ABORTS the start
    rather than provisioning over work that may be restorable."""
    presence = await snapshot_presence(app_id)
    if presence is None:
        raise SnapshotUnavailableError("snapshot state unknown after retries", app_id=app_id)
    return presence


_RESTORE_ATTEMPTS: int = 2
_RESTORE_BACKOFF_SECONDS: float = 1.0


async def _asleep(seconds: float) -> None:
    """Backoff sleep behind one indirection so tests can record the schedule without real
    waits (mirrors `sandbox/client.py::_asleep`)."""
    await asyncio.sleep(seconds)


def _terminal_status(reason: str) -> Literal[BuildSessionStatus.ENDED, BuildSessionStatus.FAILED]:
    """The terminal status for a SESSION-API-originated end reason (stop / force_end /
    idle-reap / a raised run_build). Only sound because those reasons are a closed, graceful
    set plus `build_failed` — BRAIN's reasons are NOT derivable this way (`escalated` is FAILED
    yet != `_BUILD_FAILED`), which is why its verdict carries an explicit `status`."""
    return BuildSessionStatus.FAILED if reason == _BUILD_FAILED else BuildSessionStatus.ENDED


# How long an ended session (with its envelope replay buffer) stays resident after its
# terminal commit: long enough that a late SSE reconnect still replays + [DONE], short
# enough that `_sessions` never grows unbounded. Evicted opportunistically at the top of
# start() and on the internal reap sweep — nothing evicts them on a timer. Read as scoped to
# THIS in-process map, which is per-process state no shared scheduler could reach, even though
# the repo does have other scheduled work elsewhere.
_ENDED_RETENTION_SECONDS: float = 300.0

# How long a start will wait for an ended-but-still-finalizing session's shielded end
# sequence before keeping the 409 — a refine sent right after natural completion must not
# bounce off its own finished build (the finalize is usually sub-second; the bound only
# guards a wedged teardown).
_FINALIZE_GRACE_SECONDS: float = 30.0

# How long the end sequence will wait for the outcome record before giving up and emitting the
# terminal anyway. The write is a handful of indexed queries against a live connection —
# seconds is already generous, and the terminal frame is worth more than the record: without it
# every SSE feed hangs and the session is never evicted.
_OUTCOME_WRITE_TIMEOUT_SECONDS: float = 10.0

# How long a relaunch waits for `dev/status.ready`, PER ARM. The two arms are asking genuinely
# different questions, which is why one number could not serve both.
#
# The COLD arm has just provisioned a container and restored a bundle into it: the wait covers a
# real boot — `npm` reconcile, a first Turbopack compile — so it keeps the client's historic
# 120s. Nothing is at risk while it waits; the snapshot on Blob is the durable copy.
#
# The ATTACHED arm is asking "is the app this container is ALREADY running serving yet?", and
# `ready` means a request was actually SERVED. So this budget is not really measuring
# the container at all — it is measuring the citizen's own root route, and a heavy dashboard
# query or an external fetch blows any budget you pick. Waiting longer cannot turn a slow page
# into a fast one; it only makes the citizen stare at a spinner before we hand back the very
# same URL. 15s is comfortably above a warm attach (measured at ~380ms end to end) and low
# enough that a slow app degrades promptly instead of two minutes later.
# The autosave runs on a turn's exit path, so the whole SEQUENCE gets one bound. Each exec in it
# is already capped individually, but five of them in a row is minutes, and a wedged container must
# not hold the turn's ending open. Generous enough for a real bundle over the supervisor, short
# enough that failing is quicker than hanging.
#
# AND IT IS THE TURN-BOUNDARY RECOVERY COPY'S ONLY BOUND. A separate 180 s budget used to wrap
# that copy; the autosave reconciliation replaced its `asyncio.timeout` arm with this one and
# left the constant behind, unread, contradicting the number actually enforced — so it has been
# swept. The copy is bounded here, not unbounded, and `_STOP_ACTIVE_WORK_TIMEOUT_SECONDS` below
# derives from THIS number. (The reaper's own copy is a different path and carries no wrapper.)
_RECOVERY_SNAPSHOT_TIMEOUT_SECONDS: float = 60.0

# How long "stop the work so I can switch projects" waits for the turn to actually unwind.
#
# DERIVED FROM THE PATH IT WAITS ON, not chosen. The old 30 s was picked to bound a REQUEST the
# citizen was sitting in front of — and it was BELOW the unwind's own bounds, so an ordinary,
# healthy turn could outlast it and be reported as "still running" for doing exactly what it is
# supposed to do. The two branches of `_stop_the_held_session` unwind differently, and the budget
# is the LONGER of them because one number serves both:
#
#   * A WRITE TURN's workspace: `finish_turn_sandbox`'s recovery autosave
#     (`_RECOVERY_SNAPSHOT_TIMEOUT_SECONDS`, 60 s — the whole sequence under one bound), then
#     `_OUTCOME_WRITE_TIMEOUT_SECONDS` (10 s) for the record.
#   * A BUILD session: `_do_finalize` step 1 writes the SAVED snapshot, and a user stop reaches
#     it with `force_ended` false and nothing committed, so it runs in full. That write carries
#     no timeout of its own — bounding it is not an option, because cutting a snapshot short is
#     how a citizen's unsaved work disappears — so what bounds it is its parts:
#     `SNAPSHOT_EXECS` execs of `SNAPSHOT_EXEC_TIMEOUT_SECONDS` each, plus the same 10 s record.
#     An earlier version of this derivation named only the record and missed the snapshot
#     entirely, which put the budget an order of magnitude UNDER the branch it claimed to sit
#     above — the exact defect the 30 s had, reintroduced for the branch it was meant to fix.
#
# WHAT IS STILL NOT COVERED, said plainly rather than papered over: `write_snapshot` also takes a
# per-app lock and finishes with a blob PUT, and neither is bounded here. So this is the bound on
# the WORK, not a guarantee about the wall clock, and expiring it is deliberately not a verdict —
# `_stop_the_held_session` shields the end sequence and stops WAITING, and the status read goes on
# reading the session map. A stop that outlives this budget is still reported honestly.
#
# NOTHING HOLDS A REQUEST OPEN FOR THIS. Since the stop became an ask plus a status read
# (`request_stop_of_active_work` / `stop_state_of_active_work`), this bounds a detached task,
# not a connection — which is what makes a budget of minutes affordable at all.
_SNAPSHOT_WRITE_BUDGET_SECONDS: float = SNAPSHOT_EXECS * SNAPSHOT_EXEC_TIMEOUT_SECONDS

_STOP_ACTIVE_WORK_TIMEOUT_SECONDS: float = (
    max(_RECOVERY_SNAPSHOT_TIMEOUT_SECONDS, _SNAPSHOT_WRITE_BUDGET_SECONDS)
    + _OUTCOME_WRITE_TIMEOUT_SECONDS
)

# How long a settled stop record is kept so a status read can still tell "stopped" from "nothing
# was running". The same window ended sessions keep, and for the same reason: a client that lost
# its connection mid-stop comes back and asks again. Pruning past it can only ever turn one
# proceed-able answer (stopped) into the other (nothing was running) — never a false "stopped",
# and never a false permission.
_STOP_RECORD_RETENTION_SECONDS: float = _ENDED_RETENTION_SECONDS

_ATTACHED_READY_BUDGET_SECONDS: float = 15.0
_COLD_READY_BUDGET_SECONDS: float = 120.0

# How long one process stays quiet about a given user's unreadable preview-state reads. Sized
# against the POLL, not against the outage: the accelerated cadence is 3s per tab per surface, so
# anything shorter turns one blip into a page of identical warnings, and anything much longer
# would let a genuinely new outage go unmentioned. See `_say_the_preview_read_failed`.
_UNKNOWN_REPORT_SILENCE_SECONDS: float = 60.0

# WHICH DOOR into the one-per-user workspace a claim came through, for the claim log line. A
# closed Literal rather than a bare `str` so a typo cannot invent a fourth arm that no alert
# rule has ever heard of. `shared_launch` (#198) is the recipient's own door: a colleague's
# read-only view of a project shared with them, occupying the SAME per-user slot a build would.
_ClaimArm = Literal["relaunch", "ensure_sandbox", "shared_launch"]


# The end sequence's own DB session factory (it outlives the starting request). Typed as what this
# module actually DOES with it — call it, `async with` the result — rather than as
# `async_sessionmaker`, so a test can bind it to the rolled-back session with a plain
# context-manager factory (the real `async_sessionmaker` satisfies this by construction).
SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]


class BuildSessionConflictError(Exception):
    """The user already holds a live build session (the one-per-user lock is held).
    Carries the existing `session_id` so the router can surface it in the 409."""

    def __init__(self, session_id: uuid.UUID | None) -> None:
        super().__init__("a build session is already active")
        self.session_id = session_id


class SnapshotUnavailableError(Exception):
    """The restore path could not be completed and the snapshot is NOT confirmed absent:
    either the head-check never got an answer (a `StorageError` every attempt) or the bundle
    is known-present but its restore kept failing. Fail-closed by design (ambiguity denies):
    provisioning a fresh template here would be DESTRUCTIVE, not merely degraded, since
    `_do_finalize`'s step-1 snapshot would write the blank workspace OVER the user's good
    bundle, permanently. Aborting leaves the bundle byte-for-byte intact for the next start.
    Raised from `_resolve_sandbox`, landing inside `ensure_sandbox`'s compensation block (lock
    released, any container torn down); the router maps it to a 503."""

    def __init__(self, message: str, *, app_id: uuid.UUID) -> None:
        super().__init__(message)
        self.app_id = app_id


#: How the integrity gate tells the turn what it found, BEFORE it acts on it. A coroutine rather
#: than a return value because the sentence has to reach the citizen while the slow work runs.
RecoveryAnnouncer = Callable[["RecoveryNews"], Awaitable[None]]

#: Consecutive refusals after which the integrity gate stops trusting the recovery slot (see
#: `_source_that_is_not_poisoned`).
_POISONED_SLOT_REFUSALS: Final = 2


async def _say(announce: RecoveryAnnouncer | None, news: RecoveryNews) -> None:
    """Tell the turn, if anyone is listening. Callers without a turn (a relaunch, a test) pass
    `None`, and the gate still does its work — it simply says nothing."""
    if announce is not None:
        await announce(news)


@dataclass(frozen=True)
class _IdleCheck:
    """The last answer an idle tab got about one app, and when."""

    asked_at: datetime
    verdict: WorkspaceState


# HOW LONG ONE ANSWER STANDS FOR AN IDLE TAB.
#
# Without a window, a tab left open overnight is a container exec every 45 seconds — forever — for
# an answer that changes at most once. Sized well above the poll interval and well below any
# reasonable reading session: long enough that a tab cannot spin the container, short enough that a
# citizen who walks away and comes back learns the truth within a minute of looking again.
#
# Process-local, matching the single-replica deploy contract `reaper.py` already depends on. Not
# pruned on a timer: one small entry per app that has ever been idle-checked, overwritten in place.
_IDLE_CHECK_WINDOW: Final = timedelta(seconds=60)
_idle_checks: dict[uuid.UUID, _IdleCheck] = {}


def reset_idle_checks_for_tests() -> None:
    """Drop the per-app idle-check memo. Process-local, so a remembered answer must not leak into
    the next test and silently make its container call disappear."""
    _idle_checks.clear()


class _Quarantine(enum.StrEnum):
    """What happened to the tree the integrity gate was about to restore over."""

    WRITTEN = "written"
    #: Provably nothing in it — the baked template. Skipped on purpose; see the writer below.
    SKIPPED_AS_EMPTY = "skipped_as_empty"
    #: Could not be set aside. The restore does NOT proceed.
    FAILED = "failed"


class RecoveryNews(enum.StrEnum):
    """What the pre-turn integrity gate has to tell the citizen.

    A SMALL ENUM RATHER THAN THE SENTENCE ITSELF, because the sentences live in
    `services/turns/copy.py` and this module must not import them: `services.turns` reaches
    `build_sessions` and an import back would close the cycle. The manager knows what happened;
    the turn knows how to say it."""

    #: Confirmed loss, and a durable copy exists. Said BEFORE the restore runs — see
    #: `_still_theirs_or_put_it_back` for why the ordering is not cosmetic.
    RESTORING = "restoring"
    #: Confirmed loss and nothing to put back, or the restore itself failed. There is exactly one
    #: honest next action and the citizen has to be given it.
    UNRECOVERABLE = "unrecoverable"
    #: The check could not be answered and no retry will change that. The turn proceeds under
    #: alarm with one plain sentence; nothing is restored and nothing is destroyed.
    UNVERIFIED = "unverified"


class StopOutcome(enum.StrEnum):
    """What a stop actually achieved — three named states, never a boolean. `False` already
    meant "nothing was running" (proceed), so folding a timeout into it would pull a container
    out from under a task still writing to it — and the boolean this replaced hardcoded `True`
    on timeout too, wearing success's own face. Read from the source of truth (whether a
    session still holds the app), never inferred from elapsed time: a merely slow container
    has destroyed unsaved work here before. `STOPPED` / `NOTHING_WAS_RUNNING` — proceed.
    `STILL_RUNNING` — do NOT proceed; it is not a failure, read the state again in a moment."""

    STOPPED = "stopped"
    NOTHING_WAS_RUNNING = "nothing_was_running"
    STILL_RUNNING = "still_running"


@dataclass(frozen=True)
class _StopRecord:
    """One stop that was asked for: the app it is stopping and the detached task doing it.

    THE TASK REFERENCE IS LOAD-BEARING, not bookkeeping. Nothing else holds it, and a task the
    loop can garbage-collect is a stop that silently never happens — the same reason
    `SessionManager._tasks` holds its compensation tasks.

    `requested_at` is used for ONE thing: pruning a settled record. It is never consulted to
    decide whether the stop finished, which is read from the session map instead."""

    app_id: uuid.UUID
    task: asyncio.Task[StopOutcome]
    requested_at: datetime


def _log_a_stop_that_failed(
    task: asyncio.Task[StopOutcome],
    *,
    user_id: uuid.UUID,
    project_id: uuid.UUID,
    app_id: uuid.UUID,
) -> None:
    """A detached stop that raised, said out loud — and said whose.

    The status read does not depend on this: it reads the session map, so a crashed stop
    still reports honestly as "still running" while the session is held. But a stop that
    breaks is an operator's problem the moment it repeats, and an un-retrieved task exception
    otherwise surfaces only as a bare warning at collection time. A detached task inherits no
    structlog request scope, so the identifiers are passed in explicitly rather than read off
    the record — a lookup here would race the prune."""
    if task.cancelled():
        return
    failure = task.exception()
    if failure is not None:
        _log.error(
            "stop of active work failed",
            exc_info=failure,
            user_id=str(user_id),
            project_id=str(project_id),
            app_id=str(app_id),
        )


class WorkspaceUnreadableError(Exception):
    """The integrity gate could not reach the container to ask whether it still holds the app.

    RETRYABLE, and deliberately NOT a verdict: `_resolve_sandbox` raises rather than
    proceeding, since proceeding would let the agent build on a workspace nobody has checked
    — the exact failure this gate exists to prevent — and the alternative (telling a user to
    retry) is one they can act on. The container is left running, attached and untouched, so
    the retry has something to attach to; `integrity.py`'s streak cap is what stops this
    repeating forever."""

    def __init__(self, message: str, *, app_id: uuid.UUID) -> None:
        super().__init__(message)
        self.app_id = app_id


class NoSnapshotToRelaunchError(Exception):
    """Relaunch found no saved snapshot to restore: the project was never built, or its
    bundle is CONFIRMED absent. Distinct from `SnapshotUnavailableError` (transient/unknown →
    503): this is a definite "nothing to relaunch", which the router maps to a 404. Unlike a
    build start, relaunch has NO fresh-provision fallback — a blank template is not a preview
    of the user's app — so a confirmed-absent bundle is a dead end, not a blank start."""

    def __init__(self, app_id: uuid.UUID) -> None:
        super().__init__("no saved build to relaunch")
        self.app_id = app_id


class SandboxReclaimBlockedError(Exception):
    """Another project holds this user's one sandbox slot and its workspace has unsaved work,
    so taking the slot would silently destroy it. Reclaiming is not wrong — doing it silently
    is: the slot is per-user, so something must give it up, but a citizen who loses work must
    be told first. The router turns this into a 409 naming the occupying project; the client
    offers a choice, and `release_project_sandbox` is the only thing that actually destroys a
    container. A clean incumbent never raises this — nothing to lose, so the reclaim stays
    silent and costs nothing. This is the missing telling, not a new save policy: nothing
    here writes a snapshot."""

    def __init__(
        self,
        *,
        project_id: uuid.UUID,
        project_name: str,
        app_id: uuid.UUID,
        # None is UNKNOWN and still blocks: a container we could reach but could not question,
        # or one we could not reach at all (`SandboxUnreachableError`), is not evidence of a
        # clean tree — guessing "clean" is the one guess that loses work.
        dirty: bool | None,
        # A build in progress, not just a dirty tree — a different refusal wearing the same
        # envelope. "has unsaved changes" is the wrong copy for it: there is no settled tree to
        # describe (`dirty` is deliberately not probed mid-build; git status against a container
        # the agent is writing into produced a half-written snapshot in testing), and
        # `release_project_sandbox` refuses while a live session owns the container, so the
        # build must be STOPPED first — a separate act with its own cost. The client needs a
        # third choice because of it: stop-and-save, stop-and-discard, or leave it running.
        building: bool = False,
        # A broader, separate signal from `building`, kept apart on purpose: whether the OTHER
        # project's agent is mid-thought at all, which a Plan/Ask turn is exactly as much as a
        # build is (`building` only covers write-capable turns). Folding this into `building`
        # once put a hammer icon and two Stop buttons in front of a citizen who had only asked
        # a question. `building` decides WHICH dialog; `agent_working` decides what it says is
        # happening right now.
        agent_working: bool = False,
        # WHICH REMEDY ACTUALLY WORKS (#198). `project_id`/`project_name` above name a project
        # the citizen owns for a `sbx-` occupant — `stopActiveBuild`/`release` both gate on
        # `owned_project_or_404`, which that citizen satisfies. For a `shr-` occupant the id
        # named is the SHARED PROJECT'S OWNER, which the caller (a recipient) never owns — the
        # same two routes would 404 them out of their own slot. `is_shared_view=True` is the
        # client's one signal to route to the self-scoped give-up-my-shared-view endpoint
        # instead, which needs no project id or ownership check at all.
        is_shared_view: bool = False,
    ) -> None:
        super().__init__("another project is holding the sandbox")
        self.project_id = project_id
        self.project_name = project_name
        self.app_id = app_id
        self.dirty = dirty
        self.building = building
        self.agent_working = agent_working
        self.is_shared_view = is_shared_view


@dataclass(frozen=True)
class SaveOutcome:
    """What a successful Save tells the client: the app it saved and the commit it saved AT,
    so the dirty indicator settles without a second round trip."""

    app_id: uuid.UUID
    head_sha: str | None


@dataclass(frozen=True)
class SaveState:
    """Is there unsaved work? `dirty=None` is UNKNOWN and is NOT False — no live container, or
    a store we could not read. Rendering unknown as clean tells a user their work is safe when
    nobody actually checked."""

    app_id: uuid.UUID | None
    dirty: bool | None
    container_head: str | None
    saved_head: str | None
    # When the platform last wrote this app's tree to the recovery slot, or None if it never
    # has. Distinct from `saved_head`, which is the user's own save: this exists so a workspace
    # that was reclaimed while dirty can be OFFERED back ("unsaved work from 14:32") rather than
    # silently forgotten.
    #
    # AND IT IS ALSO THE ANSWER TO "CAN THE PLATFORM PUT THIS BACK?", which is a stronger fact
    # than the clause that used to end this comment. It said the restore ladder still only ever
    # restores the user's bundle; that stopped being true when `newest_restore_source` landed,
    # 1,168 lines below in this same module. EVERY automatic restore goes through it now, and it
    # hands back the RECOVERY bundle in preference to the saved one whenever the recovery copy is
    # the newer of the two — deliberately, to close the data-loss bug its own docstring
    # describes. Where it does hand back the saved bundle, that bundle is either newer or holds
    # the same tree, so a non-null instant here means one thing either way: what a restore brings
    # back is no older than this. `restorable_presence` counts this slot for the same reason, and
    # `SaveStateResponse.recovery_at` carries the fact — and this reasoning — onto the wire, where
    # the rail and the exit guard read it to stop calling a fresh build's `dirty=True` a warning.
    #
    # RESUMPTION, NOT PROMOTION — the half of the old comment that was true stands unchanged. A
    # recovery copy is not a VERSION: `snapshot_key` is untouched, `dirty` stays True beside a
    # non-null instant, and nothing on this path performs the citizen's Save for them. Save stays
    # MANUAL, and only Save produces something they can ask to come back to by name.
    recovery_at: datetime | None = None


@dataclass(frozen=True)
class PreviewState:
    """What is (or is not) serving this project right now. `alive` is DERIVED rather than
    stored: as a field, `False` meant "never built" and "another project took the slot" and
    "asleep" and "the registry read threw" indistinguishably; as a property it can only ever
    mean `state is ALIVE`. `PreviewLifeState.STARTING` needs no field of its own: a start in
    flight names no preview URL and offers no restore, so the existing defaults
    (`preview_url=None`, `restorable=None`) are already right — `state` alone carries the
    new fact."""

    state: PreviewLifeState
    preview_url: str | None = None
    # DIAGNOSTIC ONLY, and populated on the ALIVE arm alone: the instant the serving proof was
    # stamped. NOTHING HERE OR ON THE WIRE MAY BRANCH ON IT — it is the same fact as `state`
    # spelled a second time from one read, and a client computing liveness from it would be one
    # refactor away from disagreeing with `state`. It exists so an operator can join a
    # screenshot to the `app_first_served` log line and read WHEN, which is precisely the
    # question nobody could answer about 2026-09-10. `None` on a pre-cutover hash: proven, with
    # no instant to name. `PreviewStateResponse.serving_since` carries the same rule.
    serving_since: datetime | None = None
    # SLOT_TAKEN only — whose work is in the container standing where this project's was. Also
    # populated directly from the starting marker's own payload (no registry round trip needed
    # to name the occupier) when a start, rather than a live container, is what is holding the
    # slot.
    occupying_project_id: uuid.UUID | None = None
    occupying_project_name: str | None = None
    # TRI-STATE (`restorable_presence`), and `None` is NO CLAIM rather than "no": either the
    # object store was unreachable, or nothing on screen for this state could use the answer
    # (the alive path, which declines to spend a Blob round trip per poll on a question about
    # an app that is currently running). Both readings are the same instruction to the client —
    # believe nothing from this field, fall back to what you already knew.
    restorable: bool | None = None

    @property
    def alive(self) -> bool:
        """Strictly `state is ALIVE`. Retained on the wire for the rollout window — a browser
        tab loaded before this change is still reading it, and a tab that read a missing field
        as `false` would paint "gone" over a live preview. New clients read `state`."""
        return self.state is PreviewLifeState.ALIVE


def _head_of(meta: ObjectMeta | None) -> str | None:
    """The tree a stored bundle holds, from the metadata `write_snapshot` stamps on it.

    None for a bundle written before that stamp existed, which is why every caller treats a
    missing value as "cannot compare" and falls back to timestamps rather than to equality."""
    if meta is None or not meta.metadata:
        return None
    value = meta.metadata.get("head_sha")
    return value if isinstance(value, str) else None


@dataclass(frozen=True)
class RecoverableWork:
    """A crash-recovery copy that is NEWER than the user's last saved version.

    Returned only when a copy exists and post-dates the saved one, so a caller can offer it
    rather than silently restoring the older saved bundle and presenting the app as healthy —
    the failure mode that made losing a container invisible. `written_at` is the store's own
    `last_modified`, which is what makes "newer" answerable at all: two bundle HEAD shas tell
    you nothing about which came first. It is the value shown to the user ("your work from
    14:47"), so it must be the write time, never `now`."""

    app_id: uuid.UUID
    written_at: datetime


class NoLiveSandboxError(Exception):
    """There is no container to read or save from. Raised rather than returning a falsy
    success, so a Save can never report having stored work it did not."""

    def __init__(self, subject: uuid.UUID) -> None:
        super().__init__(f"no live sandbox for {subject}")
        self.subject = subject


class SandboxUnreachableError(NoLiveSandboxError):
    """The registry still names this app, but the container would not answer. A subclass on
    purpose: every existing catcher still gets the parent's meaning ("no handle, cannot read
    or save"); the split adds a second question — why there is no handle. The parent means
    CERTAIN ABSENCE (registry says nothing of this app is live); this means UNKNOWN (registry
    says it IS live and the attach failed anyway — a cold container, a timeout, or a blip
    against one that may be alive, holding unsaved work). `_refuse_if_reclaim_would_destroy_work`
    treats the first as safe to reclaim, the second as a refusal — collapsing them risks the
    same silent destruction, just rarer."""


async def _existing_app_id(
    db: AsyncSession, user_id: uuid.UUID, project_id: uuid.UUID
) -> uuid.UUID | None:
    """The project's app id WITHOUT minting one (`resolve_app_for_project` upserts)."""
    app_id: uuid.UUID | None = await db.scalar(
        sa.select(AppRegistry.id).where(
            AppRegistry.project_id == project_id, AppRegistry.user_id == user_id
        )
    )
    return app_id


@dataclass(frozen=True)
class _OccupyingProject:
    """Whose work is in the container currently holding this user's slot."""

    app_id: uuid.UUID
    project_id: uuid.UUID
    project_name: str


async def _occupying_project(
    db: AsyncSession, user_id: uuid.UUID, app_name: str
) -> _OccupyingProject | None:
    """Resolve a live container's registry name back to the project whose work is inside it.
    `app_name_for` keeps 28 of the app_id's 32 hex chars, so it is NOT invertible: every
    consumer compares FORWARD instead, reading the user's few app rows (one per project,
    indexed by `user_id`) and re-deriving the name for each. A lossy inverse would be a silent
    mis-attribution — naming the wrong project is worse than naming none. `None` means the
    container cannot be attributed to any app this user owns (a genuine ghost, which the
    reconcile below is for) — callers must fall through, never refuse on it."""
    rows = (
        await db.execute(
            sa.select(AppRegistry.id, AppRegistry.project_id, Project.name)
            .join(Project, Project.id == AppRegistry.project_id)
            .where(AppRegistry.user_id == user_id)
        )
    ).all()
    for app_id, project_id, project_name in rows:
        if app_name_for(app_id) == app_name:
            return _OccupyingProject(
                app_id=app_id, project_id=project_id, project_name=project_name
            )
    return None


async def _occupying_shared_project(
    db: AsyncSession, reg: dict[str, str]
) -> _OccupyingProject | None:
    """The `shr-` counterpart of `_occupying_project` above — and the reason it can be a
    plain lookup rather than another forward-match loop (#198, requirement 24).

    `_occupying_project` exists ONLY because `app_name_for` cannot be reverse-parsed, so a
    caller's own app rows must be re-derived and matched forward one at a time. `shr_name_for`
    is exactly as lossy, but a `shr-` occupant's slot is stamped with
    `REGISTRY_FIELD_SHARED_PROJECT_ID`/`REGISTRY_FIELD_SHARED_OWNER_ID` at Launch
    (`launch_shared_preview`) precisely so this never needs to guess: the identity is read
    straight off the hash, not re-derived from a name.

    Called BEFORE `_occupying_project`, not after — a `shr-` occupant belongs to the
    PROJECT'S OWNER, who is almost never the caller (`user_id` in `_occupying_project`'s own
    query), so the forward-match loop there would search the wrong person's app rows and
    always miss. Absent fields (an ordinary build sandbox) or a project since deleted both
    return `None` — the second is the identical 'ghost' reading `_occupying_project` gives a
    dangling registry entry: nothing left to warn about, so the caller falls through and
    reclaims silently."""
    project_id_raw = reg.get(REGISTRY_FIELD_SHARED_PROJECT_ID)
    owner_id_raw = reg.get(REGISTRY_FIELD_SHARED_OWNER_ID)
    if not project_id_raw or not owner_id_raw:
        return None
    project_id = uuid.UUID(project_id_raw)
    owner_id = uuid.UUID(owner_id_raw)
    project_name = await db.scalar(sa.select(Project.name).where(Project.id == project_id))
    if project_name is None:
        return None
    app_id = await _existing_app_id(db, owner_id, project_id)
    if app_id is None:
        return None
    return _OccupyingProject(app_id=app_id, project_id=project_id, project_name=project_name)


async def _project_name_owned_by(
    db: AsyncSession, user_id: uuid.UUID, project_id: uuid.UUID
) -> str | None:
    """The name of a project this user owns, or `None` when it does not exist (or is not
    theirs) — the starting marker's direct counterpart to `_occupying_project` above.

    `_occupying_project` exists ONLY because a registry hash cannot say which project a
    container belongs to and must invert an app NAME back to one. The starting marker
    carries the project id outright, so the SLOT_TAKEN answer it feeds needs no inversion and
    no ghost case — a marker that named a project the caller does not own could only mean the
    marker itself is corrupt, which is exactly the `None` this returns."""
    name: str | None = await db.scalar(
        sa.select(Project.name).where(Project.id == project_id, Project.user_id == user_id)
    )
    return name


async def _saved_head(app_id: uuid.UUID) -> str | None:
    """The commit the saved bundle is at, or None when nothing was ever saved."""
    try:
        data = await get_storage().get(snapshot_key(app_id))
    except StorageError:
        return None
    try:
        head: str | None = parse_bundle_head_sha(data)
    except BundleValidationError:
        # A bundle we cannot parse cannot be compared. "Unknown", never "matches" — the
        # latter would tell a user with unsaved work that everything was already saved.
        return None
    return head


async def _recovery_written_at(app_id: uuid.UUID) -> datetime | None:
    """When the platform last autosaved this app, or None if it never has / we cannot tell.

    Unknown and never collapse into "there is nothing" — but here they are the same ANSWER,
    because this only ever adds an offer. A recovery bundle we cannot see is one we do not
    mention; nothing is claimed either way, and the user's saved version is untouched."""
    try:
        meta = await get_storage().head(recovery_key(app_id))
    except StorageError, StorageUnconfiguredError:
        return None
    return meta.last_modified if meta else None


async def _snapshot_written_at(app_id: uuid.UUID) -> datetime | None:
    """When the app's SAVED snapshot was last written (#198) — best-effort, for
    `SharedPreview`'s "as of" line, `_recovery_written_at`'s own sibling. `None` on any failure
    to ask, including a confirmed-absent snapshot: by the time a caller wants this, the restore
    it feeds into has ALREADY confirmed presence via `snapshot_exists_or_bust`, so a `None`
    here is a missing DETAIL, never a missing snapshot."""
    try:
        meta = await get_storage().head(snapshot_key(app_id))
    except StorageError, StorageUnconfiguredError:
        return None
    return meta.last_modified if meta else None


async def _sandbox_name_for_existing_app(
    db: AsyncSession, user_id: uuid.UUID, project_id: uuid.UUID
) -> str | None:
    """The container name this project's sandbox would carry — WITHOUT minting an app row.

    `resolve_app_for_project` upserts, and a read that mints is a read that leaves a DRAFT row
    behind every time a turn is refused. None means the project has never been built, so there
    is nothing live that could belong to it."""
    app_id = await _existing_app_id(db, user_id, project_id)
    return app_name_for(app_id) if app_id is not None else None


async def _the_live_sandbox_is_already_the_one_we_want(
    redis: aioredis.Redis, user_id: uuid.UUID, spare_app: str | None
) -> bool:
    """Is the container already up the very one this caller is about to ask for? The point is
    to NOT destroy a healthy container: the reconcile below exists to clear a GHOST left by a
    crashed run (the registry maps one user to one container, so a new one would silently
    orphan it forever), but write is a chat mode now — every message allocates — and running
    that rule on every message tore a perfectly good, already-running container down and
    rebuilt it from the snapshot each time. So: ask first via one hash read (same app, READY
    → attach); anything else falls through to the reconcile unchanged. `spare_app=None` means
    no claim to make, and fails toward that old, safe behaviour (`False`)."""
    if spare_app is None:
        return False
    try:
        reg = await read_registry(redis, user_id)
    except Exception:
        # A Redis blip is not a licence to spare a container we cannot identify. Fall through
        # to the reconcile, which is the behaviour that was correct before this optimisation.
        return False
    if reg is None:
        return False
    return _registry_serves_and_is_ready(reg, spare_app)


def _registry_serves_and_is_ready(reg: dict[str, str], app_name: str) -> bool:
    """Does this registry hash say a READY container is serving `app_name`? Factored out so
    two callers cannot drift: the start path's "spare or reclaim?" and
    `project_preview_state`'s "is my preview live?" share this comparison while differing on
    the error arm (the poll must not swallow a Redis failure into `False`) — two hand-written
    copies would answer differently the first time one was updated alone. READY only: a
    registry marked `ending` is a container the reaper has already committed to destroying,
    and attaching to it would race the teardown while also skipping the cleanup."""
    return (
        reg.get(REGISTRY_FIELD_APP_NAME) == app_name
        and reg.get(REGISTRY_FIELD_STATE) == REGISTRY_STATE_READY
    )


# WHICH watcher won the race to see the app answer. Only the two this module can emit are
# spelled here; `alarms.APP_FIRST_SERVED_EVENT` holds the whole vocabulary, the rest of which
# belongs to the turn watcher and the reconciler. A closed Literal so a typo cannot mint an
# observer that never existed.
_ServingObserver = Literal["relaunch_wait", "relaunch_continuation"]


async def _record_the_first_serve(
    redis: aioredis.Redis,
    user_id: uuid.UUID,
    *,
    app_name: str,
    observer: _ServingObserver,
    cold: bool,
) -> None:
    """Something watched this container's app ANSWER a request — write the proof down and say
    so. NEVER RAISES: this is bookkeeping behind a container that is already up and already
    handed to the citizen, exactly like the counters beside it, and a coordination-store blip
    must not turn a working relaunch into a 500.

    THE REFUSAL IS THE INTERESTING CASE. `mark_serving` answering False carries two situations
    that only a caller can tell apart, so the discrimination is made here, once: a hash that
    still names this app and already holds an instant is a SECOND SIGHTING — first serve wins,
    nothing to say — while a hash that is gone, marked `ending`, or naming another container is
    the near-miss the design is most afraid of, and it gets `SERVING_PROOF_STAMP_REFUSED`."""
    when = datetime.now(UTC)
    try:
        stamped = await mark_serving(redis, user_id, app_name=app_name, when=when)
        # Re-read either way: on a stamp it is where `created_at` comes from (so the operator
        # is handed the window rather than two timestamps to subtract), and on a refusal it is
        # the only thing that can say WHICH refusal this was.
        reg = await read_registry(redis, user_id)
    except RedisError:
        _log.exception(
            "serving proof could not be recorded; the reconciler's sweep is the backstop",
            user_id=str(user_id),
            app_name=app_name,
        )
        return
    if stamped:
        _log.info(
            APP_FIRST_SERVED_EVENT,
            app_name=app_name,
            serving_since=when.isoformat(),
            ms_since_container_created=elapsed_ms(
                an_instant_on_the_hash(reg, REGISTRY_FIELD_CREATED_AT), when
            ),
            observer=observer,
            cold=cold,
        )
        return
    if reg is not None and _registry_serves_and_is_ready(reg, app_name) and stamp_is_proven(reg):
        return  # a second sighting of a container that already carries its proof
    _log.warning(
        SERVING_PROOF_STAMP_REFUSED,
        expected_app=app_name,
        # A BOOL, and never the other project's container name: the id vocabulary in this log
        # stays user-scoped, and naming the loser would put one of the citizen's projects into
        # another's build trace.
        found_app_present=bool(reg and reg.get(REGISTRY_FIELD_APP_NAME)),
    )


async def _last_supervisor_reading(
    sandbox_client: SandboxClient, handle: SandboxHandle
) -> tuple[bool | None, str]:
    """The supervisor's final word on a wait that ended with no proof, for the give-up line.

    THIS IS WHAT SEPARATES THE THREE ANSWERS an operator actually needs: a dev server that never
    started, one that started and never finished compiling, and one that compiled and still
    would not answer. Without it `app_first_serve_not_observed` says only "it did not work".

    NEVER RAISES — it runs on a path that has already given up, so a supervisor that will not
    answer this either simply leaves `None`/`unknown`, which is itself a reading."""
    running: bool | None = None
    with suppress(SandboxError):
        running = (await sandbox_client.dev_status(handle)).running
    # `compile_state` guarantees it never raises (a pre-endpoint image, a transport failure and
    # a malformed body all land on UNKNOWN), so it needs no guard of its own.
    return running, (await sandbox_client.compile_state(handle)).state.value


@contextmanager
def _one_relaunch_in_the_log(**fields: str) -> Iterator[None]:
    """Bind this relaunch's correlation ids for every line written inside, then put the context
    back exactly as it was found.

    THE RESET IS NOT OPTIONAL even though a request runs in its own task: the same worker task
    can be reused by whatever this process does next, and a `build_id` left bound would sign
    somebody else's lines with this relaunch's identity — a correlation id that lies is worse
    than none, because it is believed. Detached tasks created INSIDE are unaffected by the
    reset: asyncio copied the context when they were created, which is precisely why the
    continuation inherits these ids and keeps them after this returns."""
    tokens = structlog.contextvars.bind_contextvars(**fields)
    try:
        yield
    finally:
        structlog.contextvars.reset_contextvars(**tokens)


def app_name_for(app_id: uuid.UUID) -> str:
    """An ACA-compliant container name (2–32 chars, lowercase alphanumeric/hyphen,
    letter-first, ends alphanumeric), stable per app: `sbx-` + 28 hex chars of the
    app_id (`str(app_id)` is an invalid ACA name — dots/length; the hex slug is safe)."""
    return f"{SANDBOX_NAME_PREFIX}{app_id.hex[:28]}"


def shr_name_for(app_id: uuid.UUID, recipient_id: uuid.UUID) -> str:
    """An ACA-compliant container name for a SHARED-RUNTIME sandbox (#198), stable per
    (app, recipient) PAIR: `shr-` + 28 hex chars of a SHA-256 digest of both ids.

    A hash, not a slice, unlike `app_name_for` — the same app shared with two colleagues must
    mint two different containers, one per recipient's own restricted view, and 28 hex
    characters is not room enough to losslessly encode two 128-bit UUIDs. FORWARD-MATCH-ONLY
    like its two siblings (`app_name_for`, `deploy.names.published_app_name`): nothing may
    ever reverse-parse an app id or a recipient id back out of this name; both are carried
    losslessly instead on the container's own ARM tags (`shared_sandbox_tags`)."""
    digest = hashlib.sha256(f"{app_id}:{recipient_id}".encode()).hexdigest()
    return f"{SHARED_SANDBOX_NAME_PREFIX}{digest[:28]}"


@dataclass(frozen=True)
class RelaunchedPreview:
    """What `relaunch_preview` hands the router: the durable app id, the live (READY) preview
    URL, and whether the newest recorded build outcome for the project was FAILED — in which
    case the restored snapshot is the last SAVED state, not that build's intent, and the
    portal labels it "last saved version".

    That last flag is a claim about a RESTORE and is false by construction on the attach arm:
    a relaunch that attached to a live container restored nothing, and the tree it hands back
    may be newer than any snapshot."""

    app_id: uuid.UUID
    preview_url: str
    restored_from_failed_build: bool
    ready: bool
    """Whether the readiness wait actually confirmed the app answering (`shows_a_page`), not
    merely that a container exists. Pre-existing field the router (`RelaunchPreviewResponse`)
    and this module's own `_relaunch_under_one_build_id` already read/write — the dataclass
    itself was simply missing it (found while adding `SharedPreview`, its sibling, immediately
    below; unrelated to #198, fixed here rather than left red for every future mypy run)."""


@dataclass(frozen=True)
class SharedPreview:
    """What `launch_shared_preview` hands the router: the OWNER's durable app id (never the
    recipient's — a shared view mints no app row of its own), the framable preview URL, whether
    it is actually SERVING yet, and when the snapshot being served was taken.

    `ready` mirrors `RelaunchedPreview`'s own field and for the identical reason: `preview_url`
    is always framable, but an attached container whose readiness wait timed out hands back
    `ready=False` rather than a 503 — the alternative, condemning the container, would cost a
    colleague's view of it for a slow root route that may simply need a moment. `snapshot_taken_at`
    is `None` only when the store could not be asked for the timestamp — the restore itself
    already confirmed the snapshot's PRESENCE before this dataclass is ever built, so a `None`
    here is a missing detail, never a missing snapshot."""

    app_id: uuid.UUID
    preview_url: str
    ready: bool
    snapshot_taken_at: datetime | None


class SharedProjectHasNoAppError(Exception):
    """The shared project's owner has no app row at all — UNREACHABLE in practice: `create_share`
    (#198 slice 1) refuses to create a share unless the owner's app exists with a saved
    snapshot, and deleting the project cascades the share away with it, so a live share always
    implies a live app row for its owner. Defensive only; the router maps it to the same 404
    `NoSnapshotToRelaunchError` gets — from the recipient's side, "nothing to launch" reads
    identically whichever of the two facts is missing."""

    def __init__(self, project_id: uuid.UUID) -> None:
        super().__init__("shared project's owner has no app to launch")
        self.project_id = project_id

    # Is the dev server actually SERVING this URL yet? False on either reading that says the URL
    # is framable with nothing painting behind it: the attach arm's readiness wait lapsing
    # (`_ATTACHED_READY_BUDGET_SECONDS` elapsed with the app still not answering), or the app root
    # answering with something that is not a page — a 404 while the agent has yet to write
    # `app/page.tsx`, the measured blank-white-pane defect — which happens on EITHER arm. The URL
    # is framable either way; this says whether framing it will paint or wait.
    #
    # NOT THE SAME FACT AS A PROVEN `serving_since` STAMP, and the single case that separates them
    # is stated at the stamp site so the two cannot drift silently: when the supervisor cannot be
    # asked whether the app is showing a page, this stays True — declining to demote a preview
    # that may be painting — while the stamp is withheld, because a transport error is not a
    # sighting. On every other arm they are set together or withheld together.
    ready: bool = True


@dataclass(frozen=True)
class _ResolvedSandbox:
    """What `_resolve_sandbox` hands back: the handle, and WHICH ARM produced it. The flag is
    not diagnostics — it decides whether compensation may tear the container down. Two of the
    three arms CREATE a container, so a later failure is this request's to roll back; the
    third ATTACHES to one already up and serving, and rolling that back would destroy a
    healthy container over a failure unrelated to it (see `_LockScope.spared`). Returned as a
    value rather than left to each caller to infer: a bare `SandboxHandle` looks identical
    either way."""

    handle: SandboxHandle
    attached: bool
    #: What the pre-turn integrity gate found, or `None` when it had nothing to say. The
    #: turn reads it to decide what to tell the citizen and whether to run the agent at all.
    news: RecoveryNews | None = None
    #: Whether this turn actually put an older tree back. `True` is what holds the in-flight
    #: message: the instruction the user typed was written against a workspace that no longer
    #: exists.
    restored: bool = False


@dataclass
class _LockScope:
    """The mutable state a `_holding_user_lock` body shares with its compensation: the held
    token, any container the body created (torn down if the body fails), and whether the body
    ADOPTED the lock+container (a start's session takes ownership, so a clean exit must not
    release them). `spared` is a separate escape, answering not "who owns this now" but "would
    destroying it be a rollback at all" — set when the container was ATTACHED (already running
    before this request) or READIED (`wait_ready` returned, so it is up and serving even if
    this request created it, and tearing a working preview down over a Redis heartbeat blip
    is not a fair trade)."""

    token: str
    handle: SandboxHandle | None = None
    adopted: bool = False
    spared: bool = False

    def adopt(self) -> None:
        self.adopted = True

    def spare(self) -> None:
        self.spared = True

    def take(self, resolved: _ResolvedSandbox) -> SandboxHandle:
        """Record what `_resolve_sandbox` produced, SPARING the container when it was merely
        attached to. Returns the handle so the caller can bind it in one line.

        The point is that the caller cannot get this wrong: `_resolve_sandbox` reaches its
        attach three calls deep, so nothing at the call site looks like "you are borrowing
        this" — which is exactly how `ensure_sandbox` once assigned `scope.handle` with no
        `spare()` while `relaunch_preview`, whose attach is visible inline, had one. Every
        future caller inherits the right answer instead of having to know it."""
        self.handle = resolved.handle
        if resolved.attached:
            self.spare()
        return resolved.handle


@dataclass
class BuildSession:
    """One in-flight build. Held only in memory — never persisted."""

    session_id: uuid.UUID
    user_id: uuid.UUID
    project_id: uuid.UUID
    app_id: uuid.UUID
    prompt: str
    lock_token: str
    handle: SandboxHandle
    # MAY THIS SESSION'S TURN MUTATE THE TREE? Structural, not observational: it comes from the
    # mode's toolset, which is decided before the run starts and cannot change during it.
    # `toolsets_for_kind` gives a Plan chat a `read_only_toolset` and ONLY a Build chat the
    # `sandbox_toolset` that carries `write_file` / `edit_file` / `insert_lines`, so a
    # non-writing session can never touch the workspace no matter how long it runs.
    #
    # It exists because "a session is attached" is the wrong question for two callers. Every
    # mode pins the container (`_pin_workspace` attaches for Ask and Plan as well), so
    # attachment alone says nothing about whether an agent is writing — and answering the
    # broader question made a read-only Ask turn report "your app is still being built",
    # refuse the ordinary Save button, and bypass `_nothing_to_lose`. Defaults to False:
    # a session that never said it writes is not treated as one that does, and the two
    # affected callers both fail toward LESS interruption rather than more.
    may_write: bool = False
    # What the pre-turn integrity gate found, and whether it restored. Both are facts about
    # THIS turn's attach, not about the app, so they live on the session rather than anywhere
    # durable: the next message asks the question again.
    news: RecoveryNews | None = None
    restored: bool = False
    # The thread this build belonged to, so `_do_finalize` could record the outcome in it.
    # ALWAYS `None` NOW: `_start_locked` was the one place that ever set it, and it is
    # deleted. `ensure_sandbox` builds its session without it, which is why
    # `live_session_for_conversation` can no longer match anything — see that method.
    conversation_id: uuid.UUID | None = None
    # The thread's high-water seq the moment a build STARTED — recorded on the outcome row as
    # `startedSeq` so the NEXT build could tell the turns that arrived while this one ran from
    # the ones it already consumed. ALWAYS `None` NOW, for the same reason as
    # `conversation_id`: only the deleted start path captured it. `_record_outcome` still reads
    # it and still passes it on, so a row written today simply carries no marker.
    started_seq: int | None = None
    #: WHICH ARM produced the container, forwarded from `_ResolvedSandbox`. `True` means it was
    #: already up and serving and this turn merely joined it; `False` means this turn BROUGHT
    #: ONE UP — a fresh provision or a restore. Without it, "did this turn start anything?" is
    #: unanswerable at the turn seam: `restored=False` covers both a fresh provision and a plain
    #: attach, and counting every attach as a start would make a start-ratio metric's
    #: denominator "turns" rather than "starts", reading flatteringly close to 1.
    attached: bool = False
    status: BuildSessionStatus = BuildSessionStatus.PROVISIONING
    last_seq: int = 0
    preview_url: str | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    updated_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    task: asyncio.Task[None] | None = None
    # The replay buffer (every emitted envelope) + one queue per live SSE connection.
    envelopes: list[ProgressEnvelope] = field(default_factory=list)
    subscribers: set[asyncio.Queue[ProgressEnvelope]] = field(default_factory=set)
    end_reason: str | None = None
    force_ended: bool = False
    terminal_committed: bool = False
    terminal_emitted: bool = False
    snapshot_committed: bool = False
    # The single shielded end-sequence task (created by the first _finalize caller); every
    # caller awaits it, so a caller's own cancellation can't tear the sequence in half.
    finalize_task: asyncio.Task[None] | None = None
    # Stamped when the end sequence completes — starts the retention window after which the
    # session (and its envelope buffer) is evicted from the manager.
    ended_at: datetime | None = None


class SessionManager:
    """The in-process session registry + lifecycle. Held as a module singleton with an
    accessor (mirrors `get_redis`), overridable in tests."""

    def __init__(self, session_factory: SessionFactory | None = None) -> None:
        # The end sequence outlives the request that started the build, so it cannot borrow the
        # request's session — it opens its OWN, the same discipline the turn engine's billing
        # follows. Injectable so tests bind it to their rolled-back session instead of
        # committing to the real database.
        self._session_factory: SessionFactory = session_factory or async_session_factory
        self._sessions: dict[uuid.UUID, BuildSession] = {}
        self._active_by_user: dict[uuid.UUID, uuid.UUID] = {}
        # Strong refs to detached background tasks so the loop cannot garbage-collect one
        # mid-flight. It held the `run_build` tasks too until the build path was deleted; the
        # compensation teardowns in `_holding_user_lock` are what live here now.
        self._tasks: set[asyncio.Task[None]] = set()
        # One serialization lock per user, held across the WHOLE of start() — closes the
        # window where a concurrent same-user start would reconcile-away the first start's
        # in-flight lock (held but registry not yet written) and double-allocate a sandbox.
        self._start_locks: dict[uuid.UUID, asyncio.Lock] = {}
        # The stops that have been ASKED FOR, per (user, project) — a strong ref to each
        # detached stop task, and the memory that lets the status read tell "stopped" from
        # "nothing was running" once the work is gone. Pruned lazily; see
        # `_prune_settled_stop_records`.
        self._stop_records: dict[tuple[uuid.UUID, uuid.UUID], _StopRecord] = {}
        # When this process last said out loud that a user's preview-state read failed, so it
        # says it once per outage rather than once per poll of every open tab. Monotonic, not
        # wall-clock: a clock step must not unmute or mute the warning. Pruned on every use —
        # see `_say_the_preview_read_failed`.
        self._unknown_reported_at: dict[uuid.UUID, float] = {}

    def _start_lock_for(self, user_id: uuid.UUID) -> asyncio.Lock:
        lock = self._start_locks.get(user_id)
        if lock is None:
            lock = asyncio.Lock()
            self._start_locks[user_id] = lock
        return lock

    def _maybe_prune_start_lock(self, user_id: uuid.UUID) -> None:
        """Evict the per-user start lock once no live session remains — bounding the
        otherwise-unbounded `_start_locks` growth. Skipped when a concurrent start currently
        HOLDS the lock: that start owns the exact `Lock` object, so dropping it would let the
        next start build a fresh one and shatter mutual exclusion. This addresses only the
        start-lock leak; the `_sessions`/envelope retention window is a separate decision."""
        if user_id in self._active_by_user:
            return
        lock = self._start_locks.get(user_id)
        if lock is not None and not lock.locked():
            self._start_locks.pop(user_id, None)

    def evict_ended_sessions(self, *, now: datetime | None = None) -> int:
        """Drop every session whose `ended_at` is past the retention window, with its
        envelope buffer and any consistent `_active_by_user`/`_start_locks` entries.
        Called opportunistically (start + the internal reap sweep) — a session inside the
        window is KEPT so a late SSE reconnect can still replay + `[DONE]`."""
        now = now or datetime.now(UTC)
        evicted = 0
        for session_id, session in list(self._sessions.items()):
            if session.ended_at is None:
                continue
            if (now - session.ended_at).total_seconds() < _ENDED_RETENTION_SECONDS:
                continue
            self._sessions.pop(session_id, None)
            if self._active_by_user.get(session.user_id) == session_id:
                self._active_by_user.pop(session.user_id, None)
            self._maybe_prune_start_lock(session.user_id)
            evicted += 1
        return evicted

    # --- lookups (router owns the user-scoping 404) --------------------------

    def get(self, session_id: uuid.UUID) -> BuildSession | None:
        return self._sessions.get(session_id)

    def active_session_for(self, user_id: uuid.UUID) -> BuildSession | None:
        session_id = self._active_by_user.get(user_id)
        return self._sessions.get(session_id) if session_id is not None else None

    def live_user_ids(self) -> set[uuid.UUID]:
        """Users with a live in-proc session — never reaped by a sweep."""
        return set(self._active_by_user)

    def live_session_for_conversation(self, conversation_id: uuid.UUID) -> BuildSession | None:
        """The still-running build attached to THIS thread, or None — the "is the agent working
        here right now?" question the turn/mode routes ask before they let a chat turn in.

        PER-CONVERSATION, not per-user: the rule is "this chat's composer is shut while this
        chat's agent works", so a planning chat in another thread of the same project stays
        open (the per-user build lock already refuses a second build anywhere). Reads
        `_active_by_user` rather than `_sessions` so an ended-but-retained session (kept 5
        minutes for a late SSE reconnect) never reads as live. Inherits this registry's
        single-replica invariant — see `_claim_the_one_build_slot` — and goes blind across a
        restart, which is why it is a gate BESIDE the mode check, never a replacement for it.

        IT CAN NO LONGER MATCH ANYTHING, AND THAT IS NOT NEW — the deletion only made it
        provable. `BuildSession.conversation_id` was passed at exactly one construction site,
        inside the deleted `_start_locked`; `ensure_sandbox`, the live producer, has never
        passed it, so the field defaults to `None` and the comparison below is unconditionally
        true for any real conversation id. The gate at `conversations/turns.py` that calls this
        has therefore been inert in production since the start route lost its client. IT IS
        LEFT IN PLACE DELIBERATELY, as a redesign seam rather than a deletion: `ensure_sandbox`
        has the turn state in scope at its caller (`turns/engine.py`) and threading one argument
        through would make the gate live for the first time. NOTHING IS UNGUARDED MEANWHILE —
        `turns.py` also asks `conversation_is_mid_reply` (the same-conversation case) and
        `active_session_for` (the cross-conversation case). Do not read the green tests around
        that gate as evidence it works: `tests/api/v1/conftest.py`'s `building` fixture
        hand-builds a session WITH a `conversation_id`, a shape production cannot produce, and
        says so in its own docstring."""
        for session_id in self._active_by_user.values():
            session = self._sessions.get(session_id)
            if session is None or session.conversation_id != conversation_id:
                continue
            if session.status in {BuildSessionStatus.ENDED, BuildSessionStatus.FAILED}:
                continue
            return session
        return None

    # --- the shared acquire-with-conflict-check + compensated-release shape ---

    async def _compensate_lock_and_container(
        self,
        redis: aioredis.Redis,
        user_id: uuid.UUID,
        scope: _LockScope,
        sandbox_client: SandboxClient,
    ) -> None:
        """Undo a failed `_holding_user_lock` body. Order matters: clear the starting marker
        first (a failed start is over regardless, and must not outlive it by the marker's own
        TTL), then — unless ADOPTED, in which case the session now owns everything — tear
        down any container the body CREATED (a SPARED handle is skipped: it names a container
        already up, or since brought fully up, so destroying it is collateral damage, not a
        rollback), then holder-release the lock LAST, mirroring `reap_user`'s ordering so a
        concurrent start can never acquire while the doomed container is still up. Each step
        is guarded separately so one blip cannot mask or skip another."""
        try:
            await clear_starting_marker(redis, user_id)
        except Exception:
            _log.exception("starting marker clear failed in compensation", user_id=str(user_id))
        if scope.adopted:
            return
        if scope.handle is not None and not scope.spared:
            with suppress(SandboxError):
                await sandbox_client.teardown(scope.handle)
        try:
            await release_lock_as_holder(redis, user_id, scope.token)
        except Exception:
            _log.exception("lock release failed in compensation", user_id=str(user_id))

    @asynccontextmanager
    async def _holding_user_lock(
        self,
        redis: aioredis.Redis,
        user_id: uuid.UUID,
        sandbox_client: SandboxClient,
        project_id: uuid.UUID,
        *,
        arm: _ClaimArm,
        spare_app: str | None = None,
    ) -> AsyncIterator[_LockScope]:
        """Reconcile stale state → acquire the one-per-user Redis lock → run the body
        compensated. The ONE skeleton behind `relaunch_preview` and `ensure_sandbox` (their
        pre-checks deliberately differ — see each call site; it was three doors until
        `_start_locked` was deleted with the standalone build stack), and the single writer of
        the `starting` marker: every door into a container goes through here, so a start in
        flight is reported identically to every tab, every session and a page reloaded
        mid-start, never something a browser has to remember across a request.

        `project_id` NAMES the start for `project_preview_state` and the reclamation spare
        predicate — it is the marker's whole payload. Required, not optional: a marker that
        could not say which project it is starting would be able to report `starting` but
        never `slot_taken`, which is the ghost this unit replaces.

        `arm` NAMES THE DOOR on the `build_workspace_claimed` line, and is required for the same
        reason: from inside here the two callers are indistinguishable, and "a citizen pressed
        Launch" reads nothing like "a chat message allocated a workspace" to whoever is reading
        the log. Two values, not the three the alarm's docstring lists — the standalone build
        door was deleted with `_start_locked`, and a value nothing can emit is a value an alert
        would wait for forever.

        THE TWO WAYS THE LOCK CAN DENY, and why they leave here as different exceptions.
        `acquire_lock` returning `None` now means one thing only — the lock is genuinely
        HELD — so `BuildSessionConflictError` (router 409, carrying the live session id) is
        always a true statement about a real session. A Redis failure instead raises
        `LockUnavailableError` (a `RedisError`), which passes straight through to the
        router's `build_coordination_or_503` and becomes a 503. Before that split, an outage
        was swallowed into the same `None` and every affected user was told a build session
        was already active when none existed.

        `reconcile_user` above runs BEFORE the acquire and calls the deliberately-unguarded
        primitives (see the REDIS-ERROR POLICY in `locks.py`), so a hard outage usually
        raises there first — a raw `RedisError`, which the same router seam maps to the same
        503. Both shapes land on one status; neither is a 409 and neither is a 500.

        The reconcile passes `certified_dead=True`: every caller of this context manager
        holds the per-user `_start_lock_for` AND has already verified
        `user_id not in _active_by_user`, and the deploy contract is single-replica — so a
        lock/heartbeat still present in Redis here is a dead session's residue, not
        liveness, and reconcile reaps THROUGH it instead of letting the acquire below 409
        on a ghost. The sweep's `reconcile_user` keeps the shield (it holds neither fact).

        Failure-safe by construction:
        - Compensation runs on ANY body failure INCLUDING CancelledError — relaunch blocks for
          minutes, so a dropped request (uvicorn cancels the handler) must still tear down the
          container and release the lock. It runs in its own task awaited under `shield`
          (the `_finalize` pattern), so even a second cancel delivered mid-compensation lets
          it complete.
        - A clean exit releases the lock UNLESS the body adopted it (start's session owns the
          token; `_do_finalize` releases). The release sits inside the protected region: if it
          fails, compensation still tears the container down rather than leaving a live
          preview behind a lock nobody can release.
        - The `starting` marker is written the instant the lock is acquired (nothing before
          this may write it: an unacquired lock means this request is not the one starting
          anything) and cleared on the SAME clean exit that releases or adopts the lock,
          whichever the body did. A failed body clears it from the compensation arm instead
          (see `_compensate_lock_and_container`), so every exit — success, adoption or
          failure — leaves no marker behind before its TTL would have.
        """
        # THE CLOCK STARTS AT THE DOOR, not at `acquire_lock` below — the placement is the whole
        # honesty of the number. `acquire_lock` never waits (it answers None on contention and
        # this raises a 409), so timing that call alone would report ~0 forever and say nothing.
        # What a citizen actually waits through here is the RECONCILE: tearing a stale container
        # down is an ARM delete measured in tens of seconds. `reclaimed` on the same line says
        # whether that is where the time went.
        claim_started_at = time.monotonic()
        reclaimed = False
        if not await _the_live_sandbox_is_already_the_one_we_want(redis, user_id, spare_app):
            reclaimed = await reconcile_user(
                redis, user_id, sandbox_client, has_live_session=False, certified_dead=True
            )
        else:
            # SPARE THE CONTAINER, NOT THE LOCK. Reconcile does two jobs, and only one of them
            # is the destructive one this branch exists to skip: it also `reap_lock`s, and that
            # was the ONLY thing clearing a dead process's residual lock on this path. The
            # certified-dead facts above say any lock still here is residue — a live holder
            # would be in `_active_by_user` in this very process — so skipping the reap along
            # with the reap-through left `acquire_lock` returning None and the recovery button
            # answering 409, naming no session, until the sweep caught up minutes later.
            await reap_lock(redis, user_id)
        token = await acquire_lock(redis, user_id)
        if token is None:
            raise BuildSessionConflictError(self._active_by_user.get(user_id))
        lock_wait_ms = int((time.monotonic() - claim_started_at) * 1000)
        scope = _LockScope(token=token)
        try:
            # From here until the scope exits, a poll of `project_preview_state` for
            # `project_id` answers `starting` rather than whatever it would otherwise have said
            # (a stale `asleep`, or a ghost `slot_taken`). Written AFTER the lock is held so a
            # request that loses the race to acquire it never claims a start it did not win,
            # and INSIDE the try because by this point a lock IS held: a Redis blip on this one
            # `SET`, raised from above the try, would have unwound past the compensation arm and
            # left that lock in place for its full 900s — every later start, relaunch and turn
            # for this user answered "already building" with nothing building.
            await write_starting_marker(redis, user_id, project_id)
            # THE FIRST LINE OF A BUILD, and until now the only way to answer "did this citizen
            # get a slot at all, and whose slot was it" was to infer it backwards from a later
            # failure. `reclaimed` is the field that matters most: one of this citizen's own
            # projects evicting another is otherwise completely invisible, and it is the thing
            # a `serving_proof_stamp_refused` on the same user wants reading beside it.
            #
            # The two ids are passed EXPLICITLY even though the build's contextvars already
            # carry them: this context manager is the shared skeleton behind both doors, and
            # only one of them (`relaunch_preview`) binds in this module — the other's binding
            # lives in the turn task that calls `ensure_sandbox`. A first line that cannot say
            # whose slot this was would be worth nothing at all, so it does not depend on a
            # binding made in another file.
            _log.info(
                BUILD_WORKSPACE_CLAIMED_EVENT,
                arm=arm,
                reclaimed=reclaimed,
                lock_wait_ms=lock_wait_ms,
                user_id=str(user_id),
                project_id=str(project_id),
            )
            yield scope
            # Clean exit — the body either released control back to us (RELEASE below) or
            # adopted the lock (a session now owns the container). Either way the start this
            # marker named is OVER: it succeeded or it handed off, and the container's own
            # signals (the registry, then the lease) are what protect it from here.
            #
            # GUARDED, deliberately, unlike `release_lock_as_holder`/`write_heartbeat` below
            # and elsewhere in this module: those sit BEFORE success is real, so their raise
            # is what triggers compensation on a container that has not earned its keep yet.
            # This sits AFTER it — the container is already up and, on this branch, may
            # already be ADOPTED into a live `BuildSession` the caller is about to register.
            # An unguarded raise here would unwind past that registration on a bare Redis
            # blip clearing a best-effort marker, leaking a running, adopted container with
            # no session tracking it. The marker's mandatory TTL is the backstop instead.
            try:
                await clear_starting_marker(redis, user_id)
            except Exception:
                _log.exception(
                    "starting marker clear failed on clean exit; its TTL will expire it",
                    user_id=str(user_id),
                )
            # Two tests in `test_manager.py` pin the release-inside-the-protected-region rule
            # the docstring states — `test_relaunch_spares_the_container_when_
            # the_lock_release_hits_a_redis_error` and its `..._heartbeat_seed_...` twin.
            if not scope.adopted:
                await release_lock_as_holder(redis, user_id, token)
        except BaseException:
            comp = asyncio.ensure_future(
                self._compensate_lock_and_container(redis, user_id, scope, sandbox_client)
            )
            self._tasks.add(comp)
            comp.add_done_callback(self._tasks.discard)
            # `suppress` here only ever eats a re-delivered CancelledError from the shield
            # await (the compensation task itself never raises); the original failure is
            # re-raised either way, with the compensation guaranteed to run to completion.
            with suppress(BaseException):
                await asyncio.shield(comp)
            raise

    async def _slot_conflict_for(
        self,
        user_id: uuid.UUID,
        blocking: BuildSession | None,
        blocking_id: uuid.UUID | None,
        db: AsyncSession | None,
        requested_project_id: uuid.UUID | None,
    ) -> Exception:
        """WHICH refusal a held slot has earned.

        `BuildSessionConflictError` (409 `build_session_already_active`) is the "nothing you can
        do but wait" branch of `utils/turnStreamApi.ts`, and reaches the citizen as `ComposerBox`'s
        generic "That message did not send… try again" — advice that stays wrong for as long as
        the other build runs. It is honest only for a same-project double-send, which has no
        incumbent to release. A DIFFERENT-project holder has a remedy, stop it, and
        `SandboxReclaimBlockedError` is how it is offered: `ReclaimWorkspaceDialog` already carries
        the copy and the stop-it-first handler for the `building` arm, unreachable only because
        this guard answered first with the poorer truth. Falls back to the bare conflict whenever
        the richer refusal cannot be told truthfully — no `db` to name the project with, no session
        to read, or a project row that has gone (`_occupying_project`) — since a dialog naming the
        wrong project is worse than a plain refusal.
        """
        if blocking is None or db is None or requested_project_id is None:
            return BuildSessionConflictError(blocking_id)
        if blocking.project_id == requested_project_id:
            return BuildSessionConflictError(blocking_id)
        name = await db.scalar(
            sa.select(Project.name).where(
                Project.id == blocking.project_id, Project.user_id == user_id
            )
        )
        if name is None:
            return BuildSessionConflictError(blocking_id)
        # The SAME predicates `_refuse_if_reclaim_would_destroy_work` uses, so the two refusals
        # never disagree about what is happening in there. `dirty` is deliberately NOT probed:
        # `SandboxReclaimBlockedError` states why - a `git status` taken while the agent writes
        # is true for no instant the citizen cares about.
        return SandboxReclaimBlockedError(
            project_id=blocking.project_id,
            project_name=name,
            app_id=blocking.app_id,
            dirty=None,
            building=self._writing_session_holds(user_id, blocking.app_id),
            agent_working=self._live_session_holds(user_id, blocking.app_id),
        )

    async def _claim_the_one_build_slot(
        self,
        user_id: uuid.UUID,
        *,
        db: AsyncSession | None = None,
        requested_project_id: uuid.UUID | None = None,
    ) -> None:
        """Fail closed if this user already holds the one-per-user slot — the pre-check every
        allocating path runs BEFORE reconcile/acquire. Returns normally when the slot is free
        (or freed itself while we waited); raises `BuildSessionConflictError` otherwise.

        A live in-process session is the AUTHORITATIVE double-session guard: a second run
        must never launch even if the Redis lock lapsed under the first (a lapsed lock must
        not be the ONLY guard).

        SINGLE-REPLICA CONSTRAINT (binding — see the deploy checklist): this guard is the
        `self._active_by_user` in-process dict, so on two replicas there are two guards that
        cannot see each other and the same user could run two concurrent builds — the Redis
        lock is the ONLY cross-process backstop, and it is deliberately not trusted as the
        sole guard here. One replica is a deploy-time invariant, not a runtime check.

        Written to be shared by `_start_locked` and `ensure_sandbox`, because they were the same
        claim on the same slot: a Write turn attaching a sandbox and a build starting one are
        indistinguishable to the reaper, the Redis lock and the container budget. Only
        `ensure_sandbox` is left, and it holds `_start_lock_for(user_id)` across the call, which
        is what makes the check-then-allocate below atomic per user.
        """
        if user_id not in self._active_by_user:
            return
        blocking_id = self._active_by_user.get(user_id)
        blocking = self._sessions.get(blocking_id) if blocking_id is not None else None
        finalize = blocking.finalize_task if blocking is not None else None
        if blocking is None or not blocking.terminal_committed or finalize is None:
            raise await self._slot_conflict_for(
                user_id, blocking, blocking_id, db, requested_project_id
            )
        # The blocking session has already COMMITTED its terminal — it is ended but still
        # finalizing. Wait (bounded) for the shielded end sequence instead of 409ing the user's
        # own finished build, then fall through to a fresh allocation; on a timeout or a finalize
        # error, keep the 409.
        #
        # ONLY A STOP CAN PUT US HERE NOW. `finalize_task` is assigned in exactly one place,
        # `_finalize`, and with `_run_and_finalize` deleted the only caller left is `_end` — i.e.
        # `stop` / `force_end`. The case this was written for ("a refine sent right on the heels
        # of natural completion") needed a build that finalized itself on completion, which no
        # longer exists. The guard stays because it still reads correctly and fails closed: on a
        # session that never finalizes, `finalize` is None and the 409 above is taken.
        try:
            await asyncio.wait_for(asyncio.shield(finalize), timeout=_FINALIZE_GRACE_SECONDS)
        except Exception:
            raise await self._slot_conflict_for(
                user_id, blocking, blocking_id, db, requested_project_id
            ) from None

    async def save_project_snapshot(
        self,
        db: AsyncSession,
        user: User,
        project_id: uuid.UUID,
        *,
        sandbox_client: SandboxClient,
    ) -> SaveOutcome:
        """THE SAVE — the user's click, and the only thing that writes their work to Blob.
        Requires a LIVE container (the tree only exists there); `NoLiveSandboxError` is the
        honest answer rather than a silent false success. Deliberately does NOT require an
        in-process session — the common Save has none, right after a turn ends and the
        container is pardoned — but DOES refuse while a session is actively WRITING: a save
        mid-write would bundle half-finished disk state as the saved bundle Relaunch restores.
        Scoped to WRITING sessions, not merely attached ones (Ask/Plan attach too), or the
        ordinary Save button would refuse mid-chat."""
        app_id = await _existing_app_id(db, user.id, project_id)
        if app_id is None:
            raise NoLiveSandboxError(project_id)
        # A save mid-write bundles whatever half-written state is on disk — the switch
        # dialog's "Save and switch" once let a save SUCCEED against a live build with the
        # release failing right after, leaving a corrupted saved bundle. This refusal is a
        # backstop (the client is expected to stop the build first); `may_write` (from the
        # kind's toolset) is what scopes it to WRITING sessions, not merely attached ones —
        # Ask/Plan attach too, and gating on "a session exists" would refuse the ordinary
        # Save button mid-chat.
        if self._writing_session_holds(user.id, app_id):
            raise BuildSessionConflictError(self._active_by_user.get(user.id))
        handle = await self._attach_for_read(user.id, app_id, sandbox_client)
        # THE SAVED VERSION — the user asked for this one. It drives `dirty` and it is what
        # `submit` pins, so it is the one key a platform-initiated write must never touch.
        # Commits inside the container before bundling, so a save captures the working tree
        # whether or not the agent had committed it, and the bundle carries HEAD's whole
        # history.
        await write_snapshot(sandbox_client, handle, app_id)
        # Read the head AFTER the save: `write_snapshot` runs `git init` + commit itself, so on
        # a first save this is the commit it just created — the value the client needs to
        # settle its indicator, and one that did not exist a moment ago.
        saved = await container_state(sandbox_client, handle)
        return SaveOutcome(app_id=app_id, head_sha=saved.head if saved else None)

    async def project_compile_state(
        self,
        db: AsyncSession,
        user: User,
        project_id: uuid.UUID,
        *,
        sandbox_client: SandboxClient,
    ) -> CompileState:
        """What is the app currently compiling? — for a tab with no live turn. Separate from
        the turn stream because the compile signal is emitted by the turn's preview watcher and
        stops the moment a turn ends: a tab that reloads after a red turn has no producer at
        all, and would otherwise show the framework's error screen under a live-preview label
        — reachable by pressing F5. Not folded into `project_preview_state`, whose budget is
        frozen at NO container call (a browser tab on a 45-second timer); this is its own call,
        gated by the caller on a preview already framed. `UNKNOWN` for every unanswerable case
        — absent must never read as clean; `compile_state` never raises."""
        app_id = await _existing_app_id(db, user.id, project_id)
        if app_id is None:
            return CompileState.UNKNOWN
        try:
            handle = await self._attach_for_read(user.id, app_id, sandbox_client)
        except NoLiveSandboxError:
            # Nothing is serving this project. The pane has its own vocabulary for that
            # (`previewState`), and answering `clean` here would uncover over a dead app.
            return CompileState.UNKNOWN
        report = await sandbox_client.compile_state(handle)
        return report.state

    async def project_workspace_check(
        self,
        db: AsyncSession,
        user: User,
        project_id: uuid.UUID,
        *,
        sandbox_client: SandboxClient,
    ) -> WorkspaceState:
        """Is the app the citizen is looking at still the app? — asked by an idle tab: the
        per-turn integrity check catches drift only between messages, nothing for a citizen
        reading, in another tab, or at lunch, while the completion claim keeps saying "your
        app is live". Never folded into `project_preview_state` (frozen at no container call);
        fires only when the preview already reports alive and a completion claim stands.
        RATE-LIMITED PER APP on purpose — without it an idle tab is a container exec every 45
        seconds forever. NEVER restores or destroys, only reports; the restore belongs to the
        next turn, where the citizen can confirm it."""
        app_id = await _existing_app_id(db, user.id, project_id)
        if app_id is None:
            return WorkspaceState.INTACT  # nothing built yet: nothing to have lost
        remembered = _idle_checks.get(app_id)
        now = datetime.now(UTC)
        if remembered is not None and (now - remembered.asked_at) < _IDLE_CHECK_WINDOW:
            return remembered.verdict
        try:
            handle = await self._attach_for_read(user.id, app_id, sandbox_client)
        except NoLiveSandboxError:
            # Nothing is serving this project at all. The pane has its own vocabulary for that
            # (`previewState`), and a container that is GONE is a different fact from one that is
            # running and empty — conflating them would retract a completion claim every time a
            # workspace merely went to sleep.
            return WorkspaceState.UNREADABLE
        verdict = await workspace_integrity(
            sandbox_client,
            handle,
            app_id,
            restore_source_key=await self._restore_source_for_the_gate(app_id),
        )
        _idle_checks[app_id] = _IdleCheck(asked_at=now, verdict=verdict.state)
        if verdict.state is WorkspaceState.REVERTED:
            _log.error(
                WORKSPACE_LOST_WHILE_IDLE_EVENT,
                app_id=str(app_id),
                app_name=handle.app_name,
                last_known_head=verdict.head,
                recovery_copy_available=verdict.durable_copy_exists,
                verdict=verdict.state.value,
            )
        return verdict.state

    async def project_save_state(
        self,
        db: AsyncSession,
        user: User,
        project_id: uuid.UUID,
        *,
        sandbox_client: SandboxClient,
    ) -> SaveState:
        """Is there anything to save? The container's HEAD against the saved bundle's. Compared
        by COMMIT, not by timestamp or a local dirty flag, since that is the only comparison
        that survives a reload, a second tab, and a process restart — all three lose in-memory
        state while the two commits stay put. `dirty=None` means UNKNOWN, distinct from False:
        no live container (nothing to compare), or a store we could not read. A UI that renders
        unknown as clean tells the user their work is safe when nobody checked."""
        app_id = await _existing_app_id(db, user.id, project_id)
        if app_id is None:
            return SaveState(app_id=None, dirty=None, container_head=None, saved_head=None)
        try:
            handle = await self._attach_for_read(user.id, app_id, sandbox_client)
        except NoLiveSandboxError:
            return SaveState(app_id=app_id, dirty=None, container_head=None, saved_head=None)
        return await self._save_state_of(sandbox_client, handle, app_id)

    async def _save_state_of(
        self, sandbox_client: SandboxClient, handle: SandboxHandle, app_id: uuid.UUID
    ) -> SaveState:
        """The dirty ladder for a container we ALREADY hold a handle on, reused by the reclaim
        guard. THE UNCOMMITTED ARM IS NOW LOAD-BEARING: the platform commits the tree itself
        once, inside the turn-boundary bundle, so until that runs — the whole of a building
        turn, and forever if it died first — new work exists ONLY as an uncommitted worktree
        at an unmoved HEAD; comparing commits alone would report `dirty=False` over unsaved
        work. Deleting or reordering this arm silently breaks the save indicator, losing work
        (`test_save_state.py` pins it). `dirty` stays TRI-STATE: `None` means "nobody could
        check", never "clean"."""
        state = await container_state(sandbox_client, handle)
        if state is None:
            # Could not ask the container — the only honest unknown.
            return SaveState(app_id=app_id, dirty=None, container_head=None, saved_head=None)
        recovery_at = await _recovery_written_at(app_id)
        saved_head = await _saved_head(app_id)
        # UNCOMMITTED WORK IS DIRTY regardless of what the commits say — see the docstring.
        # `saved_head` is still reported alongside it: the answer is "there is unsaved work on top
        # of the version you saved", not "nothing here is saved", and a client that lost the
        # saved version would be describing a bigger loss than the one that happened.
        #
        # ...BUT FRAMEWORK CHURN IS NOT WORK, and this arm is where that was forgotten. `next dev`
        # rewrites `next-env.d.ts` and normalises `tsconfig.json` on every boot, so merely OPENING
        # a project — never touching it — made the porcelain non-empty and every reader of this
        # flag act on it: the rail announced "You have changes that are not saved yet", the reclaim
        # dialog offered to save them, and the exit guard demanded a save before leaving. On one
        # observed hand-over that cost a citizen forty seconds of a modal spinner to store two
        # files a framework had rewritten by itself.
        #
        # THE PREDICATE IS THE SAVE INDICATOR'S OWN, NOT THE REAPER'S, and the difference is not
        # tidiness. `clean_but_for_churn` also forgives `tsconfig.json`, which the model is
        # explicitly invited to edit — `prompt_blocks.py` lists it under "editable". Forgiving it
        # HERE would report "Everything is saved" over an agent's own change — the one
        # wrong answer this indicator must never give. `only_regenerated_files_changed` forgives
        # only what the framework rewrites and the agent may not touch, and fails CLOSED on a
        # truncated porcelain, so routing through it cannot turn a genuinely dirty tree clean.
        if state.uncommitted and not only_regenerated_files_changed(state):
            return SaveState(
                app_id=app_id,
                dirty=True,
                container_head=state.head,
                saved_head=saved_head,
                recovery_at=recovery_at,
            )
        if state.head is None:
            # No commit yet. A fresh template has no `.git`, so this is every brand-new
            # project — and with nothing saved it is DIRTY, not unknown. Reading it as unknown
            # hid the Save button on exactly the projects that most need it.
            return SaveState(
                app_id=app_id,
                dirty=saved_head is None,
                container_head=None,
                saved_head=saved_head,
                recovery_at=recovery_at,
            )
        if saved_head is None:
            # Committed work, nothing ever saved.
            return SaveState(
                app_id=app_id,
                dirty=True,
                container_head=state.head,
                saved_head=None,
                recovery_at=recovery_at,
            )
        return SaveState(
            app_id=app_id,
            dirty=state.head != saved_head,
            container_head=state.head,
            saved_head=saved_head,
            recovery_at=recovery_at,
        )

    async def recoverable_work(self, app_id: uuid.UUID) -> RecoverableWork | None:
        """Is there a crash-recovery copy strictly NEWER than the saved version? Answered from
        the store's `last_modified`, not the bundles: two HEAD shas tell you which trees
        differ, never which came first, and ancestry would cost a download plus real git. Two
        kinds of "no": a store that answered and holds nothing newer returns `None`; a store
        that would NOT answer RAISES — a display surface can treat that as "no offer", but a
        caller about to RESTORE must not silently hand back an older tree instead.
        Deliberately not part of `project_save_state`, asked precisely when the container is
        gone."""
        try:
            store = get_storage()
        except StorageUnconfiguredError:
            return None
        try:
            recovery = await store.head(recovery_key(app_id))
            saved = await store.head(snapshot_key(app_id))
        except StorageError:
            # An unreadable store is NOT "nothing to recover", and the difference matters to
            # whoever is about to act on the answer. Callers that merely display the offer can
            # treat it as absent; a caller about to RESTORE must not (see
            # `_restore_source_or_bust`, which re-asks and refuses rather than silently
            # handing back an older tree).
            _log.warning("could not determine recoverable work", app_id=str(app_id))
            raise
        if recovery is None or recovery.last_modified is None:
            return None
        # No saved version at all, but a recovery copy exists: everything the user has ever
        # done is in it, so it is unambiguously worth offering.
        if saved is None or saved.last_modified is None:
            return RecoverableWork(app_id=app_id, written_at=recovery.last_modified)
        # SAME TREE, whatever the clocks say. `touched` means "a mutating tool ran", not "the
        # tree changed", so a turn that only read files still rewrites the recovery bundle from
        # an unchanged worktree. Comparing the stamped HEAD answers this exactly and stops a
        # permanent, contradictory "you have unsaved work" against a `dirty=False` save state.
        if _head_of(recovery) is not None and _head_of(recovery) == _head_of(saved):
            return None
        # ORDERING, with the tie broken TOWARD the newer work. Azure stamps `last_modified` in
        # whole seconds, so a Save and a turn-boundary write inside one second compare EQUAL —
        # and `<` alone resolved that to "the save wins", which restored the older tree over the
        # user's newer work. That is the loss this whole mechanism exists to prevent, reappearing
        # inside a one-second window; a live end-to-end run reproduced it. The shas above have
        # already established the trees differ, so an equal stamp means "written together, and
        # the recovery copy is the one written at the turn boundary" — resume it. Restoring a
        # tree that turns out to be the same age costs nothing (it is still not a promotion:
        # `dirty` stays true); restoring the older one costs the user their work.
        if recovery.last_modified < saved.last_modified:
            return None  # the save is genuinely newer — nothing extra to offer
        return RecoverableWork(app_id=app_id, written_at=recovery.last_modified)

    async def newest_restore_source(self, app_id: uuid.UUID) -> str | None:
        """The key of the bundle holding the app's MOST RECENT tree, or None for the saved
        one. Every automatic restore goes through here, closing a real data-loss bug: pulling
        `snapshot_key` unconditionally used to rebuild a reclaimed container from the last
        SAVED tree, and that turn's own recovery write then overwrote the recovery bundle —
        work done after the last Save survived one turn and then existed nowhere. RESUMPTION,
        NOT PROMOTION: `snapshot_key` is untouched and `dirty` stays true. FAILS CLOSED like
        every read here — an unreadable store raises `SnapshotUnavailableError` rather than
        silently restoring an older tree over newer work."""
        presence = await head_presence(recovery_key(app_id))
        if presence is None:
            raise SnapshotUnavailableError("recovery state unknown after retries", app_id=app_id)
        if not presence:
            return None
        try:
            return recovery_key(app_id) if await self.recoverable_work(app_id) else None
        except StorageError as exc:
            raise SnapshotUnavailableError(
                "recovery state unknown after retries", app_id=app_id
            ) from exc

    async def _nothing_to_lose(
        self, sandbox_client: SandboxClient, handle: SandboxHandle, state: SaveState
    ) -> bool:
        """Is this workspace provably empty of the user's work? The case this exists for: a
        Plan or Ask question against a brand-new project also takes the one-per-user
        workspace, whose container holds only the golden template — yet `dirty` is True for
        it (a never-built project must show a Save button), and reading that as "unsaved
        changes" locked a user out of their actual app (observed live). Not `head is None`: a
        fresh provision seeds one baseline commit, so "no commits" never fires. Four required
        conditions close each way work could hide (commits <= 1, a clean tree, nothing saved,
        no recovery bundle); an unanswerable probe is NOT permission — ambiguity denies."""
        if state.saved_head is not None or state.recovery_at is not None:
            return False
        container = await container_state(sandbox_client, handle)
        if container is None or container.commits == 0:
            return False  # could not tell — ambiguity denies
        if container.commits > 1:
            return False  # work beyond the baseline
        # ONE spelling of "is this tree empty", shared with the integrity verdict. Two subtly
        # different ones is how the reclaim gate and the reversion gate would drift into
        # disagreeing about whether a container may be destroyed.
        return clean_but_for_churn(container)

    async def _refuse_if_reclaim_would_destroy_work(
        self,
        db: AsyncSession,
        user: User,
        *,
        spare_app: str | None,
        sandbox_client: SandboxClient,
    ) -> None:
        """The missing telling: raise rather than silently destroy another project's work.
        Runs BEFORE `_holding_user_lock` — once the reconcile marks the registry `ending` the
        container can no longer be attached to or questioned. Every `return` below is an
        assertion that nothing will be lost: only a CERTAIN answer earns one, and "I could not
        tell" never justifies destroying something. The two exits that instead RAISE do so
        when the answer is unknown (Redis silent, or the attach unable to confirm anything),
        with `dirty=None`; the user is not wedged — *Switch anyway* releases through
        `reap_user`, which needs only the registry entry."""
        redis = get_redis()
        # Four ways this returns silently — "nothing is being taken", not "another project
        # holds it": (1) the live container is already the one we want, (2) no registry entry
        # or it is not READY, (3) the occupying name matches no app this user owns AND carries
        # no shared-view stamp either (a genuine ghost; the reconcile clears it), (4) the
        # container is CONFIRMED gone (`NoLiveSandboxError`). Widening any of these into a raise
        # would put up a dialog about nothing.
        #
        # A `shr-` OCCUPANT IS A FIFTH CASE, and it does NOT fall into (3): it always carries a
        # name that matches no app the caller owns (the project is somebody else's), which is
        # exactly why it is checked separately, BEFORE the ghost exit, by reading its stamped
        # identity straight off the registry hash rather than trying to forward-match it.
        #
        # The clean-incumbent exits below DO raise rather than fall through, deliberately: a
        # silently reclaimed clean incumbent is true about the work but wrong about the
        # person — their other project stops with no warning because a screen elsewhere
        # needed the workspace. Both report `dirty=False` so the dialog says a clean stop, not
        # unsaved changes that do not exist.
        #
        # Two further exits REFUSE rather than return, because the honest answer is unknown:
        # Redis not answering (the registry is unreadable, not empty — a container may be
        # live) and the attach failing to confirm anything (`SandboxNotReadyError` and the
        # like — the container may be alive). Both raise `dirty=None`, which the client
        # already treats as "may have unsaved changes".
        #
        # Only a reachable incumbent with unsaved (or unknowable) work raises. Nothing here
        # writes a snapshot — saving stays the user's explicit action.
        if await _the_live_sandbox_is_already_the_one_we_want(redis, user.id, spare_app):
            return
        # DELIBERATELY UNGUARDED. `read_registry` is one of the answer-bearing
        # primitives `locks.py` keeps bare on purpose: swallowing a `RedisError` here would
        # "manufacture a certain-looking answer out of an ambiguous store" — a phantom "no
        # sandbox" that permits the teardown. Let it propagate; the routers' existing
        # `build_coordination_or_503` seam turns it into a 503, which is a true statement.
        reg = await read_registry(redis, user.id)
        if reg is None or reg.get(REGISTRY_FIELD_STATE) != REGISTRY_STATE_READY:
            return
        occupied_by = reg.get(REGISTRY_FIELD_APP_NAME)
        if occupied_by is None or occupied_by == spare_app:
            return
        # A COLLEAGUE'S SHARED VIEW, CHECKED FIRST (#198, requirement 24). `shr_name_for`
        # hashes the (app, recipient) pair the identical forward-match-only way `app_name_for`
        # does, so `_occupying_project` below — which searches the CALLER's own app rows — can
        # never resolve one: the project belongs to somebody else. Before this stamp existed at
        # Launch, that lookup came back `None` and fell through the ghost exit two lines below,
        # silently reclaiming a colleague's still-open project with no dialog at all.
        #
        # NEITHER `building` NOR `agent_working` APPLIES: `launch_shared_preview` mints no
        # `BuildSession` and no chat turn ever writes into this container, so there is no
        # session for `_writing_session_holds`/`_live_session_holds` to find regardless of
        # which app_id is asked — reported here as a plain constant, not probed, because
        # probing something that can never be true is a wasted round trip with an already-known
        # answer. `dirty=False` for the identical reason: nothing here is the recipient's own
        # unsaved work to lose, so the CLEAN-STOP dialog is the true one — "still open" and
        # "starting it again later brings it back", both facts that hold for a shared view.
        shared_occupying = await _occupying_shared_project(db, reg)
        if shared_occupying is not None:
            raise SandboxReclaimBlockedError(
                project_id=shared_occupying.project_id,
                project_name=shared_occupying.project_name,
                app_id=shared_occupying.app_id,
                dirty=False,
                building=False,
                agent_working=False,
                is_shared_view=True,
            )
        occupying = await _occupying_project(db, user.id, occupied_by)
        if occupying is None:
            return
        # AN AGENT IS WRITING IN THERE RIGHT NOW — refuse differently, and refuse BEFORE the
        # probe below. Two reasons the order matters. Asking a container `git status` while the
        # agent is mid-write returns a tree that is true for no instant the user cares about,
        # and the honest `dirty` for it is "none of your business yet". And the answer this
        # guard would otherwise reach — "has unsaved changes, Save or Switch" — offers two
        # buttons `release_project_sandbox` refuses while a live session owns the container, so
        # the user gets a choice and then an error whichever they pick. Observed live.
        #
        # THE BROAD ANSWER, READ ONCE AND CARRIED TO EVERY REFUSAL BELOW. It is a separate fact
        # from `building`, not a replacement for it (see `SandboxReclaimBlockedError`): the
        # hand-over dialog has to tell the citizen whether the other project's agent is
        # mid-thought before they choose, and a Plan or Ask turn is mid-thought exactly as a
        # build is. `_live_session_holds` is also what `release_project_sandbox` refuses on, so
        # this is a prediction of the next step rather than a guess about it.
        #
        # READ HERE, ABOVE the `building` arm, so every exit below carries it — including the
        # two that fire BECAUSE there is nothing to lose. A pristine container held by a plan
        # turn still has an agent working in it, and the dialog that offers to take it says so.
        agent_working = self._live_session_holds(user.id, occupying.app_id)
        # `_writing_session_holds`, NOT `_live_session_holds`, and the difference is a bug this
        # arm shipped with. Every mode pins the container, so the broader predicate is true
        # throughout an ordinary Ask or Plan turn — which put a hammer icon and two Stop buttons
        # in front of a user who had asked a question, and short-circuited `_nothing_to_lose`
        # below, the escape hatch written for exactly that case ("a question is not work").
        # THE NARROW PREDICATE STAYS NARROW: `agent_working` above exists so nothing ever needs
        # to widen this one to answer the hand-over's question.
        if self._writing_session_holds(user.id, occupying.app_id):
            raise SandboxReclaimBlockedError(
                project_id=occupying.project_id,
                project_name=occupying.project_name,
                app_id=occupying.app_id,
                dirty=None,  # deliberately unprobed: see above
                building=True,
                agent_working=agent_working,
            )
        try:
            handle = await self._attach_for_read(user.id, occupying.app_id, sandbox_client)
        except SandboxUnreachableError as exc:
            # The registry named it READY and it would not answer. That is not evidence of
            # absence — a cold container or a supervisor timeout looks identical from here to
            # one holding a day's work. Refuse with the tri-state null rather than guess
            # "clean", which is the one guess that loses work.
            raise SandboxReclaimBlockedError(
                project_id=occupying.project_id,
                project_name=occupying.project_name,
                app_id=occupying.app_id,
                dirty=None,
                agent_working=agent_working,
            ) from exc
        except NoLiveSandboxError:
            # The plain parent: the registry is certain nothing of this app's is live.
            return
        state = await self._save_state_of(sandbox_client, handle, occupying.app_id)
        # THE ASKING IS NO LONGER CONDITIONAL: this and the arm below are the only two of this
        # function's returns turned into a raise (the four silent exits above stay untouched —
        # widening them would put up a dialog about nothing, and the ghost-registry one could
        # not even name a project in it). ACCEPTED COST: typing one question into a brand-new
        # project and then starting a second one now raises a dialog about the first, a
        # pristine container nobody would miss — one click, on a dialog whose clean arm claims
        # no unsaved work.
        #
        # BOTH arms below report `dirty=False` rather than forwarding `state.dirty`, on
        # purpose: `dirty` answers the SAVE BUTTON's question, where a never-built project is
        # deliberately "dirty" so the button appears — but this guard asks "would reclaiming
        # this DESTROY something?", where the same state means the opposite. Forwarding
        # `state.dirty` here would silently reintroduce the bug this guard fixed.
        #
        # `_nothing_to_lose` answers that different question on four conditions (see its
        # docstring); the `or` below short-circuits deliberately — a known-clean tree is
        # already proof, not worth a round trip to confirm again.
        if state.dirty is False or await self._nothing_to_lose(sandbox_client, handle, state):
            raise SandboxReclaimBlockedError(
                project_id=occupying.project_id,
                project_name=occupying.project_name,
                app_id=occupying.app_id,
                dirty=False,
                agent_working=agent_working,
            )

        raise SandboxReclaimBlockedError(
            project_id=occupying.project_id,
            project_name=occupying.project_name,
            app_id=occupying.app_id,
            dirty=state.dirty,
            agent_working=agent_working,
        )

    async def stop_active_work(
        self,
        db: AsyncSession,
        user: User,
        project_id: uuid.UUID,
        *,
        sandbox_client: SandboxClient,
        timeout_s: float = _STOP_ACTIVE_WORK_TIMEOUT_SECONDS,
    ) -> StopOutcome:
        """Stop whatever is running in this project, wait for it to settle, and REPORT WHAT
        ACTUALLY HAPPENED — the first of "stop and switch"'s three steps (stop → save →
        release); the other two already refuse while a session is live, so this unblocks
        them. THREE STATES, NOT A BOOLEAN: both branches here used to `return True`
        unconditionally, so an expired wait reported the same success as a genuine unwind.
        Read from one final look at the session map, never assumed from elapsed time. AWAITS
        THE WHOLE STOP — a request should go through `request_stop_of_active_work` and
        `stop_state_of_active_work` instead."""
        # Scoped to the project on purpose: the slot is per-user so at most one thing is live,
        # but stopping is destructive, and stopping a different project than the one the
        # caller named just because it happened to hold the slot would be a silent-action
        # failure of its own.
        app_id = await _existing_app_id(db, user.id, project_id)
        if app_id is None:
            return StopOutcome.NOTHING_WAS_RUNNING
        return await self._stop_the_held_session(
            user.id, app_id, sandbox_client=sandbox_client, timeout_s=timeout_s
        )

    async def _stop_the_held_session(
        self,
        user_id: uuid.UUID,
        app_id: uuid.UUID,
        *,
        sandbox_client: SandboxClient,
        timeout_s: float,
    ) -> StopOutcome:
        """The stop itself, with NO DB SESSION — which is what lets it run detached.

        The caller resolves the app row (a user-scoped read) and hands the id over; everything
        below is the in-process session map, the sandbox client singleton and Redis. So the task
        `request_stop_of_active_work` starts cannot outlive a request's `get_db` and read from a
        closed connection.

        `sandbox_client` IS CURRENTLY UNREAD, and that is a consequence of the branch below
        collapsing, not an oversight. It fed `self.stop(session, sandbox_client)` on the deleted
        build arm. It stays because retiring it would have to walk back through `stop_active_work`
        and `request_stop_of_active_work` — the live take-back path — and out to the route's
        `SandboxDep`, changing when a sandbox-off deployment fails; that is a separate change.

        ONE KIND OF LIVE NOW, and this used to branch on two. `_start_locked` registered a build
        session carrying a `run_build` task and the whole terminal-commit machinery, and that
        branch cancelled the task through `self.stop(...)`; it is deleted with the start route.
        `session.task` was assigned at exactly one place in this file, inside `_start_locked`, so
        every session production can build now carries none — `ensure_sandbox` registers a Write
        turn's workspace with no task at all, because the work is running in the turn engine
        instead. What is left is that second arm, unconditionally.

        WHERE THE DANGLING TOOL CALL COMES FROM, known and accepted. The stop cuts the run
        wherever it stands, which is routinely between a tool call and its result. The replay is
        kept valid by `_INTERRUPTED_RESULT` in `messages/store.py` — read the decision recorded
        beside it before shipping chat history, because landing this stop on a tool-result
        boundary is what has to change once a past conversation can be reopened."""
        if not self._live_session_holds(user_id, app_id):
            return StopOutcome.NOTHING_WAS_RUNNING
        session_id = self._active_by_user.get(user_id)
        session = self._sessions.get(session_id) if session_id is not None else None
        if session is None:
            return StopOutcome.NOTHING_WAS_RUNNING
        # A WRITE TURN's workspace. The work is the engine's; the manager session is only
        # holding the container for it. Imported lazily — `turns.engine` imports this
        # module, so a module-level import is a cycle (the reason `live_build.py`
        # documents). Its own answer is deliberately not the verdict: the engine can only
        # speak for its turn, while the fact that decides whether the container may be
        # taken is whether the MANAGER still has the slot, which the read below asks.
        from src.services.turns.engine import get_turn_engine

        await get_turn_engine().stop_user_turn_and_wait(user_id, timeout_s=timeout_s)
        # THE ONE READ THAT SETTLES IT. A session still holding the app means the release that
        # follows would refuse, so "still running" is not a hedge here — it is an accurate
        # prediction of the next step. Nothing about elapsed time enters this.
        if self._live_session_holds(user_id, app_id):
            return StopOutcome.STILL_RUNNING
        return StopOutcome.STOPPED

    async def request_stop_of_active_work(
        self,
        db: AsyncSession,
        user: User,
        project_id: uuid.UUID,
        *,
        sandbox_client: SandboxClient,
        timeout_s: float = _STOP_ACTIVE_WORK_TIMEOUT_SECONDS,
    ) -> StopOutcome:
        """ASK for the stop and return; never waits for it. Used to hold one request open for
        the whole stop, hostaging the budget to whatever request timeout sits in front —
        splitting the ask from the answer removes that dependency: this returns as soon as
        the stop is under way, and `stop_state_of_active_work` reports how it went, however
        long it takes. Answers `STILL_RUNNING` for a stop now in flight, `NOTHING_WAS_RUNNING`
        when nothing was asked before, `STOPPED` when an earlier ask already settled. ONE STOP
        PER PROJECT: a racing second ask joins the first rather than starting a second, so two
        racing transfers end with one container."""
        app_id = await _existing_app_id(db, user.id, project_id)
        self._prune_settled_stop_records()
        key = (user.id, project_id)
        in_flight = self._stop_records.get(key)
        if in_flight is not None and not in_flight.task.done():
            return StopOutcome.STILL_RUNNING
        if app_id is None or not self._live_session_holds(user.id, app_id):
            # Nothing to stop. Whether that reads as "stopped" or "nothing was running" depends
            # on whether we were ever asked before — the same question the status read answers,
            # asked the same way, so an ask and a read never disagree.
            if in_flight is not None:
                return StopOutcome.STOPPED
            return StopOutcome.NOTHING_WAS_RUNNING
        task = asyncio.create_task(
            self._stop_the_held_session(
                user.id, app_id, sandbox_client=sandbox_client, timeout_s=timeout_s
            )
        )
        # A stop that raises must not vanish into an un-retrieved task exception at GC time. The
        # status read still answers correctly either way — it reads the session map, not this
        # task — but an operator needs to know the stop itself broke, and WHOSE.
        task.add_done_callback(
            partial(
                _log_a_stop_that_failed,
                user_id=user.id,
                project_id=project_id,
                app_id=app_id,
            )
        )
        self._stop_records[key] = _StopRecord(
            app_id=app_id, task=task, requested_at=datetime.now(UTC)
        )
        return StopOutcome.STILL_RUNNING

    async def stop_state_of_active_work(
        self, db: AsyncSession, user: User, project_id: uuid.UUID
    ) -> StopOutcome:
        """THE STATUS READ: what the stop has actually achieved, right now. Read from the
        source of truth, never inferred from elapsed time — this repo has already destroyed a
        citizen's unsaved work by treating slow as gone. The only question that can license
        taking the container is: does a session still hold this app? — asked of the same map
        `release_project_sandbox` refuses on. CHEAP AND SAFE TO POLL: two user-scoped DB reads
        at most, an in-process dict lookup, nothing else, so a poll never manufactures
        activity of its own. `STOPPED` requires BOTH that nothing holds the app AND that a
        stop was asked for; absent the second, the answer is `NOTHING_WAS_RUNNING`."""
        app_id = await _existing_app_id(db, user.id, project_id)
        if app_id is not None and self._live_session_holds(user.id, app_id):
            # STILL UNWINDING — or something else took the slot in the meantime. Either way the
            # container is not free, and however long this goes on the answer stays this one.
            return StopOutcome.STILL_RUNNING
        self._prune_settled_stop_records()
        if (user.id, project_id) in self._stop_records:
            return StopOutcome.STOPPED
        return StopOutcome.NOTHING_WAS_RUNNING

    def _prune_settled_stop_records(self) -> None:
        """Drop settled stop records past their retention window, lazily.

        The record is what keeps "stopped" distinguishable from "nothing was running" after the
        fact, so it has to outlive the stop — long enough for a client that lost its connection
        to come back and ask again. It must not outlive the process's memory, hence a window
        rather than forever, and it is only ever dropped once its task is DONE: a stop still in
        flight is never forgotten, however long it takes."""
        if not self._stop_records:
            return
        cutoff = datetime.now(UTC) - timedelta(seconds=_STOP_RECORD_RETENTION_SECONDS)
        for key, record in list(self._stop_records.items()):
            if record.task.done() and record.requested_at < cutoff:
                del self._stop_records[key]

    def _say_the_preview_read_failed(self, user_id: uuid.UUID, project_id: uuid.UUID) -> None:
        """Report an unreadable preview-state read — AT MOST ONCE PER USER PER SILENCE WINDOW.

        RATE-LIMITED BECAUSE THE CALLER IS A BROWSER TIMER. Two surfaces poll this, in every
        open tab, as often as every three seconds during a build; an outage that lasts a minute
        would otherwise write hundreds of identical lines per citizen and bury the lifecycle
        lines an operator actually needs. One line names the outage; the rest only cost money.

        AND ORDINARY ANSWERS ARE DELIBERATELY NOT LOGGED AT ALL — recorded here as a decision so
        nobody adds it later. A per-tab poll on two surfaces would drown the stream and tell an
        operator nothing that `sandbox_registry_marked_pending`, `app_first_served` and
        `app_serving_lost` plus their timestamps do not already reconstruct.

        The window map is pruned on every call, so it cannot outgrow the set of users who are
        currently failing — and that set is empty the moment the store comes back."""
        now = time.monotonic()
        for who, when in list(self._unknown_reported_at.items()):
            if now - when >= _UNKNOWN_REPORT_SILENCE_SECONDS:
                del self._unknown_reported_at[who]
        # Anything the prune left behind is inside the window by construction, so presence
        # alone is the whole test — no second comparison to get the sense of backwards.
        if user_id in self._unknown_reported_at:
            return
        self._unknown_reported_at[user_id] = now
        _log.warning(
            PREVIEW_STATE_REPORTED_UNKNOWN_EVENT,
            user_id=str(user_id),
            project_id=str(project_id),
            silence_s=_UNKNOWN_REPORT_SILENCE_SECONDS,
        )

    async def project_preview_state(
        self, db: AsyncSession, user: User, project_id: uuid.UUID
    ) -> PreviewState:
        """What is serving THIS project — and if nothing is, WHY? Five states, never one
        boolean: `alive=False` used to mean never built, another project took the slot,
        asleep, AND the registry read throwing indistinguishably — the last is an ERROR, not
        a fact, and a STARTING state closes the same gap for a start in flight (once
        indistinguishable from asleep, inviting a second press to provision a second
        container). THE COST BUDGET IS PART OF THE CONTRACT, since the caller is a browser
        tab on a 45-second timer: one pipelined Redis round trip, at most two user-scoped DB
        rows and two object-store HEADs, NONE on the alive path, and NO container call, ever."""
        # PRECEDENCE, AND THE ORDER MATTERS. An unreadable store still answers `unknown`,
        # checked first, so ambiguity never wears a confident face. A registry that serves
        # this project, is READY and carries a SERVING PROOF still answers `alive` even with a
        # stale marker present — a marker must never hide a running app; the same registry
        # WITHOUT that proof answers `starting` from the same block, because a container that
        # has been scheduled and has never answered a request is a wait, not a preview, however
        # the markers happen to read. A marker naming THIS project answers
        # `starting`, ABOVE the never-built check below it, because a first build mints its
        # app row only once the start commits, so `app_id is None` does not yet mean nothing
        # is happening. A marker naming ANOTHER project answers `slot_taken`, named directly
        # from the marker rather than inverted from an app name (no ghost — the marker already
        # carries the project id). Only then do the existing arms run: never-built, asleep,
        # and the registry-sourced `slot_taken`.
        #
        # THE RESTORE QUESTION IS ONLY ASKED WHEN ITS ANSWER CAN CHANGE THE SCREEN. This used
        # to call `restorable_presence` before the registry read — one or two Blob round trips
        # per tab per 45 seconds to answer "could we put this back?" about an app that is
        # currently running. Every surface that renders the answer only exists when nothing is
        # serving the project, so the alive and starting arms return `None` (no claim) and the
        # client falls through to the answer the project route already gave it at load.
        #
        # NOT BUILT ON `_refuse_if_reclaim_would_destroy_work`: that would drag in an attach
        # and a container round trip, let a `RedisError` turn a poll into a 503, and make every
        # framed preview touch its container every 45 seconds — a manufactured activity signal
        # that would keep an unused sandbox looking busy forever.
        app_id = await _existing_app_id(db, user.id, project_id)
        try:
            reg, starting = await read_registry_and_starting_marker(get_redis(), user.id)
        except RedisNotConfiguredError:
            # A CERTAIN answer, not an ambiguous one (`services/redis/errors.py`): Redis is
            # genuinely optional outside production, and with no coordination store there is no
            # sandbox subsystem at all — so nothing can be serving OR starting this project.
            # `app_id` is a DB fact, independent of Redis, so it still settles NEVER_BUILT.
            if app_id is None:
                return PreviewState(state=PreviewLifeState.NEVER_BUILT, restorable=False)
            return PreviewState(
                state=PreviewLifeState.ASLEEP, restorable=await restorable_presence(app_id)
            )
        except RedisError:
            # AMBIGUITY. The store exists and would not answer, so this decided nothing —
            # and a thing that decided nothing must not be reported as a fact about a
            # container. Note it is NOT a 503 either: the caller is a poll, and 503ing a
            # background timer would turn a blip into an error the user has to read.
            #
            # UNKNOWN outranks even NEVER_BUILT here, deliberately: a Redis outage means a start
            # already in flight (which mints its app row only on success) is exactly as
            # unreadable as one that never happened, and reporting the DB's "no app row" as a
            # confident NEVER_BUILT would be papering over the one thing this arm exists to
            # admit it cannot see.
            #
            # The store question is INDEPENDENT of the registry question and still answerable,
            # so it is still asked when there is an app to ask it about.
            #
            # AND THE LOG IS THE WHOLE RECORD OF THIS ONE. Nothing is drawn for UNKNOWN by
            # design — a standing frame stays framed, a standing card stays put — so without a
            # line here the failure is invisible from both ends at once: the citizen is told
            # nothing, and so is the operator.
            self._say_the_preview_read_failed(user.id, project_id)
            return PreviewState(
                state=PreviewLifeState.UNKNOWN,
                restorable=await restorable_presence(app_id) if app_id is not None else None,
            )
        mine = app_name_for(app_id) if app_id is not None else None
        if mine is not None and reg is not None and _registry_serves_and_is_ready(reg, mine):
            if not stamp_is_proven(reg):
                # THE CONTAINER EXISTS AND HAS NEVER ANSWERED A REQUEST. This is the whole
                # citizen-visible fix: until this arm, `state=ready` — an ACA container was
                # SCHEDULED — was reported as ALIVE with a framable URL, and on 2026-09-10 a
                # citizen's pane spent eight seconds framing nginx's "This app isn't running
                # right now" page while the live region announced the preview was live.
                #
                # WHY IT SITS HERE AND NOWHERE ELSE, three times over:
                #  * ABOVE `restorable_presence`, so the whole pre-serve window — every 3s
                #    accelerated poll of it — costs no object-store HEAD. Placing it after that
                #    call would quietly move the build window onto the expensive path.
                #  * INSIDE this block, so the app-name/state comparison happens exactly once.
                #    Two hand-written copies of "is this mine and ready" answer differently the
                #    first time one is updated alone.
                #  * ABOVE the `starting` marker check below, which takes the 300s marker TTL
                #    off the screen entirely: a container that outlives its marker without ever
                #    serving still reads as a wait, instead of falling through to SLOT_TAKEN or
                #    offering a Launch button in the middle of the citizen's own build.
                #
                # No `preview_url` and no `restorable`: STARTING's existing defaults are already
                # the honest answer (nothing to frame, no restore offered mid-start).
                return PreviewState(state=PreviewLifeState.STARTING)
            fqdn = reg.get(REGISTRY_FIELD_FQDN)
            # THE HOT PATH, AND IT SPENDS NOTHING ON THE STORE. `restorable` stays `None` —
            # "no claim" — because a running app renders no restore affordance for the answer
            # to change (see the docstring). The client's `restorable ?? hasSavedBuild` reads
            # this as "the poll did not say", exactly as it reads an unreachable store.
            return PreviewState(
                state=PreviewLifeState.ALIVE,
                # The PUBLIC address, composed from the app name rather than the registry
                # FQDN. This site builds no `SandboxHandle`, so it is invisible to anything
                # that follows the handle's field — and it is what the cockpit frames, so
                # getting it wrong shows a blank preview over a perfectly healthy container.
                preview_url=settings.app_url(mine) if fqdn else None,
                serving_since=an_instant_on_the_hash(reg, REGISTRY_FIELD_SERVING_SINCE),
            )
        if starting is not None:
            # A start is in flight for THIS user, and it was not the one just ruled ALIVE
            # above (a stale marker never wins against a serving registry). Naming it directly
            # from the marker's own payload is the whole improvement over the registry-only
            # SLOT_TAKEN arm below: no app-name inversion, no ghost, because the marker already
            # says which project it is.
            if starting == project_id:
                return PreviewState(state=PreviewLifeState.STARTING)
            occupying_name = await _project_name_owned_by(db, user.id, starting)
            return PreviewState(
                state=PreviewLifeState.SLOT_TAKEN,
                occupying_project_id=starting,
                occupying_project_name=occupying_name,
                restorable=(await restorable_presence(app_id) if app_id is not None else False),
            )
        if app_id is None:
            # NEVER BUILT — BUT ONLY IF THE WORKSPACE IS NOT SOMEBODY ELSE'S, and that ordering is
            # the whole of this arm.
            #
            # This return used to sit ABOVE the slot-taken check below, which made SLOT_TAKEN
            # structurally unreachable for a project that had never built — the project most
            # likely to hit it. What a citizen actually got: "Describe what you want to build."
            # over a workspace another project was holding, and because the composer rolls its
            # bubbles back when the server refuses the start, their first message VANISHED. No
            # message, no error, no card, nothing to press. Measured on 2026-09-10: two of three
            # real runs, because holding one project open is the normal state of things.
            #
            # `restorable=False` stays a CONFIRMED absent on both paths: no app row means no
            # bundle key can exist, so skipping the store call is an answer rather than an
            # omission (the same reading `get_project` makes).
            held = reg.get(REGISTRY_FIELD_APP_NAME) if reg is not None else None
            if held is None:
                return PreviewState(state=PreviewLifeState.NEVER_BUILT, restorable=False)
            occupier = await _occupying_project(db, user.id, held)
            return PreviewState(
                state=PreviewLifeState.SLOT_TAKEN,
                occupying_project_id=occupier.project_id if occupier else None,
                occupying_project_name=occupier.project_name if occupier else None,
                restorable=False,
            )
        # Everything below is a workspace that is NOT serving this project and has no start in
        # flight — which is the only place the restore offer is rendered, so this is the one
        # place the answer earns its round trip.
        restorable = await restorable_presence(app_id)
        if reg is None:
            return PreviewState(state=PreviewLifeState.ASLEEP, restorable=restorable)
        live_app = reg.get(REGISTRY_FIELD_APP_NAME)
        if live_app == mine:
            # Ours, but mid-teardown (`ending`) — from the builder's side that is a workspace
            # going to sleep, not a workspace somebody stole. The next prompt brings it back.
            return PreviewState(state=PreviewLifeState.ASLEEP, restorable=restorable)
        occupier = await _occupying_project(db, user.id, live_app or "")
        return PreviewState(
            state=PreviewLifeState.SLOT_TAKEN,
            occupying_project_id=occupier.project_id if occupier else None,
            occupying_project_name=occupier.project_name if occupier else None,
            restorable=restorable,
        )

    async def reclaim_preflight(
        self,
        db: AsyncSession,
        user: User,
        project_id: uuid.UUID,
        *,
        sandbox_client: SandboxClient,
    ) -> None:
        """The guard asked BEFORE the 202 so the answer can be an HTTP 409. `ensure_sandbox`
        runs inside the detached turn task, where a raise becomes a chat message and the
        client has nothing to act on — no status to branch on, no project id to name, no way
        to offer Save. The same question asked here, beside the route's other cheap
        synchronous gates, gives the client a real refusal it can turn into a choice. The
        guard inside `ensure_sandbox` stays: this one is an early, kind answer, not the
        enforcement — anything that changes between the two is caught there."""
        spare_app = await _sandbox_name_for_existing_app(db, user.id, project_id)
        await self._refuse_if_reclaim_would_destroy_work(
            db, user, spare_app=spare_app, sandbox_client=sandbox_client
        )

    async def release_project_sandbox(
        self,
        db: AsyncSession,
        user: User,
        project_id: uuid.UUID,
        *,
        sandbox_client: SandboxClient,
    ) -> bool:
        """Give up this project's container, on the user's explicit say-so — the teardown the
        start path used to do behind their back, moved into an action they take. `reap_user`
        is reused verbatim (mark-ending, teardown, clear registry, release lock). Refuses
        while a build is genuinely running for this user; returns False when there is
        nothing to release, reported as a plain success. `strict=True` keeps that true: the
        lenient default would collapse "nothing registered" and "teardown failed" into the
        same False, sending the caller straight back into a reclaim refusal it was told had
        been cleared — strict re-raises instead, and the router turns it into a 503.

        ACCEPTS EITHER LINEAGE IN THE SLOT (#198, R16's acceptance example: "when the reaper
        sweeps it OR the release path runs, the container is actually deleted"). The registry
        this reads is keyed by `user.id` alone — there is exactly one entry per user — so
        whatever name it holds is unambiguously THIS caller's, whether that is `project_id`'s
        own `sbx-` container or a colleague's `shr-` view they have open. Before this, a `shr-`
        occupant failed the `app_name_for(app_id)` comparison and the function returned False
        without reaping anything: a recipient whose slot held a shared view had no route back
        to their own build sandbox, and Azure/Redis both still showed the container live."""
        async with self._start_lock_for(user.id):
            if user.id in self._active_by_user:
                raise BuildSessionConflictError(self._active_by_user.get(user.id))
            app_id = await _existing_app_id(db, user.id, project_id)
            if app_id is None:
                return False
            redis = get_redis()
            reg = await read_registry(redis, user.id)
            if reg is None or reg.get(REGISTRY_FIELD_STATE) != REGISTRY_STATE_READY:
                return False
            occupied_by = reg.get(REGISTRY_FIELD_APP_NAME, "")
            if occupied_by != app_name_for(app_id) and not is_a_shared_sandbox_name(occupied_by):
                return False
            return await reap_user(redis, user.id, sandbox_client, strict=True)

    async def give_up_shared_view(
        self, user_id: uuid.UUID, *, sandbox_client: SandboxClient
    ) -> bool:
        """Give up whatever shared view currently holds the caller's own slot (#198, requirement
        24's self-service exit). NEEDS NO `project_id` AND NO OWNERSHIP CHECK — the registry this
        reads is keyed by `user_id` alone, so whatever it names is already unambiguously theirs to
        release, and that is the whole reason this exists: `release_project_sandbox` still
        requires a `project_id` the caller OWNS to even ask the question, which a recipient who
        has never built anything of their own cannot supply, and `SandboxReclaimBlockedError`'s
        occupant for a shared view names the OWNER's project — never the recipient's — so
        neither `stopActiveBuild` nor `release` can be reached with an id that passes
        `owned_project_or_404`. A recipient's hand-over dialog needs a door that asks nothing but
        "is a shared view sitting in MY slot right now", and this is it.

        Returns `False`, not an error, when the slot holds nothing (already gone) or holds the
        caller's OWN build sandbox instead (nothing of this action's business — `sbx-` names are
        `release_project_sandbox`'s job). `strict=True` mirrors that function's own reasoning:
        the citizen is about to retry whatever the reclaim refusal blocked, and a still-standing
        container would walk them right back into it."""
        redis = get_redis()
        async with self._start_lock_for(user_id):
            if user_id in self._active_by_user:
                raise BuildSessionConflictError(self._active_by_user.get(user_id))
            reg = await read_registry(redis, user_id)
            if reg is None or reg.get(REGISTRY_FIELD_STATE) != REGISTRY_STATE_READY:
                return False
            if not is_a_shared_sandbox_name(reg.get(REGISTRY_FIELD_APP_NAME, "")):
                return False
            return await reap_user(redis, user_id, sandbox_client, strict=True)

    async def revoke_shared_preview(
        self,
        recipient_id: uuid.UUID,
        owner_app_id: uuid.UUID,
        *,
        sandbox_client: SandboxClient,
    ) -> bool:
        """Tear down `recipient_id`'s live view of a project shared with them, if their
        per-user slot currently holds exactly that container — #198's fill-in for Slice 1's
        `:unshare` teardown seam (requirement 25). `release_project_sandbox`'s own shape,
        with the two differences a revoke's caller demands: the OWNER calls this, never the
        recipient, so there is no `_active_by_user` conflict to raise here — nobody is
        competing for their own slot. And it must NOT refuse merely because the recipient is
        mid-build on a project of their OWN: that build's container simply is not the shared
        name being asked about, so the identity check below already answers False for it,
        which is the correct "nothing of this share's to revoke" outcome, not an error.

        Holds the recipient's OWN start lock — the same one `launch_shared_preview` and any
        build of theirs would hold — so this can never race a concurrent Launch/Refresh into
        tearing down a container mid-provision. `strict=True`, matching
        `release_project_sandbox`: the router is about to act on the outcome, and a failed
        teardown must reach it as a 503 rather than a silent False."""
        redis = get_redis()
        shared_name = shr_name_for(owner_app_id, recipient_id)
        async with self._start_lock_for(recipient_id):
            if not await _the_live_sandbox_is_already_the_one_we_want(
                redis, recipient_id, shared_name
            ):
                return False
            return await reap_user(redis, recipient_id, sandbox_client, strict=True)

    def _live_session_for(self, user_id: uuid.UUID, app_id: uuid.UUID) -> BuildSession | None:
        """The in-process session holding THIS app's container, if there is one.

        The same authority `_claim_the_one_build_slot` trusts, and for the same reason: a live
        session is the one fact about a container that Redis cannot be asked, because a lapsed
        lock or a stale registry hash says nothing about whether a task is running in this
        process. Single-replica is the deploy invariant that makes it sufficient (see
        `_claim_the_one_build_slot`); a second replica needs the shared lease the idle-suspend
        spike calls a prerequisite, not a second guard bolted on here.

        ASKS ABOUT THE APP RATHER THAN THE TASK, which is what let it cover both kinds of
        session while there were two: `_start_locked` registered a build carrying a `run_build`
        task, `ensure_sandbox` registers a turn's workspace with none. Only the second kind
        exists now, and the question is still the right one to ask."""
        session_id = self._active_by_user.get(user_id)
        if session_id is None:
            return None
        session = self._sessions.get(session_id)
        return session if session is not None and session.app_id == app_id else None

    def _live_session_holds(self, user_id: uuid.UUID, app_id: uuid.UUID) -> bool:
        """Is ANY session holding this app's container — read-only turns included?

        The right question for stopping, which is about freeing the slot: an Ask turn holds the
        container just as firmly as a build, and `release` refuses for either."""
        return self._live_session_for(user_id, app_id) is not None

    def _writing_session_holds(self, user_id: uuid.UUID, app_id: uuid.UUID) -> bool:
        """Is an agent actually WRITING into this app's container right now? THE NARROWER
        QUESTION two callers actually mean: `_pin_workspace` attaches the live container for
        EVERY mode, so "attached" is true throughout an ordinary Ask or Plan turn too, and
        answering with that once refused the Save button on a read-only question. `may_write`
        comes from the kind's tool surface, so this is structural, not a guess. Deliberately
        NOT `workspace_touched` (the live "has it written yet?" flag): that turns true only
        after the first write, so a save admitted on it could still land mid-write — the
        toolset is fixed for the whole run, the property a guard needs."""
        session = self._live_session_for(user_id, app_id)
        return session is not None and session.may_write

    async def _attach_for_read(
        self, user_id: uuid.UUID, app_id: uuid.UUID, sandbox_client: SandboxClient
    ) -> SandboxHandle:
        """A handle on the project's live container, or `NoLiveSandboxError`.

        Prefers the in-process session's handle when there is one (mid-turn), and otherwise
        attaches through the registry (between turns, the pardoned container). Refuses when the
        registry names a DIFFERENT app — saving project A's tree under project B's id would be
        the worst possible outcome of a convenience."""
        session_id = self._active_by_user.get(user_id)
        live = self._sessions.get(session_id) if session_id is not None else None
        if live is not None and live.app_id == app_id and live.handle is not None:
            return live.handle
        if not await _the_live_sandbox_is_already_the_one_we_want(
            get_redis(), user_id, app_name_for(app_id)
        ):
            raise NoLiveSandboxError(app_id)
        try:
            return await sandbox_client.attach_existing(str(user_id))
        except SandboxGoneError as exc:
            # CERTAIN. The client raises this only when it has confirmed absence — ARM says the
            # revision does not exist, the registry is empty, or the reaper already marked it
            # ending. `client.py` draws exactly this line and says why: "a container ARM
            # confirms is gone has nothing to lose... while a container we merely cannot
            # authenticate to right now must NOT be destroyed over a transient control-plane
            # failure." Certain absence is the plain parent.
            raise NoLiveSandboxError(app_id) from exc
        except SandboxError as exc:
            # UNKNOWN — `SandboxNotReadyError` and friends, which the same client raises when it
            # could NOT confirm anything. The registry named this app a moment ago, so the
            # container is supposed to be there and may well be, holding work. Callers that only
            # want "no handle" catch the parent and are unaffected; the reclaim guard catches
            # this subclass and refuses rather than guess.
            raise SandboxUnreachableError(app_id) from exc

    async def _retract_the_proof_and_keep_watching(
        self,
        sandbox_client: SandboxClient,
        handle: SandboxHandle,
        redis: aioredis.Redis,
        user_id: uuid.UUID,
        *,
        app_name: str,
        already_waited_s: float,
        cold: bool,
    ) -> None:
        """The remedy for a relaunch holding a framable URL with NOTHING PAINTING BEHIND IT:
        retract the standing serving proof, then keep watching for the page, detached.

        ONE IMPLEMENTATION FOR BOTH WAYS IN, and that is the point rather than tidiness. Two
        readings end here — the readiness wait lapsing, and the app root answering with something
        that is not a page — and they want the identical outcome: keep the container, hand the
        URL back with `ready=False`, and let the pane's labelled wait carry the seconds. While
        those two were separate hand-written copies, the second one reached its outcome by
        raising into the first one's handler, whose opening line re-raises on a cold relaunch —
        so a container that had just been restored was torn down for answering 404.

        RETRACTING THE PROOF IS THE ONE THING OUTSIDE A LIVE TURN THAT CAN. The container this
        runs against may be one that HAS served, so it carries an ISO stamp nothing else would
        ever clear (the only other clearer, the turn watcher's crash edge, exists only while a
        turn is streaming). Without this the preview-state poll goes on reporting RUNNING and the
        pane frames nginx's "This app isn't running right now" page — the measured defect, one
        door down, with every card that used to cover it deleted.

        IT MARKS NOTHING `ending` AND TEARS NOTHING DOWN, and that is not timidity: condemning a
        container for a slow root GET once cost a citizen their unsaved work, as the fail-open
        handler in `relaunch_preview` records at length. Retracting a claim costs them a card;
        destroying a container costs them their work.

        `cold` rides through to the continuation's log line only, and it is a parameter rather
        than a constant because a RESTORED container can reach here now: a cold start whose first
        page arrived late is exactly what an operator reading that field is looking for."""
        with suppress(RedisError):
            await clear_serving(redis, user_id, app_name=app_name)
        # AND KEEP WATCHING, detached. The wait that just ended is seconds against a container
        # that may simply be compiling a heavy route; the citizen has no patience button and this
        # backend must not need one (decision D2). This is the front half of the five-minute
        # reconciler backstop: same job, sooner, for the citizen who is looking at the pane right
        # now. The `app_first_serve_not_observed` line is deliberately NOT written here — this
        # wait is not over, it is delegated, and the continuation writes it if and only if it
        # really gives up.
        self._keep_watching_for_a_first_serve(
            sandbox_client,
            handle,
            redis,
            user_id,
            app_name=app_name,
            already_waited_s=already_waited_s,
            cold=cold,
        )

    def _keep_watching_for_a_first_serve(
        self,
        sandbox_client: SandboxClient,
        handle: SandboxHandle,
        redis: aioredis.Redis,
        user_id: uuid.UUID,
        *,
        app_name: str,
        already_waited_s: float,
        cold: bool,
    ) -> None:
        """Detach the bounded continuation and return immediately.

        THE SAME SHAPE THE DEPLOY PIPELINE USES — a task that owns its own work while the client
        polls — because the alternative is holding an HTTP request open for two more minutes
        behind an edge that gives it twenty seconds. The strong reference into `self._tasks` is
        what stops the loop garbage-collecting it mid-wait.

        It observes and records. It never marks the registry `ending`, never tears anything
        down, and holds no lock: the relaunch that spawned it has already released one, and a
        watcher that could destroy a container is a watcher that can destroy the citizen's
        unsaved work on a slow route."""
        watcher = asyncio.create_task(
            self._watch_for_a_first_serve(
                sandbox_client,
                handle,
                redis,
                user_id,
                app_name=app_name,
                already_waited_s=already_waited_s,
                cold=cold,
            )
        )
        self._tasks.add(watcher)
        watcher.add_done_callback(self._tasks.discard)

    async def _watch_for_a_first_serve(
        self,
        sandbox_client: SandboxClient,
        handle: SandboxHandle,
        redis: aioredis.Redis,
        user_id: uuid.UUID,
        *,
        app_name: str,
        already_waited_s: float,
        cold: bool,
    ) -> None:
        """Keep asking whether the app has started SHOWING A PAGE, for one more cold budget, then
        stop — either way with a line saying which. NEVER RAISES: nothing awaits this task, so
        an escaping exception would surface only as an un-retrieved-exception warning at
        collection time, long after the fact and with none of the context.

        IT READS `shows_a_page` ITSELF, AND NEVER BORROWS `wait_ready`'s VERDICT. That borrowing
        was this task's own defect: `wait_ready` returns on `/dev/status.ready`, which the
        supervisor keeps fail-open on purpose (ANY response counts, 404 and 500 included), while
        the page check that spawns this has just REFUSED to call the app ready because its root
        has no page, and cleared the standing proof on the way. One second later the first
        iteration wrote that proof straight back over the same 404, the poll flipped to RUNNING,
        and the pane framed the blank container the refusal had just saved the citizen from.
        Every other stamp site on this branch gates on `shows_a_page`; this is the fifth and it
        does too.

        WHY IT ASKS AGAIN AT ALL, given the wait it follows already ended: that wait is bounded by
        the caller's budget — 15 seconds on an attach, or one reading taken the instant a restored
        container answered — and `shows_a_page` describes only this moment, so a heavy dashboard
        route compiling under 1.0 vCPU shows no page for far longer than that without anything
        being wrong. The alternative for the citizen is a pane that waits until the five-minute
        reconciler sweep notices. The third caller asks for a different reason: it could not reach
        the supervisor for that one reading at all, and it wants the question asked again rather
        than answered by a transport error.

        ONE SUPERVISOR CALL PER SECOND, down from the two per iteration the borrowed
        `wait_ready(timeout_s=1.0)` cost — it polled inside its own one-second budget and then
        this loop slept for another. The observable cadence is unchanged; the load is halved.

        BOUNDED, AND THE BOUND IS THE POINT: one cold budget, after which it stops whatever it
        has seen. A second press of the control while this one is still watching does start a
        second watcher, and that costs nothing — the stamp is first-serve-wins, so the loser
        writes nothing and says nothing, and each watcher still dies on its own deadline. The
        reconciler is what covers everything they all miss."""
        loop = asyncio.get_running_loop()
        started_at = loop.time()
        deadline = started_at + _COLD_READY_BUDGET_SECONDS
        try:
            while True:
                try:
                    it_paints = (await sandbox_client.dev_status(handle)).shows_a_page
                except SandboxError:
                    # A transport failure is not a statement about the app — the supervisor may
                    # be busy or the container mid-restart — so it costs an iteration and nothing
                    # more, and it can never be read as a sighting: "we could not ask" has never
                    # been the same as "we watched it paint". The deadline below is what ends
                    # this, never one bad answer. No `SandboxNotReadyError` arm sits above this
                    # one any more — `dev_status` does not raise it, and as a SUBCLASS it would
                    # land here regardless, which is the correct home for it.
                    it_paints = False
                if it_paints:
                    # `cold` comes from the caller rather than being assumed False here: the page
                    # check that spawns this can now hand over a RESTORED container too, and a
                    # cold start whose first page arrived late is exactly what an operator
                    # reading this field is looking for.
                    await _record_the_first_serve(
                        redis,
                        user_id,
                        app_name=app_name,
                        observer="relaunch_continuation",
                        cold=cold,
                    )
                    return
                if loop.time() >= deadline:
                    break
                await asyncio.sleep(READINESS_POLL_S)
            dev_running, dev_compile = await _last_supervisor_reading(sandbox_client, handle)
            # NOT A CLAIM THAT THE APP IS DEAD. The reconciler's sweep may still stamp this
            # container minutes from now, and `app_first_served` with `observer=reconciler` is
            # how that shows up. What this line says is that for this long, nothing the platform
            # runs watched the app SHOW A PAGE — which for two of the three callers means a
            # citizen sat in front of a wait card for the whole of it.
            _log.warning(
                APP_FIRST_SERVE_NOT_OBSERVED_EVENT,
                waited_ms=int((already_waited_s + (loop.time() - started_at)) * 1000),
                budget_ms=int((already_waited_s + _COLD_READY_BUDGET_SECONDS) * 1000),
                arm="relaunch_continuation",
                dev_running=dev_running,
                dev_compile=dev_compile,
                app_name=app_name,
                user_id=str(user_id),
            )
        except asyncio.CancelledError:
            # Shutdown. Leave everything exactly as it is — this task owns no state to unwind,
            # and the five-minute reconciler is the backstop for the proof it did not take.
            raise
        except Exception:
            _log.exception(
                "the first-serve continuation failed; the reconciler's sweep is the backstop",
                user_id=str(user_id),
                app_name=app_name,
            )

    async def relaunch_preview(
        self,
        db: AsyncSession,
        user: User,
        project_id: uuid.UUID,
        sandbox_client: SandboxClient,
        *,
        prefer_saved: bool = False,
    ) -> RelaunchedPreview:
        """Put a READY sandbox in front of a project's saved app — for an app whose live build
        session has already been torn down.

        Resumes the NEWEST tree by default; `prefer_saved` is the user's explicit "put my last
        saved version back" and is the only way to get the older one. The default is inverted
        from the obvious reading on purpose: the failure that costs a user their work is
        restoring an older tree over a newer one, and the failure that costs them nothing is
        showing them their own most recent workspace. Neither is a promotion — `snapshot_key`
        is untouched either way, so `dirty` stays true and Save is still their click.
        `save_state.recoverableWorkAt` is what lets the portal offer the choice.

        TWO ARMS, cheapest first. If the container serving this exact app is already up and
        healthy, ATTACH to it and drive the dev server; only otherwise restore the snapshot
        into a fresh one. The attach arm exists because the button's most common use is a
        repeat — click it twice, or re-open the tab — and the old single arm answered that by
        paying a ~20s ACA delete plus a ~33.5s ACA create to arrive back at the state it
        deleted, while the user waited and watched their running app get demolished. It is
        bounded by one process lifetime: the supervisor bearer stays in-process by design, so
        the first relaunch after a deploy resolves no token, falls through, and restores.
        (Trade-off recorded, not buried: an attached container keeps its BIRTH env, so a
        relaunch no longer rotates the Blob SAS.)

        Deliberately NOT a build: it runs under `_holding_user_lock` — the same skeleton the
        deleted `_start_locked` ran under — but never adopts the lock, never enters
        `_active_by_user` and never spawns a background task, so it does NOT occupy the
        one-per-user build slot — the user's next real build never 409s on a relaunched preview. It
        registers a READY handle in Redis (a side effect of `restore_from_snapshot` via
        `_write_registry`), seeds a heartbeat, then the scope RELEASES the per-user lock on
        exit. Nothing here re-snapshots: the workspace is served read-only (an edit is a new
        build, which finalizes normally), so `_do_finalize` — the only writer of a snapshot —
        is never on this path.

        Because it holds no lock and renews no heartbeat, its container's lifetime is owned
        by an explicit STAY OF EXECUTION granted below: a bounded lease on the registry hash
        that the background sweep honors and then reaps through. Reconcile-on-start reaps it
        immediately regardless of the lease — that build needs the one-per-user slot, and
        sparing the preview there would orphan its container under the new registry entry.

        Diverged from the deleted `_start_locked` in two deliberate ways, and both still hold
        against `_claim_the_one_build_slot`, which is where that logic lives:
        - No finalize-grace wait on a terminal-committed session: the snapshot relaunch would
          restore is written only by that session's finalize, so 409ing until it settles is
          correct — never unify this with the `_FINALIZE_GRACE_SECONDS` arm.
        - It must NOT reuse `_restore_or_provision`, whose confirmed-absent arm provisions a
          BLANK template — the wrong answer for relaunch, where an empty app is not a preview
          of the user's work. Instead it checks the snapshot itself and restores directly:
          confirmed-absent (or vanished) snapshot → `NoSnapshotToRelaunchError` (router 404);
          transient/unknown snapshot state, or a restore that fails every attempt →
          `SnapshotUnavailableError` (router 503); a live build already active for this user →
          `BuildSessionConflictError` (router 409).

        The snapshot gate stays ABOVE both arms and above the commit, unmoved: a project with
        nothing saved is a 404 whether or not a container happens to be up, and that answer
        must not persist the speculative DRAFT app row.

        MEASURED HERE, and the placement of the first emit is the whole of the denominator's
        honesty. `app_start_attempted` fires at ENTRY, above every refusal — the one-slot
        conflict below, the reclaim refusal below that, and the nothing-to-restore 404 under
        both. Each of those is a press that could have started something and did not — this
        ratio is the difference between pressing the control and seeing the app — so excluding
        them would make it read flatteringly high, which is the one failure mode a measurement
        cannot afford. The cost, stated: at this point no app is resolved yet
        (`resolve_app_for_project` runs inside the user lock), so the attempted row carries no
        `app_id`. A complete denominator is worth more than attribution on a row that only ever
        means "someone pressed".

        WHAT THAT PLACEMENT ALSO COUNTS, so the denominator is read for what it is. It is one row
        per REQUEST that got this far, not strictly one per press of a citizen's own control:
          * Ownership is established INSIDE the lock (`_sandbox_name_for_existing_app` and
            `resolve_app_for_project` are the user-scoped reads), so a request naming another
            user's project — or a project id that does not exist — is counted and then answered
            404. The portal never sends one; a hand-made request can. It inflates the denominator,
            so the ratio errs LOW, which is the safe direction for a number nobody should flatter.
          * A reclaim refusal followed by the citizen confirming through it books TWO attempted
            rows and one reached-running for ONE journey. That is the same press-that-did-not-
            start rule applied twice and is correct per-press; it just means the ratio has a
            known floor on that path rather than being a clean per-journey one.
          * The router's own `sandbox is None -> 503` sits ABOVE this method, so "above every
            refusal" is true within the manager and not of the endpoint.
        """
        # ONE RELAUNCH, ONE GREP. The binding lives here, wrapping the whole implementation,
        # and not at the router seam: asyncio copies a context at TASK CREATION, so a binding
        # made on the request side would not reliably cover the bounded continuation the body
        # detaches — which is exactly where the lines worth correlating are emitted. It also
        # makes every one of the failure lines already in this file retroactively joinable,
        # with no edit to any of them.
        #
        # `build_id` IS A FRESH UUIDv7 rather than anything durable: a relaunch is its own
        # attempt at bringing a workspace up, and two presses of the same button are two
        # stories an operator must be able to tell apart. (A turn binds its turn_id for the
        # same reason — one identifier per attempt, never per app.) `app_id`/`app_name` are
        # deliberately NOT bound: neither exists until `resolve_app_for_project` runs inside
        # the lock, well after the first line is written, so the lines that have them pass
        # them as fields instead of half the build carrying an empty binding.
        with _one_relaunch_in_the_log(
            build_id=str(uuid.uuid7()), user_id=str(user.id), project_id=str(project_id)
        ):
            return await self._relaunch_under_one_build_id(
                db, user, project_id, sandbox_client, prefer_saved=prefer_saved
            )

    async def _relaunch_under_one_build_id(
        self,
        db: AsyncSession,
        user: User,
        project_id: uuid.UUID,
        sandbox_client: SandboxClient,
        *,
        prefer_saved: bool,
    ) -> RelaunchedPreview:
        """The whole of `relaunch_preview` — go there for what it does and why it does it that
        way; this half is the same code, one indent level out.

        SPLIT FOR ONE REASON: the correlation binding needs a block to own, and a `with` around
        four hundred lines of a method someone has to read is worse than a named seam that says
        what the binding covers. Never call it directly — a relaunch that runs without a
        `build_id` bound writes lifecycle lines nothing can join back up, which is the failure
        this whole log model exists to end."""
        await count(HarnessCounter.APP_START_ATTEMPTED)
        async with self._start_lock_for(user.id):
            redis = get_redis()
            user_id = user.id
            if user_id in self._active_by_user:
                # The SAME choice `_claim_the_one_build_slot` makes, and relaunch is the
                # door the citizen actually walks through: the rail composer preflights this
                # route before it opens a chat, so this is the refusal that reaches the screen
                # first. A different project holding the slot earns the hand-over dialog, not
                # "try again" advice that cannot come true while that build runs.
                blocking_id = self._active_by_user.get(user_id)
                raise await self._slot_conflict_for(
                    user_id,
                    self._sessions.get(blocking_id) if blocking_id is not None else None,
                    blocking_id,
                    db,
                    project_id,
                )
            # WHICH container would satisfy this relaunch? Read-only on purpose, and computed
            # out here because it has to be: `app_id` is not bound until inside the lock, and
            # `resolve_app_for_project` is an UPSERT that mints a DRAFT row — so it can never
            # be the source of a spare name for a request that may still be refused. Without
            # this the lock's reconcile reaped the very container the attach arm below is
            # about to reuse (`_the_live_sandbox_is_already_the_one_we_want` answers False for
            # `spare_app=None`), which is the 20-second ACA delete half of the attach arm's cost.
            spare_app = await _sandbox_name_for_existing_app(db, user_id, project_id)
            # Same guard, same reason as `ensure_sandbox`: Relaunch is the other door
            # into the one slot, and it reclaimed just as silently.
            await self._refuse_if_reclaim_would_destroy_work(
                db, user, spare_app=spare_app, sandbox_client=sandbox_client
            )
            async with self._holding_user_lock(
                redis, user_id, sandbox_client, project_id, arm="relaunch", spare_app=spare_app
            ) as scope:
                app_id = await resolve_app_for_project(db, user_id, project_id)
                # The snapshot gate runs BEFORE the commit and the storage provision: the 404
                # path must not persist the speculative DRAFT app row (`get_db` rolls the
                # uncommitted insert back) nor provision blob storage for an app that was
                # never built. No fresh-provision fallback: a confirmed-absent bundle is a
                # dead end (404), never a blank template.
                # Gate on EITHER bundle. Gating on the saved one alone told the user who
                # built an app across several turns and never clicked Save — the expected
                # behaviour for a non-developer, not an edge case — to "build the app first",
                # while `save-state` was simultaneously reporting that their work existed.
                relaunch_source = await self.newest_restore_source(app_id)
                if relaunch_source is None and not await self._snapshot_exists_or_bust(app_id):
                    raise NoSnapshotToRelaunchError(app_id)
                # The "last saved version" signal: when the newest recorded outcome FAILED,
                # the snapshot being restored is the last SAVED state, not that build's intent.
                # Read here because it must share the request transaction with the gate above;
                # only the RESTORE arm may actually make the claim (see the return below).
                restored_from_failed_build = (
                    await newest_build_outcome_status(db, user_id=user_id, project_id=project_id)
                    is BuildSessionStatus.FAILED
                )
                await db.commit()
                # THE ATTACH ARM. A relaunch onto a container that is already up and serving
                # this very app is the common case behind the button (the user clicks it
                # again, or re-opens the tab), and restoring it cost a 20s ACA delete plus a
                # 33.5s ACA create to arrive back at the state it started in. `_attach_for_read`
                # already asks exactly the right question — same app, registry READY, token
                # resolvable — so reuse it rather than growing a second predicate that can
                # drift from it. `NoLiveSandboxError` is the ONLY exception narrowed here, and
                # it is the union of every honest "no": no registry, a different app, a
                # container mid-teardown, or a control-plane restart that emptied the
                # in-process token map (a known bound — the first relaunch after a deploy
                # still pays a full restore). All four fall through to the untouched restore
                # arm below.
                attached = False
                # The cold-start latency clock, and its two instants are named because the
                # plausible choices differ by tens of seconds. It starts when the RESTORE ARM IS
                # ENTERED (below, in the `NoLiveSandboxError` handler — the attach attempt has
                # just failed and the platform has decided to restore) and stops when
                # `wait_ready` returns.
                # Everything before that first instant — the slot check, the reclaim refusal, the
                # lock wait, app resolution, the snapshot gate, the commit, the attach attempt
                # itself — is OUTSIDE the number, because none of it is a citizen waiting for a
                # container to come up. That is what makes the number quotable as "roughly how
                # long a cold start takes".
                #
                # WHAT IT DOES SPAN, stated because the obvious shorthand is wrong: blob + app-DB
                # provision, `_restore_or_bust`'s bounded retry (the bundle pull, the ACA create,
                # the container's own startup), `dev_start`, and THEN `wait_ready`. Only that last
                # leg carries `_COLD_READY_BUDGET_SECONDS`, so the interval is NOT bounded by it —
                # a slow ACA create lands inside the number, which is correct (the citizen waited
                # for it) but means the budget is not a ceiling on what gets recorded.
                cold_started_at: float | None = None
                cold_elapsed_ms: int | None = None
                try:
                    scope.handle = await self._attach_for_read(user_id, app_id, sandbox_client)
                    # Compensation must now spare this container: it was up before this
                    # request and is not ours to roll back (see `_LockScope.spared`).
                    attached = True
                    scope.spare()
                except SandboxUnreachableError:
                    # UNKNOWN, AND THEREFORE NOT RESTORABLE — caught AHEAD of its parent, which
                    # is the whole of this handler and the reason it exists.
                    #
                    # `SandboxUnreachableError` is a SUBCLASS of `NoLiveSandboxError`, so before
                    # this arm the one case meaning "the container is supposed to be there and
                    # may well be, holding work" was swallowed by the handler written for
                    # "certain absence" and fell straight into the restore arm below — which
                    # tears the live container down before pulling the last saved bundle. That
                    # is the recorded data-loss path, and this is the arm the citizen-facing
                    # start control enters.
                    #
                    # The raising site states the contract in its own words: "Callers that only
                    # want 'no handle' catch the parent and are unaffected; the reclaim guard
                    # catches this subclass and refuses rather than guess." The reclaim guard
                    # does. This path did not, because it was written when relaunch was a rarely
                    # pressed recovery button rather than the front door.
                    #
                    # Refuse, and let it propagate. The route maps it to the same 503 as every
                    # other unreadable-signal answer, whose message is already "a retry is the
                    # way forward" — which is exactly right here: the saved version is intact,
                    # the container may be too, and nothing has been destroyed to find out.
                    # The invariant in one arm: every unreadable arrow leads to escalate, never
                    # destroy.
                    raise
                except NoLiveSandboxError:
                    # THE COLD CLOCK STARTS HERE — see `cold_started_at` above for why this
                    # instant and not function entry.
                    cold_started_at = time.monotonic()
                    # The FIVE injected vars (the two always-present BIAL_* + the two blob
                    # coordinates with a freshly rotated SAS + the per-project DSN), exactly as
                    # a start's birth arm builds them. Deliberately written twice — this must
                    # NOT be unified with `_restore_or_provision` (see the docstring above), so
                    # a var added to only one of the two sites is a silent half-fix. Built only
                    # on THIS arm because a container gets its env exactly once, at birth (ACA
                    # sets vars on the revision, not on a running process) — the same reason
                    # `_resolve_sandbox`'s attach arm forwards none. Consequence, stated rather
                    # than hidden: an attached relaunch reuses the container's birth SAS, so
                    # relaunching no longer rotates it.
                    env = {
                        **build_app_env(app_id),
                        **await provision_app_storage(app_id),
                        **await provision_app_database(db, project_id),
                    }
                    # `_restore_or_bust` re-raises `StorageNotFoundError` (a bundle that
                    # vanished between head-check and pull) — the same 404 bucket.
                    try:
                        # `prefer_saved` is the user's explicit "put my last saved version
                        # back" — the one case where the older tree is what they want. Absent
                        # it, relaunch resumes the newest tree for the same reason every other
                        # restore does.
                        source_key = None if prefer_saved else relaunch_source
                        scope.handle = await self._restore_or_bust(
                            sandbox_client,
                            user_id,
                            app_name_for(app_id),
                            app_id,
                            env,
                            source_key=source_key,
                        )
                    except StorageNotFoundError as exc:
                        raise NoSnapshotToRelaunchError(app_id) from exc
                # THE RESTORE ARM'S LEASE STARTS HERE, before the wait — and ONLY the restore
                # arm's. `_restore_or_bust` has just created the container AND written its
                # registry hash, so from this instant the sweep can see a user whose state
                # reads: registry PRESENT, lock held, heartbeat ABSENT — and `reconcile_user`'s
                # guard is an AND, so lock-held-without-a-heartbeat is REAPABLE. `live_users`
                # does not cover it either: a relaunch never enters `_active_by_user`, by
                # design. Without a stay at this point a concurrent sweep tears down the
                # container we are still bringing up, and this call still returns 200 with a
                # dead preview URL. Seeding the heartbeat early is NOT a substitute:
                # HEARTBEAT_TTL_SECONDS is 90 s while `wait_ready` waits up to 120 s, so the
                # beat can lapse mid-wait.
                #
                # THE ATTACH ARM DELIBERATELY DOES NOT GRANT HERE, and that asymmetry is the
                # anti-trap: a lease granted before the wait is a lease every FAILED retry
                # re-grants, so a container whose dev server will not come up refreshed its own
                # 30-minute reprieve on each press of the recovery button. Marking the registry
                # `ending` (below) closes that for the one failure shape it can name; declining
                # to spend the lease before the container has earned it closes it for ALL of
                # them — a bare `SandboxError`, an unreachable supervisor, a client disconnect —
                # without adding a single new path that condemns a container. Nothing is lost by
                # waiting: the attach arm attached to a container that is READY in the registry,
                # which means it is already inside somebody's lease (a previous relaunch's, or
                # the pardon a completed build granted it). Its lease reaching the sweep before
                # ours is granted means the container was already due, and the honest answer to
                # that is the restore arm on the next press — never a wedge.
                if not attached:
                    await grant_stay_of_execution(
                        redis, user_id, writer=DeadlineWriter.BUILDER_ACTED
                    )
                # `restore_from_snapshot` returns a ready=False handle; without dev_start +
                # wait_ready the fresh preview URL 404s. This is the step restore omits.
                #
                # On the ATTACH arm it is an optimization instead, and it is NOT unconditionally
                # idempotent — so it fails open (ambiguity denies). The supervisor answers
                # `/dev/start` with TWO different 409s (`sandbox/supervisor/app.py`): the
                # owned-child one reports `running=True` and the client folds it into the
                # already-running sentinel, but the UNOWNED-SERVER one — the dev port is serving
                # while `_Dev.proc` is dead, which is exactly what the agent leaves behind when
                # it starts its own server through the open-sandbox `run_command` surface —
                # reports `running=False` and the client raises `SandboxError`. Unguarded that
                # would land in compensation, i.e. we would destroy a container for the sin of
                # already serving the page we came to show. `wait_ready` below is the real gate
                # either way, and it answers from the server that is up.
                # Flipped to False by either reading that says the URL is framable with nothing
                # painting behind it — the attach arm's readiness wait lapsing, or the app root
                # answering with something that is not a page, which happens on EITHER arm. It
                # rides out on the response so the pane can label a preview that is framable but
                # not yet serving, instead of being told "ready" and framing a hang.
                ready = True
                # DID ANYTHING WATCH THIS CONTAINER ANSWER WITH A PAGE? A different fact from
                # `ready`, and keeping them apart is a fix rather than a nuance. `ready` is what
                # the citizen is handed; this is the only thing the registry's serving proof may
                # be written from. They agree on every arm but one — the supervisor blip below,
                # where the platform could not ask and must therefore neither demote a preview
                # that may be painting nor mint a proof for a container nothing has ever watched
                # serve.
                something_watched_it_paint = False
                try:
                    await sandbox_client.dev_start(scope.handle)
                except SandboxError:
                    if not attached:
                        raise  # a fresh container with no dev server has nothing to preview
                    _log.warning(
                        "relaunch_dev_start_refused_on_attached_container",
                        user_id=str(user_id),
                        app_id=str(app_id),
                        exc_info=True,
                    )
                # The readiness wait's own clock, for the continuation's arithmetic. The page
                # check below can end this wait early, so the budget constant is NOT the elapsed
                # time on that path, and a give-up line quoting it would overstate how long the
                # citizen actually waited.
                readiness_started_at = time.monotonic()
                # THE WAIT AND NOTHING ELSE INSIDE THE TRY. The page check lives in the `else`
                # arm, where a `SandboxError` from it cannot reach this handler and this handler
                # cannot be reached by anything but a readiness TIMEOUT — the one condition its
                # `if not attached: raise` was written for. The first version of that check
                # raised `SandboxNotReadyError` from inside this try to reuse the handler below:
                # `SandboxNotReadyError` SUBCLASSES `SandboxError`, so the raise had to dodge its
                # own transport handler, and the handler it landed in re-raises on a cold
                # relaunch — escaping `_holding_user_lock` before `scope.spare()` and letting
                # compensation TEAR DOWN the container `_restore_or_bust` had just built. A blank
                # pane costs the citizen a card; that cost them the restored workspace and a 503.
                try:
                    scope.handle = await sandbox_client.wait_ready(
                        scope.handle,
                        timeout_s=(
                            _ATTACHED_READY_BUDGET_SECONDS
                            if attached
                            else _COLD_READY_BUDGET_SECONDS
                        ),
                    )
                except SandboxNotReadyError:
                    # AMBIGUITY DENIES, AND WE PAID FOR THIS ONE IN LOST WORK.
                    #
                    # This handler used to `mark_registry_ending` here and re-raise. A run
                    # against real Azure showed what that costs: `attach_existing` refuses an
                    # `ending` sandbox BEFORE it probes (`services/sandbox/client.py`), so the very
                    # next press took the RESTORE arm — and restore tears the live container down
                    # (`_safe_teardown`) before pulling the last SAVED bundle. Two clicks, and a
                    # citizen's unsaved edits were gone with nothing on screen to say so. The 503
                    # this used to raise is the copy that invited the second click.
                    #
                    # The mistake was reading a readiness timeout as a statement about the
                    # CONTAINER. It is a statement about the generated APP: `ready` means
                    # a request was actually served, so any root route slower than the supervisor's
                    # read timeout reports un-ready forever. A heavy dashboard query or a cold
                    # compile under 1.0 vCPU is enough. Condemning the container for that condemns
                    # the user's work for the sin of rendering slowly.
                    #
                    # So the ATTACH arm fails open: keep the container, leave the registry `ready`,
                    # and hand back the framable URL with `ready=False`. The pane already owns a
                    # labelled wait; this destroys nothing and forecloses nothing — the next press
                    # attaches again rather than restoring. The wedge the `ending` mark was added
                    # to break is still broken, by the lease we declined to grant before the wait:
                    # that lapses on its own and covers EVERY way this wait can end, not just the
                    # one shape this handler could name.
                    #
                    # The COLD arm still raises. A container we just provisioned that never came up
                    # holds no unsaved work and has nothing framable to offer, so an error is the
                    # honest answer there.
                    #
                    # AND THAT SENTENCE IS THE WHOLE SCOPE OF THIS ARM: "never came up". A
                    # container that DID come up and answers the root with a 404 is a different
                    # condition entirely — it is up, it holds the tree just restored into it, and
                    # its URL becomes framable the moment the agent writes a page. Routing that
                    # through here destroys it. It never reaches this handler now; it is answered
                    # in the `else` arm below, with the same remedy on both arms.
                    if not attached:
                        raise
                    ready = False
                    _log.warning(
                        "relaunch_attached_container_not_serving_degraded_to_unready",
                        user_id=str(user_id),
                        app_id=str(app_id),
                        budget_s=_ATTACHED_READY_BUDGET_SECONDS,
                    )
                    # THE REMEDY IS SHARED WITH THE PAGE CHECK BELOW, and it is one function
                    # so that the two arms cannot drift: retract the standing proof, keep the
                    # container, keep watching. Every "why" behind those three lives in
                    # `_retract_the_proof_and_keep_watching`, including the run against real
                    # Azure that proved the alternative costs unsaved work.
                    await self._retract_the_proof_and_keep_watching(
                        sandbox_client,
                        scope.handle,
                        redis,
                        user_id,
                        app_name=app_name_for(app_id),
                        already_waited_s=_ATTACHED_READY_BUDGET_SECONDS,
                        cold=not attached,
                    )
                else:
                    # …AND IT STOPS HERE, on the statement after the wait returns, so the reading
                    # is the restore-and-wait interval and nothing else. Only the cold arm ever
                    # armed it: a 15-second attach budget and a 120-second cold budget averaged
                    # together produce a number that describes neither.
                    if cold_started_at is not None:
                        cold_elapsed_ms = int((time.monotonic() - cold_started_at) * 1000)
                    # A PAGE, NOT MERELY AN ANSWER — one extra reading, once per press.
                    #
                    # `wait_ready` has just returned on `/dev/status.ready`, which the supervisor
                    # keeps fail-open on purpose: ANY response counts, 404 and 500 included, so a
                    # compile error cannot wedge it False and mislead the model. `shows_a_page`
                    # asks the narrower question the FRAME has to ask — would a citizen opening
                    # this preview right now see a page — and a root answering 404 does not.
                    # Measured on 2026-09-10: a build framed a container whose app root was still
                    # 404ing because the agent had not written `app/page.tsx` yet, and the citizen
                    # got a blank white pane with no words on it.
                    #
                    # THREE READINGS, THREE OUTCOMES, each handled where it is taken and none of
                    # them a `raise` into somebody else's handler.
                    try:
                        dev = await sandbox_client.dev_status(scope.handle)
                    except SandboxError:
                        # WE COULD NOT ASK — a third answer, not a quiet vote for either
                        # neighbour, and it used to be collapsed into the wrong one. Declining to
                        # DEMOTE is right: a supervisor blip is not evidence about the app, so
                        # `ready` stays exactly as the wait left it and a preview that is painting
                        # keeps its frame. But the same fail-open flag also kept `ready` True, and
                        # `ready` was what gated the serving stamp — so one transport error minted
                        # a proof for a container NOTHING HAS EVER WATCHED SERVE, which is the
                        # claim the whole branch exists to make honest.
                        # `something_watched_it_paint` therefore stays False, and the continuation
                        # asks again once a second until it can answer; the reconciler's sweep is
                        # the backstop under that.
                        _log.warning(
                            "relaunch_could_not_ask_whether_the_app_is_showing_a_page",
                            user_id=str(user_id),
                            app_id=str(app_id),
                            attached=attached,
                            exc_info=True,
                        )
                        self._keep_watching_for_a_first_serve(
                            sandbox_client,
                            scope.handle,
                            redis,
                            user_id,
                            app_name=app_name_for(app_id),
                            already_waited_s=time.monotonic() - readiness_started_at,
                            cold=not attached,
                        )
                    else:
                        something_watched_it_paint = dev.shows_a_page
                        if not something_watched_it_paint:
                            # THE SAME OUTCOME ON BOTH ARMS, and never the cold arm's raise. The
                            # container is up and holds the tree; only its root has nothing to
                            # show yet. So it keeps its life and the citizen keeps the URL, with
                            # `ready=False` on it and the pane's labelled wait doing the talking —
                            # the outcome the attach arm has always given this shape.
                            ready = False
                            _log.warning(
                                "relaunch_root_answered_without_a_page",
                                user_id=str(user_id),
                                app_id=str(app_id),
                                attached=attached,
                                root_status=dev.root_status,
                            )
                            await self._retract_the_proof_and_keep_watching(
                                sandbox_client,
                                scope.handle,
                                redis,
                                user_id,
                                app_name=app_name_for(app_id),
                                already_waited_s=time.monotonic() - readiness_started_at,
                                cold=not attached,
                            )
                # Past here the container is up, registered and serving — the same state a
                # SUCCESSFUL relaunch leaves behind — so destroying it over a later blip is no
                # longer a rollback (see `_LockScope.spared`). This matters more now that the
                # warm request below widens the window between "it works" and "we said so".
                scope.spare()
                # …and THIS is where the attach arm's lease is finally spent: the container has
                # now earned it by answering, which is precisely the condition the pre-wait grant
                # could not check. Granted here rather than left to the re-grant below because
                # the warm request sits between the two and can take seconds — long enough for a
                # sweep to reap a container whose previous lease happened to lapse mid-wait.
                if attached:
                    await grant_stay_of_execution(
                        redis, user_id, writer=DeadlineWriter.BUILDER_ACTED
                    )
                # Pay the first route compile before the response carries a preview URL back to
                # a browser that will immediately frame it. `wait_ready` returning means
                # the dev server answers, NOT that this route has been built — Turbopack compiles
                # on first request, and without this the citizen's own GET pays 5-7s of blank
                # white card. Gates nothing and raises nothing; on the attach arm it is usually a
                # no-op against an already-warm container.
                #
                # Skipped when the readiness wait degraded: warming means issuing a root GET, and
                # the only way to get here un-ready is that the root GET is exactly what will not
                # come back. Paying another `_WARM_TIMEOUT_SECONDS` to re-learn that would just
                # delay the URL the citizen is waiting for.
                if ready:
                    await sandbox_client.someone_has_to_go_first(scope.handle)
                preview_url = scope.handle.preview_url
                # Seed the heartbeat INSIDE the protected region (never enter `_active_by_user`,
                # never spawn a finalize task, by design): if it fails, the compensation still
                # tears the container down + releases the lock instead of 500ing with a live
                # container behind a held lock. The scope releases the lock on clean exit.
                await write_heartbeat(redis, user_id)
                # …and RE-grant the bounded lease that actually owns this container's
                # lifetime: nothing renews that heartbeat, so without a stay the background
                # sweep would reap a preview the user is still reading (and without the
                # sweep the container would outlive everyone). Re-granted rather than
                # granted because the provision window above already needed one — this
                # second stamp simply re-bases the 30 minutes on the instant the preview
                # actually became viewable. Inside the protected region for the same reason
                # as the heartbeat: a failure here tears the container down rather than
                # leaving it running with no owner at all.
                await grant_stay_of_execution(redis, user_id, writer=DeadlineWriter.BUILDER_ACTED)
            relaunched = RelaunchedPreview(
                app_id=app_id,
                preview_url=preview_url,
                # NEVER on the attach arm. The flag is a claim about a RESTORE — "what you are
                # looking at is the last SAVED state, not that failed build's intent" — and the
                # attach arm restored nothing: it handed back a container that has been running
                # since before this request, whose workspace may hold edits newer than any
                # snapshot. Labelling that "your last saved version" tells the user their live,
                # unsaved work is old, which is the one thing this banner must never say. The
                # query above cannot be gated instead: which arm runs is not known until below.
                restored_from_failed_build=restored_from_failed_build and not attached,
                ready=ready,
            )
        # The started/reached-running ratio's numerator, and it fires on the verdict the
        # CITIZEN got: a framable URL that nothing has told us will hang. Two readings withhold
        # it — the attach arm's readiness wait failing open with `ready=False`, and a root that
        # answered without a page (see both above) — because neither is a reached-running
        # outcome, and counting them would make the ratio measure nothing.
        #
        # OUTSIDE THE PER-USER START LOCK, which is the whole reason the result is built above and
        # returned below rather than returned there. `_start_lock_for(user.id)` is the same
        # asyncio lock a fresh BUILD queues on, so a counter write held inside it does not merely
        # delay this response — it silently queues this citizen's next build behind a measurement,
        # with no error and nothing on screen to say why. The container is up, spared, heartbeated
        # and leased by this point: the start is already a fact, and recording it can wait its turn
        # outside the lock like any other bookkeeping.
        if ready:
            await count(HarnessCounter.APP_START_REACHED_RUNNING, app_id=app_id)
        # THE SERVING PROOF, AND IT IS NO LONGER THE COUNTER'S GATE. One case separates them and
        # it is worth the second `if`: when the supervisor could not be asked whether the app is
        # showing a page, `ready` stays True — the citizen keeps a preview that may be painting
        # perfectly well — while this stays False, because a transport error is not a sighting
        # and `serving_since` is the platform's claim that SOMETHING WATCHED THIS CONTAINER SERVE
        # A PAGE. That is the whole of the daylight between them; on every other arm they move
        # together, and anyone who widens the gap has to answer the same question here.
        #
        # WHAT THE CITIZEN SEES IN THAT ONE CASE, stated rather than discovered: this response
        # carries the framable URL, and the preview-state poll answers `starting` until the
        # continuation spawned on that path takes the proof — normally the next second, once the
        # supervisor answers again. The five-minute reconciler is the backstop under it.
        #
        # `cold` is the restore arm: the attach arm reuses a container that was already
        # up, so its stamp usually loses to a proof taken long before this press.
        if something_watched_it_paint:
            await _record_the_first_serve(
                redis,
                user_id,
                app_name=app_name_for(app_id),
                observer="relaunch_wait",
                cold=cold_elapsed_ms is not None,
            )
        if cold_elapsed_ms is not None:
            await count(HarnessCounter.APP_COLD_START_MS, value=cold_elapsed_ms, app_id=app_id)
        return relaunched

    # --- a colleague's shared-runtime view (#198) -----------------------------

    async def launch_shared_preview(
        self,
        db: AsyncSession,
        recipient: User,
        project: Project,
        sandbox_client: SandboxClient,
        *,
        force_refresh: bool = False,
    ) -> SharedPreview:
        """Put a READY, read-only container in front of a project SHARED WITH `recipient` — the
        recipient's own door into the same one-per-user slot `relaunch_preview` uses for a
        builder's own project. The ROUTER has already established `recipient` may see this
        project (`resolve_project_access` returning SHARED); this trusts that and re-checks
        nothing about access — only about the container.

        SNAPSHOT-ONLY, ALWAYS (requirement 21). Deliberately NEVER `newest_restore_source`,
        which would prefer the OWNER's crash-recovery bundle over their last deliberate Save —
        a shared view is restored from what the owner chose to publish to their colleagues, not
        from whatever their build container happened to hold when it last crashed.

        `force_refresh=True` is Refresh (requirement 22): skip the attach-and-reuse arm even
        when a live container already answers for this exact (project, recipient) pair, and
        restore again from whatever is CURRENTLY saved — which may have moved since Launch.
        `False` is Launch: attach if already up (the common case — a reopened tab, a second
        click), else cold-restore.

        SAME SLOT, SAME GUARDS AS A BUILD — requirement 27 (never touching the recipient's own
        `sbx-` container) falls out of reusing them rather than needing its own check:
        `_refuse_if_reclaim_would_destroy_work` protects the recipient's OWN unsaved build if
        one currently holds their slot before this ever reaches the lock, `_holding_user_lock`
        is the identical skeleton `relaunch_preview` runs under, and a build the recipient
        starts on their own project afterward reaps straight through an unattended shared view
        exactly as it would through a relaunched preview — no separate teardown path to keep in
        step with this one.

        DELIBERATELY NARROWER THAN `relaunch_preview` in two ways, both scope decisions rather
        than oversights: no build-outcome/harness-counter instrumentation (`APP_START_ATTEMPTED`
        and siblings measure the BUILD funnel; folding a passive viewer's launches into that
        funnel would corrupt what it measures — a dedicated counter is a later, additive
        change), and no `REGISTRY_FIELD_SERVING_SINCE` proof-of-first-paint stamping (that
        signal feeds the builder's own `AppStatusPanel`; a shared view's liveness is instead
        `reaper.py::_renew_shared_view_from_traffic`'s own `shared_served_count`, a genuinely
        different signal for a genuinely different viewer)."""
        with _one_relaunch_in_the_log(
            build_id=str(uuid.uuid7()), user_id=str(recipient.id), project_id=str(project.id)
        ):
            return await self._launch_shared_preview_under_one_build_id(
                db, recipient, project, sandbox_client, force_refresh=force_refresh
            )

    async def _launch_shared_preview_under_one_build_id(
        self,
        db: AsyncSession,
        recipient: User,
        project: Project,
        sandbox_client: SandboxClient,
        *,
        force_refresh: bool,
    ) -> SharedPreview:
        """The whole of `launch_shared_preview` — go there for what it does and why; this half
        is the same code, one indent level out, for the same correlation-binding reason
        `_relaunch_under_one_build_id` is split from `relaunch_preview`."""
        async with self._start_lock_for(recipient.id):
            redis = get_redis()
            if recipient.id in self._active_by_user:
                # The recipient is mid-build on one of THEIR OWN projects — the identical
                # refusal `relaunch_preview` gives a builder caught the same way. A shared view
                # is never worth pre-empting a build the recipient is actively watching.
                blocking_id = self._active_by_user.get(recipient.id)
                raise await self._slot_conflict_for(
                    recipient.id,
                    self._sessions.get(blocking_id) if blocking_id is not None else None,
                    blocking_id,
                    db,
                    project.id,
                )
            owner_app_id = await _existing_app_id(db, project.user_id, project.id)
            if owner_app_id is None:
                raise SharedProjectHasNoAppError(project.id)
            shared_name = shr_name_for(owner_app_id, recipient.id)
            # THE GUARD ALWAYS SPARES THE RECIPIENT'S OWN INCUMBENT — Refresh included. It asks
            # "is anything of the recipient's about to be destroyed", and their own already-live
            # shared view is never that, whatever button they pressed to get here. Bug fixed
            # live: passing `None` here on a forced refresh also defeated the identity check
            # (`_the_live_sandbox_is_already_the_one_we_want`/`occupied_by == spare_app`) the
            # guard itself runs first, so Refresh on a live view fell through to the shared-
            # occupant branch and reported the recipient's OWN open app as blocking them.
            await self._refuse_if_reclaim_would_destroy_work(
                db, recipient, spare_app=shared_name, sandbox_client=sandbox_client
            )
            # `_holding_user_lock` asks a NARROWER question than the guard above — not "would
            # this destroy something" but "should the reconcile below treat the live container
            # as the one we already want, or tear it down". Those answers diverge on exactly
            # Refresh: `spare_app=None` here is what makes the reconcile reclaim a live view
            # unconditionally, so the restore arm always runs even though the name it would
            # produce is identical to what is already there.
            spare_app = None if force_refresh else shared_name
            async with self._holding_user_lock(
                redis,
                recipient.id,
                sandbox_client,
                project.id,
                arm="shared_launch",
                spare_app=spare_app,
            ) as scope:
                # THE SNAPSHOT GATE — the saved bundle ONLY, never the recovery/autosave copy
                # (requirement 21). `newest_restore_source` is never called on this path.
                if not await self._snapshot_exists_or_bust(owner_app_id):
                    raise NoSnapshotToRelaunchError(owner_app_id)
                snapshot_taken_at = await _snapshot_written_at(owner_app_id)
                attached = False
                if not force_refresh:
                    try:
                        scope.handle = await self._attach_for_shared_view(
                            recipient.id, shared_name, sandbox_client
                        )
                        attached = True
                        scope.spare()
                    except NoLiveSandboxError:
                        pass
                if not attached:
                    env = {
                        **build_app_env(owner_app_id),
                        **await provision_app_storage(owner_app_id),
                        **await provision_app_database(db, project.id),
                    }
                    try:
                        scope.handle = await self._restore_or_bust(
                            sandbox_client,
                            recipient.id,
                            shared_name,
                            owner_app_id,
                            env,
                            source_key=snapshot_key(owner_app_id),
                            kind="shared_sandbox",
                            shared_project_id=project.id,
                            shared_owner_id=project.user_id,
                        )
                    except StorageNotFoundError as exc:
                        # The bundle vanished between the head-check above and the pull — the
                        # same 404 bucket `relaunch_preview` maps this into.
                        raise NoSnapshotToRelaunchError(owner_app_id) from exc
                    # THE RESTORE ARM'S LEASE STARTS HERE, before the wait, for the identical
                    # reason `_relaunch_under_one_build_id` grants one at this exact point:
                    # `_restore_or_bust` has already written the registry hash, so from this
                    # instant the sweep can see lock-held-without-a-heartbeat, which
                    # `reconcile_user`'s AND is happy to reap. Nothing else protects a
                    # freshly-restored container until the heartbeat below.
                    await grant_stay_of_execution(
                        redis, recipient.id, writer=DeadlineWriter.BUILDER_ACTED
                    )
                # NARROWED HERE, ONCE: both branches above set `scope.handle` on every path
                # that reaches this line (the attach arm's `NoLiveSandboxError` falls through
                # to the restore arm, and the restore arm's own failures already raised past
                # this point) — a local variable lets the type checker carry that certainty
                # through the awaits below, which a mutable dataclass attribute cannot.
                assert scope.handle is not None
                handle = scope.handle
                try:
                    await sandbox_client.dev_start(handle)
                except SandboxError:
                    if not attached:
                        raise  # a fresh container with no dev server has nothing to preview
                    _log.warning(
                        "shared_launch_dev_start_refused_on_attached_container",
                        user_id=str(recipient.id),
                        app_id=str(owner_app_id),
                        exc_info=True,
                    )
                ready = True
                try:
                    handle = await sandbox_client.wait_ready(
                        handle,
                        timeout_s=(
                            _ATTACHED_READY_BUDGET_SECONDS
                            if attached
                            else _COLD_READY_BUDGET_SECONDS
                        ),
                    )
                    scope.handle = handle
                except SandboxNotReadyError:
                    # THE SAME ASYMMETRY `_relaunch_under_one_build_id` draws, and for the same
                    # reason: a readiness timeout is a statement about the APP, not the
                    # container. The ATTACH arm fails OPEN — keep the container, hand back the
                    # framable URL with `ready=False`, let the next Launch/Refresh attach again
                    # rather than restore over a container that may simply be rendering slowly.
                    # The COLD arm still raises: a container just provisioned that never came up
                    # holds no work worth preserving and has nothing framable to offer.
                    if not attached:
                        raise
                    ready = False
                    _log.warning(
                        "shared_launch_attached_container_not_serving_degraded_to_unready",
                        user_id=str(recipient.id),
                        app_id=str(owner_app_id),
                        budget_s=_ATTACHED_READY_BUDGET_SECONDS,
                    )
                # Past here the container is up and registered — the same state a SUCCESSFUL
                # launch leaves behind — so a later blip destroying it is no longer a rollback.
                scope.spare()
                # …and where the ATTACH arm's OWN lease is spent: granted only once the
                # container has earned it by answering, not merely by being attempted — a
                # lease handed out before that point is a lease every failed re-attach would
                # re-grant, refreshing a wedged container's reprieve on each retry for free.
                if attached:
                    await grant_stay_of_execution(
                        redis, recipient.id, writer=DeadlineWriter.BUILDER_ACTED
                    )
                if ready:
                    # Pays the app's first route compile so the recipient's own browser does
                    # not — the identical courtesy `relaunch_preview` extends its builder.
                    await sandbox_client.someone_has_to_go_first(handle)
                preview_url = handle.preview_url
                # Seeded INSIDE the protected region for the same reason `relaunch_preview`
                # seeds one here: a failure past this point tears the container down rather
                # than 500ing with a live container behind a held lock. Nothing RENEWS this
                # heartbeat afterward — the stay of execution above (and
                # `reaper.py`'s traffic-based renewal of it) is what actually owns this
                # container's lifetime from here on.
                await write_heartbeat(redis, recipient.id)
                # …and the FINAL re-grant, re-basing the reprieve on the instant the preview
                # actually became viewable rather than the instant either arm merely attempted
                # it — the warm request above can take seconds, long enough for a sweep to
                # reap a container whose earlier lease happened to lapse mid-wait.
                await grant_stay_of_execution(
                    redis, recipient.id, writer=DeadlineWriter.BUILDER_ACTED
                )
            return SharedPreview(
                app_id=owner_app_id,
                preview_url=preview_url,
                ready=ready,
                snapshot_taken_at=snapshot_taken_at,
            )

    async def _attach_for_shared_view(
        self, recipient_id: uuid.UUID, shared_name: str, sandbox_client: SandboxClient
    ) -> SandboxHandle:
        """A handle on an already-live shared view, or `NoLiveSandboxError`. Registry-only —
        unlike `_attach_for_read`, there is no in-process session to check first, because
        `launch_shared_preview` never adopts one: a shared view has no chat turn and mints no
        `BuildSession`."""
        if not await _the_live_sandbox_is_already_the_one_we_want(
            get_redis(), recipient_id, shared_name
        ):
            raise NoLiveSandboxError(recipient_id)
        try:
            return await sandbox_client.attach_existing(str(recipient_id))
        except SandboxGoneError as exc:
            # CERTAIN absence — the client raises this only when it has confirmed the container
            # is gone (ARM says the revision does not exist, the registry is empty, or the
            # reaper already marked it ending). The restore arm above is the honest next step.
            raise NoLiveSandboxError(recipient_id) from exc

    # --- the Write turn's sandbox --------------------------------------------

    async def ensure_sandbox(
        self,
        db: AsyncSession,
        user: User,
        project_id: uuid.UUID,
        *,
        sandbox_client: SandboxClient,
        may_write: bool,
        announce: RecoveryAnnouncer | None = None,
    ) -> BuildSession:
        """Attach a live sandbox for a turn — everything the deleted `start` allocated, minus
        the build.

        `may_write` is REQUIRED and has no default, because the caller is the only thing that
        knows and a wrong guess is user-visible in both directions. It is not "is this Write
        mode?" so much as "may this turn's toolset mutate the tree" — the same fact, taken from
        where it is decided. Ask and Plan pin the container exactly as Write does (`_pin_
        workspace` attaches for every mode), so nothing downstream can recover this from the
        session itself; the name `ensure_sandbox` no longer carries that distinction the way
        `ensure_write_sandbox` once did.

        A Write turn is an ordinary chat turn that happens to hold the sandbox slot, so it
        needs the same container, the same one-per-user lock and the same registry entry a
        build needed — but no `run_build` task, no `build_started` marker, no attachments and
        no `started_seq`. Those belonged to the deleted build feed, which the turn engine
        replaces: the turn's own frames are the narrative now, and the turn's own rows are the
        record. This is now the ONLY allocator; `_start_locked`, whose skeleton this is, is
        deleted.

        That skeleton, deliberately and completely: slot claim → reconcile → lock → mint the app
        row → commit → env → resolve the sandbox → heartbeat → adopt. Every one of those steps
        exists because a build without it broke in a way someone had to debug, and a Write turn
        is allocating exactly the same resources against exactly the same reaper.
        `_resolve_sandbox` keeps all three of its arms, `SnapshotUnavailableError` included —
        refusing to substitute a blank template for a snapshot it cannot read is even more
        important here than on a build, because a Write turn would happily start editing the
        empty template and commit the result over the user's real app.

        `resolve_app_for_project` is where a fresh project's app row is minted. That is on
        purpose and it is why this takes `db`: `turns.py`'s liveness pre-check reads the app
        id WITHOUT minting, so the row is created only once a Write turn actually commits to
        running.

        Returns a `BuildSession` with `prompt=""` — the dataclass is
        reused rather than forked because the reaper, the registry sweep and
        `active_session_for` must see this exactly as they see a build's session. The two
        empty fields are the honest answer: there is no build prompt and no mode to restore.
        """
        # The opportunistic retention sweep, and since `start` was deleted this is the ONLY
        # recurring seam for the in-process map — nothing evicts it on a timer, so ended
        # sessions must not be allowed to accumulate on a workspace that only ever chats.
        self.evict_ended_sessions()
        async with self._start_lock_for(user.id):
            redis = get_redis()
            user_id = user.id
            await self._claim_the_one_build_slot(user_id, db=db, requested_project_id=project_id)
            # WHICH container would satisfy this turn? Read-only on purpose — `resolve_app_for
            # _project` below MINTS, and minting out here would leave an app row behind for a
            # turn that then gets refused. No app row yet means nothing live can be ours, which
            # is the correct answer for a project's very first turn.
            spare_app = await _sandbox_name_for_existing_app(db, user_id, project_id)
            # ABOVE the lock, because the lock's reconcile is what destroys the incumbent
            # and an `ending` registry can no longer be attached to or questioned.
            await self._refuse_if_reclaim_would_destroy_work(
                db, user, spare_app=spare_app, sandbox_client=sandbox_client
            )
            async with self._holding_user_lock(
                redis,
                user_id,
                sandbox_client,
                project_id,
                arm="ensure_sandbox",
                spare_app=spare_app,
            ) as scope:
                app_id = await resolve_app_for_project(db, user_id, project_id)
                await db.commit()
                # The DSN merge follows the commit for the reason the deleted `_start_locked`
                # documented: `ensure_project_database` commits its own claim and its own terminal
                # marker, so calling it earlier would commit a half-built request
                # transaction (the speculative DRAFT app row included).
                env = {
                    **build_app_env(app_id),
                    **await provision_app_database(db, project_id),
                }
                # `take` records the handle AND spares it when this was the attach arm. THE
                # ATTACH ARM IS THE STEADY STATE HERE: every Write message after the first
                # reuses the running container, so without the spare a `write_heartbeat` blip
                # or a Stop pressed in the wrong millisecond deleted the app the user was
                # looking at, with every unsaved change in it.
                resolved = await self._resolve_sandbox(
                    sandbox_client, user_id, app_id, env, announce=announce
                )
                handle = scope.take(resolved)
                # Inside the protected region, before adopt: a `write_heartbeat` RedisError
                # out here would orphan `_active_by_user[user_id]` forever and leak the
                # container. In here it is caught by `_holding_user_lock`'s compensation.
                await write_heartbeat(redis, user_id)
                # The session ADOPTS the lock + container: from here `finish_turn_sandbox`
                # owns their release/teardown, so the scope must not release on exit.
                scope.adopt()

        session = BuildSession(
            session_id=uuid.uuid7(),
            user_id=user_id,
            project_id=project_id,
            app_id=app_id,
            prompt="",
            lock_token=scope.token,
            handle=handle,
            may_write=may_write,
            news=resolved.news,
            restored=resolved.restored,
            attached=resolved.attached,
        )
        self._sessions[session.session_id] = session
        self._active_by_user[user_id] = session.session_id
        return session

    async def _resolve_sandbox(
        self,
        sandbox_client: SandboxClient,
        user_id: uuid.UUID,
        app_id: uuid.UUID,
        env: dict[str, str],
        *,
        announce: RecoveryAnnouncer | None = None,
    ) -> _ResolvedSandbox:
        """The one-per-user rehydrate resolution, in three arms: a live registry ATTACHES to
        the running container; no registry (a clean end always leaves none) or a registry
        whose container is gone RESTORES the snapshot when one exists; PROVISIONS a fresh
        template only when there is none — without this arm, a graceful stop→start loop
        would discard the user's work onto a blank template. A CONTAINER GETS ITS ENVIRONMENT
        EXACTLY ONCE, AT BIRTH: the birth arms build the whole `BIAL_*` set while attach
        passes none, so rotating a credential is a REBIRTH, never an attach. REPORTS ITS ARM
        (`_ResolvedSandbox.attached`) so `_LockScope.take` can spare it from compensation."""
        redis = get_redis()
        app_name = app_name_for(app_id)
        if await read_registry(redis, user_id) is None:
            return _ResolvedSandbox(
                await self._restore_or_provision(sandbox_client, user_id, app_name, app_id, env),
                attached=False,
            )
        try:
            handle = await sandbox_client.attach_existing(str(user_id))
        except SandboxGoneError:
            return _ResolvedSandbox(
                await self._restore_or_provision(sandbox_client, user_id, app_name, app_id, env),
                attached=False,
            )
        # THE ONE ARM WHERE THE TREE IS OLDER THAN THIS REQUEST. The other two have just
        # built the workspace from a bundle or a template, so there is nothing to have lost. This
        # one hands back a container that has been running unattended, and until this unit
        # nothing ever asked whether it still held the app.
        return await self._still_theirs_or_put_it_back(
            sandbox_client, user_id, app_name, app_id, env, handle, announce=announce
        )

    async def _still_theirs_or_put_it_back(
        self,
        sandbox_client: SandboxClient,
        user_id: uuid.UUID,
        app_name: str,
        app_id: uuid.UUID,
        env: dict[str, str],
        handle: SandboxHandle,
        *,
        announce: RecoveryAnnouncer | None,
    ) -> _ResolvedSandbox:
        """Confirm the attached container still holds this app; on confirmed loss, put it
        back. THE SENTENCE COMES BEFORE THE RESTORE, and that ordering is the unit, not a
        nicety: the recovery path adds tens of seconds of otherwise-silent latency (a full
        bundle of the reverted tree plus a complete restore), so `announce` is called first
        and the slow work happens behind a sentence that explains it. NOTHING BUT `REVERTED`
        REACHES A TEARDOWN — not defensive coding, the entire safety property: `REVERTED`
        requires three independent facts to agree, and the two unanswerable states leave the
        container running, attached and untouched."""
        source = await self._restore_source_for_the_gate(app_id)
        verdict = await workspace_integrity(
            sandbox_client, handle, app_id, restore_source_key=source
        )
        if verdict.state is WorkspaceState.INTACT:
            return _ResolvedSandbox(handle, attached=True)
        if verdict.state is WorkspaceState.UNREADABLE:
            raise WorkspaceUnreadableError(verdict.reason, app_id=app_id)
        if verdict.state is WorkspaceState.UNVERIFIABLE:
            _log.warning(
                "workspace_integrity_unverifiable",
                app_id=str(app_id),
                detail=verdict.reason,
                head=verdict.head,
            )
            await _say(announce, RecoveryNews.UNVERIFIED)
            return _ResolvedSandbox(handle, attached=True, news=RecoveryNews.UNVERIFIED)

        # --- REVERTED ------------------------------------------------------------------------
        if not verdict.durable_copy_exists:
            # Nothing to put back. The one thing that must NOT happen here is presenting the
            # empty template as their app and letting the agent build on it, so the news carries
            # the honest sentence and the turn holds.
            _log.error(
                "workspace_reverted_unrecoverable",
                app_id=str(app_id),
                detail=verdict.reason,
                head=verdict.head,
            )
            await _say(announce, RecoveryNews.UNRECOVERABLE)
            return _ResolvedSandbox(handle, attached=True, news=RecoveryNews.UNRECOVERABLE)

        await _say(announce, RecoveryNews.RESTORING)
        taken_at = datetime.now(UTC)
        quarantined = await self._park_the_tree_aside(
            sandbox_client, handle, app_id, verdict, taken_at=taken_at
        )
        if quarantined is _Quarantine.FAILED:
            # NEVER DESTROY THE ONLY COPY TO MAKE A RECOVERY SUCCEED. If the tree could not be
            # set aside, the restore does not run — the container keeps whatever it has.
            await _say(announce, RecoveryNews.UNRECOVERABLE)
            return _ResolvedSandbox(handle, attached=True, news=RecoveryNews.UNRECOVERABLE)
        try:
            restored = await self._restore_or_bust(
                sandbox_client,
                user_id,
                app_name,
                app_id,
                env,
                source_key=self._source_that_is_not_poisoned(app_id, source),
            )
        except StorageError, SandboxError, SnapshotUnavailableError:
            # `restore_from_snapshot` fetches BEFORE it destroys anything and self-cleans on the
            # way out, so a failure here leaves the container either untouched or gone —
            # never half-restored. Either way the citizen has to be told, because the alternative
            # is a preview that quietly shows a template.
            _log.exception("workspace restore failed after quarantine", app_id=str(app_id))
            await _say(announce, RecoveryNews.UNRECOVERABLE)
            return _ResolvedSandbox(handle, attached=True, news=RecoveryNews.UNRECOVERABLE)
        await count(HarnessCounter.RESTORE_PERFORMED, app_id=app_id)
        _log.warning(
            "workspace_restored_after_reversion",
            app_id=str(app_id),
            detail=verdict.reason,
            quarantined=quarantined.value,
        )
        return _ResolvedSandbox(
            restored, attached=False, news=RecoveryNews.RESTORING, restored=True
        )

    async def _restore_source_for_the_gate(self, app_id: uuid.UUID) -> str | None:
        """Which bundle would a restore hand back? — asked so the verdict compares against it.

        `newest_restore_source` RAISES when the store will not answer, and on this path that is
        the same fact as a container that will not answer: we cannot tell, so we must not judge.
        Mapped to `None` here and left for `workspace_integrity`'s own store read to surface as
        `UNREADABLE`, rather than aborting the turn with a different error shape."""
        try:
            return await self.newest_restore_source(app_id)
        except SnapshotUnavailableError:
            return None

    def _source_that_is_not_poisoned(self, app_id: uuid.UUID, source: str | None) -> str | None:
        """Which bundle to actually restore, when the recovery slot may itself be the problem.

        THE SLOT CAN BE POISONED, and `recoverable_work` cannot tell. It ranks the two bundles by
        `last_modified`, never by ancestry, so a recovery copy that was overwritten with a bad
        tree outranks a perfectly good saved one — and every restore afterwards hands back the
        poison. Two consecutive refusals by the integrity guard signal that the slot, rather
        than the turn, is the problem: fall back to the user's own Save, which no platform
        write ever touches, and escalate."""
        if source is None or consecutive_diverts(app_id) < _POISONED_SLOT_REFUSALS:
            return source
        _log.error(
            "recovery_slot_looks_poisoned",
            app_id=str(app_id),
            consecutive_refusals=consecutive_diverts(app_id),
            detail="restoring the user's saved bundle instead of the recovery slot",
        )
        return None

    async def _park_the_tree_aside(
        self,
        sandbox_client: SandboxClient,
        handle: SandboxHandle,
        app_id: uuid.UUID,
        verdict: IntegrityVerdict,
        *,
        taken_at: datetime,
    ) -> _Quarantine:
        """Bundle the tree we are about to restore over, unless there is provably nothing in
        it — skipped only when we can SEE there is nothing to keep, since the headline
        factory-reset case quarantines the baked template itself, a full `git bundle` +
        base64 + upload to preserve nothing. THE GUARD IS `provably_bare`, NOT `content_empty`:
        `REVERTED` requires `content_empty` by construction, so that guard would skip EVERY
        quarantine and be dead code — worse, it is also true with no repository at all, exactly
        when the working directory may hold the user's entire app with only its `.git`
        missing. Skip only when we positively know the tree is the starter template."""
        if verdict.provably_bare:
            return _Quarantine.SKIPPED_AS_EMPTY
        try:
            await write_snapshot(
                sandbox_client,
                handle,
                app_id,
                destination=Destination.quarantine(app_id, taken_at),
            )
        except SandboxError, StorageError:
            _log.exception(
                "could not quarantine the workspace; refusing to restore over it",
                app_id=str(app_id),
            )
            return _Quarantine.FAILED
        return _Quarantine.WRITTEN

    async def _restore_or_provision(
        self,
        sandbox_client: SandboxClient,
        user_id: uuid.UUID,
        app_name: str,
        app_id: uuid.UUID,
        env: dict[str, str],
    ) -> SandboxHandle:
        """Restore the snapshot when one exists; provision a fresh template ONLY when the
        bundle is CONFIRMED absent. Fresh-provision has exactly ONE reachable arm:
        `StorageNotFoundError` — the store positively answered "no bundle" (a genuinely new
        app, or one that vanished between the head-check and the pull); no error path reaches
        it otherwise. An unknown head state, or a restore that keeps failing, raises
        `SnapshotUnavailableError` and aborts the start instead, because a fresh template here
        would be silently overwritten onto the user's saved work by finalize's step-1
        snapshot."""
        # Ensure the app's Blob container + mint a fresh session SAS ONLY on this birth
        # (provision/restore) arm — never on attach, which reuses the live container's SAS.
        # A configured-store failure propagates: it fails the start before any sandbox handle
        # exists (start's compensation releases the lock; nothing to tear down), and the idempotent
        # container is simply reused on the next start. Disabled storage (dev/test) yields {} — a
        # no-op merge.
        env = {**env, **await provision_app_storage(app_id)}
        recovery_source = await self.newest_restore_source(app_id)
        if recovery_source is not None or await self._snapshot_exists_or_bust(app_id):
            try:
                # NEWEST, not the saved one. See `newest_restore_source`: pulling `snapshot_key`
                # here is what used to discard everything the user did after their last Save,
                # one turn after a container was reclaimed.
                return await self._restore_or_bust(
                    sandbox_client,
                    user_id,
                    app_name,
                    app_id,
                    env,
                    source_key=recovery_source,
                )
            except StorageNotFoundError:
                # The ONLY error that may reach provision_new: the store positively answered
                # "no bundle" on the pull, so there is no work to overwrite.
                _log.warning(
                    "snapshot disappeared between head-check and restore; provisioning fresh",
                    app_id=str(app_id),
                )
        return await sandbox_client.provision_new(str(user_id), app_name, app_env=env)

    async def _restore_or_bust(
        self,
        sandbox_client: SandboxClient,
        user_id: uuid.UUID,
        app_name: str,
        app_id: uuid.UUID,
        env: dict[str, str],
        *,
        source_key: str | None = None,
        kind: Literal["build_sandbox", "shared_sandbox"] = "build_sandbox",
        shared_project_id: uuid.UUID | None = None,
        shared_owner_id: uuid.UUID | None = None,
    ) -> SandboxHandle:
        """Pull the known-present snapshot into a fresh container, with bounded retry.
        `source_key` selects WHICH bundle (default the saved snapshot; relaunch passes the
        recovery key). A transient npm or storage blip is retried rather than falling back to
        a fresh template — that fallback caused silent, permanent data loss, since the bundle
        EXISTS here and finalize would overwrite it. A PERSISTENT failure deliberately strands
        the session instead: a 503 with the user's work intact beats a start that silently
        destroys it. Self-cleans on every exception, so each attempt starts from no container;
        `StorageNotFoundError` must not retry — it is the caller's fresh-provision arm.

        `kind` (#198) forwards to `SandboxClient.restore_from_snapshot` unchanged — every
        existing caller means the default (a build sandbox); `launch_shared_preview` is the
        one caller that passes `shared_sandbox`, and it is also the one caller that ever
        passes `shared_project_id`/`shared_owner_id` — see that method's own docstring for
        why the occupancy check needs them stamped."""
        attempt = 0
        while True:
            attempt += 1
            try:
                return await sandbox_client.restore_from_snapshot(
                    str(user_id),
                    app_name,
                    app_env=env,
                    source_key=source_key,
                    kind=kind,
                    shared_project_id=shared_project_id,
                    shared_owner_id=shared_owner_id,
                )
            except StorageNotFoundError:
                # Discriminated by TYPE, and this clause MUST stay first: `StorageNotFoundError`
                # IS a `StorageError`, so the retry arm below would otherwise swallow a
                # confirmed-absent bundle and 503 a start that should simply provision fresh.
                raise
            except BundleValidationError as exc:
                # The bundle is present and unreadable. NOT retryable — the bytes will not
                # improve — and NOT a `StorageError`/`SandboxError`, so without this clause it
                # escaped every handler here and at both routers and surfaced as a bare 500 on
                # a path whose contract is a 503 that tells the user their work is intact.
                _log.exception("stored bundle is unreadable", app_id=str(app_id))
                raise SnapshotUnavailableError(
                    "stored bundle is unreadable", app_id=app_id
                ) from exc
            except (SandboxError, StorageError) as exc:
                if attempt >= _RESTORE_ATTEMPTS:
                    # "Contact the admin" IS the unstick path for a persistent failure;
                    # automatic snapshot-quarantine remains a noted, deliberately unbuilt
                    # follow-up.
                    _log.exception(
                        "snapshot restore failed on every attempt; failing the start closed "
                        "(saved version left intact — never provisioning over it)",
                        app_id=str(app_id),
                        attempts=attempt,
                    )
                    raise SnapshotUnavailableError(
                        "snapshot restore failed after retries", app_id=app_id
                    ) from exc
                _log.warning(
                    "snapshot restore failed; retrying",
                    app_id=str(app_id),
                    attempt=attempt,
                    exc_info=True,
                )
                await _asleep(_RESTORE_BACKOFF_SECONDS)

    async def _snapshot_exists_or_bust(self, app_id: uuid.UUID) -> bool:
        """The BUILD path's fail-closed read of the shared head-check below."""
        return await snapshot_exists_or_bust(app_id)

    # --- progress channel ----------------------------------------------------

    async def on_progress(self, session: BuildSession, env: ProgressEnvelope) -> None:
        """The `ProgressSink`: buffer the envelope, derive status, refresh liveness,
        and fan out to every live SSE subscriber (no Redis — this IS the transport).

        ONE PRODUCER LEFT. The build harness that emitted the six BRAIN members is deleted, so in
        production the only envelope that reaches here is the terminal `ended` that `_do_finalize`
        emits. The generic derivation below is kept deliberately: this is a SINK, and it has to
        derive correct state from any envelope handed to it — including the ones tests push
        directly — without reaching back into who emitted them."""
        session.envelopes.append(env)
        session.last_seq = env.seq
        session.updated_at = datetime.now(UTC)
        if isinstance(env, PreviewReadyEvent):
            session.status = BuildSessionStatus.READY
            session.preview_url = env.preview_url
        elif isinstance(env, PreviewReconnectingEvent):
            # The dev-server PROCESS crashed after the preview was framed. A feed-only
            # signal: the status enum is frozen at five members with no "reconnecting" state, so
            # the lifecycle status is deliberately LEFT UNCHANGED (a completed build stays `ended`,
            # a live one stays `ready`). It is still buffered + fanned out below like any envelope;
            # the portal reads it to show a distinct reconnecting visual, and the following
            # `preview_ready` re-frames. Explicit branch so it never falls into the provisioning
            # bump below (a reconnecting frame is never the first sign of the loop running).
            pass
        elif isinstance(env, EndedEvent):
            session.status = env.status
            if env.preview_url is not None:
                session.preview_url = env.preview_url
            session.terminal_emitted = True
            # In production this fold is an identity — `_do_finalize` builds the frame FROM
            # `session.snapshot_committed`. It stays because `on_progress` is the generic
            # progress sink: it must derive correct state from any envelope handed to it,
            # including the ones tests push directly, without reaching back into who emitted
            # them.
            session.snapshot_committed = session.snapshot_committed or env.snapshot_committed
        elif session.status == BuildSessionStatus.PROVISIONING:
            session.status = BuildSessionStatus.BUILDING  # first sign of the loop running

        # Build activity = liveness: renew the lock + heartbeat SERVER-side so an active
        # build whose tab is closed keeps its lock and is never reaped as idle. Skipped for
        # a terminal frame (the lock is about to be released) and best-effort (a redis blip
        # must not break the feed).
        if not isinstance(env, EndedEvent) and session.lock_token:
            try:
                redis = get_redis()
                if not await renew_lock(redis, session.user_id, session.lock_token):
                    # The lock lapsed under an active build (reaped / expired) — the reaper
                    # may now double-allocate. Best-effort still, but no longer invisible.
                    _log.warning(
                        "build session lock lost during an active build",
                        session_id=str(session.session_id),
                        user_id=str(session.user_id),
                    )
                await write_heartbeat(redis, session.user_id)
            except Exception:
                # A Redis blip must not break the progress relay, but — like the other
                # best-effort Redis paths in this file — it is logged, never swallowed.
                _log.exception(
                    "liveness renew/heartbeat failed during build",
                    session_id=str(session.session_id),
                )

        # Fan out with per-subscriber failure isolation — a slow/dead subscriber is dropped,
        # never allowed to raise QueueFull back at whatever is emitting.
        for queue in list(session.subscribers):
            try:
                queue.put_nowait(env)
            except asyncio.QueueFull:
                session.subscribers.discard(queue)

    # --- completion + the single-owner end sequence --------------------------

    async def _finalize(
        self,
        session: BuildSession,
        reason: str | None,
        sandbox_client: SandboxClient,
        *,
        result: BuildResult | None = None,
    ) -> None:
        """Single-owner dispatcher: the FIRST caller (synchronously, no await between the
        check and the create) spawns ONE shielded end-sequence task; every caller then
        awaits it under `asyncio.shield`, so a caller's own cancellation (a racing stop
        cancelling the run_build task mid-finalize) can NEVER tear the sequence in half —
        `_do_finalize` runs to completion in its own task regardless."""
        if session.finalize_task is None:
            session.terminal_committed = True
            session.finalize_task = asyncio.ensure_future(
                self._do_finalize(session, reason, sandbox_client, result=result)
            )
        await asyncio.shield(session.finalize_task)

    async def _record_outcome(
        self,
        session: BuildSession,
        *,
        status: BuildSessionStatus,
        preview_url: str | None,
        reason: str | None,
    ) -> None:
        """Write the build-outcome message to the session's thread. The SERVER records this,
        not the portal, since the portal is not reliably there: builds take minutes, users
        close tabs, and an in-memory session is evicted 5 minutes after its terminal.
        Best-effort and never raising: this runs inside the end sequence, where a raise would
        skip the terminal frame and hang every SSE feed; a build with no thread is a no-op,
        not an error. TIME-BOUNDED for the same reason: this is the only end-sequence step
        that opens a DB session, and a wedged connection would block here forever — the
        record is worth waiting seconds for, never the terminal."""
        if session.conversation_id is None:
            return
        try:
            async with asyncio.timeout(_OUTCOME_WRITE_TIMEOUT_SECONDS):
                async with self._session_factory() as db:
                    await write_build_outcome(
                        db,
                        user_id=session.user_id,
                        conversation_id=session.conversation_id,
                        session_id=session.session_id,
                        status=status,
                        preview_url=preview_url,
                        snapshot_committed=session.snapshot_committed,
                        reason=reason,
                        started_seq=session.started_seq,
                    )
        except (Exception, TimeoutError):  # fmt: skip  # ruff py314 strips parens
            _log.exception("build outcome write failed", session_id=str(session.session_id))

    async def _pardon_the_container(
        self, redis: aioredis.Redis, session: BuildSession, *, touched: bool
    ) -> None:
        """The success-path alternative to teardown: the container outlives its build so the
        user can use what they just built. Mirrors `relaunch_preview`'s lifetime model: the
        registry entry STAYS, a bounded stay of execution owns the lifetime, and the per-user
        lock is released so a pardoned preview never occupies the one-build slot
        (reconcile-on-start still reaps THROUGH an unexpired stay, since the incoming build
        needs it). ORDER IS LOAD-BEARING: the stay is granted while the lock is STILL HELD, or
        a concurrent sweep could see lock-gone with no lease yet and destroy the container
        just pardoned. Best-effort, and both degraded modes below are safe."""
        # `touched` is the only thing that changes here, and it is a fact about WHAT THIS RUN
        # DID, never which kind of chat sent it: a run that wrote files earns the usual long
        # stay, one that wrote nothing earns a shorter one, bounding the cost of a chat-only
        # session that pins the workspace on every turn without ever producing anything worth
        # a 30-minute reprieve. `grant_stay_of_execution`'s own monotonic guarantee
        # (`max(existing, computed)`) keeps this safe on a MIXED session: a read-only turn
        # arriving inside a write turn's still-standing stay leaves that longer deadline
        # untouched.
        writer = DeadlineWriter.TURN_IN_FLIGHT if touched else DeadlineWriter.TURN_ENDED_UNCHANGED
        try:
            await grant_stay_of_execution(redis, session.user_id, writer=writer)
        except Exception:
            # Degraded but safe: the sweep reaps at heartbeat lapse (~90s) instead, never an
            # orphan, because the registry is still there to find.
            _log.exception(
                "stay grant failed in pardon; the sweep will reap at heartbeat lapse",
                session_id=str(session.session_id),
            )
        if session.lock_token:
            try:
                await release_lock_as_holder(redis, session.user_id, session.lock_token)
            except Exception:
                # Degraded but safe: the lock lingers to its TTL and the next start's
                # `reap_lock` clears it.
                _log.exception("lock release failed in pardon", session_id=str(session.session_id))

    async def _do_finalize(
        self,
        session: BuildSession,
        reason: str | None,
        sandbox_client: SandboxClient,
        *,
        result: BuildResult | None = None,
    ) -> None:
        """The authoritative end sequence, run exactly once. Every step is best-effort:
        a Redis blip on release/delete must NOT abort the sequence (which would leave the
        session half-finalized with the SSE feed hung) — it is logged and the sequence
        continues to the terminal synthesis (fixed ordering: snapshot → teardown-or-pardon
        → release → synthesize)."""
        redis = get_redis()
        reason = reason or session.end_reason or _COMPLETED

        # 1. Snapshot — only with live progress to persist; skipped for force_end / already done.
        if (
            session.handle is not None
            and not session.force_ended
            and not session.snapshot_committed
        ):
            try:
                # A BUILD's finalize writes the saved version: the build was the user's act,
                # and `submit` must be able to approve exactly what it produced.
                await write_snapshot(sandbox_client, session.handle, session.app_id)
                session.snapshot_committed = True
            except Exception:
                _log.exception("snapshot failed in finalize", session_id=str(session.session_id))

        # 1b. The generation-time overpromise detector: while the container is still up, flag
        #     an app whose copy promises live/shared data with no refetch anywhere in the
        #     workspace. A structlog signal only — never a gate — and it swallows its own
        #     failures, so it can never delay or break the end sequence beyond one exec.
        if session.handle is not None and not session.force_ended:
            await flag_liveness_overpromise(
                sandbox_client,
                session.handle,
                app_id=session.app_id,
                session_id=session.session_id,
            )

        # The terminal status/URL are computed BEFORE step 2 because the teardown-or-pardon
        # decision needs them (see WHY at the emit below for the status derivation rules).
        status = result.status if result is not None else _terminal_status(reason)
        preview_url = (result.preview_url if result is not None else None) or session.preview_url

        # The pardon decision: ONLY a genuinely successful build keeps its
        # container. `status` (not just the reason string) is part of the test so a
        # hypothetical FAILED verdict carrying a "completed" reason could never leave a
        # broken container running as if it were a success.
        pardoned = (
            reason == _COMPLETED
            and status is BuildSessionStatus.ENDED
            and not session.force_ended
            and session.handle is not None
        )

        # READ THE CONTAINER'S OWN RECORD BEFORE THE STEP THAT DELETES IT — the last chance
        # anything has to answer "was this container ever any use to anybody". One HGETALL on a
        # path that is already tearing down an ACA container; there is no budget to protect
        # here, and it is guarded because a blip must not abort the end sequence.
        reg_at_the_end: dict[str, str] | None = None
        with suppress(RedisError):
            reg_at_the_end = await read_registry(redis, session.user_id)

        # 2. Teardown → 3. holder release (LAST) → clear registry — or, on the completed
        #    path, PARDON: keep the container + registry, lease its lifetime, release the
        #    lock (see `_pardon_the_container`). Release + registry-delete run ONLY on a
        #    CLEAN teardown: a teardown SandboxError means the container may still be live,
        #    so KEEP the Redis lock + registry (mirroring reaper.reap_user's
        #    keep-state-on-failure) for the next reaper sweep to retry — clearing them now
        #    would orphan a container the reaper's registry-only scan can never see again.
        #    `_active_by_user` is popped regardless (guaranteed-run finally) so the SSE feed
        #    always closes even on a kept-state teardown failure.
        try:
            if pardoned:
                # `touched=True` unconditionally: this is the BRAIN-driven build path, and
                # `pardoned` already required a genuinely successful build (status ENDED, not
                # force-ended) — a build that reached that verdict wrote the app it built.
                # There is no read-only arm here to distinguish (unlike `finish_turn_sandbox`,
                # an ordinary chat turn's end, where a Plan-kind or a Q&A message may touch
                # nothing at all).
                await self._pardon_the_container(redis, session, touched=True)
            else:
                torn_down = True
                if session.handle is not None:
                    try:
                        await sandbox_client.teardown(session.handle)
                    except SandboxError:
                        torn_down = False
                        _log.exception(
                            "teardown failed in finalize; keeping lock+registry for the reaper",
                            session_id=str(session.session_id),
                        )
                if torn_down:
                    if session.lock_token:
                        try:
                            await release_lock_as_holder(
                                redis, session.user_id, session.lock_token
                            )
                        except Exception:
                            _log.exception(
                                "lock release failed in finalize",
                                session_id=str(session.session_id),
                            )
                    try:
                        await delete_registry(redis, session.user_id)
                    except Exception:
                        _log.exception(
                            "registry delete failed in finalize",
                            session_id=str(session.session_id),
                        )
        finally:
            self._active_by_user.pop(session.user_id, None)
            self._maybe_prune_start_lock(session.user_id)

        # 3a. A CONTAINER'S LIFE ENDED, said out loud. Until this line the clean finish was
        #     completely silent, so the log held starts with no ends and no way to tell a tidy
        #     shutdown from a process that simply vanished.
        #
        #     `reason` NAMES THE DOOR, not this session's end reason: one event, four values,
        #     across the two files that end containers, so an external rule can key on it (THE
        #     ONE RULE in `alarms.py`). The session's own end reason is already on the terminal
        #     `ended` frame and on the outcome row below — spelling it here a third time is how
        #     two spellings of one fact get written.
        #
        #     `served` IS READ STRAIGHT OFF THE STAMP and is deliberately NOT `stamp_is_proven`:
        #     that predicate grandfathers a pre-cutover absence as proven so a live fleet stays
        #     framed across the deploy, which is the right answer for the SCREEN and the wrong
        #     one for a retrospective. Here an absent stamp means nobody ever watched this app
        #     answer, and saying otherwise would put a lie in the one field that exists to count
        #     containers that were never any use to anybody.
        #
        #     ON THE PARDON ARM the container is still up — `pardoned=True` says exactly that —
        #     so `lifetime_ms` there is how long it had lived when its session ended, not how
        #     long it lived in total. The reaper writes the closing line for that one.
        _log.info(
            SANDBOX_TORN_DOWN_EVENT,
            reason="turn_finalize",
            pardoned=pardoned,
            lifetime_ms=elapsed_ms(
                an_instant_on_the_hash(reg_at_the_end, REGISTRY_FIELD_CREATED_AT),
                datetime.now(UTC),
            ),
            served=bool(reg_at_the_end and reg_at_the_end.get(REGISTRY_FIELD_SERVING_SINCE)),
            user_id=str(session.user_id),
            app_id=str(session.app_id),
            session_id=str(session.session_id),
        )

        # 4. Emit THE terminal `ended` — the session's one and only terminal frame. It drives
        #    the derived status AND lets every SSE generator emit `[DONE]` (a bare close
        #    would leave status stuck at BUILDING/READY and hang the feed). Must run even if a
        #    prior step raised, so status is always terminal.
        #
        #    WHY HERE, and nowhere else: this point is downstream of the step-1 snapshot, so
        #    `session.snapshot_committed` is settled — true when the bundle actually pushed,
        #    false when it failed/was skipped. BRAIN cannot emit this frame (no `ended` helper
        #    exists on its emitter): anything it emitted would necessarily predate the snapshot
        #    and could only ever report `snapshot_committed=false` — the exact lie this fixes.
        #
        #    `status` comes from BRAIN's verdict when there is one — the reason string alone
        #    cannot decide it (an `escalated` end is FAILED, a `quota_exceeded` end is ENDED, and
        #    neither equals `_BUILD_FAILED`). Only the verdict-less paths (stop / force_end /
        #    idle-reap / a raised run_build) fall back to deriving it from the reason.
        #    (`status`/`preview_url` are computed above step 2 — the pardon decision needs them.)

        # 3b. Record the outcome in the thread — BEFORE the terminal frame, so the row is
        #     already there when any client learns the build is over (the reverse order races every
        #     reader). Best-effort like every other step here: a failed write must not abort the
        #     sequence and hang the SSE feed. Same values as the frame below, by construction.
        await self._record_outcome(session, status=status, preview_url=preview_url, reason=reason)

        if not session.terminal_emitted:
            ended = EndedEvent(
                status=status,
                # BRAIN's final URL wins; fall back to the last `preview_ready` we saw, so an
                # escalation that carries no URL still reports a preview that genuinely came up.
                preview_url=preview_url,
                snapshot_committed=session.snapshot_committed,
                reason=reason,
                seq=session.last_seq + 1,  # continues BRAIN's stream — gap-free across the handoff
            )
            try:
                await self.on_progress(session, ended)
            except Exception:
                _log.exception("terminal emit failed", session_id=str(session.session_id))
                session.status = status  # guarantee a terminal status regardless
        else:
            # Unreachable: `_do_finalize` runs exactly once per session (the `finalize_task`
            # single-owner guard) and is now the ONLY emitter of `ended`, so nothing can have
            # set this flag before us. Kept as the last structural line of defense for "never
            # two terminals" — but loud, because reaching it means the single-owner guard broke.
            _log.warning(
                "terminal ended already present at finalize; skipping a second emit",
                session_id=str(session.session_id),
            )

        # 5. Start the retention window — the session (and its replay buffer) stays resident
        #    for a late SSE reconnect, then `evict_ended_sessions` drops it.
        session.ended_at = datetime.now(UTC)

    async def finish_turn_sandbox(
        self,
        session: BuildSession,
        sandbox_client: SandboxClient,
        *,
        touched: bool,
    ) -> None:
        """The end of a turn: free the slot and hand the container its lease. NO SAVE — the
        saved bundle reaches Blob only on the user's explicit click; auto-snapshotting here
        (once unconditional, then on any mutating turn) quietly took that decision away, since
        every message became a new saved version. THE CONTAINER IS ALWAYS PARDONED, never torn
        down: it IS the preview the user is looking at, not build scaffolding. What actually
        lets the next message reuse it is `_the_live_sandbox_is_already_the_one_we_want`, not
        the pardon alone — an earlier version of this docstring claimed otherwise, and
        reconcile-on-start destroyed the pardoned container on every turn until fixed."""
        # The consequence of "no save" is deliberate and belongs in the UI, not buried here:
        # work that is never saved is lost when the container is reclaimed, and what earns
        # that is the dirty indicator and the leave warning — a user who loses work must have
        # been told, twice, that it was unsaved. The 1c autosave below only covers the endings
        # nobody can warn about (a crash, a closed laptop, the idle reaper).
        #
        # BOTH of those tellings fire on LEAVING, and switching projects inside the SPA is not
        # leaving — so on that path the user was told neither time. That gap is precisely what
        # `SandboxReclaimBlockedError` exists to close, which is why the reclaim refusal is not
        # redundant with the warnings named above and must not be retired as though it were.
        #
        # Steps 1b, 2 and 3 of `_do_finalize`, in that order and for those reasons. Deliberately
        # NOT here: the terminal `ended` frame (the turn's own `TurnEndedFrame` owns it),
        # `_record_outcome` (the turn's own rows are the record now), any mode restore (Write
        # is no longer a dead end the thread has to be rescued from), and the snapshot itself.
        redis = get_redis()

        # STILL NO SAVE HERE.
        #
        # 1b. The generation-time overpromise detector, while the container is still up. A
        #     structlog signal only — never a gate — and it swallows its own failures. Gated
        #     on the same flag: there is nothing new to flag about a tree this turn did not
        #     write to.
        if session.handle is not None and touched:
            await flag_liveness_overpromise(
                sandbox_client,
                session.handle,
                app_id=session.app_id,
                session_id=session.session_id,
            )

        # 1c. AUTOSAVE to the recovery slot. NOT a save: `recovery_key` is a separate
        #     namespace that `submit` never copies and a relaunch never restores in place of
        #     the user's bundle — what becomes a saved VERSION is still their
        #     click. This only stops the endings nobody can warn about (a crash, a closed
        #     laptop, the idle reaper) from costing the whole session.
        #
        #     Best-effort and swallowed, deliberately: a safety net that can fail a turn is
        #     not a safety net. The bounded timeout matters too: each exec inside the write is
        #     already capped (120s in `snapshot.py`, 30s for the ancestry probe), but five in
        #     sequence is minutes on a path whose job is to end.
        if session.handle is not None and touched:
            try:
                async with asyncio.timeout(_RECOVERY_SNAPSHOT_TIMEOUT_SECONDS):
                    written = await write_recovery_copy(
                        sandbox_client,
                        session.handle,
                        session.app_id,
                        taken_at=datetime.now(UTC),
                    )
                if written.outcome is RecoveryOutcome.DIVERTED:
                    # THE NUMBER THAT SETTLES A PAST INCIDENT the next time it happens: the
                    # difference between "the platform failed to CHECK the workspace" and "the
                    # platform failed to make it DURABLE" — a distinction nobody could answer
                    # the day it actually mattered.
                    await count(HarnessCounter.RECOVERY_WRITE_MISSED, app_id=session.app_id)
                _log.info(
                    "recovery copy",
                    app_id=str(session.app_id),
                    session_id=str(session.session_id),
                    outcome=written.outcome.value,
                    detail=written.reason,
                )
            except TimeoutError, Exception:  # fmt: skip  # ruff py314 strips the parens
                # STILL SWALLOWED — a safety net that can fail a turn is not a safety net — but
                # no longer SILENT. The swallow is exactly what once made a past reversion
                # unfalsifiable: nobody could say afterwards whether the platform had failed to
                # check the workspace or failed to make it durable, because a write that never
                # landed left no trace an operator would ever look for.
                _log.error(
                    RECOVERY_WRITE_DID_NOT_LAND_EVENT,
                    app_id=str(session.app_id),
                    session_id=str(session.session_id),
                    reason="failed",
                    exc_info=True,
                )
                await count(HarnessCounter.RECOVERY_WRITE_MISSED, app_id=session.app_id)

        # 2/3. Pardon: grant the stay while the lock is STILL HELD, then release. The order
        #      is load-bearing (see `_pardon_the_container`) — releasing first opens a window
        #      where a concurrent sweep sees lock-gone with no lease yet and executes the
        #      container we just spared. The registry entry stays: it is the sweep's only map
        #      to the container, and deleting it would orphan a live sandbox.
        #
        #      `touched` THREADS STRAIGHT THROUGH from this method's own parameter —
        #      the same fact steps 1b/1c above already key on. A turn that wrote nothing buys
        #      the shorter stay; this is where a Plan-kind chat's ordinary Q&A turn (which can
        #      never touch the tree — its toolset has no write tool) stops paying for a
        #      30-minute reprieve it never earned, without this method ever asking what kind of
        #      chat sent it.
        try:
            await self._pardon_the_container(redis, session, touched=touched)
        finally:
            # Guaranteed-run, exactly as in `_do_finalize`: the slot must free even if the
            # pardon raised, or this user can never send another Write message.
            self._active_by_user.pop(session.user_id, None)
            self._maybe_prune_start_lock(session.user_id)

        session.status = BuildSessionStatus.ENDED
        session.ended_at = datetime.now(UTC)

    # --- stop / force-end (graceful vs kill switch) --------------------------

    async def stop(
        self,
        session: BuildSession,
        sandbox_client: SandboxClient,
        *,
        reason: str = STOPPED_BY_USER,
    ) -> BuildSession:
        return await self._end(session, sandbox_client, reason=reason, force=False)

    async def force_end(
        self, session: BuildSession, sandbox_client: SandboxClient, *, reason: str = FORCE_ENDED
    ) -> BuildSession:
        return await self._end(session, sandbox_client, reason=reason, force=True)

    async def _await_end_sequence(self, session: BuildSession) -> BuildSession:
        """A terminal-committed session's end sequence, awaited to completion. The caller lost
        the race to the end sequence's owner, so it touches NO session state — it only waits
        for the shielded task and hands back the terminal session (a `stop`/`force_end` is
        idempotent, so returning mid-teardown would report a state that isn't final yet)."""
        if session.finalize_task is not None:
            with suppress(Exception):
                await asyncio.shield(session.finalize_task)
        return session

    async def _end(
        self,
        session: BuildSession,
        sandbox_client: SandboxClient,
        *,
        reason: str,
        force: bool,
    ) -> BuildSession:
        # Already ending/ended (a completion or a prior stop won the race): don't cancel —
        # just await the in-flight shielded end sequence and return the terminal state.
        if session.terminal_committed:
            return await self._await_end_sequence(session)
        # Mark-ending is best-effort and runs BEFORE the session flags are mutated: a Redis
        # blip must neither 500 the kill switch (the build would keep burning tokens) nor
        # leave a poisoned `force_ended` on a still-running session (a later natural
        # completion would then silently skip its snapshot). Matches the file's other
        # best-effort Redis paths (on_progress / _do_finalize) — logged, never swallowed.
        try:
            await mark_registry_ending(get_redis(), session.user_id)
        except Exception:
            _log.exception(
                "mark-registry-ending failed in _end; proceeding to cancel + finalize",
                session_id=str(session.session_id),
            )
        # Re-check AFTER that await — the entry check above is stale the moment we suspend.
        # INVARIANT: `force_ended` may only be written while `terminal_committed` is False,
        # checked with NO await in between. A completion landing inside the mark-ending await
        # commits the terminal and starts finalize; writing the flags now would be a write
        # BEHIND the commit — `force_ended=True` after finalize already passed its snapshot
        # step tears the container down with no bundle while the terminal frame it already
        # emitted still reports "completed". A silently lost snapshot is the worst outcome
        # this file has, so a lost race means: mutate nothing, just await the sequence.
        if session.terminal_committed:
            return await self._await_end_sequence(session)
        session.end_reason = reason
        session.force_ended = force
        task = session.task
        if task is not None and not task.done():
            task.cancel()
            # Await the FULL unwind BEFORE finalize, so no late real on_progress envelope
            # races the synthetic terminal seq (the feed's gap-free invariant). Cancelling the task
            # cannot tear the end sequence: `_finalize` runs it in a SHIELDED task, so even a
            # cancel delivered while the task is already mid-finalize lets `_do_finalize`
            # complete; every caller awaits that same shielded task.
            with suppress(asyncio.CancelledError):
                await task
        await self._finalize(session, reason, sandbox_client)
        return session


# --- accessor singleton (mirrors get_redis / get_sandbox) --------------------

_manager_singleton: SessionManager | None = None


def get_session_manager() -> SessionManager:
    global _manager_singleton
    if _manager_singleton is None:
        _manager_singleton = SessionManager()
    return _manager_singleton


def set_session_manager_for_tests(manager: SessionManager | None) -> None:
    global _manager_singleton
    _manager_singleton = manager
