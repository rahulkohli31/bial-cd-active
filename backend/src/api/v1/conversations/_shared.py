"""The turn plumbing the conversation routes run on.

These helpers used to live as underscore-private names inside the legacy relay's router, and
`conversations/turns.py` reached across a package boundary to import them anyway — which is
exactly the coupling ADR-0010 warns about: a "private" name with a second consumer is not
private, it is undocumented shared API, and the next edit to the relay silently reshaped the
turn route.

The home is `conversations/` rather than beside the relay on purpose: the relay was the surface
being retired, so the code that outlived it should not sit in the module that died. It has since
died, and this file is what that move was for. The underscore in the FILE name marks it as
internal to `api/v1` — it is plumbing, not a route module — while every NAME it exports is
public, because it genuinely has more than one caller: the send route, the transition route, and
the test fixtures that bind `chat_model` and `billing_session_factory`.
"""

from __future__ import annotations

import base64
import re
import uuid
from collections.abc import Sequence
from typing import Annotated

import sqlalchemy as sa
from fastapi import Depends
from pydantic import Field, model_validator
from pydantic_ai import BinaryContent
from pydantic_ai.models import Model
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.config import settings
from src.core.errors import AppApiError
from src.db.base import async_session_factory
from src.db.models.conversation import Conversation
from src.schemas import CamelModel
from src.services.agent.model import build_foundry_model
from src.services.messages.store import (
    AttachmentRehydrationError,
    Rehydrator,
    attachment_rehydrator,
)
from src.services.storage import ObjectStorage, StorageUnconfiguredError, get_storage

# --- message bounds -----------------------------------------------------------------------
#
# Message-shape bounds. Generous by intent: a citizen developer pasting a long spec is normal
# traffic, and the real cost gate is the daily token limit, not a byte ceiling.
#
# THIS NUMBER SITS FAR ABOVE THE BROWSER'S OWN CAP ON PURPOSE, AND THEY ARE NOT TWO SPELLINGS
# OF ONE RULE (R42a). The composer caps what a person can TYPE, which is a courtesy — it stops
# someone pasting a novel and waiting to find out it was too much. This is the platform's own
# SAFETY limit on what may be stored, and the server keeps its own precisely so it does not
# inherit a number chosen for a text box: the handoff materialises a plan the browser never
# typed, and a build's first message is written by the server, not by a keyboard.
#
# Both halves are refusals, never trims. A message cut at a ceiling is one the citizen believes
# they sent whole, and the platform has no way to tell them otherwise afterwards.
#
# So: raising the browser cap toward this one is a decision, not a tidy-up, and lowering this
# one to match the browser would silently break the server-materialised paths. If you are here
# to collapse two numbers into one, that is the reason not to.
MAX_MESSAGE_TEXT_CHARS = 64_000
MAX_ATTACHMENT_TEXT_CHARS = 600_000
MAX_ATTACHMENT_BLOCKS = 8

# An attachment id is a `secrets.token_urlsafe` value (ADR-0006) — never a path or a raw UUID.
ATTACHMENT_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,128}$")

# --- the build-in-flight refusal ------------------------------------------------------------
#
# Citizen copy — this text reaches the user verbatim in a 409 body. It says what is happening and
# when they get the chat back, and names nothing internal (no session, no mode, no lock).
#
# It lives HERE, beside the other shared plumbing, because more than one route refuses this way:
# the send route and the plan→build handoff. A gate that exists on only one of two live entry
# points is not a gate — it is a detour sign, which is the lesson the retired relay taught by
# needing its own copy of this constant.
BUILD_IN_FLIGHT_MSG = (
    "The assistant is building your app right now. Chat opens back up as soon as it finishes."
)

# What may enter the prompt as a BINARY: vision content only. Everything else travels as
# `attachment_texts`.
VISION_MEDIA_PREFIX = "image/"
PDF_MEDIA_TYPE = "application/pdf"


# --- dependencies -------------------------------------------------------------------------

# The billing/agent session factory — a dependency (like storage) so tests bind it to the
# rolled-back test session instead of committing to the real DB.
BillingSessionFactory = async_sessionmaker[AsyncSession]


def chat_model() -> Model | None:
    """The Foundry-backed Pydantic AI model, or None when Foundry isn't configured (dev/test
    boot without it). A dependency so tests inject a `TestModel` via `dependency_overrides`."""
    if settings.foundry is None:
        return None
    return build_foundry_model(settings.foundry)


def billing_session_factory() -> BillingSessionFactory:
    """The session factory the disconnect-safe drain uses (its own session, decoupled from the
    request). A dependency so tests bind it to the rolled-back test session."""
    return async_session_factory


def chat_storage() -> ObjectStorage | None:
    """The object store, or None when unconfigured — NEVER an eager raising `Depends` (the
    eager-Depends learning: a raise here would 500 every text-only turn on a storage-less
    boot). The None arm fails IN-BODY, typed, exactly where refs are actually needed: sending
    an attachment id → 503; loading a history that carries stored references → the
    rehydrator's own typed failure."""
    try:
        return get_storage()
    except StorageUnconfiguredError:
        return None


