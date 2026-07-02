"""Test data factories — never real user data (testing.md).

The first factory lands with auth; ProjectFactory / ChatFactory follow as those
models arrive. `build()` constructs an unpersisted instance; `create()` adds +
flushes it against the provided session and refreshes it so server defaults
(UUIDv7 id, token_version) are populated.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models.user import User


class UserFactory:
    @staticmethod
    def build(**overrides: Any) -> User:
        data: dict[str, Any] = {
            "azure_oid": f"oid-{uuid.uuid4()}",
            "email": "citizen@rvaiglobal.com",
        }
        data.update(overrides)
        return User(**data)

    @classmethod
    async def create(cls, db: AsyncSession, **overrides: Any) -> User:
        user = cls.build(**overrides)
        db.add(user)
        await db.flush()
        await db.refresh(user)
        return user
