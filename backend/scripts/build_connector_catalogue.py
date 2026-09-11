"""Render the connected system's schema block from its committed profile and definitions.

    uv run python scripts/build_connector_catalogue.py            # write the artefact
    uv run python scripts/build_connector_catalogue.py --stdout   # print it, write nothing

WHAT THIS PRODUCES. One text file — `src/services/agent/connector_catalogue/<key>.txt` — which is
the entire body of the `connector_schema` tool's answer. It is the only thing the platform ever
tells a citizen's agent about the shape of the connected system's data, so every sentence in it
has to be true of that data, and the way this stays true is that NOTHING IN IT IS TYPED BY HAND
except prose that makes no claim about a column.

THE THREE INPUTS, AND WHICH ONE IS THE AUTHORITY.

  data/connectors/<key>/profile.json      THE AUTHORITY. What the columns actually hold, read off
                                          the lake. Types, value sets, cardinalities, ranges.
  data/connectors/<key>/definitions.json  The working column list AND their meanings. Its 131
                                          names ARE the set this block describes — see below.
  MEASURES / MAPPING / UNANSWERABLE       The client's own KPI arithmetic, as module constants.
  / PARTIAL, below                        A formula cannot be derived from a profile; it comes
                                          from BIAL's workbook whichever file holds it, and a
                                          Python constant is at least reviewed by ruff/ty/mypy/
                                          pyright, which none of them look inside a JSON for.

THE COLUMN SET IS THE WORKBOOK'S LIST AND NOTHING ELSE. `definitions.json` is the workbook that
went to the client on 10 September 2026 — 131 columns, cut down from the table's 408 (230 were
empty in every row, 9 held a single value, 38 were exact duplicates of another column). This
script iterates that list and looks each name up in the profile. It applies NO selection rule of
its own: nothing is added to the list and nothing is filtered out of it, and a name with no row
in the profile is a fail-first error rather than a skip, because it means the two files are
describing different tables.

  (The prototype selected with `not col["never_populated"]` instead. That is a statistic about a
  thirteen-file sample deciding what ships, and it also gives the wrong answer — 178, not the
  agreed 131.)

WHAT IT SAYS ABOUT WHAT IT DOES NOT KNOW. The profile is a SAMPLE — thirteen daily load files.
Two of its fields read like population facts and are not: `distinct_max` is the largest distinct
count seen in ONE file, and `values` is the union of the value sets those files happened to
record. Neither is the set the column can hold. So a count is rendered as a floor ("at least N
distinct values observed"), a value list is introduced by a legend sentence that says in as many
words that it is not closed, and the header carries the profile's own period. An app that reads
this block and hard-codes a filter from it is the failure this wording exists to prevent.

VALUES ARE RENDERED AS STORED, NEVER AS TRIMMED. `GROUND_HANDLER` holds `'Indigo '` with a
trailing space, and fifteen values across six of the described columns are padded like it. Strip
them here and the block teaches the exact trap the header warns about: an app filtering
`= 'Indigo'` returns no rows and draws an empty chart. Inline values are quoted so the padding is
visible — and the header states how far the trap reaches, because four of those six columns hold
too many values to list, so their padding is invisible in the block whatever this file does.

A DEFINITION IS PROSE AND CAN STILL LIE. `REMP`'s read "Small closed set: 0, 1, F, H and a few
others" — a closure claim no thirteen-file sample supports, contradicting the block's own legend,
in text that was ours (`source: INFERRED — placeholder`) rather than the client's. It was
corrected in `definitions.json` and a claim test now refuses closure language in any rendered
gloss, so the next placeholder cannot reintroduce it.

THE OUTPUT IS ASCII, LF-ONLY, AND BYTE-COMPARED BY A TEST. The image is built on a Windows VM;
a CRLF checkout or a stray em-dash would change every byte the drift test compares. The root
`.gitattributes` pins the directory to `eol=lf` and `tests/scripts/
test_build_connector_catalogue.py` regenerates this and diffs it.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import textwrap
from collections import defaultdict
from datetime import date
from pathlib import Path
from typing import Any, Final

_HERE: Final = Path(__file__).resolve().parent
_BACKEND: Final = _HERE.parent

CONNECTOR_KEY: Final = "dice"
"""The one connected system there is. Named here — in `backend/scripts/`, outside the two trees
`tests/test_connector_naming_is_generic.py` walks — because this script renders that system's own
KPI arithmetic and cannot pretend not to know whose it is. Everything it WRITES is keyed off this
string, so the runtime loader names no connector at all."""

DATA_DIR: Final = _BACKEND / "data" / "connectors" / CONNECTOR_KEY
TARGET: Final = (
    _BACKEND / "src" / "services" / "agent" / "connector_catalogue" / f"{CONNECTOR_KEY}.txt"
)

INLINE_VALUE_MAX: Final = 15
"""Above this, the line states a floor on the count and the app derives the set at runtime.

