"""Auth model round-trips + constraints against the REAL migrated schema.

The test DB carries `users` + `refresh_tokens` from `alembic upgrade head`, so
these exercise the actual PostgreSQL DDL — UUIDv7 / now() server defaults, the
unique indexes, and ON DELETE CASCADE — inside the rolled-back per-test
transaction, not a create_all of a detached metadata.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from src.db.models.refresh_token import RefreshToken
from src.db.models.user import User


def _make_user(**overrides: Any) -> User:
    data: dict[str, Any] = {"azure_oid": str(uuid.uuid4()), "email": "citizen@rvaiglobal.com"}
    data.update(overrides)
    return User(**data)


def _make_token(user_id: uuid.UUID, **overrides: Any) -> RefreshToken:
    now = datetime.now(tz=UTC)
    data: dict[str, Any] = {
        "user_id": user_id,
        "family_id": uuid.uuid7(),
        "token_hash": uuid.uuid4().hex + uuid.uuid4().hex,  # 64 hex chars
        "expires_at": now + timedelta(days=7),
        "absolute_expires_at": now + timedelta(hours=8),
    }
    data.update(overrides)
    return RefreshToken(**data)


async def test_user_roundtrip_defaults(db_session) -> None:
    user = _make_user(upn="citizen@rvaiglobal.com", display_name="A Citizen")
    db_session.add(user)
    await db_session.flush()
    await db_session.refresh(user)  # load server-generated token_version

    assert user.id.version == 7  # UUIDv7 PK (app-side default)
    assert user.token_version == 0  # server_default 0
    assert user.upn == "citizen@rvaiglobal.com"
    assert user.created_at is not None

    fetched = await db_session.scalar(select(User).where(User.azure_oid == user.azure_oid))
    assert fetched is not None
    assert fetched.id == user.id


async def test_duplicate_azure_oid_rejected(db_session) -> None:
    oid = str(uuid.uuid4())
    db_session.add(_make_user(azure_oid=oid))
    await db_session.flush()
    # Savepoint so the violation rolls back only the nested block and leaves the
    # fixture's outer transaction healthy for teardown.
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(_make_user(azure_oid=oid))
            await db_session.flush()


async def test_refresh_token_roundtrip_defaults(db_session) -> None:
    user = _make_user()
    db_session.add(user)
    await db_session.flush()

    token = _make_token(user.id)
    db_session.add(token)
    await db_session.flush()
    await db_session.refresh(token)  # load server-generated revoked

    assert token.id.version == 7
    assert token.used_at is None  # nullable, no default
    assert token.revoked is False  # server_default false


async def test_duplicate_token_hash_rejected(db_session) -> None:
    user = _make_user()
    db_session.add(user)
    await db_session.flush()

    shared_hash = uuid.uuid4().hex + uuid.uuid4().hex
    db_session.add(_make_token(user.id, token_hash=shared_hash))
    await db_session.flush()
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(_make_token(user.id, token_hash=shared_hash))
            await db_session.flush()


async def test_delete_user_cascades_tokens(db_session) -> None:
    # ON DELETE CASCADE (OwnedByUserMixin FK) is enforced by PostgreSQL, not the
    # ORM — deleting the owner takes its refresh tokens with it.
    user = _make_user()
    db_session.add(user)
    await db_session.flush()
    token = _make_token(user.id)
    db_session.add(token)
    await db_session.flush()
    token_id = token.id

    await db_session.delete(user)
    await db_session.flush()

    surviving = await db_session.scalar(select(RefreshToken).where(RefreshToken.id == token_id))
    assert surviving is None
