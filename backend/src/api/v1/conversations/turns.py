"""The resumable turn transport: POST starts a detached turn, GET subscribes.

`POST /conversations/{id}/turns` → 202 `{turnId}`; the run detaches once the response
leaves (disconnect ≠ cancel — the stop endpoint is the ONLY cancel). `GET
/conversations/{id}/events` is a pure OBSERVER of the engine's frame ring: snapshot then
live tail for a subscriber that can't prove gap-free continuity, plain replay otherwise
(`?turn=&cursor=`); every subscriber sees the identical stream — fan-out is the engine's.

Wire discipline: commit the response and emit the snapshot before any model byte, `: ping`
between frames only, errors in-band, `turn_ended` followed by `data: [DONE]`. Turn
plumbing shared with plan→build lives in `_shared.py` — one source, no copies.
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator, Sequence
from typing import Literal

import sqlalchemy as sa
import structlog
from fastapi import APIRouter, Query
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic_ai import BinaryContent
from pydantic_ai.messages import ModelMessage, ModelRequest, UserPromptPart
from pydantic_ai.models import Model
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.api.deps import CurrentUser, DbSession
from src.api.deps_csrf import RequireCsrf
from src.api.v1.build_sessions.deps import OptionalSandbox, SessionManagerDep
from src.api.v1.conversations._shared import (
    BUILD_IN_FLIGHT_MSG,
    ModelDep,
    SessionFactoryDep,
    StorageDep,
    TurnMessage,
    history_rehydrator,
    prompt_content,
    resolve_binaries,
    resolve_conversation_or_404,
)
from src.api.v1.conversations.schemas import (
    TurnStartResponse,
    TurnStopResponse,
    TurnStreamFrame,
)
from src.api.v1.live_build import ReclaimBlockedEnvelope, reclaim_blocked_response
from src.core.errors import AppApiError
from src.db.models.app_registry import AppRegistry, AppStatus
from src.db.models.conversation import Conversation
from src.db.models.message import MessageEntryKind, MessageVisibility
from src.db.models.project import Project
from src.db.models.user import User
from src.schemas import AUTH_401, CamelModel, DailyTokenLimitBody, ErrorEnvelope, error_responses
from src.services.agent.mode_prompts import PromptContext
from src.services.attachments.materialize import (
    AttachmentDelivery,
    code_lane_attachments,
)
from src.services.build_sessions import SandboxReclaimBlockedError
from src.services.build_sessions.appdata import APP_SWITCHED_OFF, APP_SWITCHED_OFF_CODE
from src.services.build_sessions.manager import SessionManager
from src.services.messages.projection import DisplayItem, project_conversation
from src.services.messages.store import (
    AttachmentRehydrationError,
    SeqContentionError,
    append_batch,
    load_history,
    load_rows,
)
from src.services.redis import build_coordination_or_503
from src.services.sandbox import SandboxClient
from src.services.turns.copy import (
    ALREADY_BUILDING_HERE_CODE,
    CHAT_TOO_LONG_CODE,
    CHAT_TOO_LONG_TEXT,
    WORKSPACE_UNAVAILABLE_CODE,
    WORKSPACE_UNAVAILABLE_TEXT,
)
from src.services.turns.engine import (
    TurnNotRunningError,
    get_turn_engine,
)
from src.services.turns.guard import ConversationBusyError, conversation_is_mid_reply
from src.services.turns.plan_options import (
    NoPendingOptionsError,
    PlanOptionsExpiredError,
    resolve_pending_as_refine,
)
from src.services.turns.plan_options import (
    resolve as resolve_plan_options,
)
from src.services.usage.context_window import (
    ContextWindowExceededError,
    enforce_context_limit,
)
from src.services.usage.gate import DailyTokenLimitExceededError, enforce_daily_limit

logger = structlog.get_logger()

router = APIRouter(prefix="/conversations", tags=["turns"])

_SSE_HEADERS = {"Cache-Control": "no-cache", "Connection": "keep-alive"}
_DONE = b"data: [DONE]\n\n"

# Keepalive cadence between complete frames — MUST stay well under the portal reader's
# stall window (`turnStreamApi.ts` TURN_STREAM_STALL_TIMEOUT_MS = 60s; 4x margin).
# Both sides pin this inequality by test.
KEEPALIVE_SECONDS = 15.0


class StartTurnBody(CamelModel):
    """`POST /conversations/{id}/turns` — the new message; the conversation id rides the path.

    ★ NO `create` BLOCK RIDES THIS CALL. One used to, carrying a not-yet-written chat's
    parentage so the row could be created inside this request, after every side-effect-free
    refusal — which kept a refused first message from leaving an orphaned, titled, empty chat
    behind. That guarantee is deliberately traded away: attachments are uploaded AGAINST a
    conversation now, so the row has to exist a round trip earlier, and `POST /v1/conversations`
    creates it. The residue is real and recorded — a refused first send leaves an empty chat —
    and sweeping it is a separate, already-tracked task.

    An unknown conversation id is therefore a client bug on every turn, first or hundredth, and
    answers the same non-leaking 404 a stranger's id does."""

    message: TurnMessage


