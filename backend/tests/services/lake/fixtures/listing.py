"""The trap table: one fixture row per way the real lake produces a wrong answer quietly.

WHAT THIS IS FOR. Every entry below was measured against a structurally faithful replica of the
flight lake, and each one encodes a defect that produces an app which builds green and reports
the wrong number — or reads nothing at all and looks like a permissions failure. Written out as
data, with the trap NAMED on every row, so a reader can check the coverage against the profile
without running anything and without an Azure account.

WHY IT LIVES BESIDE THE TESTS RATHER THAN INSIDE THEM. `test_window.py` builds its cases from
generators because it is asserting arithmetic over ranges. This table is the other half: the
handful of SHAPES that only a real listing produces, kept in one place so the live check
(`test_lake_live.py`) can assert that the replica still produces them. A trap that quietly stops
existing upstream is a test that passes forever while protecting nothing.

NOTHING HERE NAMES A REAL ACCOUNT, a client id or an internal path. The container and folder
conventions are the client's and are committed deliberately (the repository is treated as
private); the account name is configuration and appears in no tracked file.
"""

from __future__ import annotations

from typing import Final, NamedTuple

from src.services.lake.client import LakeEntry

#: The folder convention, and the one thing about it that matters: the month folder is the LOAD
#: month, not the month of the data inside.
ROOT: Final = "AOS/tb_flight_fact_report/"


class Trap(NamedTuple):
    """One listing entry and the mistake it exists to catch."""

    entry: LakeEntry
    trap: str


def _file(name: str, size: int, trap: str) -> Trap:
    return Trap(LakeEntry(f"{ROOT}{name}", size), trap)


#: The four listing-observable traps, in the order a reader meets them.
TRAPS: Final[tuple[Trap, ...]] = (
    _file(
        "2026/SEPTEMBER/tb_flight_fact_report_20260831.parquet",
        4_012_003,
        "THE MONTH FOLDER IS THE LOAD MONTH. The 31 August file lives under SEPTEMBER, because "
        "the load job ran the next morning. A path constructed from the requested date finds "
        "nothing, and finding nothing reads exactly like a permissions failure.",
    ),
    _file(
        "2026/SEPTEMBER",
        0,
        "A DIRECTORY PLACEHOLDER. A hierarchical-namespace account returns its directories in a "
        "flat listing as zero-length entries. Filter on size before name and four folders land "
        "in the broken-file count, where they read as four days the lake failed to load.",
    ),
    _file(
        "2026/SEPTEMBER/tb_flight_fact_report_20260902.parquet",
        0,
        "A ZERO-BYTE STUB — the upstream load failed and left one. An existence check passes and "
        "the parquet reader then throws 'file is too short'. It is a day of flights nobody will "
        "get, and it must be counted rather than silently treated as a quiet day.",
    ),
    _file(
        "2026/SEPTEMBER/_SUCCESS",
        0,
        "A MARKER FILE. Zero bytes and not a directory, so neither a size rule nor a folder rule "
        "excludes it — only matching the file NAME does.",
    ),
)


def a_sparse_calendar() -> tuple[LakeEntry, ...]:
    """A listing spanning three months with gaps — the shape the replica actually holds.

    THE CALENDAR IS SPARSE, and that is not a fault to be corrected: there are days with no file
    at all. A reader that computes "yesterday" and fetches it renders an empty chart with nothing
    to explain it, which is why the newest readable day is derived from the listing instead."""
    days = (
        [(7, day) for day in range(1, 13)]
        + [(8, day) for day in (1, 2, 3, 10, 11, 12, 20, 25, 31)]
        + [(9, day) for day in range(1, 8)]
    )
    return tuple(
        LakeEntry(
            f"{ROOT}2026/{'SEPTEMBER' if month == 9 else 'AUGUST' if month == 8 else 'JULY'}"
            f"/tb_flight_fact_report_2026{month:02d}{day:02d}.parquet",
            4_000_000,
        )
        for month, day in days
    )


def the_trap_listing() -> tuple[LakeEntry, ...]:
    """Every trap entry, in listing order."""
    return tuple(trap.entry for trap in TRAPS)
