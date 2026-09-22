"""The census of what may branch on a chat kind, enforced instead of asserted.

WHY THIS EXISTS

A GUARD AND NOT A SENTENCE. Two comments in this codebase used to state this census — one
on `ChatKind` itself ("exactly two readers"), one in the turn engine ("three ... the closed
set") — and they were both wrong, they disagreed with each other, and neither could go red. A
number in a docstring cannot fail. This can.

WHAT IT KEYS ON, and the distinction is the whole design: a conditional that names a MEMBER of
`ChatKind` is deciding something on the strength of which kind a chat is, which nothing
outside the run configurator may do. A conditional that compares two kinds to each other
(`existing.kind == body.kind`) is asking whether they MATCH — an idempotency or ownership
question — and is deliberately not in scope. Widening the rule to "any attribute called kind"
was tried and catches `part_kind`, `problem_type`, parser kinds and half of `projection.py`;
what it buys in coverage it loses in meaning.

THE PROSE-READER HALF IS GUARDED ELSEWHERE AND IS CITED, NOT REBUILT. `_looks_plan_shaped` and
its call sites are gone, and `test_plan_options.py` carries the regression guard against the
exact shapes that used to trip it — kept verbatim from a real production incident. A second copy
here would either be that symbol list again or an AST heuristic firing on every string
operation.
"""

from __future__ import annotations

import ast
import pathlib

SRC = pathlib.Path(__file__).resolve().parents[3] / "src"

ALLOWED: dict[str, str] = {
    "services/agent/toolsets.py": (
        "THE GUARDRAIL. The registry decides what the model CAN DO, which is where every "
        "difference between the kinds is supposed to live — a Plan chat cannot change the app "
        "because the write tools are absent from its list, never because something downstream "
        "noticed. The chat-kind catalogue and the tool-surface renderer are in the same module "
        "on purpose: they are two more views of the same fact."
    ),
    "services/agent/mode_prompts.py": (
        "WHAT THE MODEL IS TOLD. The second half of the run configurator: each kind gets its "
        "own purpose segment. Everything about VOICE is shared (`NARRATION_VOICE`); what "
        "varies is what the chat is for."
    ),
    "services/turns/engine.py": (
        "WHICH HARNESS RUNS THE TURN — the node loop with its per-step billing fold versus a "
        "single `chat_agent.run`. A shape, not a behaviour: unifying the two would give a Plan "
        "run the streaming node loop and the per-step billing it has no steps for."
    ),
    "api/v1/attachments/router.py": (
        "WHICH LANES THIS CHAT'S DOOR ADMITS, asked before any bytes are stored. A generic chat "
        "has no sandbox, so a file whose only reader is a script running in one cannot be "
        "accepted there — and the refusal has to name what this chat CAN take, which the "
        "code-lane sentence's offer to open it with code cannot. It decides nothing about what "
        "the model may do or is told; the toolset and the prompt are untouched by it."
    ),
    "api/v1/conversations/turns.py": (
        "WHETHER THIS TURN RESOLVES A CONTAINER AT ALL, which is the one question the route has "
        "to answer before it can ask any of the four workspace refusals. A generic chat has no "
        "project and starts no container, so those refusals would each be asking about "
        "something that does not exist — and the first of them would refuse the turn outright "
        "on a deployment with no sandbox service. The read answers a PROJECT, so the branch is "
        "made once and everything below it narrows; nothing about what the model may do or is "
        "told follows from it."
    ),
    "api/v1/conversations/schemas.py": (
        "PARENTAGE, NOT BEHAVIOUR. The create request refuses the two combinations the database "
        "constraint also refuses — a generic chat naming a project, or either other kind naming "
        "none — so a caller is answered with a sentence rather than an integrity error. A "
        "cross-field validator cannot be spelled as a field rule, and it decides nothing about "
        "the run."
    ),
    "api/v1/conversations/transition.py": (
        "IDENTITY, NOT BEHAVIOUR, and the honest reason this list is four entries rather than "
        "the two the plan expected. The handoff's idempotency predicate is 'same owner, same "
        "project, same kind, or a flat 409' — it refuses to hand back a chat that is not the "
        "Build chat it was asked for. Nothing about what the user reads, how words are "
        "filtered, or which abilities exist follows from it."
    ),
}


