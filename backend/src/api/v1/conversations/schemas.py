"""Conversations wire-shape schemas — the SPA's Mongo-style `_id` + camelCase envelopes.

Every conversations route returns a pre-built `JSONResponse` (to emit the exact Express
wire shape and the `{"error":{"message"}}` envelope), so the RESPONSE models here are
DOCUMENTED-ONLY: FastAPI advertises them in OpenAPI but never validates or reshapes the
response — the characterization tests are the byte-identical guard. `_id` needs an
explicit alias because Pydantic treats a leading-underscore field name as private.

The legacy message-append/read schemas died with their endpoints (U4's destructive reset);
the projection read shape joins in U6.

Net-new routes (the U13 mode switch, the U10/U11/U12 turn surfaces) parse their bodies
through models normally — only the Express-era routes keep the byte-matched JSONResponse
discipline.
"""

from __future__ import annotations

import uuid
from typing import Annotated, Any, Final, Literal

from pydantic import BaseModel, ConfigDict, Discriminator, Field, Tag, TypeAdapter

from src.api.v1.build_sessions.schemas import ErrorSource
from src.db.models.conversation import ConversationKind, ConversationMode
from src.schemas import CamelModel
from src.services.messages.projection import DisplayItem, PlanOptionsItem, StepItem
from src.services.sandbox.base import CompileState


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
    # The starting chat mode (U13): the root box mints Ask/Plan/Write chats. Optional so
    # older callers keep the server default ('plan').
    mode: ConversationMode | None = None


