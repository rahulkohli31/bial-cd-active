"""Stored native history → friendly display items.

ONE derivation, two consumers: the read API (reload) and the turn engine's catch-up
snapshot (live). Reads raw ROWS, not validated dataclasses: validating would coerce a
stored attachment-ref marker to `CachePoint` (the pinned 2.5.0 hazard) and force
rehydration this read must never pay for; `payload`/`meta` are already redacted at the
persistence seam, so the Details expander must not re-redact.

Hidden rows are excluded from RENDERING but still inform derived state: an unclosed
`build_started` marker with no later same-session `build_outcome` projects the
crashed/mid-build anchor. Inside a build session, only the FIRST step row's user prompt
renders as a bubble — later ones are the harness's own repair/continue nudges."""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from typing import Any, Final, Literal

import sqlalchemy as sa
from pydantic import Field
from sqlalchemy.ext.asyncio import AsyncSession

from src.core.prompt_blocks import APPLY_SCHEMA_CHANGE_TOOL, ATTACHMENT_READ_TOOL
from src.db.models.attachment import Attachment
from src.db.models.message import Message, MessageEntryKind, MessageVisibility
from src.schemas import CamelModel
from src.services.media import chip_kind_for
from src.services.messages.store import ATTACHMENT_FILE_REF_KIND, ATTACHMENT_REF_KIND

# The Plan chat's options tool. The projection derives the card's resolution state from this
# tool's stored call/return pair.
PLAN_OPTIONS_TOOL: Final = "present_plan_options"

TELL_THE_USER_TOOL: Final = "tell_the_user"
"""The mid-work voice channel's wire name. Named HERE, beside the parser both
emitters call, for the same reason `PLAN_OPTIONS_TOOL` is: the live emitter and this one must
agree on the spelling or a spoken line renders on one side and not the other."""

PROPOSE_SLICE_TOOL: Final = "propose_first_slice"
"""The scope-negotiation tool's wire name. Named here for the same reason the other two are."""

PLATFORM_TEXT_KIND: Final = "platform_text"
"""`meta.kind` of a row whose sentence is the PLATFORM's, not the model's.

WHAT IT BUYS is the record AND the model's silence, and the second half is the one that took
work. The row is a `SYSTEM_EVENT`, so nothing downstream — a reader, an operator, an audit —
has to infer authorship from a sentence that looks exactly like a reply; and it carries NO
payload, so `load_history` — which flattens every row's messages regardless of kind or
visibility — has nothing of it to hand back to the model as words the model wrote. The
sentence lives in `meta.text` and the arm below renders it from there, which is why taking it
out of the payload changes nothing the citizen sees.

IT IS ALSO THE MARKER THE TURN-TERMINAL ROW ADOPTS. One rendering of platform speech and not
two, because two homes would show the citizen the same sentence twice."""

TURN_TERMINAL_KIND: Final = "turn_terminal"
"""`meta.kind` of the durable turn-terminal row. Named here, beside the arm that reads it, and
imported by the engine that writes it — one spelling, because a writer and a reader that each
hold their own string literal are one typo away from a row nobody projects."""

# THERE IS NO DETAILS-EXPANDER CAP HERE ANY MORE, because there is no expander material to
# cap. A step used to carry the raw arguments and the raw result of its tool call, redacted and
# clipped to four thousand characters, on every frame and every reload item INCLUDING hidden
# steps — parsed by the browser and rendered nowhere. The friendly label is safe by
# construction (`_classify_command` fails closed and never puts raw argv on screen); the
# arguments beside it were not, and "it is not rendered" is a property of today's client, not
# of the wire. Redaction that lives at the draw site is a promise; redaction that lives here is
# a fact — the field is gone, so no future expander can reach it. See `test_projection.py`'s
# field-set guard, which is where the guarantee actually lives.

# Read-only commands render as VISIBLE inspection steps — same rule as the structured read
# tools. Hiding them leaves a build's activity opening on a write with no account of what the
# agent looked at to get there; looking at the app before changing it is work the citizen
# recognises.
_READ_ONLY_BINARIES: Final = frozenset({"ls", "cat", "head", "tail", "grep", "sed", "find", "wc"})

# …EXCEPT WHEN THEY ARE ASKED TO WRITE. Two of the binaries above are read-only by default and
# destructive on a flag: `sed -i` rewrites the file in place and `find -delete` removes what it
# matched. Membership in the set is decided by argv[0], so without this both would draw the
# read class's line — "Inspected the app's files" — over a command that changed or deleted the
# citizen's work, and the change would appear nowhere else in the feed. That mislabel cost
# nothing while the read class was hidden; drawing the class makes it a false statement on
# screen, which is the opposite of what showing reads was for.
#
# KEYED BY BINARY, because the same letter means different things: `-i` is in-place for `sed`
# and case-insensitive for `grep`. One flag list shared across the set would send every
# `grep -i` to the fallback for no reason.
_FIND_ACTIONS_THAT_ACT: Final = frozenset({"-delete", "-exec", "-execdir", "-ok", "-okdir"})


def _sed_rewrites_in_place(argv: list[str]) -> bool:
    """Does this `sed` argv carry `-i`, in any of the spellings the flag actually takes?

    `-i`, `-i.bak` (the suffix rides the flag) and `-ni` (bundled with another short flag) are
    all in-place, so this reads the CLUSTER rather than matching a literal. `i` appears in no
    other short option `sed` takes, which is what makes reading the cluster safe."""
    for arg in argv[1:]:
        if arg == "--in-place" or arg.startswith("--in-place="):
            return True
        if arg.startswith("-") and not arg.startswith("--") and "i" in arg.split(".", 1)[0]:
            return True
    return False


def _reads_without_writing(argv: list[str]) -> bool:
    """Is this argv in the read-only class AND not one of its members asked to mutate?"""
    if not argv or argv[0] not in _READ_ONLY_BINARIES:
        return False
    if argv[0] == "sed":
        return not _sed_rewrites_in_place(argv)
    if argv[0] == "find":
        return not any(arg in _FIND_ACTIONS_THAT_ACT for arg in argv[1:])
    return True


