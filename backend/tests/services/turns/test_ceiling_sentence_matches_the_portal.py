"""The sentences a live reply ends with are the ones the portal shows after a reload.

Reads `portal/`, which the gates image does not copy in; deselected there by name.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.services.turns.copy import (
    ANALYSIS_CEILING_TEXT,
    ATTACHMENT_TOO_LARGE_TEXT,
    CHAT_TOO_LONG_TEXT,
)
from src.services.turns.engine import DOCUMENT_TOO_LONG_TEXT

_MESSAGE_TYPES = (
    Path(__file__).resolve().parents[4] / "portal" / "src" / "utils" / "messageTypes.ts"
)


def test_the_ceiling_sentence_is_the_portals_word_for_word() -> None:
    assert f"'{ANALYSIS_CEILING_TEXT}'" in _MESSAGE_TYPES.read_text(encoding="utf-8")


@pytest.mark.parametrize(
    "text",
    [CHAT_TOO_LONG_TEXT, DOCUMENT_TOO_LONG_TEXT, ATTACHMENT_TOO_LARGE_TEXT],
    ids=["chat_too_long", "document_too_long", "attachment_too_large"],
)
def test_each_refusal_sentence_is_the_portals_word_for_word(text: str) -> None:
    """Live, the citizen reads the server's sentence; after a reload, the portal's copy table's."""
    assert f"'{text}'" in _MESSAGE_TYPES.read_text(encoding="utf-8")
