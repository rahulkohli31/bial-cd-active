"""THE FIFTH LINK OF THE REMOVAL TRACE, made mechanical (backend half).

The repo's retire-a-behaviour convention has five links: the surface · the navigation payloads ·
the consumers and imports · the tests-become-inertness-guards · and the human-facing copy,
INCLUDING COMMENTS. The relay's removal completed four of them the first time, and the fifth is
where the damage was: `services/agent/agent.py` told the next reader that the `kind is None`
branch "retires with the relay", when a shipping endpoint (`description:generate`) runs on it.
That comment could have taken a live feature out with the dead one.

A comment cannot be asserted, so this asserts the one thing that can be: a mention of something
DELETED has to read as history. Naming what code replaced is often the clearest way to explain
its shape, so the names are allowed — the sentence around them has to say the thing is gone.

WHY A MARKER LIST RATHER THAN REAL PROSE ANALYSIS: this has to be cheap enough to survive, and
wrong in the harmless direction. A present-tense sentence that happens to contain "legacy" slips
through; that is a miss. A guard that cried wolf would be deleted inside a month and the next
wholesale deletion would leave this link undone again.

Its sibling is `portal/src/__tests__/retired-names-are-past-tense.test.ts`.
"""

from __future__ import annotations

from pathlib import Path

SRC = Path(__file__).resolve().parents[4] / "src"

# Unambiguous identifiers only. A generic word like "relay" appears in live contexts (the C3
# progress relay, the BRAIN→SESSION-API relay) and scanning for it would produce pure noise.
RETIRED = (
    "v1/claude",
    "api.v1.claude",
    "claude/router.py",
    "claude/prompts.py",
    "to_model_content",
    "services/agent/content",
)

# Deliberately generous: a miss is cheaper than a false alarm.
HISTORICAL = (
    "used to",
    "was ",
    "were ",
    "had ",
    "died",
    "dies with",
    "deleted",
    "retired",
    "removed",
    "gone",
    "no longer",
    "until",
    "before",
    "predates",
    "legacy",
    "old ",
    "since",
    "outlived",
    "replaced",
)


def _window(lines: list[str], index: int) -> str:
    """The mention's line plus two either side — a docstring sentence rarely fits on one."""
    return " ".join(lines[max(0, index - 2) : index + 3]).lower()


def test_no_source_file_mentions_a_retired_name_in_the_present_tense() -> None:
    offenders: list[str] = []
    for file in sorted(SRC.rglob("*.py")):
        lines = file.read_text(encoding="utf-8").splitlines()
        for index, line in enumerate(lines):
            for name in RETIRED:
                if name not in line:
                    continue
                if not any(marker in _window(lines, index) for marker in HISTORICAL):
                    rel = file.relative_to(SRC)
                    offenders.append(f"{rel}:{index + 1}: {name} — {line.strip()[:90]}")
    assert offenders == []


def test_the_guard_can_actually_fail() -> None:
    """Mutation-proofing. If the marker list ever grew to match everything, the check above
    would be green forever and this file would be worse than nothing."""
    present = ["# The chat relay (`/v1/claude`) carries no CSRF token."]
    historical = ["# The `/v1/claude` relay was retired; nothing carries that contract now."]

    def flags(lines: list[str]) -> bool:
        return "v1/claude" in lines[0] and not any(m in _window(lines, 0) for m in HISTORICAL)

    assert flags(present) is True
    assert flags(historical) is False
