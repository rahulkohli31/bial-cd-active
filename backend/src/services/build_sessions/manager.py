"""The in-process build-session lifecycle: `SessionManager` + `BuildSession`.

KTD-1 — the non-serializable core of a session (the `SandboxHandle` holding the raw
bearer, the progress `asyncio.Queue` subscribers, the in-process envelope buffer, the
background `run_build` task) lives in memory, NOT Postgres. On a single replica the
whole session is in-process; the frozen Redis keys (lock/heartbeat/registry) are the
durable cross-restart coordination.

KTD-2 — teardown + lock-release is SESSION-API-owned; BRAIN signals end by RETURNING a
`BuildResult`, never touching Redis and never emitting a terminal frame. `_finalize` runs
the authoritative end sequence exactly once (guarded by `terminal_committed`): snapshot →
teardown-or-pardon → holder release → emit THE terminal `ended`. A COMPLETED build's
container is PARDONED, not executed (#13/R2): it stays up under the bounded stay-of-
execution lease (registry kept, lock released) so the user can use what they just built;
every other end path — quota / escalated / stop / force_end / idle-reap / a raised
run_build — still tears down and clears the registry.

That order is the whole point of R7: the `ended` is emitted AFTER the step-1 snapshot, so
its `snapshot_committed` is the real post-commit value. Every end path converges on this
one emission, so the feed carries exactly one terminal, always truthful.

KTD-9 — the brain + sandbox client are threaded IN from the router's `Depends`, never
resolved inline, so `app.dependency_overrides` reach them in tests.
"""

from __future__ import annotations

import asyncio
import enum
import uuid
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AbstractAsyncContextManager, asynccontextmanager, suppress
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Final, Literal

