"""Stored native history → friendly display items (U6, plan 2026-07-22-002).

ONE derivation, two consumers: the conversation read API (reload) now, and the turn engine's
catch-up snapshot (live) in U10 — never a second source of truth. The input is the raw ROWS
(`store.load_rows(include_hidden=True)`), not validated dataclasses, deliberately:

* No `ModelMessagesTypeAdapter.validate_python` here — a stored attachment-ref marker would
  silently coerce to `CachePoint` (the pinned 2.5.0 hazard) and validation would demand
  rehydration this read must never pay for (the UI wants a chip, not the bytes).
* No re-redaction — every string in `payload`/`meta` was redacted at the persistence seam;
  the Details expander exposes exactly those stored values, nothing rawer.

Hidden rows are excluded from RENDERING but still inform derived state: an unclosed
`build_started` marker (no `build_outcome` with the same sessionId anywhere after it)
projects a truthful "a build was running here" anchor — the crashed/mid-build reload story
(R8). Mode-switch markers render nothing, ever.

Inside a build session's step rows, only the FIRST row's user prompt renders as a user
bubble: that is the instruction the user actually sent. Later step-row prompts are the
harness's own repair/continue nudges (`build_repair_prompt` / `CONTINUE_PROMPT`) — rendering
them would put words in the user's mouth.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any, Final, Literal

from pydantic import Field

from src.db.models.message import Message, MessageEntryKind, MessageVisibility
from src.schemas import CamelModel
from src.services.messages.store import ATTACHMENT_REF_KIND

# The Plan-mode options tool (U8 stub, U11 mechanics). The projection derives the card's
# resolution state from this tool's stored call/return pair.
PLAN_OPTIONS_TOOL: Final = "present_plan_options"

# Details-expander cap per block (args / result). Wire-size bound only — the content is
# already redacted and producer-bounded; this just keeps one giant tool return from bloating
# every reload of the conversation.
_DETAIL_CAP_CHARS: Final = 4_000
_TRUNCATION_MARK: Final = " …[truncated]"

# Read-only commands (U8's guest list) render as hidden inspection steps — same rule as the
# structured read tools: reads are noise until the user opens the Details expander.
_READ_ONLY_BINARIES: Final = frozenset({"ls", "cat", "head", "tail", "grep", "sed", "find", "wc"})

# Housekeeping shell verbs — plumbing the citizen never needs to see. Hidden like the read-only
# inspections: the model still gets the raw output, the chat stays quiet (F3/U3).
_HOUSEKEEPING_BINARIES: Final = frozenset({"mkdir", "mv", "cp", "touch", "echo", "cd"})

_INSTALL_SUBCOMMANDS: Final = frozenset({"install", "i", "ci", "add"})
_PACKAGE_MANAGERS: Final = frozenset({"npm", "pnpm", "yarn", "bun"})

# Friendly command copy (F3/U3 pinned starting set — tunable in the UI). The classifier NEVER
# surfaces the raw command; the browser only ever receives one of these labels.
_LBL_INSTALL: Final = "Setting up the tools your app needs"
_LBL_DATA_SETUP: Final = "Setting up where your app stores information"
_LBL_DATA_READY: Final = "Getting your app's data ready"
_LBL_CHECKS: Final = "Making sure everything fits together"
_LBL_TIDY: Final = "Tidying things up"
_LBL_PREVIEW: Final = "Getting your preview ready"
# The fail-closed fallback: an unrecognized command (`bash -c …`, `python -c …`, a novel CLI)
# degrades to this — the raw argv is DROPPED, never rendered. The open sandbox runs arbitrary
# commands, so a recognized-only allowlist that leaked argv on the long tail is the bug we refuse.
_LBL_FALLBACK: Final = "Working on your app"

# Friendly file-area copy (`_friendly_area`): the citizen sees an app AREA, never a filename.
_AREA_MAIN_PAGE: Final = "your app's main page"
_AREA_LAYOUT: Final = "your app's overall look"
_AREA_API: Final = "how your app saves and loads information"
_AREA_STYLING: Final = "your app's styling"
_AREA_DATA: Final = "where your app stores information"
_AREA_GENERIC: Final = "a part of your app"

_FILE_MUTATORS: Final = frozenset({"write_file", "edit_file", "insert_lines"})


class StepDetail(CamelModel):
    """The Details expander's raw (stored, already-redacted) material for one step."""

    args: str | None = None
    result: str | None = None


