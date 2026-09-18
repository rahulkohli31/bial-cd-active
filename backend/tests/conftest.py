"""Shared test fixtures.

`ENV_FILE=.env.test` is set BEFORE importing `src.config` so the Settings
singleton — and the global engine built from it in `src.db.base` — bind to the
test database, not dev/prod. CI may override by exporting ENV_FILE / DATABASE_URL
(real env wins). A name guard refuses to run against a non-"test" database.
"""

from __future__ import annotations

import os

os.environ.setdefault("ENV_FILE", ".env.test")

import httpx  # noqa: E402
import pytest  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402
from sqlalchemy.ext.asyncio import (  # noqa: E402
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm.session import JoinTransactionMode  # noqa: E402
from sqlalchemy.pool import NullPool  # noqa: E402

from src.config import settings  # noqa: E402

# Safety guard: the test DB name must mark it as a test database. Catches a
# missing .env.test (which would silently fall back to the dev DB).
_db_name = make_url(settings.DATABASE_URL.get_secret_value()).database or ""
if "test" not in _db_name:
    raise RuntimeError(
        f"Refusing to run tests against database {_db_name!r}: the test database "
        "name must contain 'test'. Create backend/.env.test (or set ENV_FILE / "
        "DATABASE_URL to a test database)."
    )

# Rebind the app's global engine to NullPool BEFORE any consumer imports
# `async_session_factory` by value. pytest-asyncio runs tests on per-function
# loops, and a pooled asyncpg connection is bound to the loop that created it —
# NullPool opens + closes a fresh connection per checkout so nothing crosses loops.
import src.db.base as _db_base  # noqa: E402

_db_base.engine = create_async_engine(settings.DATABASE_URL.get_secret_value(), poolclass=NullPool)
_db_base.async_session_factory = async_sessionmaker(_db_base.engine, expire_on_commit=False)

from src.db.session import get_db  # noqa: E402
from src.main import create_app  # noqa: E402

TEST_DATABASE_URL = settings.DATABASE_URL.get_secret_value()


@pytest.fixture(scope="session")
def test_engine():
    # Sync fixture yielding the async engine (avoids a session-scoped async
    # fixture, which pytest-asyncio's function loop scope would reject). NullPool
    # means no pooled connection outlives a test, so no explicit dispose is needed.
    return create_async_engine(TEST_DATABASE_URL, poolclass=NullPool)


@pytest.fixture(scope="session", autouse=True)
def _salt_every_provisioned_app_database():
    """Destroy every per-project database/role this SESSION provisioned, at session end.

    `.env.test` configures a real `APP_DB__*` substrate, so a test that provisions a project
    creates a REAL database/role on the shared cluster — the `db_session` rollback runs on a
    different engine and cannot undo that. Patched onto the `provision` MODULE (both callers
    bind `ensure_project_database` by name at import, so patching the name would miss them),
    and scoped to ids this session actually claimed — never a `LIKE` sweep, which on a cluster
    dev and test share would drop a developer's live database.
    """
    import asyncio
    import uuid as _uuid

    from src.services.appdb import names as _names
    from src.services.appdb import provision as _provision
    from src.services.appdb.teardown import salt_the_earth

    claimed: list[_uuid.UUID] = []
    real_claim = _provision._claim

    async def _recording_claim(db: AsyncSession, project_id: _uuid.UUID) -> bool:
        claimed.append(project_id)
        return await real_claim(db, project_id)

    async def _salt() -> None:
        for project_id in claimed:
            await salt_the_earth(
                db_name=_names.database_name(project_id),
                role_name=_names.role_name(project_id),
            )

    # `MonkeyPatch.context()` rather than a bare attribute assignment: the restore is
    # guaranteed, and `setattr`-by-name keeps the type gates out of an argument they cannot
    # win (a module-level function attribute is not re-assignable in their view).
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(_provision, "_claim", _recording_claim)
        yield
    if claimed:
        asyncio.run(_salt())


@pytest.fixture
async def db_session(test_engine, request):
    # Each test runs inside a transaction that is rolled back afterwards, so tests
    # never see each other's writes.
    #
    # `join_transaction_mode="create_savepoint"` is what makes a ROUTE's own `db.rollback()`
    # testable at all: without it, the session joins the outer transaction directly, so a
    # route that rolls back (the concurrent-insert collision arms in `turns.py` and
    # `transition.py`) unwinds the whole test transaction and takes the fixtures with it.
    #
    # OPT-IN, PER TEST — NOT the default. A savepoint-joined session provisions its connection
    # lazily, so a DETACHED task touching the session mid-statement raises
    # `InvalidRequestError: this session is provisioning a new connection` on tests that have
    # no opinion about transaction shape. Only the two collision-arm tests opt in, with
    # `@pytest.mark.route_rollback`; everything else keeps SQLAlchemy's own default.
    join_mode: JoinTransactionMode = (
        "create_savepoint"
        if request.node.get_closest_marker("route_rollback") is not None
        else "conditional_savepoint"  # SQLAlchemy's own default: the shape every other test had
    )
    async with test_engine.connect() as conn:
        transaction = await conn.begin()
        session = AsyncSession(bind=conn, expire_on_commit=False, join_transaction_mode=join_mode)
        yield session
        await session.close()
        await transaction.rollback()


@pytest.fixture
def app(db_session):
    application = create_app()

    async def _override_get_db():
        yield db_session

    application.dependency_overrides[get_db] = _override_get_db
    return application


@pytest.fixture
async def client(app):
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


# THIS FIXTURE AND `fake_storage` BIND AN APP-LEVEL SINGLETON, so a test of the "dependency not
# configured" branch must take NEITHER. With one bound, `get_redis()` / `get_storage()` always
# answer and that branch is unreachable BY CONSTRUCTION — a test written for it proves nothing,
# which is how a documented 503 stayed broken on every deployment that had the dependency switched
# off. Redis, object storage, the sandbox and the per-app database are each genuinely optional
# outside production, so every off-posture is a supported deployment that owes the caller a real
# status; the tests pinning those statuses bind no fixture on purpose.
@pytest.fixture
async def fake_redis():
    # Deterministic in-process Redis: `fakeredis[lua]` runs the compare-and-delete
    # release script in-process, so the lock/registry tests need no live server. We set
    # the app-level singleton directly so `get_redis()` returns the fake, flushed per test.
    import fakeredis.aioredis

    from src.services.redis import client as _redis_client

    fake = fakeredis.aioredis.FakeRedis(decode_responses=True)
    _redis_client._redis_singleton = fake
    yield fake
    await fake.flushall()
    await fake.aclose()
    _redis_client._redis_singleton = None


@pytest.fixture
def fake_storage():
    # Dict-backed object store bound to the app-level singleton so `get_storage()` (called
    # directly by the sandbox restore + the snapshot) round-trips without Azurite.
    from src.services.storage import accessor as _storage_accessor
    from tests.fakes import FakeStorage

    store = FakeStorage()
    _storage_accessor._backend_singleton = store
    yield store
    _storage_accessor._backend_singleton = None


async def forget_every_harness_count() -> None:
    """Empty `harness_counts` in its OWN session, because `count(...)` writes in one too.

    That is not a test smell, it is the feature under test: a count is a historical fact about
    something that HAPPENED and must not disappear because the surrounding transaction rolled
    back. The consequence is that these rows escape the per-test transaction entirely, so a test
    that reads them has to start from a known-empty table rather than from a rollback that cannot
    reach them.
    """
    from sqlalchemy import delete

    from src.db.base import async_session_factory
    from src.db.models.harness_counter import HarnessCount

    async with async_session_factory() as db:
        await db.execute(delete(HarnessCount))
        await db.commit()


@pytest.fixture
async def empty_harness_counts():
    """An empty `harness_counts`, before and after. See `forget_every_harness_count`."""
    await forget_every_harness_count()
    yield
    await forget_every_harness_count()


async def forget_every_worker_pass() -> None:
    """Empty `worker_passes`, for the same reason `harness_counts` is emptied: a pass record is a
    historical fact written in its own committed session, so no per-test rollback reaches it.

    Worth the fixture because the tests that read this table count EVERY row rather than their
    own, so one row left behind by an earlier run turns them red on an assertion that names the
    count and not the cause."""
    from sqlalchemy import delete

    from src.db.base import async_session_factory
    from src.db.models.worker_pass import WorkerPass

    async with async_session_factory() as db:
        await db.execute(delete(WorkerPass))
        await db.commit()


@pytest.fixture
async def empty_worker_passes() -> None:
    """An empty `worker_passes` to start from. See `forget_every_worker_pass`.

    BEFORE ONLY, unlike its `harness_counts` sibling: its users monkeypatch the session factory
    for the duration of the test, so a clean-up after the yield would run through that double
    and not through a real session. Starting clean is what the counting tests need anyway."""
    await forget_every_worker_pass()
