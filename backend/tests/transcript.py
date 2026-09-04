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

`live_shape` and `reload_shape` answer the third question — whether a WATCHING tab and a
RELOADED one end up reading the same thing. They live together here because the only way that
comparison means anything is if both sides are reduced by one pair of functions that agree on
what "on the screen" is; two files each keeping their own copy is how the two orders drift
apart in the first place.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

from src.api.v1.conversations.schemas import StepFrame, TextDeltaFrame, TurnStreamFrame
from src.services.messages.projection import AssistantTextItem, DisplayItem, StepItem
from src.services.turns.engine import TEXT_BLOCK_SEPARATOR


class _HasTextBlocks(Protocol):
    """The one thing this module needs of a turn state, so a test double works too."""

    def text_blocks(self) -> list[str]: ...


class _HasRing(Protocol):
    """Likewise for the frames a turn has put on the wire."""

    @property
    def ring(self) -> Sequence[TurnStreamFrame]: ...


def rendered_text(state: _HasTextBlocks) -> str:
    """Every block of prose the turn has given the citizen, as one readable string."""
    return TEXT_BLOCK_SEPARATOR.join(state.text_blocks())


def live_shape(state: _HasRing) -> list[str]:
    """The live feed reduced to what a watching tab still shows, in order.

    Text and steps only: this is the sequence a reload has to reproduce, and comparing shapes
    rather than raw frames keeps the two comparable across the frame types only one side has
    (workspace, compile, preview, and the working status that reasoning raises).

    REDUCED THE WAY THE BROWSER REDUCES IT, which is the half that makes the comparison mean
    anything. A step arrives more than once on the same `tool_call_id` and the later frame
    REPLACES the earlier one in place, so the last frame for an id is what the tab is showing
    when the turn ends — and a step whose last frame is hidden has left the screen. Counting
    `started` frames alone would report a row as present however it was later withdrawn, which
    is exactly the drift between a watching tab and a reloaded transcript these comparisons
    exist to catch."""
    order: list[tuple[str, str]] = []  # ("text", the words) | ("step", the tool_call_id)
    steps: dict[str, StepItem] = {}
    for frame in state.ring:
        if isinstance(frame, TextDeltaFrame):
            text = frame.text.strip()
            if text:
                order.append(("text", text))
        elif isinstance(frame, StepFrame):
            if frame.tool_call_id not in steps:
                order.append(("step", frame.tool_call_id))
            steps[frame.tool_call_id] = frame.item
    shape: list[str] = []
    for kind, value in order:
        if kind == "text":
            shape.append(f"text:{value}")
        elif not steps[value].hidden:
            shape.append(f"step:{steps[value].tool}")
    return shape


def reload_shape(items: Sequence[DisplayItem]) -> list[str]:
    """The same shape, read off a reloaded transcript's projected items."""
    shape: list[str] = []
    for item in items:
        if isinstance(item, AssistantTextItem):
            shape.append(f"text:{item.text.strip()}")
        elif isinstance(item, StepItem) and not item.hidden:
            shape.append(f"step:{item.tool}")
    return shape
