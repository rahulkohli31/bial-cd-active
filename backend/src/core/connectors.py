"""The connector catalogue — the ONE module in this tree that is allowed to say DICE.

WHY THIS EXISTS. Connectors are generic by name and specific only by value: the table is
`project_connectors`, the enum is `connector_window_kind`, the routes hang off
`/v1/projects/{project_id}/connectors`, and the portal ships `IntegrationsTab` /
`ProjectConnectorRow` / `connectorApi.ts`. DICE appears only as a value of `connector_key`; the
entry below renders as Flight Fact Data. The checkable form of that rule is a word-boundary,
case-insensitive search for `dice` across `backend/src/` and `portal/src/`: it must hit this file
and nothing else. (Use a word boundary — a bare substring search also matches `indices` in
`portal/src/components/chat/ActivityGroup.tsx`.)

A MODULE CONSTANT, NOT A TABLE. There is exactly one connector. A `connectors`
table would store display strings the boards own, would need seeding in every environment and every
test database, and would eventually want an admin CRUD screen for rows nobody is allowed to add.
The registry is code, reviewed like code, and deployed with the code that renders it.

EXACTLY ONE ENTRY, AND THAT IS THE POINT. The boards draw a second, greyed
`[ANOTHER BIAL SYSTEM]` / `Nothing else is connected to the platform yet` row as a placeholder for
a future integration. The owner ruled that it is not built — so it is not in this
mapping either. A registry entry that nothing may be done with is exactly how the placeholder would
get back onto the screen, because every surface in this feature (the Settings › Integrations list,
and the connected systems a turn's prompt names) is rendered by ITERATING this mapping rather than
by naming a connector in a component. `tests/db/test_connector_models.py` pins the count at one,
and adding a second entry turns it red on purpose.

NO `available` FLAG. There is no known-but-unusable key: anything outside this mapping is unknown
and 404s, which is the same guard for no field and no branch. Do not add a flag for a connector
that does not exist yet.

EVERYTHING A CONNECTOR DIFFERS BY LIVES ON ITS ENTRY. Not in a
component, not in a module constant beside it. That is what makes "add a second connector" a
registry entry plus its board copy rather than a migration, a route and a component change. In
particular `max_window_days` is DICE's RETENTION, not a platform fact — the client document caps
the DICE build at the last thirty days, and another system will have its own number or none at all.
The window resolver at the foot of this module reads the cap off the entry it is already handed, so
a second connector needs no change to the resolver.

THE WINDOW RESOLVER LIVES HERE TOO. Registry and resolver are both pure and share one home
(`src/core/` is where pure cross-cutting modules live — `errors.py`, `words.py`, `redaction.py` —
while `src/services/` is I/O), so a later reader does not open either expecting a session. The
resolver's `from src.services.usage import ist_today` is also why NOTHING under `src/db/models/`
imports this module: `src.services.usage` re-exports from `src.db.models.token_usage`, so a model
reaching back here would close a models → core → services → models loop. The connector-key column
width therefore lives in `src/db/models/project_connector.py`, and the test ties the two together.
The arrow this module DOES draw — core → db.models, for the row and the enum the resolver reads —
adds nothing to that graph: `src.services.usage` already loads the whole models package behind
`token_usage`."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from types import MappingProxyType
from typing import Final, assert_never

from src.db.models.project_connector import ConnectorWindowKind, ProjectConnector
from src.services.usage import ist_today


@dataclass(frozen=True, slots=True)
class Connector:
    """One connectable system. Frozen and slotted: the registry is read at import and never
    mutated, and a typo'd attribute assignment should fail loudly rather than land on the entry.

    `display_name` and `subtitle` are the system's name and its four-word label (`Airport
    operations`); the turn's prompt names a connected system with both. `max_window_days` is the
    connector's own retention cap and `freshness_lag_days` its own staleness — the two numbers
    `resolve_window` derives every date from."""

    display_name: str
    subtitle: str
    # WHAT THIS CONNECTOR'S DATA IS CALLED, in the settings row's two state sentences: `Reading N
    # days of {data_noun}` and `Switch it on when a chat needs {data_noun}`. The sentences are the
    # board's, but the noun inside them is this connector's, and a second connector must not cost
    # a component edit. Lowercase, because it always appears mid-sentence.
    data_noun: str
    max_window_days: int
    # HOW FAR BEHIND TODAY THIS CONNECTOR'S NEWEST DATA IS, in whole days. The same KIND of fact
    # `max_window_days` is — a property of the CONNECTED SYSTEM, not of the platform — and it
    # rides the entry for the same reason: DICE extracts overnight, so its newest complete day is
    # yesterday, while another system may be live and declare zero. `resolve_window` derives its
    # ceiling from this, which is what makes today disappear from the picker, from every resolved
    # window and from the days a generated app may draw against, all from one number.
    #
    # NOT A MODULE CONSTANT, and not three of them. A `FRESHNESS_LAG_DAYS = 1` beside this class
    # is the thing the next reader "corrects" in one of the three places `resolve_window` needs
    # it, and a lag applied to the presets but not to the ceiling is a picker that greys out
    # today while `Last 7 days` still ends on it.
    #
    # A CEILING, NOT A PROMISE. The code never OFFERS today; it also never assumes yesterday's
    # file exists. Loads fail and leave stubs and the calendar is sparse, so the newest readable
    # day is whatever the lake's own listing says — which is why the worked example in the golden
    # template derives that for itself rather than computing it from a clock.
    freshness_lag_days: int