# Housekeeping shell verbs — plumbing the citizen never needs to see, and one of the only two
# things `hidden` still marks (the other is a write to a configuration file). Drawing these
# prints a generic line that says nothing about the app; the model still gets the raw output,
# and the chat stays quiet.
_HOUSEKEEPING_BINARIES: Final = frozenset({"mkdir", "mv", "cp", "touch", "echo", "cd"})

_INSTALL_SUBCOMMANDS: Final = frozenset({"install", "i", "ci", "add"})
_PACKAGE_MANAGERS: Final = frozenset({"npm", "pnpm", "yarn", "bun"})

# Friendly command copy (a pinned starting set — tunable in the UI). The classifier NEVER
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

# WHAT A LONG OPERATION SAYS WHILE IT IS STILL RUNNING.
#
# EXTENDS the table above rather than adding a second one, and that is the whole design. Every
# label this module produces — the command classes, `_LBL_FALLBACK`, and the file-area labels
# from `_friendly_area` — is already a present-participle phrase in the citizen's register
# ("Setting up the tools your app needs"), so "Still …" turns ANY of them into a truthful
# progress sentence. A label added tomorrow is narrated for free; a parallel table would be a
# second place to forget, and the first label anyone forgot would show a citizen raw argv.
_LONG_OPERATION_TAIL: Final = " — this one takes a little longer."
_STILL: Final = "Still "

# Friendly file-area copy (`_friendly_area`): the citizen sees an app AREA, never a filename.
_AREA_MAIN_PAGE: Final = "your app's main page"
_AREA_LAYOUT: Final = "your app's overall look"
_AREA_API: Final = "how your app saves and loads information"
_AREA_STYLING: Final = "your app's styling"
_AREA_DATA: Final = "where your app stores information"
_AREA_GENERIC: Final = "a part of your app"

_FILE_MUTATORS: Final = frozenset({"write_file", "edit_file", "insert_lines"})


class AttachmentRefItem(CamelModel):
    """One attachment on a citizen's turn, as the transcript needs to draw it.

    IDENTITY WAS NOT ENOUGH. This carried only the id, and a
    chip cannot be drawn from an id: the browser needs the filename to label it, the media type
    to decide whether pressing it previews or downloads, and the kind to pick the shape. So a
    reopened conversation showed no chips at all for any format, and the citizen could not see
    which files the model was still being charged for.

    `name` AND `media_type` MAY BE EMPTY, and that is a real state rather than a defect: the
    attachment row is deleted when a conversation is reclaimed, while the id stays in the
    message payload forever. Emitting the id with no name lets the browser draw its
    "attachment unavailable" chip, which is the honest answer. Dropping the entry instead would
    hide from the citizen that a file was ever there.
    """

    attachment_id: str
    # The chip vocabulary — `document` or `image` today. Derived from the media type by
    # `chip_kind_for`, the same function the upload response uses, so a chip rebuilt on reload
    # is the same shape as the one the citizen watched appear.
    kind: str = ""
    name: str = ""
    media_type: str = ""


class UserTextItem(CamelModel):
    type: Literal["user_text"] = "user_text"
    seq: int
    text: str
    # Attachments on this turn — the UI renders chips; the bytes never travel on this read.
    # Populated with ids by `project_rows` and filled out by `project_conversation`, which is
    # the entry point every route uses.
    attachments: list[AttachmentRefItem] = Field(default_factory=list)


class AssistantTextItem(CamelModel):
    type: Literal["assistant_text"] = "assistant_text"
    seq: int
    text: str


class StepItem(CamelModel):
    """One friendly agent step. `hidden` marks PLUMBING and nothing else now — a write to a
    configuration file, a housekeeping shell command — never a read (a read is what the agent
    looked at to get here, and it is drawn), and never a step that failed (a hidden failure
    makes the group's count name a row nobody can find). `state` is derived from the stored
    return: ok / failed (a retry-refusal came back) / pending (no return recorded — in flight,
    or lost to a crash; the surrounding banner or in-progress anchor says which)."""

    type: Literal["step"] = "step"
    seq: int
    tool: str
    label: str
    state: Literal["ok", "failed", "pending"]
    hidden: bool


class TurnTerminalItem(CamelModel):
    """One turn ended — the row a transcript rebuilds with no live stream to read.

    A STORED ROW, NOT THE LIVE FRAME: `TurnEndedFrame` says the same thing exactly once, to
    whoever happened to be subscribed, so a reload or a restart has no frame and cannot tell
    "this turn finished" from "this turn is still going" — the last thing in the transcript is a
    reply either way, and anything rendering a turn as a unit (a collapsible group, a spinner, a
    control that only makes sense while a turn runs) is then stuck on the wrong answer.

    `terminal` REUSES `_banner_kind`'s vocabulary rather than inventing a parallel one, so reload
    and live derive the same word from the same stored meta; two spellings of "the turn stopped"
    is how a client ends up with two states for one fact. `reason` TRAVELS BESIDE IT because
    `_banner_kind` reads status before reason, so every named graceful end collapses into
    `failed` here, and `failed` alone cannot say whether a citizen pressed Stop, spent their
    day's limit or had their workspace put back. Coarsening `terminal` was the deliberate trade
    (see `test_the_terminal_reads_through_the_banners_own_vocabulary`); carrying the reason
    beside it is the other side of it.

    THE REASON IS A MACHINE TOKEN AND NEVER PROSE — `stopped_by_user`, `quota_exceeded`,
    `workspace_restored`, `self_heal_budget_exhausted` — a key to look up, never a string to
    show. `None` for a turn that ended with no named reason, and that absence is meaningful too:
    it is what makes a client fall back to the neutral sentence for its `terminal` instead of
    naming a cause nobody recorded.

    ENDED-UNKNOWN IS THE ABSENCE OF THIS ITEM: there is no `unknown` member, because a turn
    killed by a restart writes no row at all — the process that would have written it is gone.
    A consumer that finds a turn's rows with no terminal among them knows the turn did not
    finish cleanly, which is a stronger signal than a value a future writer could forget to
    set."""

    type: Literal["turn_terminal"] = "turn_terminal"
    seq: int
    turn_id: str
    terminal: Literal["completed", "failed", "stopped", "quota"]
    reason: str | None = None