class UserTextItem(CamelModel):
    type: Literal["user_text"] = "user_text"
    seq: int
    mode: str
    text: str
    # Attachment reference ids found in the prompt content — the UI renders chips; the bytes
    # never travel on this read.
    attachment_ids: list[str] = Field(default_factory=list)


class AssistantTextItem(CamelModel):
    type: Literal["assistant_text"] = "assistant_text"
    seq: int
    mode: str
    text: str


class StepItem(CamelModel):
    """One friendly agent step. `hidden` steps (reads) render only inside an expanded view;
    `state` is derived from the stored return: ok / failed (a retry-refusal came back) /
    pending (no return recorded — in flight, or lost to a crash; the surrounding banner or
    in-progress anchor says which)."""

    type: Literal["step"] = "step"
    seq: int
    mode: str
    tool: str
    label: str
    state: Literal["ok", "failed", "pending"]
    hidden: bool
    detail: StepDetail


class BannerItem(CamelModel):
    """A build lifecycle banner, from a `build_outcome` system row. `text` is the stored
    outcome sentence (the same prose the model replays), so live and reload can never
    disagree with the record."""

    type: Literal["banner"] = "banner"
    seq: int
    mode: str
    banner: Literal["completed", "failed", "stopped", "quota"]
    text: str
    preview_url: str | None = None
    session_id: str


class BuildInProgressItem(CamelModel):
    """A build began here and no outcome closed it — mid-build (live) or lost to a crash.
    U10's `active_turn` disambiguates; this item only states the durable truth."""

    type: Literal["build_in_progress"] = "build_in_progress"
    seq: int
    mode: str
    session_id: str


class PlanOptionsItem(CamelModel):
    """The Build it / Keep refining card (U11 consumes this). State derives from the stored
    tool RETURN: absent or unresolved → pending; `refine` / `build` / `build_failed:<reason>`
    are U11's three stored resolutions. `build_failed` re-arms the card (never
    resolved-with-no-build)."""

    type: Literal["plan_options"] = "plan_options"
    seq: int
    mode: str
    tool_call_id: str
    state: Literal["pending", "refine", "build", "build_failed"]
    reason: str | None = None


DisplayItem = (
    UserTextItem
    | AssistantTextItem
    | StepItem
    | BannerItem
    | BuildInProgressItem
    | PlanOptionsItem
)


def _clip(value: str) -> str:
    if len(value) <= _DETAIL_CAP_CHARS:
        return value
    return value[:_DETAIL_CAP_CHARS] + _TRUNCATION_MARK


def _stringify(value: Any) -> str:
    """A stored args/content value as display text. Values are already redacted; this only
    flattens shape (dict args vs JSON-string args vs structured retry content)."""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):  # fmt: skip  # ruff py314 strips parens
        return str(value)


def _args_dict(raw: Any) -> dict[str, Any]:
    """Tool args as a dict for classification. pydantic-ai stores args either as a dict or a
    JSON string (provider-dependent); anything unparseable classifies as empty rather than
    raising — the label falls back, the Details expander still shows the raw string."""
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        try:
            parsed = json.loads(raw)
        except ValueError:
            return {}
        return parsed if isinstance(parsed, dict) else {}
    return {}


def _command_argv(args: dict[str, Any]) -> list[str]:
    command = args.get("command")
    if isinstance(command, list):
        return [item for item in command if isinstance(item, str)]
    return []


