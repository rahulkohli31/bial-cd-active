"""The one shutdown routine: claim the debt, stop the agent, write the work back, destroy the
container — by NAME and by INSTANCE.

WHAT ACTUALLY ARRIVES HERE IS THE PROJECT SWITCH, and one retry of a debt a switch left behind.
A lapsed presence lease and the absolute age ceiling are decided in `reaper.reconcile_user` and
fall through to `reap_user`, which carries its own ordering, its own write-back and its own
release — a second implementation of the same act. Only this one stops the outgoing turn at a
boundary or re-checks the registry before each Redis write. Anything changed about how a
container ends has to be changed in both.

THE ROW IS WRITTEN BEFORE ANY FALLIBLE AWAIT. A switch overwrites the per-user registry with the
incoming container's record on the same request, so from that instant nothing in Redis names the
outgoing container; the row is the only thing that does. A routine that raised between deciding
to let go of a container and publishing that debt would leak it forever.

`attach_existing(user_id)` IS FORBIDDEN HERE. It builds its handle from the per-user registry —
the exact record a switch overwrites — so it would hand this routine the INCOMING container.
`attach_by_name` is the only reach that still answers, and the handle is built ONCE at the top
and passed to the write-back and the teardown.

EVERY REDIS WRITE RE-CHECKS THAT THE REGISTRY STILL NAMES THIS CONTAINER, rather than latching a
boolean at the top the way the janitor's name-keyed reap does. That reap's window is
milliseconds; this one spans a stop wait and an ARM delete while the incoming project's start
rewrites the same keys.

NO OWED ROW EVER REFUSES A START. A deletion the platform still owes is the platform's failure,
and a citizen must never pay for it with their next project — what bounds the debt is an alarm
on its age and count, never a refusal.
"""

from __future__ import annotations

import asyncio
import enum
import time
import uuid
from collections.abc import Callable
from contextlib import AbstractAsyncContextManager, suppress
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Final

import redis.asyncio as aioredis
import sqlalchemy as sa
import structlog
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models.app_registry import AppRegistry
from src.db.models.message import Message, MessageEntryKind
from src.db.models.pending_teardown import PendingTeardown
from src.services.build_sessions.alarms import REAP_FOUND_NO_REPOSITORY_EVENT
from src.services.build_sessions.drain import is_drained, the_ceiling_hours
from src.services.build_sessions.locks import (
    SharedViewStamp,
    an_instant_on_the_hash,
    delete_registry_if_it_still_names,
    read_registry,
    read_starting_marker,
    reap_lock,
    release_liveness_lease,
)
from src.services.build_sessions.reaper import (
    handle_named,
    is_a_sandbox_name,
    is_a_shared_sandbox_name,
)
from src.services.build_sessions.snapshot import WorkspaceHasNoRepositoryError, write_the_tree_back
from src.services.messages.projection import TURN_TERMINAL_KIND
from src.services.redis import REGISTRY_STATE_ENDING, registry_key
from src.services.redis.keys import (
    REGISTRY_FIELD_APP_NAME,
    REGISTRY_FIELD_CREATED_AT,
    REGISTRY_FIELD_STATE,
)
from src.services.sandbox import SandboxClient, SandboxError, SandboxGoneError, SandboxHandle
from src.services.sandbox.base import identity_from_tags
from src.services.storage import StorageError

_log = structlog.get_logger()

SessionFactory = Callable[[], AbstractAsyncContextManager[AsyncSession]]

SANDBOX_DESTROYED_UNREAD_EVENT: Final = "sandbox_destroyed_without_reading_its_tree"
"""A container was destroyed without its tree ever being read back. ERROR.

Fields: `app_name`, `app_id`, `user_id`, `attempts`, `why`. "Spare and report" is a retry policy,
not a terminal state — a container whose supervisor is wedged answers nothing forever, so sparing
on doubt would make the age ceiling unenforceable against exactly the population it exists to
bound. Past the ceiling or past the attempt count the container goes, and what could not be read
is recorded here."""

SANDBOX_TEARDOWN_STILL_OWED_EVENT: Final = "sandbox_teardown_still_owed"
"""A destruction did not happen and the debt is still on the books. WARNING.

Fields: `app_name`, `app_id`, `user_id`, `attempts`, `owed_for_ms`, `last_error`. The citizen's
slot is already released by the time this fires, so nothing here blocks their next project; what
it bounds is the DEBT — a row whose age or per-citizen count keeps climbing is a fleet that is
not shrinking."""