def _frame_bytes(frame: TurnStreamFrame) -> bytes:
    return (
        b"id: "
        + str(frame.seq).encode()
        + b"\ndata: "
        + frame.model_dump_json(by_alias=True).encode()
        + b"\n\n"
    )


async def _conversation_or_none(
    db: AsyncSession, user_id: uuid.UUID, conversation_id: uuid.UUID
) -> Conversation | None:
    """The owner-scoped conversation row, or `None` when there is not one yet.

    Deliberately NOT `resolve_conversation_or_404`: on a first message the absence is the ordinary
    case, and raising there would make the 404 arrive before the parentage that can answer it has
    been looked at."""
    row: Conversation | None = await db.scalar(
        sa.select(Conversation).where(
            Conversation.id == conversation_id, Conversation.user_id == user_id
        )
    )
    return row


async def _app_id_for_project(
    db: AsyncSession, user_id: uuid.UUID, project_id: uuid.UUID
) -> uuid.UUID | None:
    """The project's app, WITHOUT minting one (`resolve_app_for_project` upserts — a read
    path must never create). None = nothing was ever built."""
    app_id: uuid.UUID | None = await db.scalar(
        sa.select(AppRegistry.id).where(
            AppRegistry.project_id == project_id, AppRegistry.user_id == user_id
        )
    )
    return app_id


async def _app_is_switched_off(
    db: AsyncSession, user_id: uuid.UUID, project_id: uuid.UUID
) -> bool:
    """Has an administrator switched this project's app off?

    A READ, and one that mints nothing — the same discipline as `_app_id_for_project` above
    and for the same reason. A project with no app row yet answers False: there is nothing to
    have been switched off, and the first turn is allowed to mint one."""
    switched_off: bool = (
        await db.scalar(
            sa.select(AppRegistry.status == AppStatus.DISABLED).where(
                AppRegistry.project_id == project_id, AppRegistry.user_id == user_id
            )
        )
        or False
    )
    return switched_off


