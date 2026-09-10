"""The connector window resolver: one table of cases, and it IS the specification.

WHY A TABLE AND NOT A FILE OF NARRATIVE TESTS. Every row is one stored window read at one instant,
with the resolved window written out in full. That shape is what lets the three post-conditions be
asserted as a PROPERTY over every row rather than case by case — and the property is the part that
catches a wrong clamp ORDER, which satisfies most individual rows and breaks the ordering invariant
on exactly one of them (`_FUTURE_RANGE`: cap the end, then raise the start to the floor, and the
pair comes back inverted).

THE DATES ARE WRITTEN OUT, NOT COMPUTED. `2026-08-10` is spelled as a literal rather than as
`today - timedelta(days=connector.max_window_days - 1)`, because a test that recomputes the
production expression cannot disagree with it — an off-by-one in the resolver would be mirrored
by the same off-by-one here and the row would stay green. `test_the_cap_is_read_off_the_registry_
entry` is what ties the literals back to the registry's number.

WHAT IS DELIBERATELY NOT TESTED: a stored `window_start` after its `window_end`. The write side
validates `start <= end` (U4) and `ck_project_connectors_window_shape` pins the column shape, so
the resolver takes both as given and never re-checks them — the repo's parse-don't-validate rule.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta

import pytest

from src.core.connectors import CONNECTORS, Connector, ResolvedWindow, resolve_window
from src.db.models.connector_access import ConnectorRequestStatus
from src.db.models.project_connector import ConnectorWindowKind, ProjectConnector
from src.services.usage import ist_today

_DICE = CONNECTORS["dice"]


def _d(month: int, day: int, year: int = 2026) -> date:
    return date(year, month, day)


# 11:30 IST on 8 September 2026 — the day every row below is read on unless it says otherwise.
_ON_8_SEP = datetime(2026, 9, 8, 6, 0, tzinfo=UTC)

# The two bounds on that day, WRITTEN OUT. THE CEILING IS NOT THE READING DAY: DICE extracts
# overnight, so `freshness_lag_days = 1` and the newest day it holds on 8 September is the 7th.
# The floor is thirty calendar days counting back from THAT — 7 September minus twenty-nine days
# is 9 August — which is what `max_window_days` means measured against a ceiling that is not
# today. Every row below moved back one day when the lag landed, and each shift was a deliberate
# edit rather than a re-derivation: these are literals precisely so an off-by-one in the resolver
# cannot be mirrored here and stay green.
_EARLIEST = _d(8, 9)
_LATEST = _d(9, 7)

# The same UTC calendar day, either side of 18:30 UTC = IST midnight. The lag is applied AFTER
# the calendar day is decided, so these two rows still differ by exactly one day — they just both
# sit one day further back than they used to.
_BEFORE_IST_MIDNIGHT = datetime(2026, 9, 8, 18, 29, tzinfo=UTC)  # 23:59 IST, still the 8th
_AFTER_IST_MIDNIGHT = datetime(2026, 9, 8, 18, 31, tzinfo=UTC)  # 00:01 IST, already the 9th


def _relative(days: int) -> ProjectConnector:
    """A stored `relative` row — a preset day count, no dates, exactly as the CHECK requires."""
    return ProjectConnector(
        enabled=True,
        window_kind=ConnectorWindowKind.RELATIVE,
        window_days=days,
        window_start=None,
        window_end=None,
    )


def _absolute(start: date, end: date) -> ProjectConnector:
    """A stored `absolute` row — a fixed date pair, no day count. Stored UNCLAMPED: the bounds are
    a read-time rule, so these are whatever the citizen picked on the day they picked it."""
    return ProjectConnector(
        enabled=True,
        window_kind=ConnectorWindowKind.ABSOLUTE,
        window_days=None,
        window_start=start,
        window_end=end,
    )


@dataclass(frozen=True)
class _Case:
    """One stored window, one reading instant, and the whole window that comes back."""

    label: str
    stored: ProjectConnector
    start: date
    end: date
    days: int
    clamped: bool
    now: datetime = _ON_8_SEP
    earliest: date = _EARLIEST
    latest: date = _LATEST


_TABLE: tuple[_Case, ...] = (
    # --- the presets. A relative window is re-resolved on every read and cannot leave the bounds,
    # --- so `clamped` is false BY DEFINITION on both of these rows. Both END ON THE 7th: a preset
    # --- that still ended today would be the day-late rule applied to the calendar's greying and
    # --- not to the default path every citizen actually takes.
    _Case(
        label="Last 7 days ends at the ceiling and counts it",
        stored=_relative(7),
        start=_d(9, 1),
        end=_LATEST,
        days=7,
        clamped=False,
    ),
    _Case(
        label="Last 30 days starts exactly on the floor",
        stored=_relative(30),
        start=_EARLIEST,
        end=_LATEST,
        days=30,
        clamped=False,
    ),
    # --- an absolute pair already inside both bounds comes back untouched.
    _Case(
        label="an absolute 1-7 Sep read on 8 Sep is not clamped at all",
        stored=_absolute(_d(9, 1), _LATEST),
        start=_d(9, 1),
        end=_LATEST,
        days=7,
        clamped=False,
    ),
    # --- THE DAY-LATE CEILING, met where a citizen actually meets it: they picked a range ending
    # --- on what was then a legal date, and read it back the next morning.
    _Case(
        label="an absolute range ending today is pulled back to the ceiling",
        stored=_absolute(_d(9, 1), _d(9, 8)),
        start=_d(9, 1),
        end=_LATEST,
        days=7,
        clamped=True,
    ),
    # --- THE FLOOR.
    _Case(
        label="a start before the floor is pulled up to the floor",
        stored=_absolute(_d(8, 1), _d(9, 5)),
        start=_EARLIEST,
        end=_d(9, 5),
        days=28,
        clamped=True,
    ),
    # --- THE CEILING. Nothing reads the future, and the future now starts yesterday. Remove
    # --- step 1 and both of these go red.
    _Case(
        label="an end in the future is cut back to the ceiling",
        stored=_absolute(_d(9, 1), _d(9, 30)),
        start=_d(9, 1),
        end=_LATEST,
        days=7,
        clamped=True,
    ),
    _Case(
        label="a window entirely in the future collapses onto the ceiling",
        stored=_absolute(_d(10, 1), _d(10, 30)),
        start=_LATEST,
        end=_LATEST,
        days=1,
        clamped=True,
    ),
    # --- BOTH ENDS AT ONCE. The row that made the rail announce `Reading 144 days of flight data`.
    _Case(
        label="a whole year resolves to exactly the cap, ending at the ceiling",
        stored=_absolute(_d(1, 1), _d(12, 31)),
        start=_EARLIEST,
        end=_LATEST,
        days=30,
        clamped=True,
    ),
    # --- AGED OUT. Same length, ending at the ceiling — not widened to the cap.
    _Case(
        label="a three-day June range slides forward as three days, not thirty",
        stored=_absolute(_d(6, 10), _d(6, 12)),
        start=_d(9, 5),
        end=_LATEST,
        days=3,
        clamped=True,
    ),
    _Case(
        label="an aged-out range longer than the cap is cut to the cap",
        stored=_absolute(_d(1, 1), _d(5, 31)),
        start=_EARLIEST,
        end=_LATEST,
        days=30,
        clamped=True,
    ),
    # --- THE IST BOUNDARY. The same UTC day, two minutes apart, one calendar day apart in Asia/
    # --- Kolkata. A resolver that derived `today` from UTC would give both rows the 8th — and
    # --- therefore both rows the 7th as a ceiling.
    _Case(
        label="23:59 IST is still the 8th, so the ceiling is the 7th",
        stored=_relative(7),
        start=_d(9, 1),
        end=_LATEST,
        days=7,
        clamped=False,
        now=_BEFORE_IST_MIDNIGHT,
        earliest=_EARLIEST,
        latest=_LATEST,
    ),
    _Case(
        label="00:01 IST is already the 9th, so the ceiling is the 8th",
        stored=_relative(7),
        start=_d(9, 2),
        end=_d(9, 8),
        days=7,
        clamped=False,
        now=_AFTER_IST_MIDNIGHT,
        earliest=_d(8, 10),
        latest=_d(9, 8),
    ),
)

_IDS = [case.label for case in _TABLE]


def _stored_length(stored: ProjectConnector) -> int:
    """How many days the citizen actually stored, whichever way they stored it."""
    if stored.window_kind is ConnectorWindowKind.RELATIVE:
        assert stored.window_days is not None
        return stored.window_days
    assert stored.window_start is not None and stored.window_end is not None
    return (stored.window_end - stored.window_start).days + 1


def _resolve(case: _Case) -> ResolvedWindow:
    resolved = resolve_window(_DICE, case.stored, ConnectorRequestStatus.APPROVED, now=case.now)
    assert resolved is not None, "a stored row must always resolve to a window"
    return resolved


# --- the table -------------------------------------------------------------------------------


@pytest.mark.parametrize("case", _TABLE, ids=_IDS)
def test_every_stored_window_resolves_to_the_days_the_table_says(case: _Case) -> None:
    resolved = _resolve(case)

    assert (resolved.start, resolved.end) == (case.start, case.end)
    assert resolved.days == case.days
    assert resolved.clamped is case.clamped
    assert (resolved.earliest, resolved.latest) == (case.earliest, case.latest)
    # The kind is passed through untouched — clamping never turns an absolute pick into a preset.
    assert resolved.kind is case.stored.window_kind


# --- the post-conditions, as a property over the WHOLE table ----------------------------------
#
# These are the assertions a wrong clamp order fails. Each individual row above can be made green
# by a resolver that applies the floor before the ceiling; the ordering invariant cannot.


@pytest.mark.parametrize("case", _TABLE, ids=_IDS)
def test_the_resolved_pair_is_ordered_and_inside_both_bounds(case: _Case) -> None:
    resolved = _resolve(case)

    assert resolved.earliest <= resolved.start, "the floor"
    assert resolved.start <= resolved.end, "the pair must never invert"
    assert resolved.end <= resolved.latest, "the ceiling — nothing reads the future"


@pytest.mark.parametrize("case", _TABLE, ids=_IDS)
def test_the_ceiling_is_never_the_day_it_was_read_on(case: _Case) -> None:
    """R3, as the one property that cannot be satisfied by mirroring the resolver's arithmetic.

    Every other assertion in this file is a literal that a matching off-by-one in
    `resolve_window` could be written to agree with. This one compares against the READING
    INSTANT'S own calendar day, computed the way the resolver computes it and then not trusted
    for anything else: whatever the ceiling is, it is not today, and no window may end there.

    It is a property over the whole table because the four places `today` used to be assigned
    are reached by different rows — the presets, the clamp, and the aged-out arm — and a lag
    applied to three of them would leave exactly one row wrong."""
    resolved = _resolve(case)

    reading_day = ist_today(case.now)
    assert resolved.latest < reading_day, "the lake is a day behind: today is never the ceiling"
    assert resolved.end < reading_day, "and no resolved window may end on it"
    assert resolved.latest == reading_day - timedelta(days=_DICE.freshness_lag_days)


@pytest.mark.parametrize("case", _TABLE, ids=_IDS)
def test_the_floor_is_measured_back_from_the_ceiling_not_from_today(case: _Case) -> None:
    """The pair is derived once, from two fields, and the floor hangs off the CEILING.

    A draft that lagged only the ceiling would leave a thirty-day connector offering
    thirty-one distinct days: twenty-nine back from today, plus the ceiling. The picker greys
    against exactly these two bounds, so the span between them IS the connector's retention."""
    resolved = _resolve(case)

    assert resolved.earliest == resolved.latest - timedelta(days=_DICE.max_window_days - 1)
    assert (resolved.latest - resolved.earliest).days + 1 == _DICE.max_window_days


