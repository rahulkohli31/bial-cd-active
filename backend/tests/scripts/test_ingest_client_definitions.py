"""The one branch of a review round that decides what the agent is allowed to believe.

Every other answer in a round changes a definition. This one changes nothing and marks everything:
the client has said they cannot define the column, so our inferred sentence stays and loses its
borrowed authority. If either half of that marking goes missing, the schema block reads a guess of
ours exactly as it reads an answer of theirs, and no later stage can tell the two apart.
"""

from __future__ import annotations

from typing import Any

from scripts.ingest_client_definitions import QUESTION_UNDEFINED, fold


def test_an_answer_that_is_not_one_keeps_our_text_and_marks_it_as_ours() -> None:
    """`DOM_INT_OPS` is the column this bites on: a name too long to look opaque, over a value set
    (`'DOM'`, `'INT'`) whose obvious reading is the one the client declined to confirm."""
    column: dict[str, Any] = {
        "name": "DOM_INT_OPS",
        "definition": "Domestic international.",
        "source": "INFERRED — placeholder",
    }

    change = fold(column, {"answer": "No Idea", "status": "Please verify"}, "v2")

    assert change is not None and change.kind == "client cannot define it"
    assert column["definition"] == "Domestic international."
    assert column["source"] == "INFERRED — BIAL could not define (v2)"
    assert column["question"] == QUESTION_UNDEFINED
    # WITHOUT THIS MARK THE WARNING IS UNREACHABLE. The renderer shows a definition when the NAME
    # looks opaque and this name does not, so an unmarked column renders as a bare value list with
    # nothing beside it for the warning to attach to.
    assert column["gloss"] is True
