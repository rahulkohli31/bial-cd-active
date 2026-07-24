"""Mode prompt system (U9 / D4 / R13): one BASE + a positive per-mode segment, per run.

Authoring is grounded in the U9 research pass
(`docs/brainstorms/bial-walkthrough-2026-07-22-refs/mode-prompt-research.md` — untracked
reference doc, patterns cited by number below):

- Pattern 3: every segment LEADS with a purpose/identity sentence, tool talk second
  (OpenHands' Planning Agent, Copilot's "optimized for ..." one-liners).
- Pattern 1: tool surfaces are stated as facts about the mode's world ("you have read
  tools ...") — never as bans on tools the mode doesn't have. The registry
  (`toolsets.py`) makes wrong-mode tools structurally uncallable, which is the "clean
  removal" case even prohibition-heavy systems (Cline's editor tool) treat as needing
  no ban text (pattern 2). A test pins the segments prohibition-free.
- Patterns 4/5: Plan mode's output contract is a NAMED tool call (`present_plan_options`,
  the opencode `plan_exit` shape), and plan→build is gated on the user's explicit click,
  never conversational tone ("never treat the task request as approval" — Cline).
- Pattern 9: the plan has a concrete shape — but a CITIZEN-facing one (F9): the outcome,
  what the user will see and do, what the app remembers, and the one experience-level
  decision as a plain question. The steps/files/trade-offs skeleton the pattern was
  originally sourced from (opencode/OpenHands — developer CLIs) is the build's business,
  kept out of the plan the user reads. Grounding stays: the model still reads the real
  files first; only the OUTPUT register is citizen-plain.
- Pattern 6: the rare cross-mode safety rules (DATA INTEGRITY) stay positive-first and
  are stated ONCE, in BASE — imported from the single source `DATA_INTEGRITY_RULES`
  (U1), never copied.

The Write→Ask/Plan downgrade clarification deliberately does NOT live here: it rides the
direction-aware mode-switch marker rows (U4), which fire exactly when the history
contradicts the toolset. Static segments stay clean.

Delivery is per-run `@agent.instructions` (`agent.py`) — composed text is never persisted
(pydantic-ai keeps instructions out of message parts; pinned by test).
"""

from __future__ import annotations

from dataclasses import dataclass

from src.core.prompt_blocks import (
    BUILD_WORKING_RULES_HEAD,
    BUILD_WORKING_RULES_TAIL,
    DATA_INTEGRITY_RULES,
)
from src.db.models.conversation import ConversationMode


@dataclass(frozen=True)
class PromptContext:
    """What BASE needs to say who the assistant is working with and on what. Built per
    turn from the conversation's project + owner (U10); `project_description` is the
    project row's description, absent when the user never wrote/generated one."""

    user_name: str
    project_name: str
    project_description: str | None = None


def _base(context: PromptContext) -> str:
    """BASE — identity, project grounding, and the one cross-mode safety block. Shared by
    every mode so the safety wording exists exactly once (pattern 6; U1's single source)."""
    described = f" — {context.project_description}" if context.project_description else ""
    identity = (
        f"You are the Citizen Developer assistant for BIAL, working with "
        f'{context.user_name} on "{context.project_name}"{described}. You work inside '
        "this one project: its app, its code, and its data. Ground everything you say "
        "about the app in its actual files, and answer what was asked before acting."
    )
    return f"{identity}\n\n{DATA_INTEGRITY_RULES}"


_ASK_SEGMENT = """\
ASK MODE — you answer the user's questions about their app and help them understand what \
it does and how. You have read tools for exactly that: `read_file`, `list_files`, \
`search_files`, and read-only shell commands through `run_command` (ls / cat / grep and \
friends). Read the real files and ground every answer in what you find — name the actual \
files and quote the actual code, and when the app holds no answer, say so plainly. If \
there is no app yet, your tools will tell you truthfully; describe what could be built \
rather than guessing at what exists. When the user wants the app changed, point them to \
Plan mode (to shape the change together first) or Write mode (to build it directly)."""

_PLAN_SEGMENT = """\
PLAN MODE — you and the user work out WHAT to build before anything gets built. You have \
read tools for the groundwork: `read_file`, `list_files`, `search_files`, and read-only \
shell commands through `run_command`. Read the relevant files first, so the plan fits the \
app as it actually is and keeps every existing feature accounted for. Then write the plan \
the way you would explain it to the person who asked — in plain, everyday words, about the \
app they will use, not the code underneath. Lead with what the app or this change will DO \
for them, in one sentence. Then lay out what they will SEE and be able to DO — the screens \
and the actions, in human terms. Then say, in plain language, what the app will remember \
for them ("every message is saved with the date it was sent, so nothing gets lost") — the \
outcome, told the way a person would tell it. When a choice would change their experience, \
put it to them as a plain question ("Should everyone see all the feedback, or just you?") \
and state the assumption you have made for now. Keep the how-it's-built details behind the \
scenes and out of the plan itself: the tools and frameworks, the file and folder names, \
the way data is stored under the hood, the web-request wiring, and the engineering \
pros and cons all belong to the build, not to the plan the user reads — describe \
everything in words the user already knows. End a planning turn one of two ways: ask the \
user a clarifying question, or — when the plan feels ready — call `present_plan_options`, \
which puts the Build it / Keep refining buttons in front of the user. After calling it, \
wait for their choice; the click on Build it is the only signal that building starts. If \
they keep refining, revise the plan and present again."""

