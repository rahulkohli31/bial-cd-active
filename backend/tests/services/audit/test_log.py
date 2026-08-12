"""append_audit writes one row in the CALLER'S transaction (flush, not commit),
so a rolled-back action leaves no orphan audit row."""

from __future__ import annotations

import pytest
from sqlalchemy import func, select

from src.db.models.audit import AuditLog
from src.services.audit import append_audit
from tests.factories import UserFactory


async def _count(db_session) -> int:
    return await db_session.scalar(select(func.count()).select_from(AuditLog)) or 0


async def test_append_writes_one_row(db_session) -> None:
    user = await UserFactory.create(db_session)
    entry = await append_audit(
        db_session,
        actor_id=user.id,
        action="update",
        resource_type="feedback",
        resource_id="fb-1",
    )
    assert entry.id is not None
    assert entry.actor_id == user.id
    assert entry.action == "update"
    assert entry.resource_type == "feedback"
    assert entry.resource_id == "fb-1"
    assert await _count(db_session) == 1


async def test_detail_jsonb_roundtrips(db_session) -> None:
    detail = {"count": 42, "fields": ["title", "kind"], "nested": {"k": "v"}}
    entry = await append_audit(
        db_session,
        actor_id=None,
        action="clear-data",
        resource_type="app",
        resource_id="app-7",
        detail=detail,
    )
    await db_session.refresh(entry)
    assert entry.detail == detail


async def test_rolls_back_with_the_action(db_session) -> None:
    # The audit write shares the caller's transaction: if the gated action fails and
    # the surrounding work is rolled back, the audit row rolls back too (no orphan).
    user = await UserFactory.create(db_session)

    class _ActionFailedError(Exception):
        pass

    with pytest.raises(_ActionFailedError):
        async with db_session.begin_nested():  # savepoint standing in for the action
            await append_audit(
                db_session, actor_id=user.id, action="delete", resource_type="conversation"
            )
            assert await _count(db_session) == 1  # visible inside the savepoint
            raise _ActionFailedError  # the gated action fails after the audit write

    # Savepoint rolled back → the audit row is gone; the pre-savepoint user remains.
    assert await _count(db_session) == 0
