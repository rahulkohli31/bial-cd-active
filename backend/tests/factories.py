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

from src.db.models.app_registry import AppRegistry, AppStatus, mint_app_key
from src.db.models.conversation import Conversation, ConversationKind
from src.db.models.message import Message, MessageRole
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


class AppRegistryFactory:
    """Builds an app-registry row. `user_id` is required (the ownership boundary);
    a fresh appKey + conversation link are minted unless overridden."""

    @staticmethod
    def build(**overrides: Any) -> AppRegistry:
        data: dict[str, Any] = {
            "app_key": mint_app_key(),
            "conversation_id": uuid.uuid4(),
            "status": AppStatus.DRAFT,
            "name": "",
        }
        data.update(overrides)
        return AppRegistry(**data)

    @classmethod
    async def create(cls, db: AsyncSession, **overrides: Any) -> AppRegistry:
        app = cls.build(**overrides)
        db.add(app)
        await db.flush()
        await db.refresh(app)
        return app


class ConversationFactory:
    """Builds a conversation row. `user_id` is required (ownership). The id defaults to a
    fresh v4 uuid — the SPA mints client-side ids, so tests do too."""

    @staticmethod
    def build(user_id: uuid.UUID, **overrides: Any) -> Conversation:
        data: dict[str, Any] = {
            "id": uuid.uuid4(),
            "user_id": user_id,
            "kind": ConversationKind.PLANNING,
        }
        data.update(overrides)
        return Conversation(**data)

    @classmethod
    async def create(cls, db: AsyncSession, user_id: uuid.UUID, **overrides: Any) -> Conversation:
        conv = cls.build(user_id, **overrides)
        db.add(conv)
        await db.flush()
        await db.refresh(conv)
        return conv


class MessageFactory:
    """Builds a message row under a conversation. `user_id` + `conversation_id` required."""

    @staticmethod
    def build(user_id: uuid.UUID, conversation_id: uuid.UUID, **overrides: Any) -> Message:
        data: dict[str, Any] = {
            "id": uuid.uuid4(),
            "user_id": user_id,
            "conversation_id": conversation_id,
            "role": MessageRole.USER,
            "seq": 0,
            "parts": [{"type": "text", "text": "hi"}],
        }
        data.update(overrides)
        return Message(**data)

    @classmethod
    async def create(
        cls, db: AsyncSession, user_id: uuid.UUID, conversation_id: uuid.UUID, **overrides: Any
    ) -> Message:
        msg = cls.build(user_id, conversation_id, **overrides)
        db.add(msg)
        await db.flush()
        await db.refresh(msg)
        return msg
