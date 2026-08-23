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
    PORTAL_SURFACES,
    WRITE_IDENTITY,
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
    """BASE — identity, project grounding, the truthful portal self-description (R5), and the
    one cross-mode safety block. Shared by every mode so each wording exists exactly once
    (pattern 6; U1's and R5's single sources)."""
    described = f" — {context.project_description}" if context.project_description else ""
    identity = (
        f"You are the Citizen Developer assistant for BIAL, working with "
        f'{context.user_name} on "{context.project_name}"{described}. You work inside '
        "this one project: its app, its code, and its data. Ground everything you say "
        "about the app in its actual files, and answer what was asked before acting."
    )
    # R5: the walkthrough caught the model inventing portal features. The relay had this
    # clause and the mode system did not, so R5 would have regressed the moment the relay
    # retired — it belongs in BASE, where every mode carries it.
    return f"{identity}\n\n{PORTAL_SURFACES}\n\n{DATA_INTEGRITY_RULES}"


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

# NO COMMIT BLOCK LIVES HERE ANY MORE (U19 / R25), and re-adding one is a regression with two
# separate costs. `_COMMIT_DISCIPLINE` used to sit at the end of this segment teaching the agent
# to stage and commit each coherent slice.
#
# 1. THE PLATFORM ALREADY DOES IT. `snapshot._COMMIT_SCRIPT` runs `git add -A && git commit` as
#    step ONE of every turn-boundary bundle, so the agent's commits bought the user nothing and
#    cost them a shell round trip per slice plus the tokens to narrate it.
# 2. IT TAUGHT GIT-UNDO — `git checkout` and `git revert` over a tree the agent had just decided
#    it disliked. Both produce a HEAD that is NOT a descendant of the copy on record, which is
#    exactly the input the workspace-integrity verdict has to reason about before it may declare
#    a workspace REVERTED. That verdict closes the hazard on its own (it requires the CONTENT to
#    disagree as well as the lineage), but nothing should be feeding it self-inflicted
#    non-descendant HEADs. `test_the_write_prompt_teaches_no_git_undo` is the inertness guard.
#
# The reminder that enforced the deleted instruction went with it —
# `orchestrator/tools._note_write_and_maybe_remind` and `SandboxSession.uncommitted_writes`.

_WRITE_SEGMENT = f"""\
{WRITE_IDENTITY}

{BUILD_WORKING_RULES_HEAD}

{BUILD_WORKING_RULES_TAIL}"""
"""WRITE's segment — the same shared blocks `BUILD_SYSTEM_PROMPT` composes from, so the two can
never drift (KTD-5a). The original objection to a Write segment here — "it could only ever drift
from `orchestrator/prompt.py`" — is true of a COPY and false of a shared import, which is what
this is.

`DATA_INTEGRITY_RULES` is deliberately ABSENT from this list even though the build prompt names
it: `_base(context)` already appends it for every mode, so naming it again would emit the whole
block twice in every Write prompt.

`NARRATION_VOICE` (U15's audience block — R20/R22) is ABSENT for the same reason and must stay so:
it rides inside `BUILD_WORKING_RULES_TAIL`, which is the single place both this segment and
`BUILD_SYSTEM_PROMPT` pick it up. Adding it to this list would print the whole voice rule twice
here while the build prompt printed it once — the two Write prompts drifting in the one dimension
the shared blocks exist to keep identical. A test counts it in the composed prompt."""


# --- U14 (D3): ephemeral mode reminders ---------------------------------------------
#
# Long conversations bury the per-run instructions at the TOP of context; these notes
# re-anchor the active mode near the tail, where attention lands. Same authoring rules
# as the segments (positive voice, purpose + expected next action, no absent-tool
# prose), attributed and XML-delimited so the model reads them as system guidance, not
# user words. Delivery is the ENGINE's business (`services/turns/engine.py`): they ride
# `message_history` only and are never persisted or rendered.

# N9(a) — EVERY reminder says it is private. The walkthrough caught the model quoting one of these
# notes back at the citizen ("I want to flag that note…"), so the user watched the assistant argue
# with an instruction they never wrote and could not see. Nothing told the model the note was
# private, and "it is obviously internal" is not an instruction.
#
# Phrased in POSITIVE VOICE, like everything else here (R13 / pattern 1-2, pinned by
# `test_reminders_speak_no_forbidden_fruit`): "keep it out of your reply" is the same instruction
# as "never mention it" without teaching the model to reason in prohibitions. Stated on each
# constant rather than appended once at the injection site, because the constants are what a
# future author copies.
_PRIVATE = " This note is between you and the platform — keep it out of your reply."