async def start_conversation_turn(
    *,
    db: AsyncSession,
    user: User,
    conversation: Conversation,
    prompt: str | list[str | BinaryContent],
    history: list[ModelMessage],
    prompt_context: PromptContext,
    app_id: uuid.UUID | None,
    model: Model,
    factory: async_sessionmaker[AsyncSession],
    manager: SessionManager,
    sandbox: SandboxClient | None,
    visibility: MessageVisibility = MessageVisibility.VISIBLE,
    meta: dict[str, object] | None = None,
    expects_mutation: bool = False,
    attachments: AttachmentDelivery | None = None,
    file_attachment_ids: Sequence[str],
) -> uuid.UUID:
    """Persist the user turn and start the run — ONE expression, two readers.

    `POST /turns` and `Build it` differ only in prompt origin, visibility, and whether a file
    change is OWED; the rest (pre-run write, engine claim, conflict mappings) is identical, so
    one copy stops two guards drifting apart. `visibility=HIDDEN` puts Build-it's machine seed
    in model history without the citizen seeing it (`load_history` ignores it, the projection
    skips it). `expects_mutation` travels to the engine: no file change makes a Build-it turn a
    FAILED build but a Write turn just an answered question — only the caller knows which.

    `attachments` is the conversation's code-lane files. Only `POST /turns` passes
    one; Build-it's `None` is a fact rather than a gap, because that route CREATES the Build
    chat it starts — there is no conversation yet for a file to have been attached to."""

    # THE STORED ROW RECORDS THE CODE LANE; THE PROMPT DOES NOT. A code-lane file's bytes
    # must never enter the prompt — that is the whole lane — but the message still has to RECORD
    # that the file was sent, because three separate things decide what is still referenced by
    # scanning stored payloads: the never-sent reclaimer, the conversation cascade, and the
    # projection that rebuilds chips on reload. With nothing in the payload all three were blind,
    # and the reclaimer deleted live spreadsheets as orphans 48 hours after upload.
    #
    # ★ THE MARKER NAMES THE MESSAGE THAT CARRIED THE FILE, AND THE CALLER DECIDES WHICH THOSE ARE.
    # It used to be taken from `attachments.files`, which is the CONVERSATION's code-lane set —
    # deliberately so, because a recycled container is re-filled every turn — so every later
    # message in a chat was stamped with every file the chat had ever held. Two files across four
    # turns drew eight chips. The delivery stays conversation-scoped; only the durable marker
    # narrows, and it narrows here rather than there.
    #
    # NO DEFAULT, so a new caller has to answer the question. `()` is a perfectly good answer —
    # `transition.py` gives it — but it has to be given.
    #
    # The ids go to the STORE rather than into `prompt`, because a marker is a payload concept:
    # `UserPromptPart.content` has no room for one (an unknown dict coerces to `CachePoint`), which
    # is the same reason `_externalize_binaries` runs on the serialized tree. `load_history` drops
    # them again, so the model never meets one.
    file_refs = list(dict.fromkeys(file_attachment_ids))

    async def persist_user_turn() -> None:
        await append_batch(
            db,
            user_id=user.id,
            conversation_id=conversation.id,
            messages=[ModelRequest(parts=[UserPromptPart(content=prompt)])],
            entry_kind=MessageEntryKind.TURN,
            kind=conversation.kind,
            visibility=visibility,
            meta=meta,
            file_attachment_ids=file_refs,
        )

    engine = get_turn_engine()
    try:
        return await engine.start_turn(
            conversation=conversation,
            user_id=user.id,
            prompt=prompt,
            history=history,
            prompt_context=prompt_context,
            app_id=app_id,
            project_id=conversation.project_id,
            model=model,
            session_factory=factory,
            persist_user_turn=persist_user_turn,
            manager=manager,
            sandbox_client=sandbox,
            expects_mutation=expects_mutation,
            attachments=attachments,
        )
    except ConversationBusyError:
        raise AppApiError(409, "A turn is already running for this conversation.") from None
    except SeqContentionError:
        raise AppApiError(
            409, "Another message is being recorded for this conversation. Try again."
        ) from None


