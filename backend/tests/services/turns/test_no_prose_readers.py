"""U14 / R92 / N2 — the census of what may branch on a chat kind, enforced instead of asserted.

WHY A GUARD AND NOT A SENTENCE. Two comments in this codebase used to state this census — one
on `ChatKind` itself ("exactly two readers"), one in the turn engine ("three ... the closed
set") — and they were both wrong, they disagreed with each other, and neither could go red. A
number in a docstring cannot fail. This can.

WHAT IT KEYS ON, and the distinction is the whole design: a conditional that names a MEMBER of
`ChatKind` is deciding something on the strength of which kind a chat is, which is what N2
forbids outside the run configurator. A conditional that compares two kinds to each other
(`existing.kind == body.kind`) is asking whether they MATCH — an idempotency or ownership
question — and is deliberately not in scope. Widening the rule to "any attribute called kind"
was tried and catches `part_kind`, `problem_type`, parser kinds and half of `projection.py`;
what it buys in coverage it loses in meaning.

THE PROSE-READER HALF IS PLAN B'S AND IS CITED, NOT REBUILT. `_looks_plan_shaped` and its call
sites are gone, and `test_plan_options.py` carries the regression guard against the exact
shapes that used to trip it — kept verbatim from a real production incident. A second copy here
would either be B's symbol list again or an AST heuristic that fires on every string operation.
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
    """Line numbers of every branch in `path` whose test names a `ChatKind` member.

    `if` / `while` / a ternary's condition, and a `match` — the four places Python puts a value
    under a decision.

    Matched against whatever THIS module calls the enum (see `_local_names_for_chat_kind`),
    not against the literal spelling.

    A `match` is scanned SUBJECT AND PATTERNS TOGETHER, and counted once. `match kind:` names
    the enum nowhere in its subject and everywhere in its arms, so a subject-only rule reports
    the run configurator as having no branch at all — which is how a guard quietly stops
    guarding the one module it was written for."""
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
    """★ N2, enforced. A fifth module branching on a chat kind fails here rather than passing a
    review — which is what "behaviour lives in the toolset, not in branches" has to mean if it
    is to survive the next feature.

    WRITTEN AGAINST THE HONEST LIST. The plan expected two entries; the tree has four, and the
    fourth is an identity check in the handoff route. Trimming the list to two would have meant
    either deleting a legitimate ownership guard or weakening this test into a superset, and a
    guard that cannot pass gets weakened until it proves nothing.

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
    """★ U4's verification, pinned as its own claim because it is the one that regressed twice.

    The reload emitter used to drop a response's prose only in a Build chat. It reads no kind
    now — not in a conditional, not anywhere — so the same stored response projects the same
    way whichever chat it came from, which is what AE43 is about.

    Asserted on the IMPORT rather than on a branch: a module that cannot name `ChatKind` cannot
    branch on one, and that is a stronger and simpler claim than enumerating the branches it
    does not have."""
    source = (SRC / "services/messages/projection.py").read_text()
    assert "ChatKind" not in source
