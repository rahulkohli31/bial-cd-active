"""Append-only audit helper (R9).

`append_audit` writes ONE accountability row WITHIN the caller's transaction — it
flushes but never commits, so the audit row shares the fate of the gated action:
if the action's transaction rolls back, the audit row rolls back with it (no orphan
trail for an action that never actually happened). Called wherever a
permission-gated / state-changing action succeeds; the caller commits both together.
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models.audit import AuditLog


async def append_audit(
    db: AsyncSession,
    *,
    actor_id: uuid.UUID | None,
    action: str,
    resource_type: str,
    resource_id: str | None = None,
    detail: dict[str, Any] | None = None,
) -> AuditLog:
    """Record one accountability event in the CALLER'S transaction. The flush (not
    commit) assigns the id and surfaces a bad FK immediately while keeping the write
    atomic with the gated action — the caller owns the commit."""
    entry = AuditLog(
        actor_id=actor_id,
        action=action,
        resource_type=resource_type,
        resource_id=resource_id,
        detail=detail,
    )
    db.add(entry)
    await db.flush()
    return entry