def _classify_command(argv: list[str]) -> tuple[str, bool]:
    """`run_command` argv → (friendly label, hidden). The pinned F3/U3 mapping. The RAW command is
    NEVER part of the label: an unrecognized command fails CLOSED to `_LBL_FALLBACK` (dropping the
    argv entirely), because the open sandbox runs arbitrary commands and a recognized-only
    allowlist that fell open would leak raw shell (`bash -c …`, `python -c …`) on the long tail."""
    if not argv:
        return (_LBL_FALLBACK, False)
    head = argv[0]
    rest = argv[1:]
    joined = " ".join(argv)
    if head in _PACKAGE_MANAGERS and any(sub in _INSTALL_SUBCOMMANDS for sub in rest[:2]):
        return (_LBL_INSTALL, False)
    if "db:migrate" in joined or "db-migrate" in joined or "drizzle-kit migrate" in joined:
        return (_LBL_DATA_READY, False)
    if "drizzle-kit" in joined or "db:generate" in joined:
        return (_LBL_DATA_SETUP, False)
    if "tsc" in argv or ("run" in rest[:1] and "build" in rest) or "next build" in joined:
        return (_LBL_CHECKS, False)
    if "lint" in joined or "eslint" in joined or "prettier" in joined:
        return (_LBL_TIDY, False)
    if ("run" in rest[:1] and "dev" in rest) or "next dev" in joined:
        return (_LBL_PREVIEW, False)
    if head in _READ_ONLY_BINARIES:
        return ("Inspected the app's files", True)
    if head in _HOUSEKEEPING_BINARIES:
        return ("Organized the app's files", True)
    return (_LBL_FALLBACK, False)


def _friendly_area(path: str) -> tuple[str, bool]:
    """A workspace-relative file path → (friendly area, hidden). NEVER returns the raw path — the
    citizen sees an app AREA ("your app's main page"), never a filename. Config/settings files are
    hidden as noise (they are plumbing, not a part of the app the user reasons about)."""
    clean = path.strip().lstrip("./")
    base = clean.rsplit("/", 1)[-1]
    lower = clean.lower()
    # App settings / config — plumbing the citizen never reasons about.
    if base in ("package.json", "tsconfig.json") or ".config." in base:
        return (_AREA_GENERIC, True)
    # Styling.
    if lower.endswith(".css"):
        return (_AREA_STYLING, False)
    # The data layer — folded into the "setting up your app's data" narrative.
    if clean == "db/schema.ts" or clean.startswith("drizzle/"):
        return (_AREA_DATA, False)
    # The Next.js App Router surface.
    if clean.startswith("app/"):
        segments = clean[len("app/") :].split("/")
        if segments[0] == "api":
            return (_AREA_API, False)
        if segments == ["page.tsx"]:
            return (_AREA_MAIN_PAGE, False)
        if segments == ["layout.tsx"]:
            return (_AREA_LAYOUT, False)
        if len(segments) >= 2 and segments[-1].startswith("page."):
            return (f"the {segments[-2]} page", False)
        return (_AREA_GENERIC, False)
    # Reusable UI pieces.
    if clean.startswith("components/"):
        return (f"the {base.rsplit('.', 1)[0]} part of the screen", False)
    return (_AREA_GENERIC, False)


def _file_step_label(tool_name: str, path: str | None) -> tuple[str, bool]:
    """(label, hidden) for a file tool — the friendly AREA, never the raw path.
    `write_file` reads as *Building*, edits as *Updating*, a read as *Looking at*; the state
    glyph carries done-ness.

    READS COME THROUGH HERE TOO (U16). The read arm used to build its own label as
    `f"Read {path}"`, which contradicted three invariants stated in this module's own comments —
    including `_friendly_area`, which exists precisely so a citizen sees an app AREA and never a
    filename — and it reached BOTH feeds, live and reload. Routing it through the same helper the
    writes use is what makes that structurally impossible to reintroduce on one side only."""
    area, hidden = _friendly_area(path) if path else (_AREA_GENERIC, False)
    verb = {"write_file": "Building", "read_file": "Looking at"}.get(tool_name, "Updating")
    return (f"{verb} {area}", hidden)


