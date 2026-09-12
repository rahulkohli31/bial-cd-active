"""The schema block must not state a confident falsehood about the client's data.

WHAT THIS FILE IS FOR. `src/services/agent/connector_catalogue/dice.txt` is the whole of what the
platform tells a citizen's agent about the connected system's columns, and an agent believes it. A
sentence in it that is subtly wrong — a value that is not really stored, a count read as a total,
a padded value quietly trimmed — produces an app that builds green, runs, and reports the wrong
number, with nothing anywhere to catch it. So every claim the block makes about a column is
asserted here against `data/connectors/dice/profile.json`, which is the record of what was
actually read off the lake.

THREE KINDS OF TEST, AND THEY FAIL FOR DIFFERENT REASONS.

  CURRENCY   the shipped file is what the generator produces today. Fails when somebody edits the
             artefact by hand or changes the generator without regenerating.
  CLAIMS     every name, value and count in the shipped text is true of the profile. Fails when
             the generator learns to say something the data does not support. This is the class
             that matters, and `test_a_fabricated_value_is_caught` proves the check can fail.
  SAMPLES    each rendering branch produces the text a reader expects. Six defects in this feature
             were found by READING the output, so the branches are pinned as text rather than as
             structure.

WHY THE CLAIM CHECKS ARE FUNCTIONS AND NOT INLINE ASSERTIONS. A checker that is only ever run
against correct input is a checker nobody has seen fail. Every claim below is a function returning
its OFFENDERS, run twice: once over the shipped bytes (expecting none) and once over deliberately
corrupted text (expecting the corruption, named). `assert render == profile` is symbol equality —
it cannot tell you the sentence means more than the field does — which is exactly how an earlier
draft of this feature would have shipped `distinct_max` as a population fact.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest

from scripts.build_connector_catalogue import (
    marked_for_gloss,
    CONNECTOR_KEY,
    DATA_DIR,
    GROUPS,
    INLINE_VALUE_MAX,
    MAPPING,
    MEASURES,
    NOT_A_VOCABULARY_ABOVE,
    TARGET,
    ascii_only,
    build,
    group_of,
    load_definitions,
    load_profile,
    simple_type,
)
from tests.subprocess_env import child_env

_BACKEND = Path(__file__).resolve().parents[2]

PROFILE: dict[str, Any] = load_profile()
COLUMNS: dict[str, dict[str, Any]] = {str(c["name"]): c for c in PROFILE["columns"]}
DEFINITIONS: tuple[tuple[str, str], ...] = load_definitions()
DESCRIBED: tuple[str, ...] = tuple(name for name, _ in DEFINITIONS)


def shipped() -> str:
    """The artefact's bytes, decoded WITHOUT newline translation.

    `newline=""` matters the way it does in the golden template's own generated-file test:
    Python's universal-newline decoding would turn a CRLF checkout into `\\n` and hide the very
    defect the root `.gitattributes` rule exists to prevent."""
    return TARGET.read_text(encoding="utf-8", newline="")


SHIPPED = shipped()


def column_lines(text: str) -> dict[str, str]:
    """name -> its rendered line, for every column line in the block.

    A column line is one that starts with an upper-case identifier followed by a space or the end
    of the line. That excludes the `## group -- label` headings, the prose paragraphs (which wrap
    and start with words, but whose first token is never a bare column name followed by nothing)
    and the measure lines (whose names carry spaces before the `=`)."""
    lines: dict[str, str] = {}
    for line in text.split("\n"):
        head = line.split(" ", 1)[0]
        if head in COLUMNS and (line == head or line.startswith(head + " ")):
            lines[head] = line
    return lines


# --------------------------------------------------------------------------------------------
# CURRENCY
# --------------------------------------------------------------------------------------------


def test_the_generator_reproduces_the_shipped_artefact_byte_for_byte() -> None:
    """The shipped file is a rendering of the committed inputs and nothing else.

    Shelled out to `--stdout` rather than calling `build()` in-process, for the same reason
    `sandbox/tests/test_template_reference_is_generated.py` shells out to its generator: the
    documented way to regenerate this file is the command, so the command is what has to agree
    with the file. An in-process comparison would still pass if `main()` wrote something else."""
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-B", "scripts/build_connector_catalogue.py", "--stdout"],
        cwd=_BACKEND,
        env=child_env(ENV_FILE=".env.test"),
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout == SHIPPED, (
        "the shipped catalogue is not what the generator produces. Regenerate it with "
        "`uv run python scripts/build_connector_catalogue.py` — do not hand-edit the artefact."
    )


def test_the_artefact_ships_where_the_image_can_reach_it() -> None:
    """`backend/Dockerfile` copies `src` and nothing else, so the artefact has to live under it
    and its generator inputs must not. Asserted as PATHS rather than trusted from a comment: this
    is the one packaging fact whose failure mode is a tool that refuses in production and works
    in every test."""
    assert TARGET.is_relative_to(_BACKEND / "src")
    assert not DATA_DIR.is_relative_to(_BACKEND / "src")
    assert TARGET.name == f"{CONNECTOR_KEY}.txt"


# --------------------------------------------------------------------------------------------
# CLAIMS — the anti-fabrication guard
# --------------------------------------------------------------------------------------------


def test_the_described_set_is_exactly_the_workbooks_list() -> None:
    """Counted BOTH WAYS against `definitions.json`, never against the profile.

    The profile holds 408 columns and 277 of them are deliberately not described, so a both-ways
    count against the profile is a test that cannot pass. The direction that matters here is the
    second: a column rendered that the workbook does not list is the block describing a column
    the client never agreed to."""
    rendered = set(column_lines(SHIPPED))
    assert rendered == set(DESCRIBED)
    assert len(DESCRIBED) == len(set(DESCRIBED)), "definitions.json lists a name twice"
    assert len(rendered) < len(COLUMNS), "the profile should hold more columns than are described"


def fabricated_values(text: str) -> list[str]:
    """Columns whose rendered inline values are not what the profile records — the offenders.

    COMPARED RAW, WITH NO STRIP ON EITHER SIDE. That is the whole point of the check.
    `GROUND_HANDLER` stores `'Indigo '` with a trailing space, and a generator that trimmed before
    rendering would produce `'Indigo'` — a value that is not in the column, in a block whose
    header warns about exactly this. A comparison that stripped both sides would call that
    correct."""
    offenders: list[str] = []
    for name, line in column_lines(text).items():
        rendered = _quoted_values(line)
        if rendered is None:
            continue
        recorded = {str(value) for value in (COLUMNS[name].get("values") or [])}
        # EQUALITY, NOT A SUBSET, AND THE DIFFERENCE IS A REAL DEFECT CLASS. A subset check calls
        # a TRUNCATED list correct — `value_clause` capped at eight values instead of fifteen
        # would render four columns short and every claim test would stay green — while the block
        # says these are "the values that were present when this metadata was built". Under the
        # inline cap the generator renders the whole recorded set, so equality is the contract.
        if set(rendered) != recorded:
            offenders.append(name)
    return offenders


def _quoted_values(line: str) -> list[str] | None:
    """The quoted values in a column line's VALUE clause, or `None` when it lists none.

    ★ THE GLOSS IS CUT OFF FIRST, and it has to be. `BAGS`'s definition ends "...stored as a
    decimal string (e.g. '42.00000') rather than a number" — a QUOTED string that is not a value
    clause at all. A parser that read the whole line after `=` would test that example against the
    column's recorded values and report a fabrication whenever the example happened not to be one
    (it is, for BAGS, which is exactly the kind of accident that makes a checker look correct).

    The grammar is `NAME [(type)] [= values] [-- gloss]`, and the two separators cannot appear
    inside a rendered value — `test_the_line_grammar_has_no_ambiguous_separators` pins that against
    the profile rather than assuming it."""
    if " = " not in line:
        return None
    before_gloss = line.split(" -- ", 1)[0]
    if " = " not in before_gloss:
        return None
    values = re.findall(r"'([^']*)'", before_gloss.split(" = ", 1)[1])
    return values or None


def test_the_line_grammar_has_no_ambiguous_separators() -> None:
    """The two separators the line grammar reserves must not occur inside any value the block can
    render, or the block itself becomes ambiguous to a reader and to a parser. Checked against the
    profile's own values rather than against the rendered text, so it stays true of values the
    inline cap happens to be hiding today."""
    offenders = [
        (name, value)
        for name in DESCRIBED
        for value in (COLUMNS[name].get("values") or [])
        if " -- " in str(value) or " = " in str(value)
    ]
    assert offenders == []


def test_every_rendered_value_is_one_the_column_actually_holds() -> None:
    assert fabricated_values(SHIPPED) == []


def test_a_fabricated_value_is_caught() -> None:
    """★ THE REQUIRED MUTANT, run rather than reasoned about.

    A claim check that has never been observed failing is a claim check nobody can trust. The
    corruption is the realistic one: a plausible-looking value substituted for the awkward real
    one — 'IndiGo Airlines' where the column stores 'Indigo ' — which is precisely what a
    well-meaning cleanup would produce and what no amount of reading the code would reveal.

    The profile is NOT doctored here. Doctoring the input and regenerating would produce a block
    that agrees with its own source and offend nothing; the mutant has to be in the OUTPUT, which
    is where a real fabrication would be."""
    corrupted = SHIPPED.replace("'Indigo '", "'IndiGo Airlines'")
    assert corrupted != SHIPPED, "the fixture value moved — pick another padded value"
    assert fabricated_values(corrupted) == ["GROUND_HANDLER"]


def test_stripping_the_gloss_does_not_blind_the_check() -> None:
    """★ THE SECOND MUTANT, and the one that proves cutting the gloss off did not open a hole.

    `_quoted_values` discards everything after ` -- `, so a quoted EXAMPLE inside a definition is
    not mistaken for a rendered value. The risk that creates is the mirror image: a fabrication
    going unchecked on a glossed line because the parser cut too much. `REMP` is the fixture for
    exactly that shape — a value list AND a gloss on one line."""
    glossed = [line for line in SHIPPED.split("\n") if line.startswith("REMP = ")]
    assert len(glossed) == 1 and " -- " in glossed[0], glossed

    # A quoted example inside a gloss that is NOT one of its column's values: not a fabrication.
    example_only = SHIPPED.replace("(e.g. '42.00000')", "(e.g. '999999.99999')")
    assert example_only != SHIPPED, "the BAGS gloss example moved — update the fixture"
    assert fabricated_values(example_only) == []

    # A fabrication in the VALUE clause of that same shape of line: still caught.
    corrupted = SHIPPED.replace("REMP = '0'", "REMP = 'ZZZ'")
    assert corrupted != SHIPPED, "REMP's first rendered value moved — update the fixture"
    assert fabricated_values(corrupted) == ["REMP"]


def overstated_counts(text: str) -> list[str]:
    """Columns whose rendered count is not the tightest floor the profile supports.

    TWO FIELDS, NEITHER OF WHICH IS THE ANSWER. `distinct_max` is the most distinct values any ONE
    of the thirteen files held; `values` is the union of the value sets those files happened to
    record. Each is a lower bound on what the sample contained and they disagree in both
    directions — `AIRCRAFT_REGN` records 1,013 values against a `distinct_max` of 2,652, while
    `CHECKIN_FROM_CKIF` records 277 against 275. The larger is the tightest bound both support,
    and anything above it is a number the profile cannot back."""
    offenders: list[str] = []
    for name, line in column_lines(text).items():
        stated = re.search(r"= at least ([\d,]+) distinct values observed", line)
        if stated is None:
            continue
        column = COLUMNS[name]
        floor = max(int(column.get("distinct_max") or 0), len(column.get("values") or []))
        if int(stated.group(1).replace(",", "")) != floor:
            offenders.append(name)
    return offenders


def test_every_rendered_count_is_a_floor_the_profile_supports() -> None:
    assert overstated_counts(SHIPPED) == []


def test_an_inflated_count_is_caught() -> None:
    """The mutant for the count check. `AIRCRAFT_REGN`'s floor is its `distinct_max` of 2,652;
    2,700 is the shape of a number somebody rounded up or read off a newer export."""
    corrupted = SHIPPED.replace("AIRCRAFT_REGN = at least 2,652", "AIRCRAFT_REGN = at least 2,700")
    assert corrupted != SHIPPED, "AIRCRAFT_REGN's rendered floor moved — update the fixture"
    assert overstated_counts(corrupted) == ["AIRCRAFT_REGN"]


def test_no_count_is_worded_as_a_total() -> None:
    """★ K5, AND IT IS A WORDING TEST BECAUSE THE DEFECT WAS A WORDING DEFECT.

    The prototype rendered `2,652 distinct values` and a legend claiming a value list "gives the
    complete value set". Both are symbol-for-symbol faithful to the profile and both are false
    about the table: a thirteen-file sample cannot support a closure claim, and an app that
    believes one writes `IN (...)` and silently drops production rows carrying a code that never
    appeared in those thirteen days. Every claim test in this file would have passed."""
    for name, line in column_lines(SHIPPED).items():
        if "distinct values" in line:
            assert "at least" in line, f"{name} states a count as a total"
    for closure in ("complete value set", "the full set", "all of the values", "every value"):
        assert closure not in SHIPPED.lower(), f"the block claims closure: {closure!r}"


def test_the_legend_says_the_value_lists_are_not_closed() -> None:
    """The inline lists are the ones an app copies into a filter, and the per-line wording cannot
    carry the caveat without paying for it 21 times. One legend sentence does, and it has to
    actually be there — the above-cap lines already tell the agent to read the set at runtime, and
    before this ruling the inline ones said nothing at all."""
    flat = " ".join(SHIPPED.split())
    assert "THAT IS NOT A CLOSED SET" in flat
    assert "derive filter and dropdown values from the data at runtime" in flat
    assert "SELECT DISTINCT" in flat


def test_no_gloss_claims_a_value_set_is_closed() -> None:
    """★ A DEFINITION IS PROSE AND CAN LIE INDEPENDENTLY OF THE PROFILE.

    `REMP`'s placeholder definition read "Small closed set: 0, 1, F, H and a few others" — a
    closure claim contradicting the block's own legend, four lines below a value list that already
    rendered all nine values correctly. It was OUR text rather than the client's, and it survived
    every other check in this file, because nothing here reads a definition for meaning.

    Scoped to the rendered glosses rather than to all 131 definitions: an unrendered definition
    can say what it likes, and widening this would fail on client text nobody here may edit."""
    offenders = [
        name
        for name, line in column_lines(SHIPPED).items()
        if " -- " in line
        and any(
            phrase in line.split(" -- ", 1)[1].lower()
            for phrase in ("closed set", "complete list", "exhaustive", "only values")
        )
    ]
    assert offenders == []


_FORMAT_CLAIMS: dict[str, re.Pattern[str]] = {
    "YYYYMMDDHHMMSS": re.compile(r"\d{14}$"),
    "YYYY-MM-DD HH:MM:SS": re.compile(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$"),
}
"""The text-timestamp shapes a gloss is allowed to name, and how to check each against the data."""


def test_a_gloss_naming_one_format_is_true_of_every_observed_value() -> None:
    """★ A GLOSS CAN LIE WITHOUT ANY OTHER CHECK NOTICING, and this is the general guard.

    `ENRC`'s placeholder read "written as an ISO date-time string rather than a date". Measured,
    96.1% of its observed values are `YYYYMMDDHHMMSS` and 3.9% are the dashed form — so the gloss
    named the minority shape as THE shape, and the input file's own `question` field recorded the
    mixture we then dropped. An app parsing it as the gloss says gets `Invalid Date` on almost
    every row; sorting it as text ranks every dashed row below every compact one.

    THE RULE, generally: a gloss that names exactly ONE format must be true of every value the
    profile recorded. A gloss that names two (as ENRC's corrected text does) is claiming a
    mixture, and both must actually occur."""
    for name, line in column_lines(SHIPPED).items():
        if " -- " not in line:
            continue
        gloss = line.split(" -- ", 1)[1]
        claimed = [shape for shape in _FORMAT_CLAIMS if shape in gloss]
        values = [str(value) for value in (COLUMNS[name].get("values") or [])]
        if not claimed or not values:
            continue
        if len(claimed) == 1:
            pattern = _FORMAT_CLAIMS[claimed[0]]
            stray = [value for value in values if not pattern.match(value)]
            assert not stray, (
                f"{name}'s gloss names {claimed[0]} as the format, but "
                f"{len(stray)} of {len(values)} observed values are a different shape: {stray[:3]}"
            )
        else:
            for shape in claimed:
                assert any(_FORMAT_CLAIMS[shape].match(value) for value in values), (
                    f"{name}'s gloss names {shape}, which no observed value matches"
                )


def test_no_gloss_calls_a_second_granular_column_minutes() -> None:
    """★ THE UNIT DEFECT, AS A RULE RATHER THAN AS ONE CORRECTION.

    `OTP`'s placeholder said "in MINUTES against schedule". Every one of its 301 observed values
    is a whole multiple of 60, as are both extremes — which is what a column of SECONDS looks
    like, and not what a column of minutes looks like: `TAT`, a genuine minutes column, takes 3,
    9, 11, 23. So a chart labelled from that gloss was 60x out.

    The rule this pins is general and cheap: an integer column whose every observed value is a
    multiple of 60 is second-granular, and its gloss may not call it minutes. Applied to every
    glossed integer column, so the next placeholder cannot reintroduce it."""
    for name, line in column_lines(SHIPPED).items():
        if " -- " not in line or "(integer)" not in line:
            continue
        values = [int(value) for value in (COLUMNS[name].get("values") or [])]
        if len(values) < 10 or not all(value % 60 == 0 for value in values):
            continue
        gloss = line.split(" -- ", 1)[1].lower()
        assert "minute" not in gloss.split("minutes.")[0] or "seconds" in gloss, (
            f"{name}'s every observed value is a multiple of 60 (seconds), but its gloss calls "
            f"it minutes: {gloss[:120]}"
        )
        assert "seconds" in gloss, (
            f"{name} is second-granular and its gloss does not say so: {gloss[:120]}"
        )


def test_every_column_a_measure_names_is_one_of_the_described() -> None:
    """★ NOT "IS IN THE PROFILE" — that predicate passes on every column the cut removed.

    Six of the sixteen KPIs named a column outside the agreed set (`SCHLD_DEP_TIME_STOD`,
    `SCHLD_ARR_TIME_STOA`, `ARR_DEP_FLG_ADID`), and all three are still in `profile.json`, so a
    membership check against the profile would have waved every one of them through and shipped
    the model a formula naming a column this block does not describe."""
    described = set(DESCRIBED)
    named = {column.strip() for _, _, columns, _ in MEASURES for column in columns.split(",")} | {
        ours for _, ours, _ in MAPPING
    }
    assert named <= described, f"outside the described set: {sorted(named - described)}"


def test_the_committed_profile_carries_no_real_flight_rows() -> None:
    """★ STRIPPED AT COPY TIME, NOT TESTED AROUND.

    The raw profile carries a `sample_rows` key holding 130 real production rows — 1.24 MB of the
    3.37 MB file — so it is dropped when the profile is copied into `backend/data/`. That keeps
    all 408 `columns` entries, which is what makes the 408 -> 131 cut auditable in the repo, and
    puts no flight row in it.

    ONE STRUCTURAL ASSERTION REPLACES A SUBSTRING TEST THAT CANNOT WORK. Searching the artefact
    for sample-row content is measured to be useless: 58 of the 131 columns render a value that
    also appears verbatim in a sample row — `AIRCRAFT_BODY_TYPE` renders `NARROW_BODY`,
    `DOM_INT_OPS` renders `DOM` — because they are the same vocabulary."""
    assert "sample_rows" not in PROFILE
    raw = json.loads((DATA_DIR / "profile.json").read_text(encoding="utf-8"))
    assert set(raw) == {"generated", "source", "period", "files", "drift", "columns"}
    assert len(raw["columns"]) == PROFILE["drift"]["columns_total"]


# --------------------------------------------------------------------------------------------
# CLAIMS — the header, whose sentences are prose but whose facts are not
# --------------------------------------------------------------------------------------------


def flat(text: str) -> str:
    """The block with its paragraph wrapping removed.

    Every authored paragraph is wrapped at a fixed width from interpolated values, so a landmark
    assertion has to be made against the unwrapped text or it goes red the day a rendered figure
    changes length — which is a check on the wrapper, not on the claim."""
    return " ".join(text.split())


def test_the_header_carries_the_profiles_own_period() -> None:
    """K5's provenance line, and it is a LANDMARK assertion: if the whole header were deleted this
    test is what goes red, so an empty-bodied regression cannot pass the file quietly."""
    period = PROFILE["period"]
    assert f"read from {period['files']} daily load files" in flat(SHIPPED)
    assert "covering 1 January 2026 to 7 September 2026" in flat(SHIPPED)
    assert period["from"] == "20260101" and period["to"] == "20260907"


def test_the_header_states_the_load_date_trap_from_the_profiles_own_spread() -> None:
    """The most consequential rule in the block, and every figure in it is derived.

    The scheduled-time spread is `SIBT_SOBT_TIME`'s own min and max across the sample — flights
    from more than three years before the earliest load file. Nothing about the trap is typed."""
    flight_time = COLUMNS["SIBT_SOBT_TIME"]
    assert str(flight_time["min"]).startswith("2022-08")
    assert str(flight_time["max"]).startswith("2026-10")
    body = flat(SHIPPED)
    assert "THE FILE'S DATE IS THE LOAD DATE, NOT THE FLIGHT DATE" in body
    assert "scheduled between August 2022 and October 2026" in body
    assert "filter ROWS on SIBT_SOBT_TIME" in body


def test_the_header_warns_off_the_column_that_drops_half_the_traffic() -> None:
    """`SCHEDULED_OFF_BLOCK_TIME_SOBT` is the departure time and null on every arrival row, so
    filtering dates on it reports half the traffic with no error anywhere. The profile's own null
    rate is what makes "half" a measured word rather than a figure of speech."""
    assert 45.0 < float(COLUMNS["SCHEDULED_OFF_BLOCK_TIME_SOBT"]["null_pct_avg"]) < 55.0
    assert 45.0 < float(COLUMNS["SCHEDULED_IN_BLOCK_TIME_SIBT"]["null_pct_avg"]) < 55.0
    assert float(COLUMNS["SIBT_SOBT_TIME"]["null_pct_avg"]) == 0.0
    body = flat(SHIPPED)
    assert "Do NOT filter on SCHEDULED_OFF_BLOCK_TIME_SOBT" in body
    assert "null on every arrival row" in body
    assert "SCHEDULED_IN_BLOCK_TIME_SIBT has the mirror-image problem" in body


def test_the_header_states_the_dedupe_rule_without_an_unproven_number() -> None:
    """The rule is load-bearing; the "about 11%" that used to sit beside it is not proven at
    production scale — it was measured on a thirty-day REPLICA window whose absolute figures the
    full-lake scan showed to be 21x off. The rule stands without it, and stating it with a number
    the data cannot back is the failure mode this whole file exists to prevent."""
    body = flat(SHIPPED)
    assert "ONE ROW PER FLIGHT PER LOAD" in body
    assert "highest LAST_UPDATE_DATE_TIME for each AODB_AFTTAB_PK_URNO" in body
    assert "11%" not in body


def test_the_header_states_how_far_the_padding_trap_actually_reaches() -> None:
    """★ THE COUNTS ARE DERIVED, AND THE THIRD ONE IS THE USEFUL ONE.

    Six described columns hold a space-padded value, and only two of them are under the inline cap
    — so for the other four the block states a count and the padding is INVISIBLE in it.
    `AIRLINE_NAME`'s padded 'AKASA AIR' is never shown to the agent. A header that implied the
    lines would demonstrate the trap would be teaching the agent to look for something that is
    not there."""
    padded = {
        name: [v for v in (COLUMNS[name].get("values") or []) if str(v) != str(v).strip()]
        for name in DESCRIBED
    }
    padded = {name: values for name, values in padded.items() if values}
    listed = [n for n in padded if len(COLUMNS[n]["values"]) <= INLINE_VALUE_MAX]
    assert set(padded) == {
        "AIRLINE_NAME",
        "AIRLNS_CODE_ALC3",
        "FIRST_BAG_DEL_RSN1",
        "FLIGHT_TYPE_FTYP",
        "GROUND_HANDLER",
        "LAST_BAG_DEL_RSN1",
    }
    body = flat(SHIPPED)
    assert f"{sum(len(v) for v in padded.values())} values across {len(padded)} of the" in body
    assert f"only {len(listed)} of those {len(padded)} list their values here" in body
    assert "the padding is invisible in this block" in body


def test_the_header_names_the_two_blank_shaped_columns_it_claims() -> None:
    """`FLIGHT_NATURE_TTYP_DESC` uses an empty string where a null belongs and `FLIGHT_TYPE_FTYP`
    holds a single space — both invisible to `IS NOT NULL`, both asserted against the profile
    rather than taken from the sentence that names them."""
    assert "" in COLUMNS["FLIGHT_NATURE_TTYP_DESC"]["values"]
    assert " " in COLUMNS["FLIGHT_TYPE_FTYP"]["values"]
    assert "AKASA AIR" in COLUMNS["AIRLINE_NAME"]["values"]
    assert any(
        v.strip() == "AKASA AIR" and v != "AKASA AIR" for v in COLUMNS["AIRLINE_NAME"]["values"]
    )
    body = flat(SHIPPED)
    assert "FLIGHT_NATURE_TTYP_DESC is one, and FLIGHT_TYPE_FTYP holds a single space" in body


def test_the_measures_header_states_the_two_assumptions_it_makes() -> None:
    """Their OTP formulas name `ATD` and `ATA`, which the table does not have, and the two
    readings differ by ten to twenty-five minutes. Choosing is unavoidable; choosing silently is
    not. Both are printed where the agent reads them."""
    body = flat(SHIPPED)
    assert "TWO ASSUMPTIONS WE MADE" in body
    assert "ATD -> ACTUAL_OFF_BLOCK_TIME_AOBT" in body
    assert "ATA -> ACTUAL_IN_BLOCK_TIME_AIBT" in body
    assert "which does not exist" in body
    for name, _, _, _ in MEASURES:
        assert f"{name} = " in SHIPPED


def test_the_block_names_no_tool_that_is_not_registered() -> None:
    """★ THE PROTOTYPE'S COVERAGE NOTE TOLD THE MODEL TO CALL `connector_column_values`, a tool
    the single-tool design does not register. A block instructing the model to call something that
    does not exist is the confident-falsehood class this whole pass exists to prevent, in the one
    file the model actually reads."""
    # A WORD BOUNDARY, or the banner's own `build_connector_catalogue.py` matches: `_` is a
    # word character, so `\bconnector` cannot start mid-identifier.
    named = set(re.findall(r"\bconnector_[a-z_]+", SHIPPED))
    assert named <= {"connector_schema"}, f"the block names unregistered tools: {sorted(named)}"
    assert "connector_column_values" not in SHIPPED


# --------------------------------------------------------------------------------------------
# SAMPLES — one per rendering branch, because six defects here were found by reading output
# --------------------------------------------------------------------------------------------


def test_a_small_enum_renders_its_values_quoted_and_verbatim() -> None:
    lines = column_lines(SHIPPED)
    assert lines["AIRCRAFT_BODY_TYPE"] == "AIRCRAFT_BODY_TYPE = 'NARROW_BODY', 'WIDE_BODY'"
    assert lines["TERMINAL"] == "TERMINAL = '1', '2'"
    # ★ THE PADDING IS VISIBLE BECAUSE THE VALUE IS QUOTED AND UNTRIMMED. Without the quotes a
    # reader — and a model — cannot tell 'Indigo ' from 'Indigo', which is the whole trap.
    assert "'Indigo '" in lines["GROUND_HANDLER"]
    assert "'Indigo'," not in lines["GROUND_HANDLER"]
    # A single space is a value, and it renders as one.
    assert lines["FLIGHT_TYPE_FTYP"].startswith("FLIGHT_TYPE_FTYP = ' ', 'B',")


def test_a_large_enum_renders_an_observed_floor_and_no_list() -> None:
    line = column_lines(SHIPPED)["AIRCRAFT_REGN"]
    assert line == "AIRCRAFT_REGN = at least 2,652 distinct values observed"
    assert "'" not in line
    assert len(COLUMNS["AIRCRAFT_REGN"]["values"]) > INLINE_VALUE_MAX


def test_an_integer_renders_its_type_and_no_range() -> None:
    """Ranges were deliberately dropped: they are statistics about a frozen sample, and for `OTP`
    the observed extremes are data-quality outliers, so printing them as "the range" misinforms.
    Where the range matters, the DEFINITION carries it — which is why `OTP`'s gloss says so."""
    lines = column_lines(SHIPPED)
    assert lines["NO_OF_SEATS_NOSE"] == "NO_OF_SEATS_NOSE (integer)"
    assert lines["FOR_ATM"] == "FOR_ATM (integer)"
    # The profile stores bounds as STRINGS (the profiler widens them across files textually),
    # which is itself a reason not to render them: they are not numbers by the time they get
    # here. The gloss carries the range for the one column where it matters.
    assert COLUMNS["OTP"]["min"] == "-90720"
    assert "-90,720" in lines["OTP"]
    assert "min" not in lines["NO_OF_SEATS_NOSE"]


def test_a_decimal_renders_its_type_and_not_its_values() -> None:
    """★ THE OTHER HALF OF THE NUMERIC SUPPRESSION, WHICH HAD NO TEST.

    `value_clause` suppresses the value list for `integer` AND `decimal`, but only the integer
    half was pinned. The decimal half is live in the real data and it is the half that would look
    plausible if it broke: `AIRLINES_ID` has 1,458 recorded values, so narrowing the exclusion to
    `{"integer"}` ships `= at least 1,458 distinct values observed` against a numeric surrogate
    key — numerically true, and an invitation to build a dropdown of airline IDs.

    Mutation check: narrow the set to `{"integer"}` and this goes red naming the column."""
    lines = column_lines(SHIPPED)
    # THE VALUE CLAUSE IS THE SUBJECT, and a gloss may now sit beside it: the client's 2026-09
    # round said this column is an internal sequence, not the airline code it resembles, which is
    # exactly the confusion this test exists to prevent. Assert the half under test.
    assert lines["AIRLINES_ID"].split(" -- ")[0] == "AIRLINES_ID (decimal)", lines["AIRLINES_ID"]
    assert lines["AIRCRAFT_ID"].split(" -- ")[0] == "AIRCRAFT_ID (decimal)", lines["AIRCRAFT_ID"]
    # The suppression is doing real work: these columns DO carry values, they are simply not a
    # vocabulary anyone should filter on.
    assert len(COLUMNS["AIRLINES_ID"]["values"]) > INLINE_VALUE_MAX
    assert "=" not in lines["AIRLINES_ID"]


def test_a_timestamp_renders_bare() -> None:
    lines = column_lines(SHIPPED)
    assert lines["SIBT_SOBT_TIME"] == "SIBT_SOBT_TIME (timestamp)"
    assert lines["ACTUAL_OFF_BLOCK_TIME_AOBT"] == "ACTUAL_OFF_BLOCK_TIME_AOBT (timestamp)"


def test_a_text_column_states_no_type_at_all() -> None:
    """Text is the majority, so it is the legend's stated default and the marked ones stand out.
    A reader has to be able to tell "text" from "the generator forgot"."""
    assert column_lines(SHIPPED)["AODB_AFTTAB_PK_URNO"] == "AODB_AFTTAB_PK_URNO"
    assert "(text)" not in SHIPPED
    assert "A column with no type in brackets holds text" in flat(SHIPPED)


def test_an_opaque_code_renders_its_whole_gloss_unclipped() -> None:
    """★ THE CLIPPING WAS THE DEFECT, NOT THE LENGTH.

    The prototype rendered the FIRST CLAUSE of a definition, capped at 80 characters. That drops
    `OTP`'s unit and sign convention and `BAGS`'s "stored as a decimal string" — the two facts on
    those lines most able to produce a green build with wrong numbers — and the clause splitter
    had already been fixed twice for cutting mid-phrase. The whole definition is rendered instead,
    which also deletes the regex.

    ASSERTED AS THE PROPERTY, NOT AS A COPIED LITERAL. A test holding one column's gloss as a 300-
    character string is a test that goes red whenever the client returns a better definition, and
    reviewers learn to paste the new text in without reading it. This says the thing that has to
    stay true of ALL of them: what is rendered is the whole definition, ASCII-folded, nothing cut.
    """
    definitions = dict(DEFINITIONS)
    glossed = {
        name: line.split(" -- ", 1)[1]
        for name, line in column_lines(SHIPPED).items()
        if " -- " in line
    }
    assert glossed, "no column renders a gloss at all"
    for name, gloss in glossed.items():
        assert gloss == ascii_only(definitions[name]), name
    # The three whose whole point is what the first clause would have thrown away.
    assert "SECONDS" in glossed["OTP"] and "negative is early" in glossed["OTP"]
    assert "decimal string" in glossed["BAGS"]
    # TAT WAS A THIRD LITERAL HERE and it went red on the client's round-1 answers, which replaced
    # our sentence with the acronym spelled out. That is their call to make -- what turnaround
    # MEASURES is stated as a formula in the MEASURES block regardless -- and this file's own
    # docstring warns that a pinned literal teaches reviewers to paste new text in unread. So the
    # property is asserted instead: the longest definition arrives whole, which is what clipping
    # would break. OTP and BAGS stay literal because they pin MEASURED facts, not prose.
    longest = max(glossed, key=lambda name: len(definitions[name]))
    assert glossed[longest] == ascii_only(definitions[longest])


def test_a_self_describing_name_gets_no_gloss() -> None:
    """108 of the 131 names say what they are. Glossing them would be prose the model can already
    read off the name, and it was measured at ~7,500 tokens across the 408-column set."""
    lines = column_lines(SHIPPED)
    for self_describing in (
        "ACTUAL_OFF_BLOCK_TIME_AOBT",
        "SCHEDULED_IN_BLOCK_TIME_SIBT",
        "NO_OF_SEATS_NOSE",
        "AIRCRAFT_BODY_TYPE",
    ):
        assert " -- " not in lines[self_describing]
    # A LONG NAME IS GLOSSED ONLY WHEN IT WAS MARKED, and the mark is evidence-driven rather than
    # taste: the client's 2026-09 review round returned meanings no name can carry (AIBT_AOBT_TIME
    # holds the arrival's in-block time OR the departure's off-block time depending on the row --
    # the SIBT_SOBT_TIME trap again). Before that round the count was 23, all bare codes. The rule
    # this test defends is unchanged: prose the model can read off the name is still not rendered.
    glossed = {name for name, line in lines.items() if " -- " in line}
    marked = marked_for_gloss()
    assert all(len(name) <= 5 or name in marked for name in glossed), sorted(
        name for name in glossed if len(name) > 5 and name not in marked
    )
    assert len(marked) < 20, f"{len(marked)} marks is a tier system, not an exception: {marked}"


def test_a_timestamp_stored_as_text_is_not_offered_as_a_code_list() -> None:
    """Thirteen columns hold more distinct values than any vocabulary plausibly has because they
    are timestamps written as text. Telling the agent to `SELECT DISTINCT` 125,028 of them would
    be advice to read the whole table for nothing."""
    # THE VALUE CLAUSE IS THE ASSERTION, not the gloss beside it: the gloss is the client's text and
    # moves every review round, while "free text, not a code list" is the decision under test.
    line = column_lines(SHIPPED)["ACGT"]
    assert line.split(" -- ")[0] == "ACGT = free text, not a code list", line
    assert int(COLUMNS["ACGT"]["distinct_max"]) > NOT_A_VOCABULARY_ABOVE


def test_every_column_lands_in_a_named_domain_group() -> None:
    """★ THERE IS NO "EVERYTHING ELSE" BUCKET, and that is what makes the groups usable.

    `ACGT` ("Start Ground Handling") and `AEGT` ("Actual End of Ground Handling") used to fall
    through to it — filed away from the handling group an agent building a turnaround board would
    actually look in. A catch-all makes that failure silent; a fail-first `group_of` makes the
    next such column somebody's decision."""
    headings = re.findall(r"^## (\w+) -- (.+)$", SHIPPED, re.M)
    assert [key for key, _ in headings] == [key for key, _, _ in GROUPS]
    assert dict(headings) == {key: label for key, label, _ in GROUPS}
    assert group_of("ACGT") == "handling"
    assert group_of("AEGT") == "handling"
    assert "Everything else" not in SHIPPED


# --------------------------------------------------------------------------------------------
# STRUCTURAL — properties of the shipped bytes alone
# --------------------------------------------------------------------------------------------


def test_the_artefact_is_ascii_and_lf_only() -> None:
    """The image is built on a Windows VM. A CRLF checkout would change every byte the drift test
    compares, so the root `.gitattributes` pins the directory to `eol=lf` — and ASCII is asserted
    beside it because an em-dash surviving into a value clause is the other way this file becomes
    bytes nobody predicted. `git check-attr` covers the rule; this covers the content."""
    assert SHIPPED.isascii(), sorted({c for c in SHIPPED if not c.isascii()})
    assert "\r" not in SHIPPED
    assert not any(line != line.rstrip() for line in SHIPPED.split("\n"))
    assert SHIPPED.endswith("\n")


def test_the_artefact_says_it_is_generated() -> None:
    """It is the only thing standing between this file and somebody editing 14 KB of plausible
    text by hand, which the drift test would then report as a generator bug."""
    first = SHIPPED.split("\n")[0]
    assert first.startswith("# GENERATED FILE -- do not hand-edit")
    assert "build_connector_catalogue.py" in SHIPPED.split("\n\n")[0]
    assert len(SHIPPED) > 10_000


# --------------------------------------------------------------------------------------------
# FAIL-FIRST — the two ways the inputs can disagree, and neither is a skip
# --------------------------------------------------------------------------------------------


def test_a_definition_with_no_profile_row_stops_the_build() -> None:
    """The two files would be describing different tables, and the alternative — skipping the
    name — is a column silently missing from the block with nothing to notice it."""
    with pytest.raises(SystemExit, match="describe different tables"):
        build(PROFILE, (*DEFINITIONS, ("NO_SUCH_COLUMN_AT_ALL", "invented")))


def test_a_column_no_group_claims_stops_the_build() -> None:
    with pytest.raises(SystemExit, match="matches no domain group"):
        group_of("zzz_unclaimed_column")


def test_ascii_only_folds_to_the_characters_it_claims() -> None:
    """★ THE FUNCTION, NOT THE FUNCTION COMPARED WITH ITSELF.

    The only other exercise of `ascii_only` asserts `gloss == ascii_only(definitions[name])` — it
    applies the function to BOTH sides, so a wrong-but-still-ASCII entry in `_ASCII_FOLDS` (an
    em-dash folding to `+`, say) passes on every column. This pins the fold table against
    hardcoded literals instead, so the expected bytes are written down rather than computed by
    the code under test.

    This is the same defect shape as two others already found in this change: an assertion whose
    two sides share the fault it is meant to catch."""
    assert ascii_only("a—b") == "a-b"  # em-dash
    assert ascii_only("a–b") == "a-b"  # en-dash
    assert ascii_only("‘q’") == "'q'"  # single curly quotes, both sides
    # Already-ASCII text is returned untouched — the fold table must not rewrite ordinary prose.
    assert ascii_only("plain 'text' -- with punctuation") == "plain 'text' -- with punctuation"
    # The table is FOUR entries and everything else is a failure, so the DOUBLE curly quotes are
    # not folded — they raise. Pinning that here because it is the property that makes the table
    # safe to extend: a new entry is a deliberate decision, never a silent widening.
    with pytest.raises(SystemExit, match="non-ASCII"):
        ascii_only("“q”")


def test_a_character_the_fold_table_does_not_know_stops_the_build() -> None:
    """`ascii_only`'s fail-first arm, which had no test of its own.

    The ellipsis is not a hypothetical: it arrived in `ENRC`'s corrected definition and this is
    the guard that caught it. A lenient `.encode("ascii", "ignore")` would have swallowed it."""
    with pytest.raises(SystemExit, match="non-ASCII"):
        ascii_only("an ellipsis…")


def test_a_column_whose_files_disagree_on_type_stops_the_build() -> None:
    """`dtypes[0]` was the one silent pick left in a fail-first file: a column profiled as two
    types has no single type to state, and picking the first states one anyway."""
    with pytest.raises(SystemExit, match="holds 2 dtypes"):
        simple_type(["Int64", "String"])


def test_a_dtype_no_branch_names_stops_the_build() -> None:
    """The fall-through used to return "text", so a future `Float64` or `Boolean` column would be
    labelled free text — the same mislabelling `value_clause` trusts `simple_type` to prevent."""
    with pytest.raises(SystemExit, match="matches no known dtype"):
        simple_type(["Float64"])


def test_a_non_ascii_value_stops_the_build_not_just_a_non_ascii_definition() -> None:
    """★ `ascii_only` GUARDS OUR PROSE; THIS GUARDS THE CLIENT'S BYTES.

    Values are rendered VERBATIM by contract — never folded, never stripped. So the one guard the
    definitions get does not reach them, and a value carrying a newline does not corrupt a line,
    it SPLITS one: the halves then read as two more columns in a file whose entire format is one
    column per line. That is a fabricated column, which is the exact failure this artefact exists
    to make impossible."""
    poisoned = json.loads(json.dumps(PROFILE))
    for column in poisoned["columns"]:
        # A column rendered INLINE, so the poisoned value actually reaches the artefact —
        # `GROUND_HANDLER` holds more than the cap and renders as a count, which would have made
        # this test pass for the wrong reason.
        if column["name"] == "AIRCRAFT_BODY_TYPE":
            column["values"] = [*column["values"], "FAKE\nSPLIT_COLUMN (text)"]
            break
    with pytest.raises(SystemExit, match=r"AIRCRAFT_BODY_TYPE's value .* not printable ASCII"):
        build(poisoned, DEFINITIONS)


def test_the_tools_docstring_counts_match_what_the_generator_renders() -> None:
    """★ A CLAIM IN SHIPPED PROSE, PINNED TO THE THING IT CLAIMS ABOUT.

    `connector_tools.py`'s module docstring states the bucket split off the artefact — 21 inline,
    68 with no vocabulary, 13 free-text, 29 a floor. Those numbers were correct when typed and
    nothing held them there, so the next workbook revision moves the artefact and leaves the prose
    asserting a split that no longer exists. The docstring is read by humans deciding what this
    tool returns, and the same paragraph already carries a correction of the plan's stale 21/26/84
    — which is what a hand-counted number does the second time.

    Counted from the SHIPPED bytes rather than from the profile: the claim is about what the block
    renders, not about what the data could support."""
    counts = {"inline": 0, "none": 0, "free_text": 0, "floor": 0}
    for line in column_lines(SHIPPED).values():
        clause = line.split(" -- ", 1)[0]
        if "= '" in clause:
            counts["inline"] += 1
        elif "free text, not a code list" in clause:
            counts["free_text"] += 1
        elif "distinct values observed" in clause:
            counts["floor"] += 1
        else:
            counts["none"] += 1

    assert sum(counts.values()) == len(DESCRIBED) == 131

    # WHOLE SENTENCES, NOT `str(count) in docstring`. That substring check is what I wrote first
    # and it proves nothing: the same paragraph corrects the plan's stale "21 / 26 / 84", so "21"
    # is in the text whatever the inline count becomes. Whitespace is collapsed because the
    # docstring wraps, and a claim must not be pinnable or unpinnable by where a line breaks.
    docstring = " ".join(
        (_BACKEND / "src/services/agent/connector_tools.py")
        .read_text(encoding="utf-8")
        .split('"""')[1]
        .split()
    )
    claims = {
        "inline": f"{counts['inline']} columns carry their values INLINE",
        "none": f"{counts['none']} have no vocabulary to state at all",
        "free_text": f"{counts['free_text']} hold more distinct values",
        "floor": f"the remaining {counts['floor']} are airport",
    }
    for label, claim in claims.items():
        assert claim in docstring, (
            f"the artefact renders {counts[label]} {label} columns and connector_tools.py's "
            f"docstring does not say {claim!r}. Recount it — those numbers are a claim about "
            "this file, and the paragraph already carries one stale count it had to correct."
        )
    assert counts == {"inline": 21, "none": 68, "free_text": 13, "floor": 29}


def test_a_profile_without_the_date_filter_column_stops_the_build() -> None:
    """`header` writes the date-filter rule from `SIBT_SOBT_TIME`. A re-profile that dropped it
    gave a bare KeyError from a script whose every other missing-column path names the column and
    says what to do — the third silent lookup in this file, after `simple_type`'s two."""
    without = json.loads(json.dumps(PROFILE))
    without["columns"] = [c for c in without["columns"] if c["name"] != "SIBT_SOBT_TIME"]
    kept = tuple((n, d) for n, d in DEFINITIONS if n != "SIBT_SOBT_TIME")
    with pytest.raises(SystemExit, match="SIBT_SOBT_TIME has no row"):
        build(without, kept)