@pytest.mark.parametrize("case", _TABLE, ids=_IDS)
def test_no_window_is_longer_than_the_connectors_cap(case: _Case) -> None:
    resolved = _resolve(case)

    assert resolved.days == (resolved.end - resolved.start).days + 1
    assert resolved.days <= _DICE.max_window_days


@pytest.mark.parametrize("case", _TABLE, ids=_IDS)
def test_clamping_never_lengthens_what_the_citizen_stored(case: _Case) -> None:
    resolved = _resolve(case)

    assert resolved.days <= _stored_length(case.stored)


@pytest.mark.parametrize("case", _TABLE, ids=_IDS)
def test_resolving_leaves_the_callers_row_exactly_as_it_found_it(case: _Case) -> None:
    """The resolver is pure. The stored row ages out on its own; it is never rewritten."""
    before = (
        case.stored.enabled,
        case.stored.window_kind,
        case.stored.window_days,
        case.stored.window_start,
        case.stored.window_end,
    )

    _resolve(case)

    assert (
        case.stored.enabled,
        case.stored.window_kind,
        case.stored.window_days,
        case.stored.window_start,
        case.stored.window_end,
    ) == before


# --- the cap comes off the entry, not out of the module ---------------------------------------


def test_the_table_is_written_against_the_registrys_actual_numbers() -> None:
    """The literals above (9 August, 7 September, thirty days) assume DICE keeps thirty days and
    runs one day behind. If either number ever moves, this fails FIRST and points at the table,
    instead of a dozen rows failing obscurely."""
    assert _DICE.max_window_days == 30
    assert _DICE.freshness_lag_days == 1