@router.post(
    "/{conversation_id}/turns",
    status_code=202,
    response_model=TurnStartResponse,
    dependencies=[RequireCsrf],
    responses=error_responses(
        (400, ErrorEnvelope, "Invalid message"),
        AUTH_401,
        (403, ErrorEnvelope, "CSRF check failed"),
        (404, ErrorEnvelope, "Conversation not found"),
        (
            409,
            ReclaimBlockedEnvelope,
            "The agent is already working here, or another project holds the workspace",
        ),
        # ONE REFUSAL ON THIS STATUS. A per-message document cap used to share it and is gone;
        # a message carrying too many files is refused by the request validator instead.
        (
            413,
            ErrorEnvelope,
            "This conversation has grown past its per-conversation limit",
        ),
        (429, DailyTokenLimitBody, "Daily token limit exceeded"),
        (503, ErrorEnvelope, "Claude client not configured"),
    ),
)
async def start_turn(
    conversation_id: uuid.UUID,
    body: StartTurnBody,
    user: CurrentUser,
    db: DbSession,
    model: ModelDep,
    factory: SessionFactoryDep,
    storage: StorageDep,
    manager: SessionManagerDep,
    sandbox: OptionalSandbox,
) -> TurnStartResponse | JSONResponse:
    # ★ THIS ROUTE CREATES NO CONVERSATION: the row exists a round trip before this call.
    #
    # It used to. A `create` block rode the first message so the row could be staged here and
    # flushed below, after every side-effect-free refusal — which meant a refused first message
    # left nothing behind at all, and the project's chat list was afterwards exactly as long as
    # it was before (R-18). That was worth having, and it is being traded knowingly.
    #
    # WHAT BOUGHT IT OUT: a file is uploaded AGAINST a conversation now, so the row has to exist
    # before the composer's first upload — a round trip earlier than this one. Two orderings
    # cannot both be true, and the one that makes an upload's owner knowable at the door is the
    # one that removes a whole class of ownerless file.
    #
    # THE RESIDUE, STATED RATHER THAN DISCOVERED: a first message refused by the workspace gate,
    # the daily cap, the sandbox slot or the context wall now leaves a real, empty conversation
    # row. It carries no title (`POST /v1/conversations` is called before the citizen's text is
    # known, and stamping refused text into a row nobody can delete would be worse), and sweeping
    # it is a separate, already-tracked task rather than this route's.
    conversation = await _conversation_or_none(db, user.id, conversation_id)
    if conversation is None:
        # An unknown id is a client bug on every turn now, first or hundredth — and a cross-user
        # id is indistinguishable from it, which is one non-leaking 404 (ADR-0004).
        raise AppApiError(404, "Conversation not found.")
    project_id = conversation.project_id

    # Daily-token gate BEFORE anything persists — a capped user's message is refused
    # whole, never half-recorded. The error carries its own byte-stable body (limit/used/
    # remaining, what the SPA's interceptor reads), so it is RETURNED, not flattened into
    # the plain envelope. The context refusal below chooses the opposite and says why.
    try:
        await enforce_daily_limit(db, user.id)
    except DailyTokenLimitExceededError as exc:
        return exc.as_response()
    if model is None:
        raise AppApiError(503, "Claude client not configured.")
    # NO WORKSPACE SERVICE, SAID HERE RATHER THAN DEGRADED SILENTLY. Both kinds read the
    # project's live app and only that, so a deployment with no sandbox service has nothing for
    # either of them to read. The same shape as the refusal above it, with a machine-readable
    # code so the browser can tell it from the workspace CONFLICTS that share its status family
    # — different cause, different remedy, and a client reading only the status cannot tell.
    #
    # AT THE MOMENT OF SENDING, and before anything is claimed or written: the message is not
    # consumed, no turn exists, and there is no half-started reply to explain afterwards.
    if sandbox is None:
        raise AppApiError(503, WORKSPACE_UNAVAILABLE_TEXT, code=WORKSPACE_UNAVAILABLE_CODE)

    # A MESSAGE, NOT A GATE. The refusal itself lives in one place —
    # `resolve_app_for_project`, which every door into a container comes through — and it
    # holds whether or not this line exists. What this buys is WORDS: that refusal is raised
    # inside the detached turn, where the engine's attach arm catches it as an unexpected
    # failure and says "the workspace service is not available", which is both wrong and
    # retryable-sounding for an app an administrator deliberately switched off. Said here,
    # at the moment of sending and above the first write, the citizen gets the true sentence
    # and no turn is spent. Same string, same code, one source (`appdata`).
    if await _app_is_switched_off(db, user.id, project_id):
        raise AppApiError(409, APP_SWITCHED_OFF, code=APP_SWITCHED_OFF_CODE)

    # Every side-effect-free rejection lands BEFORE `resolve_pending_as_refine`, which is a
    # WRITE: a refused start must never burn the user's pending plan-options card. Both
    # checks are re-made downstream (the engine owns the real, race-free claim) — these are
    # the early, cheap copies that keep the write from happening at all.
    #
    # THE ONE GATE, server side: while the agent is working in this thread — a reply in flight
    # OR a build running — no new turn starts. The build arm is a LIVENESS check, not the mode
    # check below it: the two genuinely disagree (a build's first seconds run before the flip;
    # `POST /build-sessions` never touches the mode at all), and only liveness answers "is the
    # agent building THIS thread right now".
    if manager.live_session_for_conversation(conversation_id) is not None:
        raise AppApiError(409, BUILD_IN_FLIGHT_MSG)
    if conversation_is_mid_reply(conversation_id):
        raise AppApiError(409, "A turn is already running for this conversation.")
    # UNCONDITIONAL, and BELOW the mid-reply guard on purpose: a send during a streaming
    # reply must still 409 as a busy conversation, not as a taken workspace. This
    # one asks a different question — is this user's single workspace already committed to a
    # DIFFERENT conversation of their own? Cheap and synchronous; the expensive provision
    # happens inside the detached turn, because blocking the POST on 30-60s recreates the dead
    # end the composer contract exists to remove.
    #
    # IT NO LONGER READS THE CHAT'S KIND: every turn takes the whole workspace
    # for as long as it runs, whatever kind of chat it was sent in. A Plan turn pins the live
    # container exactly as a Build turn does, by design, so a Plan send that
    # slipped past this gate would take a workspace another of the user's chats was mid-build
    # in, which is the one thing this check exists to prevent.
    active = manager.active_session_for(user.id)
    if active is not None and active.conversation_id != conversation_id:
        raise AppApiError(409, BUILD_IN_FLIGHT_MSG, code=ALREADY_BUILDING_HERE_CODE)

    # BOTH KINDS, not just Build, and the guard above cannot answer this one.
    #
    # Two reasons it sits outside that block. `active_session_for` only sees in-process
    # sessions, so a finished build's pardoned container — warm, holding no session, no lock
    # and no heartbeat — is invisible to it, and that is the state a user is most often in.
    # And `_pin_workspace` attaches the project's LIVE container for a Plan turn as well
    # ("Resolve the turn-pinned read surface ONCE, for BOTH KINDS"), so a Plan turn in
    # another project reclaims the incumbent's workspace exactly as a Build turn does.
    #
    # Gating this on the chat's kind meant a Plan send still destroyed the other project's
    # unsaved work, and did it inside the detached turn where the only thing the user saw was
    # "Your workspace could not be started right now" — no dialog, no named project, no way
    # to save. Asked here so the refusal is an HTTP 409 the client turns into a choice.
    #
    # THE SECOND OF TWO REFUSALS, and it carries `sandbox_reclaim_blocked` where the one
    # above carries `already_building_here`. Same status, different cause, different remedy:
    # one is "your own other chat is using it", the other is "somebody's unsaved work in
    # another project is in the way". A client that could only read the status told the citizen
    # the wrong thing about half the time.
    if sandbox is not None:
        # The seam wraps the preflight because the guard reads the registry through the
        # deliberately-unguarded `read_registry` (`locks.py`'s policy: an answer-bearing
        # primitive must not swallow a `RedisError` and manufacture a certain-looking "no
        # sandbox"). So an unreadable store arrives here as a `RedisError` and has to become
        # the same 503 every other coordination route gives, not a 500. An UNCONFIGURED Redis
        # skips the block and proceeds, which is right: with no coordination subsystem there is
        # no registry, no slot, and nothing a reclaim could destroy.
        #
        # AND IT IS ALSO THE HAND-OVER'S PREFLIGHT, which is why the body it returns carries
        # more than the status. The browser asks the one-workspace question BY SENDING — every
        # refusal above this line leaves no turn row and no spent card (it no longer leaves no
        # CHAT: the row is created a round trip earlier now, see the block above the daily gate)
        # — and draws its dialog from what comes back:
        # `projectName` for which project holds the workspace, and `agentWorking` for whether
        # that project's agent is mid-thought, of ANY kind (`building` stays narrow, and only
        # marks a turn that can write — see `SandboxReclaimBlockedError`). Neither fact is
        # obtainable from the cheap state poll: its documented budget forbids the container
        # round trip the unsaved-work half needs.
        with build_coordination_or_503():
            try:
                await manager.reclaim_preflight(db, user, project_id, sandbox_client=sandbox)
            except SandboxReclaimBlockedError as exc:
                return reclaim_blocked_response(exc)

    project = await db.get(Project, project_id)
    if project is None:  # ownership was checked above; fail loudly if it ever breaks
        raise AppApiError(404, "Conversation not found.")

    rehydrate = history_rehydrator(db, storage, user.id)

    async def _history() -> list[ModelMessage]:
        """Read twice on the rare path below, so the translation of a rehydration failure into
        the citizen's 400 is written once rather than kept in step by hand."""
        try:
            return await load_history(
                db, user_id=user.id, conversation_id=conversation_id, rehydrate=rehydrate
            )
        except AttachmentRehydrationError as exc:
            raise AppApiError(400, str(exc)) from None

    history = await _history()
    # THE TWO LANES SPLIT HERE, AND THIS IS THE ONLY PLACE THAT KNOWS BOTH.
    #
    # One query answers both halves. The files it returns are the ones the platform must write
    # into the container and name to the agent; their ids are exactly the ids that must NOT reach
    # `resolve_binaries`, because the rehydrator behind it re-asserts the model allowlist — which
    # was deliberately not widened — and would refuse a perfectly good spreadsheet as "no longer
    # matching its declared type". Asking the question twice is how those two answers drift apart.
    #
    # SCOPED TO THE CONVERSATION, NOT TO THIS MESSAGE, for a reason that only shows up on the
    # second turn: `/workspace/attachments` is a sibling of the app tree so that no snapshot or
    # restore carries it, which means a recycled container comes back without it. The message's
    # own ids are passed as well, so a row whose conversation link was never stamped is still
    # found (the column is nullable on purpose).
    code_lane = await code_lane_attachments(
        db,
        user_id=user.id,
        conversation_id=conversation_id,
        attachment_ids=body.message.attachment_ids,
    )
    delivery = (
        AttachmentDelivery(files=tuple(code_lane), storage=storage)
        if code_lane and storage is not None
        else None
    )
    binaries = await resolve_binaries(
        db,
        storage,
        user.id,
        body.message.attachment_ids,
        skip={file.attachment_id for file in code_lane},
    )
    prompt = prompt_content(body.message, binaries)

    # The per-conversation guardrail — STILL ABOVE THE FIRST WRITE, which is what the ordering
    # below it is arranged to keep true. Nothing has been persisted, so a refusal leaves no
    # turn row, no usage row, no claim to release, and no spent plan-options card.
    #
    # IT CANNOT RELY ON A ROLLBACK, and that is why it is here rather than three lines lower.
    # `resolve_pending_as_refine` reaches `append_batch`, which OWNS ITS COMMIT — so a refusal
    # raised after it would leave the citizen's card resolved on disk with `get_db`'s rollback
    # powerless to take it back: their message refused AND their offer silently consumed.
    #
    # It is a REFUSAL, not a run bound. The three ceilings inside the engine stop a run already
    # under way; this one declines to start a turn on a conversation the provider has already
    # reported as past its owner's ceiling — which is why it copies the daily cap's pre-start
    # gate rather than the mid-run terminal. It reads a measurement rather than sizing `prompt`:
    # the message about to be sent has no count until the turn that carries it completes.
    #
    # AND WHAT IT MEASURED RIDES BACK OUT ON THE 202 (`contextTokens`). The browser's meter and
    # this wall are then the same number taken by the same expression on the same request —
    # never two readings of one scale, which is what the deleted estimator was. It costs no
    # extra round trip and, crucially, nothing is sized BEFORE a send: the figure is the one
    # this admission just computed from turns the provider has already served.
    try:
        occupied = await enforce_context_limit(db, user.id, history=history)
    except ContextWindowExceededError as exc:
        # The PROSE says neither number on purpose — a citizen does not think in tokens. The
        # `detail` does, because a non-browser caller has no other way to learn how far over it
        # is: the daily cap's 429 carries `limit`/`used`/`remaining` for exactly this reason, and
        # a refusal that withholds what it already measured makes the second caller guess.
        raise AppApiError(
            413,
            CHAT_TOO_LONG_TEXT,
            code=CHAT_TOO_LONG_CODE,
            detail={"occupied": exc.occupied, "hardLimit": exc.hard_limit},
        ) from None

    # ★ NOTHING IS CREATED OR ADOPTED HERE ANY MORE, and the two deletions are one change.
    #
    # A staged conversation row used to be flushed at this point — after every side-effect-free
    # refusal, before the turn's own commit — together with an `IntegrityError` arm for two first
    # messages racing the same minted id. Both are gone with the `create` block: the row exists
    # before this request is made, and the race it guarded is `POST /v1/conversations`'s now,
    # where the same arm already lives.
    #
    # And this is where NULL-linked uploads were adopted into the conversation. There are none to
    # adopt: an upload names its conversation at the door, so the link is stamped at insert. What
    # the link no longer proves is that a message CARRIED the file — a refused first send leaves
    # its uploads linked and unsent — which is why `code_lane_attachments` reads the sent set out
    # of the stored payloads rather than trusting the link.

    # Free text while plan options are pending resolves them as an implicit "keep refining".
    # The model must see a RESOLVED call — the dangling-call repair never has to guess
    # about a card the user typed past — so when this actually writes one, the history is read
    # again. Only then: the common case is no pending card, and a second full load of a long
    # conversation on every turn to serve the rare one would be a poor trade.
    #
    # It moved BELOW the guardrail above (it used to lead this block) because it is this
    # route's first committing write, and every side-effect-free refusal has to land above it.
    if await resolve_pending_as_refine(db, user_id=user.id, conversation_id=conversation_id):
        history = await _history()

    display_name = user.display_name or user.email
    prompt_context = PromptContext(
        user_name=display_name,
        project_name=project.name,
        project_description=project.description or None,
    )
    app_id = await _app_id_for_project(db, user.id, project_id)
    sent_ids = set(body.message.attachment_ids)

    turn_id = await start_conversation_turn(
        db=db,
        user=user,
        conversation=conversation,
        prompt=prompt,
        history=history,
        prompt_context=prompt_context,
        app_id=app_id,
        model=model,
        factory=factory,
        manager=manager,
        sandbox=sandbox,
        attachments=delivery,
        # ★ THE FILES THIS MESSAGE CARRIED, WHICH IS NOT THE SAME SET THE DELIVERY HOLDS.
        # `delivery` is the whole conversation's code lane — every turn re-places it, because a
        # recycled container comes back empty — while the durable marker is a claim about THIS
        # message, and the projection draws a chip from it. Stamping the delivery made every later
        # message claim every file the chat had ever held: two files across four turns, eight
        # chips. Intersected with what the citizen actually sent, and ordered by the delivery so
        # the marker follows upload order rather than however the request listed them.
        file_attachment_ids=[
            file.attachment_id
            for file in (delivery.files if delivery is not None else ())
            if file.attachment_id in sent_ids
        ],
    )
    # `None` rather than `0` for a conversation nobody has measured — see the field's own note.
    # A brand-new chat is UNMEASURED, not empty, and the meter stays silent on the difference.
    return TurnStartResponse(
        turn_id=str(turn_id), context_tokens=occupied if occupied > 0 else None
    )