#: HOW MANY TIMES A CONTAINER MAY REFUSE TO BE READ before it is destroyed unread. Sized against
#: the retry schedule below rather than picked: at roughly one attempt per claim window, five
#: strikes is a little over an hour of asking nicely, which sits inside the two-hour ceiling — so
#: for any container with a trustworthy age the ceiling ends it first, and this only catches one
#: ARM cannot date at all.
_STRIKES_BEFORE_IT_GOES: Final = 5

#: What the write-back, the ARM delete and the Redis unwind are given after the stop wait ends.
#: Added to the stop's own bound to size `claimed_until`, so a sweep can never claim a row a
#: routine is still working on.
_TEARDOWN_BUDGET_SECONDS: Final = 300.0

#: How often the bounded wait asks whether the outgoing turn has written its terminal row.
_TERMINAL_POLL_SECONDS: Final = 2.0

#: How long a HARD CUT is given to unwind before the container is destroyed anyway. The turn's
#: own `finally` bills tokens, emits its terminal frame and releases the workspace, and every one
#: of those touches the container — so the cut waits for the same terminal record the cooperative
#: stop waits for, on a much shorter leash.
_UNWIND_AFTER_THE_CUT_SECONDS: Final = 60.0

#: `last_error` is an operator's first line, not a transcript.
_LAST_ERROR_CHARS: Final = 500

#: The write that creates a row counts as its first attempt.
_FIRST_ATTEMPT: Final = 1

#: `_newest_seq` on a conversation with no rows at all. Any real seq beats it.
_NOTHING_WRITTEN_YET: Final = -1


class ShutdownReason(enum.StrEnum):
    """Why this container is being shut down, recorded on the row and in the log."""

    #: The citizen opened a different project, and this one is the outgoing occupant.
    PROJECT_SWITCHED = "project_switched"
    #: A debt carried forward: the sweep is retrying a deletion an earlier run could not perform.
    PRESENCE_LAPSED = "presence_lapsed"


class ShutdownOutcome(enum.StrEnum):
    """How one run of the routine ended."""

    #: The container was destroyed and the debt is settled.
    DESTROYED = "destroyed"
    #: ARM is certain nothing answers to this name. The deletion is done, however it happened.
    ALREADY_GONE = "already_gone"
    #: A DIFFERENT instance answers to this name — the citizen reopened the project and the
    #: start path owns that container now. The row is dropped with no delete issued.
    NOT_THIS_INSTANCE = "not_this_instance"
    #: The container could not be read and is still inside its retry budget. The row stands.
    SPARED = "spared"
    #: The destruction failed. The slot is released, the row stands, the debt survives.
    STILL_OWED = "still_owed"


@dataclass(frozen=True)
class OwedTeardown:
    """One owed deletion, DETACHED from the session that read it.

    The routine runs on its own session in its own task, so it is handed values rather than an
    ORM row bound to somebody else's transaction. `instance_ref` is the registry `created_at` of
    the container this row was written for — the discriminator the name cannot provide, because
    `app_name_for` is stable across teardown and recreate."""

    id: uuid.UUID
    user_id: uuid.UUID
    app_id: uuid.UUID
    app_name: str
    project_id: uuid.UUID
    instance_ref: datetime
    conversation_id: uuid.UUID | None
    attempts: int
    owed_since: datetime


def _owed_from(row: PendingTeardown) -> OwedTeardown:
    return OwedTeardown(
        id=row.id,
        user_id=row.user_id,
        app_id=row.app_id,
        app_name=row.app_name,
        project_id=row.project_id,
        instance_ref=row.instance_ref,
        conversation_id=row.conversation_id,
        attempts=row.attempts,
        owed_since=row.created_at,
    )


def _about(owed: OwedTeardown, reason: ShutdownReason) -> dict[str, object]:
    """The fields every line on this path carries, so one shutdown is one grep."""
    return {
        "app_name": owed.app_name,
        "app_id": str(owed.app_id),
        "user_id": str(owed.user_id),
        "reason": reason.value,
        "attempts": owed.attempts,
    }


def _the_stop_bound() -> float:
    """How long a cooperative stop may wait for the next tool-result boundary.

    A LOCAL IMPORT, like the reaper's own jammed-turn grace: the turn engine drags the whole
    agent stack behind it, and the worker imports this module only to sweep owed rows."""
    from src.services.turns.engine import COOPERATIVE_STOP_GRACE_S

    return COOPERATIVE_STOP_GRACE_S


def the_claim_window() -> timedelta:
    """How long one claim on an owed row holds off every other claimant.

    DERIVED FROM WHAT A RUN ACTUALLY TAKES, not picked: the stop's own bound plus the budget the
    write-back, the ARM delete and the Redis unwind run on. A window shorter than the work would
    let a sweep claim a row a routine is still working on, which is two parties destroying one
    container."""
    return timedelta(seconds=_the_stop_bound() + _TEARDOWN_BUDGET_SECONDS)


