"""Plan-options resolution (U11 / R7): the user's click IS the tool result.

`present_plan_options` DEFERS (the run ends with the call unanswered); the choice arrives
minutes later as a button click — or implicitly, when the user keeps typing instead
(free text while options are pending resolves them as `refine`). The stored resolution is
a plain `ToolReturnPart` row (`refine` / `build` / `build_failed:<reason>`), so the next
run's history carries call + return natively, and the U6 projection derives the card
state from exactly what the model will see — one record, no drift.

A SYNTHESIZED pending (the model never called the tool even when forced — the engine's
fallback) has no real tool call to answer: its pending and resolution both live as
`system_event` rows (`meta.kind = plan_options_pending / plan_options_resolved`) with an
empty payload, so the model's wire history is never polluted with a return for a call
that does not exist.

Only the NEWEST pending is actionable (older cards render expired); a duplicate click or
a second tab resolves idempotently to the already-stored choice — a reload can never show
resolved-with-no-record.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from typing import Any, Literal

from pydantic_ai.messages import ModelRequest, ToolReturnPart

from src.db.models.conversation import ConversationMode
from src.db.models.message import Message, MessageEntryKind, MessageVisibility
from src.services.messages.projection import PLAN_OPTIONS_TOOL
from src.services.messages.store import append_batch, load_rows

PlanChoice = Literal["refine", "build", "build_failed"]

META_PENDING = "plan_options_pending"
META_RESOLVED = "plan_options_resolved"
META_RESOLUTION = "plan_options_resolution"


class PlanOptionsExpiredError(Exception):
    """The named card is not the newest pending one — only the newest is actionable."""


class NoPendingOptionsError(Exception):
    """No pending plan options exist for this conversation (or that id is unknown)."""


@dataclass(frozen=True)
class PendingPlanOptions:
    """The newest unresolved card: who to answer and the plan-time snapshot pin."""

    tool_call_id: str
    row_seq: int
    head_sha: str | None
    synthesized: bool


@dataclass(frozen=True)
class Resolution:
    tool_call_id: str
    choice: PlanChoice
    reason: str | None
    already_resolved: bool


def _resolution_content(choice: PlanChoice, reason: str | None) -> str:
    if choice == "build_failed":
        return f"build_failed:{reason or 'unknown'}"
    return choice


def _scan(rows: list[Message]) -> tuple[list[PendingPlanOptions], dict[str, str]]:
    """One pass over the conversation's rows → (pendings newest-last, resolutions by
    call id). Real calls ride response payloads; returns ride request payloads;
    synthesized pendings/resolutions ride system_event meta."""
    calls: list[PendingPlanOptions] = []
    resolutions: dict[str, str] = {}
    for row in rows:
        meta = row.meta if isinstance(row.meta, dict) else {}
        if meta.get("kind") == META_PENDING and isinstance(meta.get("toolCallId"), str):
            calls.append(
                PendingPlanOptions(
                    tool_call_id=meta["toolCallId"],
                    row_seq=row.seq,
                    head_sha=meta.get("headSha"),
                    synthesized=bool(meta.get("synthesized", False)),
                )
            )
        if meta.get("kind") == META_RESOLVED and isinstance(meta.get("toolCallId"), str):
            resolutions[meta["toolCallId"]] = str(meta.get("choice", "refine"))
        for message in row.payload if isinstance(row.payload, list) else []:
            if not isinstance(message, dict):
                continue
            for part in message.get("parts", []):
                if not isinstance(part, dict):
                    continue
                if (
                    part.get("part_kind") == "tool-return"
                    and part.get("tool_name") == PLAN_OPTIONS_TOOL
                    and isinstance(part.get("tool_call_id"), str)
                ):
                    resolutions[part["tool_call_id"]] = str(part.get("content", ""))
    return calls, resolutions


async def find_pending(
    db: Any, *, user_id: uuid.UUID, conversation_id: uuid.UUID
) -> PendingPlanOptions | None:
    """The newest unresolved card, or None. Older unresolved cards are expired by
    construction — a newer presentation supersedes them."""
    rows = list(
        await load_rows(db, user_id=user_id, conversation_id=conversation_id, include_hidden=True)
    )
    calls, resolutions = _scan(rows)
    for pending in reversed(calls):
        if pending.tool_call_id not in resolutions:
            return pending
        return None  # the newest card is resolved — everything older is superseded
    return None


async def resolve(
    db: Any,
    *,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
    tool_call_id: str,
    choice: PlanChoice,
    reason: str | None = None,
) -> Resolution:
    """Record the user's choice as the tool result — idempotent on the call id: a second
    click (or second tab) answers with the ALREADY-stored resolution, never a rewrite."""
    rows = list(
        await load_rows(db, user_id=user_id, conversation_id=conversation_id, include_hidden=True)
    )
    calls, resolutions = _scan(rows)
    stored = resolutions.get(tool_call_id)
    if stored is not None:
        stored_choice: PlanChoice
        stored_reason: str | None = None
        if stored.startswith("build_failed"):
            stored_choice = "build_failed"
            _, _, stored_reason = stored.partition(":")
        elif stored == "build":
            stored_choice = "build"
        else:
            stored_choice = "refine"
        return Resolution(
            tool_call_id=tool_call_id,
            choice=stored_choice,
            reason=stored_reason or None,
            already_resolved=True,
        )

    by_id = {pending.tool_call_id: pending for pending in calls}
    target = by_id.get(tool_call_id)
    if target is None:
        raise NoPendingOptionsError
    newest_unresolved = next(
        (p for p in reversed(calls) if p.tool_call_id not in resolutions), None
    )
    if newest_unresolved is None or newest_unresolved.tool_call_id != tool_call_id:
        raise PlanOptionsExpiredError

    content = _resolution_content(choice, reason)
    if target.synthesized:
        # No real call exists — the resolution is a companion system record, never a
        # tool return the wire history would reject.
        await append_batch(
            db,
            user_id=user_id,
            conversation_id=conversation_id,
            messages=[],
            entry_kind=MessageEntryKind.SYSTEM_EVENT,
            mode=ConversationMode.PLAN,
            visibility=MessageVisibility.HIDDEN,
            meta={"kind": META_RESOLVED, "toolCallId": tool_call_id, "choice": content},
        )
    else:
        await append_batch(
            db,
            user_id=user_id,
            conversation_id=conversation_id,
            messages=[
                ModelRequest(
                    parts=[
                        ToolReturnPart(
                            tool_name=PLAN_OPTIONS_TOOL,
                            tool_call_id=tool_call_id,
                            content=content,
                        )
                    ]
                )
            ],
            entry_kind=MessageEntryKind.TURN,
            mode=ConversationMode.PLAN,
            meta={"kind": META_RESOLUTION, "toolCallId": tool_call_id, "choice": content},
        )
    return Resolution(
        tool_call_id=tool_call_id, choice=choice, reason=reason, already_resolved=False
    )


async def resolve_pending_as_refine(
    db: Any, *, user_id: uuid.UUID, conversation_id: uuid.UUID
) -> Resolution | None:
    """The implicit resolution (free text while options are pending → `refine`). Called
    by the turn-start path BEFORE history loads, so the model always sees a resolved
    call — the dangling-call repair never has to guess about a card the user simply
    typed past. No pending → None, not an error."""
    pending = await find_pending(db, user_id=user_id, conversation_id=conversation_id)
    if pending is None:
        return None
    return await resolve(
        db,
        user_id=user_id,
        conversation_id=conversation_id,
        tool_call_id=pending.tool_call_id,
        choice="refine",
    )


def resolution_of(rows: list[Message], tool_call_id: str) -> str | None:
    """The stored resolution content for one card, if any (newest write wins)."""
    _calls, resolutions = _scan(rows)
    return resolutions.get(tool_call_id)


def pending_card(rows: list[Message], tool_call_id: str) -> PendingPlanOptions | None:
    """The named card's pending record (real or synthesized), regardless of resolution."""
    calls, _resolutions = _scan(rows)
    for pending in calls:
        if pending.tool_call_id == tool_call_id:
            return pending
    return None


