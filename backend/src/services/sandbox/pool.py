"""The pool of ready sandboxes: the ledger's statements, the bound on pool work, and the pass that
holds the pool at its size.

Each statement runs in a short transaction of its own and commits at once, so a claim is visible
to every other process the moment it is made. A claim and every retire are compare-and-sets on a
row's state, so two starts never receive the same container and a retire never takes one a start
has claimed. The pass acts only on containers the ledger holds, never on one a registry names.

Pool creates, retires and tag restamps in a process all pass through `pool_work_bound`, which
leaves that process's Azure worker threads free for the starts people are waiting on.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Collection
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from typing import Final, Protocol, runtime_checkable

import sqlalchemy as sa
import structlog

from src.core.alarms import SANDBOX_POOL_BELOW_SIZE_EVENT
from src.db import base as db_base
from src.db.models.pending_teardown import PendingTeardown
from src.db.models.sandbox_pool import SandboxPoolMember, SandboxPoolState
from src.services.sandbox import aca
from src.services.sandbox import client as sandbox_client
from src.services.sandbox.config import SandboxConfig

_log = structlog.get_logger()

#: How much pool work one process runs at a time.
POOL_WORK_AT_ONCE: Final = 2

#: Longer than a pool create or a claim can still be running: each of `_create_with_retry`'s
#: attempts waits at most the ARM ceiling and the longest backoff. A filling or claimed row older
#: than this was left by a process that died.
ROW_DEADLINE: Final = timedelta(
    seconds=sandbox_client._ACA_MAX_ATTEMPTS
    * (aca._LRO_CEILING_SECONDS + sandbox_client._ACA_RETRY_MAX_SECONDS)
)

#: The advisory lock every pass takes, in the worker and at the backend's startup alike.
POOL_LOCK_KEY: Final = 0x50_4F_4F_4C_01  # "POOL" + 01

POOL_PASS_EVENT: Final = "sandbox_pool_pass_completed"
POOL_PASS_LOCKED_OUT_EVENT: Final = "sandbox_pool_pass_locked_out"

_bound: tuple[asyncio.AbstractEventLoop, asyncio.Semaphore] | None = None


@dataclass(frozen=True)
class ClaimedMember:
    """A ledger row this process has just marked claimed."""

    id: uuid.UUID
    name: str
    fqdn: str | None
    image_ref: str


@dataclass(frozen=True)
class PoolPass:
    """What one pass found and did. `ready` is counted as the pass ends; `overdue` says it found a
    filling or claimed row past `ROW_DEADLINE`."""

    target: int
    ready: int
    filled: int
    retired: int
    deleted: int
    refused: bool
    overdue: bool


@runtime_checkable
class PoolKeeper(Protocol):
    """What a pass needs of the sandbox client: one more ready container, made through the bound
    and answering whether the pool gained it, and the delete of one the ledger holds."""

    async def fill_one(self) -> bool: ...

    async def delete_pool_container(self, name: str) -> bool: ...


def pool_work_bound() -> asyncio.Semaphore:
    """This process's bound on pool work. Made for the running event loop, since a semaphore
    belongs to the loop it was first used on."""
    global _bound
    loop = asyncio.get_running_loop()
    if _bound is None or _bound[0] is not loop:
        _bound = (loop, asyncio.Semaphore(POOL_WORK_AT_ONCE))
    return _bound[1]


async def ready_count() -> int:
    async with db_base.async_session_factory() as db:
        counted = await db.scalar(
            sa.select(sa.func.count())
            .select_from(SandboxPoolMember)
            .where(SandboxPoolMember.state == SandboxPoolState.READY)
        )
    return int(counted or 0)


async def claim(image_ref: str) -> ClaimedMember | None:
    """Mark one ready container claimed and return it, or `None` when none is free. One made from
    `image_ref` first; one from an older image only when none of those is ready."""
    one_ready = (
        sa.select(SandboxPoolMember.id)
        .where(SandboxPoolMember.state == SandboxPoolState.READY)
        .order_by(
            (SandboxPoolMember.image_ref == image_ref).desc(),
            SandboxPoolMember.state_changed_at,
            SandboxPoolMember.id,
        )
        .limit(1)
        .with_for_update(skip_locked=True)
        .scalar_subquery()
    )
    async with db_base.async_session_factory() as db:
        row = (
            await db.execute(
                sa.update(SandboxPoolMember)
                .where(
                    SandboxPoolMember.id == one_ready,
                    SandboxPoolMember.state == SandboxPoolState.READY,
                )
                .values(state=SandboxPoolState.CLAIMED, state_changed_at=sa.func.now())
                .returning(
                    SandboxPoolMember.id,
                    SandboxPoolMember.name,
                    SandboxPoolMember.fqdn,
                    SandboxPoolMember.image_ref,
                )
            )
        ).one_or_none()
        await db.commit()
    if row is None:
        return None
    return ClaimedMember(id=row.id, name=row.name, fqdn=row.fqdn, image_ref=row.image_ref)


async def add_filling(name: str, image_ref: str) -> uuid.UUID:
    """Write the row of a container about to be created, before its create, so every count of the
    pool sees the create in flight."""
    async with db_base.async_session_factory() as db:
        member_id = (
            await db.execute(
                sa.insert(SandboxPoolMember)
                .values(
                    name=name,
                    image_ref=image_ref,
                    state=SandboxPoolState.FILLING,
                    state_changed_at=sa.func.now(),
                )
                .returning(SandboxPoolMember.id)
            )
        ).scalar_one()
        await db.commit()
    return member_id


async def mark_ready(member_id: uuid.UUID, fqdn: str) -> bool:
    """Mark a filling row ready at the address its create answered with. False when the row is no
    longer filling: a pass judged the create overdue and let it go."""
    async with db_base.async_session_factory() as db:
        marked = (
            await db.execute(
                sa.update(SandboxPoolMember)
                .where(
                    SandboxPoolMember.id == member_id,
                    SandboxPoolMember.state == SandboxPoolState.FILLING,
                )
                .values(state=SandboxPoolState.READY, fqdn=fqdn, state_changed_at=sa.func.now())
                .returning(SandboxPoolMember.id)
            )
        ).first()
        await db.commit()
    return marked is not None


async def forget(member_id: uuid.UUID) -> None:
    """Delete a row whose container the registry now records, or whose container is gone."""
    async with db_base.async_session_factory() as db:
        await db.execute(sa.delete(SandboxPoolMember).where(SandboxPoolMember.id == member_id))
        await db.commit()


async def retire(
    member_id: uuid.UUID, *, was: SandboxPoolState = SandboxPoolState.CLAIMED
) -> bool:
    """Mark a row retiring as its container is let go, only while it is still `was`, and answer
    whether it was. A pass deletes the container of a retiring row whose delete did not finish."""
    async with db_base.async_session_factory() as db:
        retired = (
            await db.execute(
                sa.update(SandboxPoolMember)
                .where(SandboxPoolMember.id == member_id, SandboxPoolMember.state == was)
                .values(state=SandboxPoolState.RETIRING, state_changed_at=sa.func.now())
                .returning(SandboxPoolMember.id)
            )
        ).first()
        await db.commit()
    return retired is not None


# --- the pass --------------------------------------------------------------------------------


async def keep_the_pool_under_the_lock(keeper: PoolKeeper, config: SandboxConfig) -> None:
    """One pass now, unless another process holds the pool's lock — the second scheduler of a
    deploy, or a backend's startup pass — in which case this one says so and stands down."""
    from src.services.build_sessions.destroy import single_flight_lock

    async with single_flight_lock(POOL_LOCK_KEY) as took_the_lock:
        if not took_the_lock:
            _log.info(POOL_PASS_LOCKED_OUT_EVENT)
            return
        await keep_the_pool(keeper, config, at=datetime.now(UTC))