# THE registry. A `MappingProxyType` rather than a plain dict so a caller cannot install an entry
# at runtime — the surfaces that iterate this are the product's whole connector list, and "the list
# is whatever somebody put in the dict" is not a reviewable claim. The key is the stored
# `connector_key` value: lowercase, stable, and never rendered (the display name is).
CONNECTORS: Final[Mapping[str, Connector]] = MappingProxyType(
    {
        "dice": Connector(
            display_name="Flight Fact Data",
            subtitle="Airport operations",
            data_noun="flight data",
            # DICE's retention, not the platform's rule. See the module docblock.
            max_window_days=30,
            # The extract runs overnight, so the newest complete day in the lake is yesterday.
            freshness_lag_days=1,
        ),
    }
)


@dataclass(frozen=True, slots=True)
class ResolvedWindow:
    """What one project actually reads from one connector, right now — the permission answer and
    the two dates, resolved together because nothing downstream is allowed to compute either again.

    `effectively_on` IS THE WHOLE PREDICATE: the project's switch. It is answered here, beside the
    days, so a caller that wants to know whether a connector reads never decides it for itself —
    a second place the platform decides that is a second place that will eventually disagree.

    `earliest` and `latest` are RETURNED, not merely used. The calendar grid greys its dates out
    against exactly the two bounds the clamp applied, so a citizen in Bangalore picking the
    earliest enabled date at 18:35 IST cannot be refused a date the grid had just drawn as
    selectable.

    `days` is the resolved length — `(end - start) + 1` AFTER clamping — because it is what the
    rail announces (`Reading N days of flight data`), and the number in that sentence has to be
    the number of days the app can actually see."""

    effectively_on: bool
    kind: ConnectorWindowKind
    start: date
    end: date
    days: int
    clamped: bool
    earliest: date
    latest: date


# A row whose columns disagree with its own `window_kind` cannot exist:
# `ck_project_connectors_window_shape` refuses it at the database. The two guards below are both
# the type narrowing and the honest failure if that constraint is ever dropped — a resolver that
# quietly substituted a default would hand the citizen a window nobody chose, worse than a 500.
_SHAPE_VIOLATION: Final = (
    "a project_connectors row disagrees with its window_kind — "
    "ck_project_connectors_window_shape should have refused the write"
)