def _step_label(tool_name: str, args: dict[str, Any]) -> tuple[str, bool]:
    """(label, hidden) for one tool call — the data-driven friendly mapping (U6/U15/F3)."""
    path = args.get("path") if isinstance(args.get("path"), str) else None
    if tool_name in _FILE_MUTATORS:
        return _file_step_label(tool_name, path)
    if tool_name == "read_file":
        # Hidden regardless of the area's own noise verdict: a read is inspection, and the whole
        # class stays out of the visible feed (F3/U3). The LABEL still has to be clean, because
        # an expanded Details view renders it.
        label, _ = _file_step_label(tool_name, path)
        return (label, True)
    if tool_name in ("list_files", "search_files"):  # fmt: skip
        return ("Looked through the app's files", True)
    if tool_name == "declare_done":
        return ("Wrapping up the build", False)
    if tool_name == "run_command":
        return _classify_command(_command_argv(args))
    return (f"Used {tool_name}", False)


def classify_command(argv: list[str]) -> tuple[str, bool]:
    """Public entry to the run_command classifier — the LIVE emitter (`orchestrator/tools.py`)
    shares this exact logic with the reload projection. The shared contract is the friendly BASE
    label + the `hidden` flag + the step state: neither feed ever shows raw shell/argv, both hide
    the same read-only/housekeeping steps, and a given command classifies identically on both. The
    ONE intentional live-only affordance is a short human SUFFIX the live emitter appends to a
    blocked/failed step (`… — blocked to protect your data`, `… — couldn't finish`); on reload the
    same reason rides the step's Details expander instead (the state, failed, matches either way).
    So parity is 'same friendly item, no raw shell', not byte-identical labels on a failure."""
    return _classify_command(argv)


def command_needs_the_long_timeout(argv: list[str]) -> bool:
    """Is this a command that LEGITIMATELY runs for minutes (F4)?

    Reuses the same `_classify_command` mapping the labels come from, rather than growing a
    second classifier that could disagree with the first about what a command is.

    Only the install and type-check/build classes qualify. A cold-base `npm install` routinely
    burns the full long bound, and `next build` can too — killing either at the short bound would
    fail healthy builds. Everything else gets the short one, which is the point: the observed
    wedge was a `drizzle-kit generate` blocking on an interactive prompt for 4m09s, and it is
    DELIBERATELY not in this set. A migration generate should take seconds; if it has not
    finished in minutes it is waiting for a terminal that does not exist, and the fastest honest
    thing to do is kill it and tell the model."""
    label, _ = _classify_command(argv)
    return label in {_LBL_INSTALL, _LBL_CHECKS}


def classify_file_step(tool_name: str, path: str | None) -> tuple[str, bool]:
    """Public entry to the file-tool friendly-area mapping — shared by the live emitter and the
    reload projection (one translator, one source of truth)."""
    return _file_step_label(tool_name, path)


def classify_tool_call(tool_name: str, args_json: str) -> tuple[str, bool]:
    """(friendly label, hidden) for a LIVE tool call, from the wire args JSON — the same
    `_step_label` mapping the reload projection uses, so live and reload can never drift
    (U10's engine is the consumer). Unparseable args degrade to the argless label."""
    try:
        parsed = json.loads(args_json) if args_json else {}
    except ValueError:
        parsed = {}
    return _step_label(tool_name, parsed if isinstance(parsed, dict) else {})


def step_detail(args: str | None, result: str | None) -> StepDetail:
    """A Details-expander block from already-redacted stored/live values, clipped to the
    same cap the reload projection applies."""
    return StepDetail(
        args=_clip(args) if args else None,
        result=_clip(result) if result else None,
    )