async def keep_the_pool(keeper: PoolKeeper, config: SandboxConfig, *, at: datetime) -> PoolPass:
    """One pass over the ledger at `at`, an aware instant, logged as one line. It clears rows left
    past `ROW_DEADLINE` and retries deletes that failed; retires ready rows above the size for
    `at`, older images first, which swaps an image change in new before old; then fills until the
    configured image's rows reach the size. A refused create, or a row past its deadline, ends
    the filling for this pass, and the alarm fires if the pool is left below its size."""
    target = config.pool_size_at(at)
    members = await _the_ledger()
    overdue = [
        member
        for member in members
        if member.state in (SandboxPoolState.FILLING, SandboxPoolState.CLAIMED)
        and member.state_changed_at < at - ROW_DEADLINE
    ]
    stuck_claims = [m.name for m in overdue if m.state is SandboxPoolState.CLAIMED]
    in_use: set[str] = await _names_still_in_use(stuck_claims) if stuck_claims else set()
    deleted = 0
    for member in overdue:
        if member.name in in_use:
            # Its claim reached the registry and its row outlived it: the container is somebody's
            # workspace, or their work is still being saved out of it.
            await forget(member.id)
        elif await retire(member.id, was=member.state) and await _let_go(keeper, member):
            deleted += 1
    for member in members:
        if member.state is SandboxPoolState.RETIRING and await _let_go(keeper, member):
            deleted += 1

    ready = sorted(
        (member for member in members if member.state is SandboxPoolState.READY),
        key=lambda member: (member.image_ref == config.image_ref, member.state_changed_at),
    )
    retired = 0
    for member in ready[: max(len(ready) - target, 0)]:
        if await retire(member.id, was=SandboxPoolState.READY):
            retired += 1
            if await _let_go(keeper, member):
                deleted += 1

    filled = 0
    refused = False
    if not overdue:
        for _ in range(target - await _in_hand(config.image_ref)):
            if not await keeper.fill_one():
                refused = True
                break
            filled += 1

    outcome = PoolPass(
        target=target,
        ready=await ready_count(),
        filled=filled,
        retired=retired,
        deleted=deleted,
        refused=refused,
        overdue=bool(overdue),
    )
    _log.info(POOL_PASS_EVENT, **asdict(outcome))
    if outcome.ready < target and (refused or overdue):
        _log.warning(
            SANDBOX_POOL_BELOW_SIZE_EVENT,
            target=target,
            ready=outcome.ready,
            refused=refused,
            overdue=outcome.overdue,
        )
    return outcome