THE LOAD-BEARING NUMBER IN THIS DESIGN, not a nicety. Removing the cap entirely was measured at
28,543 tokens — 4.2x the whole response — because `AIRCRAFT_REGN` alone would list 1,013
registrations. Raising it to 30 would inline the three columns BIAL is still writing definitions
for, for +442 tokens; the owner chose to keep 15 on 2026-09-11 and again on 2026-09-09."""

NOT_A_VOCABULARY_ABOVE: Final = 5_000
"""More distinct values than a code list plausibly has. These columns are timestamps written as
text (`ACGT` has 125,028 of them), and telling the model to `SELECT DISTINCT` them would be
advice to read a hundred thousand rows for nothing."""


# --- The domain grouping -----------------------------------------------------------------------
#
# Ordered, and FIRST MATCH WINS, so the specific patterns must precede the general ones. The
# groups are the questions an airport actually asks rather than an alphabetical carve-up:
# somebody building a punctuality dashboard should find every column they need inside one or two
# of them. R4's one-pass column selection is what depends on the descriptions being here.
#
# THERE IS NO "EVERYTHING ELSE" ARM. A column that matches no pattern stops the build (see
# `group_of`), because a silent catch-all is how `ACGT` ("Start Ground Handling") and `AEGT`
# ("Actual End of Ground Handling") ended up filed away from the handling group an agent would
# look in for them.
GROUPS: Final[tuple[tuple[str, str, str], ...]] = (
    (
        "record",
        "About the record, not the flight",
        r"LAST_UPDATE|CREATION|BatchID|DP_CREATED|^LSTU$|^USEU$|^RKEY$|DATASOURCE|DSSF|yearmonth",
    ),
    (
        "identity",
        "Which flight this is",
        r"URNO|FLIGHT_ID|RowId|FLNO|FLTN|CSGN|CALL_SIGN|_ID$",
    ),
    (
        "airline",
        "Who is operating it",
        r"AIRLNS|AIRLINE|OPERATOR|CARRIER",
    ),
    (
        "aircraft",
        "What is flying it",
        r"AIRCRFT|AIRCRAFT|REGN|ACT3|ACT5|ACTI|BODY_TYPE|NOSE|SEATS",
    ),
    (
        "route",
        "Where it is coming from or going to",
        r"ORIGIN|DEST|VIA|ROUTE|ORG3|ORG4|DES3|DES4|SECTOR",
    ),
    (
        "schedule",
        "When it was meant to happen",
        r"SCHEDULED|SCHLD|SCH_|SIBT|SOBT|STOA|STOD",
    ),
    (
        "actual_times",
        "When it actually happened (A-CDM milestones)",
        r"ACTUAL|AIBT|AOBT|ALDT|ATOT|ARDT|ASAT|ASRT|AXIT|EXOT|TOBT|TSAT|AIRBORNE|LANDING|TAKEOFF"
        r"|OFFBLOCK|ONBLOCK|TOUCH_DOWN|OFBL|OFBD|OFBS|OFBU|ONBL|ONBD|ONBS|ONBU|LNDD|LNDA|LAND|LNDU"
        r"|^TLDT$|^TTOT$|^TMOA$|^FPLA$|^FPLD$|ARR_DEP_TIME|FB_TIME|LB_TIME",
    ),
    (
        "estimates",
        "What was expected at the time",
        r"^E[A-Z]{3}$|ESTD|ESTIMATE|BEST_EST|ETAA|ETOA|ETOD|ETAI|ETDI|EXP_|CTOT|ETDE|SLOT",
    ),
    (
        "delay",
        "Why it was late",
        r"DELAY|DEL[AD]$|DCD|REASON|RSN",
    ),
    (
        "stand_gate",
        "Where it parked and which gate it used",
        r"STAND|PSTA|PSTD|GATE|GTA|GTD|GA1|GA2|GD1|GD2|POSITION",
    ),
    (
        "terminal",
        "Which terminal and check-in",
        r"TERMINAL|CHECKIN|CKIF|CKIT|COUNTER",
    ),
    (
        "passengers",
        "How many people were on board",
        r"PAX|PASSENGER|ADULT|INFANT|TRANSFER|TRANSIT",
    ),
    (
        "baggage",
        "Bags and belts",
        r"BAG|BLT|BELT|B1BA|B2BA|B1EA|B2EA|B1BS|B2BS|B1ES|B2ES",
    ),
    (
        "cargo",
        "Freight and mail",
        r"CARGO|CGO|FREIGHT|MAIL|TONS|WGT|WEIGHT",
    ),
    (
        "boarding",
        "Boarding and the final call",
        r"BOARD|BOA[CO]|FCAL|FINAL_CALL",
    ),
    (
        "runway",
        "Which runway it used",
        r"RUNWAY|RWYA|RWYD|^RWY",
    ),
    (
        "punctuality",
        "On-time performance and turnaround",
        r"^OTP$|_ONTIME|ONTIME|^TAT$|FOR_OTP|FOR_ATM|PUNCTUAL",
    ),
    (
        "handling",
        "Ground handling and the operational zone",
        # `ACGT`/`ACOR`/`AEGT` are named one by one rather than by a stem pattern: they are the
        # three ground-handling milestones whose names carry no word an agent would search for,
        # and a loose `^AC` would swallow half the aircraft group.
        r"GROUND_HANDLER|ZONE_|^REMP$|^BAZ1$|HANDLER|^ACGT$|^ACOR$|^AEGT$",
    ),
    (
        "classification",
        "What kind of flight it is",
        r"NATURE|TTYP|SERVICE|STYP|ARR_DEP_FLG|ADID|DOM|INT|SEASON|^IS_|FTYP|FLIGHT_TYPE"
        r"|HOME_AIRPORT|HOPO|SUFFIX|FLNS|^JFNO$|DAY_DOO|BOOK_LOAD|ENRC",
    ),
)


# --- The client's KPI arithmetic ----------------------------------------------------------------
#
# WHY THESE ARE PYTHON CONSTANTS AND NOT A THIRD JSON FILE. "Everything is generated, nothing is
# typed" is a rule about claims made ABOUT THE DATA — names, types, value sets — which must come
# off `profile.json` rather than out of somebody's head. A KPI FORMULA is not that kind of claim:
# it comes from BIAL's own workbook whichever file holds it, and it cannot be derived from a
# profile at all. Here it is at least read by ruff, ty, mypy and pyright, and the one check that
# does matter — every column a measure names is one of the described 131 — runs identically over
# a tuple.
#
# SIX OF THE SIXTEEN NAMED A COLUMN THE 10 SEPTEMBER CUT REMOVED, and all six are re-pointed
# below. Every one is a correction rather than a judgement call:
#
#   SCHLD_DEP_TIME_STOD -> SCHEDULED_OFF_BLOCK_TIME_SOBT   verified exact duplicate
#   SCHLD_ARR_TIME_STOA -> SCHEDULED_IN_BLOCK_TIME_SIBT    verified exact duplicate
#   ARR_DEP_FLG_ADID    -> dropped from the two movement measures. It was listed to enable a
#                          BREAKDOWN, not because either formula needs one: "arrivals +
#                          departures" is every movement, and FOR_ATM (0/1, never null) is
#                          already the flag that says a flight counts as one. The peak-hour
#                          bucket needs no direction either, because SIBT_SOBT_TIME is the merged
#                          scheduled time and is present on arrivals and departures alike.
#
# Departure and Arrival OTP never needed the flag: SCHEDULED_OFF_BLOCK_TIME_SOBT is null on every
# arrival row, so those formulas self-select.

MAPPING: Final[tuple[tuple[str, str, str], ...]] = (
    ("AIBT", "ACTUAL_IN_BLOCK_TIME_AIBT", ""),
    ("AOBT", "ACTUAL_OFF_BLOCK_TIME_AOBT", ""),
    ("ALDT", "ACTUAL_LANDING_TIME_ALDT", ""),
    ("ATOT", "ACTUAL_TAKEOFF_TIME_ATOT", ""),
    ("STD", "SCHEDULED_OFF_BLOCK_TIME_SOBT", ""),
    ("STA", "SCHEDULED_IN_BLOCK_TIME_SIBT", ""),
    (
        "ATD",
        "ACTUAL_OFF_BLOCK_TIME_AOBT",
        "their formula says ATD, which does not exist; off-block chosen over take-off",
    ),
    (
        "ATA",
        "ACTUAL_IN_BLOCK_TIME_AIBT",
        "their formula says ATA, which does not exist; in-block chosen over landing",
    ),
)
"""Their formula vocabulary -> the column that actually holds it. Where the reading is a CHOICE
rather than a lookup the third element says so, and the rendered header prints it, because
off-block and take-off are ten to twenty-five minutes apart and Departure OTP means a different
number depending which was picked."""

MEASURES: Final[tuple[tuple[str, str, str, str], ...]] = (
    (
        "Turnaround time",
        "next AOBT - AIBT for the same registration",
        "ACTUAL_OFF_BLOCK_TIME_AOBT, ACTUAL_IN_BLOCK_TIME_AIBT, AIRCRAFT_REGN",
        "90 min wide-body / 45 min narrow-body",
    ),
    (
        "Departure OTP",
        "share of departures where AOBT <= SOBT + 15 min",
        "ACTUAL_OFF_BLOCK_TIME_AOBT, SCHEDULED_OFF_BLOCK_TIME_SOBT, FOR_OTP",
        "> 85%",
    ),
    (
        "Arrival OTP",
        "share of arrivals where AIBT <= SIBT + 15 min",
        "ACTUAL_IN_BLOCK_TIME_AIBT, SCHEDULED_IN_BLOCK_TIME_SIBT, FOR_OTP",
        "> 85%",
    ),
    (
        "Taxi-out time",
        "ATOT - AOBT",
        "ACTUAL_TAKEOFF_TIME_ATOT, ACTUAL_OFF_BLOCK_TIME_AOBT",
        "< 12 min",
    ),
    (
        "Taxi-in time",
        "AIBT - ALDT",
        "ACTUAL_IN_BLOCK_TIME_AIBT, ACTUAL_LANDING_TIME_ALDT",
        "< 10 min",
    ),
    (
        "Average departure delay",
        "AOBT - SOBT",
        "ACTUAL_OFF_BLOCK_TIME_AOBT, SCHEDULED_OFF_BLOCK_TIME_SOBT",
        "< 15 min",
    ),
    (
        "Average arrival delay",
        "AIBT - SIBT",
        "ACTUAL_IN_BLOCK_TIME_AIBT, SCHEDULED_IN_BLOCK_TIME_SIBT",
        "< 15 min",
    ),
    (
        "Gate occupancy / ground time",
        "AOBT - AIBT",
        "ACTUAL_OFF_BLOCK_TIME_AOBT, ACTUAL_IN_BLOCK_TIME_AIBT",
        "minimise",
    ),
    (
        "Total aircraft movements",
        "count of flights with FOR_ATM = 1, deduped on the flight key",
        "FOR_ATM, AODB_AFTTAB_PK_URNO",
        "set daily by BIAL",
    ),
    (
        "Peak hour movements",
        "group by hour of SIBT_SOBT_TIME, count FOR_ATM = 1, take the largest",
        "SIBT_SOBT_TIME, FOR_ATM, AODB_AFTTAB_PK_URNO",
        "stay within declared capacity",
    ),
    (
        "Runway throughput",
        "take-offs + landings per hour",
        "ACTUAL_TAKEOFF_TIME_ATOT, ACTUAL_LANDING_TIME_ALDT, RUNWAY",
        "limited by runway capacity",
    ),
    (
        "Departure queue time",
        "ATOT - AOBT",
        "ACTUAL_TAKEOFF_TIME_ATOT, ACTUAL_OFF_BLOCK_TIME_AOBT",
        "minimise",
    ),
    (
        "Airborne time",
        "ALDT - ATOT",
        "ACTUAL_LANDING_TIME_ALDT, ACTUAL_TAKEOFF_TIME_ATOT",
        "varies by route",
    ),
    (
        "Flights exceeding turnaround SLA",
        "count where turnaround > the target for the body type",
        "TAT, AIRCRAFT_BODY_TYPE",
        "< 5%",
    ),
    (
        "Aircraft waiting for stand",
        "AIBT - ALDT, flagged when unusually long",
        "ACTUAL_IN_BLOCK_TIME_AIBT, ACTUAL_LANDING_TIME_ALDT",
        "minimise",
    ),
    (
        "Passenger throughput",
        "sum of passengers, domestic and international split",
        "NO_OF_PAX_PAXT, TOTAL_DOM_PAX, TOTAL_INT_PAX, FOR_PAX",
        "set daily by BIAL",
    ),
)
"""The measures the table can answer, with the arithmetic written against real columns. Anything
needing a denominator the table does not carry is in `PARTIAL` or `UNANSWERABLE` instead."""

UNANSWERABLE: Final[tuple[tuple[str, str], ...]] = (
    ("Flight cancellation rate", "the flight-status column held no value in any profiled row"),
    ("Flight diversion rate", "the diversion column held no value in any profiled row"),
    (
        "Airside operational incidents",
        "needs an Incident Management System, which is not connected",
    ),
    ("Runway incursions", "needs a Safety System, which is not connected"),
)
PARTIAL: Final[tuple[tuple[str, str], ...]] = (
    ("Stand utilisation", "needs 'available stand time', which the table does not carry"),
    ("Contact / remote stand utilisation", "needs the contact-vs-remote classification too"),
    ("Apron congestion index", "needs a count of available stands"),
    (
        "Stand conflict events",
        "derivable from stand and times, but the allocation rules are not here",
    ),
)
"""Named so the agent says so rather than approximating, and so a reviewer can see what the table
cannot do. The two empty columns are described rather than named: they are outside the working
set, so naming them here would point the agent at a column this block does not describe."""


# --- Reading the inputs --------------------------------------------------------------------------


def load_profile() -> dict[str, Any]:
    """The committed column profile — the authority on what the data holds."""
    path = DATA_DIR / "profile.json"
    if not path.is_file():
        raise SystemExit(f"cannot find {path} — it is the only record of the real schema")
    parsed: dict[str, Any] = json.loads(path.read_text(encoding="utf-8"))
    return parsed


def load_definitions() -> tuple[tuple[str, str], ...]:
    """(name, definition) for every column in the working set, in the workbook's own order.

    Two fields are read and no others. `source` and `client_status` carry which definitions the
    client has confirmed and which are ours; nothing here branches on them, nothing marks them and
    no test counts them (owner ruling, 2026-09-11) — the whole distinction ends the day the client
    returns the workbook, and a tiering system built for it would outlive it."""
    path = DATA_DIR / "definitions.json"
    if not path.is_file():
        raise SystemExit(f"cannot find {path} — it is the working column list")
    rows: list[dict[str, Any]] = json.loads(path.read_text(encoding="utf-8"))["columns"]
    return tuple((str(row["name"]), str(row["definition"])) for row in rows)


# --- Rendering one column ------------------------------------------------------------------------

_OPAQUE_NAME: Final = re.compile(r"[A-Za-z0-9]{2,5}")

_ASCII_FOLDS: Final = str.maketrans({"\u2014": "-", "\u2013": "-", "\u2018": "'", "\u2019": "'"})
"""The smart punctuation a definition written in a word processor carries, folded to ASCII.

