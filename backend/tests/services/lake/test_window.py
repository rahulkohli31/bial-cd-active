"""Which files a window selects — one table, and it IS the specification.

EVERY ROW HERE IS A TRAP THE REAL LAKE SETS, with a measured consequence. None of them raises an
error where the mistake was made; each produces a file set that is quietly wrong, and an app built
on it is green and lying. That is why this function takes a list of `(name, size)` rather than a
client: with no Azure and no clock, every trap is a table-driven assertion that runs anywhere.

THE ORDERING CLAIM IS THE POINT OF THE UNIT. Match the NAME first, then look at the size. On a
hierarchical-namespace account a flat listing returns directories as zero-length entries, so
filtering on size first skips four folders AND hides the files that really are broken. Invert the
two and `test_directory_placeholders_are_dropped_by_the_name_not_the_size` goes red on its own.

WHAT A WINDOW MEANS (the owner's ruling, and it supersedes the origin's phrasing). A window is
the files whose OWN DATE falls inside it. Unreadable dates are skipped and counted; the count is
never topped up by reaching further back. So a run of failed loads makes a window SMALLER, not
OLDER — and the skipped count is the only visible trace of a bad stretch, which is why it is
reported rather than logged.
"""

from __future__ import annotations

from datetime import date

import pytest

from src.core.connectors import CONNECTORS
from src.db.models.project_connector import ConnectorWindowKind
from src.services.lake.client import LakeEntry
from src.services.lake.window import LAKE_FILE_PATTERN, select_files

_DICE = CONNECTORS["dice"]
_ROOT = "AOS/tb_flight_fact_report/"

# The measured shape: `<prefix>/<year>/<UPPERCASE MONTH>/tb_flight_fact_report_<YYYYMMDD>.parquet`,
# where the MONTH FOLDER IS THE LOAD MONTH and the date in the filename is the load date. They
# disagree for every file loaded on the first of a month — see the month-folder case below.
_MONTHS = {
    6: "JUNE",
    7: "JULY",
    8: "AUGUST",
    9: "SEPTEMBER",
    10: "OCTOBER",
}


def _name(day: date, *, folder_month: int | None = None) -> str:
    month = _MONTHS[folder_month if folder_month is not None else day.month]
    return f"{_ROOT}{day.year}/{month}/tb_flight_fact_report_{day:%Y%m%d}.parquet"


def _file(day: date, size: int = 4_000, *, folder_month: int | None = None) -> LakeEntry:
    return LakeEntry(_name(day, folder_month=folder_month), size)


def _window(start: date, end: date):
    """A resolved window, exactly as `resolve_window` hands one over.

    Built by hand rather than through the resolver: this unit's job is to honour whatever pair it
    is given, and running the resolver here would couple these assertions to a clock."""
    from src.core.connectors import ResolvedWindow

    return ResolvedWindow(
        effectively_on=True,
        kind=ConnectorWindowKind.RELATIVE,
        start=start,
        end=end,
        days=(end - start).days + 1,
        clamped=False,
        earliest=start,
        latest=end,
    )


def _days(*names: str) -> list[str]:
    return list(names)


def _selected(selection) -> list[date]:
    return [item.day for item in selection.files]


# --- the happy path -----------------------------------------------------------------------------


def test_a_healthy_stretch_selects_every_day_in_the_window_newest_first() -> None:
    listing = [_file(date(2026, 8, day)) for day in range(1, 32)]
    window = _window(date(2026, 8, 2), date(2026, 8, 31))

    selection = select_files(listing, window, max_files=_DICE.max_window_days)

    assert len(selection.files) == 30
    assert _selected(selection)[0] == date(2026, 8, 31), "newest first"
    assert _selected(selection)[-1] == date(2026, 8, 2)
    assert selection.skipped == 0
    assert selection.total_bytes == 30 * 4_000


def test_the_listings_order_does_not_decide_the_result() -> None:
    """Azure lists lexically, which is not chronological across a month boundary. The selection
    sorts for itself, so a caller can hand over the listing exactly as it arrived."""
    listing = [_file(date(2026, 9, 1)), _file(date(2026, 8, 30)), _file(date(2026, 8, 31))]
    window = _window(date(2026, 8, 30), date(2026, 9, 1))

    selection = select_files(listing, window, max_files=_DICE.max_window_days)

    assert _selected(selection) == [date(2026, 9, 1), date(2026, 8, 31), date(2026, 8, 30)]


# --- trap 1: the month folder is the LOAD month --------------------------------------------------