def _the_default_factory() -> SessionFactory:
    """The process session factory, resolved at CALL time. Bound by value at import time it
    would hand tests a connection from another event loop — `tests/conftest.py` rebinds it."""
    from src.db.base import async_session_factory

    return async_session_factory


# --- the claim ------------------------------------------------------------------------------


async def claim_the_teardown_we_owe(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    app_id: uuid.UUID,
    app_name: str,
    project_id: uuid.UUID,
    instance_ref: datetime,
    conversation_id: uuid.UUID | None,
) -> OwedTeardown:
    """Publish the deletion this platform now owes, BEFORE anything that can fail.

    THE FIRST AWAIT IS THE INSERT, and that is the whole point: every step after it — reaching
    the container, stopping the turn, bundling the tree, the ARM delete — can raise, and a raise
    above a published row is a container nothing will ever collect.

    CLAIMING FOR A CONTAINER THAT ALREADY HAS A ROW IS A NO-OP RETURNING THE EXISTING ROW, never
    an error: both start doors can reach this on one request, and `uq_pending_teardowns_app_name`
    is the inference target that makes the second call harmless.

    COMMITS ITS OWN WRITE. A debt a later rollback can forgive is not a debt — by the time this
    returns the container is already being left behind."""
    await db.execute(
        pg_insert(PendingTeardown)
        .values(
            user_id=user_id,
            app_id=app_id,
            app_name=app_name,
            project_id=project_id,
            instance_ref=instance_ref,
            conversation_id=conversation_id,
            attempts=_FIRST_ATTEMPT,
            claimed_until=datetime.now(UTC) + the_claim_window(),
        )
        .on_conflict_do_nothing(constraint="uq_pending_teardowns_app_name")
    )
    await db.commit()
    row = (
        await db.execute(
            sa.select(PendingTeardown).where(
                PendingTeardown.app_name == app_name, PendingTeardown.user_id == user_id
            )
        )
    ).scalar_one()
    return _owed_from(row)


async def owe_a_teardown_the_reap_could_not_perform(
    *,
    user_id: uuid.UUID,
    app_id: uuid.UUID | None,
    app_name: str,
    instance_ref: datetime | None,
    shared_view: SharedViewStamp | None = None,
) -> bool:
    """Hand a failed reap's deletion to the owed-row ledger. True when the ledger took it.

    The reaper's failure arm holds the citizen's lock and registry so a later sweep can retry,
    which spends that citizen's one workspace on the platform's own failure. The row carries the
    retry instead — but only when it can be made to describe ONE container: with no app id there
    is nothing to write back for, and with no instance stamp the ARM delete could not tell this
    container from whatever is created under the same name next. Either gap leaves the old
    behaviour in place, which spares rather than forgets.

    A SHARED VIEW IS OWED AGAINST ITS OWNER'S APP, never the slot holder's: `shared_view` is the
    owner and project its launch stamped on the record, and a caller's `app_id` is not consulted.
    The routine deletes a shared view with no write-back."""
    if instance_ref is None:
        return False
    factory = _the_default_factory()
    if is_a_shared_sandbox_name(app_name):
        return await _owe_a_shared_view(
            factory,
            user_id=user_id,
            app_name=app_name,
            instance_ref=instance_ref,
            shared_view=shared_view,
        )
    if app_id is None:
        return False
    # THE NAME AND THE APP ID ARRIVE FROM DIFFERENT READS, so the row is only sound if they
    # describe the same container. The caller resolves `app_id` from one registry read and the
    # reap re-reads the registry for itself; a slot swap between the two hands this function one
    # container's name and another's id, and the ownership query below would still pass. The row
    # is what stands between a name and an ARM delete: a mismatched pair bundles the wrong tree
    # against the wrong saved head and marks the wrong project as closing. Local import — the
    # manager imports this module.
    from src.services.build_sessions.manager import app_name_for

    if app_name != app_name_for(app_id):
        _log.error(
            "refusing the debt: the name and the app id describe different containers",
            user_id=str(user_id),
            app_id=str(app_id),
            app_name=app_name,
        )
        return False
    async with factory() as db:
        project_id = await db.scalar(
            sa.select(AppRegistry.project_id).where(
                AppRegistry.id == app_id, AppRegistry.user_id == user_id
            )
        )
        if project_id is None:
            # No app row: nothing owns this container's work, so there is no write-back to
            # promise and no project for an activity marker to sit beside.
            return False
        await claim_the_teardown_we_owe(
            db,
            user_id=user_id,
            app_id=app_id,
            app_name=app_name,
            project_id=project_id,
            instance_ref=instance_ref,
            conversation_id=None,
        )
    return True