class BannerItem(CamelModel):
    """A build lifecycle banner, from a `build_outcome` system row. `text` is the stored
    outcome sentence (the same prose the model replays), so live and reload can never
    disagree with the record."""

    type: Literal["banner"] = "banner"
    seq: int
    banner: Literal["completed", "failed", "stopped", "quota"]
    text: str
    preview_url: str | None = None
    session_id: str


class BuildInProgressItem(CamelModel):
    """A build began here and no outcome closed it — mid-build (live) or lost to a crash.
    The catch-up snapshot's `active_turn` disambiguates; this item only states the durable truth.

    HISTORICAL ROWS ONLY. Its source, the hidden `build_started` marker, had exactly one writer
    (`outcome.write_build_started`, called from `SessionManager.start`), and that writer is deleted
    with the standalone build stack. No new `build_started` row can be created, so this item can
    only ever be derived from rows already in the database — which is precisely why it, and the
    three `{session_id}` routes the portal reattaches through, were kept. A build that runs as an
    ordinary Write chat turn records its ending as a `turn_terminal` row instead."""

    type: Literal["build_in_progress"] = "build_in_progress"
    seq: int
    session_id: str


class PlanOptionsItem(CamelModel):
    """The Build it / Keep refining card. State derives from the stored tool RETURN: absent or
    unresolved → pending; `refine` and `build` are the two a user can produce, and there is no
    third. `build_failed` used to be one, re-arming a card a failed press had burned — the
    handoff answers the offer after the turn has started, so a failure burns nothing and there
    is nothing to re-arm."""

    type: Literal["plan_options"] = "plan_options"
    seq: int
    tool_call_id: str
    state: Literal["pending", "refine", "build"]


DisplayItem = (
    UserTextItem
    | AssistantTextItem
    | StepItem
    | BannerItem
    | BuildInProgressItem
    | PlanOptionsItem
    | TurnTerminalItem
)


def _stringify(value: Any) -> str:
    """A stored tool-return value as a plain string, for COMPARISON — never for display.

    The card's resolution is stored as a tool return whose content has to be read back
    (`"build"` vs anything else). It only flattens shape: dict content vs JSON-string content vs
    a structured retry body."""
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError):  # fmt: skip  # ruff py314 strips parens
        return str(value)


def _args_dict(raw: Any) -> dict[str, Any]:
    """Tool args as a dict for classification. pydantic-ai stores args either as a dict or a
    JSON string (provider-dependent); anything unparseable classifies as empty rather than
    raising — the label falls back to its generic form, which is the whole of what a step says
    now."""
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
    """`run_command` argv → (friendly label, hidden). The pinned mapping. The RAW command is
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
    if _reads_without_writing(argv):
        # The shell half of the read class, and visible with the rest of it. A member of that
        # class carrying a mutating flag falls through to the fallback below instead: the feed
        # says "Working on your app", which is true of a write, rather than claiming an
        # inspection that never happened.
        return ("Inspected the app's files", False)
    if head in _HOUSEKEEPING_BINARIES:
        # STILL HIDDEN, and this is now one of only two things `hidden` marks. `mkdir`, `mv`,
        # `touch` are plumbing between the steps that matter; drawing them prints a generic
        # line into the feed that tells the citizen nothing about their app. The other is a
        # write to a configuration file, in `_friendly_area`.
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

    READS COME THROUGH HERE TOO: a read arm building its own label (`f"Read {path}"`) shows a
    citizen a FILENAME, which is the one thing `_friendly_area` exists to prevent — and it
    reaches BOTH feeds, live and reload. Routing it through the same helper the writes use is
    what makes that structurally impossible to reintroduce on one side only."""
    area, hidden = _friendly_area(path) if path else (_AREA_GENERIC, False)
    verb = {"write_file": "Building", "read_file": "Looking at"}.get(tool_name, "Updating")
    return (f"{verb} {area}", hidden)


def _attachment_step_label(file: str | None) -> tuple[str, bool]:
    """(label, hidden) for a read of an attached file — and here the NAME is the friendly thing.

    ★ THE ONE PLACE THIS MODULE SHOWS A FILE NAME ON PURPOSE. `_friendly_area` exists
    because a citizen has no idea what `components/GateTable.tsx` is: that is the platform's own
    machinery, named by the agent. An attachment is the opposite in every respect — the citizen
    chose the file, named it, and is looking at a chip carrying that name a few inches up the
    screen. "Looking at part of your app" over their spreadsheet would be LESS informative than
    the raw name, and it would leave the transcript unable to say which of five attached files an
    answer came from.

    The name comes from the model's own argument, so it is the on-disk spelling rather than the
    display name — close enough to recognise, and this module has no database to resolve the
    other one from.
    """
    if not file:
        return ("Reading the attached file", False)
    return (f"Reading {file.rsplit('/', 1)[-1]}", False)


def _step_label(tool_name: str, args: dict[str, Any]) -> tuple[str, bool]:
    """(label, hidden) for one tool call — the data-driven friendly mapping."""
    path = args.get("path") if isinstance(args.get("path"), str) else None
    if tool_name == ATTACHMENT_READ_TOOL:
        file = args.get("file")
        return _attachment_step_label(file if isinstance(file, str) else None)
    if tool_name in _FILE_MUTATORS:
        return _file_step_label(tool_name, path)
    if tool_name == "read_file":
        # VISIBLE, and the area's own noise verdict is deliberately NOT consulted. A read is
        # work the citizen recognises — looking at their app before changing it — and the whole
        # class used to be hidden, which is why a build's activity opened on a write with no
        # account of what the agent had read to get there. Taking the area's verdict here would
        # let the configuration-file rule leak into the read class and hide a read for a reason
        # that is about WRITES being generic.
        label, _ = _file_step_label(tool_name, path)
        return (label, False)
    if tool_name in ("list_files", "search_files"):  # fmt: skip
        return ("Looked through the app's files", False)
    if tool_name == "fetch_output_slice":
        # Inspection, and visible with the rest of the read class. It needs a branch of its
        # own because the fallback below renders the RAW TOOL NAME ("Used fetch_output_slice")
        # into a citizen's feed, which is exactly the raw-machinery leak `_friendly_area` exists
        # to prevent — and that leak is the reason it cannot simply fall through now that it
        # is drawn.
        return ("Looked at what a command printed", False)
    if tool_name == APPLY_SCHEMA_CHANGE_TOOL:
        # The composite runs `drizzle-kit generate` then `npm run db:migrate`, so it lands on
        # the SAME friendly label the two raw commands already classified to — a citizen watching
        # a build must not be able to tell which spelling the agent reached for. Its own branch
        # rather than the fallback below, which renders the raw tool name ("Used
        # apply_schema_change") into the feed.
        return (_LBL_DATA_SETUP, False)
    if tool_name == "declare_done":
        return ("Wrapping up the build", False)
    if tool_name == "run_command":
        return _classify_command(_command_argv(args))
    return (f"Used {tool_name}", False)