def test_a_file_filed_under_the_next_months_folder_is_still_selected_by_its_own_date() -> None:
    """★ THE TRAP THAT PROVES THE LISTING IS TRUSTED AND THE FOLDER IGNORED.

    The 31 August file lives under `2026/SEPTEMBER/`, because the load job ran the next morning.
    A path constructed from the requested date — `.../2026/AUGUST/...` — finds nothing at all,
    and finding nothing reads exactly like a permissions failure. The only correct move is to
    LIST and read the date out of the filename."""
    listing = [_file(date(2026, 8, 31), folder_month=9)]
    window = _window(date(2026, 8, 25), date(2026, 8, 31))

    selection = select_files(listing, window, max_files=_DICE.max_window_days)

    assert _selected(selection) == [date(2026, 8, 31)]
    assert "SEPTEMBER" in selection.files[0].name, "the folder is not where the date came from"


# --- trap 2: directories are zero-length entries -------------------------------------------------


def test_directory_placeholders_are_dropped_by_the_name_not_the_size() -> None:
    """★ THE ORDERING TEST. Four folders, each reported by a flat listing as a zero-length entry.

    They are dropped because their NAMES do not match a file, and they are NOT counted as broken
    files. Filter on size first and all four vanish into the skipped count, which then reads as
    "four days of the lake failed to load" — a sentence an operator would act on."""
    listing = [
        LakeEntry(f"{_ROOT}2026", 0),
        LakeEntry(f"{_ROOT}2026/AUGUST", 0),
        LakeEntry(f"{_ROOT}2026/SEPTEMBER", 0),
        LakeEntry(f"{_ROOT}2026/SEPTEMBER/_SUCCESS", 0),
        _file(date(2026, 9, 1)),
    ]
    window = _window(date(2026, 8, 20), date(2026, 9, 1))

    selection = select_files(listing, window, max_files=_DICE.max_window_days)

    assert _selected(selection) == [date(2026, 9, 1)]
    assert selection.skipped == 0, "a folder is not a failed load"


# --- trap 3: a zero-byte file IS a failed load ---------------------------------------------------


def test_zero_byte_stubs_are_excluded_and_counted() -> None:
    """★ A name that matches and is zero bytes is the upstream load having failed and left a stub.

    An existence check passes and the parquet reader then throws "file is too short". Excluded
    from the result — and COUNTED, because "the lake was quiet" and "the lake was broken" are
    different sentences and nothing else in the answer tells them apart."""
    listing = [
        _file(date(2026, 9, 1)),
        _file(date(2026, 9, 2), size=0),
        _file(date(2026, 9, 3), size=0),
        _file(date(2026, 9, 4)),
    ]
    window = _window(date(2026, 9, 1), date(2026, 9, 4))

    selection = select_files(listing, window, max_files=_DICE.max_window_days)

    assert _selected(selection) == [date(2026, 9, 4), date(2026, 9, 1)]
    assert selection.skipped == 2


def test_a_stub_outside_the_window_is_not_counted_against_it() -> None:
    """The count answers "how many days THIS WINDOW could not read", so a broken day from three
    months ago is not one of them. Asserted explicitly because the obvious implementation —
    counting every zero-byte match in the listing — passes every other case in this file."""
    listing = [
        _file(date(2026, 6, 1), size=0),
        _file(date(2026, 9, 1)),
    ]
    window = _window(date(2026, 9, 1), date(2026, 9, 4))

    selection = select_files(listing, window, max_files=_DICE.max_window_days)

    assert _selected(selection) == [date(2026, 9, 1)]
    assert selection.skipped == 0


def test_a_bad_stretch_makes_the_window_smaller_rather_than_older() -> None:
    """★ THE OWNER'S RULING, as the one assertion that separates the two readings of a window.

    Five days requested, three of them unreadable. The answer is the two that survived plus the
    number that did not — NEVER the two plus three older files reached for to make the count
    whole. A window means exactly the dates the picker showed."""
    listing = [_file(date(2026, 8, day)) for day in range(20, 27)]
    listing += [_file(date(2026, 9, day), size=0) for day in (1, 2, 3)]
    listing += [_file(date(2026, 9, 4)), _file(date(2026, 9, 5))]
    window = _window(date(2026, 9, 1), date(2026, 9, 5))

    selection = select_files(listing, window, max_files=_DICE.max_window_days)

    assert _selected(selection) == [date(2026, 9, 5), date(2026, 9, 4)]
    assert selection.skipped == 3
    assert all(item.day >= date(2026, 9, 1) for item in selection.files), (
        "nothing outside the window may be reached for to top the count up"
    )


# --- trap 4: the calendar is sparse --------------------------------------------------------------


