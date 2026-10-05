"""The pool of ready sandboxes: the ledger statements a claim makes, and the bound on pool work.

Each statement runs in a short transaction of its own and commits at once, so a claim is visible
to every other process the moment it is made. The claim is one compare-and-set: it locks one
`ready` row, skipping rows another claim holds, so two starts never receive the same container.

Pool creates, retires and tag restamps in a process all pass through `pool_work_bound`, which
leaves that process's Azure worker threads free for the starts people are waiting on.
"""

from __future__ import annotations

import asyncio
import uuid
from dataclasses import dataclass
from typing import Final

import sqlalchemy as sa

from src.db import base as db_base
from src.db.models.sandbox_pool import SandboxPoolMember, SandboxPoolState

#: How much pool work one process runs at a time.
POOL_WORK_AT_ONCE: Final = 2

_bound: tuple[asyncio.AbstractEventLoop, asyncio.Semaphore] | None = None


@dataclass(frozen=True)
class ClaimedMember:
    """A ledger row this process has just marked claimed."""

    id: uuid.UUID
    name: str
    fqdn: str | None
    image_ref: str


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


async def forget(member_id: uuid.UUID) -> None:
    """Delete a row whose container the registry now records, or whose container is gone."""
    async with db_base.async_session_factory() as db:
        await db.execute(sa.delete(SandboxPoolMember).where(SandboxPoolMember.id == member_id))
        await db.commit()


async def retire(member_id: uuid.UUID) -> None:
    """Mark a claimed row retiring as its container is let go: a later pass deletes a container
    whose delete did not finish."""
    async with db_base.async_session_factory() as db:
        await db.execute(
            sa.update(SandboxPoolMember)
            .where(
                SandboxPoolMember.id == member_id,
                SandboxPoolMember.state == SandboxPoolState.CLAIMED,
            )
            .values(state=SandboxPoolState.RETIRING, state_changed_at=sa.func.now())
        )
        await db.commit()
