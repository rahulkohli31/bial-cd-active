"""Chat-kind prompt system: one BASE + a positive per-kind segment, per run.

Authoring choices: every segment LEADS with purpose/identity, tool talk second; tool
surfaces are stated as facts ("you have read tools ...") never bans — the registry
(`toolsets.py`) makes absent tools structurally uncallable. Plan's output contract is the
NAMED `present_plan_options` call, gated on the user's click, never tone; its segment names
WHAT the plan is for and WHO reads it, not a fixed five-part shape. Cross-mode safety rules
(DATA INTEGRITY) live ONCE, in BASE, single-sourced — never copied (see `_base`).

A chat's kind is fixed at creation, so history can never contradict its toolset — no
downgrade clarification, no marker rows. Delivery is per-run `@agent.instructions`;
composed text is never persisted (pinned by test)."""

from __future__ import annotations

from dataclasses import dataclass

from src.core.prompt_blocks import (
    BUILD_THIS_PLAN_LABEL,
    BUILD_WORKING_RULES_HEAD,
    BUILD_WORKING_RULES_TAIL,
    DATA_INTEGRITY_RULES,
    DATA_INTEGRITY_RULES_WITHOUT_THE_WRITE_MACHINERY,
    FIRST_SLICE_RULE,
    KEEP_PLANNING_LABEL,
    NARRATION_EXAMPLES,
    NARRATION_VOICE,
    PORTAL_SURFACES,
    WRITE_IDENTITY,
)
from src.db.models.conversation import ChatKind


@dataclass(frozen=True)
class PromptContext:
    """What BASE needs to say who the assistant is working with and on what. Built per
    turn from the conversation's project + owner; `project_description` is the
    project row's description, absent when the user never wrote/generated one."""

    user_name: str
    project_name: str
    project_description: str | None = None


def _base(context: PromptContext, kind: ChatKind) -> str:
    """BASE — the voice examples, identity, project grounding, the truthful portal
    self-description, and the one cross-mode safety block. Shared by every kind so each wording
    exists exactly once.

    THE EXAMPLES COME BEFORE THE IDENTITY SENTENCE. The audience contract has never been missing
    from this prompt; what it lacked was a position and a pair of sentences to match against.
    `NARRATION_VOICE` still states the rule where it always has, some 530 words in — this is the
    same contract shown first, in three pairs the model reads before it writes anything.
    Anything inserted above it takes that away.

    THE ONE THING BASE VARIES BY KIND: the same `DATA_INTEGRITY_RULES` string with two
    Build-only clauses dropped (the destructive-SQL sentinel, the migration channel) via
    `DATA_INTEGRITY_RULES_WITHOUT_THE_WRITE_MACHINERY` — byte-identical rules otherwise. This
    is why `_base` takes a kind at all: the false half of a cross-mode block turned out to be
    the mode-specific half."""
    described = f" — {context.project_description}" if context.project_description else ""
    identity = (
        f"You are the Citizen Developer assistant for BIAL, working with "
        f'{context.user_name} on "{context.project_name}"{described}. You work inside '
        "this one project: its app, its code, and its data. Ground everything you say "
        "about the app in its actual files, and answer what was asked before acting."
    )
    # The walkthrough caught the model inventing portal features. The relay had this
    # clause and the mode system did not, so this rule would have regressed the moment the relay
    # retired — it belongs in BASE, where every mode carries it.
    integrity = (
        DATA_INTEGRITY_RULES
        if kind is ChatKind.BUILD
        else DATA_INTEGRITY_RULES_WITHOUT_THE_WRITE_MACHINERY
    )
    return (
        f"{NARRATION_EXAMPLES}\n\n{identity}\n\n{PORTAL_SURFACES}\n\n{integrity}\n\n"
        f"{NARRATION_VOICE}\n\n{FIRST_SLICE_RULE}"
    )