def _is_attachment_fence(text: str) -> bool:
    """A client-built `<attachment …>…</attachment>` content block (U7): DATA riding in the
    prompt, not prose. The bubble must show what the user TYPED — a 200 KB inlined CSV in the
    bubble would bury it (chips represent attachments; full fence UX is U15's)."""
    stripped = text.strip()
    return stripped.startswith("<attachment ") and stripped.endswith("</attachment>")


def _user_text_and_refs(content: Any) -> tuple[str, list[str]]:
    """A stored user-prompt content value → (typed prose, attachment reference ids).
    Attachment fence blocks are excluded from the prose (they are attachment CONTENT — the
    U7 wire shape carries them as their own string items, typed prose last)."""
    if isinstance(content, str):
        return (content, [])
    texts: list[str] = []
    refs: list[str] = []
    if isinstance(content, list):
        for item in content:
            if isinstance(item, str):
                if not _is_attachment_fence(item):
                    texts.append(item)
            elif isinstance(item, dict) and item.get("kind") == ATTACHMENT_REF_KIND:
                attachment_id = item.get("attachment_id")
                if isinstance(attachment_id, str):
                    refs.append(attachment_id)
    return ("\n".join(texts), refs)


def _index_tool_results(rows: Sequence[Message]) -> dict[str, tuple[str, bool, int]]:
    """tool_call_id → (stored result text, was_retry, the ROW SEQ it came from). Returns ride
    in requests (the NEXT step's row for BRAIN, the same batch for a whole-turn row) — one flat
    index covers both. The seq rides along so `project_rows` can merge newest-wins."""
    results: dict[str, tuple[str, bool, int]] = {}
    for row in rows:
        for message in row.payload:
            if not isinstance(message, dict) or message.get("kind") != "request":
                continue
            for part in message.get("parts", []):
                if not isinstance(part, dict):
                    continue
                part_kind = part.get("part_kind")
                call_id = part.get("tool_call_id")
                if not isinstance(call_id, str):
                    continue
                if part_kind == "tool-return":
                    results[call_id] = (_stringify(part.get("content")), False, row.seq)
                elif part_kind == "retry-prompt":
                    results[call_id] = (_stringify(part.get("content")), True, row.seq)
    return results


def _closed_sessions(rows: Sequence[Message]) -> set[str]:
    """Session ids that have a recorded `build_outcome` row."""
    closed: set[str] = set()
    for row in rows:
        if (
            row.entry_kind is MessageEntryKind.SYSTEM_EVENT
            and isinstance(row.meta, dict)
            and row.meta.get("kind") == "build_outcome"
            and isinstance(row.meta.get("sessionId"), str)
        ):
            closed.add(row.meta["sessionId"])
    return closed


def _synthetic_resolutions(rows: Sequence[Message]) -> dict[str, tuple[str, int]]:
    """toolCallId → (stored choice, the ROW SEQ it was recorded at), for SYNTHESIZED
    plan-options cards (U11's retry-cap fallback): no real tool call exists, so both the
    pending card and its resolution live as system rows and never touch the wire history.
    The seq rides along so the merge in `project_rows` can order by recency."""
    resolutions: dict[str, tuple[str, int]] = {}
    for row in rows:
        if (
            row.entry_kind is MessageEntryKind.SYSTEM_EVENT
            and isinstance(row.meta, dict)
            and row.meta.get("kind") == "plan_options_resolved"
            and isinstance(row.meta.get("toolCallId"), str)
        ):
            resolutions[row.meta["toolCallId"]] = (str(row.meta.get("choice", "")), row.seq)
    return resolutions


def _first_step_rows(rows: Sequence[Message]) -> set[int]:
    """seqs of each build session's FIRST step row — the only step row whose user prompt is
    the user's own instruction (see the module docstring)."""
    seen: set[str] = set()
    first: set[int] = set()
    for row in rows:
        if row.entry_kind is not MessageEntryKind.STEP:
            continue
        session_id = row.meta.get("sessionId") if isinstance(row.meta, dict) else None
        key = session_id if isinstance(session_id, str) else f"unkeyed-{row.seq}"
        if key not in seen:
            seen.add(key)
            first.add(row.seq)
    return first


