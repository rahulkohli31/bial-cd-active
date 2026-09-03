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

import ast
import inspect
import pathlib

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


# --- three bounds, one ending ---------------------------------------------------------------
#
# THE UNIFICATION IS THE REQUIREMENT, not a tidy-up. R91 asks that an exhausted bound end where
# the app works AND say what remains, and it says it of the bound in general. Before this, only
# the spend arm secured anything: the other two told the citizen "your changes are still in the
# workspace — click Save to keep them" and then ended the turn having copied nothing, which is
# verbatim the sentence `at_limit_ending`'s own docstring records as securing nothing and
# asserting something nobody had checked.

_BOUNDED_REASONS = {
    "wall_clock_deadline_exceeded",
    "request_limit",
    "run_budget_reached",
}


def _engine_source() -> str:
    from src.services.turns import engine as engine_module

    return pathlib.Path(inspect.getfile(engine_module)).read_text()


def _bounded_raises() -> dict[str, ast.Raise]:
    """Every `raise _WriteEndedError("<a bounded reason>", ...)` in the write loop, by reason."""
    found: dict[str, ast.Raise] = {}
    for node in ast.walk(ast.parse(_engine_source())):
        if not isinstance(node, ast.Raise) or not isinstance(node.exc, ast.Call):
            continue
        callee = node.exc.func
        if not (isinstance(callee, ast.Name) and callee.id == "_WriteEndedError"):
            continue
        if not node.exc.args:
            continue
        reason = node.exc.args[0]
        if not isinstance(reason, ast.Constant) or not isinstance(reason.value, str):
            continue
        if reason.value in _BOUNDED_REASONS:
            found[reason.value] = node
    return found


def test_all_three_internal_bounds_end_through_the_one_securing_function() -> None:
    """★ R91's "three bounds, one ending", asserted structurally rather than by reading copy.

    Each of the three ceilings that can end a run must hand its message to
    `_bounded_run_ending`, which is the only thing on this path that secures the citizen's tree
    before composing a word. A second securing call site is the failure this pins: a divergent
    snapshot-then-teardown ordering here loses somebody's work, which is exactly why
    `at_limit_ending` takes its sentence as a parameter instead of each arm growing a copy.

    STRUCTURAL, BECAUSE THE COPY IS SHARED. All three now render the same sentence, so a test
    that only read the message could not tell an arm that secures from one that does not.

    Mutation check: restore any one arm to a bare string literal and this goes red naming it."""
    raises = _bounded_raises()
    assert set(raises) == _BOUNDED_REASONS, (
        f"missing bounded arms: {_BOUNDED_REASONS - set(raises)}"
    )

    for reason, node in sorted(raises.items()):
        assert isinstance(node.exc, ast.Call)
        message = node.exc.args[1]
        assert isinstance(message, ast.Await), f"{reason} does not await its ending"
        call = message.value
        assert isinstance(call, ast.Call), f"{reason} awaits something other than a call"
        attribute = call.func
        assert isinstance(attribute, ast.Attribute), f"{reason} does not call a method"
        assert attribute.attr == "_bounded_run_ending", (
            f"{reason} composes its own ending ({attribute.attr}) instead of going through the "
            "one function that secures the tree first"
        )