def test_a_sparse_calendar_selects_only_what_is_inside_the_window() -> None:
    """36 files across three months against a 30-day window. Whatever few fall inside is the
    answer; the window never widens to reach a target count."""
    listing = [_file(date(2026, 7, day)) for day in range(1, 13)]
    listing += [_file(date(2026, 8, day)) for day in range(1, 13)]
    listing += [_file(date(2026, 9, day)) for day in range(1, 13)]
    window = _window(date(2026, 8, 15), date(2026, 9, 13))

    selection = select_files(listing, window, max_files=_DICE.max_window_days)

    assert _selected(selection) == [date(2026, 9, day) for day in range(12, 0, -1)]
    assert len(selection.files) == 12


def test_an_empty_listing_selects_nothing_and_is_not_an_error() -> None:
    """A lake that has not loaded yet is not a fault — it is a window with no files in it."""
    selection = select_files([], _window(date(2026, 9, 1), date(2026, 9, 5)), max_files=30)

    assert selection.files == ()
    assert selection.skipped == 0
    assert selection.total_bytes == 0


# --- the boundaries ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "day", [date(2026, 9, 1), date(2026, 9, 5)], ids=["first-day", "last-day"]
)
def test_both_ends_of_the_window_are_inclusive(day: date) -> None:
    """The pair the citizen picked is inclusive at both ends — the resolver's `days` is
    `(end - start) + 1` — so a strict comparison here would silently drop the first or last day
    of every window in the product."""
    selection = select_files(
        [_file(day)], _window(date(2026, 9, 1), date(2026, 9, 5)), max_files=30
    )

    assert _selected(selection) == [day]


def test_a_file_dated_after_the_window_is_not_selected() -> None:
    """The window's end is the connector's day-late ceiling, so a file dated TODAY should not
    exist — but an early load can produce one, and it must not be silently admitted into a
    window that was resolved to end yesterday."""
    listing = [_file(date(2026, 9, 7)), _file(date(2026, 9, 8))]
    window = _window(date(2026, 9, 1), date(2026, 9, 7))

    selection = select_files(listing, window, max_files=30)

    assert _selected(selection) == [date(2026, 9, 7)]


# --- near-misses ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    [
        f"{_ROOT}2026/SEPTEMBER/tb_flight_fact_report_20260901.parquet.tmp",
        f"{_ROOT}2026/SEPTEMBER/tb_flight_fact_report_20260901.csv",
        f"{_ROOT}2026/SEPTEMBER/tb_flight_fact_report_2026090.parquet",
        f"{_ROOT}2026/SEPTEMBER/tb_flight_fact_report_.parquet",
        f"{_ROOT}2026/SEPTEMBER/old_tb_flight_fact_report_20260901.parquet",
        f"{_ROOT}2026/SEPTEMBER/tb_flight_fact_summary_20260901.parquet",
    ],
    ids=["trailing-suffix", "wrong-extension", "short-date", "no-date", "prefixed", "wrong-stem"],
)
def test_a_name_that_only_nearly_matches_is_not_selected(name: str) -> None:
    """★ A near-match silently admitted is a file the parquet reader fails on later, in the
    generated app, with no trace of where it came from.

    `prefixed` is the one that needs a left anchor: the verified worked example's regex has none,
    so `old_tb_flight_fact_report_….parquet` matches it as a suffix. The pattern here anchors on
    a path-segment boundary."""
    selection = select_files(
        [LakeEntry(name, 4_000)], _window(date(2026, 9, 1), date(2026, 9, 5)), max_files=30
    )

    assert selection.files == ()
    assert selection.skipped == 0


def test_a_name_carrying_an_impossible_date_is_not_selected() -> None:
    """`20260931` is eight digits and no day. It cannot be placed in a window, so it is dropped
    like any other non-file rather than crashing the selection for every other day."""
    name = f"{_ROOT}2026/SEPTEMBER/tb_flight_fact_report_20260931.parquet"

    selection = select_files(
        [LakeEntry(name, 4_000)], _window(date(2026, 9, 1), date(2026, 9, 30)), max_files=30
    )

    assert selection.files == ()


def test_the_pattern_matches_the_shape_the_lake_actually_produces() -> None:
    """The pattern is a literal, so this is the row that ties it to a real object name — the one
    recorded in the replica lake's own README, folder and all."""
    real = "AOS/tb_flight_fact_report/2026/SEPTEMBER/tb_flight_fact_report_20260831.parquet"

    match = LAKE_FILE_PATTERN.search(real)

    assert match is not None
    assert match.group("day") == "20260831"


# --- the cap -------------------------------------------------------------------------------------


