"""The ledger of the pool of ready sandboxes: the statements every other part of the pool runs.

Each statement runs in a short transaction of its own and commits at once, so a claim is visible
to every other process the moment it is made. A claim and every retire are compare-and-sets on a
row's state, so two starts never receive the same container and a retire never takes one a start
has claimed. The pass that holds the pool at its size is `build_sessions/pool_pass.py`.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Final

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from src.db import base as db_base
from src.db.models.sandbox_pool import (
    SandboxPoolMember,
    SandboxPoolState,
    sandbox_pool_state_enum,
)

# Held for the transaction of each fill's row, so two fills never both take the last place.
_FILL_LOCK_KEY: Final = 0x50_4F_4F_4C_02  # "POOL" + 02


@dataclass(frozen=True)
class ClaimedMember:
    """A ledger row this process has just marked claimed."""

    id: uuid.UUID
    name: str
    fqdn: str


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
        .where(
            SandboxPoolMember.state == SandboxPoolState.READY,
            SandboxPoolMember.fqdn.is_not(None),
        )
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
                .returning(SandboxPoolMember.id, SandboxPoolMember.name, SandboxPoolMember.fqdn)
            )
        ).one_or_none()
        await db.commit()
    if row is None:
        return None
    return ClaimedMember(id=row.id, name=row.name, fqdn=str(row.fqdn))


async def add_filling(name: str, image_ref: str, *, up_to: int) -> uuid.UUID | None:
    """Write the row of a container about to be made for the pool, before it waits for anything,
    so every count of the pool sees it; `None`, writing nothing, while the filling and ready rows
    made from `image_ref` already number `up_to`."""
    in_hand = (
        sa.select(sa.func.count())
        .select_from(SandboxPoolMember)
        .where(
            SandboxPoolMember.image_ref == image_ref,
            SandboxPoolMember.state.in_([SandboxPoolState.FILLING, SandboxPoolState.READY]),
        )
        .scalar_subquery()
    )
    the_row = sa.select(
        sa.literal(name),
        sa.literal(image_ref),
        sa.cast(sa.literal(SandboxPoolState.FILLING.value), sandbox_pool_state_enum),
        sa.func.now(),
    ).where(in_hand < up_to)
    async with db_base.async_session_factory() as db:
        await db.execute(sa.select(sa.func.pg_advisory_xact_lock(_FILL_LOCK_KEY)))
        member_id = (
            await db.execute(
                sa.insert(SandboxPoolMember)
                .from_select(["name", "image_ref", "state", "state_changed_at"], the_row)
                .returning(SandboxPoolMember.id)
            )
        ).scalar_one_or_none()
        await db.commit()
    return member_id


async def hold_a_create(name: str, image_ref: str) -> uuid.UUID:
    """Write the row of a container a start is about to create for itself, claimed from the
    outset. Until the registry records the container this row is all that names it, and a claimed
    row past its deadline has its container deleted unless a registry record or an owed teardown
    names it."""
    async with db_base.async_session_factory() as db:
        member_id = (
            await db.execute(
                sa.insert(SandboxPoolMember)
                .values(
                    name=name,
                    image_ref=image_ref,
                    state=SandboxPoolState.CLAIMED,
                    state_changed_at=sa.func.now(),
                )
                .returning(SandboxPoolMember.id)
            )
        ).scalar_one()
        await db.commit()
    return member_id


async def restart_the_clock(member_id: uuid.UUID) -> bool:
    """Restart a filling row's deadline as its create begins, and answer whether the row is still
    filling: a fill queued past its deadline finds its row let go by a pass."""
    return await _move(member_id, SandboxPoolState.FILLING, SandboxPoolState.FILLING)


async def mark_ready(member_id: uuid.UUID, fqdn: str) -> bool:
    """Mark a filling row ready at the address its create answered with. False when the row is no
    longer filling: a pass judged the create overdue and let it go."""
    return await _move(member_id, SandboxPoolState.FILLING, SandboxPoolState.READY, fqdn=fqdn)


async def retire(member_id: uuid.UUID, *, was: SandboxPoolState) -> bool:
    """Mark a row retiring as its container is let go, only while it is still `was`, and answer
    whether it was. A pass deletes the container of a retiring row whose delete did not finish."""
    return await _move(member_id, was, SandboxPoolState.RETIRING)


async def put_back(member_id: uuid.UUID) -> bool:
    """Mark a claimed row ready again, only while it is still claimed: its claim learnt nothing of
    the container, which may serve the next start."""
    return await _move(member_id, SandboxPoolState.CLAIMED, SandboxPoolState.READY)


async def hold_for_deletion(name: str, image_ref: str) -> None:
    """Hold a container whose delete Azure refused on a retiring row, for a pass to retry: a new
    row when a pass has already let the container's own row go, else the row that names it."""
    async with db_base.async_session_factory() as db:
        await db.execute(
            postgresql.insert(SandboxPoolMember)
            .values(
                name=name,
                image_ref=image_ref,
                state=SandboxPoolState.RETIRING,
                state_changed_at=sa.func.now(),
            )
            .on_conflict_do_nothing(constraint="uq_sandbox_pool_name")
        )
        await db.commit()


async def forget(member_id: uuid.UUID) -> None:
    """Delete a row whose container the registry now records, or whose container is gone."""
    async with db_base.async_session_factory() as db:
        await db.execute(sa.delete(SandboxPoolMember).where(SandboxPoolMember.id == member_id))
        await db.commit()


async def the_ledger() -> list[SandboxPoolMember]:
    async with db_base.async_session_factory() as db:
        return list((await db.scalars(sa.select(SandboxPoolMember))).all())


async def _move(
    member_id: uuid.UUID, was: SandboxPoolState, to: SandboxPoolState, **values: Any
) -> bool:
    async with db_base.async_session_factory() as db:
        moved = (
            await db.execute(
                sa.update(SandboxPoolMember)
                .where(SandboxPoolMember.id == member_id, SandboxPoolMember.state == was)
                .values(state=to, state_changed_at=sa.func.now(), **values)
                .returning(SandboxPoolMember.id)
            )
        ).first()
        await db.commit()
    return moved is not None