def test_the_click_save_sentence_survives_only_where_it_is_still_true() -> None:
    """★ THE REGRESSION THAT PROTECTS THE INCIDENT PATH.

    "Your changes are still in the workspace — click Save to keep them" is the sentence
    `at_limit_ending` was built to replace. On the three bounded endings it secured nothing:
    whether the work survived depended on an exit-path autosave that is deliberately swallowed,
    so on the day it failed the citizen had already been told it had not. Two of those three
    still carried it, on the paths where a container is most likely to be wedged.

    ONE SITE KEEPS IT, AND KEEPS IT HONESTLY. The self-heal budget arm
    (`self_heal_budget_exhausted`) is a FOURTH bound that R91 does not name — it ends a repair
    loop, not a run — and it secures nothing on purpose: KTD-5e says there is no autosave, so
    on that path the changes really do sit in the workspace until the citizen clicks Save. The
    sentence is accurate there. Unifying it would mean deciding something R91 never decided,
    and would quietly add a container round trip to a path that never had one.

    So this is an ALLOWLIST, not an absence: the phrase may appear in exactly one ending, and a
    fifth copy — the way this comes back — fails naming its line. Whether that fourth arm should
    also secure the tree is a real question, and it belongs to whoever owns the self-heal budget,
    not to a test that would answer it by going green.

    LITERALS, NOT RAW SOURCE. The arms that no longer carry the sentence now carry a comment
    explaining why, and a substring scan cannot tell prose about a defect from the defect — it
    flagged the very comment recording the fix. Module and function docstrings are skipped for
    the same reason; nothing else is.

    Mutation check: paste the sentence into any other ending and this goes red on its line."""
    tree = ast.parse(_engine_source())
    docstrings = {
        ast.get_docstring(node, clean=False)
        for node in ast.walk(tree)
        if isinstance(node, ast.Module | ast.ClassDef | ast.FunctionDef | ast.AsyncFunctionDef)
    }
    permitted = {
        node.exc.args[1].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Raise)
        and isinstance(node.exc, ast.Call)
        and isinstance(node.exc.func, ast.Name)
        and node.exc.func.id == "_WriteEndedError"
        and len(node.exc.args) > 1
        and isinstance(node.exc.args[0], ast.Constant)
        and node.exc.args[0].value == "self_heal_budget_exhausted"
        and isinstance(node.exc.args[1], ast.Constant)
        and isinstance(node.exc.args[1].value, str)
    }
    carriers = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and "click Save to keep them" in node.value
        and node.value not in docstrings
        and node.value not in permitted
    ]
    assert not carriers, (
        "an ending promises a save it never performs, at line(s) "
        f"{sorted(node.lineno for node in carriers)}"
    )
    # LIVENESS: the allowlisted site still exists. If the self-heal arm is ever reworded, this
    # test would otherwise keep passing while guarding nothing at all.
    assert any("click Save to keep them" in text for text in permitted), (
        "the allowlisted self-heal ending no longer carries the sentence — narrow this guard"
    )


def test_which_bound_fired_stays_in_the_record_while_the_citizen_reads_one_sentence() -> None:
    """R91 asks for one ending, not for the platform forgetting which ceiling fired. The three
    reasons stay distinct in `end_reason` — that is where the person who can act on it looks —
    while nothing distinguishes them in front of the citizen, whose next move is the same
    message either way."""
    raises = _bounded_raises()
    assert len(set(raises)) == 3, "the three bounds collapsed into one record value"
    rendered = SPENT_ENOUGH_TEXT.format(kept=KEPT_A_COPY).lower()
    for reason in _BOUNDED_REASONS:
        for word in reason.split("_"):
            assert word not in rendered, f"the shared ending leaks {reason!r} at {word!r}"


# --- what the bound actually measures --------------------------------------------------------


def test_the_bound_does_not_price_a_cache_read_like_fresh_input() -> None:
    """★★ THE 2026-07-30 INCIDENT, PREVENTED ON THE SECOND CEILING TOO.

    Under pydantic-ai `input_tokens` is the grand-total prompt size and the cache buckets are
    ALREADY INSIDE it — a request with 10 fresh tokens and a 90k cache read reports
    `input_tokens == 90_010`, so `total_tokens` is 90_015. This loop re-sends the same
    instructions and tool definitions behind a cache breakpoint on every step, so a bound
    reading that raw number charges the whole prefix again per step.

    That is not hypothetical: `billable_spend`'s docstring records one calculator build booking
    956k of a 1M daily cap on 68 tokens of real fresh input, which is why the DAILY meter is
    cost-weighted. A per-run ceiling reading the raw total would have reintroduced exactly that
    on a second ceiling — ending honest builds early and measuring how many steps a build took
    rather than how much work it did, which `RUN_TOKEN_BUDGET`'s own docstring says it must not.

    Mutation check: return `usage.total_tokens` from `_run_spend` and this goes red."""
    from pydantic_ai.usage import RunUsage

    from src.services.turns.engine import _run_spend

    mostly_cached = RunUsage(
        input_tokens=90_010,  # 90k of it read from cache, 10 genuinely fresh
        output_tokens=5,
        cache_read_tokens=90_000,
        cache_write_tokens=0,
    )
    assert mostly_cached.total_tokens == 90_015  # what the naive reading would have charged
    # Weighted: 10 fresh + 5 output + 90_000/10 = 9_015.
    assert _run_spend(mostly_cached) == 9_015
    # The claim that matters is the RATIO, not the constant: a turn that is almost entirely
    # cache reads must not be charged as though it were almost entirely fresh input.
    assert _run_spend(mostly_cached) < mostly_cached.total_tokens / 5