def test_more_files_than_the_cap_returns_the_cap_taking_the_newest() -> None:
    """A CEILING, NOT A TARGET. One file per date and a window no longer than the connector's
    retention means this cannot bite in production — it is the guard for the day that stops
    being true (two files sharing a date, or a resolver that stopped clamping), and it takes the
    newest rather than whatever the listing happened to put first."""
    listing = [_file(date(2026, 8, day)) for day in range(1, 32)]
    window = _window(date(2026, 8, 1), date(2026, 8, 31))

    selection = select_files(listing, window, max_files=5)

    assert _selected(selection) == [date(2026, 8, day) for day in range(31, 26, -1)]
    assert selection.total_bytes == 5 * 4_000, "the total counts what was SELECTED, not what fit"


def test_the_total_is_the_worst_case_the_caller_must_budget_for() -> None:
    """★ Reported so the transfer can refuse BEFORE it writes rather than after.

    Two backfill days at 40 MB apiece alongside ordinary ones: the total is the sum of what was
    actually selected, which is the number a byte budget has to be checked against."""
    listing = [
        _file(date(2026, 9, 1), size=40_000_000),
        _file(date(2026, 9, 2), size=40_000_000),
        _file(date(2026, 9, 3), size=4_000),
    ]
    window = _window(date(2026, 9, 1), date(2026, 9, 3))

    selection = select_files(listing, window, max_files=30)

    assert selection.total_bytes == 80_004_000
    assert selection.total_bytes == sum(item.size for item in selection.files)


# --- one file per date, which the ceiling depends on ---------------------------------------------


def test_a_second_file_for_the_same_date_is_dropped_and_counted() -> None:
    """★ THE PREMISE THE `max_files` CEILING RESTS ON. The pattern anchors on `(?:^|/)`, so a copy
    of a day living in a sub-folder under the prefix — `archive/`, `backup/`, whatever a human
    made — matches exactly as the live file does and carries the same date.

    Left alone that is not a tidy duplicate: a thirty-file ceiling would cover fifteen days while
    still calling itself thirty, and every row of the duplicated day would be counted twice by
    anything that reads the copy. Exactly one entry per date survives, and its bytes are the only
    ones the budget is asked to cover.

    `duplicates` is asserted alongside, because a silent de-duplication would hide the fact that
    the lake's shape has changed — which is the thing an operator needs to know."""
    live = _file(date(2026, 9, 1))
    archived = LakeEntry(f"{_ROOT}archive/{live.name.rsplit('/', 1)[1]}", 9_000)
    window = _window(date(2026, 9, 1), date(2026, 9, 3))

    selection = select_files([archived, live, _file(date(2026, 9, 2))], window, max_files=30)

    assert [item.day for item in selection.files] == [date(2026, 9, 2), date(2026, 9, 1)]
    assert selection.duplicates == 1
    assert selection.total_bytes == 4_000 + min(archived.size, live.size), (
        "only ONE of the two files for 1 Sep may be counted"
    )


def test_the_winner_between_two_copies_is_the_same_one_on_every_read() -> None:
    """★ DETERMINISM, WHICH IS THE ONLY PROPERTY ON OFFER — there is no principled winner between
    two files claiming the same day, so the rule is arbitrary by design. What must not happen is
    the LISTING ORDER deciding: the same lake would then yield different windows on different
    reads, and a number that moves for no reason is worse than one that is consistently the
    second-best answer."""
    window = _window(date(2026, 9, 1), date(2026, 9, 1))
    a = LakeEntry(f"{_ROOT}aaa/tb_flight_fact_report_20260901.parquet", 1_000)
    b = LakeEntry(f"{_ROOT}zzz/tb_flight_fact_report_20260901.parquet", 2_000)

    forwards = select_files([a, b], window, max_files=30)
    backwards = select_files([b, a], window, max_files=30)

    assert forwards.files == backwards.files
    assert forwards.files[0].name == a.name


def test_the_ceiling_now_counts_days_rather_than_files() -> None:
    """★ The pairing that makes the fix worth having: with a duplicate per day, a cap of five used
    to return five FILES covering three days. It now returns five DAYS."""
    listing: list[LakeEntry] = []
    for day in range(1, 11):
        original = _file(date(2026, 9, day))
        listing.append(original)
        listing.append(LakeEntry(f"{_ROOT}backup/{original.name.rsplit('/', 1)[1]}", 4_000))
    window = _window(date(2026, 9, 1), date(2026, 9, 10))

    selection = select_files(listing, window, max_files=5)

    assert _selected(selection) == [date(2026, 9, day) for day in range(10, 5, -1)]
    assert selection.duplicates == 10
