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


@pytest.fixture
async def db_session(test_engine):
    # Each test runs inside a transaction that is rolled back afterwards, so tests
    # never see each other's writes.
    async with test_engine.connect() as conn:
        transaction = await conn.begin()
        session = AsyncSession(bind=conn, expire_on_commit=False)
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


@pytest.fixture
async def fake_redis():
    # Deterministic in-process Redis (KTD-8): `fakeredis[lua]` runs the compare-and-delete
    # release script in-process, so the C5 lock/registry tests need no live server. We set
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
    # directly by the sandbox restore + the C4 snapshot) round-trips without Azurite.
    from src.services.storage import accessor as _storage_accessor

    from tests.fakes import FakeStorage

    store = FakeStorage()
    _storage_accessor._backend_singleton = store
    yield store
    _storage_accessor._backend_singleton = None