def newest_card(rows: list[Message]) -> PendingPlanOptions | None:
    """The newest presented card (resolved or not) — Build-it acts only on this one."""
    calls, _resolutions = _scan(rows)
    return calls[-1] if calls else None


def approved_plan_text(rows: list[Message], pending: PendingPlanOptions) -> str:
    """The plan the user approved: the assistant text of the presenting turn. For a real
    card that is the text parts of ITS OWN response row; for a synthesized card (the model
    narrated but never called) it is the newest assistant text at or before the card row."""
    best = ""
    for row in rows:
        if row.seq > pending.row_seq:
            break
        texts: list[str] = []
        for message in row.payload if isinstance(row.payload, list) else []:
            if not isinstance(message, dict) or message.get("kind") != "response":
                continue
            for part in message.get("parts", []):
                if isinstance(part, dict) and part.get("part_kind") == "text":
                    content = part.get("content")
                    if isinstance(content, str) and content.strip():
                        texts.append(content)
        if texts:
            best = "\n".join(texts)
    return best


async def record_build_failure(
    db: Any,
    *,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
    tool_call_id: str,
    reason: str,
) -> None:
    """A Build-it attempt failed (lock held / cap / provision). Recorded as a SYSTEM
    overlay for real AND synthesized cards — never a ToolReturnPart, so a later retry's
    success can still write the one true return (exactly one per call id on the wire).
    The projection reads `build_failed:<reason>` and RE-ARMS the card."""
    await append_batch(
        db,
        user_id=user_id,
        conversation_id=conversation_id,
        messages=[],
        entry_kind=MessageEntryKind.SYSTEM_EVENT,
        mode=ConversationMode.PLAN,
        visibility=MessageVisibility.HIDDEN,
        meta={
            "kind": META_RESOLVED,
            "toolCallId": tool_call_id,
            "choice": f"build_failed:{reason}",
        },
    )


async def record_build_started(
    db: Any,
    *,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
    pending: PendingPlanOptions,
) -> None:
    """The build genuinely started — the terminal resolution. A real card gets its ONE
    ToolReturnPart ("build"); a synthesized card gets the system overlay (no real call
    exists to answer). Written only after `manager.start` returned a live session:
    resolved-with-no-build stays impossible by ordering."""
    if pending.synthesized:
        await append_batch(
            db,
            user_id=user_id,
            conversation_id=conversation_id,
            messages=[],
            entry_kind=MessageEntryKind.SYSTEM_EVENT,
            mode=ConversationMode.PLAN,
            visibility=MessageVisibility.HIDDEN,
            meta={"kind": META_RESOLVED, "toolCallId": pending.tool_call_id, "choice": "build"},
        )
        return
    await append_batch(
        db,
        user_id=user_id,
        conversation_id=conversation_id,
        messages=[
            ModelRequest(
                parts=[
                    ToolReturnPart(
                        tool_name=PLAN_OPTIONS_TOOL,
                        tool_call_id=pending.tool_call_id,
                        content="build",
                    )
                ]
            )
        ],
        entry_kind=MessageEntryKind.TURN,
        mode=ConversationMode.PLAN,
        meta={"kind": META_RESOLUTION, "toolCallId": pending.tool_call_id, "choice": "build"},
    )
