"""Conversations wire-shape schemas — the SPA's Mongo-style `_id` + camelCase envelopes.

Every conversations route returns a pre-built `JSONResponse` (to emit the exact Express
wire shape and the `{"error":{"message"}}` envelope), so the RESPONSE models here are
DOCUMENTED-ONLY: FastAPI advertises them in OpenAPI but never validates or reshapes the
response — the characterization tests are the byte-identical guard. `_id` needs an
explicit alias because Pydantic treats a leading-underscore field name as private.

The legacy message-append/read schemas died with their endpoints (U4's destructive reset);
the projection read shape joins in U6.

The one exception is `BuilderThreadRequest`: the canonical-thread route is net-new (it has
no Express body contract to byte-match), so its body IS parsed through the model and its
validation errors are FastAPI's own 422.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Discriminator, Field, Tag, TypeAdapter

from src.db.models.conversation import ConversationKind
from src.schemas import CamelModel
from src.services.messages.projection import DisplayItem, StepItem


class HeaderOut(CamelModel):
    """One conversation header. `title`/`context` are omitted when unset — the route's
    `_header_dict` builds them in only when present. `mode` is the server-owned sticky chat
    mode (U4). This model is documented-only (the route returns a pre-built `JSONResponse`),
    so no exclude-unset serialization flag is involved; the `= None` defaults are what
    document those fields as non-required."""

    id: str = Field(alias="_id")
    project_id: str
    kind: str
    mode: str
    created_at: str
    updated_at: str
    title: str | None = None
    context: Any = None


class ConversationListResponse(CamelModel):
    conversations: list[HeaderOut]


class ConversationCreateRequest(CamelModel):
    """`POST /conversations` — create the row BEFORE the first turn (U7). The id stays
    client-minted (`crypto.randomUUID`, the R14 model); the server validates ownership of the
    parent project and makes the call idempotent per owner, so the SPA's synchronous
    mint-then-navigate flow needs no extra round trip on a retry."""

    id: uuid.UUID
    project_id: uuid.UUID
    kind: ConversationKind
    title: str | None = None
    context: Any = None


class ConversationCreateResponse(CamelModel):
    conversation: HeaderOut


class BuilderThreadRequest(CamelModel):
    """The `POST /conversations/builder-thread` body — the project whose canonical build
    thread to resolve. Unlike the rest of this module these two ARE live models: the route
    parses its body through `BuilderThreadRequest` and hand-builds only the response (to keep
    the `_id` wire shape), so a malformed `projectId` is FastAPI's 422, not a hand-rolled 400."""

    project_id: uuid.UUID


class BuilderThreadResponse(CamelModel):
    """The project's ONE canonical builder conversation — resolved or freshly created."""

    conversation: HeaderOut


class ActiveTurnOut(CamelModel):
    """The in-flight turn, when one is running (U10 wires the real registry; until then the
    route always answers null). `last_seq` is the turn's newest event seq — the cursor a
    subscriber resumes the event stream from."""

    turn_id: str
    last_seq: int


class ConversationDetailResponse(CamelModel):
    """Header + display projection + the U10 `activeTurn` seam — one read rebuilds the chat.
    `projection` items are the `services/messages/projection.py` models (the single
    history→display derivation; documented here, produced there)."""

    conversation: HeaderOut
    projection: list[DisplayItem]
    active_turn: ActiveTurnOut | None = None


# ---------------------------------------------------------------------------------------
# The U10 turn-stream frame union (one transport for chat + build activity)
# ---------------------------------------------------------------------------------------
#
# Every frame carries the per-turn monotonic `seq` (the SSE `id:` cursor). The union
# discriminates through a CALLABLE Discriminator + Tag (the chat-message-shapes learning):
# a malformed KNOWN tag routes to its model and RAISES on validation, while an unknown tag
# is captured as `UnknownFrame` without degrading the known members — new server frame
# types never silently break an older parser that revalidates a stream.


class SnapshotFrame(CamelModel):
    """The catch-up snapshot — always the first frame of a subscription that cannot prove
    gap-free continuity (fresh subscribe, F5, cursor fallen out of the ring). `items` are
    the turn's PERSISTED rows projected through the one U6 derivation; `text_so_far` and
    `steps` are the in-memory tail the DB does not hold yet. A client renders snapshot
    state then applies the live tail from `seq`."""

    type: Literal["snapshot"] = "snapshot"
    seq: int
    turn_id: str | None
    turn_status: Literal["idle", "running", "completed", "failed", "stopped"]
    items: list[DisplayItem] = Field(default_factory=list)
    text_so_far: str = ""
    steps: list[StepItem] = Field(default_factory=list)


class TextDeltaFrame(CamelModel):
    """One streamed slice of the assistant's reply text."""

    type: Literal["text_delta"] = "text_delta"
    seq: int
    text: str


class StepFrame(CamelModel):
    """A friendly agent step, live. `phase='started'` announces the tool call (item state
    `pending`); `phase='finished'` re-delivers the SAME step resolved (`ok`/`failed`).
    Clients key on `tool_call_id` to replace the pending card in place — `item` is the
    identical shape the reload projection produces, so live and reload can never drift."""

    type: Literal["step"] = "step"
    seq: int
    tool_call_id: str
    phase: Literal["started", "finished"]
    item: StepItem


class TurnErrorFrame(CamelModel):
    """An in-band, user-safe failure notice (the turn is failing; a terminal follows)."""

    type: Literal["error"] = "error"
    seq: int
    message: str


class TurnEndedFrame(CamelModel):
    """The semantic terminal — exactly one per turn; the transport closes right after
    (`data: [DONE]`)."""

    type: Literal["turn_ended"] = "turn_ended"
    seq: int
    turn_id: str
    status: Literal["completed", "failed", "stopped"]


class UnknownFrame(BaseModel):
    """A frame type this parser does not know — captured verbatim (extra="allow"), never
    an error: streams must be forward-extensible without breaking old parsers."""

    model_config = ConfigDict(extra="allow")

    type: str
    seq: int = 0


_KNOWN_FRAME_TAGS: Final = frozenset({"snapshot", "text_delta", "step", "error", "turn_ended"})


def _frame_tag(value: Any) -> str | None:
    """Callable discriminator: route known tags to their model (whose validation then
    RAISES on a malformed body — never silent), everything else to `UnknownFrame`."""
    tag = value.get("type") if isinstance(value, dict) else getattr(value, "type", None)
    if isinstance(tag, str) and tag in _KNOWN_FRAME_TAGS:
        return tag
    return "unknown"


TurnStreamFrame = Annotated[
    Annotated[SnapshotFrame, Tag("snapshot")]
    | Annotated[TextDeltaFrame, Tag("text_delta")]
    | Annotated[StepFrame, Tag("step")]
    | Annotated[TurnErrorFrame, Tag("error")]
    | Annotated[TurnEndedFrame, Tag("turn_ended")]
    | Annotated[UnknownFrame, Tag("unknown")],
    Discriminator(_frame_tag),
]

TURN_STREAM_FRAME_ADAPTER: TypeAdapter[TurnStreamFrame] = TypeAdapter(TurnStreamFrame)
"""The one validating parser for turn-stream frames (tests + any server-side re-parse)."""


class TurnStartResponse(CamelModel):
    """`POST /conversations/{id}/turns` → 202: the turn now runs detached."""

    turn_id: str


class TurnStopResponse(CamelModel):
    """`POST /conversations/{id}/turns/{turnId}/stop` → the stop landed (or the turn was
    already settled — stopping twice is not an error)."""

    status: Literal["stopping", "already_settled"]
