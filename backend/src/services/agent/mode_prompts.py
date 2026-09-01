"""Chat-kind prompt system (U9 / D4 / R13): one BASE + a positive per-kind segment, per run.

Authoring is grounded in the U9 research pass
(`docs/brainstorms/bial-walkthrough-2026-07-22-refs/mode-prompt-research.md` — untracked
reference doc, patterns cited by number below):

- Pattern 3: every segment LEADS with a purpose/identity sentence, tool talk second
  (OpenHands' Planning Agent, Copilot's "optimized for ..." one-liners).
- Pattern 1: tool surfaces are stated as facts about the kind's world ("you have read
  tools ...") — never as bans on tools the kind doesn't have. The registry
  (`toolsets.py`) makes absent tools structurally uncallable, which is the "clean
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

There is no downgrade clarification any more and there is nothing for one to say: a chat's
kind is fixed at creation, so a conversation's history can never contradict the toolset it is
running under. The direction-aware marker rows that carried it are gone with the switch.

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
from src.db.models.conversation import ChatKind


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
which puts the Build this plan / Keep planning buttons in front of the user. After calling \
it, wait for their choice; the click on Build this plan is the only signal that building \
starts. If they keep planning, revise the plan and present again."""

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

_RECONCILE_WITH_REALITY = """\
A message may describe a plan that was written some time ago. Where the code on disk differs \
from what the plan assumed, follow the code's reality and tell the user plainly what you found \
and what you did differently."""
"""R25's second half, and it lives HERE because the thing that used to carry it is gone.

It was a prefix the Build-it endpoint glued onto a hidden seed message — so it only ever reached
a build started from a plan, and only in the same conversation. The handoff now posts the plan as
an ordinary user message in a new chat, byte-identical to the citizen having pasted it, and there
is nowhere in that message for a platform instruction to hide. Putting it in the segment is
strictly better than where it was: a plan can be built weeks after it was written, and the agent
in a fresh Build chat has LESS context to notice a divergence with, not more.

The wording is this plan's to preserve, not to perfect — the voice work owns how it is phrased,
and may reword it. It may not drop it."""

_WRITE_SEGMENT = f"""\
{WRITE_IDENTITY}

{_RECONCILE_WITH_REALITY}

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


# --- THE PER-TURN RESTATEMENT IS GONE, and nothing replaced it (R17) ------------------
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
# tail as well, so deleting it with the reminders would break the one ephemeral note this plan
# is protecting.

# N9(a) — the note says it is private. The walkthrough caught the model quoting one of these
# notes back at the citizen ("I want to flag that note…"), so the user watched the assistant
# argue with an instruction they never wrote and could not see. Nothing told the model the note
# was private, and "it is obviously internal" is not an instruction.
#
# Phrased in POSITIVE VOICE, like everything else here (R13 / pattern 1-2): "keep it out of
# your reply" is the same instruction as "never mention it" without teaching the model to
# reason in prohibitions.
_PRIVATE = " This note is between you and the platform — keep it out of your reply."


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


def compose_kind_prompt(kind: ChatKind, context: PromptContext) -> str:
    """BASE + exactly one segment, for both kinds.

    `_base` is invariant across kinds and always has been; only the segment varies. Which
    segment a run gets follows from what it can DO, so this selection sits one file away from
    the toolset registry that decides that."""
    match kind:
        case ChatKind.PLAN:
            segment = _PLAN_SEGMENT
        case ChatKind.BUILD:
            segment = _WRITE_SEGMENT
    return f"{_base(context)}\n\n{segment}"