def classify_command(argv: list[str]) -> tuple[str, bool]:
    """Public entry to the run_command classifier — the LIVE emitter (`orchestrator/tools.py`)
    shares this exact logic with the reload projection: same friendly BASE label + `hidden`
    flag + step state, neither feed ever shows raw shell/argv, and a command classifies
    identically on both. The ONE live-only affordance is a short human SUFFIX the live
    emitter appends to a blocked/failed step; on reload the same reason rides the step's
    Details expander instead. So parity is 'same friendly item, no raw shell', not
    byte-identical labels on a failure."""
    return _classify_command(argv)


def command_needs_the_long_timeout(argv: list[str]) -> bool:
    """Is this a command that LEGITIMATELY runs for minutes?

    Reuses `_classify_command`'s mapping so this can't disagree with the labels about what a
    command is. Only install and type-check/build qualify — a cold-base `npm install` routinely
    burns the full long bound and `next build` can too, so killing either at the short bound
    would fail healthy builds. Everything else gets the short one: the observed
    wedge was a `drizzle-kit generate` blocking on an interactive prompt for 4m09s, and it is
    DELIBERATELY excluded — a migration generate should take seconds, and past minutes the
    honest move is to kill it and tell the model."""
    label, _ = _classify_command(argv)
    return label in {_LBL_INSTALL, _LBL_CHECKS}


def command_only_inspects(argv: list[str]) -> bool:
    """Does this argv only LOOK at the workspace (`cat`, `sed -n`, `grep`, `ls`, `wc`)?

    Reads off the same predicate the classifier labels steps by — one answer to "is this an
    inspection," not two that can disagree. Consumed by the output formatter: an inspection's
    output IS file content and a filter must never silently drop a real line from it. Fails
    CLOSED for the long tail — an unrecognized binary is treated as a log (worst case a kept
    noise line, never a deleted real one); `sed -i` / `find -delete` are logs too, for the
    same reason they are not labelled inspections."""
    return _reads_without_writing(argv)


def long_operation_line(label: str) -> str:
    """A step's friendly label, restated for an operation that has outrun the stillness
    threshold — the harness's own words for "this is still running".

    FAILS CLOSED LIKE THE TABLE. The input is always a label this module already produced, so
    it is already free of argv and file paths; an empty one degrades to `_LBL_FALLBACK` rather
    than to nothing, because a blank status line is a still screen with extra steps.

    IDEMPOTENT ON
    PURPOSE — re-derived from the step's label each refresh and must come back byte-identical,
    or the portal's atomic live region (only re-announces on change) would re-read an
    unchanged line to a screen reader."""
    base = label.strip() or _LBL_FALLBACK
    if base.endswith(_LONG_OPERATION_TAIL):
        return base
    opener = base if base.startswith(_STILL) else f"{_STILL}{base[0].lower()}{base[1:]}"
    return f"{opener}{_LONG_OPERATION_TAIL}"


def classify_file_step(tool_name: str, path: str | None) -> tuple[str, bool]:
    """Public entry to the file-tool friendly-area mapping — shared by the live emitter and the
    reload projection (one translator, one source of truth)."""
    return _file_step_label(tool_name, path)


def classify_tool_call(tool_name: str, args_json: str) -> tuple[str, bool]:
    """(friendly label, hidden) for a LIVE tool call, from the wire args JSON — the same
    `_step_label` mapping the reload projection uses, so live and reload can never drift
    (the engine is the consumer). Unparseable args degrade to the argless label."""
    try:
        parsed = json.loads(args_json) if args_json else {}
    except ValueError:
        parsed = {}
    return _step_label(tool_name, parsed if isinstance(parsed, dict) else {})


def _is_attachment_fence(text: str) -> bool:
    """A client-built `<attachment …>…</attachment>` content block: DATA riding in the prompt,
    not prose. The bubble must show what the user TYPED — a 200 KB inlined CSV in the bubble
    would bury it, and chips are what represent attachments."""
    stripped = text.strip()
    return stripped.startswith("<attachment ") and stripped.endswith("</attachment>")


def _user_text_and_refs(content: Any) -> tuple[str, list[str]]:
    """A stored user-prompt content value → (typed prose, attachment reference ids).
    Attachment fence blocks are excluded from the prose (they are attachment CONTENT — the
    wire shape carries them as their own string items, typed prose last)."""
    if isinstance(content, str):
        return (content, [])
    texts: list[str] = []
    refs: list[str] = []
    if isinstance(content, list):
        for item in content:
            if isinstance(item, str):
                if not _is_attachment_fence(item):
                    texts.append(item)
            elif isinstance(item, dict) and item.get("kind") in (
                ATTACHMENT_REF_KIND,
                ATTACHMENT_FILE_REF_KIND,
            ):
                # BOTH KINDS. A code-lane file leaves the second marker because its bytes
                # never became a `BinaryContent` — and reading only the first is what made a
                # spreadsheet's chip vanish on reload while an image's survived, which is the R23a
                # regression this work exists to close, inverted for the new formats.
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
    """Session ids that have a recorded `build_outcome` row.

    Both halves of the pair it answers about are legacy now: `write_build_started` is deleted and
    `write_build_outcome` only still runs on the `stop` path of a session nothing can create. The
    two writers had to go or stay TOGETHER — deleting the start marker's writer alone would have
    left every legacy build rendering as permanently in progress, and deleting the outcome
    writer alone would have done the same to any build that did start."""
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
    plan-options cards (the retry-cap fallback): no real tool call exists, so both the
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
) -> Literal["pending", "refine", "build"]:
    """The three stored resolutions (+ pending). Anything unrecognized reads as pending: the
    card must re-render actionable rather than invent a resolution that was never stored."""
    if stored is None:
        return "pending"
    content, was_retry = stored
    if was_retry:
        return "pending"
    if content == "build":
        return "build"
    # ANYTHING ELSE READS AS SPENT, not as pending, and the distinction matters for exactly
    # one input: a `build_failed:<reason>` overlay written before that state was retired. Those
    # cards are resolved and their build did not happen; showing them as live would offer a
    # button with nothing behind it.
    return "refine"


