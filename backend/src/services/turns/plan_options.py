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
from typing import Literal

from pydantic_ai.messages import ModelRequest, ToolCallPart, ToolReturnPart
from sqlalchemy.ext.asyncio import AsyncSession

from src.db.models.conversation import ChatKind
from src.db.models.message import Message, MessageEntryKind, MessageVisibility
from src.services.messages.projection import PLAN_OPTIONS_TOOL
from src.services.messages.store import append_batch, load_rows

PlanChoice = Literal["refine", "build"]
"""EVERY VALUE HERE IS REACHABLE FROM A USER ACTION, and there used to be a third that was not.

`build_failed` existed so a failed Build-it press could RE-ARM the card the press had burned:
the press resolved the offer first and started the build second, so a failure in between left a
spent card with nothing behind it. `record_build_failure` wrote that state, and it had no
production caller — its only reference outside this module was a test.

The handoff removed the need for it by construction rather than by wiring one up. A failed
handoff commits NOTHING: the offer's answer is the last write on that path, after the turn has
already started, so there is no burned card to re-arm. Keeping a re-arm mechanism for a card
that can no longer be burned would be a second answer to a question that now has one."""

META_PENDING = "plan_options_pending"
META_RESOLVED = "plan_options_resolved"
META_RESOLUTION = "plan_options_resolution"


class PlanOptionsExpiredError(Exception):
    """The named card is not the newest pending one — only the newest is actionable."""


class NoPendingOptionsError(Exception):
    """No pending plan options exist for this conversation (or that id is unknown)."""


@dataclass(frozen=True)
class PendingPlanOptions:
    """The newest unresolved card: who to answer, and where to find the call that made it.

    `row_seq` is enough to find the row, and the row is where the PLAN is — inside the stored
    call's own `args`. Nothing here carries the plan text, and nothing carries a snapshot pin
    any more: the first would be a copy that can silently disagree with the call it describes,
    and the second died with the stale-plan warning it fed."""

    tool_call_id: str
    row_seq: int
    synthesized: bool


@dataclass(frozen=True)
class Resolution:
    tool_call_id: str
    choice: PlanChoice
    reason: str | None
    already_resolved: bool


def _resolution_content(choice: PlanChoice, _reason: str | None) -> str:
    return choice


def _is_open_resolution(resolution: str | None) -> bool:
    """A card is actionable exactly while it has no resolution at all.

    There is no re-arming arm any more: a resolution written is a resolution that stands. A
    stray `build_failed:` string from before the retired recorder therefore reads as TERMINAL
    rather than as live, which is the right way round — a migrated card must not offer a
    button that nothing behind it can answer."""
    return resolution is None


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
    db: AsyncSession, *, user_id: uuid.UUID, conversation_id: uuid.UUID
) -> PendingPlanOptions | None:
    """The newest unresolved card, or None. Older unresolved cards are expired by
    construction — a newer presentation supersedes them."""
    rows = list(
        await load_rows(db, user_id=user_id, conversation_id=conversation_id, include_hidden=True)
    )
    calls, resolutions = _scan(rows)
    for pending in reversed(calls):
        if _is_open_resolution(resolutions.get(pending.tool_call_id)):
            return pending  # unresolved, or a re-armed build_failed — still actionable
        return None  # the newest card is terminally resolved — everything older is superseded
    return None