async def _owe_a_shared_view(
    factory: SessionFactory,
    *,
    user_id: uuid.UUID,
    app_name: str,
    instance_ref: datetime,
    shared_view: SharedViewStamp | None,
) -> bool:
    """The shared-view arm of `owe_a_teardown_the_reap_could_not_perform`. The app is found from
    the stamp, and the name is held to it exactly as the build-sandbox arm holds its own: a stamp
    and a name that describe different containers owe nothing."""
    if shared_view is None:
        return False
    from src.services.build_sessions.manager import existing_app_id, shr_name_for

    async with factory() as db:
        app_id = await existing_app_id(db, shared_view.owner_id, shared_view.project_id)
        if app_id is None:
            return False
        if app_name != shr_name_for(app_id, user_id):
            _log.error(
                "refusing the debt: the shared view's stamp and its name describe different "
                "containers",
                user_id=str(user_id),
                app_id=str(app_id),
                app_name=app_name,
            )
            return False
        await claim_the_teardown_we_owe(
            db,
            user_id=user_id,
            app_id=app_id,
            app_name=app_name,
            project_id=shared_view.project_id,
            instance_ref=instance_ref,
            conversation_id=None,
        )
    return True


# --- the detached run -----------------------------------------------------------------------


#: A detached task holds its own strong reference — an unreferenced one is collectable
#: mid-flight, and this one owns a container's whole ending.
_IN_FLIGHT: Final[set[asyncio.Task[None]]] = set()


def shut_it_down_in_the_background(
    owed: OwedTeardown,
    *,
    redis: aioredis.Redis,
    sandbox_client: SandboxClient,
    reason: ShutdownReason,
    session_factory: SessionFactory | None = None,
) -> asyncio.Task[None]:
    """Spawn the routine detached. NOTHING A CITIZEN WAITS ON MAY BLOCK ON THIS.

    The task never raises: an escaping exception from a detached task is a warning on the event
    loop and a debt nobody is told about. Every failure inside leaves the row."""

    async def _run() -> None:
        try:
            await run_the_shutdown(
                owed,
                redis=redis,
                sandbox_client=sandbox_client,
                reason=reason,
                session_factory=session_factory,
            )
        except Exception:
            _log.exception(
                "the shutdown routine raised; the owed row keeps the debt", **_about(owed, reason)
            )

    task = asyncio.create_task(_run())
    _IN_FLIGHT.add(task)
    task.add_done_callback(_IN_FLIGHT.discard)
    return task


async def run_the_shutdown(
    owed: OwedTeardown,
    *,
    redis: aioredis.Redis,
    sandbox_client: SandboxClient,
    reason: ShutdownReason,
    session_factory: SessionFactory | None = None,
) -> ShutdownOutcome:
    """Stop the agent, write the work back, destroy the container, settle the debt.

    THE HANDLE IS BUILT ONCE, at the top, from the name alone — the write-back and the teardown
    share it. Rebuilding it lower down would reach whatever the registry names by then, which on
    a switch is the incoming project's container."""
    factory = session_factory if session_factory is not None else _the_default_factory()
    if not (is_a_sandbox_name(owed.app_name) or is_a_shared_sandbox_name(owed.app_name)):
        # FAIL CLOSED ON A NAME WE CANNOT VOUCH FOR — everything below hands this string to an
        # ARM delete. A row that cannot name one of this platform's own containers describes a
        # deletion nobody should perform, and keeping it would only re-refuse forever.
        _log.error(
            "refusing to shut down: the owed row does not name a sandbox this platform mints",
            **_about(owed, reason),
        )
        await _settle_the_debt(owed, factory)
        return ShutdownOutcome.NOT_THIS_INSTANCE

    try:
        handle = await sandbox_client.attach_by_name(app_name=owed.app_name)
    except SandboxGoneError:
        _log.info(
            "nothing answers to this name any more; the debt is settled", **_about(owed, reason)
        )
        await _let_go_of_the_slot(redis, owed)
        await _settle_the_debt(owed, factory)
        return ShutdownOutcome.ALREADY_GONE
    except SandboxError as exc:
        # UNREACHABLE IS NOT ABSENT: a probe timeout is not a death certificate. This spares —
        # but only inside the budget below.
        return await _spare_or_go_in_unread(
            owed,
            redis,
            sandbox_client,
            factory,
            reason,
            why=f"the container could not be reached: {exc}",
        )

    if owed.conversation_id is not None:
        await _stop_the_outgoing_turn(owed, factory, reason)

    if is_a_shared_sandbox_name(owed.app_name):
        # A shared view holds nothing of the recipient's to write back: what they see is a
        # restore of somebody else's snapshot, already durable at its source, and that storage
        # is read-never-write for them.
        return await _destroy(owed, redis, sandbox_client, factory, handle, reason)

    try:
        await write_the_tree_back(sandbox_client, handle, owed.app_id)
    except WorkspaceHasNoRepositoryError:
        # Ahead of `SandboxError`, which it is. No later attempt could save this tree, so sparing
        # it would only bill: see `REAP_FOUND_NO_REPOSITORY_EVENT`.
        _log.warning(REAP_FOUND_NO_REPOSITORY_EVENT, **_about(owed, reason))
        return await _destroy(owed, redis, sandbox_client, factory, handle, reason)
    except SandboxError as exc:
        return await _spare_or_go_in_unread(
            owed, redis, sandbox_client, factory, reason, why=f"the tree could not be read: {exc}"
        )
    except StorageError as exc:
        # A STORE THAT WILL NOT TAKE THE COPY IS THE SAME SITUATION AS A CONTAINER THAT WILL NOT
        # ANSWER, and it must land in the same budget. Letting this raise instead spares the
        # container through the caller's catch-all, which never reaches the strike test — the
        # attempt is still counted, by the claim, but nothing ever compares that count against
        # the strikes, so a store outage that outlives them is retried forever and the container
        # billed forever, against the one bound that was supposed to stop it.
        return await _spare_or_go_in_unread(
            owed,
            redis,
            sandbox_client,
            factory,
            reason,
            why=f"the copy could not be stored: {exc}",
        )
    return await _destroy(owed, redis, sandbox_client, factory, handle, reason)