DELIBERATELY SHORT, AND EVERYTHING ELSE IS A FAILURE. A fold table that silently swallowed any
character it was handed would let a definition ship an ellipsis or a non-breaking space into a
14 KB file whose whole test strategy rests on byte comparison — the ellipsis case is not
hypothetical, it arrived in `ENRC`'s corrected text and was caught here rather than three steps
later by the artefact-wide ASCII assertion."""


def simple_type(dtypes: list[str]) -> str:
    """The dtype in words the model can act on, not polars' spelling of it.

    FAIL-FIRST ON BOTH UNKNOWNS, because either one ships a confident falsehood. A column whose
    files disagree on dtype has no single answer and `dtypes[0]` would silently pick one; a dtype
    no branch below names would fall through to "text" and label a numeric column as free text,
    which is the mislabelling `value_clause` trusts this function to prevent."""
    if len(dtypes) != 1:
        raise SystemExit(
            f"{dtypes!r} holds {len(dtypes)} dtypes, not one — a column whose files disagree "
            "has no single type to state. Re-profile, or decide the type and record it."
        )
    dtype = dtypes[0]
    if dtype.startswith("Datetime"):
        return "timestamp"
    if dtype == "Int64":
        return "integer"
    if dtype.startswith("Decimal"):
        return "decimal"
    if dtype == "String":
        return "text"
    raise SystemExit(
        f"{dtype!r} matches no known dtype. Add a branch naming it rather than letting it fall "
        "through to 'text' — a mislabelled type is a query written against the wrong shape."
    )


def group_of(name: str) -> str:
    """The domain group `name` belongs to. FAIL-FIRST on a column no pattern claims: a catch-all
    group is a silent place for a column to go missing from the one screen an agent looks at."""
    for key, _, pattern in GROUPS:
        if re.search(pattern, name):
            return key
    raise SystemExit(
        f"{name!r} matches no domain group. Add it to the pattern of the group an agent "
        "would look in for it — do not add an 'everything else' bucket."
    )


def is_opaque(name: str) -> bool:
    """True when the NAME alone tells the agent nothing.

    108 of the 131 are self-describing — `ACTUAL_OFF_BLOCK_TIME_AOBT` needs no gloss and glossing
    it would be prose the model can already read off the name. The other 23 are bare codes
    (`ACGT`, `TAT`, `OTP`, `BAZ1`), and for those the name is evidence of nothing at all."""
    return bool(_OPAQUE_NAME.fullmatch(name))


def ascii_only(text: str) -> str:
    """Whitespace-collapsed and ASCII.

    The substitutions are the smart punctuation a definition typed in a word processor carries.
    Anything else non-ASCII STOPS THE BUILD: the artefact is built on macOS and shipped from a
    Windows VM, a test asserts the whole file is ASCII, and a character this function does not
    recognise is one nobody decided about — better a named failure here than a mojibake in
    somebody's prompt or a byte-diff nobody can read."""
    folded = " ".join(text.translate(_ASCII_FOLDS).split())
    if not folded.isascii():
        stray = sorted({character for character in folded if not character.isascii()})
        raise SystemExit(
            f"non-ASCII in a rendered definition: {stray} in {folded[:60]!r}. "
            "Rewrite it in ASCII, or add the character to _ASCII_FOLDS if it has an obvious fold."
        )
    return folded