_WRITE_PURPOSE = """\
WRITE MODE — you build. You write and iterate on real code in the app's live sandbox \
until the app type-checks and renders."""

_WRITE_PRESERVE = """\
PRESERVE WHAT WORKS — the app may already do things its user relies on. Read before you \
change, keep every existing feature working through your change, and remove one only \
when the user's requirements remove it — saying so plainly in your summary."""

_FROM_SCRATCH_FRAMING = """\
Build from the user's request in this conversation. When an app already exists, read it \
first and build on what is there."""


def _write_segment(approved_plan: str | None) -> str:
    """WRITE — purpose first (pattern 3), then the honest framing for HOW this build was
    commissioned: executing an approved plan, or building directly from conversation.
    The approved-plan clause composes ONLY when a plan was actually approved — direct
    Write entry never claims a plan that doesn't exist (plan decision, 2026-07-22)."""
    if approved_plan is None:
        framing = _FROM_SCRATCH_FRAMING
    else:
        framing = (
            "You are executing the plan the user approved:\n\n"
            f"{approved_plan}\n\n"
            "Build what the plan says. Where the code on disk differs from what the "
            "plan assumed, follow the code's reality and tell the user what changed."
        )
    return (
        f"{_WRITE_PURPOSE}\n\n{framing}\n\n{_WRITE_PRESERVE}\n\n"
        f"{BUILD_WORKING_RULES_HEAD}\n\n{BUILD_WORKING_RULES_TAIL}"
    )


# --- U14 (D3): ephemeral mode reminders ---------------------------------------------
#
# Long conversations bury the per-run instructions at the TOP of context; these notes
# re-anchor the active mode near the tail, where attention lands. Same authoring rules
# as the segments (positive voice, purpose + expected next action, no absent-tool
# prose), attributed and XML-delimited so the model reads them as system guidance, not
# user words. Delivery is the ENGINE's business (`services/turns/engine.py`): they ride
# `message_history` only and are never persisted or rendered.

_ASK_REMINDER_FULL = (
    "<system-note>Ask mode is active. Answer the user's questions about their app from "
    "its real files — read them with your read tools and ground every answer in what "
    "you find. When the user wants the app changed, point them to Plan mode (to shape "
    "the change together) or Write mode (to build it directly).</system-note>"
)
_ASK_REMINDER_NUDGE = (
    "<system-note>Ask mode is active — ground every answer in the app's real files.</system-note>"
)
_PLAN_REMINDER_FULL = (
    "<system-note>Plan mode is active. Keep shaping WHAT to build with the user: read "
    "the relevant files, then describe the plan in plain, everyday words — what the app "
    "will DO for them, what they will SEE and be able to DO, what it will remember for "
    "them, and any choice that changes their experience (as a plain question) — with the "
    "tools, file names, and data-storage details kept behind the scenes. End a planning "
    "turn by asking a clarifying question, or — when the plan feels ready — by calling "
    "present_plan_options to put the Build it / Keep refining buttons in front of the "
    "user, then wait for their click.</system-note>"
)
_PLAN_REMINDER_NUDGE = (
    "<system-note>Plan mode is active — describe the plan in plain, everyday words, then "
    "call present_plan_options when it is ready to show the confirmation buttons.</system-note>"
)
_WRITE_REMINDER_FULL = (
    "<system-note>Write mode is active. Keep building in the app's live sandbox until "
    "the app type-checks and renders, keeping every existing feature working through "
    "your changes.</system-note>"
)
_WRITE_REMINDER_NUDGE = (
    "<system-note>Write mode is active — keep building until the app type-checks and "
    "renders.</system-note>"
)


def mode_reminder(mode: ConversationMode, *, full: bool) -> str:
    """The mode's reminder text: FULL restates purpose + the expected next action (the
    cadence anchors and post-switch turns); the NUDGE is the one-line touch between."""
    match mode:
        case ConversationMode.ASK:
            return _ASK_REMINDER_FULL if full else _ASK_REMINDER_NUDGE
        case ConversationMode.PLAN:
            return _PLAN_REMINDER_FULL if full else _PLAN_REMINDER_NUDGE
        case ConversationMode.WRITE:
            return _WRITE_REMINDER_FULL if full else _WRITE_REMINDER_NUDGE


def compose_mode_prompt(
    mode: ConversationMode,
    context: PromptContext,
    *,
    approved_plan: str | None = None,
) -> str:
    """BASE + exactly one mode segment (D4). `approved_plan` is Write-only fuel: the
    approved plan's text when the turn executes one, None for direct Write entry. It is
    a programming error on Ask/Plan turns (fail-first — silently ignoring it would hide
    a mis-wired turn engine)."""
    if approved_plan is not None and mode is not ConversationMode.WRITE:
        raise ValueError(f"approved_plan composes only in Write mode, not {mode.value}.")
    match mode:
        case ConversationMode.ASK:
            segment = _ASK_SEGMENT
        case ConversationMode.PLAN:
            segment = _PLAN_SEGMENT
        case ConversationMode.WRITE:
            segment = _write_segment(approved_plan)
    return f"{_base(context)}\n\n{segment}"
