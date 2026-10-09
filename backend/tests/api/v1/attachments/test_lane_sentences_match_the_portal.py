"""The two lane sentences and the three size limits, pinned across the stack.

A citizen reads one of these in the composer and the other in a refusal, often in the same
minute, so both halves have to carry the same words to the byte. Nothing compiles across the
two languages: the backend constants are IMPORTED and the portal's are READ OFF ITS SOURCE,
because deriving both from one place would let a mutation agree with itself.

THE READER UNDERSTANDS ONE FORM PER KIND: quoted string literals joined by `+` for a sentence, a
bare integer literal for a limit. Rewrite a declaration in any other form, or reach for a
backslash escape outside the few below, and this goes red naming the declaration it could not
read — a reformat, not a drift.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Final

import pytest

from src.api.v1.attachments.router import (
    ATTACHMENT_LANES_SENTENCE,
    GENERIC_ATTACHMENT_LANES_SENTENCE,
)
from src.services.media.lanes import CODE_LANE_MAX_MB, IMAGE_MAX_MB, PDF_MAX_MB

_ATTACHMENT_INPUT: Final = (
    Path(__file__).resolve().parents[5] / "portal" / "src" / "utils" / "attachmentInput.ts"
)

_LITERAL: Final = re.compile(r"""\s*(?:"((?:[^"\\]|\\.)*)"|'((?:[^'\\]|\\.)*)')""")
_JOIN: Final = re.compile(r"\s*\+")
_ESCAPES: Final = {"\\": "\\", "'": "'", '"': '"', "n": "\n", "t": "\t"}


def _unescape(body: str) -> str:
    """The characters a TypeScript literal's body stands for."""
    out: list[str] = []
    index = 0
    while index < len(body):
        char = body[index]
        if char != "\\":
            out.append(char)
            index += 1
            continue
        escape = body[index + 1]
        assert escape in _ESCAPES, (
            f"`{_ATTACHMENT_INPUT.name}` spells a lane sentence with the escape `\\{escape}`, "
            "which this reader does not decode — teach it that escape rather than loosening "
            "the comparison"
        )
        out.append(_ESCAPES[escape])
        index += 2
    return "".join(out)


def _sentence(name: str) -> str:
    """The portal's value for `name`, assembled from the literals its declaration joins.

    Read off disk rather than mirrored here: a copy in this file would be a third place the
    words live, and the drift this exists to catch would be free to happen between them."""
    source = _ATTACHMENT_INPUT.read_text(encoding="utf-8")
    head = f"export const {name} ="
    assert head in source, f"`{_ATTACHMENT_INPUT.name}` no longer exports `{name}`"
    rest = source.split(head, 1)[1]

    parts: list[str] = []
    position = 0
    while True:
        found = _LITERAL.match(rest, position)
        assert found is not None, (
            f"`{name}` is no longer written as quoted string literals joined by `+`, so this "
            "reader cannot see its value and the comparison below would be vacuous"
        )
        body = found.group(1) if found.group(1) is not None else found.group(2)
        parts.append(_unescape(body))
        position = found.end()
        joined = _JOIN.match(rest, position)
        if joined is None:
            return "".join(parts)
        position = joined.end()


def test_the_builder_lane_sentence_is_the_portal_s_word_for_word() -> None:
    """The composer's tooltip and the upload route's refusal are the same sentence to a
    citizen; the backend test beside this one compares a response to this constant, which
    holds the server to itself and says nothing about what the browser shows."""
    assert _sentence("ATTACHMENT_LANES_SENTENCE") == ATTACHMENT_LANES_SENTENCE


def test_the_generic_lane_sentence_is_the_portal_s_word_for_word() -> None:
    """BIAL Chat with no analysis runtime promises less because it can do less, so the two
    sides are each other's only witness."""
    assert _sentence("GENERIC_ATTACHMENT_LANES_SENTENCE") == GENERIC_ATTACHMENT_LANES_SENTENCE


def _limit(name: str) -> int:
    """The portal's value for `name`, written as a bare integer literal."""
    source = _ATTACHMENT_INPUT.read_text(encoding="utf-8")
    found = re.search(rf"^export const {name} = (\d+)$", source, re.MULTILINE)
    assert found is not None, (
        f"`{_ATTACHMENT_INPUT.name}` no longer exports `{name}` as a bare integer literal, so "
        "this reader cannot see its value and the comparison below would be vacuous"
    )
    return int(found.group(1))


@pytest.mark.parametrize(
    ("name", "server_limit"),
    [
        ("IMAGE_MAX_MB", IMAGE_MAX_MB),
        ("CHAT_FILES_MAX_MB", PDF_MAX_MB),
        ("CODE_LANE_MAX_MB", CODE_LANE_MAX_MB),
    ],
)
def test_each_size_limit_is_the_portal_s_number_for_number(name: str, server_limit: int) -> None:
    """The browser refuses an oversized file before it uploads. Set lower than the server's, it
    refuses files the server takes; set higher, a citizen waits out an upload that is refused.
    The portal's total for a chat's pictures and PDFs is also its largest PDF."""
    assert _limit(name) == server_limit