def value_clause(column: dict[str, Any]) -> str:
    """The `= ...` half of a column line, or `""` when the column has no vocabulary to state.

    THREE OUTCOMES, AND THE MIDDLE ONE IS A FLOOR RATHER THAN A COUNT. `values` is the union of
    the value sets thirteen files happened to record and `distinct_max` is the most distinct
    values any ONE of them held; neither is the number the column can hold, and they do not even
    agree with each other (`AIRCRAFT_REGN` records 1,013 values against a `distinct_max` of
    2,652). The larger of the two is the tightest lower bound both support, which is why the
    wording is "at least".

    VERBATIM AND QUOTED, never stripped — see the module docstring on `'Indigo '`."""
    values = [str(value) for value in (column.get("values") or [])]
    # THE CLIENT'S BYTES, CHECKED BEFORE THEY BECOME A LINE. `ascii_only` folds the definitions WE
    # write; values are rendered verbatim by contract and never pass through it. A value holding a
    # newline does not corrupt its line, it SPLITS one — and the halves read as two more columns
    # in a file whose entire format is one column per line, which is a fabricated column in the
    # one artefact that exists to make fabrication impossible. Checked per value, not on the
    # assembled text, because by then the text is full of legitimate newlines.
    for value in values:
        if not value.isascii() or not value.isprintable():
            raise SystemExit(
                f"{column['name']}'s value {value!r} is not printable ASCII. Values are rendered "
                "verbatim, so this is the client's byte, not ours — decide how to represent it."
            )
    if not values or simple_type(column["dtypes"]) in {"integer", "decimal"}:
        return ""
    distinct = int(column.get("distinct_max") or 0)
    if distinct > NOT_A_VOCABULARY_ABOVE:
        return "= free text, not a code list"
    if len(values) <= INLINE_VALUE_MAX:
        return "= " + ", ".join(f"'{value}'" for value in values)
    return f"= at least {max(distinct, len(values)):,} distinct values observed"


