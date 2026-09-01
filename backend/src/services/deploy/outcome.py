"""Write a deploy outcome into the citizen's chat.

A build failure the citizen cannot see is a build failure they cannot ask the agent to fix,
so the outcome goes where they are already looking rather than only into an API response.

`meta["kind"]` is deliberately `deploy_outcome`, NOT `build_outcome`. The projection gates
its banner card on `build_outcome` plus a session id; an unknown visible kind falls through
to the arm that renders it as plain assistant prose — an outcome that module documents as
intended ("a future lifecycle entry should degrade to prose, not vanish"). That means this
needs no projection change and no frontend work, at the cost of a plain message instead of
a card. Worth revisiting when the portal grows a Deploy surface; not worth blocking on now.

Owner-scoped: the conversation must belong to the caller, or this is a no-op rather than a
cross-user write. Idempotent on the deployment id — a reconciler that promotes a row the
pipeline also settled must not append a second message.
"""

from __future__ import annotations

import uuid
from typing import Any

import sqlalchemy as sa
import structlog
from pydantic_ai.messages import ModelResponse, TextPart
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models.conversation import ChatKind, Conversation
from src.db.models.message import Message, MessageEntryKind, MessageVisibility
from src.services.messages.store import append_batch

_log = structlog.get_logger()

DEPLOY_OUTCOME_KIND = "deploy_outcome"


async def write_deploy_outcome(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
    deployment_id: uuid.UUID,
    app_id: uuid.UUID,
    succeeded: bool,
    message: str,
    url: str | None = None,
    detail: str | None = None,
) -> bool:
    """Append the outcome as a visible `system_event` row. True if written."""
    owned = await db.scalar(
        sa.select(Conversation.id).where(
            Conversation.id == conversation_id, Conversation.user_id == user_id
        )
    )
    if owned is None:
        # The conversation was deleted, or is not this user's. Not an error — the
        # deployment row is the record of truth and it has already been written.
        return False

    if await _already_recorded(db, conversation_id=conversation_id, deployment_id=deployment_id):
        return False

    meta: dict[str, Any] = {
        "kind": DEPLOY_OUTCOME_KIND,
        "deploymentId": str(deployment_id),
        "appId": str(app_id),
        "status": "succeeded" if succeeded else "failed",
    }
    if url is not None:
        meta["url"] = url
    if detail is not None:
        # Already redacted and capped by the caller; `append_batch` redacts meta again.
        meta["detail"] = detail

    try:
        await append_batch(
            db,
            user_id=user_id,
            conversation_id=conversation_id,
            messages=[ModelResponse(parts=[TextPart(content=message)])],
            entry_kind=MessageEntryKind.SYSTEM_EVENT,
            kind=ChatKind.BUILD,
            visibility=MessageVisibility.VISIBLE,
            meta=meta,
        )
    except Exception:
        # Best-effort by design: the deployment row is the record, and a chat write that
        # fails must not undo a deploy that worked.
        _log.warning(
            "deploy_outcome_write_failed", deployment_id=str(deployment_id), exc_info=True
        )
        return False
    return True


async def _already_recorded(
    db: AsyncSession, *, conversation_id: uuid.UUID, deployment_id: uuid.UUID
) -> bool:
    """Keyed on BOTH the kind and the deployment id — the kind predicate is load-bearing,
    exactly as it is for build outcomes: without it, any other system row carrying the same
    id would suppress this one."""
    found = await db.scalar(
        sa.select(Message.id).where(
            Message.conversation_id == conversation_id,
            Message.entry_kind == MessageEntryKind.SYSTEM_EVENT,
            Message.meta["kind"].astext == DEPLOY_OUTCOME_KIND,
            Message.meta["deploymentId"].astext == str(deployment_id),
        )
    )
    return found is not None