_ASK_REMINDER_FULL = (
    "<system-note>Ask mode is active. Answer the user's questions about their app from "
    "its real files — read them with your read tools and ground every answer in what "
    "you find. When the user wants the app changed, point them to Plan mode (to shape "
    "the change together) or Write mode (to build it directly)." + _PRIVATE + "</system-note>"
)
_ASK_REMINDER_NUDGE = (
    "<system-note>Ask mode is active — ground every answer in the app's real files."
    + _PRIVATE
    + "</system-note>"
)
_PLAN_REMINDER_FULL = (
    "<system-note>Plan mode is active. Keep shaping WHAT to build with the user: read "
    "the relevant files, then describe the plan in plain, everyday words — what the app "
    "will DO for them, what they will SEE and be able to DO, what it will remember for "
    "them, and any choice that changes their experience (as a plain question) — with the "
    "tools, file names, and data-storage details kept behind the scenes. End a planning "
    "turn by asking a clarifying question, or — when the plan feels ready — by calling "
    "present_plan_options to put the Build it / Keep refining buttons in front of the "
    "user, then wait for their click." + _PRIVATE + "</system-note>"
)
_PLAN_REMINDER_NUDGE = (
    "<system-note>Plan mode is active — describe the plan in plain, everyday words, then "
    "call present_plan_options when it is ready to show the confirmation buttons."
    + _PRIVATE
    + "</system-note>"
)

# N9(b) — the SAME mode anchor, minus the call-the-tool instruction, for the turns where that
# instruction would be actively wrong: a card is already on screen unresolved, or the user has
# just answered one. Firing "call present_plan_options" immediately after the citizen clicked
# **Keep refining** burned a whole turn — and the user's tokens — on the model correctly
# resisting an instruction the platform should not have sent. Suppressing the reminder outright
# would throw away its real job (keep the plan in plain language), so only the wrong sentence goes.
_PLAN_REMINDER_FULL_HOLDING = (
    "<system-note>Plan mode is active. Keep shaping WHAT to build with the user: read "
    "the relevant files, then describe the plan in plain, everyday words — what the app "
    "will DO for them, what they will SEE and be able to DO, what it will remember for "
    "them, and any choice that changes their experience (as a plain question) — with the "
    "tools, file names, and data-storage details kept behind the scenes. The confirmation "
    "buttons are already with the user, so keep refining the plan and wait for their "
    "choice." + _PRIVATE + "</system-note>"
)
_PLAN_REMINDER_NUDGE_HOLDING = (
    "<system-note>Plan mode is active — describe the plan in plain, everyday words. The "
    "confirmation buttons are already with the user." + _PRIVATE + "</system-note>"
)

_WRITE_REMINDER_FULL = (
    "<system-note>Write mode is active. Keep building in the app's live sandbox until "
    "the app type-checks and renders, keeping every existing feature working through "
    "your changes." + _PRIVATE + "</system-note>"
)
_WRITE_REMINDER_NUDGE = (
    "<system-note>Write mode is active — keep building until the app type-checks and "
    "renders." + _PRIVATE + "</system-note>"
)


# --- U8 (R14): the ephemeral workspace note ------------------------------------------
#
# THE MODEL IS TOLD WHAT THE WORKSPACE IS DOING RIGHT NOW, on every turn, whether it asked or not.
# The prohibition ("do not answer from memory") existed and was obeyed the way prohibitions are:
# a user said their app was broken, and the assistant answered from the conversation — where the
# app had been working — because that was the only account of the app it had. Instructing a model
# not to answer from stale context, while giving it nothing else, asks it to know something it
# cannot know. This hands it the fact instead, so answering from stale history stops being
# forbidden and starts being unnecessary.
#
# IT RIDES THE HISTORY TAIL AND IS NEVER PERSISTED, exactly like the reminders above and for the
# same structural reason: `_persistable_messages` drops requests carrying a `UserPromptPart`, so
# nothing downstream has to remember to strip it. It is emphatically NOT part of `_reminder_text`,
# which is cadence-gated and returns `None` between anchors — riding that would inject the
# workspace state on roughly one turn in four, while this exists precisely to hold on all of them.

_WORKSPACE_NOTE_HEAD = "<system-note>The platform checked this app's workspace just now: "