import redis.asyncio as aioredis
import sqlalchemy as sa
import structlog
from pydantic_ai import BinaryContent
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
    RunBuild,
)
from src.db.base import async_session_factory
from src.db.models.app_registry import AppRegistry
from src.db.models.harness_counter import HarnessCounter
from src.db.models.project import Project
from src.db.models.user import User
from src.services.build_sessions.alarms import (
    RECOVERY_WRITE_DID_NOT_LAND_EVENT,
    WORKSPACE_LOST_WHILE_IDLE_EVENT,
)
from src.services.build_sessions.appdata import build_app_env, resolve_app_for_project
from src.services.build_sessions.appdb_env import provision_app_database
from src.services.build_sessions.appstorage import provision_app_storage
from src.services.build_sessions.attachments import resolve_build_attachments
from src.services.build_sessions.counters import count
from src.services.build_sessions.integrity import (
    IntegrityVerdict,
    WorkspaceState,
    clean_but_for_churn,
    container_state,
    workspace_integrity,
)
from src.services.build_sessions.liveness import flag_liveness_overpromise
from src.services.build_sessions.locks import (
    DeadlineWriter,
    acquire_lock,
    delete_registry,
    grant_stay_of_execution,
    mark_registry_ending,
    read_registry,
    reap_lock,
    release_lock_as_holder,
    renew_lock,
    write_heartbeat,
)
from src.services.build_sessions.outcome import (
    FORCE_ENDED,
    STOPPED_BY_USER,
    newest_build_outcome_status,
    transcript_head_seq,
    write_build_outcome,
    write_build_started,
)
from src.services.build_sessions.reaper import reap_user, reconcile_user
from src.services.build_sessions.snapshot import (
    Destination,
    RecoveryOutcome,
    consecutive_diverts,
    write_recovery_copy,
    write_snapshot,
)
from src.services.redis import RedisNotConfiguredError, get_redis
from src.services.redis.keys import (
    REGISTRY_FIELD_APP_NAME,
    REGISTRY_FIELD_FQDN,
    REGISTRY_FIELD_STATE,
    REGISTRY_STATE_READY,
)
from src.services.sandbox import (
    SANDBOX_NAME_PREFIX,
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

# The one end reason that PARDONS the container instead of tearing it down (#13/R2): a
# successful build's preview stays live under the idle lease so the user sees what they
# just built. Matches BRAIN's success verdict and `_do_finalize`'s legacy fallback.
_COMPLETED: str = "completed"

# R6 — bounded retry for the restore path's two fallible steps. Budgets differ because the
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
    """Does this KEY hold a restorable bundle? THREE honest answers:
    `True` = present, `False` = CONFIRMED absent, `None` = the store could not be reached.

    `head()` has always given all three signals (meta / `None` / raise); the build path was
    once lossy, collapsing a transient `StorageError` into `False`, and that single wrong
    answer is the most expensive one available — "absent" provisions a blank template, which
    finalize then snapshots over the user's real work. So a blip is retried and an unanswered
    head-check is reported as unknown rather than guessed (R6, plan
    `docs/plans/2026-07-16-002-feat-pilot-closure-plan.md` §U6). Mirrors submit's own
    fail-closed read (`api/v1/apps/router.py`, D9).

    TWO READERS, ONE EXPRESSION (N7). The build path wraps this in `snapshot_exists_or_bust`
    and refuses to proceed on `None`; the projects read surfaces `None` to the client as "we
    cannot say", which renders as the plain empty state rather than a claim in either
    direction. Deriving the two answers independently is exactly how a half-landed fix
    happens (the daily-token-double-count learning).

    The store is resolved ONCE, outside the loop: no-store-configured is a permanent config
    fact, so retrying it three times only delays the same answer.
    """
    try:
        store = get_storage()
    except StorageUnconfiguredError:
        # NOT a transient failure — the supported storage-off deployment (`src.config` gates
        # the requirement on `is_production`; `provision_app_storage` returns {} here for the
        # same reason). With no store there can be no bundle, so this is a CONFIRMED absent,
        # the exact distinction R6 cares about. Folding it into the unknown arm instead would
        # 503 EVERY build start on such a deployment.
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
    """Could the platform put this app back, from ANYTHING? (C3 §8.3, R18.)

    The pair is `recovery_key` OR `snapshot_key` — deliberately the same pair
    `newest_restore_source` consults, because the honest predicate for "offer them a restore"
    is "would a restore find something", and that is the question the restore itself asks.

    `snapshot_presence` alone was the old answer and it under-reported the exact case that
    matters most: a builder who worked across several turns and NEVER PRESSED SAVE has no
    saved bundle at all, only the platform's turn-boundary recovery copy. Telling that person
    "this project has no saved build" while holding their whole workspace on Blob is the
    single least forgivable sentence this pane can say.

    Container-independent on purpose. `SaveState.recovery_at` could almost answer this, but it
    is written inside `_save_state_of` — after a successful attach AND a container read — so it
    is null in precisely the reclaimed cases a restore offer exists for.

    TRI-STATE, and the null is Kleene-honest rather than convenient: a confirmed presence wins
    immediately (one HEAD in the common case), two confirmed absences are a real `False`, and
    an unreadable store anywhere in the ladder returns `None` — the store was unreachable, so
    nothing is claimed in either direction."""
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
# start() and on the internal reap sweep — nothing evicts them on a timer. (Narrowed 2026-08-11:
# read as scoped to THIS in-process map, which is per-process state no shared scheduler could
# reach. The repo does have scheduled work — ADR-0011.)
_ENDED_RETENTION_SECONDS: float = 300.0

# The whole budget for one turn-boundary recovery copy. It runs inside `asyncio.shield` and
# BEFORE the build slot and the conversation guard are released, so this is the longest a
# container that stopped answering can hold a user's session hostage. Generous enough for a
# large tree over `/exec`; far short of the 900 s the client would otherwise allow per call.
_RECOVERY_COPY_BUDGET_SECONDS: float = 180.0

# How long a start will wait for an ended-but-still-finalizing session's shielded end
# sequence before keeping the 409 — a refine sent right after natural completion must not
# bounce off its own finished build (the finalize is usually sub-second; the bound only
# guards a wedged teardown).
_FINALIZE_GRACE_SECONDS: float = 30.0

# How long the end sequence will wait for the outcome record before giving up and emitting the
# terminal anyway (003-U5). The write is a handful of indexed queries against a live connection —
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
# since U6 `ready` means a request was actually SERVED. So this budget is not really measuring
# the container at all — it is measuring the citizen's own root route, and a heavy dashboard
# query or an external fetch blows any budget you pick. Waiting longer cannot turn a slow page
# into a fast one; it only makes the citizen stare at a spinner before we hand back the very
# same URL. 15s is comfortably above a warm attach (measured at ~380ms end to end) and low
# enough that a slow app degrades promptly instead of two minutes later.
# The autosave runs on a turn's exit path, so the whole SEQUENCE gets one bound. Each exec in it
# is already capped individually, but five of them in a row is minutes, and a wedged container must
# not hold the turn's ending open. Generous enough for a real bundle over the supervisor, short
# enough that failing is quicker than hanging.
_RECOVERY_SNAPSHOT_TIMEOUT_SECONDS: float = 60.0

# How long "stop the build so I can switch projects" waits for the turn to actually unwind
# Bounds a REQUEST the user is sitting in front of, so it cannot be generous: a cancelled
# turn's `finally` has a terminal frame, a billing write and `finish_turn_sandbox` to get
# through, which is fast unless the container is wedged. Expiring is not a failure the user
# needs explained — the release that follows refuses on its own, and they retry.
_STOP_ACTIVE_WORK_TIMEOUT_SECONDS: float = 30.0

_ATTACHED_READY_BUDGET_SECONDS: float = 15.0
_COLD_READY_BUDGET_SECONDS: float = 120.0


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
    """R6 — the restore path could not be completed and the snapshot is NOT confirmed
    absent: either the head-check never got an answer (transient `StorageError` on every
    attempt) or the bundle is known-present but its restore kept failing.

    This is the fail-closed half of the three-state head-check (`.claude/rules/fail-first.md`:
    ambiguity denies). The tempting "recovery" — provision a fresh template and let the user
    work — is the exact outcome R6 forbids, because it is not a degraded start but a
    DESTRUCTIVE one: `_do_finalize`'s step-1 snapshot would write the blank workspace OVER
    the user's good bundle, permanently. Aborting leaves the bundle byte-for-byte intact for
    the next start. Raised from `_resolve_sandbox`, so it lands inside `_start_locked`'s
    compensation block (lock released, any container torn down); the router maps it to a 503.
    """

    def __init__(self, message: str, *, app_id: uuid.UUID) -> None:
        super().__init__(message)
        self.app_id = app_id


#: How the integrity gate tells the turn what it found, BEFORE it acts on it. A coroutine rather
#: than a return value because the sentence has to reach the citizen while the slow work runs.
RecoveryAnnouncer = Callable[["RecoveryNews"], Awaitable[None]]

#: Consecutive U3 refusals after which U2 stops trusting the recovery slot (see
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
    """What happened to the tree U2 was about to restore over."""

    WRITTEN = "written"
    #: Provably nothing in it — the baked template. Skipped on purpose; see the writer below.
    SKIPPED_AS_EMPTY = "skipped_as_empty"
    #: Could not be set aside. The restore does NOT proceed.
    FAILED = "failed"


class RecoveryNews(enum.StrEnum):
    """What the pre-turn integrity gate (U2) has to tell the citizen.

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


class WorkspaceUnreadableError(Exception):
    """The integrity gate could not reach the container to ask whether it still holds the app.

    RETRYABLE, and deliberately NOT a verdict. `_resolve_sandbox` raises rather than proceeding,
    because proceeding would let the agent build on a workspace nobody has checked — which is
    exactly the 2026-08-18 shape — and because the alternative failure (telling a user to retry)
    is one they can act on. The container is left running, attached and untouched, so the retry
    has something to attach to; `integrity.py`'s streak cap is what stops this repeating forever.
    """

    def __init__(self, message: str, *, app_id: uuid.UUID) -> None:
        super().__init__(message)
        self.app_id = app_id


class NoSnapshotToRelaunchError(Exception):
    """Relaunch (#43) found no saved snapshot to restore: the project was never built, or its
    bundle is CONFIRMED absent. Distinct from `SnapshotUnavailableError` (transient/unknown →
    503): this is a definite "nothing to relaunch", which the router maps to a 404. Unlike a
    build start, relaunch has NO fresh-provision fallback — a blank template is not a preview
    of the user's app — so a confirmed-absent bundle is a dead end, not a blank start."""

    def __init__(self, app_id: uuid.UUID) -> None:
        super().__init__("no saved build to relaunch")
        self.app_id = app_id


class SandboxReclaimBlockedError(Exception):
    """Another project holds this user's one sandbox slot and its workspace has unsaved work,
    so taking the slot would destroy it (#83).

    THE POINT IS THAT RECLAIMING IS NOT WRONG — DOING IT SILENTLY IS. The slot is per-user by
    design, so something has to give it up; what the code used to do was tear the incumbent
    down inside the incoming request, with no prompt and no snapshot. `finish_turn_sandbox`
    states the accepted bargain: work that is never saved is lost when the container is
    reclaimed, and "a user who loses work must have been told, twice". Both tellings — the
    dirty indicator and the leave warning — fire on LEAVING; switching projects inside the SPA
    is not leaving, so the user was told neither time.

    This is the missing telling, not a new save policy: KTD-5e still holds and nothing here
    writes a snapshot. The router turns it into a 409 naming the occupying project, the client
    offers Save / Switch anyway / Cancel, and the destruction only happens through the explicit
    `release` route. A CLEAN incumbent never raises this — there is nothing to lose, so the
    reclaim stays silent and costs the user nothing.

    `dirty=None` is UNKNOWN and still blocks. Two things arrive wearing it — a container we
    could reach but could not question, and one we could not reach at all
    (`SandboxUnreachableError`) — and neither is evidence of absence. Guessing "clean" is the
    one guess that loses work.

    `building=True` is a DIFFERENT REFUSAL wearing the same envelope: the incumbent is not
    merely holding a workspace, an agent is writing into it right now. The distinction is not
    cosmetic — it changes what is true, what the user can do, and what the copy may claim.

    * "has unsaved changes" is wrong. The project is mid-build; there is no settled tree to
      describe, and `dirty` is deliberately not probed (running `git status` inside a container
      while the agent writes tells you nothing you can trust, and the probe itself is the thing
      that produced a half-written snapshot in testing).
    * `release` alone cannot resolve it — `release_project_sandbox` refuses while a live
      session owns the container, so a client that offered only Save / Switch would offer two
      buttons the server declines. The build has to be STOPPED first, which is a separate act
      with its own cost: the agent's in-flight work.

    So the client gets a third choice — stop and save, stop and discard, or leave it running —
    and `release` stays the only thing that destroys a container.
    """

    def __init__(
        self,
        *,
        project_id: uuid.UUID,
        project_name: str,
        app_id: uuid.UUID,
        dirty: bool | None,
        building: bool = False,
    ) -> None:
        super().__init__("another project is holding the sandbox")
        self.project_id = project_id
        self.project_name = project_name
        self.app_id = app_id
        self.dirty = dirty
        self.building = building


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
    # When the platform last autosaved this app to the recovery slot, or None if it never has.
    # Distinct from `saved_head`, which is the user's own save: this exists so a workspace that
    # was reclaimed while dirty can be OFFERED back ("unsaved work from 14:32") rather than
    # silently forgotten. Offered, never substituted — the R6 ladder still only ever restores
    # the user's bundle.
    recovery_at: datetime | None = None


@dataclass(frozen=True)
class PreviewState:
    """What is (or is not) serving this project right now (#83, reshaped by C3 §8.3).

    `alive` is DERIVED rather than stored, and that is the whole point of the reshape: as a
    field it was the only answer, and `False` meant "never built" and "another project took the
    slot" and "asleep" and "the registry read threw" indistinguishably. As a property it can
    only ever mean `state is ALIVE`, so there is no longer anywhere for an error to hide."""

    state: PreviewLifeState
    preview_url: str | None = None
    # SLOT_TAKEN only — whose work is in the container standing where this project's was.
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
    the failure mode that made losing a container invisible.

    `written_at` is the store's own `last_modified`, which is what makes "newer" answerable at
    all: two bundle HEAD shas tell you nothing about which tree came first. It is the value the
    caller shows the user ("your work from 14:47"), so it must be the write time, never `now`."""

    app_id: uuid.UUID
    written_at: datetime


class NoLiveSandboxError(Exception):
    """There is no container to read or save from. Raised rather than returning a falsy
    success, so a Save can never report having stored work it did not."""

    def __init__(self, subject: uuid.UUID) -> None:
        super().__init__(f"no live sandbox for {subject}")
        self.subject = subject


class SandboxUnreachableError(NoLiveSandboxError):
    """The registry still names this app, but the container would not answer.

    A SUBCLASS on purpose: every existing catcher wants the parent's meaning ("I have no
    handle, so I cannot read or save") and keeps working untouched. What the split adds is a
    second question the parent could not answer — *why* there is no handle — and exactly one
    caller needs it.

    The parent now means CERTAIN ABSENCE: the registry says nothing of this app's is live.
    This means UNKNOWN: the registry says it IS live and the attach failed anyway, which is a
    cold container, a supervisor timeout, or an ARM blip against a container that may be very
    much alive and holding hours of unsaved work. `_refuse_if_reclaim_would_destroy_work`
    treats the first as safe to reclaim and the second as a refusal, because collapsing them
    is #83 with a rarer trigger (#83 review, finding 4)."""


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
    """Whose work is in the container currently holding this user's slot (#83)."""

    app_id: uuid.UUID
    project_id: uuid.UUID
    project_name: str


async def _occupying_project(
    db: AsyncSession, user_id: uuid.UUID, app_name: str
) -> _OccupyingProject | None:
    """Resolve a live container's registry name back to the project whose work is inside it.

    `app_name_for` keeps 28 of the app_id's 32 hex chars, so it is NOT invertible and there is
    no reverse builder to reach for. Every other consumer in this module compares FORWARD, and
    so does this: the user's app rows are few (`user_id` is indexed, and it is one app per
    project by `uq(project_id)`), so we read the handful that could match and re-derive the
    name for each. A lossy inverse would be a silent mis-attribution — naming the wrong project
    in a prompt about destroying work is worse than declining to name one at all.

    `None` means the container cannot be attributed to any app this user owns: a genuine ghost,
    which the reconcile below is for. Callers must fall through, never refuse on it.
    """
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
    """Is the container already up the very one this caller is about to ask for?

    THE POINT OF THIS FUNCTION IS TO NOT DESTROY A HEALTHY CONTAINER. The reconcile below it
    exists to clear a GHOST — a container left behind by a crashed run. That is a real hazard:
    the registry maps one user to one container, so a new container silently overwrites the
    ghost's entry and the ghost then runs forever with nothing able to find or delete it.
    Clearing it before allocating is the right answer to that.

    It is the wrong answer to "the same conversation sent another message." A sandbox used to
    exist only for the length of a build, so reconcile-then-allocate ran once per build and its
    cost was invisible. Write is a chat mode now: every message allocates, so the same rule tore
    down a perfectly good container and rebuilt it from the snapshot on EVERY message — a
    blocking container delete, a blocking create, an image pull and a bundle restore, to arrive
    back at the state it had just deleted. The user waits through all of it while their app sits
    there already running.

    So: ask first. The registry records which app the live container serves, and the name is
    stable per app, so "is this mine?" is a single hash read. Same app and READY → attach to it.
    Anything else — a different app, a container mid-teardown, no registry at all — falls through
    to the reconcile exactly as before, and the ghost hazard stays closed.

    `spare_app=None` means the caller has no claim to make (no app row yet, or it does not care),
    and the answer is always False: fail toward the old, safe behaviour."""
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
    """Does this registry hash say a READY container is serving `app_name`?

    Factored out so the two callers cannot drift. The predicate above answers "spare or
    reclaim?" on the start path; `project_preview_state` answers "is my preview live?" on a
    browser poll, and it cannot reuse the predicate wholesale because that one swallows a Redis
    failure into `False` — which is the exact conflation the poll now exists to undo. Sharing
    the COMPARISON while differing on the error arm is the whole trick; two hand-written copies
    of "same name and READY" would answer differently the first time one was updated alone.

    READY only. A registry marked `ending` is a container the reaper has already committed to
    destroying — attaching to it would race a teardown, and `attach_existing` refuses it anyway
    (`SandboxGoneError`), so we would pay the restore having also skipped the cleanup."""
    return (
        reg.get(REGISTRY_FIELD_APP_NAME) == app_name
        and reg.get(REGISTRY_FIELD_STATE) == REGISTRY_STATE_READY
    )


def app_name_for(app_id: uuid.UUID) -> str:
    """An ACA-compliant container name (2–32 chars, lowercase alphanumeric/hyphen,
    letter-first, ends alphanumeric), stable per app: `sbx-` + 28 hex chars of the
    app_id (`str(app_id)` is an invalid ACA name — dots/length; the hex slug is safe)."""
    return f"{SANDBOX_NAME_PREFIX}{app_id.hex[:28]}"


@dataclass(frozen=True)
class RelaunchedPreview:
    """What `relaunch_preview` (#43) hands the router: the durable app id, the live (READY)
    preview URL, and whether the newest recorded build outcome for the project was FAILED —
    in which case the restored snapshot is the last SAVED state, not that build's intent, and
    the portal labels it "last saved version" (U6).

    That last flag is a claim about a RESTORE and is false by construction on U1's attach arm:
    a relaunch that attached to a live container restored nothing, and the tree it hands back
    may be newer than any snapshot."""

    app_id: uuid.UUID
    preview_url: str
    restored_from_failed_build: bool
    # Is the dev server actually SERVING this URL yet? False only on the attach arm's fail-open
    # path (`_ATTACHED_READY_BUDGET_SECONDS` elapsed with the app still not answering). The URL is
    # framable either way — this says whether framing it will paint or wait.
    ready: bool = True


@dataclass(frozen=True)
class _ResolvedSandbox:
    """What `_resolve_sandbox` hands back: the handle, and WHICH ARM produced it.

    The flag is not diagnostics. It decides whether compensation may tear the container down.
    Two of the three arms CREATE a container, so a later failure is genuinely this request's to
    roll back. The third ATTACHES to a container that was already up and serving, and
    "rolling that back" would destroy a healthy container over a failure that had nothing to do
    with it — see `_LockScope.spared`, which has stated that rule all along.

    Returned as a value rather than left to each caller to infer, because a bare
    `SandboxHandle` looks identical either way and there is nothing at the call site to
    suggest otherwise."""

    handle: SandboxHandle
    attached: bool
    #: What the pre-turn integrity gate (U2) found, or `None` when it had nothing to say. The
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
    ADOPTED the lock+container (a start's session takes ownership — `_do_finalize` releases
    and tears down — so a clean exit must not release, and compensation must not touch them).

    `spared` is the OTHER escape from the teardown, and it answers a different question: not
    "who owns this container now" but "would destroying it be a rollback at all". Two states
    earn it, and both mean the same thing — the container survives compensation:

    - ATTACHED: it was already running when this request arrived. You cannot roll back
      something you did not do. A `wait_ready` timeout, a `write_heartbeat` Redis blip or a
      client disconnect (compensation runs on `CancelledError` by design) would otherwise
      destroy the very healthy container the attach arm exists to preserve.
    - READIED: `wait_ready` has returned, so even a container this request created is now up,
      registered and serving — the same state a SUCCESSFUL relaunch leaves behind. Past that
      line the later steps are bookkeeping, and tearing a working preview down over a Redis
      blip in the heartbeat seed costs the user their app to tidy a hash. U3's warm request
      widened that window by seconds, which is what made it worth naming.

    YOU BREAK IT, YOU BOUGHT IT — and by here, you did not break it."""

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

        The whole point is that the caller cannot get this wrong. `_resolve_sandbox` reaches
        its attach three calls deep, so nothing at the call site looks like "you are borrowing
        this" — which is exactly how `ensure_sandbox` ended up assigning `scope.handle` with no
        `spare()` while `relaunch_preview`, whose attach is visible inline, had one. Every
        future caller that routes through here inherits the right answer instead of having to
        know it."""
        self.handle = resolved.handle
        if resolved.attached:
            self.spare()
        return resolved.handle


@dataclass
class BuildSession:
    """One in-flight build (KTD-1). Held only in memory — never persisted."""

    session_id: uuid.UUID
    user_id: uuid.UUID
    project_id: uuid.UUID
    app_id: uuid.UUID
    prompt: str
    lock_token: str
    handle: SandboxHandle
    # MAY THIS SESSION'S TURN MUTATE THE TREE? Structural, not observational: it comes from the
    # mode's toolset, which is decided before the run starts and cannot change during it.
    # `toolsets_for_mode` gives Ask and Plan a `read_only_toolset` and ONLY Write the
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
    # U2 — what the pre-turn integrity gate found, and whether it restored. Both are facts about
    # THIS turn's attach, not about the app, so they live on the session rather than anywhere
    # durable: the next message asks the question again.
    news: RecoveryNews | None = None
    restored: bool = False
    # The thread this build belongs to, carried past `start` so `_do_finalize` can record the
    # outcome in it (003-U5). None when the start named no conversation — an API-only caller,
    # which has no transcript to write to.
    conversation_id: uuid.UUID | None = None
    # The thread's high-water seq the moment this build STARTED — recorded on the outcome part as
    # `startedSeq` so the NEXT build can tell the turns that arrived while this one ran from the
    # ones it already consumed (R3). None when the start named no conversation. Captured at start
    # because it is unrecoverable later: at the terminal, a turn sent mid-build looks exactly like
    # one sent before it.
    started_seq: int | None = None
    # R3 — the conversation's attachments, already materialized (blob bytes rehydrated, office
    # text fenced) at start. Empty when the start carried no `conversationId` or the thread has
    # no attachments since its last build outcome. Resolved BEFORE the lock (see `start`), so by
    # the time a session exists these are pure in-memory content; `_live_session_spec` appends
    # them to the prompt when BRAIN resolves its run context.
    attachments: list[str | BinaryContent] = field(default_factory=list)
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
        # request's session — it opens its OWN, exactly as the chat relay's disconnect-safe
        # billing drain does. Injectable so tests bind it to their rolled-back session instead of
        # committing to the real database.
        self._session_factory: SessionFactory = session_factory or async_session_factory
        self._sessions: dict[uuid.UUID, BuildSession] = {}
        self._active_by_user: dict[uuid.UUID, uuid.UUID] = {}
        # Strong refs to background run_build tasks so a client disconnect (which cancels
        # only the SSE generator) can't let the loop GC a still-running build.
        self._tasks: set[asyncio.Task[None]] = set()
        # One serialization lock per user, held across the WHOLE of start() — closes the
        # window where a concurrent same-user start would reconcile-away the first start's
        # in-flight lock (held but registry not yet written) and double-allocate a sandbox.
        self._start_locks: dict[uuid.UUID, asyncio.Lock] = {}

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
        next start build a fresh one and shatter mutual exclusion. (The safe slice of #2 —
        the `_sessions`/envelope retention window is a separate design decision.)"""
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
        """Users with a live in-proc session — never reaped by a sweep (KTD-3)."""
        return set(self._active_by_user)

    def live_session_for_conversation(self, conversation_id: uuid.UUID) -> BuildSession | None:
        """The still-running build attached to THIS thread, or None — the "is the agent working
        here right now?" question the turn/mode routes ask before they let a chat turn in.

        PER-CONVERSATION, not per-user, deliberately: the one-gate rule is "this chat's composer
        is shut while this chat's agent works". A planning chat in another thread of the same
        project is legitimate traffic and stays open (the per-user build LOCK already refuses a
        second BUILD anywhere, which is a different question).

        Reads `_active_by_user` rather than `_sessions` so an ended-but-retained session (kept
        5 minutes for a late SSE reconnect) never reads as live. Inherits this registry's
        single-replica invariant — see `_start_locked` — and goes blind across a restart, which
        is why it is a gate BESIDE the mode check, never a replacement for it.
        """
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
        """Undo a failed `_holding_user_lock` body: tear down any container it CREATED, then
        holder-release the lock (LAST — mirroring `reap_user`'s ordering, so a concurrent
        start can never acquire while the doomed container is still up). Each step is guarded
        separately: a Redis blip on release must never mask the teardown, and vice versa. An
        adopted scope is a no-op — the session owns both from `_do_finalize` onward.

        Created, not merely assigned: a SPARED handle names a container that either was up
        before this request existed or has since been brought all the way up, so tearing it
        down is not a rollback, it is collateral damage (see `_LockScope.spared`). The lock is
        still released either way — that one IS this request's to give back."""
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
        *,
        spare_app: str | None = None,
    ) -> AsyncIterator[_LockScope]:
        """Reconcile stale state → acquire the one-per-user Redis lock → run the body
        compensated. The ONE skeleton behind `_start_locked` and `relaunch_preview` (their
        pre-checks deliberately differ — see each call site).

        THE TWO WAYS THE LOCK CAN DENY, and why they leave here as different exceptions
        (U3). `acquire_lock` returning `None` now means one thing only — the lock is
        genuinely HELD — so `BuildSessionConflictError` (router 409, carrying the live
        session id) is always a true statement about a real session. A Redis failure
        instead raises `LockUnavailableError` (a `RedisError`), which passes straight
        through to the router's `build_coordination_or_503` and becomes a 503. Before that
        split, an outage was swallowed into the same `None` and every affected user was
        told a build session was already active when none existed.

        `reconcile_user` above runs BEFORE the acquire and calls the deliberately-unguarded
        primitives (see the REDIS-ERROR POLICY in `locks.py`), so a HARD outage usually
        raises there first — a raw `RedisError`, which the same router seam maps to the same
        503. Both shapes land on one status; neither is a 409 and neither is a 500.

        The reconcile passes `certified_dead=True` (#10/R3): every caller of this context
        manager holds the per-user `_start_lock_for` AND has already verified
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
        """
        if not await _the_live_sandbox_is_already_the_one_we_want(redis, user_id, spare_app):
            await reconcile_user(
                redis, user_id, sandbox_client, has_live_session=False, certified_dead=True
            )
        else:
            # SPARE THE CONTAINER, NOT THE LOCK. Reconcile does two jobs, and only one of them
            # is the destructive one this branch exists to skip: it also `reap_lock`s, and that
            # was the ONLY thing clearing a dead process's residual lock on this path. The three
            # facts above (`certified_dead=True`) say any lock still here is residue — a live
            # holder would be in `_active_by_user` in this very process — so skipping the reap
            # along with the reap-through left `acquire_lock` returning None and the RECOVERY
            # BUTTON answering 409, naming no session, until the sweep caught up minutes later.
            await reap_lock(redis, user_id)
        token = await acquire_lock(redis, user_id)
        if token is None:
            raise BuildSessionConflictError(self._active_by_user.get(user_id))
        scope = _LockScope(token=token)
        try:
            yield scope
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

    async def _claim_the_one_build_slot(self, user_id: uuid.UUID) -> None:
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

        Shared by `_start_locked` and `ensure_sandbox` because they are the same claim
        on the same slot: a Write turn attaching a sandbox and a build starting one are
        indistinguishable to the reaper, the Redis lock and the container budget, so they
        must be indistinguishable here too. Both callers hold `_start_lock_for(user_id)`,
        which is what makes the check-then-allocate below atomic per user.
        """
        if user_id not in self._active_by_user:
            return
        blocking_id = self._active_by_user.get(user_id)
        blocking = self._sessions.get(blocking_id) if blocking_id is not None else None
        finalize = blocking.finalize_task if blocking is not None else None
        if blocking is None or not blocking.terminal_committed or finalize is None:
            raise BuildSessionConflictError(blocking_id)
        # The blocking session has already COMMITTED its terminal — it is ended but still
        # finalizing (a refine sent right on the heels of natural completion). Wait (bounded)
        # for the shielded end sequence instead of 409ing the user's own finished build, then
        # fall through to a fresh start; on a timeout or a finalize error, keep the 409.
        try:
            await asyncio.wait_for(asyncio.shield(finalize), timeout=_FINALIZE_GRACE_SECONDS)
        except Exception:
            raise BuildSessionConflictError(blocking_id) from None

    # --- start ---------------------------------------------------------------

    async def start(
        self,
        db: AsyncSession,
        user: User,
        project_id: uuid.UUID,
        prompt: str,
        *,
        conversation_id: uuid.UUID | None = None,
        run_build: RunBuild,
        sandbox_client: SandboxClient,
    ) -> BuildSession:
        # Opportunistic retention sweep — the only guaranteed-recurring seam for this
        # in-process map (nothing evicts it on a timer), so ended sessions never accumulate
        # unboundedly. Narrowed 2026-08-11: scoped to this map, not a claim about the repo.
        self.evict_ended_sessions()
        # R3 — materialize the conversation's attachments FIRST: before the per-user start lock,
        # before the Redis lock, before any container. A `ConversationNotFoundError` (404) or a
        # `BuildAttachmentError` (422) therefore aborts a start that has allocated NOTHING — no
        # lock to release, no sandbox to tear down, no quota burnt. That ordering is the whole
        # point of the fail-first rule here: a build that silently ignores the user's spreadsheet
        # is the bug R3 deletes, and the only honest alternative is refusing to start at all.
        # (Outside the start lock deliberately: blob rehydration is I/O, and it needs no
        # mutual exclusion — it reads the caller's own committed rows.)
        attachments: list[str | BinaryContent] = []
        started_seq: int | None = None
        if conversation_id is not None:
            attachments = await resolve_build_attachments(db, user.id, project_id, conversation_id)
            # The START marker, read in the same breath as the attachments this build consumes —
            # so the two can never disagree about which turns this build saw. `resolve_build_
            # attachments` has just proven the thread is the caller's, which is what earns this
            # unscoped read of its seq space.
            started_seq = await transcript_head_seq(db, conversation_id)
        # Serialize concurrent same-user starts: the whole start (reconcile → acquire →
        # provision → register) runs under one per-user lock, so a second start can't
        # reconcile-away the first start's in-flight lock (held but registry-not-yet-written)
        # and double-allocate a sandbox (the critical reap_lock/fresh-acquire race).
        async with self._start_lock_for(user.id):
            return await self._start_locked(
                db,
                user,
                project_id,
                prompt,
                attachments=attachments,
                conversation_id=conversation_id,
                started_seq=started_seq,
                run_build=run_build,
                sandbox_client=sandbox_client,
            )

    async def save_project_snapshot(
        self,
        db: AsyncSession,
        user: User,
        project_id: uuid.UUID,
        *,
        sandbox_client: SandboxClient,
    ) -> SaveOutcome:
        """THE SAVE — the user's click, and the only thing that writes their work to Blob.

        Requires a LIVE container, because the tree only exists there. `NoLiveSandboxError` is
        the honest answer rather than a silent success: a Save button that reports "saved"
        having saved nothing is worse than one that says the workspace is gone.

        Deliberately does not REQUIRE an in-process session. The common case for a Save is
        exactly the one where there is none — the user finished a turn, read the reply, and
        clicked Save, by which point `finish_turn_sandbox` has popped the slot and pardoned the
        container. Requiring a session would have made Save work only mid-turn, which is when
        nobody clicks it.

        It does REFUSE while a session is actively writing, which is the opposite question and
        a different answer. A save mid-write bundles whatever the agent happens to have on disk
        at that instant — half-written files, a component that references an import that does
        not exist yet — and stores it as the user's saved bundle, which is what Relaunch
        restores. That is not a save, it is a photograph of a workshop mid-swing. Found while
        testing the switch dialog, whose "Save and switch" reached exactly this path: the save
        SUCCEEDED against a live build, and the release then failed, so the user was left with
        a corrupted bundle and an error message.

        SCOPED TO WRITING SESSIONS, not merely attached ones. Ask and Plan pin the container
        too — `_pin_workspace` attaches for every mode — so gating on "a session exists" made
        the ordinary Save button answer "your app is still being built" while the user was
        waiting on a chat answer that could not touch a file. `may_write` comes from the
        mode's toolset (`toolsets_for_mode` hands Ask and Plan a read-only set), so a
        non-writing turn is structurally incapable of the mid-write bundle described above.

        The refusal is a backstop, not the mechanism: the client stops the build first (that is
        what the "still being built" dialog is for) and only then saves, by which point the
        turn's own terminal has left the tree at a coherent point.

        `write_snapshot` commits inside the container before bundling, so a save captures the
        working tree whether or not the agent had committed it — and the bundle carries HEAD's
        whole history, which is what makes the per-slice commits the prompt asks for worth
        anything.

        Returns the new head so the caller can settle its dirty indicator without a second
        round trip."""
        app_id = await _existing_app_id(db, user.id, project_id)
        if app_id is None:
            raise NoLiveSandboxError(project_id)
        if self._writing_session_holds(user.id, app_id):
            raise BuildSessionConflictError(self._active_by_user.get(user.id))
        handle = await self._attach_for_read(user.id, app_id, sandbox_client)
        # THE SAVED VERSION — the user asked for this one. It drives `dirty` and it is what
        # `submit` pins, so it is the one key a platform-initiated write must never touch.
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
        """What is the app currently compiling? — for a tab with no live turn (R17/R18).

        WHY THIS EXISTS SEPARATELY FROM THE TURN STREAM. The compile signal is emitted by the
        turn's preview watcher, so it stops the moment a turn ends. A tab that reloads after a
        red turn therefore has no producer at all: the pane initialises uncovered, and the
        citizen is shown the framework's error screen under a live-preview label — the exact
        thing the cover was built to stop, reachable by pressing F5.

        WHY NOT ON `project_preview_state`. That route's budget is frozen in C3 §8.3 at NO
        container call of any kind, because it is a browser tab on a 45-second timer. This one
        is deliberately its own call, gated by the caller on a preview that is already framed.

        `UNKNOWN` for every unanswerable case — no app, no live container, storage or transport
        trouble — because absent must never read as clean. `compile_state` never raises, so the
        only failure this has to name is "there is nothing to ask"."""
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
        """Is the app the citizen is looking at still the app? — asked by an IDLE tab (U4, R4/R7).

        THE TURN MAY NEVER COME. U2 asks this question at the start of every turn, which catches
        every reversion that happens between one message and the next — and catches nothing at all
        for a citizen who is reading, or in another tab, or at lunch. The completion claim above
        their preview goes on saying "your app is live below" for as long as they leave the page
        open. That is the 2026-08-18 shape with the clock running.

        DELIBERATELY NOT FOLDED INTO `project_preview_state`, whose budget is frozen in C3 §8.3 at
        NO container call of any kind because it is a browser tab on a 45-second timer. This is
        its own call, and the caller fires it only when the preview already reports alive AND a
        completion claim is standing — so the two never both run on a dark pane.

        RATE-LIMITED PER APP, and the limit is the point rather than politeness: without it a tab
        left open overnight is a container exec every 45 seconds, forever, for an answer that
        changes at most once. Repeated asks inside the window return the last answer and make no
        container call.

        NEVER RESTORES AND NEVER DESTROYS. It only reports. The restore belongs to the next turn,
        where the citizen is present, has been told, and can confirm — recovering an app behind
        somebody's back while they are looking at a different tab is not a kindness."""
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
        """Is there anything to save? The container's HEAD against the saved bundle's.

        Compared by COMMIT, not by timestamp or a local dirty flag, because that is the only
        comparison that survives a reload, a second tab, and a process restart — all three of
        which lose in-memory state while the two commits stay exactly where they were.

        `dirty=None` means UNKNOWN, and it is a distinct answer from False: no live container
        (nothing to compare), or a store we could not read. A UI that renders unknown as clean
        tells the user their work is safe when nobody checked."""
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
        """The dirty ladder for a container we ALREADY hold a handle on.

        Split out from `project_save_state` so the #83 reclaim guard can ask "does the outgoing
        project have unsaved work?" through exactly this ladder rather than a second copy of
        it. The attach is the caller's, deliberately: the guard has to tell an unreachable
        container (a ghost — reap it, do not block the user) apart from a reachable one that
        will not answer (unknown — ask), and `_attach_for_read` collapses both into
        `NoLiveSandboxError`.
        """
        state = await container_state(sandbox_client, handle)
        if state is None:
            # Could not ask the container — the only honest unknown.
            return SaveState(app_id=app_id, dirty=None, container_head=None, saved_head=None)
        recovery_at = await _recovery_written_at(app_id)
        saved_head = await _saved_head(app_id)
        # UNCOMMITTED WORK IS DIRTY regardless of what the commits say. This arm is what stops
        # "all changes saved" appearing over files the agent wrote and never committed.
        if state.uncommitted:
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
        """Is there a crash-recovery copy strictly NEWER than the saved version?

        Answered from the store's `last_modified`, not from the bundles: comparing two HEAD
        shas tells you which trees differ, never which came first, so ancestry would be the
        only content-based answer and that costs a download plus real git.

        TWO KINDS OF "NO", and callers must not confuse them. A store that answered and holds
        no newer bundle returns None — nothing to offer. A store that would NOT answer RAISES,
        because those are different facts and the caller about to act on them needs to tell
        them apart: a display surface can render an unreadable store as "no offer", but a
        caller about to RESTORE must not silently hand back an older tree instead of the work
        it was asked for. Everything else — no recovery copy, a missing timestamp — is None.

        Deliberately NOT part of `project_save_state`: that answers "does the user have unsaved
        work in a LIVE container", which is a question about the container. This one is about
        the store and is asked precisely when the container is gone."""
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
        """The key of the bundle holding the app's MOST RECENT tree, or None for the saved one.

        Every automatic restore goes through here, and the reason is a data-loss bug this
        function exists to close. `_resolve_sandbox`'s restore arm used to pull `snapshot_key`
        unconditionally, so after a container was reclaimed the user's next message rebuilt
        their app from the last SAVED tree — and then that turn's own recovery write overwrote
        the recovery bundle with it. Work done after the last Save survived exactly one turn
        and then existed nowhere. Restoring the newest tree is what makes the copy worth
        writing at all.

        THIS IS RESUMPTION, NOT PROMOTION, and the distinction is what keeps it compatible with
        KTD-5e. `snapshot_key` is untouched, `_saved_head` still reads the user's last Save, so
        `dirty` stays true and the Save button still offers itself. The user gets the workspace
        they left; what becomes their saved version is still only ever their click.

        FAILS CLOSED, like every other read on this path. An unreadable store does NOT mean
        "use the saved bundle": that is precisely how a transient blip would restore an older
        tree over newer work, which is the loss this whole mechanism exists to prevent. It
        raises `SnapshotUnavailableError` and the start aborts with the bundles intact — the
        same trade `snapshot_exists_or_bust` already makes for the same reason (R6: ambiguity
        denies). Retries on the way, so a single blip is not an outage."""
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
        """Is this workspace provably empty of the user's work? (#83 follow-up.)

        THE CASE THIS EXISTS FOR. `_pin_workspace` attaches the container for EVERY mode, so
        one Plan or Ask question against a brand-new project takes the one-per-user workspace.
        That container holds the untouched golden template, and `dirty` is True for it —
        `_save_state_of` answers the SAVE BUTTON's question, where a never-built project must
        show a Save button. Read as "unsaved changes" it locked a user out of the project
        holding their actual app, to protect nothing. Observed live.

        WHY THIS IS NOT `head is None`. A fresh provision is never commit-less: the sandbox
        client seeds `bial: golden template baseline` at birth so the agent's commits cannot
        fail on "not a git repository". A pristine container therefore has exactly ONE commit,
        and a check for "no commits" is dead code that never fires — as the first cut of this
        was.

        Four conditions, all required, and each closes a different way work could be hiding:
        commits <= 1 (nothing beyond the baseline), a clean tree (nothing written since —
        this is what catches an agent that wrote files without committing), nothing ever
        saved, and no recovery bundle (`finish_turn_sandbox` writes one on any turn that
        touched files, so its absence means no turn ever did). A count of 0 means the probe
        could not answer, and that is NOT permission: unknown falls through to the refusal.
        """
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
        """#83 — the missing telling. Raise rather than silently destroy another project's work.

        Runs BEFORE `_holding_user_lock`, because that is where the reap happens and by the time
        the reconcile has marked the registry `ending` the container can no longer be attached
        to or questioned. Falling through here means the teardown below proceeds, so every
        `return` is an assertion that nothing will be lost — and only a CERTAIN answer earns
        one. This guard exists to protect real work, not to invent new ways for a start to
        fail, but "I could not tell" is not a reason to destroy something.

        - the live container is the one we want              → attach; nothing is destroyed
        - no registry, or not READY                          → certain: nothing live to lose
        - the name matches no app this user owns             → a ghost; the reconcile clears it
        - the container is CONFIRMED gone (`SandboxGoneError`) → certain: nothing to lose
        - the incumbent is CLEAN                             → nothing to lose; reclaim silently

        And the two that REFUSE rather than fall through, because the honest answer is unknown
        (#83 review, findings 4 and 5):

        - Redis would not answer                             → the registry is unreadable, not
                                                               empty; a container may be live
        - the attach could not CONFIRM anything              → `SandboxNotReadyError` and the
                                                               like; the container may be alive

        Both raise with `dirty=None`, which the error and the client already treat as "may have
        unsaved changes". The user is not wedged by this: *Switch anyway* → `release` tears down
        through `reap_user`, which needs only the registry entry, never an attach.

        Only a reachable incumbent with unsaved (or unknowable) work raises. Nothing here
        writes a snapshot: KTD-5e is untouched, and saving stays the user's explicit action —
        the client offers it, the `release` route performs the teardown.
        """
        redis = get_redis()
        if await _the_live_sandbox_is_already_the_one_we_want(redis, user.id, spare_app):
            return
        # DELIBERATELY UNGUARDED (finding 5). `read_registry` is one of the answer-bearing
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
        # `_writing_session_holds`, NOT `_live_session_holds`, and the difference is a bug this
        # arm shipped with. Every mode pins the container, so the broader predicate is true
        # throughout an ordinary Ask or Plan turn — which put a hammer icon and two Stop buttons
        # in front of a user who had asked a question, and short-circuited `_nothing_to_lose`
        # below, the escape hatch written for exactly that case ("a question is not work").
        if self._writing_session_holds(user.id, occupying.app_id):
            raise SandboxReclaimBlockedError(
                project_id=occupying.project_id,
                project_name=occupying.project_name,
                app_id=occupying.app_id,
                dirty=None,  # deliberately unprobed: see above
                building=True,
            )
        try:
            handle = await self._attach_for_read(user.id, occupying.app_id, sandbox_client)
        except SandboxUnreachableError as exc:
            # The registry named it READY and it would not answer. That is not evidence of
            # absence — a cold container or a supervisor timeout looks identical from here to
            # one holding a day's work. Refuse with the tri-state null rather than guess
            # "clean", which is the one guess that loses work (finding 4).
            raise SandboxReclaimBlockedError(
                project_id=occupying.project_id,
                project_name=occupying.project_name,
                app_id=occupying.app_id,
                dirty=None,
            ) from exc
        except NoLiveSandboxError:
            # The plain parent: the registry is certain nothing of this app's is live.
            return
        state = await self._save_state_of(sandbox_client, handle, occupying.app_id)
        if state.dirty is False:
            return
        if await self._nothing_to_lose(sandbox_client, handle, state):
            return
        # NOTHING TO LOSE — the case that made this guard worse than the bug.
        #
        # `dirty` answers the SAVE BUTTON's question ("is there anything Save would write?"),
        # and for a never-built project the answer is deliberately yes: `_save_state_of` maps
        # "no commit, nothing saved" to dirty so the button appears on exactly the projects
        # that most need it. This guard is asking a different question — "would reclaiming
        # this DESTROY something?" — and there the same state means the opposite.
        #
        # A Plan or Ask turn attaches the container (`_pin_workspace` does, for every mode)
        # and writes nothing. So a user who typed one question into a brand-new project held
        # the workspace with an untouched golden template, which reported "unsaved changes"
        # and locked them out of the project that had their actual app. Observed in live
        # testing, and a straight downgrade on the behaviour this guard replaced.
        #
        # `_nothing_to_lose` is where that different question gets answered, on FOUR conditions
        # — see its docstring for why "no commit in the container" is not one of them: the
        # sandbox client seeds a baseline commit at birth, so a pristine container has exactly
        # one and a no-commits check is dead code. Together they are proof, not inference.

        raise SandboxReclaimBlockedError(
            project_id=occupying.project_id,
            project_name=occupying.project_name,
            app_id=occupying.app_id,
            dirty=state.dirty,
        )

    async def stop_active_work(
        self,
        db: AsyncSession,
        user: User,
        project_id: uuid.UUID,
        *,
        sandbox_client: SandboxClient,
        timeout_s: float = _STOP_ACTIVE_WORK_TIMEOUT_SECONDS,
    ) -> bool:
        """Stop whatever is running in this project and wait for it to settle. Returns True if
        something was actually stopped — the first step of "stop and switch".

        THE FIRST STEP of the three the dialog performs, and the only one that is new: stop →
        save → release. The other two already existed and both refuse while a session is live,
        so this is what unblocks them — and the refusals stay in place as the backstop, which
        is what makes the ordering an invariant rather than a convention a client must honour.

        TWO KINDS OF LIVE, one door. `_start_locked` registers a build session carrying a
        `run_build` task and the whole terminal-commit machinery; `ensure_sandbox` registers a
        Write turn's workspace with no task at all, because the work is running in the turn
        engine instead. From the container's point of view an agent is writing either way, so
        the caller should not have to know which — the branch is here.

        NOT idempotent-by-omission: returning False means nothing was running, which is a
        success the caller can proceed on. A timeout is NOT reported as success — the session
        stays live, the release that follows refuses, and the user is told to try again. Better
        a retry than a container torn out from under a task that never unwound.

        Scoped to the project on purpose. The slot is per-user so at most one thing is live,
        but stopping is destructive to work-in-progress and the caller asked about a specific
        project; stopping a different one because it happened to hold the slot would be the
        silent-action failure this whole issue is about."""
        app_id = await _existing_app_id(db, user.id, project_id)
        if app_id is None or not self._live_session_holds(user.id, app_id):
            return False
        session_id = self._active_by_user.get(user.id)
        session = self._sessions.get(session_id) if session_id is not None else None
        if session is None:
            return False
        if session.task is not None:
            # A BUILD session. `stop` runs the graceful end sequence and awaits the shielded
            # finalize, so when it returns the terminal is committed and the slot is free.
            #
            # BOUNDED HERE, because `stop` takes no timeout of its own: `_end` awaits the
            # cancelled task and `_await_end_sequence` awaits `finalize_task` unbounded, and a
            # finalize does real work (a snapshot bundle over the supervisor) that a wedged
            # container can stall indefinitely. Without this the documented `timeout_s` applied
            # to only one of the two branches, and a user sat in a request that might never
            # return. `shield` so expiring does not cancel the end sequence — it is mid-teardown
            # and killing it there is how containers get orphaned; we stop WAITING, we do not
            # stop the stop. The caller treats a timeout as "still running", which is true.
            with suppress(TimeoutError):
                await asyncio.wait_for(
                    asyncio.shield(asyncio.ensure_future(self.stop(session, sandbox_client))),
                    timeout=timeout_s,
                )
            return True
        # A WRITE TURN's workspace. The work is the engine's; the manager session is only
        # holding the container for it. Imported lazily — `turns.engine` imports this module,
        # so a module-level import is a cycle (the same reason `live_build.py` documents).
        from src.services.turns.engine import get_turn_engine

        await get_turn_engine().stop_user_turn_and_wait(user.id, timeout_s=timeout_s)
        return True

    async def project_preview_state(
        self, db: AsyncSession, user: User, project_id: uuid.UUID
    ) -> PreviewState:
        """What is serving THIS project — and if nothing is, WHY? (#83, reshaped by C3 §8.3.)

        FOUR STATES, NOT ONE BOOLEAN. This used to answer `alive=False` identically for *never
        built*, *another project took the slot*, *asleep*, and *the registry read threw*. Three
        of those are ordinary facts about a workspace; the fourth is an ERROR, and returning it
        wearing the same face as a fact is how the portal came to pull a live preview off the
        screen because Redis hiccuped once.

        THE COST BUDGET IS PART OF THE CONTRACT (C3 §8.3), because the caller is a browser tab
        on a 45-second timer: ONE registry hash read, at most two user-scoped DB rows, at most
        two object-store HEADs — NONE AT ALL on the alive path, which is the overwhelming
        majority of polls — and NO container `exec`, NO attach, NO ARM call, ever. Two
        independent reasons, either sufficient. Reusing `_refuse_if_reclaim_would_destroy_work`
        would drag in `_attach_for_read` and `_save_state_of` (a container round trip) and would
        let a `RedisError` turn a poll into a 503; the "is there unsaved work" question stays on
        the user-initiated 409 where a human is waiting for it. And an attach-based poll would
        make every framed preview touch its container every 45 seconds, which R14 forbids
        outright as a manufactured activity signal — a sandbox nobody is using would look busy
        forever and never be reclaimed.

        The registry read is ONE `hgetall` rather than the two this used to spend: the shared
        comparison (`_registry_serves_and_is_ready`) is applied to a hash we already hold, so
        the start path's predicate and this poll still cannot drift while the error arm — the
        one thing the predicate deliberately swallows — is handled here instead of hidden.

        AND THE RESTORE QUESTION IS ONLY ASKED WHEN ITS ANSWER CAN CHANGE THE SCREEN. This
        used to call `restorable_presence` before the registry read, i.e. on every poll of
        every healthy container — one or two Blob round trips per tab per 45 seconds to
        answer "could we put this back?" about an app that is currently running. Every
        surface that renders the answer (the gone card's Relaunch, the "nothing is lost"
        copy) is a surface that only exists when nothing is serving the project, so the alive
        arm returns `None` — NO CLAIM — and the client falls through to the answer the project
        route already gave it at load. That is what `null` has always meant here, and the
        client's `??` was written for exactly this fall-through."""
        app_id = await _existing_app_id(db, user.id, project_id)
        if app_id is None:
            # No app row, so no bundle key can exist either: `restorable=False` is a CONFIRMED
            # absent here, not an unknown, and skipping the store call is an answer rather than
            # an omission (the same reading `get_project` makes).
            return PreviewState(state=PreviewLifeState.NEVER_BUILT, restorable=False)
        try:
            reg = await read_registry(get_redis(), user.id)
        except RedisNotConfiguredError:
            # A CERTAIN answer, not an ambiguous one (`services/redis/errors.py`): Redis is
            # genuinely optional outside production, and with no coordination store there is no
            # sandbox subsystem at all — so nothing can be serving this project. Reporting that
            # as UNKNOWN would put a permanent "we could not check" on every dev deployment,
            # and letting it escape would 500 a poll, which is what it did before.
            return PreviewState(
                state=PreviewLifeState.ASLEEP, restorable=await restorable_presence(app_id)
            )
        except RedisError:
            # AMBIGUITY. The store exists and would not answer, so this decided nothing —
            # and a thing that decided nothing must not be reported as a fact about a
            # container. Note it is NOT a 503 either: the caller is a poll, and 503ing a
            # background timer would turn a blip into an error the user has to read.
            #
            # The store question is INDEPENDENT of the registry question and still answerable,
            # so it is still asked: an unknown container state is precisely when the pane may
            # have to offer a way back.
            return PreviewState(
                state=PreviewLifeState.UNKNOWN, restorable=await restorable_presence(app_id)
            )
        mine = app_name_for(app_id)
        if reg is not None and _registry_serves_and_is_ready(reg, mine):
            fqdn = reg.get(REGISTRY_FIELD_FQDN)
            # THE HOT PATH, AND IT SPENDS NOTHING ON THE STORE. `restorable` stays `None` —
            # "no claim" — because a running app renders no restore affordance for the answer
            # to change (see the docstring). The client's `restorable ?? hasSavedBuild` reads
            # this as "the poll did not say", exactly as it reads an unreachable store.
            return PreviewState(
                state=PreviewLifeState.ALIVE,
                preview_url=f"https://{fqdn}/" if fqdn else None,
            )
        # Everything below is a workspace that is NOT serving this project — which is the only
        # place the restore offer is rendered, so this is the one place the answer earns its
        # round trip.
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
        """The #83 guard, asked BEFORE the 202 so the answer can be an HTTP 409 (U5).

        `ensure_sandbox` runs inside the detached turn task, where a raise becomes a chat
        message and the client has nothing to act on — no status to branch on, no project id to
        name, no way to offer Save. The same question asked here, beside the route's other
        cheap synchronous gates, gives the client a real refusal it can turn into a choice.

        The guard inside `ensure_sandbox` stays: this one is an early, kind answer, not the
        enforcement. Anything that changes between the two (a sibling tab starting a build) is
        caught there."""
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
        """Give up this project's container, on the user's explicit say-so (#83).

        The teardown the start path used to do behind their back, moved out into an action they
        take. `reap_user` is reused verbatim — mark-ending → teardown → clear registry →
        release lock, in that order — so there is exactly one teardown sequence in the codebase
        and this route cannot drift from the reaper's.

        Refuses while a build is genuinely running for this user: an in-process session owns its
        container's lifecycle, and releasing underneath it is the strand this whole module is
        written to prevent. Returns False when there was nothing of this project's to release,
        which the router reports as a plain success — releasing an already-gone container is the
        outcome the caller asked for.

        `strict=True` on the reap is what keeps that last sentence true. `reap_user`'s lenient
        default returns False BOTH for "nothing was registered" and for "teardown failed", and
        collapsing those here would report a release that did not happen: the caller's next act
        is to start the project that wanted the slot, which walks straight back into the reclaim
        refusal it was just told had been cleared. Strict re-raises the `SandboxError` instead,
        and the router turns it into a 503 the client can retry (#83 review, blocker 2)."""
        async with self._start_lock_for(user.id):
            if user.id in self._active_by_user:
                raise BuildSessionConflictError(self._active_by_user.get(user.id))
            app_id = await _existing_app_id(db, user.id, project_id)
            if app_id is None:
                return False
            redis = get_redis()
            if not await _the_live_sandbox_is_already_the_one_we_want(
                redis, user.id, app_name_for(app_id)
            ):
                return False
            return await reap_user(redis, user.id, sandbox_client, strict=True)

    def _live_session_for(self, user_id: uuid.UUID, app_id: uuid.UUID) -> BuildSession | None:
        """The in-process session holding THIS app's container, if there is one.

        The same authority `_claim_the_one_build_slot` trusts, and for the same reason: a live
        session is the one fact about a container that Redis cannot be asked, because a lapsed
        lock or a stale registry hash says nothing about whether a task is running in this
        process. Single-replica is the deploy invariant that makes it sufficient (see
        `_claim_the_one_build_slot`); a second replica needs the shared lease the idle-suspend
        spike calls a prerequisite, not a second guard bolted on here.

        Covers BOTH kinds of session, which is why it asks about the app rather than the task:
        `_start_locked` registers a build with a `run_build` task, `ensure_sandbox` registers a
        turn's workspace with no task at all."""
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
        """Is an agent actually WRITING into this app's container right now?

        THE NARROWER QUESTION, and the one two callers actually mean. `_pin_workspace` attaches
        the live container for EVERY mode, so "a session is attached" is true throughout an
        ordinary Ask or Plan turn — and answering with that made a read-only question report
        "your app is still being built" and refuse the Save button while the user sat waiting
        for a chat answer. `may_write` comes from the mode's toolset, so this is structural
        rather than a guess about what the agent might be doing.

        Deliberately NOT `workspace_touched` (the orchestrator's live "has it written yet?"
        flag). That answers a moving question — false a moment before the first `write_file`
        and true a moment after — so a save admitted on it could still land mid-write. The
        toolset is fixed for the whole run, which is the property a guard needs."""
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
            # this subclass and refuses rather than guess (finding 4).
            raise SandboxUnreachableError(app_id) from exc

    async def relaunch_preview(
        self,
        db: AsyncSession,
        user: User,
        project_id: uuid.UUID,
        sandbox_client: SandboxClient,
        *,
        prefer_saved: bool = False,
    ) -> RelaunchedPreview:
        """Put a READY sandbox in front of a project's saved app — the #43 "Relaunch preview"
        path for an app whose live build session has already been torn down.

        Resumes the NEWEST tree by default; `prefer_saved` is the user's explicit "put my last
        saved version back" and is the only way to get the older one. The default is inverted
        from the obvious reading on purpose: the failure that costs a user their work is
        restoring an older tree over a newer one, and the failure that costs them nothing is
        showing them their own most recent workspace. Neither is a promotion — `snapshot_key`
        is untouched either way, so `dirty` stays true and Save is still their click.
        `save_state.recoverableWorkAt` is what lets the portal offer the choice.

        TWO ARMS, cheapest first (U1/R1). If the container serving this exact app is already
        up and healthy, ATTACH to it and drive the dev server; only otherwise restore the
        snapshot into a fresh one. The attach arm exists because the button's most common use
        is a repeat — click it twice, or re-open the tab — and the old single arm answered
        that by paying a ~20s ACA delete plus a ~33.5s ACA create to arrive back at the state
        it deleted, while the user waited and watched their running app get demolished. It is
        bounded by one process lifetime: the supervisor bearer stays in-process by design, so
        the first relaunch after a deploy resolves no token, falls through, and restores.
        (Trade-off recorded, not buried: an attached container keeps its BIRTH env, so a
        relaunch no longer rotates the Blob SAS.)

        Deliberately NOT a build (Decision 6): it runs under `_holding_user_lock` (the same
        skeleton as `_start_locked`) but never adopts the lock, never enters `_active_by_user`
        and never spawns a `run_and_finalize` task, so it does NOT occupy the one-per-user
        build slot — the user's next real build never 409s on a relaunched preview. It
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

        Diverges from `_start_locked` in two deliberate ways:
        - No finalize-grace wait on a terminal-committed session: the snapshot relaunch would
          restore is written only by that session's finalize, so 409ing until it settles is
          correct — never unify this with start's `_FINALIZE_GRACE_SECONDS` arm.
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
        """
        async with self._start_lock_for(user.id):
            redis = get_redis()
            user_id = user.id
            if user_id in self._active_by_user:
                raise BuildSessionConflictError(self._active_by_user.get(user_id))
            # WHICH container would satisfy this relaunch? Read-only on purpose, and computed
            # out here because it has to be: `app_id` is not bound until inside the lock, and
            # `resolve_app_for_project` is an UPSERT that mints a DRAFT row — so it can never
            # be the source of a spare name for a request that may still be refused. Without
            # this the lock's reconcile reaped the very container the attach arm below is
            # about to reuse (`_the_live_sandbox_is_already_the_one_we_want` answers False for
            # `spare_app=None`), which is the 20-second ACA delete half of R1.
            spare_app = await _sandbox_name_for_existing_app(db, user_id, project_id)
            # #83 — same guard, same reason as `ensure_sandbox`: Relaunch is the other door
            # into the one slot, and it reclaimed just as silently.
            await self._refuse_if_reclaim_would_destroy_work(
                db, user, spare_app=spare_app, sandbox_client=sandbox_client
            )
            async with self._holding_user_lock(
                redis, user_id, sandbox_client, spare_app=spare_app
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
                # U6's "last saved version" signal: when the newest recorded outcome FAILED,
                # the snapshot being restored is the last SAVED state, not that build's intent.
                # Read here because it must share the request transaction with the gate above;
                # only the RESTORE arm may actually make the claim (see the return below).
                restored_from_failed_build = (
                    await newest_build_outcome_status(db, user_id=user_id, project_id=project_id)
                    is BuildSessionStatus.FAILED
                )
                await db.commit()
                # U1/R1 — THE ATTACH ARM. A relaunch onto a container that is already up and
                # serving this very app is the common case behind the button (the user clicks
                # it again, or re-opens the tab), and restoring it cost a 20s ACA delete plus a
                # 33.5s ACA create to arrive back at the state it started in. `_attach_for_read`
                # already asks exactly the right question — same app, registry READY, token
                # resolvable — so reuse it rather than growing a second predicate that can
                # drift from it. `NoLiveSandboxError` is the ONLY exception narrowed here, and
                # it is the union of every honest "no": no registry, a different app, a
                # container mid-teardown, or a control-plane restart that emptied the
                # in-process token map (the documented R1 bound — the first relaunch after a
                # deploy still pays a full restore). All four fall through to the untouched
                # restore arm below.
                attached = False
                try:
                    scope.handle = await self._attach_for_read(user_id, app_id, sandbox_client)
                    # Compensation must now spare this container: it was up before this
                    # request and is not ours to roll back (see `_LockScope.spared`).
                    attached = True
                    scope.spare()
                except NoLiveSandboxError:
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
                # does not cover it either: a relaunch never enters `_active_by_user`
                # (Decision 6). Without a stay at this point a concurrent sweep tears down the
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
                # On the ATTACH arm it is an optimization instead, and it is NOT
                # unconditionally idempotent — so it fails open (R6). The supervisor answers
                # `/dev/start` with TWO different 409s (`sandbox/supervisor/app.py`): the
                # owned-child one reports `running=True` and the client folds it into the
                # already-running sentinel, but the UNOWNED-SERVER one — the dev port is
                # serving while `_Dev.proc` is dead, which is exactly what the agent leaves
                # behind when it starts its own server through the open-sandbox `run_command`
                # surface — reports `running=False` and the client raises `SandboxError`.
                # Unguarded that would land in compensation, i.e. we would destroy a container
                # for the sin of already serving the page we came to show. `wait_ready` below
                # is the real gate either way, and it answers from the server that is up.
                # Flipped to False only by the attach arm's fail-open readiness path below; it
                # rides out on the response so the pane can label a preview that is framable but
                # not yet serving, instead of being told "ready" and framing a hang.
                ready = True
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
                    # R6, AND WE PAID FOR THIS ONE IN LOST WORK.
                    #
                    # This handler used to `mark_registry_ending` here and re-raise. SL-20 ran it
                    # against real Azure and showed what that costs: `attach_existing` refuses an
                    # `ending` sandbox BEFORE it probes (`services/sandbox/client.py`), so the very
                    # next press took the RESTORE arm — and restore tears the live container down
                    # (`_safe_teardown`) before pulling the last SAVED bundle. Two clicks, and a
                    # citizen's unsaved edits were gone with nothing on screen to say so. The 503
                    # this used to raise is the copy that invited the second click.
                    #
                    # The mistake was reading a readiness timeout as a statement about the
                    # CONTAINER. It is a statement about the generated APP: since U6, `ready` means
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
                    if not attached:
                        raise
                    ready = False
                    _log.warning(
                        "relaunch_attached_container_not_serving_degraded_to_unready",
                        user_id=str(user_id),
                        app_id=str(app_id),
                        budget_s=_ATTACHED_READY_BUDGET_SECONDS,
                    )
                # Past here the container is up, registered and serving — the same state a
                # SUCCESSFUL relaunch leaves behind — so destroying it over a later blip is no
                # longer a rollback (see `_LockScope.spared`). This matters more since U3: the
                # warm request below widened the window between "it works" and "we said so".
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
                # a browser that will immediately frame it (U3/R3). `wait_ready` returning means
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
                # never spawn a finalize task — Decision 6): if it fails, the compensation still
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
            return RelaunchedPreview(
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

    async def _start_locked(
        self,
        db: AsyncSession,
        user: User,
        project_id: uuid.UUID,
        prompt: str,
        *,
        attachments: list[str | BinaryContent],
        conversation_id: uuid.UUID | None,
        started_seq: int | None,
        run_build: RunBuild,
        sandbox_client: SandboxClient,
    ) -> BuildSession:
        redis = get_redis()
        user_id = user.id
        await self._claim_the_one_build_slot(user_id)
        # Not live: reconcile the user's OWN stale state before acquiring (KTD-3 — closes the
        # crashed-tab lockout at the exact moment it matters), then run the provision steps
        # compensated: any failure — a cancelled request included — tears down any container
        # that was created and holder-releases the lock (`_holding_user_lock`).
        # WHICH container would satisfy this build? Read-only, and passed for the same reason
        # the two turn paths pass it — without it `_the_live_sandbox_is_already_the_one_we_want`
        # answers False unconditionally, so EVERY start reaped the live container, including one
        # already serving this very app: a Write turn's pardoned container was torn down and
        # rebuilt from the last SAVED bundle, losing everything since the user's last Save.
        # The SPA no longer calls this route (`BuilderPage.jsx` stopped calling `session.start()`),
        # so this is a landmine rather than a live bug — which is exactly why it gets defused now
        # instead of at whatever future moment the route is re-enabled.
        spare_app = await _sandbox_name_for_existing_app(db, user_id, project_id)
        await self._refuse_if_reclaim_would_destroy_work(
            db, user, spare_app=spare_app, sandbox_client=sandbox_client
        )
        async with self._holding_user_lock(
            redis, user_id, sandbox_client, spare_app=spare_app
        ) as scope:
            app_id = await resolve_app_for_project(db, user_id, project_id)
            await db.commit()
            # The DSN merge lives HERE and not beside the storage merge in
            # `_restore_or_provision`, which has neither `db` nor `project_id` in scope —
            # the database is PROJECT-keyed while everything on that seam is app-keyed.
            # It must also follow the commit above: `ensure_project_database` commits its
            # own claim and its own terminal marker, so calling it earlier would commit a
            # half-built request transaction (the speculative DRAFT app row included).
            # This is also the LAZY ensure: a project created before the feature existed,
            # or while the substrate was unconfigured, is provisioned on its next build.
            env = {
                **build_app_env(app_id),
                **await provision_app_database(db, project_id),
            }
            # `take` records the handle AND spares it when this was the attach arm — a
            # container that was already serving is not this request's to roll back.
            handle = scope.take(await self._resolve_sandbox(sandbox_client, user_id, app_id, env))
            # Seed the heartbeat INSIDE the protected region, BEFORE adopt (mirroring
            # relaunch_preview's seed) so an immediate reconcile can't reap the fresh session.
            # The placement is load-bearing: under the new retry policy a `write_heartbeat`
            # RedisError is a multi-second window, and out here — after adopt, after the block
            # exited — it propagated uncaught, orphaning `_active_by_user[user_id]` forever and
            # leaking the container. Inside the region (scope not yet adopted) its raise is caught
            # by `_holding_user_lock`'s `except BaseException`, which tears the container down and
            # releases the lock, exactly as a failing lock acquire does.
            await write_heartbeat(redis, user_id)
            # The session ADOPTS the lock + container: from here `_do_finalize` owns their
            # release/teardown, so the scope must not release on exit.
            scope.adopt()

        session = BuildSession(
            session_id=uuid.uuid7(),
            user_id=user_id,
            project_id=project_id,
            app_id=app_id,
            prompt=prompt,
            lock_token=scope.token,
            handle=handle,
            # A build is a Write run by definition — `_run_write` builds its agent with
            # `toolsets_for_mode(ConversationMode.WRITE, ...)`, so the sandbox toolset is
            # always present and the tree is always in play.
            may_write=True,
            attachments=attachments,
            conversation_id=conversation_id,
            started_seq=started_seq,
        )
        self._sessions[session.session_id] = session
        self._active_by_user[user_id] = session.session_id

        # U5 — the hidden `build_started` lifecycle row, BEFORE the run task so it precedes
        # every step row. Best-effort past this point by necessity: the lock + container are
        # adopted and the session is registered, so a raise here would strand them — a build
        # missing its start marker is the strictly smaller failure.
        if session.conversation_id is not None and session.started_seq is not None:
            try:
                await write_build_started(
                    db,
                    user_id=user_id,
                    conversation_id=session.conversation_id,
                    session_id=session.session_id,
                    started_seq=session.started_seq,
                )
            except Exception:
                _log.exception(
                    "build_started marker write failed; continuing",
                    session_id=str(session.session_id),
                )

        task = asyncio.create_task(self._run_and_finalize(session, run_build, sandbox_client))
        session.task = task
        self._tasks.add(task)

        def _on_done(finished: asyncio.Task[None]) -> None:
            # Drop the strong ref, then surface any exception that escaped
            # `_run_and_finalize` — a clean run or a stop/force-end cancellation is silent,
            # but a real bug must never die invisibly in a detached background task.
            self._tasks.discard(finished)
            if finished.cancelled():
                return
            exc = finished.exception()
            if exc is not None and not isinstance(exc, asyncio.CancelledError):
                _log.error(
                    "build task exited with an unhandled exception",
                    exc_info=exc,
                    session_id=str(session.session_id),
                    user_id=str(session.user_id),
                )

        task.add_done_callback(_on_done)
        return session

    # --- the Write turn's sandbox (U5) ---------------------------------------

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
        """Attach a live sandbox for a turn — everything `start` allocates, minus the build
        (U5's convergence).

        `may_write` is REQUIRED and has no default, because the caller is the only thing that
        knows and a wrong guess is user-visible in both directions. It is not "is this Write
        mode?" so much as "may this turn's toolset mutate the tree" — the same fact, taken from
        where it is decided. Ask and Plan pin the container exactly as Write does (`_pin_
        workspace` attaches for every mode), so nothing downstream can recover this from the
        session itself; `59b5d13` renaming `ensure_write_sandbox` to `ensure_sandbox` is the
        commit where the name stopped carrying it.

        A Write turn is an ordinary chat turn that happens to hold the sandbox six, so it
        needs the same container, the same one-per-user lock and the same registry entry as a
        build — but no `run_build` task, no `build_started` marker, no attachments and no
        `started_seq`. Those four belong to the C7 build feed, which the turn engine replaces:
        the turn's own frames are the narrative now, and the turn's own rows are the record.

        The claim/allocate skeleton is `_start_locked`'s, deliberately and completely:
        slot claim → reconcile → lock → mint the app row → commit → env → resolve the
        sandbox → heartbeat → adopt. Every one of those steps exists because a build without
        it broke in a way someone had to debug, and a Write turn is allocating exactly the
        same resources against exactly the same reaper. `_resolve_sandbox` keeps all three of
        its arms, `SnapshotUnavailableError` included — refusing to substitute a blank
        template for a snapshot it cannot read is even more important here than on a build,
        because a Write turn would happily start editing the empty template and commit the
        result over the user's real app.

        `resolve_app_for_project` is where a fresh project's app row is minted. That is on
        purpose and it is why this takes `db`: `turns.py`'s liveness pre-check reads the app
        id WITHOUT minting, so the row is created only once a Write turn actually commits to
        running.

        Returns a `BuildSession` with `prompt=""` — the dataclass is
        reused rather than forked because the reaper, the registry sweep and
        `active_session_for` must see this exactly as they see a build's session. The two
        empty fields are the honest answer: there is no build prompt and no mode to restore.
        """
        # Same opportunistic retention sweep `start` runs — this is now a second recurring
        # seam, and ended sessions must not accumulate on a workspace that only ever chats.
        self.evict_ended_sessions()
        async with self._start_lock_for(user.id):
            redis = get_redis()
            user_id = user.id
            await self._claim_the_one_build_slot(user_id)
            # WHICH container would satisfy this turn? Read-only on purpose — `resolve_app_for
            # _project` below MINTS, and minting out here would leave an app row behind for a
            # turn that then gets refused. No app row yet means nothing live can be ours, which
            # is the correct answer for a project's very first turn.
            spare_app = await _sandbox_name_for_existing_app(db, user_id, project_id)
            # #83 — ABOVE the lock, because the lock's reconcile is what destroys the incumbent
            # and an `ending` registry can no longer be attached to or questioned.
            await self._refuse_if_reclaim_would_destroy_work(
                db, user, spare_app=spare_app, sandbox_client=sandbox_client
            )
            async with self._holding_user_lock(
                redis, user_id, sandbox_client, spare_app=spare_app
            ) as scope:
                app_id = await resolve_app_for_project(db, user_id, project_id)
                await db.commit()
                # The DSN merge follows the commit for `_start_locked`'s reason:
                # `ensure_project_database` commits its own claim and its own terminal
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
                # looking at, with every unsaved change in it (#90).
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
        """The one-per-user rehydrate resolution: live registry → attach; otherwise (no
        registry — which a CLEAN end always leaves behind, since finalize deletes it — or
        registry-but-gone) restore the C4 snapshot when one exists, else provision fresh.
        Without the no-registry restore arm every graceful stop→start loop would discard
        the user's work onto a blank template.

        The ATTACH arm passes no `env`, and that is correct: a container keeps its BIRTH
        env forever (ACA env vars are set on the revision, not on a running process). Same
        reason the Blob SAS is not rotated on attach (KTD-3) — and the same consequence for
        `BIAL_DATABASE_URL`: re-pointing an app at a different database means a REBIRTH
        (teardown + restore), never an attach.

        REPORTS ITS ARM (`_ResolvedSandbox.attached`) rather than returning a bare handle. The
        two are indistinguishable downstream, and that indistinguishability had teeth: on the
        attach arm the caller holds a container it did not create, so letting compensation
        treat it as a rollback destroys the very container this function went out of its way to
        reuse. Hand the result to `_LockScope.take`, which applies the rule for you."""
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
        # U2 — THE ONE ARM WHERE THE TREE IS OLDER THAN THIS REQUEST. The other two have just
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
        """Confirm the attached container still holds this app; on confirmed loss, put it back.

        THE SENTENCE COMES BEFORE THE RESTORE, and that ordering is the unit rather than a
        nicety. The recovery path adds tens of seconds of otherwise-silent latency — a full
        bundle of the reverted tree plus a complete restore — during which the citizen is looking
        at a screen that says nothing at all. `announce` is called first, and then the slow work
        happens behind a sentence that explains it.

        NOTHING BUT `REVERTED` REACHES A TEARDOWN. That is not defensive coding, it is the
        entire safety property: `REVERTED` requires three independent facts to agree (see
        `judge_workspace`), and the two unanswerable states leave the container running,
        attached and untouched."""
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
            # AE3. Nothing to put back. The one thing that must NOT happen here is presenting the
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
            # way out (ASM7), so a failure here leaves the container either untouched or gone —
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
        poison. Two consecutive refusals by U3's guard is the signal that the slot rather than the
        turn is the problem: fall back to the user's own Save, which no platform write ever
        touches, and escalate."""
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
        """Bundle the tree we are about to restore over, unless there is provably nothing in it.

        SKIPPED ONLY WHEN WE CAN SEE THAT THERE IS NOTHING TO KEEP. In the headline factory-reset
        case the tree being quarantined IS the baked template, so the write would be a full
        `git bundle` + base64 + upload on the slowest path in the system to preserve nothing.

        THE GUARD IS `provably_bare`, NOT `content_empty`, and the difference is the whole point.
        The plan specified `content_empty` — but `REVERTED` requires `content_empty` by
        construction, so that guard would skip EVERY quarantine and the write would be dead code.
        Worse, `content_empty` is true whenever there is no repository at all, which is exactly
        the case where the working directory may hold the user's entire app with only its `.git`
        missing. Skip when we positively know the tree is the starter template; quarantine when we
        cannot tell."""
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
        """Restore the C4 snapshot when one exists; provision a fresh template ONLY when the
        bundle is CONFIRMED absent.

        R6 — fresh-provision has exactly ONE reachable arm: `StorageNotFoundError`, i.e. the
        store positively answered "no bundle" (a genuinely new app, or one that vanished
        between the head-check and the pull). No error path reaches it. An unknown head state
        or a restore that keeps failing raises `SnapshotUnavailableError` and aborts the
        start, because a fresh template here would be silently overwritten onto the user's
        saved work by finalize's step-1 snapshot."""
        # Ensure the app's Blob container + mint a fresh session SAS ONLY on this birth
        # (provision/restore) arm — never on attach, which reuses the live container's SAS (KTD-3).
        # A configured-store failure propagates: it fails the start before any sandbox handle
        # exists (start's compensation releases the lock; nothing to tear down), and the idempotent
        # container is simply reused on the next start. Disabled storage (dev/test) yields {} — a
        # no-op merge (KTD-2). C9 §6.
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
    ) -> SandboxHandle:
        """Pull the known-present snapshot into a fresh container, with bounded retry.

        `source_key` selects WHICH bundle, defaulting to the app's saved snapshot. Relaunch
        passes the recovery key when the user asks for the work they did after their last save.

        `npm install` lives INSIDE the `set -e` restore script, so a transient npm/registry
        blip surfaces here as a `SandboxError`. This used to fall back to `provision_new` to
        keep the start "recoverable" — the worry being that propagating would strand the
        session, since every later start re-runs the same failing restore. That worry was
        real but the cure was worse than the disease: the bundle EXISTS on this arm, so the
        fallback handed the user a blank template and finalize then snapshotted it over their
        good bundle — silent, permanent data loss (R6).

        The retry is what answers the stranding worry for the case that actually motivated it
        (a TRANSIENT blip): attempt two usually succeeds and the start proceeds normally. A
        PERSISTENT failure — a genuinely corrupt bundle, a lasting registry outage — does
        strand the session, deliberately: a 503 telling the user to retry or contact the
        admin, with their work intact and recoverable, beats a start that silently destroys
        it. "Contact the admin" IS the unstick path; automatic snapshot-quarantine remains
        the noted follow-up.

        `restore_from_snapshot` self-cleans on every exception (teardown + registry delete +
        token evict, C4), so each attempt starts from no container and an exhausted retry
        leaves nothing running — start's compensation only has the lock to release.

        The retry covers BOTH fallible halves of the attempt, because the pull is one too:
        `restore_from_snapshot` opens with `get_storage().get(...)`, so a `StorageAuthError` or
        a transient blob blip is exactly as retryable as an npm blip — and if it escaped
        uncaught it would be a bare 500 with zero retries, which is neither the docstring's
        promise nor a fair answer to the user. `StorageNotFoundError` is the ONE storage
        outcome that must not retry: it is the caller's legitimate fresh-provision arm.
        """
        attempt = 0
        while True:
            attempt += 1
            try:
                return await sandbox_client.restore_from_snapshot(
                    str(user_id), app_name, app_env=env, source_key=source_key
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
        """The C7 `ProgressSink`: buffer the envelope, derive status, refresh liveness,
        and fan out to every live SSE subscriber (no Redis — this IS the transport)."""
        session.envelopes.append(env)
        session.last_seq = env.seq
        session.updated_at = datetime.now(UTC)
        if isinstance(env, PreviewReadyEvent):
            session.status = BuildSessionStatus.READY
            session.preview_url = env.preview_url
        elif isinstance(env, PreviewReconnectingEvent):
            # F8/U5 — the dev-server PROCESS crashed after the preview was framed. A feed-only
            # signal: the C3 status enum is frozen at five members with no "reconnecting" state, so
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
            # `session.snapshot_committed`. It stays because `on_progress` is the generic C7
            # sink: it must derive correct state from any envelope handed to it, including the
            # ones tests push directly, without reaching back into who emitted them.
            session.snapshot_committed = session.snapshot_committed or env.snapshot_committed
        elif session.status == BuildSessionStatus.PROVISIONING:
            session.status = BuildSessionStatus.BUILDING  # first sign of the loop running

        # Build activity = liveness: renew the lock + heartbeat SERVER-side so an active
        # build whose tab is closed keeps its lock and is never reaped as idle. Skipped for
        # a terminal frame (the lock is about to be released) and best-effort (a redis blip
        # must not break the relay).
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
        # never allowed to raise QueueFull back into run_build.
        for queue in list(session.subscribers):
            try:
                queue.put_nowait(env)
            except asyncio.QueueFull:
                session.subscribers.discard(queue)

    # --- completion + the single-owner end sequence --------------------------

    async def _run_and_finalize(
        self, session: BuildSession, run_build: RunBuild, sandbox_client: SandboxClient
    ) -> None:
        """Await the opaque BRAIN task and finalize on a NORMAL or FAILED completion. A
        CANCELLATION is re-raised WITHOUT finalizing here — `stop`/`force_end` own the end
        sequence in that case, running it OUTSIDE the cancelled task so `_finalize`'s awaits
        actually complete (a cancelled task's `finally` awaits would themselves be cancelled,
        leaving the session half-finalized). `_finalize`'s `terminal_committed` guard keeps
        it single-owner across the two paths (KTD-2).

        The `BuildResult` is BRAIN's whole verdict and is threaded into the end sequence: it
        is the ONLY carrier of the outcome (BRAIN emits no terminal frame), so dropping it
        would cost the real `reason`/`status`/`preview_url` of every completed build (R7)."""

        async def sink(env: ProgressEnvelope) -> None:
            await self.on_progress(session, env)

        try:
            result = await run_build(session.session_id, session.user_id, sandbox_client, sink)
        except asyncio.CancelledError:
            raise  # stop/force_end finalizes; just unwind
        except Exception:
            # BRAIN broke its own never-raise invariant (KD-12) — no verdict exists, so the end
            # sequence derives a `build_failed` terminal itself rather than stranding the feed.
            _log.exception("run_build raised", session_id=str(session.session_id))
            await self._finalize(session, _BUILD_FAILED, sandbox_client)
            return
        await self._finalize(session, result.reason, sandbox_client, result=result)

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
        """Write the build-outcome message to the session's thread (003-U5).

        The SERVER records this, not the portal, because the portal is not reliably there: builds
        take minutes and users close tabs, and an in-memory session is evicted 5 minutes after its
        terminal — so a portal-only record would miss exactly the users the record serves. See
        `outcome.py` for the full rationale.

        Best-effort and never raising: this runs inside the end sequence, where a raise would skip
        the terminal frame and hang every SSE feed. A build with no thread (an API-only start with
        no `conversationId`) has nowhere to record and is a no-op, not an error.

        TIME-BOUNDED for the same reason a raise is caught. This is the only step in the end
        sequence that opens a DB session, and a wedged connection (no statement_timeout, a network
        partition) would block here forever — the terminal would never be emitted, every SSE feed
        would hang without `[DONE]`, and `ended_at` would never be stamped, so the session would
        never be evicted. The record is worth waiting seconds for, never the terminal.
        """
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

    async def _pardon_the_container(self, redis: aioredis.Redis, session: BuildSession) -> None:
        """#13/R2 — the success-path alternative to teardown: the container outlives its
        build so the user can actually use what they just built.

        Mirrors `relaunch_preview`'s lifetime model exactly. The registry entry STAYS (it is
        the sweep's only map to the container — deleting it would orphan a live sandbox);
        the bounded stay of execution owns the lifetime (`sweep_all(honor_stay=True)` spares
        the preview until the lease lapses, then reaps through it); and the per-user lock is
        released so a pardoned preview never occupies the one-build slot. Reconcile-on-start
        still reaps THROUGH an unexpired stay (the incoming build needs the slot), which is
        the "cleanly replaced, never orphaned" half of the contract — the freshly written
        step-1 snapshot is what the next start restores.

        ORDER IS LOAD-BEARING: the stay is granted while the lock is STILL HELD. Releasing
        first would open a window where a concurrent sweep sees lock-gone (and, ≤90 s later,
        heartbeat-lapsed) with no lease yet, and executes the container we just pardoned.

        Best-effort per the end-sequence policy (a raise here would hang every SSE feed).
        Degraded modes are all safe: a failed stay grant means the sweep reaps at heartbeat
        lapse (~90 s — the pre-#13 lifetime, never an orphan, because the registry is still
        there to find); a failed lock release means the lock lingers to its TTL and the next
        start's `reap_lock` clears it."""
        try:
            await grant_stay_of_execution(
                redis, session.user_id, writer=DeadlineWriter.TURN_IN_FLIGHT
            )
        except Exception:
            _log.exception(
                "stay grant failed in pardon; the sweep will reap at heartbeat lapse",
                session_id=str(session.session_id),
            )
        if session.lock_token:
            try:
                await release_lock_as_holder(redis, session.user_id, session.lock_token)
            except Exception:
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
        continues to the terminal synthesis (C4/C5 ordering: snapshot → teardown-or-pardon
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

        # 1b. The #46 generation-time detector (plan U1): while the container is still up, flag
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

        # #13/R2 — the pardon decision: ONLY a genuinely successful build keeps its
        # container. `status` (not just the reason string) is part of the test so a
        # hypothetical FAILED verdict carrying a "completed" reason could never leave a
        # broken container running as if it were a success.
        pardoned = (
            reason == _COMPLETED
            and status is BuildSessionStatus.ENDED
            and not session.force_ended
            and session.handle is not None
        )

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
                await self._pardon_the_container(redis, session)
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

        # 4. Emit THE terminal `ended` — the session's one and only terminal frame (R7). It
        #    drives the derived status AND lets every SSE generator emit `[DONE]` (a bare close
        #    would leave status stuck at BUILDING/READY and hang the feed). Must run even if a
        #    prior step raised, so status is always terminal.
        #
        #    WHY HERE, and nowhere else: this point is downstream of the step-1 snapshot, so
        #    `session.snapshot_committed` is settled — true when the C4 bundle actually pushed,
        #    false when it failed/was skipped. BRAIN cannot emit this frame (no `ended` helper
        #    exists on its emitter): anything it emitted would necessarily predate the snapshot
        #    and could only ever report `snapshot_committed=false` — the exact lie R7 fixes.
        #
        #    `status` comes from BRAIN's verdict when there is one — the reason string alone
        #    cannot decide it (an `escalated` end is FAILED, a `quota_exceeded` end is ENDED, and
        #    neither equals `_BUILD_FAILED`). Only the verdict-less paths (stop / force_end /
        #    idle-reap / a raised run_build) fall back to deriving it from the reason.
        #    (`status`/`preview_url` are computed above step 2 — the pardon decision needs them.)

        # 3b. Record the outcome in the thread (003-U5) — BEFORE the terminal frame, so the row is
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
        """The end of a turn: free the slot and hand the container its lease. NO SAVE.

        SAVING IS THE USER'S ACTION (KTD-5e, confirmed by the user 2026-07-30). The agent
        commits inside the container as it works; the bundle reaches Blob only when the user
        clicks Save (`save_project_snapshot`). This method used to snapshot here — first
        unconditionally, then on any mutating turn — which quietly took that decision away
        from them: every message became a new saved version, so there was no such thing as
        trying something and walking away from it.

        The consequence is deliberate and belongs in the UI, not buried here: work that is
        never saved is lost when the container is reclaimed. What earns that is the dirty
        indicator and the leave warning — a user who loses work must have been told, twice,
        that it was unsaved.

        WHAT IS HERE SINCE THE #83 FOLLOW-UP: an autosave to `recovery_key`, which is NOT a
        save and does not touch the bundle above. The bargain this docstring describes still
        costs a user their work on every ending the UI cannot warn about — a crash, a closed
        laptop, the idle reaper — and "you were told twice" only answers the endings a human
        drives. Durability is the platform's job; deciding what becomes a saved version stays
        the user's. Best-effort and swallowed: a safety net that can fail a turn is not one.

        Steps 1b, 2 and 3 of `_do_finalize`, in that order and for those reasons. What is
        deliberately NOT here:
        - The terminal `ended` frame. There is no C7 feed on this path; `TurnEndedFrame` is
          the turn's one terminal and the engine owns it.
        - `_record_outcome`. The turn's own rows are the transcript record now, so writing a
          build-outcome part as well would render the same ending twice.
        - Any mode restore. Write is no longer a dead end the thread has to be rescued
          from — that was the whole point of the convergence.
        - The snapshot, as of 2026-07-30. See above.

        THE CONTAINER IS ALWAYS PARDONED, never torn down, and this is the one place the
        Write path genuinely diverges from `_do_finalize` rather than merely omitting from
        it. A build's container is scaffolding, so it survives only a clean success; a Write
        turn's container IS the preview the user is looking at, and the turn ending is not a
        reason for their app to vanish mid-sentence. Tearing it down would black out the
        iframe the instant the model stopped typing. The lease bounds the lifetime exactly as
        it does for `relaunch_preview` (a live preview that holds no build slot), and a failed
        turn is pardoned for the same reason a successful one is — the user still needs to see
        what happened.

        The pardon keeps the container ALIVE; what lets the next message actually USE it is
        `_the_live_sandbox_is_already_the_one_we_want`, which stops reconcile-on-start from
        reaping through the lease. An earlier version of this docstring claimed the pardon
        alone spared the next message a cold restore. It did not: the reconcile destroyed the
        pardoned container at the start of every single turn, and the user paid a full delete,
        create and restore on each message while looking at a running app.
        """
        redis = get_redis()

        # STILL NO SAVE HERE. The SAVED bundle is pushed only by `save_project_snapshot`, on
        # the user's click. See the docstring: an auto-save on every mutating turn silently
        # made each message a new saved version, so there was no such thing as trying
        # something and walking away from it.
        #
        # 1b. The #46 generation-time detector, while the container is still up. A structlog
        #     signal only — never a gate — and it swallows its own failures. Gated on the same
        #     flag: there is nothing new to flag about a tree this turn did not write to.
        if session.handle is not None and touched:
            await flag_liveness_overpromise(
                sandbox_client,
                session.handle,
                app_id=session.app_id,
                session_id=session.session_id,
            )

        # 1c. AUTOSAVE to the recovery slot. NOT a save: `recovery_key` is a separate
        #     namespace that `submit` never copies and a relaunch never restores in place of
        #     the user's bundle, so KTD-5e holds — what becomes a saved VERSION is still their
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
                    # THE NUMBER THAT SETTLES 2026-08-18 the next time it happens: the difference
                    # between "the platform failed to CHECK the workspace" and "the platform
                    # failed to make it DURABLE", which nobody could answer on the day.
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
                # no longer SILENT. The swallow is exactly what made the 2026-08-18 reframe
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
        try:
            await self._pardon_the_container(redis, session)
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
            # races the synthetic terminal seq (C7 gap-free invariant). Cancelling the task
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


def reset_session_manager_for_tests() -> None:
    global _manager_singleton
    _manager_singleton = None