ModelDep = Annotated[Model | None, Depends(chat_model)]
SessionFactoryDep = Annotated[BillingSessionFactory, Depends(billing_session_factory)]
StorageDep = Annotated[ObjectStorage | None, Depends(chat_storage)]


# --- the wire message ---------------------------------------------------------------------


class TurnMessage(CamelModel):
    """The new message — the ONLY content the browser sends (R9).

    `attachment_texts` are complete, client-built `<attachment …>…</attachment>` fence blocks:
    inline text files (whose bytes are never uploaded) and office extractions (whose bytes are
    stored but never model-visible). They are opaque text to this route — fencing/neutralizing
    happened where the content was assembled, and redaction happens at the persistence seam.
    `attachment_ids` are owned references to STORED binaries (image/PDF), resolved to base64
    server-side at send."""

    text: str = Field(max_length=MAX_MESSAGE_TEXT_CHARS)
    attachment_texts: list[str] = Field(default_factory=list, max_length=MAX_ATTACHMENT_BLOCKS)
    attachment_ids: list[str] = Field(default_factory=list, max_length=MAX_ATTACHMENT_BLOCKS)

    @model_validator(mode="after")
    def _bounded_and_non_empty(self) -> TurnMessage:
        for block in self.attachment_texts:
            if len(block) > MAX_ATTACHMENT_TEXT_CHARS:
                raise ValueError("an attachment text block is too large")
        for attachment_id in self.attachment_ids:
            if not ATTACHMENT_ID_RE.fullmatch(attachment_id):
                raise ValueError("an attachment id is invalid")
        return self


# --- turn preparation ---------------------------------------------------------------------


async def resolve_conversation_or_404(
    db: AsyncSession, user_id: uuid.UUID, conversation_id: uuid.UUID
) -> Conversation:
    """The turn's owner-scoped conversation row. U7 retires the old load-bearing None arm:
    conversations are created BEFORE the first turn (`POST /v1/conversations`), so an unknown
    id is a client bug and a cross-user id is indistinguishable from it — one non-leaking 404
    (ADR-0004)."""
    conversation: Conversation | None = await db.scalar(
        sa.select(Conversation).where(
            Conversation.id == conversation_id, Conversation.user_id == user_id
        )
    )
    if conversation is None:
        raise AppApiError(404, "Conversation not found.")
    return conversation


def history_rehydrator(
    db: AsyncSession, storage: ObjectStorage | None, user_id: uuid.UUID
) -> Rehydrator:
    """The rehydrator `load_history` swaps stored reference markers through. With storage
    unconfigured it fails TYPED — and only if the history actually carries a reference, so a
    text-only conversation on a storage-less boot keeps working."""
    if storage is not None:
        return attachment_rehydrator(db, storage, user_id)

    async def unconfigured(attachment_ids: Sequence[str]) -> dict[str, tuple[str, str]]:
        if not attachment_ids:
            return {}
        raise AttachmentRehydrationError(
            "an attached file could not be read (file storage is not configured)"
        )

    return unconfigured


async def resolve_binaries(
    db: AsyncSession, storage: ObjectStorage | None, user_id: uuid.UUID, attachment_ids: list[str]
) -> list[BinaryContent]:
    """Owned attachment refs → base64-backed `BinaryContent` for the model prompt (the plan's
    refs→base64-at-send resolver). Rides the store's own rehydrator — owner-scoped row, magic
    re-check, authoritative media type — then gates on WHAT may enter the prompt: only
    image/PDF vision content. Office originals and anything else are a 400 (their content
    travels as `attachmentTexts`), and an unknown/foreign id fails the same typed way the
    rehydrator words it."""
    if not attachment_ids:
        return []
    if storage is None:
        raise AppApiError(503, "File storage is not configured.")
    rehydrate = attachment_rehydrator(db, storage, user_id)
    try:
        resolved = await rehydrate(attachment_ids)
    except AttachmentRehydrationError as exc:
        raise AppApiError(400, str(exc)) from None
    binaries: list[BinaryContent] = []
    for attachment_id in attachment_ids:
        data_b64, media_type = resolved[attachment_id]
        if not (media_type.startswith(VISION_MEDIA_PREFIX) or media_type == PDF_MEDIA_TYPE):
            raise AppApiError(
                400,
                "an attached file of this type cannot be sent to the assistant as a file; "
                "its extracted text travels with the message instead",
            )
        binaries.append(
            BinaryContent(
                data=base64.b64decode(data_b64), media_type=media_type, identifier=attachment_id
            )
        )
    return binaries


def prompt_content(
    message: TurnMessage, binaries: list[BinaryContent]
) -> str | list[str | BinaryContent]:
    """The turn's user content: binaries first, fenced attachment text next, the typed prose
    LAST (Anthropic's files-before-text ordering — the same shape `BuildSpec` pins). A plain
    text-only message stays a bare string (the historical single-string shape)."""
    if not binaries and not message.attachment_texts:
        return message.text
    return [*binaries, *message.attachment_texts, message.text]
