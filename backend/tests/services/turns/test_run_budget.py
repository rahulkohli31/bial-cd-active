"""U13 / R91 — a build turn is bounded by what it SPENDS, not only by requests and seconds.

THE THIRD BOUND ON ONE LOOP. `MODEL_TURN_CEILING` counts requests and
`RUN_WALL_CLOCK_DEADLINE_S` counts seconds, and a build can sit comfortably inside both while
spending a fortune — fifty requests carrying a large context are cheap in count, cheap in
elapsed time and expensive in tokens. The citizen can see the meter; the agent cannot. That
asymmetry is the whole argument for the platform holding this line, and for it being a NUMBER
rather than a sentence in a prompt.

The securing half — that this ending copies the citizen's tree before it composes a word, using
the same function the daily budget uses — lives in `test_at_limit.py`, beside the ordering tests
it has to keep intact. What is here is the bound's shape and its sentence.
"""

from __future__ import annotations

from src.config import settings
from src.services.orchestrator.constants import (
    MODEL_TURN_CEILING,
    RUN_TOKEN_BUDGET,
    RUN_WALL_CLOCK_DEADLINE_S,
)
from src.services.turns.copy import KEPT_A_COPY, SPENT_ENOUGH_TEXT


def test_the_bound_is_a_number_the_platform_holds() -> None:
    """★ R91's shape, asserted as a shape. The bound is a module constant beside the other two
    ceilings — a property of how this loop is built, not a per-deployment knob, and not
    something the agent is asked to observe.

    THE THREE COEXIST. A reader arriving at any one of them has to find the other two, because
    a build that is fine on requests and fine on seconds can still be the one that runs away."""
    assert isinstance(RUN_TOKEN_BUDGET, int)
    assert RUN_TOKEN_BUDGET > 0
    # Not a deployment knob: nothing reads it off settings, and nothing can.
    assert not hasattr(settings, "RUN_TOKEN_BUDGET")
    # And its two siblings survive — this is a third bound, never a replacement for either.
    assert MODEL_TURN_CEILING > 0
    assert RUN_WALL_CLOCK_DEADLINE_S > 0


def test_the_ending_names_no_bound_and_no_limit_the_citizen_did_not_set() -> None:
    """★ THREE BOUNDS, ONE ENDING.

    Which internal ceiling fired is not something a citizen can act on differently — the next
    move is the same message either way — and every word for it ("token budget", "request
    limit", "wall clock") is a word for the platform's problem rather than theirs, which is
    exactly what `copy.py`'s register rule exists to keep out. Which bound fired is in the
    record and the logs, where the person who can act on it looks.

    IT ALSO MUST NOT READ AS THE DAILY BUDGET. That ending says "you have used up your building
    budget for today ... you can carry on after midnight". Telling someone to wait until
    midnight when they can carry on immediately is the confusion this separation exists to
    prevent, and it is the easy mistake to make once the two endings share a function.

    Mutation check: pass `AT_LIMIT_TEXT` as the spend bound's sentence and this goes red on
    `midnight`."""
    rendered = SPENT_ENOUGH_TEXT.format(kept=KEPT_A_COPY)

    for platform_word in ("token", "budget for today", "midnight", "limit", "ceiling", "quota"):
        assert platform_word not in rendered.lower(), f"the ending names {platform_word!r}"
    # What it DOES say: the app works, and what to do next.
    assert "working" in rendered
    assert "next bit" in rendered


def test_the_ending_carries_the_conditional_reassurance_rather_than_asserting_it() -> None:
    """`{kept}` is the same field the daily-budget sentence uses, filled by the same securing
    function — so "your work is safe" is said only where a copy actually landed. A sentence
    that asserted it unconditionally would be a false reassurance the citizen acts on by
    closing the tab."""
    assert "{kept}" in SPENT_ENOUGH_TEXT
    assert KEPT_A_COPY in SPENT_ENOUGH_TEXT.format(kept=KEPT_A_COPY)
