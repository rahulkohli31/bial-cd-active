"""The connector catalogue — the ONE module in this tree that is allowed to say DICE.

WHY THIS EXISTS. Connectors are generic by name and specific only by value (R18): the tables are
`connector_access_requests` and `project_connectors`, the enums are `connector_request_status` and
`connector_window_kind`, the routes are `/v1/connectors` and `/v1/admin/connector-requests`, and
the portal ships `IntegrationsDialog` / `ConnectorRow` / `connectorApi.ts`. DICE appears only as a
value of `connector_key` and as the literals on the one entry below. The checkable form of that
rule is a word-boundary, case-insensitive search for `dice` across `backend/src/` and
`portal/src/`: it must hit this file and nothing else. (Use a word boundary — a bare substring
search also matches `indices` in `portal/src/components/chat/ActivityGroup.tsx`.)

A MODULE CONSTANT, NOT A TABLE (R15, origin Q18). There is exactly one connector. A `connectors`
table would store display strings the boards own, would need seeding in every environment and every
test database, and would eventually want an admin CRUD screen for rows nobody is allowed to add.
The registry is code, reviewed like code, and deployed with the code that renders it.

EXACTLY ONE ENTRY, AND THAT IS THE POINT. The boards draw a second, greyed
`[ANOTHER BIAL SYSTEM]` / `Nothing else is connected to the platform yet` row as a placeholder for
a future integration. The owner ruled on 2026-09-08 that it is not built — so it is not in this
mapping either. A registry entry that nothing may be done with is exactly how the placeholder would
get back onto the screen, because every surface in this feature (the Integrations dialog's list,
the admin queue's filter pills, the rail's DATA rows) is rendered by ITERATING this mapping rather
than by naming a connector in a component. `tests/db/test_connector_models.py` pins the count at
one, and adding a second entry turns it red on purpose.

NO `available` FLAG. An earlier draft carried one so the switch-on and ask routes could refuse a
write against the greyed placeholder — a *known* key that a hand-crafted request could otherwise
name. With the placeholder gone there is no known-but-unusable key: anything outside this mapping
is unknown and 404s, which is the same guard for no field and no branch. Do not add the flag back
for a connector that does not exist yet.

EVERYTHING A CONNECTOR DIFFERS BY LIVES ON ITS ENTRY (owner ruling, 2026-09-08). Not in a
component, not in a module constant beside it. That is what makes "add a second connector" a
registry entry plus its board copy rather than a migration, a route and a component change. In
particular `max_window_days` is DICE's RETENTION, not a platform fact — the client document caps
the DICE build at the last thirty days, and another system will have its own number or none at all.
The window resolver at the foot of this module reads the cap off the entry it is already handed, so
a second connector needs no change to the resolver.

TWO CONSENT SETS, NOT ONE — READ THIS BEFORE MERGING THEM BACK TOGETHER. The plan's U1 approach
described `consent_lines` as a single tuple "the ask dialog and the decide dialog show". The boards
disagree, and the boards are the specification (R1): `AskAccess` draws `WHAT AN APPROVAL GIVES YOU`
in the second person for the citizen, and `AdminReview` draws `WHAT APPROVING GIVES THEM` in the
third person for the administrator — different voice AND different content. The administrator's
third line is the only one of the six that names the thirty-day cap, and the citizen's first line
reads `Nothing you build can change DICE data`. Collapsing the two sets would therefore drop a
promise from the approver's panel and simultaneously ship second-person copy to the approver. Both
panels are consent copy, which R1's first condition makes binding in substance, so they ship as two
fields. The literals below are byte-exact from
`docs/ux-canvas/dice/boards/{AskAccess,AdminReview}.dc.html.txt`, cross-checked against the PNGs.

THE WINDOW RESOLVER LIVES HERE TOO. Registry and resolver are both pure and share one home
(`src/core/` is where pure cross-cutting modules live — `errors.py`, `words.py`, `redaction.py` —
while `src/services/` is I/O), so a later reader does not open either expecting a session. The
resolver's `from src.services.usage import ist_today` is also why NOTHING under `src/db/models/`
imports this module: `src.services.usage` re-exports from `src.db.models.token_usage`, so a model
reaching back here would close a models → core → services → models loop. The connector-key column
width therefore lives in `src/db/models/connector_access.py`, and the test ties the two together.
The arrow this module DOES draw — core → db.models, for the two row types the resolver reads — adds
nothing to that graph: `src.services.usage` already loads the whole models package behind
`token_usage`."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from types import MappingProxyType
from typing import Final, assert_never

from src.db.models.connector_access import ConnectorRequestStatus
from src.db.models.project_connector import ConnectorWindowKind, ProjectConnector
from src.services.usage import ist_today


@dataclass(frozen=True, slots=True)
class ConsentLine:
    """One ticked line of an informed-consent panel: the bold lead and the body after it.

    A pair, not one pre-joined string, because the boards set the lead in `font-weight:700` and the
    body in the panel's ordinary grey — a joined string would force the renderer to guess the split
    at the first full stop, which the approver's `Read access to the Flight Fact Report.` breaks
    (its body starts lowercase, mid-sentence, on purpose)."""

    lead: str
    body: str


@dataclass(frozen=True, slots=True)
class Connector:
    """One connectable system. Frozen and slotted: the registry is read at import and never
    mutated, and a typo'd attribute assignment should fail loudly rather than land on the entry.

    `display_name` and `subtitle` are the two strings every connector row and every admin filter
    pill renders. `max_window_days` is the connector's own retention cap and `freshness_lag_days`
    its own staleness — the two numbers `resolve_window` derives every date from.
    The two `consent_lines_*` tuples are the two panels described in the module docblock — see it
    before considering them redundant.

    `ask_subtitle` IS NOT `subtitle` SHOUTED LOUDER. `subtitle` is the row's four-word label
    (`Airport operations`); `ask_subtitle` is the whole sentence the `AskAccess` board sets under
    its title, which says what the system holds AND that one administrator answers once for you.
    The panel that draws it cannot derive one from the other, so both ride the entry and both ride
    the wire — the alternative is a component that knows what DICE is, which is the exact thing
    R18 forbids."""

    display_name: str
    subtitle: str
    ask_subtitle: str
    # WHAT THIS CONNECTOR'S DATA IS CALLED, in the rail's two state sentences: `Reading N days of
    # {data_noun}` and `Switch it on when a chat needs {data_noun}`. It lives here for the same
    # reason `ask_subtitle` does — the sentences are the board's, but the noun inside them is this
    # connector's, and a second connector must not cost a component edit (R18). Lowercase, because
    # it always appears mid-sentence.
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
    consent_lines_requester: tuple[ConsentLine, ...]
    consent_lines_approver: tuple[ConsentLine, ...]


# The `WHAT AN APPROVAL GIVES YOU` panel on `AskAccess`, shown to the citizen who is asking.
# Second person throughout; these are promises the harness track makes true, and R1 makes them
# binding in substance — they may be shortened, they may not start meaning something else.
_DICE_CONSENT_REQUESTER: Final = (
    ConsentLine(
        lead="Read-only.",
        body="Nothing you build can change DICE data.",
    ),
    ConsentLine(
        lead="One dataset.",
        body=(
            "The Flight Fact Report — flight schedules, gates, stands and status. "
            "Nothing else in DICE."
        ),
    ),
    ConsentLine(
        lead="Every project you own.",
        body=(
            "Including ones you have not made yet. You switch it on per project, "
            "and pick the days each one reads."
        ),
    ),
)

# The `WHAT APPROVING GIVES THEM` panel on `AdminReview`, shown to the administrator deciding.
# Third person, and the third line names the thirty days — the one fact the citizen's panel does
# not carry. `test_connector_models.py` asserts that number against `max_window_days`, because two
# emitters of the same fact that can drift apart is a shape this repo has already been bitten by.
_DICE_CONSENT_APPROVER: Final = (
    ConsentLine(
        lead="Read access to the Flight Fact Report.",
        body="and nothing else in DICE.",
    ),
    ConsentLine(
        lead="Every project they own.",
        body="including ones they have not made yet. They switch it on per project.",
    ),
    ConsentLine(
        lead="Up to 30 days of history while they build.",
        body=("each project picks its own range; a published app reads the dates its users pick."),
    ),
)


# THE registry. A `MappingProxyType` rather than a plain dict so a caller cannot install an entry
# at runtime — the surfaces that iterate this are the product's whole connector list, and "the list
# is whatever somebody put in the dict" is not a reviewable claim. The key is the stored
# `connector_key` value: lowercase, stable, and never rendered (the display name is).
CONNECTORS: Final[Mapping[str, Connector]] = MappingProxyType(
    {
        "dice": Connector(
            display_name="DICE",
            subtitle="Airport operations",
            data_noun="flight data",
            ask_subtitle=(
                "DICE is BIAL’s airport operations data. An administrator decides who may "
                "read it — you are asking once, for yourself."
            ),
            # DICE's retention, not the platform's rule. See the module docblock.
            max_window_days=30,
            # The extract runs overnight, so the newest complete day in the lake is yesterday.
            freshness_lag_days=1,
            consent_lines_requester=_DICE_CONSENT_REQUESTER,
            consent_lines_approver=_DICE_CONSENT_APPROVER,
        ),
    }
)


@dataclass(frozen=True, slots=True)
class ResolvedWindow:
    """What one project actually reads from one connector, right now — the permission answer and
    the two dates, resolved together because nothing downstream is allowed to compute either again.

    `effectively_on` IS THE WHOLE PREDICATE (R12/R13): the project's switch AND the owner's
    approval. `project_connectors.enabled` on its own is only the switch position; a caller that
    spells the conjunction itself has created a second place the platform decides whether a
    connector reads, and the two will eventually disagree.

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


