"""From a date pair to a file set — the pure function that decides which files a window selects.

DELIBERATELY NARROW, AND THAT IS THE WHOLE DESIGN. It takes a listing as `(name, size)` pairs, the
resolved window, and a ceiling on how many files may come back. No Azure, no network, no clock.
Every trap the real lake sets is therefore a table-driven unit test that runs on a machine with no
Azure access at all — which is the only reason this feature has deterministic coverage.

WHAT A WINDOW MEANS. The files whose OWN DATE falls inside it. Nothing else. Unreadable dates are
skipped and counted; the count is NEVER topped up by reaching further back. So a run of failed
loads makes a window SMALLER, not OLDER, and the skipped count is the only visible trace of a bad
stretch. (This supersedes an earlier reading in which stubs "must not consume one of the thirty" —
a stub costs a file, deliberately. The lake holds one file per date, so on a healthy stretch a
thirty-day window simply IS thirty files and the two readings agree; they diverge only at a gap.)

THE ORDER OF THE FIRST TWO STEPS IS THE MOST IMPORTANT LINE IN THIS MODULE. Match the NAME, then
look at the size. A hierarchical-namespace account reports its DIRECTORIES in a flat listing as
zero-length entries, so filtering on size first drops four folders into the skipped count — where
they read as four days the lake failed to load — AND hides the files that really are broken.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Final

from src.core.connectors import ResolvedWindow
from src.services.lake.client import LakeEntry

# THE ONE CONNECTOR-SPECIFIC LITERAL IN THIS PACKAGE, and it is a literal on purpose.
#
# It is the object-name shape the lake actually produces, measured against the real container:
# `<prefix>/<year>/<UPPERCASE MONTH>/tb_flight_fact_report_<YYYYMMDD>.parquet`. It is not injected
# through configuration, because indirection here would buy nothing — the repository is treated as
# private by standing ruling, so hiding the name is not a goal, and a second connector will have
# its own reader rather than its own regex for this one.
#
# THE LEFT ANCHOR IS LOAD-BEARING. The verified worked example's regex has none, so
# `old_tb_flight_fact_report_20260901.parquet` matches it as a suffix. Anchoring on a path-segment
# boundary is what makes a near-miss a non-match rather than a file the parquet reader fails on
# later, inside the generated app, with no trace of where it came from.
#
# THE DATE IN THE NAME IS THE **LOAD** DATE, and the enclosing month folder is the load MONTH —
# the two disagree for every file loaded on the first of a month (the 31 August file lives under
# `2026/SEPTEMBER/`). Selection reads the filename and ignores the folder, which is also why a
# path constructed from a requested date finds nothing and reads as a permissions fault.
LAKE_FILE_PATTERN: Final = re.compile(r"(?:^|/)tb_flight_fact_report_(?P<day>\d{8})\.parquet$")


@dataclass(frozen=True, slots=True)
class SelectedFile:
    """One file a window selected, with the date it was read OUT OF THE NAME rather than derived
    from its folder or from a clock."""

    name: str
    size: int
    day: date


@dataclass(frozen=True, slots=True)
class WindowSelection:
    """What a window actually resolves to, and the two numbers a caller needs to act on it.

    `files` is newest-first, which is the order a caller copying under a byte budget wants: if
    something has to be dropped it should be the oldest day, not an arbitrary one.

    `skipped` counts the days INSIDE the window that matched a filename and were zero bytes — a
    failed upstream load. It answers "how many days could this window not read", so a broken day
    from three months ago is not one of them, and a folder is never one at all.

    `total_bytes` is the sum of what was SELECTED — the number a byte budget is checked against,
    reported so the transfer can refuse before it writes rather than after.

    `duplicates` counts extra files carrying a date another file in this window already carried.
    It should always be zero, and it is reported precisely because a non-zero value means the
    lake's shape stopped matching the one-file-per-date premise the `max_files` ceiling rests
    on — a condition that would otherwise show up only as a window covering half the days it
    claims, with nothing anywhere saying so."""

    files: tuple[SelectedFile, ...]
    skipped: int
    total_bytes: int
    duplicates: int = 0


def _day_in_name(name: str) -> date | None:
    """The load date a file name carries, or `None` when the name is not one of our files.

    An eight-digit run that is not a real date (`20260931`) reads as `None` rather than raising:
    it cannot be placed in a window, so it is dropped like any other non-file instead of failing
    the whole selection for every other day."""
    match = LAKE_FILE_PATTERN.search(name)
    if match is None:
        return None
    stamp = match.group("day")
    try:
        return date(int(stamp[:4]), int(stamp[4:6]), int(stamp[6:]))
    except ValueError:
        return None


def select_files(
    listing: list[LakeEntry] | tuple[LakeEntry, ...],
    window: ResolvedWindow,
    *,
    max_files: int,
) -> WindowSelection:
    """The files `window` selects out of `listing`, newest first, with the days it could not read.

    `max_files` is a CEILING, not a target. One file per date and a window the resolver has
    already clamped to the connector's retention means it cannot bite in production; it is the
    guard for the day that stops being true, and it takes the newest rather than whatever the
    listing happened to put first.

    THE STEPS, IN THIS ORDER, AND THE ORDER IS NOT INTERCHANGEABLE:

    1. Read the date out of the NAME. Anything without one is not our file — a directory
       placeholder, a `_SUCCESS` marker, a near-miss — and is dropped silently. It is emphatically
       NOT counted as broken.
    2. Is that date inside the window? If not, drop it silently. This runs BEFORE the size check
       so that `skipped` counts days this window could not read, rather than every stub the
       container has ever held.
    3. Zero bytes is a FAILED LOAD, not a folder: excluded, and counted.
    4. ONE FILE PER DATE. The pattern anchors on `(?:^|/)`, so a copy of a day sitting in a
       sub-folder under the prefix — `archive/`, `backup/`, anything a human made — matches
       exactly as the live file does and carries the same date. Left alone, a lake with such a
       folder yields two entries per day, the `max_files` ceiling covers half the days it claims,
       and every row of that day is counted twice by whatever reads the copy.

       THE WINNER IS THE LEXICOGRAPHICALLY SMALLEST NAME, AND THAT IS AN ARBITRARY RULE ON
       PURPOSE. There is no principled winner: the real files live under `YYYY/MONTH/` and are
       DEEPER than a copy dropped beside them, so "shallowest is canonical" is exactly backwards
       here; "largest wins" would prefer an archived full copy over a live truncated one, which
       may or may not be right. What actually matters is that the same lake yields the same
       window on every read, so the rule is chosen for determinism and nothing else — and the
       losers are COUNTED, so a `duplicates` above zero sends somebody to look at the container
       rather than leaving the platform to guess well.
    5. Sort newest first and cap.
    """
    best: dict[date, SelectedFile] = {}
    skipped = 0
    duplicates = 0
    for name, size in listing:
        day = _day_in_name(name)
        if day is None:
            continue
        if not (window.start <= day <= window.end):
            continue
        if size == 0:
            skipped += 1
            continue
        candidate = SelectedFile(name=name, size=size, day=day)
        held = best.get(day)
        if held is None:
            best[day] = candidate
            continue
        duplicates += 1
        if candidate.name < held.name:
            best[day] = candidate

    selected = sorted(best.values(), key=lambda item: item.day, reverse=True)
    kept = tuple(selected[:max_files])
    return WindowSelection(
        files=kept,
        skipped=skipped,
        total_bytes=sum(item.size for item in kept),
        duplicates=duplicates,
    )
