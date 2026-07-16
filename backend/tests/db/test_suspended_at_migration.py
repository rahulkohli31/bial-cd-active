"""`users.suspended_at` round-trips against the REAL migrated schema (U10a).

The test DB carries the column from `alembic upgrade head` (revision
0016_user_suspended_at, now chained under 0017_user_approved_at), so these
exercise the actual DDL — nullable timestamptz, no default — inside the
rolled-back per-test transaction. The upgrade/downgrade round-trip itself is
verified out-of-band via `alembic upgrade head` / `downgrade`
(U10a Verification); `tests/test_alembic_single_head.py` guards the head count,
and `tests/db/test_approved_at_migration.py` now owns the "chain ends at HEAD"
assertion (this revision is no longer the head).
"""

from __future__ import annotations

from datetime import UTC, datetime

from sqlalchemy import select

from src.db.models.user import User
from tests.factories import UserFactory


async def test_fresh_user_is_not_suspended(db_session) -> None:
    user = await UserFactory.create(db_session)
    assert user.suspended_at is None  # NULL = active; no default suspends anyone


async def test_suspended_at_set_and_clear_roundtrip(db_session) -> None:
    user = await UserFactory.create(db_session)
    moment = datetime(2026, 7, 9, 12, 30, tzinfo=UTC)

    user.suspended_at = moment
    await db_session.flush()
    fetched = await db_session.scalar(select(User).where(User.id == user.id))
    assert fetched is not None
    assert fetched.suspended_at == moment  # timestamptz survives intact

    fetched.suspended_at = None  # reactivate clears back to NULL
    await db_session.flush()
    cleared = await db_session.scalar(select(User).where(User.id == user.id))
    assert cleared is not None
    assert cleared.suspended_at is None
