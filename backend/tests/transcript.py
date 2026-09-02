"""Reading a live turn's prose in a test, the way the citizen reads it.

A turn's state holds its content as ORDERED PARTS — blocks of prose interleaved with the
steps that ran between them — because the live feed and a reloaded transcript have to put
them in the same order. Most assertions here care about one of two things:

* WHAT was said, in which case `rendered_text` gives the blocks joined the way the browser
  draws them, and a substring check reads naturally; or
* WHERE it was said, in which case `state.text_blocks()` is the list to compare against
  directly — an equality on the list is what pins the order this plan exists to fix, and a
  joined string would pass whether or not the blocks were interleaved correctly.

Joined with the engine's own separator rather than an empty string: the blocks are separate
paragraphs, and concatenating them raw runs the last sentence of one into the first word of
the next — which is the defect that constant exists to prevent.
"""

from __future__ import annotations

from typing import Protocol

from src.services.turns.engine import TEXT_BLOCK_SEPARATOR


class _HasTextBlocks(Protocol):
    """The one thing this module needs of a turn state, so a test double works too."""

    def text_blocks(self) -> list[str]: ...


def rendered_text(state: _HasTextBlocks) -> str:
    """Every block of prose the turn has given the citizen, as one readable string."""
    return TEXT_BLOCK_SEPARATOR.join(state.text_blocks())
