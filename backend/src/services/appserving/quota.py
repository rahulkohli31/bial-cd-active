"""Shared per-app quota counter ops (R22, R25) — the ONE atomic conditional reserve
and the unconditional release/adjust, used by BOTH the records and files data-service
routers (and the governance draft-purge decrements).

The reserve is race-free by construction: the UPDATE only matches when both counters
still have room (`counter <= cap - delta`), so there is no read-then-write window. The
adjust path optionally clamps at zero (`GREATEST(0, ...)`) so a bulk decrement can never
drive a counter negative. Keeping this in one place means fixes to the counter math
(reserve/release/purge) stay coherent across every call site.
"""

from __future__ import annotations

import uuid

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import InstrumentedAttribute

from src.core.errors import AppApiError
from src.db.models.app_registry import AppRegistry


async def reserve_app_quota(
    db: AsyncSession,
    app_id: uuid.UUID,
    *,
    count_col: InstrumentedAttribute[int],
    bytes_col: InstrumentedAttribute[int],
    count_cap: int,
    bytes_cap: int,
    d_count: int,
    d_bytes: int,
    error_code: str,
    error_message: str,
) -> None:
    """Atomically reserve quota for an INCREASE, or raise 413. The UPDATE only matches
    when both counters have room, so there is no read-then-write race."""
    reserved = (
        await db.execute(
            sa.update(AppRegistry)
            .where(
                AppRegistry.id == app_id,
                count_col <= count_cap - d_count,
                bytes_col <= bytes_cap - d_bytes,
            )
            .values({count_col: count_col + d_count, bytes_col: bytes_col + d_bytes})
            .returning(AppRegistry.id)
        )
    ).first()
    if reserved is None:
        raise AppApiError(413, error_message, code=error_code)


async def adjust_app_quota(
    db: AsyncSession,
    app_id: uuid.UUID,
    *,
    count_col: InstrumentedAttribute[int],
    bytes_col: InstrumentedAttribute[int],
    d_count: int,
    d_bytes: int,
    clamp: bool = False,
) -> None:
    """Unconditional counter adjust for a RELEASE / decrease. With `clamp`, the result
    is floored at zero (`GREATEST(0, ...)`) so a bulk decrement can never go negative."""
    count_val: sa.ColumnElement[int] = count_col + d_count
    bytes_val: sa.ColumnElement[int] = bytes_col + d_bytes
    if clamp:
        count_val = sa.func.greatest(0, count_val)
        bytes_val = sa.func.greatest(0, bytes_val)
    await db.execute(
        sa.update(AppRegistry)
        .where(AppRegistry.id == app_id)
        .values({count_col: count_val, bytes_col: bytes_val})
    )