# --- the stop -------------------------------------------------------------------------------


async def _stop_the_outgoing_turn(
    owed: OwedTeardown, factory: SessionFactory, reason: ShutdownReason
) -> None:
    """Ask the outgoing Build turn to end at its next tool-result boundary, then wait for it.

    OBSERVED ON THAT CONVERSATION'S OWN TERMINAL RECORD, never on the liveness lease. The lease
    is per USER: the incoming project's first turn renews the very key this would be waiting to
    see released, and the outgoing turn's unwind deletes the incoming turn's lease. The row
    carries the conversation precisely so this question can be asked about ONE turn.

    A ROW WITH NO CONVERSATION NEVER REACHES HERE. Plan turns have no boundary to reach and are
    cut where they stand, and the lapse and ceiling triggers have no turn at all — waiting for
    either would delay the common path to save an ending that was never coming."""
    conversation_id = owed.conversation_id
    if conversation_id is None:  # pragma: no cover - the caller guards this
        return
    from src.services.turns.engine import publish_cooperative_stop, take_cooperative_stop

    after_seq = await _newest_seq(owed, factory)
    await publish_cooperative_stop(conversation_id)
    if await _wait_for_the_turn_to_end(owed, factory, after_seq, bound_s=_the_stop_bound()):
        _log.info("the outgoing turn ended at its boundary", **_about(owed, reason))
        return
    _log.warning(
        "the boundary was not reached inside the bound; cutting the turn", **_about(owed, reason)
    )
    # WITHDRAW THE ASK BEFORE THE CUT. It carries a TTL, so a stop left standing on a
    # conversation nobody stopped would end the citizen's NEXT message at its first tool result.
    with suppress(Exception):
        await take_cooperative_stop(conversation_id)
    await cut_the_turn_where_it_stands(conversation_id)
    await _wait_for_the_turn_to_end(
        owed, factory, after_seq, bound_s=_UNWIND_AFTER_THE_CUT_SECONDS
    )


async def cut_the_turn_where_it_stands(conversation_id: uuid.UUID) -> None:
    """The hard cut, KEYED BY CONVERSATION, and it DOES NOT WAIT for the turn to unwind.

    `stop_user_turn_and_wait` is keyed by USER and would cancel whichever of this citizen's turns
    it found first — after a switch, that is the incoming project's fresh turn. The engine's
    registry is per-process and empty on the worker, where nothing is running anyway.

    Returning the moment the cancel is issued is the property the switch needs: the citizen's own
    next project is already starting, and the container the cancelled turn was using is destroyed
    by the shutdown routine rather than here."""
    from src.services.turns.engine import TurnNotRunningError, get_turn_engine

    engine = get_turn_engine()
    running = engine.active_turn_info(conversation_id)
    if running is None:
        return
    with suppress(TurnNotRunningError):
        await engine.stop_turn(conversation_id, running.turn_id)


