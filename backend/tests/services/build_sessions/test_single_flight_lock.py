"""`single_flight_lock` — the Postgres advisory lock every scheduled destructive pass shares.

A SESSION-SCOPED LOCK ON A POOLED CONNECTION IS NOT SINGLE-FLIGHT, and that is the failure this
whole file is asserted against: a session that releases its connection mid-pass drops the lock
while a caller keeps working believing it is alone. `NullPool` ties the connection's lifetime to
the lock's own hold, so even a hard crash releases it."""

from __future__ import annotations

import pytest
import sqlalchemy as sa

from src.services.build_sessions.destroy import _the_lock_engine, single_flight_lock

#: A key this file owns alone, distinct from any production caller's — two tests racing a
#: production key would be indistinguishable from the exact overlap the lock exists to prevent.
_TEST_LOCK_KEY = 0x54_45_53_54_01  # "TEST" + 01


async def test_a_key_held_on_another_session_yields_false() -> None:
    """*Two schedulers exist during an ACA revision roll*, and both would otherwise run the same
    pass. Simulated by taking the lock on a second session before the body runs, which is
    exactly what the overlapping replica does."""
    async with _the_lock_engine().connect() as holder:
        await holder.execute(sa.select(sa.func.pg_try_advisory_lock(_TEST_LOCK_KEY)))
        try:
            async with single_flight_lock(_TEST_LOCK_KEY) as took_the_lock:
                assert took_the_lock is False
        finally:
            await holder.execute(sa.select(sa.func.pg_advisory_unlock(_TEST_LOCK_KEY)))


async def test_the_lock_does_not_ride_the_application_pool() -> None:
    """NOT THE APPLICATION POOL: `pg_try_advisory_lock` is SESSION-scoped, so riding the shared
    pool risks the connection being released mid-pass (commit, rollback, expiry) — the lock
    vanishes while a caller keeps working believing it is alone. Asserted structurally — the
    failure needs a pool under real contention, which a unit test cannot provoke.

    Mutation-check: drop `poolclass=NullPool`, or take the lock on the caller's session again,
    and this goes red."""
    from sqlalchemy.pool import NullPool

    from src.db.base import engine as application_engine

    lock_engine = _the_lock_engine()

    assert lock_engine is not application_engine, "the lock must not share the request pool"
    assert isinstance(lock_engine.pool, NullPool)
    assert lock_engine.dialect.name == application_engine.dialect.name
    # Built once and reused: a fresh engine per pass would leak connectors on every tick.
    assert _the_lock_engine() is lock_engine


async def test_the_lock_is_taken_again_after_the_body_raises() -> None:
    """A wedged pass holding the lock forever would stop every scheduled pass sharing this
    engine silently — the failure worth not causing."""
    with pytest.raises(RuntimeError):
        async with single_flight_lock(_TEST_LOCK_KEY):
            raise RuntimeError("the body raised")

    # The lock is free again: a fresh caller can take it.
    async with single_flight_lock(_TEST_LOCK_KEY) as took_the_lock:
        assert took_the_lock is True