async def resolve(
    db: AsyncSession,
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
    # EVERY resolution is terminal, so a second click replays the stored answer rather than
    # writing a rival one. An unrecognised stored value — a `build_failed:` string from before
    # the retired recorder — replays as `refine`, which is the only honest reading left: the
    # build did not happen, and the card is spent.
    if stored is not None:
        stored_choice: PlanChoice = "build" if stored == "build" else "refine"
        return Resolution(
            tool_call_id=tool_call_id,
            choice=stored_choice,
            reason=None,
            already_resolved=True,
        )

    by_id = {pending.tool_call_id: pending for pending in calls}
    target = by_id.get(tool_call_id)
    if target is None:
        raise NoPendingOptionsError
    newest_open = next(
        (p for p in reversed(calls) if _is_open_resolution(resolutions.get(p.tool_call_id))), None
    )
    if newest_open is None or newest_open.tool_call_id != tool_call_id:
        raise PlanOptionsExpiredError

    content = _resolution_content(choice, reason)
    if target.synthesized:
        # No real call to answer: the retired synthesizer's cards have no `ToolCallPart` on the
        # wire, so their choice is recorded as a system overlay instead. Migrated rows only —
        # nothing writes a synthesized card any more.
        await append_batch(
            db,
            user_id=user_id,
            conversation_id=conversation_id,
            messages=[],
            entry_kind=MessageEntryKind.SYSTEM_EVENT,
            kind=ChatKind.PLAN,
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
            kind=ChatKind.PLAN,
            meta={"kind": META_RESOLUTION, "toolCallId": tool_call_id, "choice": content},
        )
    return Resolution(
        tool_call_id=tool_call_id, choice=choice, reason=reason, already_resolved=False
    )


async def resolve_pending_as_refine(
    db: AsyncSession, *, user_id: uuid.UUID, conversation_id: uuid.UUID
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


def stored_call(rows: list[Message], tool_call_id: str) -> ToolCallPart | None:
    """The offer's own stored tool call, rebuilt from the row that persisted it.

    THIS IS WHERE THE PLAN LIVES, and it is the only place it lives. The call's `args` carry
    the plan the agent wrote, so the handoff, the projection and anything else that needs it
    all read the same string from the same row — which is what makes "the plan a user reads and
    the plan a build starts from are the same text" a property of the storage rather than an
    agreement between two functions.

    Returns None for a call that was never stored with arguments — every card presented before
    the plan became the tool's argument — so the caller can refuse by name rather than build on
    a stand-in."""
    for row in rows:
        for message in row.payload if isinstance(row.payload, list) else []:
            if not isinstance(message, dict) or message.get("kind") != "response":
                continue
            for part in message.get("parts", []):
                if (
                    isinstance(part, dict)
                    and part.get("part_kind") == "tool-call"
                    and part.get("tool_name") == PLAN_OPTIONS_TOOL
                    and part.get("tool_call_id") == tool_call_id
                ):
                    return ToolCallPart(
                        tool_name=PLAN_OPTIONS_TOOL,
                        args=part.get("args"),
                        tool_call_id=tool_call_id,
                    )
    return None


# THERE IS NO `approved_plan_text`, and its absence is the point rather than a tidy-up.
#
# It walked backwards through the conversation's rows collecting assistant prose, and whatever
# it found was treated as "the plan the user approved" — for a synthesized card, prose from a
# DIFFERENT turn entirely. So a build could start from text nobody had offered, and there was
# no way to tell, from the stored rows, which text a citizen had actually agreed to.
#
# The plan is the offer call's own `args` now. One string, written deliberately by the agent in
# the same act that put the buttons on screen, and read from the same place by everything that
# needs it — which is what makes "the plan a user reads and the plan a handoff posts are the
# same string" a fact about the storage rather than a claim about two functions agreeing.


# THERE IS NO `record_build_failure`, AND NOTHING NEEDS ONE. It wrote a `build_failed:<reason>`
# overlay that re-armed a card a failed Build-it press had already burned — a compensating write
# for an ordering the handoff no longer has. The handoff answers the offer LAST, after the turn
# has started, so a failure leaves the card untouched and still pressable with nothing to undo.
# It never had a production caller in the first place; its only reference outside this module was
# a test, which is how an unreachable state survives a review.


async def record_build_started(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    conversation_id: uuid.UUID,
    pending: PendingPlanOptions,
    answered_already: bool = False,
) -> None:
    """The build genuinely started — the terminal resolution. A real card gets its ONE
    ToolReturnPart ("build"); a synthesized card gets the system overlay (no real call
    exists to answer). Written only after `manager.start` returned a live session:
    resolved-with-no-build stays impossible by ordering.

    `answered_already` is the Build-it vs turn-start race guard (the caller re-checks the card
    right before this write): when a concurrent turn-start already put a real `ToolReturnPart`
    on the wire for this card, the build is recorded as a system overlay too — so the wire
    never carries two returns for one call id, and the projection still reads "build" as the
    newest resolution."""
    if pending.synthesized or answered_already:
        await append_batch(
            db,
            user_id=user_id,
            conversation_id=conversation_id,
            messages=[],
            entry_kind=MessageEntryKind.SYSTEM_EVENT,
            kind=ChatKind.PLAN,
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
        kind=ChatKind.PLAN,
        meta={"kind": META_RESOLUTION, "toolCallId": pending.tool_call_id, "choice": "build"},
    )