def _plan_argument(args: Any) -> str | None:
    """The plan out of an offer call's stored arguments, or None when it carries none.

    Tolerant of both stored shapes — pydantic-ai persists a tool call's `args` as a JSON string
    or as an object depending on the provider — and of neither being parseable. A malformed
    argument is the same answer as a missing one here: there is no plan to show, and a
    projection that raised would take a whole transcript down over one row."""
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            return None
    if not isinstance(args, dict):
        return None
    plan = args.get("plan")
    if not isinstance(plan, str):
        return None
    return plan.strip() or None


def update_from_args(args: Any) -> str | None:
    """The words a `tell_the_user` call carries, or None when it carries none that may be shown.

    ★ THE SINGLE PLACE THE VOICE CHANNEL'S RULE LIVES — both emitters call this (live at
    `FunctionToolCallEvent`, reload at the call's stored part), so a call carrying nothing
    renders nothing on either path.

    THE CHARACTER CEILING IS GONE, from here and the tool body together. A number here decided
    how much of what the model had written a citizen was allowed to read, and a call one
    character over it vanished entirely — refused at the tool, dropped at the renderer, so the
    citizen got silence where the agent had spoken. Removing it from only one of the two would
    have been worse than leaving it: the model would be taught it may write at length while the
    renderer went on deleting it.

    Both `args` shapes go through `_args_dict`; a malformed one reads as missing."""
    parsed = _args_dict(args)
    update = parsed.get("update")
    if not isinstance(update, str):
        return None
    text = update.strip()
    return text or None


def _slice_argument(args: Any) -> dict[str, Any] | None:
    """A proposal call's arguments, or None when the call carries nothing renderable.

    ★ THE ONE PLACE THE PROPOSAL'S SHAPE IS DECIDED, read by three callers that must agree:
    the live emitter, this projection, and the engine. A call the tool body refuses renders
    nowhere here either. THE PIECE-COUNT CEILING IS GONE, from here and the tool body
    together — that was a taste judgement, not a rule. Still enforced: every piece in the
    first round must appear in the list of everything found. Both `args` shapes go through
    `_args_dict`; a malformed one reads as missing."""
    parsed = _args_dict(args)
    found = clean_pieces(parsed.get("found"))
    first = clean_pieces(parsed.get("first"))
    why = parsed.get("why")
    question = parsed.get("question")
    if not found or not first or not isinstance(why, str) or not isinstance(question, str):
        return None
    if not set(first) <= set(found):
        return None
    if not why.strip() or not question.strip():
        return None
    return {"found": found, "first": first, "why": why.strip(), "question": question.strip()}


def clean_pieces(raw: Any) -> list[str]:
    """A list-of-strings argument, emptied of anything that is not a usable piece name.

    PUBLIC BECAUSE THE TOOL BODY READS IT TOO — both sides must clean identically, or a piece
    named five times could be refused by one (raw list) and drawn by the other (cleaned) at
    the same moment.

    DE-DUPLICATED, ORDER PRESERVED: a piece named twice is one piece to the citizen, and order
    is the agent's, since it is the order the citizen agreed to."""
    if not isinstance(raw, list):
        return []
    return list(
        dict.fromkeys(item.strip() for item in raw if isinstance(item, str) and item.strip())
    )


def render_proposal(proposal: dict[str, Any]) -> str:
    """The proposal's message, built by the PLATFORM from the call's arguments.

    THE SHAPE IS THE RENDERER'S, NOT THE MODEL'S PROSE — the whole reason the proposal is a
    tool rather than an instruction: "lists everything, names the first slice, says what
    happens to the rest, asks one question" is true by construction, not just prompted for.
    THE "REST" SENTENCE IS CONDITIONAL — a slice covering everything found has no remainder,
    and promising to return to nothing would invent an outstanding item. EXACTLY ONE
    QUESTION, because the argument is singular."""
    # IMPORTED HERE, NOT AT MODULE SCOPE, and the cycle is real rather than theoretical:
    # `services/turns/__init__` re-exports the engine, which imports this module, so a
    # top-level `from src.services.turns.copy import ...` fails at import time with a partially
    # initialised projection. The sentences belong in `copy.py` regardless — that module's
    # jargon guard iterates over it, and a frame written anywhere else would be outside the one
    # check that exists for citizen-facing wording.
    from src.services.turns.copy import (
        PROPOSAL_EVERYTHING_LEAD,
        PROPOSAL_FIRST_LEAD,
        PROPOSAL_REST_TEXT,
    )

    lines = [PROPOSAL_EVERYTHING_LEAD, ""]
    lines += [f"- {piece}" for piece in proposal["found"]]
    lines += ["", PROPOSAL_FIRST_LEAD, ""]
    lines += [f"- {piece}" for piece in proposal["first"]]
    lines += ["", proposal["why"]]
    if len(proposal["first"]) < len(proposal["found"]):
        lines += ["", PROPOSAL_REST_TEXT]
    lines += ["", proposal["question"]]
    return "\n".join(lines)


def proposal_from_args(args: Any) -> str | None:
    """The rendered proposal for a stored call, or None when the call carries none."""
    proposal = _slice_argument(args)
    return None if proposal is None else render_proposal(proposal)


def finished_from_args(args: Any) -> str | None:
    """The piece a `tell_the_user` call marked finished, or None when it marked none.

    Separate from `update_from_args` because the two answer different questions and either can
    be present without the other: an update with no mark is the ordinary case, and a mark that
    arrives with nothing showable beside it — an empty or unparseable `update` — still happened.
    The piece IS finished, and losing that because the sentence alongside it could not be
    rendered would corrupt the closing account over a copy problem."""
    finished = _args_dict(args).get("finished")
    if not isinstance(finished, str):
        return None
    return finished.strip() or None


