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

from pydantic import (
    BaseModel,
    ConfigDict,
    Discriminator,
    Field,
    Tag,
    TypeAdapter,
    model_validator,
)

from src.api.v1.build_sessions.schemas import ErrorSource
from src.db.models.conversation import ChatKind
from src.schemas import CamelModel
from src.services.messages.projection import DisplayItem, PlanOptionsItem, StepItem
from src.services.orchestrator.errors import user_facing
from src.services.sandbox.base import CompileState


class HeaderOut(CamelModel):
    """One conversation header. `title`/`context` are omitted when unset — the route's
    `_header_dict` builds them in only when present. `kind` is what the chat IS, fixed when it
    was created (R14/R16) — there is no second field beside it, because there is no longer a
    second concept. This model is documented-only (the route returns a pre-built
    `JSONResponse`), so no exclude-unset serialization flag is involved; the `= None` defaults
    are what document those fields as non-required."""

    id: str = Field(alias="_id")
    project_id: str
    kind: str
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
    # REQUIRED, and the only place a chat's kind is ever set (R15). There is no route that
    # changes it afterwards; a value outside the enum is refused at this boundary rather than
    # coerced to a default, because "which chat is this" decides what the model can do.
    kind: ChatKind
    title: str | None = None
    context: Any = None


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
    # U2 — SOMETHING THE PLATFORM NEEDS THE CITIZEN TO SEE, as distinct from `message`, which
    # narrates the phase this frame announces ("Getting your workspace ready…").
    #
    # The two are different kinds of speech and they belong in different places on screen, which
    # is why they are different fields rather than one. `message` describes what is happening
    # right now and is correctly replaced by whatever happens next. A notice is a statement about
    # the APP — it was reset and is being put back, it was reset and cannot be, we could not check
    # it — and it stays true after the phase that carried it has passed. Sharing one field made
    # every ordinary turn post its phase narration into the banner slot above the composer.
    notice: str | None = None


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
    failing — a repair run follows.

    ONE AUDIENCE RIDES THIS FRAME, and it did not used to be that way. `title` and
    `cleaned_stack` — the compiler's own first meaningful line and the de-noised log — travelled
    here beside the citizen's half, described in this very docstring as "safe to transmit; NOT a
    product surface". That distinction is not one a wire format can hold: the sentence "safe to
    render verbatim" is what once put a stack trace under a file-path title in a citizen's chat,
    and the note warning against it was already in place when that happened.

    So the model's half stays SERVER-SIDE, where the repair run reads it off the `BuildError`
    that produced it. It is not lost, it is not redacted, and it is not sent. What crosses is
    `user_message` / `user_action`: a plain sentence about the citizen's app and something they
    can do about it, which is the only half any surface ever rendered.
    """

    type: Literal["diagnostic"] = "diagnostic"
    seq: int
    # The build's OWN enum, not a re-spelled Literal. `ErrorSource` is a StrEnum, so the
    # wire shape is identical either way — but a second copy of the member list is a copy
    # that can drift, and the producer already holds a `BuildError.source`.
    #
    # KEPT, and it is worth saying why when its two neighbours went: `source` is a closed
    # vocabulary of error CLASSES this schema already publishes, it is what the citizen-facing
    # pair is derived from below, and it carries nothing out of the build — a class name is not
    # a stack.
    source: ErrorSource
    # DERIVED FROM `source` WHEN THE PRODUCER SUPPLIES NOTHING, which is the whole safety
    # property: the pair cannot be forgotten into emptiness by a caller that only knows about
    # the model's half, so every diagnostic that reaches a person carries a sentence AND a next
    # action. A producer that has something better to say may still pass its own.
    user_message: str = ""
    user_action: str = ""

    @model_validator(mode="after")
    def _speak_product_language(self) -> DiagnosticFrame:
        """Backfill either empty half of the citizen-facing pair from the error class.

        A cross-field invariant, not a presence check dressed up as one: what the pair should
        say is a function of `source`, so it cannot be expressed as a no-default field — and a
        blank half is the one state that must never egress, in every environment."""
        fallback = user_facing(self.source)
        if not self.user_message:
            self.user_message = fallback.message
        if not self.user_action:
            self.user_action = fallback.action
        return self


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