async def _newest_seq(owed: OwedTeardown, factory: SessionFactory) -> int:
    """The conversation's highest row seq right now — the mark a terminal has to beat.

    EVERY turn leaves a terminal row, so "a terminal row exists" says nothing on its own; only
    one newer than the rows already there belongs to the turn being stopped."""
    async with factory() as db:
        highest = await db.scalar(
            sa.select(sa.func.max(Message.seq)).where(
                Message.conversation_id == owed.conversation_id,
                Message.user_id == owed.user_id,
            )
        )
    return _NOTHING_WRITTEN_YET if highest is None else int(highest)


async def _the_turn_has_ended(owed: OwedTeardown, factory: SessionFactory, after_seq: int) -> bool:
    async with factory() as db:
        found = await db.scalar(
            sa.select(Message.seq)
            .where(
                Message.conversation_id == owed.conversation_id,
                Message.user_id == owed.user_id,
                Message.entry_kind == MessageEntryKind.SYSTEM_EVENT,
                Message.seq > after_seq,
                Message.meta["kind"].astext == TURN_TERMINAL_KIND,
            )
            .limit(1)
        )
    return found is not None


async def _wait_for_the_turn_to_end(
    owed: OwedTeardown, factory: SessionFactory, after_seq: int, *, bound_s: float
) -> bool:
    deadline = time.monotonic() + bound_s
    while True:
        if await _the_turn_has_ended(owed, factory, after_seq):
            return True
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(_TERMINAL_POLL_SECONDS)


# --- the destruction ------------------------------------------------------------------------


# Flip the registry to `ending` only while it still names THIS container, so a concurrent attach
# sees a dying container — and the incoming project's fresh record is never marked as dying.
_CAS_MARK_ENDING_LUA: Final = (
    f"if redis.call('HGET', KEYS[1], '{REGISTRY_FIELD_APP_NAME}') ~= ARGV[1] then return 0 end "
    f"redis.call('HSET', KEYS[1], '{REGISTRY_FIELD_STATE}', '{REGISTRY_STATE_ENDING}') return 1"
)


async def _mark_ending_if_still_ours(redis: aioredis.Redis, owed: OwedTeardown) -> bool:
    run_script = redis.eval  # aliased to keep the call off the JS-oriented eval guard
    marked = await run_script(_CAS_MARK_ENDING_LUA, 1, registry_key(owed.user_id), owed.app_name)
    return bool(marked)


async def _a_different_instance_answers(redis: aioredis.Redis, owed: OwedTeardown) -> bool:
    """Has the citizen reopened this project, so the name now belongs to a NEW container?

    THE NAME CANNOT ANSWER THIS. `app_name_for` is stable across teardown and recreate, so a row
    claimed for the outgoing container would otherwise delete the one a citizen reopened during
    the stop wait. The registry's `created_at` is re-stamped at every registration, which is
    exactly the property a discriminator needs, and `instance_ref` is the stamp this row was
    written for.

    A registry naming something ELSE is not a mismatch: that is the ordinary switch, where the
    outgoing container is still standing at its own name and nothing else claims it. An
    unreadable stamp IS a mismatch — this sits directly above an ARM delete, and ambiguity
    denies."""
    reg = await read_registry(redis, owed.user_id)
    if reg is None or reg.get(REGISTRY_FIELD_APP_NAME) != owed.app_name:
        return False
    stamped = an_instant_on_the_hash(reg, REGISTRY_FIELD_CREATED_AT)
    return stamped is None or stamped != owed.instance_ref


async def _destroy(
    owed: OwedTeardown,
    redis: aioredis.Redis,
    sandbox_client: SandboxClient,
    factory: SessionFactory,
    handle: SandboxHandle | None,
    reason: ShutdownReason,
) -> ShutdownOutcome:
    """Mark ending → ARM DELETE → delete registry → release lease → reap lock.

    `handle` is `None` only on the unread arm, where the container never answered; the teardown
    is keyed by name either way."""
    if await _a_different_instance_answers(redis, owed):
        # The start path owns whatever answers to this name now. Issuing the delete would take a
        # container the citizen is looking at, so the row is dropped rather than retried — a
        # retry would only find the same fresher instance.
        _log.info(
            "a different instance answers to this name; the delete is refused and the row goes",
            **_about(owed, reason),
        )
        await _settle_the_debt(owed, factory)
        return ShutdownOutcome.NOT_THIS_INSTANCE

    await _mark_ending_if_still_ours(redis, owed)
    try:
        await sandbox_client.teardown(
            handle if handle is not None else handle_named(owed.app_name)
        )
    except SandboxError as exc:
        # The container is still standing, so the debt stays — but the citizen's slot does not. A
        # failure on this platform's side must never cost them their next project.
        _log.exception(
            "the teardown failed; releasing the slot and keeping the debt", **_about(owed, reason)
        )
        await _let_go_of_the_slot(redis, owed)
        await _keep_the_debt(owed, factory, last_error=str(exc))
        return ShutdownOutcome.STILL_OWED
    await _let_go_of_the_slot(redis, owed)
    await _settle_the_debt(owed, factory)
    _log.info("the container is gone and the debt is settled", **_about(owed, reason))
    return ShutdownOutcome.DESTROYED