@router.post(
    "/{conversation_id}/turns/{turn_id}/stop",
    response_model=TurnStopResponse,
    dependencies=[RequireCsrf],
    responses=error_responses(
        AUTH_401,
        (403, ErrorEnvelope, "CSRF check failed"),
        (404, ErrorEnvelope, "Conversation not found"),
        (409, ErrorEnvelope, "That turn is not this conversation's in-flight turn"),
    ),
)
async def stop_turn(
    conversation_id: uuid.UUID,
    turn_id: uuid.UUID,
    user: CurrentUser,
    db: DbSession,
) -> TurnStopResponse:
    """The explicit cancel (disconnect never cancels)."""
    await resolve_conversation_or_404(db, user.id, conversation_id)
    engine = get_turn_engine()
    try:
        cancelled = await engine.stop_turn(conversation_id, turn_id)
    except TurnNotRunningError:
        raise AppApiError(409, "That turn is not this conversation's in-flight turn.") from None
    return TurnStopResponse(status="stopping" if cancelled else "already_settled")


@router.get(
    "/{conversation_id}/events",
    responses=error_responses(AUTH_401, (404, ErrorEnvelope, "Conversation not found")),
)
async def turn_events(
    conversation_id: uuid.UUID,
    user: CurrentUser,
    db: DbSession,
    cursor: int = Query(default=0, ge=0),
    turn: uuid.UUID | None = Query(default=None),
) -> StreamingResponse:
    """Subscribe to the conversation's turn stream: snapshot-then-tail, or a gap-free
    replay when `turn` matches the live turn and `cursor` is still inside the ring.

    The DB read for the snapshot's persisted items happens HERE, before the response
    commits — the generator itself never touches the request session (an SSE lifetime
    must not hold a DB session; the streamed-reply learning)."""
    conversation = await resolve_conversation_or_404(db, user.id, conversation_id)
    engine = get_turn_engine()
    state = engine.peek(conversation.id)

    # Continuity: same turn + cursor still replayable → tail-only resume.
    replay_only = False
    if state is not None and turn == state.turn_id and cursor > 0:
        _frames, gap = engine.frames_since(state, cursor)
        replay_only = not gap

    snapshot = None
    if not replay_only:
        items: list[DisplayItem] = []
        if state is not None:
            # The turn's persisted rows (its user turn now, plus any mid-build steps),
            # via the one shared derivation — live and reload can never drift.
            rows = await load_rows(
                db, user_id=user.id, conversation_id=conversation.id, include_hidden=True
            )
            projected = await project_conversation(db, user_id=user.id, rows=rows)
            items = projected[-8:]  # the turn's own tail; full history is a separate GET
        snapshot = engine.build_snapshot(state, items=items)

    # Every DB read this route needs is done. Commit now so the pooled connection goes back
    # BEFORE the response starts streaming — otherwise one long-lived SSE pins one connection
    # for the whole turn and a handful of open tabs drains the pool (the streamed-reply
    # learning the docstring above already promises).
    await db.commit()

    queue: asyncio.Queue[None] = asyncio.Queue(maxsize=1)
    if state is not None:
        state.subscribers.add(queue)

    async def generator() -> AsyncIterator[bytes]:
        last_sent = cursor if replay_only else 0
        try:
            if snapshot is not None:
                yield _frame_bytes(snapshot)
                last_sent = snapshot.seq
            if state is None:
                # Idle conversation: snapshot said so; close cleanly.
                yield _DONE
                return
            while True:
                frames, gap = engine.frames_since(state, last_sent)
                if gap:
                    # Fell past the ring's tail — consolidate instead of losing frames.
                    fresh = engine.build_snapshot(state)
                    yield _frame_bytes(fresh)
                    last_sent = fresh.seq
                    continue
                for frame in frames:
                    yield _frame_bytes(frame)
                    last_sent = frame.seq
                    if frame.type == "turn_ended":
                        yield _DONE
                        return
                if state.status != "running" and last_sent >= state.seq:
                    # Settled and fully replayed, but the ring never carried a terminal
                    # (evicted): close explicitly rather than hang.
                    yield _DONE
                    return
                try:
                    await asyncio.wait_for(queue.get(), timeout=KEEPALIVE_SECONDS)
                except TimeoutError:
                    yield b": ping\n\n"
        finally:
            if state is not None:
                state.subscribers.discard(queue)

    return StreamingResponse(generator(), media_type="text/event-stream", headers=_SSE_HEADERS)