def test_the_cap_is_read_off_the_registry_entry_it_is_handed() -> None:
    """A second connector with a different retention gets ITS number, with no change here.

    This is the row a module-level `MAX_WINDOW_DAYS = 30` would survive: every other case in this
    file is DICE's, so a hard-coded thirty passes all of them."""
    seven_day_system = Connector(
        display_name="A SHORTER-LIVED SYSTEM",
        subtitle="keeps one week",
        ask_subtitle="A SHORTER-LIVED SYSTEM keeps one week of history.",
        data_noun="test data",
        max_window_days=7,
        # A LIVE connector — nothing to wait for overnight, so its newest day IS today. This is
        # the posture the day-late rule must not impose on every connector, and the assertions
        # below are what prove it does not: they read the reading day itself as the ceiling.
        freshness_lag_days=0,
        consent_lines_requester=(),
        consent_lines_approver=(),
    )

    resolved = resolve_window(
        seven_day_system,
        _absolute(_d(1, 1), _d(12, 31)),
        ConnectorRequestStatus.APPROVED,
        now=_ON_8_SEP,
    )

    assert resolved is not None
    assert (resolved.earliest, resolved.latest) == (_d(9, 2), _d(9, 8))
    assert (resolved.start, resolved.end) == (_d(9, 2), _d(9, 8))
    assert resolved.days == 7


