"""Request-scoped async session dependency (ADR-0013).

One `AsyncSession` per request: FastAPI opens it, the endpoint uses it, and it is
rolled back on any exception and closed on the way out. Never a module-global
session — that would leak state across requests.
"""

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession

from src.db.base import async_session_factory


async def get_db() -> AsyncGenerator[AsyncSession]:
    async with async_session_factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise
