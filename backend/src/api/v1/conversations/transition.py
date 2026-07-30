"""The atomic Build-it transition (U12 / R6 / R7): one endpoint flips the conversation to
Write, starts the build, and records the choice — or restores the mode and records a typed
failure that RE-ARMS the card. Resolved-with-no-build is impossible by ORDERING: the "build"
resolution is written only after `SessionManager.start` returned a live session. The mode
flip, by contrast, is committed BEFORE the start, because the build's own end sequence is
what hands the mode back and it can run before the start call returns (see the flip below).

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
from fastapi.responses import JSONResponse

from src.api.deps import CurrentUser, DbSession
from src.api.deps_csrf import RequireCsrf
from src.api.v1.build_sessions.deps import OptionalSandbox, SessionManagerDep
from src.api.v1.conversations._shared import (
    BUILD_IN_FLIGHT_MSG,
    ModelDep,
    SessionFactoryDep,
    StorageDep,
    history_rehydrator,
    resolve_conversation_or_404,
)
from src.api.v1.conversations.turns import _app_id_for_project, start_conversation_turn
from src.core.errors import AppApiError
from src.db.models.conversation import ConversationMode
from src.db.models.message import MessageVisibility
from src.db.models.project import Project
from src.schemas import AUTH_401, CamelModel, ErrorEnvelope, error_responses
from src.services.agent.mode_prompts import PromptContext
from src.services.messages.store import (
    AttachmentRehydrationError,
    append_mode_switch_marker,
    load_history,
    load_rows,
)
from src.services.storage import ObjectStorage, StorageNotFoundError, parse_bundle_head_sha
from src.services.storage.keys import snapshot_key
from src.services.turns.engine import get_turn_engine
from src.services.turns.plan_options import (
    approved_plan_text,
    newest_card,
    pending_card,
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
    """The typed outcome union. `started` carries the TURN to subscribe to — a build is a
    turn now, and the turn id is the only identity the client needs. `stale_plan` carries
    both SHAs so the client can say what moved.

    `build_failed` is gone, and so is every reason it carried. Each of those cases is now a
    typed HTTP status the client's fetch layer already understands: 429 for the daily cap,
    409 for a busy workspace, 503 for an unconfigured engine. Collapsing them into a 200
    meant the browser had to re-implement error handling it already had, and a genuine bug
    arrived looking exactly like a quota refusal."""

    outcome: Literal["started", "already_started", "stale_plan"]
    turn_id: str | None = None
    app_id: str | None = None
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
    manager: SessionManagerDep,
    storage: StorageDep,
    model: ModelDep,
    factory: SessionFactoryDep,
) -> BuildTransitionResponse | JSONResponse:
    conversation = await resolve_conversation_or_404(db, user.id, conversation_id)
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
        # A double click / second tab — the build already started; answer idempotently with
        # whatever turn is live on this thread, so the second tab attaches to the same run
        # instead of starting a rival one.
        live_turn = get_turn_engine().peek(conversation.id)
        return BuildTransitionResponse(
            outcome="already_started",
            turn_id=str(live_turn.turn_id) if live_turn is not None else None,
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

    # 429 with its byte-stable body, and the card STAYS PENDING — nothing has been written
    # yet, so there is no half-started build to compensate for and the user can simply click
    # Build again tomorrow.
    try:
        await enforce_daily_limit(db, user.id)
    except DailyTokenLimitExceededError as exc:
        return exc.as_response()

    if model is None:
        raise AppApiError(status.HTTP_503_SERVICE_UNAVAILABLE, "Claude client not configured.")
    if sandbox is None:
        raise AppApiError(status.HTTP_503_SERVICE_UNAVAILABLE, _SANDBOX_UNAVAILABLE_MSG)
    # The cheap conflict check, same question `POST /turns` asks: one sandbox per user, and
    # it is not this thread's to take if another conversation is mid-build.
    active = manager.active_session_for(user.id)
    if active is not None and active.conversation_id != conversation.id:
        raise AppApiError(409, BUILD_IN_FLIGHT_MSG)

    plan_text = approved_plan_text(rows, card)
    prompt = (
        _EXECUTE_PLAN_PREFIX + plan_text
        if plan_text
        else ("Build what the user planned in this conversation.")
    )

    # THE FLIP, committed before the turn starts. Write is where the thread STAYS now — there
    # is no end-sequence restore to race, because the mode is no longer a dead end someone has
    # to rescue the user out of. That was the whole point of the convergence: a citizen who
    # built something can keep talking to it in the same mode.
    entry_mode = conversation.mode
    conversation.mode = ConversationMode.WRITE
    db.add(conversation)
    if entry_mode is not ConversationMode.WRITE:
        # The marker is how the MODEL learns where its toolset changed; it stays.
        await append_mode_switch_marker(
            db,
            user_id=user.id,
            conversation_id=conversation.id,
            old_mode=entry_mode,
            new_mode=ConversationMode.WRITE,
        )
    await db.commit()

    # The card resolution, BEFORE the turn: everything above is side-effect-free, so there is
    # no arm left that can burn a card without starting anything. Re-read the rows first —
    # a concurrent free-text send may have resolved this same card as `refine` in the window,
    # and two ToolReturnParts for one call id would wedge the thread on its next load.
    fresh_rows = list(
        await load_rows(db, user_id=user.id, conversation_id=conversation.id, include_hidden=True)
    )
    prior = resolution_of(fresh_rows, tool_call_id)
    await record_build_started(
        db,
        user_id=user.id,
        conversation_id=conversation.id,
        pending=card,
        answered_already=prior is not None and not prior.startswith("build_failed"),
    )

    rehydrate = history_rehydrator(db, storage, user.id)
    try:
        history = await load_history(
            db, user_id=user.id, conversation_id=conversation.id, rehydrate=rehydrate
        )
    except AttachmentRehydrationError as exc:
        raise AppApiError(400, str(exc)) from None

    project = await db.get(Project, conversation.project_id)
    if project is None:  # FK guarantees this; fail loudly if it ever breaks
        raise AppApiError(404, "Conversation not found.")

    # NOT wrapped in `build_coordination_or_503`. That helper SKIPS its block when Redis is
    # unconfigured — correct for a coordination CHECK ("nothing can hold a lock, proceed"),
    # catastrophic for the start itself, which would leave `turn_id` unbound and 500. The
    # sandbox attach happens inside the detached turn now and reports its own failure as a
    # `workspace unavailable` frame, so there is no coordination read left on this path.
    turn_id = await start_conversation_turn(
        db=db,
        user=user,
        conversation=conversation,
        prompt=prompt,
        history=history,
        prompt_context=PromptContext(
            user_name=user.display_name or user.email,
            project_name=project.name,
            project_description=project.description or None,
        ),
        app_id=app_id,
        model=model,
        factory=factory,
        manager=manager,
        sandbox=sandbox,
        # THE SEED IS HIDDEN. It is the platform telling the model to execute the plan,
        # not something the citizen typed, and rendering it as a user bubble is exactly
        # the "I never said that" moment the transcript must never produce. Hidden gives
        # the model its instruction and the reader their honest history in one row.
        visibility=MessageVisibility.HIDDEN,
        meta={"kind": "write_seed"},
    )

    return BuildTransitionResponse(
        outcome="started",
        turn_id=str(turn_id),
        app_id=str(app_id) if app_id else None,
    )