def _local_names_for_chat_kind(tree: ast.AST) -> set[str]:
    """Whatever this module calls `ChatKind`, including under an alias.

    A GUARD THAT ONLY KNOWS ONE SPELLING IS NOT A GUARD. `from ... import ChatKind as _CK`
    then `if kind is _CK.PLAN:` is a branch on the chat kind by any reading, and a scan keyed
    on the literal name walks straight past it — which is not a hypothetical: it is the first
    mutation this test was checked against, and it survived."""
    names = {"ChatKind"}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            names |= {a.asname or a.name for a in node.names if a.name == "ChatKind"}
        elif isinstance(node, ast.Import):
            names |= {a.asname or a.name for a in node.names if a.name.endswith("ChatKind")}
    return names


def _conditionals_naming_a_chat_kind(path: pathlib.Path) -> list[int]:
    """Line numbers of every branch in `path` whose test names a `ChatKind` member: an
    `if`/`while`/ternary condition, or a `match`, matched against whatever THIS module calls
    the enum (see `_local_names_for_chat_kind`), not the literal spelling.

    A `match` is scanned SUBJECT AND PATTERNS TOGETHER, and counted once — `match kind:` names
    the enum nowhere in its subject and everywhere in its arms, so a subject-only rule would
    report the run configurator as branch-free, quietly un-guarding the one module this test
    was written for."""
    tree = ast.parse(path.read_text())
    spellings = _local_names_for_chat_kind(tree)
    found: list[int] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.If | ast.While | ast.IfExp):
            scanned: list[ast.AST] = [node.test]
        elif isinstance(node, ast.Match):
            scanned = [node.subject, *(case.pattern for case in node.cases)]
        else:
            continue
        named: set[str] = set()
        for part in scanned:
            named |= {n.id for n in ast.walk(part) if isinstance(n, ast.Name)}
            named |= {n.attr for n in ast.walk(part) if isinstance(n, ast.Attribute)}
        if named & spellings:
            found.append(node.lineno)
    return found


def test_only_the_named_modules_decide_anything_on_a_chat_kind() -> None:
    """★ THE CENSUS, enforced: a fifth module branching on a chat kind fails here, not in review.

    WRITTEN AGAINST WHAT THE TREE DOES: the allowlist holds four modules, the fourth an identity
    check in the handoff route rather than a run-configuring branch. Trimming it out would mean
    deleting a legitimate guard or weakening this test into a superset.

    Mutation check: add `if conversation.kind is ChatKind.PLAN:` to any other module under
    `src/` and this goes red naming it."""
    offenders: dict[str, list[int]] = {}
    for path in sorted(SRC.rglob("*.py")):
        relative = path.relative_to(SRC).as_posix()
        lines = _conditionals_naming_a_chat_kind(path)
        if lines and relative not in ALLOWED:
            offenders[relative] = lines

    assert offenders == {}, (
        "these modules decide something on a chat kind and are not in the allowlist:\n"
        + "\n".join(f"  {where}: lines {lines}" for where, lines in offenders.items())
        + "\n\nBehaviour belongs in the toolset. If this branch is genuinely a run "
        "configurator, a harness shape, or an identity check, add it to ALLOWED with the "
        "reason — and expect that reason to be read."
    )


def test_every_allowlisted_module_still_has_the_branch_it_is_allowed_for() -> None:
    """The other direction, and it is what stops the allowlist rotting into decoration.

    An entry whose branch has since been deleted is a licence nobody is using, and the next
    author to add a kind conditional to that module will find the guard already waving it
    through. Every entry has to still be earning its place."""
    unused = [where for where in ALLOWED if not _conditionals_naming_a_chat_kind(SRC / where)]
    assert unused == [], (
        f"these modules are allowed to branch on a chat kind but no longer do: {unused}. "
        "Remove them from ALLOWED rather than leaving a standing permission."
    )


def test_the_projection_reads_no_chat_kind_at_all() -> None:
    """★ Pinned as its own claim, separate from the census above, because this is the guarantee
    that regressed twice.

    The reload emitter used to drop a response's prose only in a Build chat; it reads no kind
    now, so the same stored response projects the same way whichever chat it came from.

    Asserted on the IMPORT rather than a branch: a module that cannot name `ChatKind` cannot
    branch on one — stronger and simpler than enumerating the branches it does not have."""
    source = (SRC / "services/messages/projection.py").read_text()
    assert "ChatKind" not in source
