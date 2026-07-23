"""The atomic Build-it transition (U12 / R6 / R7): one endpoint records the choice, flips
the conversation to Write, and starts the build — or records a typed failure that
RE-ARMS the card. Resolved-with-no-build is impossible by ORDERING: the "build"
resolution is written only after `SessionManager.start` returned a live session.

Business outcomes travel as a typed 200 union (`started` / `already_built` /
`stale_plan` / `build_failed`) — they are decisions, not transport errors; 4xx stays for
ownership and card-identity problems. Failures are recorded as SYSTEM overlays (never a
ToolReturnPart), so a later retry's success can still write the call's ONE true return.

The stale-plan check (plan Key Decision): the card carries the snapshot head SHA pinned
at Plan time; if the app moved since (another build landed), Build-it answers
`stale_plan` and the card STAYS pending — the user decides (`force=true` proceeds. The
warn-or-replan choice is theirs, not the server's).

Warm sessions note (U12): the refine loop's warmth is the U2 pardon + U3 reap-through —
a completed build's container stays live under its lease; the next Build-it reaps
through and restores from the snapshot written seconds earlier. Same-container re-attach
(skipping the restore) is a named follow-up, not this endpoint's concern.
"""

from __future__ import annotations

import uuid
from typing import Literal

import structlog
from fastapi import APIRouter, status

from src.api.deps import CurrentUser, DbSession
from src.api.deps_csrf import RequireCsrf
from src.api.v1.build_sessions.deps import OptionalSandbox, RunBuildDep, SessionManagerDep
from src.api.v1.claude.router import StorageDep, _resolve_conversation_or_404
from src.api.v1.conversations.turns import _app_id_for_project
from src.core.errors import AppApiError
from src.db.models.conversation import ConversationMode
from src.schemas import AUTH_401, CamelModel, ErrorEnvelope, error_responses
from src.services.build_sessions import BuildSessionConflictError
from src.services.messages.store import append_mode_switch_marker, load_rows
from src.services.redis import build_coordination_or_503
from src.services.storage import ObjectStorage, StorageNotFoundError, parse_bundle_head_sha
from src.services.storage.keys import snapshot_key
from src.services.turns.plan_options import (
    approved_plan_text,
    newest_card,
    pending_card,
    record_build_failure,
    record_build_started,
    resolution_of,
)
from src.services.usage.gate import DailyTokenLimitExceededError, enforce_daily_limit

logger = structlog.get_logger()

router = APIRouter(prefix="/conversations", tags=["turns"])

_SANDBOX_UNAVAILABLE_MSG = "The build environment is temporarily unavailable."

# The approved-plan framing the build prompt opens with (mirrors U9's Write segment).
_EXECUTE_PLAN_PREFIX = (
    "Execute the approved plan below. Where the code on disk differs from what the plan "
    "assumed, follow the code's reality and tell the user what changed.\n\n"
)


class BuildTransitionBody(CamelModel):
    """`force=true` proceeds past a stale-plan warning (the user chose to build anyway)."""

    force: bool = False


class BuildTransitionResponse(CamelModel):
    """The typed outcome union. `started` carries the session to attach the build UI to;
    `stale_plan` carries both SHAs so the client can say what moved; `build_failed`
    carries the reason (the card re-arms) and, for `lock_held`, the conflicting session."""

    outcome: Literal["started", "already_built", "stale_plan", "build_failed"]
    session_id: str | None = None
    app_id: str | None = None
    reason: str | None = None
    conflict_session_id: str | None = None
    plan_head_sha: str | None = None
    current_head_sha: str | None = None


async def _current_snapshot_head(
    storage: ObjectStorage | None, app_id: uuid.UUID | None
) -> str | None:
    """The app's snapshot head right now — None when no app/snapshot exists (or storage
    is unconfigured, a dev-only boot where the build itself would fail first anyway)."""
    if storage is None or app_id is None:
        return None
    try:
        data = await storage.get(snapshot_key(app_id))
    except StorageNotFoundError:
        return None
    return parse_bundle_head_sha(data)