def _banner_kind(meta: dict[str, Any]) -> Literal["completed", "failed", "stopped", "quota"]:
    if meta.get("status") == "failed":
        return "failed"
    reason = meta.get("reason")
    if reason == "quota_exceeded":
        return "quota"
    if reason in ("stopped_by_user", "force_ended", "idle_teardown"):  # fmt: skip
        return "stopped"
    return "completed"


def _payload_text(payload: list[Any]) -> str:
    """Every text part's content across a row's payload, joined — the outcome rows store
    their user-facing sentence this way."""
    texts: list[str] = []
    for message in payload:
        if not isinstance(message, dict):
            continue
        for part in message.get("parts", []):
            if isinstance(part, dict) and part.get("part_kind") == "text":
                content = part.get("content")
                if isinstance(content, str) and content:
                    texts.append(content)
    return "\n".join(texts)


def _plan_options_state(
    stored: tuple[str, bool] | None,
) -> tuple[Literal["pending", "refine", "build", "build_failed"], str | None]:
    """U11's three stored resolutions (+ pending). Anything unrecognized — including the U8
    stub's wait-for-choice ack — reads as pending: the card must re-render actionable rather
    than invent a resolution that was never stored."""
    if stored is None:
        return ("pending", None)
    content, was_retry = stored
    if was_retry:
        return ("pending", None)
    if content == "build":
        return ("build", None)
    if content == "refine":
        return ("refine", None)
    if content.startswith("build_failed"):
        _, _, reason = content.partition(":")
        return ("build_failed", reason or None)
    return ("pending", None)


def _project_response_parts(
    row: Message,
    message: dict[str, Any],
    results: dict[str, tuple[str, bool]],
    items: list[DisplayItem],
) -> None:
    """One stored ModelResponse → assistant text + friendly steps + plan-options cards, in
    part order (text streamed before a tool call renders before it, matching the live feed)."""
    mode = row.mode.value
    for part in message.get("parts", []):
        if not isinstance(part, dict):
            continue
        part_kind = part.get("part_kind")
        if part_kind == "text":
            content = part.get("content")
            if isinstance(content, str) and content.strip():
                items.append(AssistantTextItem(seq=row.seq, mode=mode, text=content))
        elif part_kind == "tool-call":
            tool_name = part.get("tool_name")
            if not isinstance(tool_name, str):
                continue
            call_id = part.get("tool_call_id")
            stored = results.get(call_id) if isinstance(call_id, str) else None
            if tool_name == PLAN_OPTIONS_TOOL:
                options_state, reason = _plan_options_state(stored)
                items.append(
                    PlanOptionsItem(
                        seq=row.seq,
                        mode=mode,
                        tool_call_id=call_id if isinstance(call_id, str) else "",
                        state=options_state,
                        reason=reason,
                    )
                )
                continue
            args = _args_dict(part.get("args"))
            label, hidden = _step_label(tool_name, args)
            raw_args = part.get("args")
            state: Literal["ok", "failed", "pending"]
            if stored is None:
                state = "pending"
            else:
                state = "failed" if stored[1] else "ok"
            items.append(
                StepItem(
                    seq=row.seq,
                    mode=mode,
                    tool=tool_name,
                    label=label,
                    state=state,
                    hidden=hidden,
                    detail=StepDetail(
                        args=_clip(_stringify(raw_args)) if raw_args is not None else None,
                        result=_clip(stored[0]) if stored is not None else None,
                    ),
                )
            )
        # thinking / builtin-tool / file / compaction parts render nothing (reasoning and
        # provider internals are not chat content); unknown kinds are skipped, not raised —
        # a NEWER writer's part must degrade to invisible, never break every reload.