# A row whose columns disagree with its own `window_kind` cannot exist: U1's
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
    owner_access_state: ConnectorRequestStatus | None,
    *,
    now: datetime | None = None,
) -> ResolvedWindow | None:
    """The one place that answers "is this connector on for this project, and which days does it
    read". Pure and synchronous — the caller loads the row; this function only decides.

    THE TWO ARGUMENTS ARE THE TWO HALVES THE BOARDS KEEP APART (`DateRange`, first amber callout).
    ACCESS belongs to the person — `owner_access_state`, answered once by an administrator, for
    every project they own including the ones they have not made yet. The DAYS belong to the
    project — `stored`, because a departures board and a six-month trend want different amounts of
    history and the same person owns both. Neither is derivable from the other, which is why both
    are passed in and why no caller may answer half the question on its own.

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

    TWO HISTORICAL WRONG TURNS, PINNED HERE BECAUSE THE ORDER OF THE CLAMP IS THE WHOLE OF IT:

    1. The first draft had NO CEILING AT ALL. A stored 10 August – 31 December resolved to 144 days
       with `clamped` reading false, and the rail announced `Reading 144 days of flight data` for a
       connector that keeps thirty and holds nothing at all after its own ceiling.
    2. The correction introduced a second bug: capping `end` and THEN raising `start` to the floor
       INVERTS THE PAIR for a future-dated window — 1–30 October read on 8 September gives
       `start = 1 October`, `end = 8 September`. Step 2 (pull `start` down to the capped `end`) and
       the `else` arm of step 3 (cap the length against the RESOLVED end, not against the floor
       alone) are what make the order safe. Do not reorder them.

    The aged-out arm resolves to a window OF THE SAME LENGTH ending at `latest`, itself capped. An
    earlier "falls back to the floor" silently widened a three-day pick to thirty.

    `stored.window_start <= stored.window_end` is the writer's guarantee (U4 validates it) and is
    taken as given here — validated once at the boundary, never re-checked inward.

    `owner_access_state` is the person's derived access state, or `None` for never-asked. There is
    NO `AND not withdrawn` term: nothing in this pass can set that fact, and a permanently-true
    conjunct in the most load-bearing predicate in the feature reads as live to the next person
    who opens this file. This function also does not consult `users.suspended_at` — suspension is
    enforced fail-closed upstream at the auth seam (`src/api/deps.py`), and a second, weaker copy
    of that check here would invite someone to delete the real one."""
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

    effectively_on = stored.enabled and owner_access_state is ConnectorRequestStatus.APPROVED

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

        # 1. THE CEILING, FIRST. Nothing reads the future. (Wrong turn 1.)
        end = min(stored_end, latest)
        # 2. A start that is now after the end collapses onto it. (Wrong turn 2 — this is the step
        #    whose absence inverts a future-dated pair.)
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
        effectively_on=effectively_on,
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
    window says whether this project may read it at all (`effectively_on` — the switch AND the
    owner's approval, see `ResolvedWindow`). Neither half is useful alone: a display name with no
    permission behind it is a row the citizen should never have been shown, and a permission with
    no name is something the prompt cannot mention.

    RESOLVED AT THE ROUTER, READ IN TWO PLACES. The turn's prompt names what is connected, and the
    turn's tool surface registers the schema tool for exactly the same set. They read ONE value,
    so they cannot disagree about whether this project reads this system — which is the property
    that makes "the tool is absent, not refused" true rather than aspirational.

    NOTHING HERE REACHES THE MODEL BUT THE TWO NAMES. `window` is carried so a tool can fail
    first on a system that came back not-effectively-on; its DATES are deliberately never
    rendered into a prompt (owner ruling, 2026-09-10). The window is a portal and approval
    concept — the code an agent writes reads the lake directly, for whatever dates the app's own
    users pick — so telling the model about a thirty-day sample would describe a constraint that
    does not exist and that nothing it writes would honour."""

    key: str
    connector: Connector
    window: ResolvedWindow