@router.post(
    "/{conversation_id}/plan-options/{tool_call_id}/build",
    response_model=BuildTransitionResponse,
    dependencies=[RequireCsrf],
    responses=error_responses(
        (400, ErrorEnvelope, "Unknown card"),
        AUTH_401,
        (403, ErrorEnvelope, "CSRF check failed"),
        (404, ErrorEnvelope, "Conversation not found"),
        (409, ErrorEnvelope, "The card is superseded"),
        (503, ErrorEnvelope, "Build engine, sandbox, or coordination unavailable"),
    ),
)
async def build_it(
    conversation_id: uuid.UUID,
    tool_call_id: str,
    body: BuildTransitionBody,
    user: CurrentUser,
    db: DbSession,
    sandbox: OptionalSandbox,
    run_build: RunBuildDep,
    manager: SessionManagerDep,
    storage: StorageDep,
) -> BuildTransitionResponse:
    conversation = await _resolve_conversation_or_404(db, user.id, conversation_id)
    rows = list(
        await load_rows(db, user_id=user.id, conversation_id=conversation.id, include_hidden=True)
    )

    card = pending_card(rows, tool_call_id)
    if card is None:
        raise AppApiError(400, "No such plan options card.")
    newest = newest_card(rows)
    if newest is None or newest.tool_call_id != tool_call_id:
        raise AppApiError(409, "A newer plan supersedes these options.")

    stored = resolution_of(rows, tool_call_id)
    if stored == "build":
        # A double click / second tab — the build already started; answer idempotently.
        live = manager.active_session_for(user.id)
        return BuildTransitionResponse(
            outcome="already_built",
            session_id=str(live.session_id) if live is not None else None,
        )
    if stored is not None and not stored.startswith("build_failed"):
        raise AppApiError(409, "These options were already resolved.")

    app_id = await _app_id_for_project(db, user.id, conversation.project_id)
    if not body.force:
        current_head = await _current_snapshot_head(storage, app_id)
        if current_head != card.head_sha:
            # The app moved since Plan time (or appeared/vanished) — warn, don't build.
            # The card STAYS pending; the user forces or replans.
            return BuildTransitionResponse(
                outcome="stale_plan",
                plan_head_sha=card.head_sha,
                current_head_sha=current_head,
            )

    try:
        await enforce_daily_limit(db, user.id)
    except DailyTokenLimitExceededError:
        await record_build_failure(
            db,
            user_id=user.id,
            conversation_id=conversation.id,
            tool_call_id=tool_call_id,
            reason="daily_cap",
        )
        return BuildTransitionResponse(outcome="build_failed", reason="daily_cap")

    if run_build is None:
        raise AppApiError(status.HTTP_503_SERVICE_UNAVAILABLE, "Build engine not configured.")
    if sandbox is None:
        raise AppApiError(status.HTTP_503_SERVICE_UNAVAILABLE, _SANDBOX_UNAVAILABLE_MSG)

    plan_text = approved_plan_text(rows, card)
    prompt = (
        _EXECUTE_PLAN_PREFIX + plan_text
        if plan_text
        else ("Build what the user planned in this conversation.")
    )

    with build_coordination_or_503():
        try:
            session = await manager.start(
                db,
                user,
                conversation.project_id,
                prompt,
                conversation_id=conversation.id,
                run_build=run_build,
                sandbox_client=sandbox,
            )
        except BuildSessionConflictError as exc:
            await record_build_failure(
                db,
                user_id=user.id,
                conversation_id=conversation.id,
                tool_call_id=tool_call_id,
                reason="lock_held",
            )
            return BuildTransitionResponse(
                outcome="build_failed",
                reason="lock_held",
                conflict_session_id=str(exc.session_id) if exc.session_id else None,
            )
        except Exception:
            logger.exception("build_transition_start_failed", conversation_id=str(conversation.id))
            await record_build_failure(
                db,
                user_id=user.id,
                conversation_id=conversation.id,
                tool_call_id=tool_call_id,
                reason="provision_failed",
            )
            return BuildTransitionResponse(outcome="build_failed", reason="provision_failed")

    # The build is live — now the record: resolution, mode flip, and the durable marker
    # (this exact order; a crash between start and here leaves a running build with a
    # pending card, which reload renders truthfully — never resolved-with-no-build).
    #
    # Re-check the card's resolution first: `manager.start` can take seconds, and a concurrent
    # turn-start on this conversation (free text → resolve_pending_as_refine) may have written
    # a real `refine` ToolReturnPart for this same card in that window. If so, record the build
    # as a system overlay instead of a second ToolReturnPart — two returns for one call id is
    # exactly what would wedge the conversation on the next load.
    fresh_rows = list(
        await load_rows(db, user_id=user.id, conversation_id=conversation.id, include_hidden=True)
    )
    prior = resolution_of(fresh_rows, tool_call_id)
    answered_already = prior is not None and not prior.startswith("build_failed")
    await record_build_started(
        db,
        user_id=user.id,
        conversation_id=conversation.id,
        pending=card,
        answered_already=answered_already,
    )
    old_mode = conversation.mode
    conversation.mode = ConversationMode.WRITE
    db.add(conversation)
    if old_mode != ConversationMode.WRITE:
        await append_mode_switch_marker(
            db,
            user_id=user.id,
            conversation_id=conversation.id,
            old_mode=old_mode,
            new_mode=ConversationMode.WRITE,
        )
    await db.commit()
    return BuildTransitionResponse(
        outcome="started",
        session_id=str(session.session_id),
        app_id=str(session.app_id),
    )
