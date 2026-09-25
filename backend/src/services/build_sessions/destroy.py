"""The lock primitive shared by every scheduled destructive pass, plus the dev allowlist.

`single_flight_lock` holds a Postgres advisory lock, not Redis (`locks.py` says why Redis
isn't trusted to hold one) — ACA revision overlap means two schedulers can exist during a
deploy, and each caller takes its own key so unrelated passes never stand each other down.
`may_destroy_on_this_control_plane` is the allowlist a scheduled destructive pass checks before
it acts on anything.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine
from sqlalchemy.pool import NullPool

_lock_engine: AsyncEngine | None = None


def _the_lock_engine() -> AsyncEngine:
    """The pass lock's OWN engine — AUTOCOMMIT, `NullPool`, built on first use.
    NOT THE APPLICATION POOL: `pg_try_advisory_lock` is SESSION-scoped, so riding the
    shared pool risks the connection being released mid-pass (commit, rollback, expiry) —
    the lock vanishes while the destroy loop keeps deleting believing it's alone, exactly
    the overlap the lock exists to prevent. `NullPool` gives the lock a connection whose
    lifetime is the pass alone, so even a hard crash frees it via session end. AUTOCOMMIT
    because there's no transaction here to speak of. LAZY, for the reason `appdb/engine.py`
    documents: an eagerly-built engine binds to whichever event loop imported it."""
    global _lock_engine
    if _lock_engine is None:
        from src.config import settings
        from src.db.base import attach_entra_token

        _lock_engine = create_async_engine(
            settings.DATABASE_URL.get_secret_value(),
            isolation_level="AUTOCOMMIT",
            poolclass=NullPool,
            # Same no-parameters-in-logs rule as `db/base.py`. These scheduled passes run
            # unattended, so anything this engine renders into an exception goes straight to
            # an operator log with nobody reading the response.
            hide_parameters=True,
        )
        # MIRRORS `db/base.py` ON THE CREDENTIAL, NOT ON THE WHOLE CONFIGURATION — the rest
        # of this engine is deliberately different (AUTOCOMMIT, `NullPool`), for the reasons
        # above. What it copies is the token attach: a deployment authenticating with an
        # Entra token needs this engine to get one too, or single-flight would fail closed on
        # connect and every pass would report itself locked out by a lock nobody holds.
        if settings.DB_AUTH_MODE == "entra":
            attach_entra_token(_lock_engine)
    return _lock_engine


@asynccontextmanager
async def single_flight_lock(key: int) -> AsyncIterator[bool]:
    """Hold `key`'s advisory lock for the body, on a connection of its own. Yields whether we
    took it.

    THE KEY IS THE CALLER'S, because more than one scheduled pass needs this shape and they must
    not share a lock: two passes doing unrelated destructive work would otherwise stand each other
    down for no reason. What is shared is the ENGINE — one AUTOCOMMIT `NullPool` connection source
    for every lock, because the hazard it answers (a pooled connection recycled mid-pass, silently
    dropping the lock) is the same one whatever the pass.

    The explicit unlock is belt-and-braces over the connection close — with `NullPool` the
    close alone would free it, and saying so twice costs one statement."""
    async with _the_lock_engine().connect() as conn:
        took = bool((await conn.execute(sa.select(sa.func.pg_try_advisory_lock(key)))).scalar())
        try:
            yield took
        finally:
            if took:
                await conn.execute(sa.select(sa.func.pg_advisory_unlock(key)))


def may_destroy_on_this_control_plane(environment: str) -> bool:
    """THE DEV ALLOWLIST. Production only, and no argument gets around it.

    The dev subscription is a test bed holding containers that people are actively using to
    validate features against; deleting one because a scheduled pass said so would destroy the
    evidence. This is the gate every scheduled destructive pass checks before it acts, so it
    makes flipping any pass's own destructive flag in development harmless."""
    return environment == "production"