_WORKSPACE_NOTE_TAIL = (
    " Use this rather than what earlier messages in this conversation said about the app — those "
    "describe how it was, and this is how it is." + _PRIVATE + "</system-note>"
)

_WORKSPACE_UNKNOWN = (
    "the platform could not tell what state it is in this time. If the user says something is "
    "wrong, look at the app's files and check for yourself rather than assuming it still works."
)

_WORKSPACE_NOT_SERVING = (
    "the app is not currently serving. Something it needs at startup is most likely failing, so "
    "treat any question about what the app does today as a question about a broken app."
)

_WORKSPACE_STILL_TEMPLATE = (
    "the app is serving, and its home page is still byte-for-byte the starter template the "
    "workspace was created with — nothing the user asked for is on the page they actually look "
    "at. Whatever else exists in the files, the app they see has not been built yet."
)

_WORKSPACE_LIVE = "the app is serving, and its home page is no longer the starter template."


def workspace_note(*, serving: bool | None, still_the_template: bool | None) -> str:
    """The private note telling the model what this app's workspace is doing, right now (U8/R14).

    `None` means the platform could not find out, and it is deliberately not collapsed into either
    of the other answers: a model told "your app is fine" on the strength of a check that never
    completed is worse off than one told nothing, because it will now defend the claim.

    Ordering. "Could not tell" wins over everything — an unanswered check cannot be reported as a
    finding. Then "not serving", because an app that is down is not an app whose home page is
    worth discussing. Only then the content answer."""
    if serving is None:
        body = _WORKSPACE_UNKNOWN
    elif not serving:
        body = _WORKSPACE_NOT_SERVING
    elif still_the_template is None:
        body = _WORKSPACE_UNKNOWN
    elif still_the_template:
        body = _WORKSPACE_STILL_TEMPLATE
    else:
        body = _WORKSPACE_LIVE
    return f"{_WORKSPACE_NOTE_HEAD}{body}{_WORKSPACE_NOTE_TAIL}"


def mode_reminder(
    mode: ConversationMode, *, full: bool, plan_options_outstanding: bool = False
) -> str:
    """The mode's reminder text: FULL restates purpose + the expected next action (the
    cadence anchors and post-switch turns); the NUDGE is the one-line touch between.

    `plan_options_outstanding` is Plan-only and means a confirmation card is already with the
    user — unresolved, or resolved on the turn that just ran. It swaps the "call
    present_plan_options" sentence for "the buttons are already there", because telling the model
    to present buttons that are on screen is how the platform burned a turn arguing with itself
    (N9b). Ask and Write ignore it: neither has the tool."""
    match mode:
        case ConversationMode.ASK:
            return _ASK_REMINDER_FULL if full else _ASK_REMINDER_NUDGE
        case ConversationMode.PLAN:
            if plan_options_outstanding:
                return _PLAN_REMINDER_FULL_HOLDING if full else _PLAN_REMINDER_NUDGE_HOLDING
            return _PLAN_REMINDER_FULL if full else _PLAN_REMINDER_NUDGE
        case ConversationMode.WRITE:
            return _WRITE_REMINDER_FULL if full else _WRITE_REMINDER_NUDGE


def compose_mode_prompt(
    mode: ConversationMode,
    context: PromptContext,
    *,
    approved_plan: str | None = None,
) -> str:
    """BASE + exactly one mode segment (D4) — for EVERY mode, Write included.

    A mode switch swaps the segment and nothing else happens (KTD-5). The Write refusal that
    used to live here was not a design statement: no `_WRITE_SEGMENT` had been authored, to avoid
    duplicating `orchestrator/prompt.py`. A shared import solves that, so the seam is finished
    rather than worked around.

    `approved_plan` still has nowhere to go — the plan a user approves is seeded as an ordinary
    Write message on the same conversation (KTD-6), not spliced into the system prompt — so
    accepting it silently would paper over a mis-wired caller."""
    if approved_plan is not None:
        raise ValueError(
            "approved_plan has no home in the mode prompt — an approved plan is seeded as the "
            "first Write MESSAGE on the conversation (api/v1/conversations/transition.py)."
        )
    match mode:
        case ConversationMode.ASK:
            segment = _ASK_SEGMENT
        case ConversationMode.PLAN:
            segment = _PLAN_SEGMENT
        case ConversationMode.WRITE:
            segment = _WRITE_SEGMENT
    return f"{_base(context)}\n\n{segment}"