def resolve_window(
    connector: Connector,
    stored: ProjectConnector | None,
    *,
    now: datetime | None = None,
) -> ResolvedWindow | None:
    """The one place that answers "is this connector on for this project, and which days does it
    read". Pure and synchronous — the caller loads the row; this function only decides.

    BOTH ANSWERS BELONG TO THE PROJECT. `stored` carries the switch and the days, because a
    departures board and a six-month trend want different amounts of history and the same person
    owns both.

    Returns `None` when `stored` is `None`, which is a legitimately-absent result rather than an
    error: no row means this connector was never switched on for this project, a different fact
    from `enabled = false`, and there is no window to render for it.

    NOTHING IS EVER WRITTEN BACK. A stored window is kept exactly as the citizen picked it and the
    bounds are applied on every READ, so a window ages out on its own instead of a stored value
    slowly becoming a lie. The pair the citizen sees is therefore allowed to differ from the pair
    the database holds — `clamped` is how the chip says so.

    THE THIRTY IS THE CONNECTOR'S, NOT THE PLATFORM'S. `max_window_days` is read off the entry this
    function is handed (DICE's retention: the client document caps the BUILD at the last thirty
    days — "enough real data to build and prove a dashboard, and no more" — and it is NOT a cap on
    the published app, which reads whatever dates its own users pick). A second connector with a
    different retention needs no change here, which is why there is no module constant to find.

    BOTH BOUNDS COME OFF THAT ONE NUMBER, IN ONE PLACE. An earlier draft wrote `FLOOR_DAYS = 30`
    and then `today - 29` — an off-by-one waiting to be "corrected" by the next reader who noticed
    the two numbers differ.

    THE CEILING IS NOT TODAY, AND THAT IS THE CONNECTOR'S FACT TOO. `freshness_lag_days` says how
    far behind the connected system runs; `latest` is `today` minus it, and EVERY end in this
    function is `latest`. There are four such places, not one — the pair above, the `RELATIVE`
    arm (the presets, which is the default UI path), and the aged-out arm — and leaving any of
    them as `today` ships a picker that greys out a day the presets still resolve to. The
    calendar grid greys its dates against the returned `earliest`/`latest`, so the popover
    inherits the rule with no change of its own.

    THE ORDER OF THE CLAMP IS THE WHOLE OF IT: cap `end` at `latest` first, then pull `start` down
    to that capped `end`, then raise it to the floor and cap the length against the RESOLVED `end`.
    Skipping the second step inverts the pair for a future-dated window.

    The aged-out arm resolves to a window OF THE SAME LENGTH ending at `latest`, itself capped.

    `stored.window_start <= stored.window_end` is the writer's guarantee (`AbsoluteWindowChoice`
    validates it) and is taken as given here — validated once at the boundary, never re-checked
    inward.

    This function does not consult `users.suspended_at` — suspension is enforced fail-closed
    upstream at the auth seam (`src/api/deps.py`), and a second, weaker copy of that check here
    would invite someone to delete the real one."""
    today = ist_today(now)
    # THE PAIR, AND `today` IS NOT HALF OF IT. The ceiling is the newest day the connector
    # actually holds — today minus its own freshness lag — and the floor is measured back from
    # THAT, not from today, so a thirty-day window is thirty days the lake can answer for rather
    # than twenty-nine plus a day that does not exist yet.
    #
    # `- 1` because the floor is INCLUSIVE of the ceiling: thirty calendar days counting the
    # ceiling ends twenty-nine days before it, not thirty. Named together and derived from one
    # field each so a reader cannot take one without the other.
    #
    # `today` SURVIVES AS A LOCAL AND IS NEVER AN ANSWER. Every place that used to assign it as
    # an `end` now assigns `latest`; it exists only as the input the lag is measured from. If a
    # fifth use ever appears, that is the bug this comment is here to catch.
    latest = today - timedelta(days=connector.freshness_lag_days)
    earliest = latest - timedelta(days=connector.max_window_days - 1)

    if stored is None:
        return None

    if stored.window_kind is ConnectorWindowKind.RELATIVE:
        if stored.window_days is None:
            raise ValueError(_SHAPE_VIOLATION)
        # A preset is re-resolved from the CEILING on every read, so `Last 7 days` still means the
        # last seven days the connector holds a month later — and never includes today. It cannot
        # leave the bounds, so `clamped` is FALSE BY DEFINITION here, not "false because we
        # checked" — a day count is not a date pair, so "the stored and the resolved values
        # differ" is not a question that can be asked of it. The portal keys on that flag (it
        # pre-selects the RESOLVED range in the popover), so do not make it truthy here to signal
        # something else. The one premise: every preset the write side offers is
        # within the connector's own cap. That holds for `{7, 14, 30}` against DICE's thirty; a
        # connector whose retention is SHORTER than a preset would need the preset set derived from
        # `max_window_days` at the write, which is where the choice is made.
        start = latest - timedelta(days=stored.window_days - 1)
        end = latest
        clamped = False
    elif stored.window_kind is ConnectorWindowKind.ABSOLUTE:
        stored_start, stored_end = stored.window_start, stored.window_end
        if stored_start is None or stored_end is None:
            raise ValueError(_SHAPE_VIOLATION)

        # 1. THE CEILING, FIRST. Nothing reads the future.
        end = min(stored_end, latest)
        # 2. A start that is now after the end collapses onto it — the step whose absence
        #    inverts a future-dated pair.
        start = min(stored_start, end)
        if end < earliest:
            # 3a. AGED OUT ENTIRELY — the whole pick is older than the connector keeps. Slide it
            #     forward to the SAME LENGTH ending at the CEILING rather than widening it to the
            #     cap: a three-day pick from June is three days, not thirty.
            length = min((stored_end - stored_start).days + 1, connector.max_window_days)
            end = latest
            start = end - timedelta(days=length - 1)
        else:
            # 3b. THE FLOOR, then the length cap — the latter against the RESOLVED `end`, which is
            #     what keeps `start <= end` true for a pair whose end was just pulled back.
            start = max(start, earliest)
            start = max(start, end - timedelta(days=connector.max_window_days - 1))
        # The citizen is told the dates moved whenever EITHER end moved. Compared against the pair
        # as stored, not against an intermediate — step 2 alone changes `start` on a pair that step
        # 3 then leaves alone.
        clamped = (start, end) != (stored_start, stored_end)
    else:
        assert_never(stored.window_kind)

    return ResolvedWindow(
        effectively_on=stored.enabled,
        kind=stored.window_kind,
        start=start,
        end=end,
        days=(end - start).days + 1,
        clamped=clamped,
        earliest=earliest,
        latest=latest,
    )


