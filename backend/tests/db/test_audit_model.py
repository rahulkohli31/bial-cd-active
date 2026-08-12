"""AuditLog round-trips + the ON DELETE SET NULL actor link against the REAL
migrated schema (alembic upgrade head), inside the rolled-back per-test transaction.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy import select

from src.db.models.audit import AuditLog
from src.db.models.user import User


def _make_user(**overrides: Any) -> User:
    data: dict[str, Any] = {"azure_oid": str(uuid.uuid4()), "email": "citizen@rvaiglobal.com"}
    data.update(overrides)
    return User(**data)


async def test_audit_roundtrip_defaults(db_session) -> None:
    user = _make_user()
    db_session.add(user)
    await db_session.flush()

    entry = AuditLog(
        actor_id=user.id,
        action="delete",
        resource_type="conversation",
        resource_id=str(uuid.uuid7()),
        detail={"count": 3},
    )
    db_session.add(entry)
    await db_session.flush()
    await db_session.refresh(entry)

    assert entry.id.version == 7  # UUIDv7 PK (app-side default)
    assert entry.created_at is not None
    assert entry.detail == {"count": 3}


async def test_actor_id_nullable(db_session) -> None:
    # A system/anonymous actor (Express stores username-or-null).
    entry = AuditLog(actor_id=None, action="clear-data", resource_type="app", resource_id="app-1")
    db_session.add(entry)
    await db_session.flush()
    await db_session.refresh(entry)
    assert entry.actor_id is None


async def test_delete_actor_sets_null_not_cascade(db_session) -> None:
    # ON DELETE SET NULL (NOT CASCADE): deleting the actor PRESERVES the audit row
    # and unlinks the actor — the accountability trail outlives the user. Enforced
    # by PostgreSQL, so we re-read the DB value after the delete.
    user = _make_user()
    db_session.add(user)
    await db_session.flush()
    entry = AuditLog(actor_id=user.id, action="disable", resource_type="app", resource_id="app-9")
    db_session.add(entry)
    await db_session.flush()
    entry_id = entry.id

    await db_session.delete(user)
    await db_session.flush()
    # The session doesn't track the DB-side SET NULL — refresh to read it back.
    await db_session.refresh(entry)

    assert entry.actor_id is None
    surviving = await db_session.scalar(select(AuditLog).where(AuditLog.id == entry_id))
    assert surviving is not None
