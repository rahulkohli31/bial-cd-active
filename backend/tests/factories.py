"""Test data factories — never real user data (testing.md).

`build()` constructs an unpersisted instance; `create()` adds + flushes it against
the provided session and refreshes it so server defaults (UUIDv7 id, token_version)
are populated.

Since projects landed (R2/KD-4), every app and conversation needs a NOT NULL
`project_id`. `AppRegistryFactory.create` / `ConversationFactory.create` lazily
mint a fresh owning project when `project_id` isn't supplied, so existing call
sites keep working; pass an explicit `project_id=` to place children under the
SAME project (e.g. one-app-per-project or shared-context tests).
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models.app_registry import AppRegistry, AppStatus, mint_app_key
from src.db.models.conversation import Conversation, ConversationKind
from src.db.models.message import Message, MessageRole
from src.db.models.project import Project
from src.db.models.user import User


class ProjectFactory:
    """Builds a project row. `user_id` is required (the ownership boundary)."""

    @staticmethod
    def build(user_id: uuid.UUID, **overrides: Any) -> Project:
        data: dict[str, Any] = {"user_id": user_id, "name": "Test Project"}
        data.update(overrides)
        return Project(**data)

    @classmethod
    async def create(cls, db: AsyncSession, user_id: uuid.UUID, **overrides: Any) -> Project:
        project = cls.build(user_id, **overrides)
        db.add(project)
        await db.flush()
        await db.refresh(project)
        return project


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
        # One app per project (KD-4): mint a fresh owning project unless the caller
        # placed the app in an explicit one (uq_app_registry_project forbids two apps
        # in the same project). The project is owned by the app's user (ADR-0004).
        if "project_id" not in overrides:
            owner = overrides["user_id"]  # ownership boundary is always supplied
            project = await ProjectFactory.create(db, owner)
            overrides["project_id"] = project.id
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
        # Every conversation is a session under a project (R2/KD-4); mint a fresh owning
        # project unless the caller supplied one (pass project_id= to co-locate sessions).
        if "project_id" not in overrides:
            project = await ProjectFactory.create(db, user_id)
            overrides["project_id"] = project.id
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
