"""The trap table, asserted end to end — and the claim that none of it needs Azure.

THIS IS THE COVERAGE LEDGER. `test_window.py` proves each rule with cases built for that rule;
this file runs the SAME resolver over the fixture table, whose rows are one-per-trap and carry the
trap's description on them. The point of the duplication is not the assertions — it is that a
reader (or a reviewer holding the measured profile) can check the coverage by reading
`fixtures/listing.py` and this file, without running anything.

It is also the file the live check mirrors: `test_lake_live.py` asserts the REPLICA still produces
these shapes. A trap that quietly stops existing upstream is a test that passes forever while
protecting nothing, and only a live run can notice.
"""

from __future__ import annotations

import os
from datetime import date

from src.core.connectors import CONNECTORS, ResolvedWindow
from src.db.models.project_connector import ConnectorWindowKind
from src.services.lake.window import select_files
from tests.services.lake.fixtures.listing import TRAPS

_DICE = CONNECTORS["dice"]


def _window(start: date, end: date) -> ResolvedWindow:
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


def test_every_trap_in_the_table_names_the_mistake_it_encodes() -> None:
    """The table is a document as much as a fixture. A row whose description went missing is a
    row a reviewer cannot check against the measured profile."""
    assert len(TRAPS) == 4
    for trap in TRAPS:
        assert len(trap.trap) > 80, f"{trap.entry.name} has no usable description"


def test_the_whole_trap_listing_resolves_to_exactly_one_file(trap_listing) -> None:
    """★ FOUR ENTRIES IN, ONE FILE OUT, AND ONE COUNTED AS BROKEN.

    The 31 August file is selected despite living under the SEPTEMBER folder. The directory and
    the `_SUCCESS` marker are dropped by the name match and are NOT counted as broken. The
    zero-byte stub matches the name, so it IS counted. Every one of those four outcomes is a
    different rule, and getting any of them wrong produces a plausible-looking answer."""
    selection = select_files(
        trap_listing, _window(date(2026, 8, 25), date(2026, 9, 7)), max_files=30
    )

    assert [item.day for item in selection.files] == [date(2026, 8, 31)]
    assert "SEPTEMBER" in selection.files[0].name
    assert selection.skipped == 1, "the stub, and ONLY the stub"


def test_a_sparse_calendar_never_reaches_outside_the_window(sparse_calendar) -> None:
    """Three months of real gaps against a thirty-day window. Whatever few days fall inside is
    the answer — the window never widens to make a count whole."""
    selection = select_files(
        sparse_calendar, _window(date(2026, 8, 20), date(2026, 9, 7)), max_files=30
    )

    assert [item.day for item in selection.files] == [
        date(2026, 9, day) for day in range(7, 0, -1)
    ] + [date(2026, 8, 31), date(2026, 8, 25), date(2026, 8, 20)]
    assert selection.skipped == 0, "a day with no file at all is not a broken day"


def test_the_deterministic_lane_needs_no_azure_credential(
    no_azure_environment: None, trap_listing, sparse_calendar
) -> None:
    """★ ASSERTED BY RUNNING IT THAT WAY, not by assuming it.

    Every `AZURE_*`, `MSI_*` and `IDENTITY_*` name is removed from the environment first. The
    resolver sees only names and sizes, so this passes on a laptop with no Azure account, in CI,
    and on the day the trial subscription behind the replica lapses. That is the whole reason
    `select_files` takes a list rather than a client."""
    assert not [name for name in os.environ if name.startswith(("AZURE_", "MSI_", "IDENTITY_"))]

    window = _window(date(2026, 7, 1), date(2026, 9, 7))
    assert select_files(trap_listing, window, max_files=30).files
    assert select_files(sparse_calendar, window, max_files=30).files