class ConversationCreateResponse(CamelModel):
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
    state then applies the live tail from `seq`.

    `error_message` carries the WHY of a failed turn. The in-band `error` frame lives only in
    the ring, so a subscriber that arrives after the failure — or whose cursor fell past it —
    would otherwise read `turnStatus: "failed"` with no reason to show the user.

    The workspace/preview trio is the same catch-up reasoning applied to a Write turn: a
    `preview` frame that fired BEFORE the client connected lives only in the ring, so a
    mid-Write reconnect would otherwise have to re-read the sandbox over REST to learn the
    url it already missed. Carrying the three facts here makes the snapshot self-sufficient.
    `compile_state` is the fourth fact and it is carried for a sharper version of the same
    reason: compile frames are emitted ON CHANGE, so a tab that reloads while the app is sitting
    broken would learn nothing until the NEXT change — and until then its preview cover would be
    down over the error screen the cover exists to hide. The snapshot is what makes a refresh
    mid-build land covered.

    All four are optional and default to None — a chat turn has no workspace to describe."""

    type: Literal["snapshot"] = "snapshot"
    seq: int
    turn_id: str | None
    turn_status: Literal["idle", "running", "completed", "failed", "stopped"]
    items: list[DisplayItem] = Field(default_factory=list)
    text_so_far: str = ""
    steps: list[StepItem] = Field(default_factory=list)
    error_message: str | None = None
    workspace_state: Literal["preparing", "ready", "unavailable"] | None = None
    preview_url: str | None = None
    preview_state: Literal["ready", "reconnecting"] | None = None
    compile_state: CompileState | None = None


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


class PlanOptionsFrame(CamelModel):
    """The Build it / Keep refining card, live (U11). `item` is the identical shape the
    reload projection produces — resolution state always derives from the stored record."""

    type: Literal["plan_options"] = "plan_options"
    seq: int
    item: PlanOptionsItem


class WorkspaceFrame(CamelModel):
    """The turn's sandbox lifecycle — what the client's phase machine reads. `preparing` is a
    cold provision or a snapshot restore (30-60s, which is why it never blocks the POST);
    `unavailable` is a TYPED failure carrying citizen-readable copy, not a hang."""

    type: Literal["workspace"] = "workspace"
    seq: int
    state: Literal["preparing", "ready", "unavailable"]
    message: str | None = None


class PreviewFrame(CamelModel):
    """The app's live preview. `ready` carries the url to frame — the client keys its iframe
    on it, so a NEW url remounts and reloads; `reconnecting` says the dev process died and a
    re-frame is coming, which only the server can know (`/dev/status` is bearer-guarded)."""

    type: Literal["preview"] = "preview"
    seq: int
    state: Literal["ready", "reconnecting"]
    preview_url: str | None = None


class CompileFrame(CamelModel):
    """What the app's dev server is compiling right now (R17/R18), so the preview pane can
    cover its frame instead of letting the framework's full-screen error screen fill it.

    Emitted ON CHANGE, not on every poll: the watcher asks once a second for the whole turn,
    and a frame per poll would be several hundred per build on a ring sized for narrative.

    `unknown` is a real value and the client MUST hold its current cover on it rather than
    clearing — see `CompileState`. The signal reaches a container only once it runs an image
    carrying `/dev/compile`; until then every poll is `unknown`, which is precisely why an
    absent signal may never read as clean."""

    type: Literal["compile"] = "compile"
    seq: int
    # The build's OWN enum rather than a re-spelled Literal, exactly as `DiagnosticFrame.source`
    # does: a StrEnum has an identical wire shape, and a second copy of the member list is a
    # copy that can drift from the value the producer already holds.
    state: CompileState


class DiagnosticFrame(CamelModel):
    """An in-narrative build diagnostic. Deliberately NOT an `error` frame: the turn is not
    failing — a repair run follows. `cleaned_stack` is already de-noised and secret-redacted
    by `errors.declutter`, so it is safe to render verbatim."""

    type: Literal["diagnostic"] = "diagnostic"
    seq: int
    # The build's OWN enum, not a re-spelled Literal. `ErrorSource` is a StrEnum, so the
    # wire shape is identical either way — but a second copy of the member list is a copy
    # that can drift, and the producer already holds a `BuildError.source`.
    source: ErrorSource
    title: str
    cleaned_stack: str


class QuotaFrame(CamelModel):
    """The daily cap, hit mid-turn. Structured rather than free text because the client
    formats the numbers itself and drives its own banner — an `error` frame's message string
    cannot be re-formatted."""

    type: Literal["quota"] = "quota"
    seq: int
    limit: int
    used: int
    resets_at: str


class TurnEndedFrame(CamelModel):
    """The semantic terminal — exactly one per turn; the transport closes right after
    (`data: [DONE]`).

    `reason` names WHY a non-`completed` turn stopped (`quota_exceeded`,
    `self_heal_budget_exhausted`, `sandbox_gone`, `wall_clock_deadline_exceeded`,
    `request_limit`, `build_wrote_nothing`, `stopped_by_user`). `snapshot_committed` is
    deliberately TRI-STATE:
    `true`/`false` are the finalize's answer, and `null` means UNKNOWN — a non-Write turn
    with nothing to snapshot, or a terminal that never reached the finalize. `null` is not
    `false`; a client that collapses the two claims lost work that may well be saved.

    All three are optional additions — the portal narrows this frame field by field, so old
    and new frames must both keep parsing."""

    type: Literal["turn_ended"] = "turn_ended"
    seq: int
    turn_id: str
    status: Literal["completed", "failed", "stopped"]
    reason: str | None = None
    preview_url: str | None = None
    snapshot_committed: bool | None = None


class UnknownFrame(BaseModel):
    """A frame type this parser does not know — captured verbatim (extra="allow"), never
    an error: streams must be forward-extensible without breaking old parsers."""

    model_config = ConfigDict(extra="allow")

    type: str
    seq: int = 0


_KNOWN_FRAME_TAGS: Final = frozenset(
    {
        "snapshot",
        "text_delta",
        "step",
        "plan_options",
        "error",
        "turn_ended",
        "workspace",
        "preview",
        "diagnostic",
        "quota",
        "compile",
    }
)


def _frame_tag(value: Any) -> str | None:
    """Callable discriminator: route known tags to their model (whose validation then
    RAISES on a malformed body — never silent), everything else to `UnknownFrame`."""
    tag = value.get("type") if isinstance(value, dict) else getattr(value, "type", None)
    if isinstance(tag, str) and tag in _KNOWN_FRAME_TAGS:
        return tag
    return "unknown"


# A new frame type needs BOTH `_KNOWN_FRAME_TAGS` above and a member here: a tag listed in
# only one of the two still parses, silently, as an `UnknownFrame` — the forward-compat
# escape hatch swallowing our own frame instead of the foreign one it exists for.
TurnStreamFrame = Annotated[
    Annotated[SnapshotFrame, Tag("snapshot")]
    | Annotated[TextDeltaFrame, Tag("text_delta")]
    | Annotated[StepFrame, Tag("step")]
    | Annotated[PlanOptionsFrame, Tag("plan_options")]
    | Annotated[TurnErrorFrame, Tag("error")]
    | Annotated[TurnEndedFrame, Tag("turn_ended")]
    | Annotated[WorkspaceFrame, Tag("workspace")]
    | Annotated[PreviewFrame, Tag("preview")]
    | Annotated[DiagnosticFrame, Tag("diagnostic")]
    | Annotated[QuotaFrame, Tag("quota")]
    | Annotated[CompileFrame, Tag("compile")]
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


class ModeSwitchResponse(CamelModel):
    """`POST /conversations/{id}/mode` → the conversation's mode AFTER the switch (the same
    value on an idempotent same-mode call). Documentation-only: the route hand-builds this
    body as a `JSONResponse`, so declaring it changes the schema, never the bytes."""

    mode: ConversationMode