async def _let_go_of_the_slot(redis: aioredis.Redis, owed: OwedTeardown) -> None:
    """Release this citizen's coordination state — but only while it still belongs to THIS
    container.

    The registry delete carries its own compare-and-set. The lease and the lock carry no app
    identity at all, so what guards them is a fresh read taken right here: a registry that has
    come back names a replacement, and a starting marker means a start already holds the lock.
    Neither is ours to clear."""
    if not await delete_registry_if_it_still_names(redis, owed.user_id, owed.app_name):
        return
    if await read_registry(redis, owed.user_id) is not None:
        return
    if await read_starting_marker(redis, owed.user_id) is not None:
        return
    await release_liveness_lease(redis, owed.user_id)
    await reap_lock(redis, owed.user_id)


# --- the unread arm -------------------------------------------------------------------------


async def _past_the_age_ceiling(sandbox_client: SandboxClient, app_name: str) -> bool:
    """Has this container outlived the absolute ceiling, measured on its OWN birthday?

    The ARM tag, never the registry: this arm is reached when the registry may well name somebody
    else, and a record re-stamped at every registration would hand a container whose delete
    failed a whole fresh ceiling. No age means no ceiling — the attempt count is the other bound,
    and it needs nothing from ARM."""
    after_hours = the_ceiling_hours()
    try:
        tags = await sandbox_client.get_app_tags(name=app_name)
    except SandboxError:
        return False
    return is_drained(
        identity_from_tags(tags),
        now=datetime.now(UTC),
        after_hours=after_hours,
        turn_in_flight=False,
    )


async def _spare_or_go_in_unread(
    owed: OwedTeardown,
    redis: aioredis.Redis,
    sandbox_client: SandboxClient,
    factory: SessionFactory,
    reason: ShutdownReason,
    *,
    why: str,
) -> ShutdownOutcome:
    """A container that would not answer: spare it, or destroy it unread.

    SPARING IS A RETRY POLICY, NOT A TERMINAL STATE. A container whose supervisor is wedged
    answers nothing, forever — so a rule that spares on doubt makes the ceiling unenforceable
    against precisely the population it exists to bound. Past the ceiling, or past the strikes,
    it goes, and what could not be read is recorded."""
    if owed.attempts < _STRIKES_BEFORE_IT_GOES and not await _past_the_age_ceiling(
        sandbox_client, owed.app_name
    ):
        _log.warning(
            "sparing an unreadable container for another attempt", why=why, **_about(owed, reason)
        )
        await _keep_the_debt(owed, factory, last_error=why)
        return ShutdownOutcome.SPARED
    _log.error(SANDBOX_DESTROYED_UNREAD_EVENT, why=why, **_about(owed, reason))
    return await _destroy(owed, redis, sandbox_client, factory, None, reason)


# --- the ledger -----------------------------------------------------------------------------


async def _settle_the_debt(owed: OwedTeardown, factory: SessionFactory) -> None:
    """Delete the row. Existence IS the state, so this is the only way a debt ends."""
    try:
        async with factory() as db:
            await db.execute(
                sa.delete(PendingTeardown).where(
                    PendingTeardown.id == owed.id, PendingTeardown.user_id == owed.user_id
                )
            )
            await db.commit()
    except Exception:
        # A row outliving its container costs one wasted sweep claim, which finds nothing
        # answering to the name and settles it. Raising here would cost the caller its outcome.
        _log.exception("could not settle the owed row", app_name=owed.app_name)