_PLAN_SEGMENT = f"""\
PLAN MODE — you and the user work out WHAT to build before anything gets built. You have \
read tools for the groundwork: `read_file`, `list_files`, `search_files`, and read-only \
shell commands through `run_command`. Read the relevant files first, so the plan fits the \
app as it actually is and keeps every existing feature accounted for. When that reading \
turns up nothing but the starter template, that is not a gap to apologize for — it is the \
opening for the plan you are about to write: talk about what could be built for them.

WHERE THE PLAN GOES — you write the plan as the `plan` argument of \
`present_plan_options`, not as a message beside the call. The argument is what the buttons \
are attached to, so a plan announced next to the call would leave the user reading a plan \
with nothing to press. Everything else you write does reach them, in the order you write it.

Nothing in the plan names a file, a folder, a framework, a library, a command, or the way \
data is stored underneath. The engineering pros and cons belong to the build.

ATTACHED FILES ARE READ, NOT GUESSED AT. A spreadsheet, document, deck, CSV or TSV the \
user attached is already in your workspace, and the turn tells you its path and the one \
command that opens it. Use that reader — it is the tested one, and it reports the whole \
file: every sheet, the true row counts, the columns that hold formulas with no calculated \
result, the table headers. A description written from a file's name reads exactly as \
confident as a correct one. Then say what the file MEANS in your own words, because the \
build chat starts fresh with the plan and nothing else — anything only the file holds is \
lost unless the plan says it.

End a planning turn one of two ways: ask the user a clarifying question, or — when the plan \
is ready — call `present_plan_options` with it, which puts the \
`{BUILD_THIS_PLAN_LABEL}` and `{KEEP_PLANNING_LABEL}` buttons in front of them. After \
calling it, wait for their choice; `{BUILD_THIS_PLAN_LABEL}` is the only signal that \
building starts. If they choose `{KEEP_PLANNING_LABEL}`, revise the plan and present again."""

# NO COMMIT BLOCK LIVES HERE ANY MORE, and re-adding one is a regression with two
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
#    non-descendant HEADs. `test_neither_write_prompt_instructs_the_agent_in_git` is the guard.
#
# The reminder that enforced the deleted instruction went with it —
# `orchestrator/tools._note_write_and_maybe_remind` and `SandboxSession.uncommitted_writes`.

_RECONCILE_WITH_REALITY = """\
A message may describe a plan that was written some time ago. Where the code on disk differs \
from what the plan assumed, follow the code's reality and tell the user plainly what you found \
and what you did differently."""
"""Reconciles a stale plan with what the code actually is now, kept here because the thing
that used to carry this instruction is gone.

It was a prefix the Build-it endpoint glued onto a hidden seed message — so it only ever reached
a build started from a plan, and only in the same conversation. The handoff now posts the plan as
an ordinary user message in a new chat, byte-identical to the citizen having pasted it, and there
is nowhere in that message for a platform instruction to hide. Putting it in the segment is
strictly better than where it was: a plan can be built weeks after it was written, and the agent
in a fresh Build chat has LESS context to notice a divergence with, not more.

This wording is carried forward as-is rather than polished here — the voice work owns how it
is phrased, and may reword it. It may not drop it."""

_WRITE_SEGMENT = f"""\
{WRITE_IDENTITY}

{_RECONCILE_WITH_REALITY}

{BUILD_WORKING_RULES_HEAD}

{BUILD_WORKING_RULES_TAIL}"""
"""WRITE's segment, and since the build harness was deleted THE ONLY WRITE PROMPT THERE IS. It
composes from the shared `core/prompt_blocks.py` sources rather than typing the text out, which is
what kept it from drifting against the standalone `BUILD_SYSTEM_PROMPT` while that existed — and
is now simply where the one copy lives. The original objection to a Write segment here — "it
could only ever drift from `orchestrator/prompt.py`" — was true of a COPY and false of a shared
import, which is what this is.

`DATA_INTEGRITY_RULES` is deliberately ABSENT from this list even though a Write turn is told the
rules: `_base(context)` already appends them for every mode, so naming them again would emit the
whole block twice in every Write prompt.

`NARRATION_VOICE` (the audience contract) is ABSENT for the same reason and must
stay so: `_base(context)` names it for every kind, so adding it here would print the whole voice
rule twice. A test counts it at exactly one in the composed prompt, and that count is the guard
against the deletion this block has already suffered twice.

`NARRATION_EXAMPLES` is ABSENT for a third reason on top of that one: `_base()` names it, and it
has to lead the composed prompt. Naming it in a segment would put a second copy six hundred words
down — the position is the point, and a copy in the middle quietly cancels it."""


