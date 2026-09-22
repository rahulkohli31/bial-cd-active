"""`conversations.updated_at` reads as "last touched by a person": migration 0045's
statement-level trigger advances it on a message insert, alongside the existing ORM
`onupdate` for a title/context PATCH. The default lane proves the trigger's effect against the
real migrated schema inside the per-test transaction; the destructive lane
(`uv run pytest -m destructive_migration`) walks the chain to prove the backfill, which only a
downgrade/upgrade round trip can show.
"""

from __future__ import annotations

import asyncio
import datetime
import uuid
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.config import Config
from pydantic_ai.messages import ModelRequest, UserPromptPart
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from alembic import command
from src.config import settings
from src.db.models.conversation import ChatKind
from src.db.models.message import Message, MessageEntryKind
from src.db.models.user import User
from src.services.messages.store import append_batch
from tests.factories import ConversationFactory, MessageFactory, UserFactory

_BACKEND_ROOT = Path(__file__).resolve().parent.parent.parent
_REVISION = "0045_conversation_touch"
_PRE_REVISION = "0044_chat_kind_generic"
_UTC = datetime.UTC
_LONG_AGO = datetime.datetime(2020, 1, 1, tzinfo=_UTC)


# --- the trigger's effect, against the real migrated schema ---------------------------


async def test_appending_a_message_advances_the_conversations_timestamp(db_session) -> None:
    user = await UserFactory.create(db_session)
    conversation = await ConversationFactory.create(db_session, user.id, updated_at=_LONG_AGO)

    await MessageFactory.create(db_session, user.id, conversation.id, seq=0)

    await db_session.refresh(conversation)
    assert conversation.updated_at > _LONG_AGO


async def test_a_batch_of_several_messages_advances_it_once(db_session) -> None:
    """★ One INSERT statement, three rows, one conversation. A row-level trigger would fire
    three times and leave three dead tuples on the conversation row; the statement-level
    trigger fires once, so this table's OWN transaction-local update counter reads 1."""
    user = await UserFactory.create(db_session)
    conversation = await ConversationFactory.create(db_session, user.id, updated_at=_LONG_AGO)

    async def _conversations_tuples_updated() -> int:
        return await db_session.scalar(
            sa.text("SELECT pg_stat_get_xact_tuples_updated('conversations'::regclass)")
        )

    before = await _conversations_tuples_updated()
    await db_session.execute(
        sa.text(
            "INSERT INTO messages (user_id, conversation_id, seq, entry_kind, kind, payload) "
            "VALUES "
            "(:user_id, :conversation_id, 0, 'turn', 'build', '[]'::jsonb), "
            "(:user_id, :conversation_id, 1, 'turn', 'build', '[]'::jsonb), "
            "(:user_id, :conversation_id, 2, 'turn', 'build', '[]'::jsonb)"
        ),
        {"user_id": user.id, "conversation_id": conversation.id},
    )
    after = await _conversations_tuples_updated()

    assert after - before == 1
    await db_session.refresh(conversation)
    assert conversation.updated_at > _LONG_AGO


async def test_editing_a_conversations_title_still_advances_it(db_session) -> None:
    """Both writers land on the same column: a header edit is a person touching the chat too."""
    user = await UserFactory.create(db_session)
    conversation = await ConversationFactory.create(db_session, user.id, updated_at=_LONG_AGO)

    conversation.title = "renamed"
    await db_session.flush()

    await db_session.refresh(conversation)
    assert conversation.updated_at > _LONG_AGO


async def test_a_conversation_with_no_messages_keeps_its_creation_time(db_session) -> None:
    user = await UserFactory.create(db_session)
    conversation = await ConversationFactory.create(db_session, user.id)

    assert conversation.updated_at == conversation.created_at


def test_the_revision_id_fits_the_version_column() -> None:
    """`alembic_version.version_num` is `varchar(32)` with no headroom left. Compared against a
    LITERAL, never against the same string that produced it — the latter passes at any length."""
    assert _REVISION == "0045_conversation_touch"
    assert len(_REVISION) <= 32


# --- real concurrency: the trigger's row lock does not turn into the contention error --------


@pytest.fixture
async def committed_conversation(
    test_engine: AsyncEngine,
) -> AsyncIterator[tuple[uuid.UUID, uuid.UUID]]:
    """A user + BUILD conversation committed for real, outside the rolled-back `db_session` —
    the concurrency test needs two independent sessions racing over one conversation, which the
    single connection-bound `db_session` fixture cannot provide."""
    async with AsyncSession(test_engine, expire_on_commit=False) as session:
        user = await UserFactory.create(session, email=f"touch-{uuid.uuid4().hex}@rvaiglobal.com")
        conversation = await ConversationFactory.create(session, user.id)
        await session.commit()
        user_id, conversation_id = user.id, conversation.id
    try:
        yield user_id, conversation_id
    finally:
        async with AsyncSession(test_engine, expire_on_commit=False) as session:
            await session.execute(sa.delete(User).where(User.id == user_id))
            await session.commit()