def column_line(column: dict[str, Any], definition: str) -> str:
    """One line for one column: `NAME (type) = values -- meaning`.

    SELF-DESCRIBING RATHER THAN POSITIONAL, and that is a deliberate 109 tokens. A tab-separated
    row is 2.4% cheaper and asks the model to remember that field three is the value set because a
    header said so ninety lines earlier; misread it and the column's type is wrong, the query is
    wrong, the dashboard is wrong, and nothing catches it.

    Four smaller choices inside the shape. The TYPE appears only when it is not text, because text
    is the majority and the legend states the default, so the marked ones stand out. `=` is the
    commonest "this is that" idiom there is. `--` is SQL's own line-comment marker and therefore
    goes last, where a line comment goes. And the whole line is ASCII."""
    parts = [column["name"]]
    kind = simple_type(column["dtypes"])
    if kind != "text":
        parts.append(f"({kind})")
    clause = value_clause(column)
    if clause:
        parts.append(clause)
    # A gloss for the bare-code names and for no others, and the WHOLE definition rather than its
    # first clause. Taking the first clause dropped `OTP`'s sign convention ("negative is early")
    # and `BAGS`'s "stored as a decimal string" — the two facts on those lines most likely to
    # produce a green build with wrong numbers.
    if is_opaque(column["name"]) and definition.strip():
        parts.append(f"-- {ascii_only(definition)}")
    return " ".join(parts)