# --- THE PER-TURN RESTATEMENT IS GONE, and nothing replaced it ------------------------
#
# There used to be a cadence here: a full restatement of "which mode you are in" every eighth
# turn, a one-line nudge every fourth between, re-anchored by the mode-switch marker rows. It
# went with the thing it was restating. A chat's kind is fixed at creation, and having a
# different set of ABILITIES is what carries "which chat this is" — the model cannot call a
# tool it was not handed, whatever it was last told.
#
# The research is not one-sided and the deletion is not a claim that it is: a short refresher
# every several turns, at system role, phrased as context, is explicitly endorsed for
# standing-permission modes. This one was neither — it restated a mode that no longer exists,
# and it was delivered as a `user`-role message on a per-turn cadence, which is the wrong tier
# and a named cache-breaking action.
#
# `_PRIVATE` below OUTLIVES them, and deliberately: it is composed into the workspace note's
# tail as well, so deleting it with the reminders would break the one ephemeral note that
# still relies on it.

# The note says it is private. The walkthrough caught the model quoting one of these
# notes back at the citizen ("I want to flag that note…"), so the user watched the assistant
# argue with an instruction they never wrote and could not see. Nothing told the model the note
# was private, and "it is obviously internal" is not an instruction.
#
# Phrased in POSITIVE VOICE, like everything else here: "keep it out of
# your reply" is the same instruction as "never mention it" without teaching the model to
# reason in prohibitions.
_PRIVATE = " This note is between you and the platform — keep it out of your reply."


# --- The ephemeral workspace note ------------------------------------------------------
#
# THE MODEL IS TOLD WHAT THE WORKSPACE IS DOING RIGHT NOW, on every turn, whether it asked or not.
# The prohibition ("do not answer from memory") existed and was obeyed the way prohibitions are:
# a user said their app was broken, and the assistant answered from the conversation — where the
# app had been working — because that was the only account of the app it had. Instructing a model
# not to answer from stale context, while giving it nothing else, asks it to know something it
# cannot know. This hands it the fact instead, so answering from stale history stops being
# forbidden and starts being unnecessary.
#
# IT RIDES THE HISTORY TAIL AND IS NEVER PERSISTED: `_persistable_messages` drops requests
# carrying a `UserPromptPart`, so nothing downstream has to remember to strip it. It is injected
# UNCONDITIONALLY, on every turn that pinned a workspace, in both kinds — never on a cadence.
# That distinction is why it survived the deletion of the restatement machinery above it: a note
# that arrives on one turn in four cannot be what makes answering from stale history unnecessary.

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
    """The private note telling the model what this app's workspace is doing, right now.

    `None` (could not find out) is NOT collapsed into either answer: a model told "it's
    fine" on an incomplete check is worse off than one told nothing — it will defend the
    claim.

    ORDER: "could not tell" > "not serving" > the content answer — an unanswered check is
    not a finding, and a down app has no home page worth discussing."""
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


def compose_kind_prompt(kind: ChatKind, context: PromptContext) -> str:
    """BASE + exactly one segment, for both kinds.

    The segment varies, and so does exactly one clause-pair inside BASE — see `_base`. Which
    segment a run gets follows from what it can DO, so this selection sits one file away from
    the toolset registry that decides that, and BASE's one variation follows the same rule: the
    two dropped clauses describe tools a Plan run is not handed."""
    match kind:
        case ChatKind.PLAN:
            segment = _PLAN_SEGMENT
        case ChatKind.BUILD:
            segment = _WRITE_SEGMENT
    return f"{_base(context, kind)}\n\n{segment}"