async def test_two_concurrent_appends_produce_gap_free_seqs_and_neither_raises(
    test_engine: AsyncEngine, committed_conversation: tuple[uuid.UUID, uuid.UUID]
) -> None:
    """★ The trigger's UPDATE takes a row lock on the conversation being appended to. This
    proves that lock does not serialise the platform's busiest write path into the seq-
    contention failure: two REAL concurrent appends still land gap-free and consecutive."""
    user_id, conversation_id = committed_conversation

    async def _append(text: str):
        async with AsyncSession(test_engine, expire_on_commit=False) as session:
            return await append_batch(
                session,
                user_id=user_id,
                conversation_id=conversation_id,
                messages=[ModelRequest(parts=[UserPromptPart(content=text)])],
                entry_kind=MessageEntryKind.TURN,
                kind=ChatKind.BUILD,
            )

    first, second = await asyncio.gather(_append("first"), _append("second"))

    assert {first.seq, second.seq} == {0, 1}
    async with AsyncSession(test_engine, expire_on_commit=False) as session:
        count = await session.scalar(
            sa.select(sa.func.count())
            .select_from(Message)
            .where(Message.conversation_id == conversation_id)
        )
    assert count == 2


# --- the chain walk (destructive lane): the backfill --------------------------------------


def _alembic_config() -> Config:
    return Config(str(_BACKEND_ROOT / "alembic.ini"))


@pytest.mark.destructive_migration
def test_the_backfill_sets_an_existing_conversations_timestamp_from_its_newest_message() -> None:
    """★ The newest row is picked by SEQUENCE, not by wall clock: seq 1's timestamp is the
    latest of the three, seeded deliberately out of created_at order, so a `max(created_at)`
    backfill would pick the wrong row and this test would catch it."""
    config = _alembic_config()
    command.upgrade(config, "head")
    command.downgrade(config, _PRE_REVISION)

    user_id, conversation_id = uuid.uuid4(), uuid.uuid4()
    tag = conversation_id.hex[:8]
    created = datetime.datetime(2020, 1, 1, tzinfo=_UTC)
    newest = datetime.datetime(2020, 6, 1, tzinfo=_UTC)
    message_times = (
        datetime.datetime(2020, 3, 1, tzinfo=_UTC),
        newest,
        datetime.datetime(2020, 2, 1, tzinfo=_UTC),
    )

    async def _seed() -> None:
        engine = create_async_engine(settings.DATABASE_URL.get_secret_value(), poolclass=NullPool)
        try:
            async with engine.begin() as conn:
                await conn.execute(
                    sa.text(
                        "INSERT INTO users (id, azure_oid, email, token_version) "
                        "VALUES (:id, :oid, :email, 0)"
                    ),
                    {"id": user_id, "oid": f"b45-{tag}", "email": f"b45-{tag}@rvaiglobal.com"},
                )
                await conn.execute(
                    sa.text(
                        "INSERT INTO conversations "
                        "(id, user_id, project_id, kind, created_at, updated_at) "
                        "VALUES (:id, :user_id, NULL, 'generic', :created, :created)"
                    ),
                    {"id": conversation_id, "user_id": user_id, "created": created},
                )
                for seq, ts in enumerate(message_times):
                    await conn.execute(
                        sa.text(
                            "INSERT INTO messages "
                            "(user_id, conversation_id, seq, entry_kind, visibility, kind, "
                            " payload, created_at, updated_at) "
                            "VALUES (:user_id, :conversation_id, :seq, 'turn', 'visible', "
                            " 'generic', '[]'::jsonb, :ts, :ts)"
                        ),
                        {
                            "user_id": user_id,
                            "conversation_id": conversation_id,
                            "seq": seq,
                            "ts": ts,
                        },
                    )
        finally:
            await engine.dispose()

    async def _updated_at() -> datetime.datetime:
        engine = create_async_engine(settings.DATABASE_URL.get_secret_value(), poolclass=NullPool)
        try:
            async with engine.connect() as conn:
                return await conn.scalar(
                    sa.text("SELECT updated_at FROM conversations WHERE id = :id"),
                    {"id": conversation_id},
                )
        finally:
            await engine.dispose()

    async def _cleanup() -> None:
        engine = create_async_engine(settings.DATABASE_URL.get_secret_value(), poolclass=NullPool)
        try:
            async with engine.begin() as conn:
                await conn.execute(sa.text("DELETE FROM users WHERE id = :id"), {"id": user_id})
        finally:
            await engine.dispose()

    asyncio.run(_seed())
    try:
        command.upgrade(config, "head")
        assert asyncio.run(_updated_at()) == newest
    finally:
        asyncio.run(_cleanup())