async def _the_ledger() -> list[SandboxPoolMember]:
    async with db_base.async_session_factory() as db:
        return list((await db.scalars(sa.select(SandboxPoolMember))).all())


async def _in_hand(image_ref: str) -> int:
    """Filling and ready rows made from `image_ref`: what the pool holds, or soon will, on it."""
    async with db_base.async_session_factory() as db:
        counted = await db.scalar(
            sa.select(sa.func.count())
            .select_from(SandboxPoolMember)
            .where(
                SandboxPoolMember.image_ref == image_ref,
                SandboxPoolMember.state.in_([SandboxPoolState.FILLING, SandboxPoolState.READY]),
            )
        )
    return int(counted or 0)


async def _names_still_in_use(names: Collection[str]) -> set[str]:
    """Which of `names` a registry record or an owed teardown names. The registry is read first:
    a workspace handed over is owed before its record goes, so a container moving from one to the
    other between the two reads is still seen."""
    from src.services.build_sessions.inventory import registered_app_names
    from src.services.redis import get_redis

    held = await registered_app_names(get_redis()) & set(names)
    async with db_base.async_session_factory() as db:
        held.update(
            await db.scalars(
                sa.select(PendingTeardown.app_name).where(PendingTeardown.app_name.in_(names))
            )
        )
    return held


async def _let_go(keeper: PoolKeeper, member: SandboxPoolMember) -> bool:
    """Delete a retiring row's container, then the row once Azure confirms the container gone."""
    if not await keeper.delete_pool_container(member.name):
        return False
    await forget(member.id)
    return True