@dataclass(frozen=True, slots=True)
class ConnectedSystem:
    """One connector a project may ACTUALLY read, resolved once and carried for the whole turn.

    WHY THE PAIR TRAVELS TOGETHER. The registry entry says what the system is called; the resolved
    window says whether this project may read it at all (`effectively_on` — the project's switch,
    see `ResolvedWindow`). Neither half is useful alone: a display name with no permission behind
    it is a row the citizen should never have been shown, and a permission with no name is
    something the prompt cannot mention.

    RESOLVED AT THE ROUTER, READ IN TWO PLACES. The turn's prompt names what is connected, and the
    turn's tool surface registers the schema tool for exactly the same set. They read ONE value,
    so they cannot disagree about whether this project reads this system — which is the property
    that makes "the tool is absent, not refused" true rather than aspirational.

    NOTHING HERE REACHES THE MODEL BUT THE TWO NAMES. `window` is carried so a tool can fail
    first on a system that came back not-effectively-on; its DATES are deliberately never
    rendered into a prompt. The window is a portal concept — the code an agent writes reads the
    lake directly, for whatever dates the app's own users pick — so telling the model about a
    thirty-day sample would describe a constraint that does not exist and that nothing it writes
    would honour."""

    key: str
    connector: Connector
    window: ResolvedWindow
