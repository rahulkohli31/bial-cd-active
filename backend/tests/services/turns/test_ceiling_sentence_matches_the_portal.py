"""The ceiling sentence a live BIAL Chat reply shows is the one the portal shows after a reload.

Reads `portal/`, which the gates image does not copy in; deselected there by name.
"""

from __future__ import annotations

from pathlib import Path

from src.services.turns.copy import ANALYSIS_CEILING_TEXT

_MESSAGE_TYPES = (
    Path(__file__).resolve().parents[4] / "portal" / "src" / "utils" / "messageTypes.ts"
)


def test_the_ceiling_sentence_is_the_portals_word_for_word() -> None:
    assert f"'{ANALYSIS_CEILING_TEXT}'" in _MESSAGE_TYPES.read_text(encoding="utf-8")