def project_rows(rows: Sequence[Message]) -> list[DisplayItem]:
    """The one history→display derivation. `rows` must be the `include_hidden=True` read —
    hidden rows render nothing directly, but unclosed `build_started` markers derive the
    in-progress anchor."""
    closed = _closed_sessions(rows)
    first_steps = _first_step_rows(rows)
    synthetic = _synthetic_resolutions(rows)
    # One call id can be answered TWICE — a system overlay (U12's build_failed record; a real
    # card's failure is never a ToolReturnPart) and a payload return, in either order. The
    # merge is by ROW SEQ, newest wins, matching `plan_options._scan`'s rule: merging by
    # SOURCE meant a refine return that landed FIRST still overwrote the build-failure overlay
    # recorded after it, and the user's card silently lost the failure it was meant to show.
    merged: dict[str, tuple[str, bool, int]] = {
        answered: (choice, False, seq) for answered, (choice, seq) in synthetic.items()
    }
    for answered, entry in _index_tool_results(rows).items():
        existing = merged.get(answered)
        if existing is None or entry[2] >= existing[2]:
            merged[answered] = entry
    results = {answered: (text, was_retry) for answered, (text, was_retry, _) in merged.items()}
    items: list[DisplayItem] = []

    for row in rows:
        if row.entry_kind is MessageEntryKind.MODE_SWITCH:
            continue

        if row.entry_kind is MessageEntryKind.SYSTEM_EVENT:
            meta = row.meta if isinstance(row.meta, dict) else {}
            kind = meta.get("kind")
            session_id = meta.get("sessionId")
            if kind == "build_started":
                if isinstance(session_id, str) and session_id not in closed:
                    items.append(
                        BuildInProgressItem(
                            seq=row.seq, mode=row.mode.value, session_id=session_id
                        )
                    )
                continue
            if kind == "plan_options_pending" and meta.get("synthesized"):
                # The retry-cap fallback card (U11): hidden row, visible card — its state
                # derives from the companion `plan_options_resolved` record.
                call_id = meta.get("toolCallId")
                if isinstance(call_id, str):
                    recorded = synthetic.get(call_id)
                    options_state, reason = _plan_options_state(
                        (recorded[0], False) if recorded is not None else None
                    )
                    items.append(
                        PlanOptionsItem(
                            seq=row.seq,
                            mode=row.mode.value,
                            tool_call_id=call_id,
                            state=options_state,
                            reason=reason,
                        )
                    )
                continue
            if kind == "plan_options_resolved":
                continue  # the companion record renders through its pending card
            if row.visibility is MessageVisibility.HIDDEN:
                continue  # hidden system rows render nothing
            if kind == "build_outcome" and isinstance(session_id, str):
                preview = meta.get("previewUrl")
                items.append(
                    BannerItem(
                        seq=row.seq,
                        mode=row.mode.value,
                        banner=_banner_kind(meta),
                        text=_payload_text(row.payload) or "Build finished.",
                        preview_url=preview if isinstance(preview, str) else None,
                        session_id=session_id,
                    )
                )
                continue
            # A visible system row of an unknown kind: render its text truthfully (a future
            # lifecycle entry should degrade to prose, not vanish).
            text = _payload_text(row.payload)
            if text:
                items.append(AssistantTextItem(seq=row.seq, mode=row.mode.value, text=text))
            continue

        if row.visibility is MessageVisibility.HIDDEN:
            continue

        render_user_prompts = row.entry_kind is not MessageEntryKind.STEP or row.seq in first_steps
        for message in row.payload:
            if not isinstance(message, dict):
                continue
            if message.get("kind") == "request":
                if not render_user_prompts:
                    continue
                for part in message.get("parts", []):
                    if isinstance(part, dict) and part.get("part_kind") == "user-prompt":
                        text, refs = _user_text_and_refs(part.get("content"))
                        if text or refs:
                            items.append(
                                UserTextItem(
                                    seq=row.seq,
                                    mode=row.mode.value,
                                    text=text,
                                    attachment_ids=refs,
                                )
                            )
            elif message.get("kind") == "response":
                _project_response_parts(row, message, results, items)

    return items