def agreed_slice(messages: Sequence[Any]) -> list[str]:
    """What the citizen last agreed to build first, read out of the conversation's own record.

    ★ LATEST WINS, and there is no stored linkage anywhere: the agreed list is the arguments of
    the most recent HONOURABLE `propose_first_slice` call in these messages. That is the same
    route the plan itself travels — the conversation's own rows — so re-proposing mid-build
    replaces the agreement without a column, a table, or anything that can go stale when a later
    build quietly delivers a deferred piece.

    ORDER-DEPENDENT ON PURPOSE — callers must pass messages oldest-first, and both do: the run's
    `message_history` and `load_rows`'s `ORDER BY seq` are the only two sources. Accepts
    serialized payload dicts OR pydantic-ai message objects, because the two callers hold
    different shapes of the same fact: the engine has live `ModelResponse`s, a reader over
    stored rows has payload dicts."""
    agreed: list[str] = []
    for message in messages:
        for part in _parts_of(message):
            if _part_field(part, "part_kind") != "tool-call":
                continue
            if _part_field(part, "tool_name") != PROPOSE_SLICE_TOOL:
                continue
            proposal = _slice_argument(_part_field(part, "args"))
            if proposal is not None:
                agreed = proposal["first"]
    return agreed


def finished_slice(messages: Sequence[Any]) -> set[str]:
    """Which pieces of the CURRENT agreement have already been marked finished, from the record.

    THE OTHER HALF OF `agreed_slice`: the agreement is re-derived every turn, so its marks must
    be too, or turn two would falsely report turn one's finished piece as still outstanding.
    A NEW PROPOSAL CLEARS THEM, mirroring the live rule — marks against a since-re-proposed
    slice are not evidence about the new one, the same reasoning `_already_marked_against`
    gives. Scoping by POSITION rather than by name matters because a piece can be named in both
    the old agreement and the new one. SCOPED TO WHAT WAS AGREED: a mark naming
    something outside the current agreement is ignored, or an invented name would make the
    remainder's honest arm unreachable."""
    agreed = agreed_slice(messages)
    if not agreed:
        return set()
    marked: set[str] = set()
    for message in messages:
        for part in _parts_of(message):
            tool_name = _part_field(part, "tool_name")
            if tool_name == PROPOSE_SLICE_TOOL:
                if _slice_argument(_part_field(part, "args")) is not None:
                    marked.clear()
                continue
            if tool_name != TELL_THE_USER_TOOL:
                continue
            piece = finished_from_args(_part_field(part, "args"))
            if piece is not None and piece in agreed:
                marked.add(piece)
    return marked


def _parts_of(message: Any) -> list[Any]:
    if isinstance(message, dict):
        return [p for p in message.get("parts", []) if isinstance(p, dict)]
    return list(getattr(message, "parts", []))


def _part_field(part: Any, name: str) -> Any:
    if isinstance(part, dict):
        return part.get(name)
    if name == "part_kind":
        return getattr(part, "part_kind", None)
    return getattr(part, name, None)


def _project_response_parts(
    row: Message,
    message: dict[str, Any],
    results: dict[str, tuple[str, bool]],
    items: list[DisplayItem],
) -> None:
    """One stored ModelResponse → assistant text + friendly steps + plan-options cards, in
    part order (text streamed before a tool call renders before it, matching the live feed)."""
    parts = [p for p in message.get("parts", []) if isinstance(p, dict)]
    # EVERY TEXT PART REACHES THE CITIZEN, WHATEVER SAT BESIDE IT.
    #
    # A response that also called a tool keeps its prose. Suppressing it here — on the rule
    # that text beside a tool call is the model narrating its way to the call — throws away the
    # explanation between the receipts and leaves a run of steps with nothing joining them,
    # which is the opposite of the voice this product has. This is a render-time filter and not
    # a gate on when the row was written, so it renders the prose in transcripts already on
    # disk too: persistence never dropped a word.
    #
    # `thinking` parts fall through this loop untouched, and that is the guarantee rather than
    # an omission: reasoning is stored so the next turn can replay it, and it is never
    # projected, never framed and never sent to the browser.
    for part in parts:
        part_kind = part.get("part_kind")
        if part_kind == "text":
            content = part.get("content")
            if isinstance(content, str) and content.strip():
                items.append(AssistantTextItem(seq=row.seq, text=content))
        elif part_kind == "tool-call":
            tool_name = part.get("tool_name")
            if not isinstance(tool_name, str):
                continue
            call_id = part.get("tool_call_id")
            stored = results.get(call_id) if isinstance(call_id, str) else None
            if tool_name == PLAN_OPTIONS_TOOL:
                # THE PLAN, THEN THE CARD BENEATH IT — read out of the call's own stored
                # `args`, which is the single authoritative copy. The live stream pushes the
                # same string from the same call at `FunctionToolCallEvent`, so a reloaded
                # transcript is byte-identical to what the citizen watched arrive; and because
                # both sides read one field rather than agreeing to keep two in step, they
                # cannot drift.
                #
                # NOT A SECOND STORED ROW, deliberately. Rendering the plan as its own durable
                # assistant message would put it in the conversation TWICE — once as the text
                # row and once inside the tool call the model actually made — and every
                # subsequent turn in that chat would carry both copies into the model's
                # context. A plan is long by design; paying for it twice on every turn, on a
                # platform whose token meter the citizen can see, is not a rounding error.
                #
                # A call with no plan in it renders no text and still renders its card: that is
                # every offer presented before the plan became the argument, and their cards
                # were resolved by revision 0035 rather than deleted, so a migrated transcript
                # reads exactly as it always did.
                plan = _plan_argument(part.get("args"))
                if plan:
                    items.append(AssistantTextItem(seq=row.seq, text=plan))
                items.append(
                    PlanOptionsItem(
                        seq=row.seq,
                        tool_call_id=call_id if isinstance(call_id, str) else "",
                        state=_plan_options_state(stored),
                    )
                )
                continue
            if tool_name == PROPOSE_SLICE_TOOL:
                # THE PROPOSAL, RENDERED FROM ITS ARGUMENTS at the position the call occupies —
                # the same shape as the voice channel and the offer, and for the same reason:
                # one stored field, two emitters, no agreement to keep in step.
                #
                # NOT A STEP. Above `_step_label` like the other two, so the transcript shows
                # the proposal and never `Used propose_first_slice`.
                proposed = proposal_from_args(part.get("args"))
                if proposed:
                    items.append(AssistantTextItem(seq=row.seq, text=proposed))
                continue
            if tool_name == TELL_THE_USER_TOOL:
                # THE WORDS, AT THE POSITION THE CALL OCCUPIES — the `present_plan_options`
                # shape, and the reason live order and reload order are the same order.
                #
                # AND IT IS NOT A STEP. Handled above `_step_label`, exactly as the offer is,
                # so the transcript shows what was said and never a row announcing that the
                # agent decided to say it — and never `Used tell_the_user`, which is what the
                # label fallback would have printed into a citizen's feed.
                #
                # A refused call renders nothing here because `update_from_args` returns None
                # for the same argument the tool body refused. No second copy of the rule, and
                # no consulting the stored RESULT: a turn cut short before the tool return
                # landed still said the words, and the citizen saw them.
                spoken = update_from_args(part.get("args"))
                if spoken:
                    items.append(AssistantTextItem(seq=row.seq, text=spoken))
                continue
            args = _args_dict(part.get("args"))
            label, hidden = _step_label(tool_name, args)
            state: Literal["ok", "failed", "pending"]
            if stored is None:
                state = "pending"
            else:
                state = "failed" if stored[1] else "ok"
            items.append(
                StepItem(
                    seq=row.seq,
                    tool=tool_name,
                    label=label,
                    # NOTHING IS HIDDEN WHEN SOMETHING WENT WRONG, whatever class it belongs
                    # to. The group opens itself saying one thing went wrong and then counts
                    # the rows the citizen can see; a hidden failure makes that count name a
                    # row nobody can find. A housekeeping command that failed is exactly the
                    # case — plumbing while it works, and the whole story the moment it does
                    # not. Mirrored live in `engine._resolve_step`.
                    hidden=hidden and state != "failed",
                    state=state,
                )
            )
        # thinking / builtin-tool / file / compaction parts render nothing (reasoning and
        # provider internals are not chat content); unknown kinds are skipped, not raised —
        # a NEWER writer's part must degrade to invisible, never break every reload.