# --- Rendering the whole artefact ---------------------------------------------------------------

_GENERATED_BANNER: Final = (
    "# GENERATED FILE -- do not hand-edit. Built by "
    "backend/scripts/build_connector_catalogue.py\n"
    f"# from backend/data/connectors/{CONNECTOR_KEY}/{{profile,definitions}}.json."
)


WRAP: Final = 98
"""One wrap width for every authored paragraph. The paragraphs interpolate dates and counts read
off the profile, so hand-wrapping them would re-wrap — differently — the day a figure changed
length, and the drift test compares bytes."""


def _day_month_year(stamp: str) -> str:
    """`20260101` -> `1 January 2026`, for a header the model reads rather than parses."""
    day = date(int(stamp[0:4]), int(stamp[4:6]), int(stamp[6:8]))
    return f"{day.day} {day:%B} {day.year}"


def _month_year(stamp: str) -> str:
    """`2022-08-24 12:30:00` -> `August 2022`.

    The scheduled-time spread is stated in MONTHS on purpose. It is the widest pair of values
    thirteen files happened to contain, and a to-the-day figure invites reading a sample's
    extremes as the table's bounds — which is the whole class of mistake this block avoids."""
    return f"{date(int(stamp[0:4]), int(stamp[5:7]), 1):%B} {stamp[0:4]}"


def _paragraphs(*blocks: str) -> str:
    """Wrap each paragraph to `WRAP` and join them with a blank line.

    NEITHER HYPHENS NOR LONG WORDS ARE BREAK POINTS. The default wrapper split `hard-coding` across
    two lines, and the same rule applied to a column name would put `SCHEDULED_OFF_BLOCK_` on one
    line and `TIME_SOBT` on the next — a column the model would then reasonably believe exists.
    Every token here fits inside the width, so refusing both break points costs nothing and closes
    the one way this wrapper could invent a name."""
    return "\n\n".join(
        textwrap.fill(
            " ".join(block.split()), width=WRAP, break_on_hyphens=False, break_long_words=False
        )
        for block in blocks
    )