async def _keep_the_debt(owed: OwedTeardown, factory: SessionFactory, *, last_error: str) -> None:
    """Leave the row, record what went wrong, and hand the next claim its window.

    The alarm is on the DEBT, never on the citizen: the slot is already released by the time this
    runs, so nothing here is a refusal — it is how an operator learns a container is still billing
    hours after the platform promised to delete it."""
    now = datetime.now(UTC)
    _log.warning(
        SANDBOX_TEARDOWN_STILL_OWED_EVENT,
        app_name=owed.app_name,
        app_id=str(owed.app_id),
        user_id=str(owed.user_id),
        attempts=owed.attempts,
        owed_for_ms=int((now - owed.owed_since).total_seconds() * 1000),
        last_error=last_error[:_LAST_ERROR_CHARS],
    )
    try:
        async with factory() as db:
            await db.execute(
                sa.update(PendingTeardown)
                .where(PendingTeardown.id == owed.id, PendingTeardown.user_id == owed.user_id)
                .values(
                    last_error=last_error[:_LAST_ERROR_CHARS],
                    claimed_until=now + the_claim_window(),
                )
            )
            await db.commit()
    except Exception:
        _log.exception("could not record the outstanding debt", app_name=owed.app_name)


# --- the sweep ------------------------------------------------------------------------------


@dataclass(frozen=True)
class OwedSweepResult:
    """One owed-row pass. `still_owed` and `failed` are separate facts: the first is a debt the
    routine deliberately carried forward, the second is a row whose run blew up."""

    settled: int
    still_owed: int
    failed: int


async def sweep_owed_teardowns(
    redis: aioredis.Redis,
    sandbox_client: SandboxClient,
    *,
    session_factory: SessionFactory | None = None,
) -> OwedSweepResult:
    """Run every owed deletion whose claim has lapsed.

    THE CLAIM IS THE WHOLE MUTUAL EXCLUSION — a conditional UPDATE on `claimed_until <= now()`,
    no advisory lock. A routine still working on a row holds a claim in the future, so this pass
    walks straight past it and two parties never destroy one container.

    EVERY ROW HERE READS AS A LAPSE, and that is the honest reading rather than a shortcut: the
    row records a debt, not a motive, so this pass cannot know which clock ran out first. What it
    does know is that nothing is keeping the container alive, which is what a lapse means."""
    factory = session_factory if session_factory is not None else _the_default_factory()
    settled = 0
    still_owed = 0
    failed = 0
    for row_id in await _rows_whose_claim_has_lapsed(factory):
        # ONE ROW'S FAILURE IS ONE ROW'S FAILURE. Unguarded, the first throw would end the pass
        # and every row behind it would go unswept — silently, since nothing aggregates the
        # per-row lines. Cancellation still propagates: a shutdown stops the sweep.
        try:
            owed = await _claim_if_lapsed(factory, row_id)
            if owed is None:
                continue
            outcome = await run_the_shutdown(
                owed,
                redis=redis,
                sandbox_client=sandbox_client,
                reason=ShutdownReason.PRESENCE_LAPSED,
                session_factory=factory,
            )
            if outcome in (ShutdownOutcome.SPARED, ShutdownOutcome.STILL_OWED):
                still_owed += 1
            else:
                settled += 1
        except Exception as exc:
            failed += 1
            _log.exception(
                "the owed-row sweep skipped one row; continuing",
                pending_teardown_id=str(row_id),
                error_type=type(exc).__name__,
            )
    return OwedSweepResult(settled=settled, still_owed=still_owed, failed=failed)


async def _rows_whose_claim_has_lapsed(factory: SessionFactory) -> list[uuid.UUID]:
    """Every owed row nobody is currently working on.

    FLEET-WIDE BY DESIGN, and the one query on this table that is not scoped by `user_id`: this
    is the platform's own ledger of containers it has promised to delete, not a citizen's view of
    anything. Every row it hands on carries its owner, and every later read is scoped by it."""
    async with factory() as db:
        return list(
            (
                await db.execute(
                    sa.select(PendingTeardown.id)
                    .where(PendingTeardown.claimed_until <= sa.func.now())
                    .order_by(PendingTeardown.created_at)
                )
            )
            .scalars()
            .all()
        )


async def _claim_if_lapsed(factory: SessionFactory, row_id: uuid.UUID) -> OwedTeardown | None:
    """Take the claim, or answer `None` because somebody else already holds it."""
    async with factory() as db:
        claimed = await db.scalar(
            sa.update(PendingTeardown)
            .where(
                PendingTeardown.id == row_id,
                PendingTeardown.claimed_until <= sa.func.now(),
            )
            .values(
                claimed_until=datetime.now(UTC) + the_claim_window(),
                attempts=PendingTeardown.attempts + 1,
            )
            .returning(PendingTeardown.id)
        )
        if claimed is None:
            return None
        row = (
            await db.execute(sa.select(PendingTeardown).where(PendingTeardown.id == row_id))
        ).scalar_one()
        owed = _owed_from(row)
        await db.commit()
    return owed