class ResolvePlanOptionsBody(CamelModel):
    """The user's click. Only `refine` resolves HERE — `build` goes through the Build-it
    handoff endpoint, which creates the new Build chat and starts its turn BEFORE it answers
    the offer, so a resolved-build can never exist without the build it names."""

    choice: Literal["refine"]


class ResolvePlanOptionsResponse(CamelModel):
    state: Literal["refine", "build"]
    already_resolved: bool


@router.post(
    "/{conversation_id}/plan-options/{tool_call_id}/resolve",
    response_model=ResolvePlanOptionsResponse,
    dependencies=[RequireCsrf],
    responses=error_responses(
        (400, ErrorEnvelope, "Unknown card, or a choice this endpoint does not record"),
        AUTH_401,
        (403, ErrorEnvelope, "CSRF check failed"),
        (404, ErrorEnvelope, "Conversation not found"),
        (409, ErrorEnvelope, "The card is superseded by a newer one"),
    ),
)
async def resolve_plan_options_route(
    conversation_id: uuid.UUID,
    tool_call_id: str,
    body: ResolvePlanOptionsBody,
    user: CurrentUser,
    db: DbSession,
) -> ResolvePlanOptionsResponse:
    """Record "Keep refining" — idempotent on the card id (a second click or second tab
    reads back the stored resolution; a reload can never show resolved-with-no-record)."""
    await resolve_conversation_or_404(db, user.id, conversation_id)
    try:
        resolution = await resolve_plan_options(
            db,
            user_id=user.id,
            conversation_id=conversation_id,
            tool_call_id=tool_call_id,
            choice=body.choice,
        )
    except NoPendingOptionsError:
        raise AppApiError(400, "No such plan options card.") from None
    except PlanOptionsExpiredError:
        raise AppApiError(409, "A newer plan supersedes these options.") from None
    return ResolvePlanOptionsResponse(
        state=resolution.choice, already_resolved=resolution.already_resolved
    )