def padding_reach(described: list[dict[str, Any]]) -> tuple[int, int, int]:
    """(padded values, columns holding one, how many of those columns list their values).

    THE THIRD NUMBER IS THE POINT AND IT IS SMALL. Only two of the six described columns holding a
    space-padded value are under the inline cap, so for the other four the block states a count
    and the padding is INVISIBLE in it — `AIRLINE_NAME`'s space-padded 'AKASA AIR' is never shown.
    The header therefore has to say the trap exists rather than let the lines demonstrate it, and
    has to say so without implying the lines will."""
    padded_values = 0
    padded_columns: list[dict[str, Any]] = []
    for column in described:
        padded = [v for v in (column.get("values") or []) if str(v) != str(v).strip()]
        if padded:
            padded_values += len(padded)
            padded_columns.append(column)
    # DERIVED FROM THE RENDERER, NOT RE-SPELLED. `value_clause` decides whether a column's values
    # are listed on three conditions, not one; counting `len(values) <= INLINE_VALUE_MAX` here
    # restated a third of that rule, so the header's sentence could disagree with the lines it
    # describes. Asking the renderer is the only spelling that cannot drift.
    listed = sum(1 for c in padded_columns if value_clause(c).startswith("= '"))
    return padded_values, len(padded_columns), listed


def header(profile: dict[str, Any], columns: dict[str, dict[str, Any]], names: list[str]) -> str:
    """The rules a query has to obey to be right, and the provenance of everything below them.

    EVERY NUMBER AND DATE HERE IS READ OFF THE PROFILE — the period, the file count, the spread of
    scheduled times, both column totals, and how far the padding trap reaches. The one figure that
    was DROPPED rather than rendered is the dedupe over-report ("about 11%"): the only measurement
    behind it is a thirty-day window on the REPLICA lake, and the 10 September full-lake scan
    showed that window's absolute figures to be 21x off production, so the ratio is unproven at
    scale. The rule stands without a number."""
    period = profile["period"]
    # NAMED IN THE ERROR, LIKE EVERY OTHER MISSING COLUMN. `build` already refuses when a
    # definition has no profile row; this one is referenced by the header rather than by the
    # definitions, so a re-profile that drops it would surface as a bare KeyError from a script
    # whose whole posture is to say what is wrong and stop.
    if "SIBT_SOBT_TIME" not in columns:
        raise SystemExit(
            "SIBT_SOBT_TIME has no row in profile.json, and the header's date-filter rule is "
            "written from it. Re-profile with the column, or rewrite the rule around its "
            "replacement — do not ship the header without it."
        )
    flight_time = columns["SIBT_SOBT_TIME"]
    described = [columns[name] for name in names]
    padded_values, padded_columns, padded_inline = padding_reach(described)
    return "FLIGHT DATA -- one table, tb_flight_fact_report\n\n" + _paragraphs(
        f"""
        WHAT THIS IS, AND WHEN IT WAS MEASURED. Everything below was read from {period["files"]}
        daily load files covering {_day_month_year(period["from"])} to
        {_day_month_year(period["to"])}. It says what those files held; it is not a promise about
        what the table holds today, and no value list here is a closed set. {len(names)} of the
        table's {len(columns)} columns are described -- the rest are outside the agreed working
        set and are not listed.
        """,
        """
        ONE ROW PER FLIGHT PER LOAD. The same flight is re-stated in later files as its actual
        times are filled in, so ALWAYS collapse to one row per flight by keeping the highest
        LAST_UPDATE_DATE_TIME for each AODB_AFTTAB_PK_URNO. Skip that and you over-report flight
        counts, and every average over those rows is wrong in the same direction.
        """,
        f"""
        THE FILE'S DATE IS THE LOAD DATE, NOT THE FLIGHT DATE. These files are stamped
        {_day_month_year(period["from"])} to {_day_month_year(period["to"])}, and the flights
        inside them are scheduled between {_month_year(str(flight_time["min"]))} and
        {_month_year(str(flight_time["max"]))}: a load date bounds neither end of the flights in
        it. To get flights in a date range, read the files and then filter ROWS on
        SIBT_SOBT_TIME, the merged scheduled time, present on arrivals and departures alike. Do
        NOT filter on SCHEDULED_OFF_BLOCK_TIME_SOBT -- it is the DEPARTURE time and is null on
        every arrival row, so it silently drops half the traffic.
        SCHEDULED_IN_BLOCK_TIME_SIBT has the mirror-image problem.
        """,
        # DESCRIBED IN WORDS RATHER THAN SHOWN, for two separate reasons. An earlier draft wrote
        # the padded value out -- `'AKASA AIR    '` -- and the paragraph wrapper collapsed the run
        # of spaces to one, so the example illustrating invisible padding was itself quietly
        # de-padded. And most of the padded columns hold too many values to list, so the lines
        # genuinely cannot demonstrate it; the sentence must not imply they will.
        f"""
        TRIM TEXT BEFORE GROUPING OR COMPARING. Some text columns hold the same value twice, once
        padded with trailing spaces: AIRLINE_NAME carries 'AKASA AIR' and a second, space-padded
        copy of it as two distinct values, so a plain GROUP BY shows that airline twice.
        {padded_values} values across {padded_columns} of the columns below are padded like that,
        and only {padded_inline} of those {padded_columns} list their values here -- for the rest
        the padding is invisible in this block, so assume it rather than look for it. Some columns
        also use an EMPTY STRING rather than null, which IS NOT NULL does not filter --
        FLIGHT_NATURE_TTYP_DESC is one, and FLIGHT_TYPE_FTYP holds a single space. Trim, and treat
        blank as missing.
        """,
        """
        Column names ending in a four-letter code follow the EUROCONTROL A-CDM convention, where
        the first three letters name the milestone (AIBT in-block, AOBT off-block, ALDT landing,
        ATOT take-off, TOBT target off-block) and the fourth is an attribute of it.
        """,
    )