def test_the_freshness_lag_is_read_off_the_registry_entry_it_is_handed() -> None:
    """A second connector with a different staleness gets ITS number, with no change here.

    This is the row a module-level `FRESHNESS_LAG_DAYS = 1` would survive: every other case in
    this file is DICE's, so a hard-coded one passes all of them. Three days back, and BOTH
    bounds move with it — the ceiling because it is `today - lag`, the floor because it is
    measured from the ceiling."""
    a_slow_system = Connector(
        display_name="A SLOWER SYSTEM",
        subtitle="loads every third day",
        ask_subtitle="A SLOWER SYSTEM publishes three days behind.",
        data_noun="test data",
        max_window_days=30,
        freshness_lag_days=3,
        consent_lines_requester=(),
        consent_lines_approver=(),
    )

    resolved = resolve_window(
        a_slow_system, _relative(7), ConnectorRequestStatus.APPROVED, now=_ON_8_SEP
    )

    assert resolved is not None
    assert (resolved.earliest, resolved.latest) == (_d(8, 7), _d(9, 5))
    assert (resolved.start, resolved.end) == (_d(8, 30), _d(9, 5))
    assert resolved.days == 7


# --- is it on? -------------------------------------------------------------------------------


def test_a_project_that_never_switched_it_on_has_no_window_at_all() -> None:
    """No row is a different fact from `switched off`, and it resolves to nothing to render."""
    assert resolve_window(_DICE, None, ConnectorRequestStatus.APPROVED, now=_ON_8_SEP) is None


def test_the_switch_and_the_approval_must_both_be_true() -> None:
    enabled = _relative(7)
    switched_off = _relative(7)
    switched_off.enabled = False

    on = resolve_window(_DICE, enabled, ConnectorRequestStatus.APPROVED, now=_ON_8_SEP)
    off = resolve_window(_DICE, switched_off, ConnectorRequestStatus.APPROVED, now=_ON_8_SEP)

    assert on is not None and on.effectively_on is True
    assert off is not None and off.effectively_on is False


@pytest.mark.parametrize(
    "access",
    [
        None,  # never asked
        ConnectorRequestStatus.PENDING,
        ConnectorRequestStatus.DECLINED,
        ConnectorRequestStatus.CANCELLED,
    ],
    ids=["never-asked", "pending", "declined", "cancelled"],
)
def test_an_unapproved_owner_is_not_effectively_on_but_still_gets_a_window(
    access: ConnectorRequestStatus | None,
) -> None:
    """States c and d on `ConnectorStates` still draw a calendar chip. Losing the window here
    would render them as a broken row rather than as `Ask an administrator`."""
    resolved = resolve_window(_DICE, _relative(7), access, now=_ON_8_SEP)

    assert resolved is not None
    assert resolved.effectively_on is False
    assert (resolved.start, resolved.end) == (_d(9, 1), _LATEST)
    assert resolved.days == 7