def measured_context_tokens(rows: Sequence[Message]) -> int | None:
    """How full this conversation is, off the stored rows — or None when nobody has measured it.

    ★ THE TWIN OF `usage.context_window.enforce_context_limit`'s occupancy, and they must stay
    twins: this is the number the browser's meter shows and that one is the number the server
    refuses on. Same rule, two inputs — the check reads a validated `list[ModelMessage]` the
    route already loaded, and this reads the RAW JSONB, because that is what this module is
    allowed to touch (validating here would coerce a stored attachment marker into a
    `CachePoint` and demand a rehydration a read must never pay for — see the module docstring).
    `tests/services/messages/test_context_measure.py` pins the two against each other.

    ★ RAW `input_tokens`, AND NEVER `billable_spend` / `weighted_spend`. Under pydantic-ai
    `input_tokens` is ALREADY INCLUSIVE of both cache classes, which is exactly the occupancy
    wanted; the weighted helpers are COST figures that discount a cache read to a tenth because
    that is what it costs. A cached token still occupies the window, byte for byte. Real
    conversations here run 97-99% cache-read, so a spend-shaped number reads a 190,000-token
    chat as a 23,500-token one. This codebase has shipped that confusion three times, twice past
    review; verify any change by RUNNING `RequestUsage.extract` on a cache-heavy payload, never
    by reading a docstring — reading is what reinforced the wrong belief twice.

    ★ THE LARGEST, NOT THE LAST. Rows the PLATFORM wrote carry a `RequestUsage` whose every
    field is zero, because no provider ever served them; so does the hidden per-turn terminal
    row, which carries no payload at all. "The last response" reads one of those zeros and
    hands a full conversation back as an empty one — the under-count that lets an over-long
    chat past the meter. A maximum cannot be fooled that way, and a prompt only grows, so the
    largest measurement is also the most recent real one. A zero is therefore NOT a
    measurement, and a conversation with none answers None: unmeasured, not empty."""
    measured: list[int] = []
    for row in rows:
        payload = row.payload
        if not isinstance(payload, list):
            continue  # system-event rows carry no messages (the turn terminal is one)
        for message in payload:
            if not isinstance(message, dict) or message.get("kind") != "response":
                continue
            usage = message.get("usage")
            if not isinstance(usage, dict):
                continue
            reported = usage.get("input_tokens")
            # `bool` is an `int` in Python and `True` would sort as 1; narrowed, not cast,
            # because `payload` is untyped JSON and a non-number there is no measurement.
            if isinstance(reported, int) and not isinstance(reported, bool) and reported > 0:
                measured.append(reported)
    return max(measured, default=None)


