"""The one sentence the platform says about attachments, in both languages that say it.

A constant is only shared if something checks that it is. This sentence is written twice — once
in Python for the refusals and the help text, once in TypeScript for the composer — because the
two halves ship as separate builds, and the drift it guards against is exactly what happened to
the rule it replaced: three copies each telling a citizen something slightly different about what
they could attach.
"""

from __future__ import annotations

from pathlib import Path

from src.api.v1.attachments.router import ATTACHMENT_LANES_SENTENCE

#: `backend/tests/api/v1/attachments/` up to the repository root, then down into the portal.
_PORTAL = Path(__file__).resolve().parents[5] / "portal" / "src" / "utils" / "attachmentInput.ts"
_DECLARATION = "export const ATTACHMENT_LANES_SENTENCE"


def _portal_sentence() -> str:
    """The TypeScript constant, read out of the source the browser is built from.

    Read rather than imagined: a test that cannot see the file it compares against is not a
    cross-language check at all. The declaration spans two string literals joined by `+`, so the
    quoted halves are collected until a line stops continuing — the same join the compiler makes.
    """
    lines = _PORTAL.read_text(encoding="utf-8").splitlines()
    start = next((i for i, line in enumerate(lines) if line.startswith(_DECLARATION)), None)
    assert start is not None, f"{_DECLARATION} is not declared in {_PORTAL}"

    parts: list[str] = []
    for line in lines[start:]:
        quoted = line.split('"')
        if len(quoted) >= 3:
            parts.append(quoted[1])
        if quoted[-1].rstrip().endswith("+"):
            continue
        if parts:
            break
    return "".join(parts)


def test_the_two_copies_of_the_lanes_sentence_are_byte_identical() -> None:
    """★ THE CLAIM THE DOCSTRING BESIDE THE CONSTANT MAKES, now true.

    The sentence is shown by the composer, by the help page and by every unsupported-format
    refusal. Two of those come from the server and one from the browser, so nothing short of a
    test spanning both trees can tell that they still agree — and a citizen meeting two slightly
    different explanations of one rule is how the rule this replaced lost its meaning.

    Mutation receipt: change a word in either copy and this fails showing both.
    """
    assert _portal_sentence() == ATTACHMENT_LANES_SENTENCE