def test_the_platform_s_thinking_is_not_charged_to_the_citizen() -> None:
    """★ THE OWNER'S RULING (2026-09-02): the meter shows what the citizen spent on their app,
    not what the platform spent thinking about it.

    Reasoning is a choice this platform made on their behalf. They did not ask for it, they
    cannot see it, and they cannot turn it off — so a daily allowance that moved because the
    platform thought harder would move for a reason the person has no way to act on.

    THE SUBTRACTION IS SOUND BECAUSE THE PROVIDER BILLS THINKING *INSIDE* `output_tokens`
    rather than beside it, so `thinking_tokens` is a readable subset of the output total. The
    first assertion is what would catch someone "fixing" this by adding instead: adding would
    make a reasoning turn cost MORE than its own output.

    Mutation check: pass `usage.output_tokens` straight through in `_citizen_output_tokens` and
    the second assertion goes red; add instead of subtract and the third does."""
    from pydantic_ai.usage import RunUsage

    from src.services.turns.engine import _citizen_output_tokens, _run_spend

    thought_hard = RunUsage(
        input_tokens=1_000,
        output_tokens=900,  # of which 700 was the platform thinking
        cache_read_tokens=0,
        cache_write_tokens=0,
        details={"thinking_tokens": 700},
    )
    # A SUBSET, never an addition — the fixture is only honest if this holds.
    assert thought_hard.details["thinking_tokens"] < thought_hard.output_tokens
    assert _citizen_output_tokens(thought_hard) == 200
    # 1_000 fresh input + 200 of the citizen's own output. The 700 the platform spent thinking
    # is charged to nobody.
    assert _run_spend(thought_hard) == 1_200

    # AND A TURN THAT DID NOT THINK IS UNCHANGED, which is the half that proves the subtraction
    # is reading a real key rather than discounting every turn. The provider OMITS the key
    # entirely when a response used no thinking, so this is the ordinary shape and not an edge.
    plain = RunUsage(input_tokens=1_000, output_tokens=900)
    assert "thinking_tokens" not in plain.details
    assert _citizen_output_tokens(plain) == 900
    assert _run_spend(plain) == 1_900


def test_the_run_bound_and_the_daily_meter_weigh_a_token_identically() -> None:
    """ONE POLICY, TWO READERS. The daily meter is a SQL column expression and the run bound is
    an in-process scalar, so they cannot share an implementation — but they must not drift into
    two numbers the citizen hears the same word for. Both spell the weighting from the same two
    divisors, and this pins that they agree on real arithmetic rather than merely importing the
    same constants.

    Mutation check: change either divisor in `weighted_spend` alone and this goes red."""
    from src.services.usage.gate import (
        _CACHE_READ_DIVISOR,
        _CACHE_WRITE_SURCHARGE_DIVISOR,
        weighted_spend,
    )

    fresh, output, read, write = 1_000, 500, 40_000, 8_000
    expected = (
        fresh
        + output
        + read / _CACHE_READ_DIVISOR
        + write
        + write / _CACHE_WRITE_SURCHARGE_DIVISOR
    )
    assert weighted_spend(
        input_tokens=fresh + read + write,  # the grand total, as pydantic-ai reports it
        output_tokens=output,
        cache_read_tokens=read,
        cache_write_tokens=write,
    ) == int(expected)