def project_rows(rows: Sequence[Message]) -> list[DisplayItem]:
    """The one history→display derivation. `rows` must be the `include_hidden=True` read —
    hidden rows render nothing directly, but unclosed `build_started` markers derive the
    in-progress anchor."""
    closed = _closed_sessions(rows)
    first_steps = _first_step_rows(rows)
    synthetic = _synthetic_resolutions(rows)
    # One call id can be answered TWICE — a system overlay (a `build_failed` record; a real
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
    # ★ ONE CHIP PER FILE, ON THE MESSAGE THAT CARRIED IT — first wins across the transcript.
    #
    # THE STORED MARKER CHANGED MEANING AND THE ROWS DID NOT. It used to be stamped with the whole
    # conversation's code-lane set on every turn, so two files across four turns rendered eight
    # chips on reload; the send route narrows it to what the message actually carried now. But a
    # pre-narrowing row and a post-narrowing row are indistinguishable on disk — there is nothing
    # in the payload to branch on — so `SCHEMA_VERSION` cannot discriminate them and this dedupe
    # is a permanent heuristic rather than a migration.
    #
    # IT IS CORRECT ON BOTH SHAPES for one reason worth stating: `code_lane_attachments` orders by
    # the attachment's UUIDv7 primary key, which is upload order, so an id first appears on the
    # message that carried it whichever way the marker was written. That ordering is this dedupe's
    # load-bearing assumption and nothing here can check it — the tests below pin the dedupe, not
    # the ordering it rests on.
    seen_attachments: set[str] = set()

    for row in rows:
        if row.entry_kind is MessageEntryKind.SYSTEM_EVENT:
            meta = row.meta if isinstance(row.meta, dict) else {}
            kind = meta.get("kind")
            session_id = meta.get("sessionId")
            if kind == "build_started":
                if isinstance(session_id, str) and session_id not in closed:
                    items.append(BuildInProgressItem(seq=row.seq, session_id=session_id))
                continue
            if kind == "plan_options_pending" and meta.get("synthesized"):
                # The retry-cap fallback card: hidden row, visible card — its state
                # derives from the companion `plan_options_resolved` record.
                call_id = meta.get("toolCallId")
                if isinstance(call_id, str):
                    recorded = synthetic.get(call_id)
                    items.append(
                        PlanOptionsItem(
                            seq=row.seq,
                            tool_call_id=call_id,
                            state=_plan_options_state(
                                (recorded[0], False) if recorded is not None else None
                            ),
                        )
                    )
                continue
            if kind == "plan_options_resolved":
                continue  # the companion record renders through its pending card
            if kind == TURN_TERMINAL_KIND:
                # HIDDEN, and rendered anyway — the one system row that does. It carries no
                # sentence for anyone to read; it is structure, and the arm below would drop it
                # on the way to "hidden rows render nothing". Placed ABOVE that line for exactly
                # that reason.
                turn_id = meta.get("turnId")
                if isinstance(turn_id, str):
                    # THE REASON COMES OUT WITH THE TERMINAL, from the same meta, in one
                    # construction — see the item's docstring for why withholding it left a
                    # reloaded stop unreadable. Narrowed rather than cast: `meta` is
                    # untyped JSON, and a non-string reason is no reason at all.
                    reason = meta.get("reason")
                    items.append(
                        TurnTerminalItem(
                            seq=row.seq,
                            turn_id=turn_id,
                            terminal=_banner_kind(meta),
                            reason=reason if isinstance(reason, str) else None,
                        )
                    )
                continue
            if row.visibility is MessageVisibility.HIDDEN:
                continue  # hidden system rows render nothing
            if kind == PLATFORM_TEXT_KIND:
                # THE WORDS COME OUT OF `meta`, because the payload is empty on purpose — see
                # the constant above. The citizen reads the sentence exactly as they always
                # have; it is the model that no longer receives it.
                #
                # THE PAYLOAD FALLBACK IS FOR THE ROWS ALREADY WRITTEN. Rows from before the
                # sentence moved carry it as a `ModelResponse` and no `meta.text`, and a
                # transcript that silently dropped a paragraph a citizen had read is a worse
                # outcome than the replay this changed. Their model-facing copy stays; only
                # rows written from now on are out of the model's history.
                spoken = meta.get("text")
                text = spoken if isinstance(spoken, str) else _payload_text(row.payload)
                if text:
                    items.append(AssistantTextItem(seq=row.seq, text=text))
                continue
            if kind == "build_outcome" and isinstance(session_id, str):
                preview = meta.get("previewUrl")
                items.append(
                    BannerItem(
                        seq=row.seq,
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
                items.append(AssistantTextItem(seq=row.seq, text=text))
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
                        fresh = [ref for ref in refs if ref not in seen_attachments]
                        seen_attachments.update(fresh)
                        if text or fresh:
                            items.append(
                                UserTextItem(
                                    seq=row.seq,
                                    text=text,
                                    attachments=[
                                        AttachmentRefItem(attachment_id=ref) for ref in fresh
                                    ],
                                )
                            )
            elif message.get("kind") == "response":
                _project_response_parts(row, message, results, items)

    return items


async def project_conversation(
    db: AsyncSession, *, user_id: uuid.UUID, rows: Sequence[Message]
) -> list[DisplayItem]:
    """`project_rows`, with every attachment chip filled in. THE ENTRY POINT ROUTES USE.

    `project_rows` is a pure function over stored rows and stays that way — it is the one
    derivation, and it is tested as a pure function. But a chip needs the filename and media
    type, and those live in the `attachments` table rather than in the message payload, so
    somebody with a database session has to fill them in. That is this.

    IT WRAPS THE PURE FUNCTION RATHER THAN SITTING BESIDE IT because there are two callers —
    the reload read and the live turn's catch-up snapshot — and `live == reload` is a stated
    invariant of this module. Two routes each remembering to enrich is exactly how the two
    drift; one entry point that cannot be used without enriching is not.

    ONE QUERY FOR THE WHOLE TRANSCRIPT. The ids are collected across every item first, so a
    conversation with forty attachments costs one read rather than forty — the N+1 this
    codebase treats as a defect rather than a style note.

    An id with no row is left with empty name and media type on purpose: the row is gone
    (reclaimed with its conversation) but the reference survives in the payload forever, and
    the browser draws "attachment unavailable" from exactly that state.
    """
    items = project_rows(rows)
    wanted = {
        ref.attachment_id
        for item in items
        if isinstance(item, UserTextItem)
        for ref in item.attachments
    }
    if not wanted:
        return items

    found = (
        await db.execute(
            sa.select(Attachment.attachment_id, Attachment.name, Attachment.media_type).where(
                Attachment.user_id == user_id,
                Attachment.attachment_id.in_(wanted),
            )
        )
    ).all()
    # OWNER-SCOPED, like every other read on this platform (ADR-0004). An attachment id is a
    # client-supplied string, so without the `user_id` predicate one citizen's transcript could
    # name another's file simply by carrying their id.
    by_id = {row.attachment_id: (row.name, row.media_type) for row in found}

    for item in items:
        if not isinstance(item, UserTextItem):
            continue
        for ref in item.attachments:
            known = by_id.get(ref.attachment_id)
            if known is None:
                continue
            ref.name, ref.media_type = known
            ref.kind = chip_kind_for(ref.media_type)
    return items