def measures_header() -> str:
    """BIAL's own KPI definitions, one self-describing line each."""
    return "\n\n".join(
        (
            _paragraphs(
                """
                MEASURES -- BIAL's own KPI definitions. Prefer these over inventing arithmetic.
                When a request names one of these, use the formula and the columns given here:
                they are the airport's definitions, not ours. Every one still obeys the rules
                above -- collapse to one row per flight, and filter dates on SIBT_SOBT_TIME.
                """
            ),
            "\n".join(
                f"{name} = {formula}; uses {used}" + (f"; target {target}" if target else "")
                for name, formula, used, target in MEASURES
            ),
            _paragraphs(
                """
                FOR_ATM, FOR_OTP and FOR_PAX are BIAL's own inclusion flags (0/1, never null).
                Filter on the matching flag when computing movements, punctuality or passenger
                totals -- a flight with the flag at 0 is deliberately outside that KPI's
                denominator.
                """,
                """
                TWO ASSUMPTIONS WE MADE, because their formulas name fields the table does not
                have:
                """,
            ),
            "\n".join(f"  {theirs} -> {ours} ({note})" for theirs, ours, note in MAPPING if note),
            "CANNOT BE ANSWERED from this table -- say so rather than approximating:",
            "\n".join(f"  {name} -- {why}" for name, why in (*UNANSWERABLE, *PARTIAL)),
        )
    )


LEGEND: Final = _paragraphs(
    """
    HOW TO READ THE COLUMN LINES. One line per column, grouped by what it is about. A column
    with no type in brackets holds text; otherwise its type is stated -- (timestamp), (integer),
    (decimal). A `--` at the end of a line is what that column means.
    """,
    """
    A line reading `= 'A', 'B'` lists the values that were present when this metadata was built.
    THAT IS NOT A CLOSED SET: derive filter and dropdown values from the data at runtime rather
    than hard-coding these, or the day a new code appears your app silently drops those rows.
    A line reading `= at least N distinct values observed` means there were too many to list --
    read them from the data with SELECT DISTINCT at runtime.
    """,
)


def build(profile: dict[str, Any], definitions: tuple[tuple[str, str], ...]) -> str:
    """The whole artefact: the rules, the measures, the legend, then the columns by domain."""
    columns = {str(column["name"]): column for column in profile["columns"]}

    missing = [name for name, _ in definitions if name not in columns]
    if missing:
        raise SystemExit(
            f"{len(missing)} column(s) in definitions.json have no row in profile.json "
            f"({', '.join(missing[:5])}) — the two files describe different tables."
        )

    grouped: dict[str, list[str]] = defaultdict(list)
    for name, definition in definitions:
        grouped[group_of(name)].append(column_line(columns[name], definition))

    body = "\n\n".join(
        f"## {key} -- {label}\n" + "\n".join(grouped[key])
        for key, label, _ in GROUPS
        if grouped[key]
    )
    return (
        f"{_GENERATED_BANNER}\n\n"
        f"{header(profile, columns, [n for n, _ in definitions])}\n\n"
        f"{measures_header()}\n\n"
        f"{LEGEND}\n\n"
        f"{body}\n"
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--stdout",
        action="store_true",
        help="print exactly what would be written, and write nothing",
    )
    args = parser.parse_args()

    text = build(load_profile(), load_definitions())
    if args.stdout:
        sys.stdout.write(text)
        return 0
    TARGET.parent.mkdir(parents=True, exist_ok=True)
    # `newline=""` so the LF stays an LF on the Windows build host as well.
    with TARGET.open("w", encoding="utf-8", newline="") as handle:
        handle.write(text)
    print(f"wrote {TARGET} ({len(text):,} bytes, {text.count(chr(10)):,} lines)", file=sys.stderr)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
