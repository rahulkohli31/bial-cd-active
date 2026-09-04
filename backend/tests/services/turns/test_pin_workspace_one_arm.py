"""U13 — the canvas divergence on the sandbox in planning, asserted instead of described.

WHAT THIS PROVES, AND WHY A COMMENT COULD NOT. The `Removals` board removes "the sandbox in
planning": a Plan chat is drawn as reading the latest copy of the app and no longer holding a
container. The shipped code does the opposite — `_pin_workspace` resolves the project's LIVE
container for BOTH kinds, with one arm and no branch — and that disagreement was settled in the
CODE's favour, so the board is the stale artifact. A dated sentence saying so cannot fail, and a
sentence that cannot fail is exactly how the last census of this method went wrong (see
`test_no_prose_readers.py`, which exists because two docstrings disagreed about a count). This
can fail: the day someone re-introduces the branch the board describes, this goes red and they
have to reconcile the two before it lands.

IT IS A STRUCTURAL ASSERTION ON PURPOSE. The behavioural coverage already exists — the write-turn
and turn-stream suites drive real turns of both kinds through the live container. What none of
them can say is "there is ONE arm here": a branch that returned a live workspace on both sides
would keep every one of those tests green while re-establishing the very shape R18 removed. So
this reads the method's AST and asserts the absence of a fork, which is the property, rather than
sampling the outcomes a fork would still produce.

WHEN TO DELETE THIS FILE: when the board row is redrawn to match what ships. The marker in
`_pin_workspace`'s docstring points here and goes with it.
"""

from __future__ import annotations

import ast
import pathlib

ENGINE = pathlib.Path(__file__).resolve().parents[3] / "src" / "services" / "turns" / "engine.py"

MARKER_CLASS = "canvas-divergence"


def _pin_workspace_node(source: str) -> ast.AsyncFunctionDef:
    """The `_pin_workspace` definition, or a hard failure naming what was found instead.

    Walks the whole tree rather than indexing a known class body: a method that moved to another
    class would otherwise make this test vanish silently, which is the failure mode a guard must
    not have."""
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "_pin_workspace":
            return node
    raise AssertionError(
        "`_pin_workspace` is not in engine.py any more. If the turn-pinned read surface moved, "
        "re-point this guard; if it was removed, the board row it contradicts may finally be "
        "right and this file goes with the marker in its docstring."
    )


def _forks(node: ast.AST) -> list[str]:
    """Every conditional inside the body — the shape this method is asserted NOT to have.

    `if`, `match` and a conditional expression all count: the branch R18 removed was an `if`, but
    a ternary picking between two workspace classes is the same defect written smaller. A `try`
    is not a fork — it selects on failure, not on what kind of chat this is."""
    found: list[str] = []
    for child in ast.walk(node):
        if isinstance(child, ast.If):
            found.append(f"if at line {child.lineno}")
        elif isinstance(child, ast.Match):
            found.append(f"match at line {child.lineno}")
        elif isinstance(child, ast.IfExp):
            found.append(f"conditional expression at line {child.lineno}")
    return found


def _returned_calls(node: ast.AST) -> list[str]:
    """The name called by each `return`, so "returns one kind of workspace" is checkable."""
    names: list[str] = []
    for child in ast.walk(node):
        if isinstance(child, ast.Return) and isinstance(child.value, ast.Call):
            func = child.value.func
            names.append(func.id if isinstance(func, ast.Name) else ast.unparse(func))
    return names


def test_pin_workspace_still_has_exactly_one_arm() -> None:
    node = _pin_workspace_node(ENGINE.read_text(encoding="utf-8"))
    assert _forks(node) == [], (
        "`_pin_workspace` has grown a branch. The `Removals` board's 'the sandbox in planning' "
        "row wants a Plan chat reading a saved copy; R18 removed that arm and R0 puts "
        "architecture outside canvas authority. Reconcile the board before re-adding it."
    )
    assert _returned_calls(node) == ["LiveSandboxWorkspace"], (
        "`_pin_workspace` no longer resolves the project's live container as its single answer."
    )


def test_the_divergence_marker_travels_with_the_method() -> None:
    """The marker is the grep entry point; losing it loses the pointer to the audit record."""
    doc = ast.get_docstring(_pin_workspace_node(ENGINE.read_text(encoding="utf-8"))) or ""
    assert MARKER_CLASS in doc, (
        "the canvas-divergence marker is gone from `_pin_workspace`'s docstring while the "
        "divergence itself is still here"
    )


def test_the_guard_can_actually_fail() -> None:
    """Mutation-proofing. Both helpers above are absence checks, and an absence check whose
    finder is broken is green forever — the exact false-green this repo pairs every
    `toEqual([])` with a liveness assertion to avoid."""
    branched = ast.parse(
        "async def _pin_workspace(self):\n"
        "    if kind is ChatKind.PLAN:\n"
        "        return SnapshotWorkspace(x)\n"
        "    return LiveSandboxWorkspace(y)\n"
    )
    ternary = ast.parse(
        "async def _pin_workspace(self):\n"
        "    return SnapshotWorkspace(x) if plan else LiveSandboxWorkspace(y)\n"
    )
    assert len(_forks(branched)) == 1
    assert sorted(_returned_calls(branched)) == ["LiveSandboxWorkspace", "SnapshotWorkspace"]
    assert len(_forks(ternary)) == 1
    # ...and the live source still parses through the same finder, so a walker that silently
    # matched nothing could not produce the green above.
    assert _returned_calls(_pin_workspace_node(ENGINE.read_text(encoding="utf-8")))
